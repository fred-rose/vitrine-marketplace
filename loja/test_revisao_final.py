import os
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import stripe
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.management.utils import get_random_secret_key
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .checkout import cancelar_pedido, realizar_checkout
from .models import Avaliacao, Categoria, Pagamento, Pedido, Produto


class RevisaoPedidoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        usuarios = get_user_model().objects
        cls.comprador = usuarios.create_user(username="comprador")
        cls.outro = usuarios.create_user(username="outro")
        cls.admin = usuarios.create_superuser(username="admin")
        categoria = Categoria.objects.create(nome="Casa")
        cls.produto = Produto.objects.create(
            vendedor=cls.outro, categoria=categoria, nome="Caneca <especial>", preco="25.00", estoque=8,
        )
        cls.pedido = realizar_checkout(cls.comprador, {str(cls.produto.pk): 2})

    def setUp(self):
        self.historico = reverse("loja:historico_pedidos")
        self.cancelar = reverse("loja:cancelar_pedido", args=[self.pedido.pk])
        self.avaliar = reverse("loja:avaliar_produto", args=[self.produto.pk])
        self.pagar = reverse("loja:pagar_pedido", args=[self.pedido.pk])

    def verificar(self, status, estoque):
        self.pedido.refresh_from_db()
        self.produto.refresh_from_db()
        self.assertEqual(self.pedido.status, status)
        self.assertEqual(self.produto.estoque, estoque)

    def tornar_antigo(self):
        Pedido.objects.filter(pk=self.pedido.pk).update(criado_em=timezone.now() - timedelta(hours=25))

    def test_cancelamento_so_aceita_post_com_csrf_do_comprador_e_restitui_uma_vez(self):
        self.assertEqual(self.client.post(self.cancelar).status_code, 302)
        self.client.force_login(self.outro)
        self.assertEqual(self.client.post(self.cancelar).status_code, 404)
        seguro = Client(enforce_csrf_checks=True)
        seguro.force_login(self.comprador)
        self.assertEqual(seguro.get(self.cancelar).status_code, 405)
        self.assertEqual(seguro.post(self.cancelar).status_code, 403)
        seguro.get(self.historico)
        token = seguro.cookies["csrftoken"].value
        for _ in range(2):
            self.assertRedirects(seguro.post(self.cancelar, HTTP_X_CSRFTOKEN=token), self.historico)
        self.verificar(Pedido.Status.CANCELADO, 8)
        self.assertFalse(self.pedido.estoque_reservado)

    def test_cancelamento_reverte_o_status_se_devolucao_falhar(self):
        with patch.object(Produto.objects, "filter", side_effect=DatabaseError), self.assertRaises(DatabaseError):
            cancelar_pedido(self.pedido.pk)
        self.verificar(Pedido.Status.ABERTO, 6)
        self.assertTrue(self.pedido.estoque_reservado)

    def test_comando_expira_antigos_e_preserva_pedidos_recentes(self):
        self.tornar_antigo()
        recente = realizar_checkout(self.comprador, {str(self.produto.pk): 1})
        self.client.force_login(self.comprador)
        self.assertNotContains(self.client.get(self.historico), self.pagar)
        for quantidade in (1, 0):
            saida = StringIO()
            call_command("expirar_pedidos", stdout=saida)
            self.assertIn(f"Pedidos cancelados: {quantidade}", saida.getvalue())
        self.verificar(Pedido.Status.CANCELADO, 7)
        recente.refresh_from_db()
        self.assertEqual(recente.status, Pedido.Status.ABERTO)

    def test_tentativa_stripe_sem_id_bloqueia_devolucao_e_e_sinalizada(self):
        self.tornar_antigo()
        self.pedido.pagamentos.create(valor_centavos=5000)
        self.client.force_login(self.comprador)
        self.assertContains(self.client.post(self.cancelar, follow=True), "Aguarde a confirmação ou a expiração")
        saida = StringIO()
        call_command("expirar_pedidos", stdout=saida, stderr=StringIO())
        self.assertIn("Pendentes de verificação: 1", saida.getvalue())
        self.verificar(Pedido.Status.ABERTO, 6)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", STRIPE_WEBHOOK_SECRET="whsec_mock",
                       STRIPE_PUBLIC_ORIGIN="http://127.0.0.1:8000")
    def test_comando_confere_sessao_stripe_conhecida_antes_de_devolver(self):
        self.tornar_antigo()
        pagamento = self.pedido.pagamentos.create(valor_centavos=5000, sessao_stripe="cs_test_devolucao")
        dados = {
            "id": pagamento.sessao_stripe, "object": "checkout.session", "livemode": False,
            "mode": "payment", "metadata": {"vitrine_pagamento_id": str(pagamento.pk)},
            "client_reference_id": str(self.pedido.pk), "currency": "brl", "amount_total": 5000,
        }
        with patch("loja.pagamentos.stripe.StripeClient") as cliente:
            buscar = cliente.return_value.v1.checkout.sessions.retrieve
            buscar.return_value = stripe.checkout.Session.construct_from({
                **dados, "status": "complete", "payment_status": "paid",
            }, None)
            call_command("expirar_pedidos", stdout=StringIO(), stderr=StringIO())
            self.verificar(Pedido.Status.ABERTO, 6)
            buscar.return_value = stripe.checkout.Session.construct_from({
                **dados, "status": "expired", "payment_status": "unpaid",
            }, None)
            call_command("expirar_pedidos", stdout=StringIO(), stderr=StringIO())
        self.verificar(Pedido.Status.CANCELADO, 8)
        pagamento.refresh_from_db()
        self.assertEqual(pagamento.status, Pagamento.Status.EXPIRADO)

    def test_admin_cancela_com_devolucao_e_nao_reabre_pedido_cancelado(self):
        self.client.force_login(self.admin)
        url = reverse("admin:loja_pedido_change", args=[self.pedido.pk])
        dados = {"itens-TOTAL_FORMS": 1, "itens-INITIAL_FORMS": 1,
                 "itens-0-id": self.pedido.itens.get().pk, "_save": "Salvar"}
        self.assertEqual(self.client.post(url, {**dados, "status": Pedido.Status.CANCELADO}).status_code, 302)
        self.verificar(Pedido.Status.CANCELADO, 8)
        self.assertContains(self.client.post(url, {**dados, "status": Pedido.Status.ABERTO}),
                            "Esta transição de status não está disponível.")
        self.verificar(Pedido.Status.CANCELADO, 8)

    def test_interface_de_avaliacao_exige_pedido_concluido_proprio_e_csrf(self):
        dados = {"nota": 5, "comentario": "Minha avaliação"}
        self.assertEqual(self.client.get(self.avaliar).status_code, 302)
        self.client.force_login(self.comprador)
        self.assertNotContains(self.client.get(self.historico), self.avaliar)
        self.assertEqual(self.client.post(self.avaliar, dados).status_code, 404)
        Pedido.objects.filter(pk=self.pedido.pk).update(status=Pedido.Status.CONCLUIDO)
        self.client.force_login(self.outro)
        self.assertEqual(self.client.post(self.avaliar, dados).status_code, 404)

        seguro = Client(enforce_csrf_checks=True)
        seguro.force_login(self.comprador)
        self.assertContains(seguro.get(self.historico), self.avaliar)
        self.assertContains(seguro.get(self.avaliar), "Caneca &lt;especial&gt;")
        self.assertEqual(seguro.post(self.avaliar, dados).status_code, 403)
        token = seguro.cookies["csrftoken"].value
        erro = seguro.post(self.avaliar, {**dados, "nota": 6, "comentario": "<b>x</b>"}, HTTP_X_CSRFTOKEN=token)
        self.assertContains(erro, "&lt;b&gt;x&lt;/b&gt;")
        self.assertFalse(Avaliacao.objects.exists())
        for _ in range(2):
            self.assertRedirects(seguro.post(self.avaliar, dados, HTTP_X_CSRFTOKEN=token), self.historico)
        avaliacao = Avaliacao.objects.get()
        self.assertEqual(avaliacao.comprador, self.comprador)
        self.assertEqual(avaliacao.nota, 5)
        self.assertContains(seguro.get(self.historico), "Avaliado")
        self.assertContains(seguro.get(reverse("loja:catalogo")), "Avaliação 5,0 / 5")


