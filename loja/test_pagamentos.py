import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import patch

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .checkout import realizar_checkout
from .models import Categoria, ItemPedido, Pagamento, Pedido, Produto


@override_settings(
    STRIPE_SECRET_KEY="sk_test_mock",
    STRIPE_WEBHOOK_SECRET="whsec_mock",
    STRIPE_PUBLIC_ORIGIN="http://127.0.0.1:8000",
)
class PagamentoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        usuarios = get_user_model().objects
        cls.comprador = usuarios.create_user(username="comprador")
        cls.outro = usuarios.create_user(username="outro")
        cls.admin = usuarios.create_superuser(username="admin")
        categoria = Categoria.objects.create(nome="Livros")
        cls.produto = Produto.objects.create(
            vendedor=cls.outro, categoria=categoria, nome="Livro", preco="12.35", estoque=9,
        )
        cls.pedido = realizar_checkout(cls.comprador, {str(cls.produto.pk): 2})
        Produto.objects.filter(pk=cls.produto.pk).update(preco="999.00")
        cls.pedido_alheio = Pedido.objects.create(comprador=cls.outro)

    def setUp(self):
        self.pagar = reverse("loja:pagar_pedido", args=[self.pedido.pk])
        self.historico = reverse("loja:historico_pedidos")
        self.aprovado = reverse("loja:pagamento_aprovado", args=[self.pedido.pk])
        self.cancelado = reverse("loja:pagamento_cancelado", args=[self.pedido.pk])
        self.webhook = reverse("loja:webhook_stripe")
        self.mock_client = self.enterContext(patch("loja.pagamentos.stripe.StripeClient"))
        self.sessoes = self.mock_client.return_value.v1.checkout.sessions
        self.sessoes.create.side_effect = self.criar_sessao

    def criar_sessao(self, dados, options):
        return stripe.checkout.Session.construct_from({
            "id": f"cs_test_{dados['metadata']['vitrine_pagamento_id']}", "object": "checkout.session",
            "livemode": False, "mode": "payment", "status": "open", "payment_status": "unpaid",
            "client_reference_id": dados["client_reference_id"], "metadata": dados["metadata"],
            "currency": "brl", "amount_total": dados["line_items"][0]["price_data"]["unit_amount"],
            "url": "https://checkout.stripe.com/c/pay/mock",
        }, None)

    def abrir(self, **dados):
        self.client.force_login(self.comprador)
        return self.client.post(self.pagar, dados)

    def montar_evento(self, **campos_sessao):
        pagamento = self.pedido.pagamentos.latest("criado_em")
        return {
            "id": "evt_validado", "object": "event", "livemode": False, "type": "checkout.session.completed",
            "data": {"object": {
                "id": pagamento.sessao_stripe or f"cs_test_{pagamento.pk}", "object": "checkout.session",
                "livemode": False, "mode": "payment", "status": "complete", "payment_status": "paid",
                "client_reference_id": str(self.pedido.pk), "metadata": {"vitrine_pagamento_id": str(pagamento.pk)},
                "amount_total": 2470, "currency": "brl", "payment_intent": "pi_validado", **campos_sessao,
            }},
        }

    def enviar_evento(self, evento, *, segredo=None, atraso=0):
        dados = json.dumps(evento).encode()
        timestamp = int(time.time()) - atraso
        assinatura = hmac.new(
            (segredo or settings.STRIPE_WEBHOOK_SECRET).encode(),
            f"{timestamp}.".encode() + dados, hashlib.sha256,
        ).hexdigest()
        return self.client.post(
            self.webhook, dados, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={timestamp},v1={assinatura}",
        )

    def test_autenticacao_propriedade_metodo_e_csrf(self):
        self.assertEqual(self.client.post(self.pagar).status_code, 302)
        self.assertEqual(self.client.get(self.historico).status_code, 302)
        self.client.force_login(self.outro)
        self.assertEqual(self.client.post(self.pagar).status_code, 404)
        self.assertEqual(self.client.get(self.aprovado).status_code, 404)
        self.assertEqual(self.client.get(self.cancelado).status_code, 404)
        self.client.force_login(self.comprador)
        self.assertEqual(self.client.get(self.pagar).status_code, 405)
        cliente = Client(enforce_csrf_checks=True)
        cliente.force_login(self.comprador)
        self.assertEqual(cliente.post(self.pagar).status_code, 403)
        self.mock_client.assert_not_called()
        self.assertFalse(Pagamento.objects.exists())

    def test_criacao_ignora_valores_do_frontend_e_usa_preco_historico(self):
        resposta = self.abrir(total="0.01", quantidade=1, moeda="usd", pedido_id=self.pedido_alheio.pk)
        self.assertEqual(resposta.url, "https://checkout.stripe.com/c/pay/mock")
        pagamento = Pagamento.objects.get()
        self.assertEqual((pagamento.pedido, pagamento.valor_centavos, pagamento.moeda), (self.pedido, 2470, "brl"))
        dados = self.sessoes.create.call_args.args[0]
        self.assertEqual(dados["line_items"][0]["price_data"]["unit_amount"], 2470)
        self.assertEqual(dados["line_items"][0]["price_data"]["currency"], "brl")
        self.assertEqual(dados["adaptive_pricing"], {"enabled": False})
        self.assertEqual(dados["allowed_payment_method_types"], ["card"])
        self.assertNotIn("payment_method_types", dados)
        self.assertEqual(dados["success_url"], settings.STRIPE_PUBLIC_ORIGIN + self.aprovado)
        self.assertEqual(dados["cancel_url"], settings.STRIPE_PUBLIC_ORIGIN + self.cancelado)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.status, Pedido.Status.ABERTO)

    def test_pedido_cancelado_ou_pago_nao_inicia_sessao(self):
        for status in (Pedido.Status.CANCELADO, Pedido.Status.PAGO):
            with self.subTest(status=status):
                Pedido.objects.filter(pk=self.pedido.pk).update(status=status)
                self.assertRedirects(self.abrir(), self.historico)
                self.assertNotContains(self.client.get(self.historico), self.pagar)
        self.mock_client.assert_not_called()

    def test_configuracao_ausente_ou_chave_live_bloqueia_criacao(self):
        for chave in ("", "sk_live_mock"):
            with self.settings(STRIPE_SECRET_KEY=chave):
                self.assertRedirects(self.abrir(), self.historico)
        with self.settings(STRIPE_WEBHOOK_SECRET=""):
            self.assertRedirects(self.abrir(), self.historico)
            self.assertEqual(self.client.post(self.webhook).status_code, 503)
        self.mock_client.assert_not_called()
        self.assertFalse(Pagamento.objects.exists())

    def test_diagnostico_informa_todas_as_falhas_sem_expor_valores(self):
        chave = "sk_live_dado_ficticio"
        segredo = '"whsec_dado_ficticio"'
        origem = "https://usuario:dado_ficticio@example.org"
        with self.settings(STRIPE_SECRET_KEY=chave, STRIPE_WEBHOOK_SECRET=segredo, STRIPE_PUBLIC_ORIGIN=origem):
            with self.assertLogs("loja.pagamentos", level="WARNING") as logs:
                self.assertRedirects(self.abrir(), self.historico)
            with self.assertLogs("loja.pagamento_views", level="WARNING") as webhook:
                self.assertEqual(self.client.post(self.webhook).status_code, 503)
        saida = " ".join(logs.output + webhook.output)
        self.assertIn("STRIPE_SECRET_KEY: chave live bloqueada", saida)
        self.assertIn("STRIPE_WEBHOOK_SECRET: contém aspas literais", saida)
        self.assertIn("STRIPE_PUBLIC_ORIGIN: contém credenciais na URL", saida)
        self.assertNotIn("dado_ficticio", saida)
        self.mock_client.assert_not_called()

    def test_diagnostico_do_sdk_exclui_mensagem_original_da_api(self):
        self.sessoes.create.side_effect = stripe.InvalidRequestError(
            "detalhe_ficticio_sensivel", param="success_url", code="url_invalid", http_status=400,
        )
        with self.assertLogs("loja.pagamento_views", level="WARNING") as logs:
            self.assertRedirects(self.abrir(), self.historico)
        saida = " ".join(logs.output)
        self.assertIn("InvalidRequestError", saida)
        self.assertIn("HTTP=400", saida)
        self.assertIn("código=url_invalid", saida)
        self.assertIn("parâmetro=success_url", saida)
        self.assertNotIn("detalhe_ficticio_sensivel", saida)

    def test_pagamento_repetido_reaproveita_sessao(self):
        primeira = self.abrir()
        dados = self.sessoes.create.call_args.args[0]
        opcoes = self.sessoes.create.call_args.kwargs["options"]
        sessao = self.criar_sessao(dados, opcoes)
        self.sessoes.retrieve.return_value = sessao
        self.assertEqual(self.abrir().url, primeira.url)
        self.assertEqual(Pagamento.objects.count(), 1)
        self.sessoes.create.assert_called_once()
        self.sessoes.retrieve.assert_called_once_with(sessao["id"])

    def test_conflito_com_rejeicao_legada_reutiliza_a_tentativa_com_chave_estavel(self):
        pagamento = self.pedido.pagamentos.create(valor_centavos=2470)
        chave = f"vitrine-checkout-{pagamento.pk}"

        def criar(dados, options):
            if options["idempotency_key"] == f"{chave}-endive":
                return self.criar_sessao(dados, options)
            if "payment_method_types" in dados:
                raise stripe.InvalidRequestError(
                    "The payment_method_types parameter is no longer supported.",
                    param="payment_method_types", http_status=400,
                )
            raise stripe.IdempotencyError("Parâmetros diferentes")

        self.sessoes.create.side_effect = criar
        self.assertTrue(self.abrir().url.startswith("https://checkout.stripe.com/"))
        primeira, legada, atualizada = self.sessoes.create.call_args_list
        self.assertEqual(primeira.kwargs["options"], {"idempotency_key": chave})
        self.assertEqual(legada.kwargs["options"], primeira.kwargs["options"])
        self.assertEqual(legada.args[0]["payment_method_types"], ["card"])
        self.assertEqual(atualizada.kwargs["options"], {"idempotency_key": f"{chave}-endive"})
        self.assertEqual(atualizada.args[0], primeira.args[0])
        self.assertEqual(Pagamento.objects.count(), 1)
        pagamento.refresh_from_db()
        self.assertTrue(pagamento.sessao_stripe.startswith("cs_test_"))

    def test_conflito_reaproveita_sessao_legada_quando_ela_existe(self):
        def criar(dados, options):
            if "payment_method_types" in dados:
                return self.criar_sessao(dados, options)
            raise stripe.IdempotencyError("Parâmetros diferentes")

        self.sessoes.create.side_effect = criar
        self.assertTrue(self.abrir().url.startswith("https://checkout.stripe.com/"))
        primeira, legada = self.sessoes.create.call_args_list
        self.assertEqual(primeira.kwargs["options"], legada.kwargs["options"])
        self.assertEqual(Pagamento.objects.count(), 1)

    def test_conflito_sem_prova_da_rejeicao_nao_tenta_criar_com_outra_chave(self):
        self.sessoes.create.side_effect = [
            stripe.IdempotencyError("Parâmetros diferentes"),
            stripe.InvalidRequestError("Erro de configuração", param="success_url", http_status=400),
        ]
        with self.assertLogs("loja.pagamento_views", level="WARNING"):
            self.assertRedirects(self.abrir(), self.historico)
        primeira, legada = self.sessoes.create.call_args_list
        self.assertEqual(primeira.kwargs["options"], legada.kwargs["options"])
        self.assertIsNone(Pagamento.objects.get().sessao_stripe)

    def test_rede_inconclusiva_reaproveita_chave_de_idempotencia(self):
        self.sessoes.create.side_effect = stripe.APIConnectionError("timeout")
        with self.assertLogs("loja.pagamento_views", level="WARNING"):
            self.assertRedirects(self.abrir(), self.historico)
        requisicao = self.sessoes.create.call_args
        self.sessoes.create.side_effect = self.criar_sessao
        self.assertTrue(self.abrir().url.startswith("https://checkout.stripe.com/"))
        self.assertEqual(requisicao, self.sessoes.create.call_args)
        self.assertEqual(Pagamento.objects.count(), 1)

    def test_tentativa_incerta_antiga_nao_pode_gerar_segunda_sessao(self):
        pagamento = self.pedido.pagamentos.create(valor_centavos=2470)
        Pagamento.objects.filter(pk=pagamento.pk).update(criado_em=timezone.now() - timedelta(hours=24))
        self.assertRedirects(self.abrir(), self.historico)
        self.sessoes.create.assert_not_called()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.pedido.pagamentos.create(valor_centavos=2470)

    def test_retornos_e_historico_do_comprador_nao_confirmam_pagamento(self):
        self.abrir()
        resposta = self.client.get(self.historico)
        self.assertContains(resposta, self.pagar)
        self.assertContains(resposta, "R$ 24,70")
        self.assertContains(resposta, "Aguardando pagamento")
        self.assertNotContains(resposta, f"Pedido #{self.pedido_alheio.pk}")
        self.assertContains(self.client.get(self.aprovado, {"payment_status": "paid"}), "Aguardando confirmação")
        self.assertContains(self.client.get(self.cancelado), "Você saiu do pagamento")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.status, Pedido.Status.ABERTO)
        self.assertEqual(Pagamento.objects.get().status, Pagamento.Status.PENDENTE)

    def test_webhook_valida_assinatura_e_rejeita_dados_incompativeis(self):
        self.abrir()
        evento = self.montar_evento()
        self.assertEqual(self.enviar_evento(evento, segredo="invalido").status_code, 400)
        self.assertEqual(self.enviar_evento(evento, atraso=301).status_code, 400)
        self.assertEqual(self.client.post(self.webhook, b"{}", content_type="application/json").status_code, 400)
        for campos in (
            {"amount_total": 2469}, {"amount_total": "2470"}, {"currency": "usd"},
            {"client_reference_id": str(self.pedido_alheio.pk)}, {"id": "cs_test_outra"},
            {"livemode": True}, {"mode": "subscription"}, {"payment_status": "unpaid"},
            {"status": "open"}, {"payment_intent": None},
        ):
            with self.subTest(campos=campos):
                self.assertEqual(self.enviar_evento(self.montar_evento(**campos)).status_code, 400)
        self.assertEqual(self.enviar_evento({**evento, "livemode": True}).status_code, 400)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.status, Pedido.Status.ABERTO)

    def test_webhook_confirma_uma_vez_sem_outra_baixa_de_estoque(self):
        self.abrir()
        evento = self.montar_evento()
        for tentativa in (evento, evento, {**evento, "id": "evt_outro"}):
            self.assertEqual(self.enviar_evento(tentativa).status_code, 200)
        self.pedido.refresh_from_db()
        self.produto.refresh_from_db()
        pagamento = Pagamento.objects.get()
        self.assertEqual(self.pedido.status, Pedido.Status.PAGO)
        self.assertEqual(pagamento.status, Pagamento.Status.PAGO)
        self.assertEqual(pagamento.evento_stripe, evento["id"])
        self.assertIsNotNone(pagamento.confirmado_em)
        self.assertEqual(self.produto.estoque, 7)
        self.assertContains(self.client.get(self.aprovado), "Pagamento aprovado")
        self.assertNotContains(self.client.get(self.historico), self.pagar)
        self.sessoes.create.assert_called_once()

    def test_webhook_nao_confirma_pedido_cancelado_ou_com_total_alterado(self):
        self.abrir()
        evento = self.montar_evento()
        Pedido.objects.filter(pk=self.pedido.pk).update(status=Pedido.Status.CANCELADO)
        self.assertEqual(self.enviar_evento(evento).status_code, 400)
        Pedido.objects.filter(pk=self.pedido.pk).update(status=Pedido.Status.ABERTO)
        ItemPedido.objects.filter(pedido=self.pedido).update(preco_unitario="9.99")
        self.assertEqual(self.enviar_evento(evento).status_code, 400)
        self.assertEqual(Pagamento.objects.get().status, Pagamento.Status.PENDENTE)

    def test_webhook_pode_chegar_antes_do_registro_da_sessao(self):
        pagamento = self.pedido.pagamentos.create(valor_centavos=2470)
        evento = self.montar_evento()
        self.assertEqual(self.enviar_evento(evento).status_code, 200)
        pagamento.refresh_from_db()
        self.assertEqual(pagamento.sessao_stripe, evento["data"]["object"]["id"])
        self.assertEqual(pagamento.status, Pagamento.Status.PAGO)

    def test_expiracao_assinada_permite_nova_tentativa(self):
        self.abrir()
        primeiro = Pagamento.objects.get()
        evento = self.montar_evento(status="expired", payment_status="unpaid", payment_intent=None)
        evento["type"] = "checkout.session.expired"
        self.assertEqual(self.enviar_evento(evento).status_code, 200)
        self.assertEqual(self.enviar_evento(evento).status_code, 200)
        self.assertTrue(self.abrir().url.startswith("https://checkout.stripe.com/"))
        primeiro.refresh_from_db()
        segundo = Pagamento.objects.get(status=Pagamento.Status.PENDENTE)
        self.assertEqual(primeiro.status, Pagamento.Status.EXPIRADO)
        self.assertNotEqual(primeiro.sessao_stripe, segundo.sessao_stripe)

    def test_admin_nao_marca_pago_manualmente_nem_altera_tentativa_ativa(self):
        self.client.force_login(self.admin)
        url = reverse("admin:loja_pedido_change", args=[self.pedido.pk])
        dados = {"comprador": self.comprador.pk, "status": Pedido.Status.PAGO, "itens-TOTAL_FORMS": 1,
                 "itens-INITIAL_FORMS": 1, "itens-0-id": self.pedido.itens.get().pk,
                 "itens-0-produto": self.produto.pk, "itens-0-quantidade": 2, "_save": "Salvar"}
        self.assertContains(self.client.post(url, dados), "O pagamento é confirmado pelo webhook do Stripe.")
        self.abrir()
        self.client.force_login(self.admin)
        resposta = self.client.post(url, {
            **dados, "status": Pedido.Status.CANCELADO, "itens-0-quantidade": 99, "itens-0-DELETE": "on",
        })
        self.assertRedirects(resposta, reverse("admin:loja_pedido_changelist"))
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.status, Pedido.Status.ABERTO)
        self.assertEqual(self.pedido.itens.get().quantidade, 2)

        self.assertEqual(self.enviar_evento(self.montar_evento()).status_code, 200)
        self.assertContains(self.client.post(url, {**dados, "status": Pedido.Status.CANCELADO}),
                            "Este pedido tem um pagamento confirmado.")
        self.assertRedirects(self.client.post(url, {**dados, "status": Pedido.Status.ENVIADO}),
                             reverse("admin:loja_pedido_changelist"))
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.status, Pedido.Status.ENVIADO)