class RevisaoDemoTests(TestCase):
    def setUp(self):
        call_command("seed_demo", stdout=StringIO())

    def test_env_configura_apenas_senhas_desativadas_e_ignora_variavel_vazia(self):
        usuarios = get_user_model().objects
        vendedor = usuarios.get(username="demo_vendedor_1")
        vendedor.is_staff = False
        vendedor.save(update_fields=("is_staff",))
        vendedor.user_permissions.clear()
        senha = get_random_secret_key()
        outra = get_random_secret_key()
        saida = StringIO()
        env = {"DEMO_BUYER_PASSWORD": senha, "DEMO_SELLER_1_PASSWORD": outra, "DEMO_SELLER_2_PASSWORD": ""}
        with patch.dict(os.environ, env):
            call_command("configurar_senha_demo", "--from-env", stdout=saida)

        comprador = usuarios.get(username="demo_comprador")
        self.assertTrue(comprador.check_password(senha))
        vendedor.refresh_from_db()
        self.assertTrue(vendedor.check_password(outra))
        self.assertFalse(vendedor.is_staff)
        self.assertFalse(vendedor.user_permissions.exists())
        self.assertFalse(usuarios.get(username="demo_vendedor_2").has_usable_password())

        with patch.dict(os.environ, {**env, "DEMO_BUYER_PASSWORD": outra}):
            call_command("configurar_senha_demo", "--from-env", stdout=saida)
        comprador.refresh_from_db()
        self.assertTrue(comprador.check_password(senha))
        self.assertNotIn(senha, saida.getvalue())
        self.assertNotIn(outra, saida.getvalue())

    def test_env_reverte_configuracao_se_uma_senha_for_invalida(self):
        senha = get_random_secret_key()
        env = {"DEMO_BUYER_PASSWORD": senha, "DEMO_SELLER_1_PASSWORD": "12", "DEMO_SELLER_2_PASSWORD": ""}
        saida = StringIO()
        with patch.dict(os.environ, env), self.assertRaisesMessage(CommandError, "DEMO_SELLER_1_PASSWORD"):
            call_command("configurar_senha_demo", "--from-env", stdout=saida)
        self.assertFalse(get_user_model().objects.get(username="demo_comprador").has_usable_password())
        self.assertEqual(saida.getvalue(), "")

    def test_reexecucao_preserva_dados_editados_e_permissoes_revogadas(self):
        vendedor = get_user_model().objects.get(username="demo_vendedor_1")
        vendedor.is_staff = False
        vendedor.save(update_fields=("is_staff",))
        vendedor.user_permissions.clear()
        produto = Produto.objects.filter(vendedor=vendedor).first()
        produto.imagem_local = "loja/img/hero.jpg"
        produto.estoque = 1
        produto.save(update_fields=("imagem_local", "estoque"))

        saida = StringIO()
        call_command("seed_demo", stdout=saida)

        vendedor.refresh_from_db()
        produto.refresh_from_db()
        self.assertEqual(Produto.objects.count(), 16)
        self.assertFalse(vendedor.is_staff)
        self.assertFalse(vendedor.user_permissions.exists())
        self.assertEqual(produto.imagem_local, "loja/img/hero.jpg")
        self.assertEqual(produto.estoque, 1)
        self.assertIn("Produtos novos: 0", saida.getvalue())
        self.assertIn("Vendas fictícias novas: 0", saida.getvalue())

    def test_seed_associa_16_arquivos_locais_e_vendas_ficticias_sem_pagamentos(self):
        produtos = Produto.objects.all()
        self.assertEqual(produtos.values("imagem_local").distinct().count(), 16)
        self.assertTrue(all(finders.find(produto.imagem_local) for produto in produtos))
        dados = self.client.get(reverse("loja:api_produtos")).json()
        self.assertTrue(all(produto["imagem_exibicao"].startswith("/static/loja/img/produtos/") for produto in dados))
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Pedido.objects.filter(referencia_demo__isnull=False).count(), 4)
        self.assertFalse(Pagamento.objects.exists())
        usuarios = get_user_model().objects
        self.assertFalse(usuarios.get(username="demo_comprador").is_staff)
        for username in ("demo_vendedor_1", "demo_vendedor_2"):
            vendedor = usuarios.get(username=username)
            self.client.force_login(vendedor)
            painel = self.client.get(reverse("loja:painel_vendedor"))
            self.assertContains(painel, "Os totais incluem vendas fictícias de demonstração.")
            self.assertGreater(painel.context["resumo"]["faturamento"], 0)
            self.assertTrue(all(item.produto.vendedor_id == vendedor.pk for item in painel.context["historico"]))
            self.assertFalse(vendedor.has_perm("loja.view_pedido"))

    def test_comando_configura_senha_validada_sem_exibir_ou_redefinir_no_seed(self):
        senha = get_random_secret_key()
        conta = "demo_comprador"
        saida = StringIO()
        caminho = "loja.management.commands.configurar_senha_demo.getpass.getpass"
        with patch(caminho, side_effect=[senha, "diferente"]), self.assertRaises(CommandError):
            call_command("configurar_senha_demo", conta, stdout=saida)
        with patch(caminho, side_effect=[senha, senha]):
            call_command("configurar_senha_demo", conta, stdout=saida)
        call_command("seed_demo", stdout=saida)
        self.assertNotIn(senha, saida.getvalue())
        self.assertTrue(self.client.login(username=conta, password=senha))
