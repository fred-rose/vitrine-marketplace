from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db.models import QuerySet
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse

from .models import Categoria, ItemPedido, Pedido, Produto


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CarrinhoCheckoutTests(TransactionTestCase):
    def setUp(self):
        usuarios = get_user_model().objects
        vendedor = usuarios.create_user(username="vendedor")
        self.comprador = usuarios.create_user(username="comprador", password="senha-de-teste")
        categoria = Categoria.objects.create(nome="Livros")
        self.produto = Produto.objects.create(
            vendedor=vendedor, categoria=categoria, nome="Livro", preco="12.50", estoque=5
        )
        self.outro_produto = Produto.objects.create(
            vendedor=vendedor, categoria=categoria, nome="Revista", preco="3.75", estoque=2
        )
        self.url_carrinho = reverse("loja:carrinho")
        self.url_checkout = reverse("loja:checkout")

    def adicionar(self, produto, quantidade=1):
        url = reverse("loja:carrinho_adicionar", args=[produto.pk])
        resposta = self.client.post(url, {"quantidade": quantidade})
        self.assertRedirects(resposta, self.url_carrinho, fetch_redirect_response=False)

    def verificar_estoque(self, produto, esperado):
        produto.refresh_from_db()
        self.assertEqual(produto.estoque, esperado)

    def test_carrinho_sem_login_exibe_quantidades_subtotais_e_total(self):
        self.assertContains(self.client.get(reverse("loja:catalogo")), "Livro")
        self.adicionar(self.produto)
        self.adicionar(self.produto)
        self.adicionar(self.outro_produto)

        resposta = self.client.get(self.url_carrinho)

        self.assertEqual(resposta.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(
            self.client.session["carrinho"],
            {str(self.produto.pk): 2, str(self.outro_produto.pk): 1},
        )
        self.assertEqual(resposta.context["itens"][0]["quantidade"], 2)
        self.assertEqual(resposta.context["itens"][0]["subtotal"], Decimal("25.00"))
        self.assertEqual(resposta.context["itens"][1]["subtotal"], Decimal("3.75"))
        self.assertEqual(resposta.context["total"], Decimal("28.75"))
        self.assertContains(resposta, "R$ 28,75")

    def test_carrinho_anonimo_e_preservado_apos_login_drf(self):
        self.adicionar(self.produto, 2)
        url_login = f"{settings.LOGIN_URL}?next={self.url_carrinho}"
        self.assertContains(self.client.get(self.url_carrinho), f'href="{url_login}"')
        pagina_login = self.client.get(url_login)
        self.assertContains(pagina_login, f'<input type="hidden" name="next" value="{self.url_carrinho}" />', html=True)

        resposta = self.client.post(
            settings.LOGIN_URL,
            {
                "username": self.comprador.username,
                "password": "senha-de-teste",
                "next": self.url_carrinho,
            },
        )

        self.assertRedirects(resposta, self.url_carrinho)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.comprador.pk))
        self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 2})

    def test_checkout_exige_autenticacao(self):
        self.adicionar(self.produto)

        resposta = self.client.post(self.url_checkout)

        self.assertRedirects(resposta, f"{settings.LOGIN_URL}?next={self.url_carrinho}")
        self.assertFalse(Pedido.objects.exists())
        self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 1})
        self.verificar_estoque(self.produto, 5)

    def test_mutacoes_do_carrinho_e_checkout_exigem_post(self):
        self.client.force_login(self.comprador)
        for nome in ("carrinho_adicionar", "carrinho_alterar", "carrinho_remover"):
            with self.subTest(rota=nome):
                url = reverse(f"loja:{nome}", args=[self.produto.pk])
                self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.get(self.url_checkout).status_code, 405)

    def test_checkout_exige_csrf(self):
        cliente = Client(enforce_csrf_checks=True)
        cliente.force_login(self.comprador)
        self.assertEqual(cliente.post(self.url_checkout).status_code, 403)

    def test_compra_com_estoque_suficiente_registra_itens_e_limpa_carrinho(self):
        self.client.force_login(self.comprador)
        self.adicionar(self.produto, 2)
        self.adicionar(self.outro_produto)

        resposta = self.client.post(self.url_checkout, follow=True)

        pedido = Pedido.objects.get()
        self.assertContains(resposta, f"Pedido {pedido.pk} registrado com sucesso.")
        self.assertEqual(pedido.comprador, self.comprador)
        self.assertEqual(pedido.status, Pedido.Status.ABERTO)
        self.assertCountEqual(
            pedido.itens.values_list("produto_id", "quantidade", "preco_unitario"),
            [(self.produto.pk, 2, Decimal("12.50")), (self.outro_produto.pk, 1, Decimal("3.75"))],
        )
        self.assertEqual(pedido.total, Decimal("28.75"))
        self.verificar_estoque(self.produto, 3)
        self.verificar_estoque(self.outro_produto, 1)
        self.assertNotIn("carrinho", self.client.session)

    def test_checkout_valida_todos_os_estoques_antes_de_criar_pedido(self):
        self.client.force_login(self.comprador)
        self.adicionar(self.produto)
        self.adicionar(self.outro_produto, 3)
        carrinho = self.client.session["carrinho"]

        with patch.object(Pedido.objects, "create") as criar_pedido:
            resposta = self.client.post(self.url_checkout, follow=True)
        criar_pedido.assert_not_called()

        self.assertContains(resposta, "Estoque insuficiente para Revista.")
        self.assertFalse(Pedido.objects.exists())
        self.assertFalse(ItemPedido.objects.exists())
        self.assertEqual(self.client.session["carrinho"], carrinho)
        self.verificar_estoque(self.produto, 5)
        self.verificar_estoque(self.outro_produto, 2)

    def test_erro_ao_criar_segundo_item_reverte_pedido_itens_e_estoque(self):
        self.client.force_login(self.comprador)
        self.adicionar(self.produto)
        self.adicionar(self.outro_produto)
        carrinho = self.client.session["carrinho"]
        criar_item = ItemPedido.objects.create

        def falhar_no_segundo_item(**campos):
            if campos["produto"] == self.outro_produto:
                self.assertTrue(ItemPedido.objects.filter(produto=self.produto).exists())
                self.verificar_estoque(self.produto, 4)
                self.verificar_estoque(self.outro_produto, 1)
                raise IntegrityError("Falha simulada no segundo item.")
            return criar_item(**campos)

        with patch.object(ItemPedido.objects, "create", side_effect=falhar_no_segundo_item):
            with self.assertLogs("loja.views", level="ERROR"):
                resposta = self.client.post(self.url_checkout, follow=True)

        self.assertContains(resposta, "Não foi possível registrar o pedido.")
        self.assertFalse(Pedido.objects.exists())
        self.assertFalse(ItemPedido.objects.exists())
        self.assertEqual(self.client.session["carrinho"], carrinho)
        self.verificar_estoque(self.produto, 5)
        self.verificar_estoque(self.outro_produto, 2)

    def test_preco_historico_reflete_preco_do_checkout(self):
        self.client.force_login(self.comprador)
        self.adicionar(self.produto, 2)
        Produto.objects.filter(pk=self.produto.pk).update(preco=Decimal("15.00"))

        self.client.post(self.url_checkout)
        Produto.objects.filter(pk=self.produto.pk).update(preco=Decimal("20.00"))

        item = ItemPedido.objects.get()
        self.assertEqual(item.preco_unitario, Decimal("15.00"))
        self.assertEqual(item.subtotal, Decimal("30.00"))
        self.assertEqual(item.pedido.total, Decimal("30.00"))

    def test_compra_ate_zerar_estoque_e_rejeita_nova_baixa(self):
        self.client.force_login(self.comprador)
        self.adicionar(self.produto, 5)
        self.client.post(self.url_checkout)
        self.verificar_estoque(self.produto, 0)

        self.adicionar(self.produto)
        resposta = self.client.post(self.url_checkout, follow=True)

        self.assertContains(resposta, "Estoque insuficiente para Livro.")
        self.verificar_estoque(self.produto, 0)
        self.assertEqual(Pedido.objects.count(), 1)
        self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 1})

    def test_baixa_condicional_rejeita_estoque_reduzido_apos_validacao(self):
        self.client.force_login(self.comprador)
        self.adicionar(self.produto, 2)
        atualizar = QuerySet.update

        def simular_baixa_apos_validacao(queryset, **campos):
            if queryset.model is Produto:
                atualizar(Produto.objects.filter(pk=self.produto.pk), estoque=1)
            return atualizar(queryset, **campos)

        with patch.object(QuerySet, "update", autospec=True, side_effect=simular_baixa_apos_validacao):
            resposta = self.client.post(self.url_checkout, follow=True)

        self.assertContains(resposta, "Estoque insuficiente para Livro.")
        self.assertFalse(Pedido.objects.exists())
        self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 2})
        self.verificar_estoque(self.produto, 5)

    def test_altera_quantidade_e_remove_produto_sem_login(self):
        self.adicionar(self.produto)
        self.adicionar(self.outro_produto)
        url_alterar = reverse("loja:carrinho_alterar", args=[self.produto.pk])
        url_remover = reverse("loja:carrinho_remover", args=[self.outro_produto.pk])

        resposta = self.client.post(url_alterar, {"quantidade": 3}, follow=True)
        self.assertEqual(resposta.context["total"], Decimal("41.25"))
        resposta = self.client.post(url_remover, follow=True)
        self.assertEqual(resposta.context["total"], Decimal("37.50"))
        self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 3})

    def test_quantidade_invalida_preserva_carrinho(self):
        self.adicionar(self.produto)
        url_alterar = reverse("loja:carrinho_alterar", args=[self.produto.pk])

        for quantidade in ("0", "-1", "1.5", "texto", "2147483648"):
            with self.subTest(quantidade=quantidade):
                resposta = self.client.post(url_alterar, {"quantidade": quantidade}, follow=True)
                self.assertContains(resposta, "Informe uma quantidade válida.")
                self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 1})

    def test_produto_excluido_pode_ser_removido_e_impede_checkout(self):
        self.client.force_login(self.comprador)
        self.adicionar(self.produto)
        self.adicionar(self.outro_produto)
        produto_id = self.outro_produto.pk
        self.outro_produto.delete()

        resposta = self.client.post(self.url_checkout, follow=True)

        self.assertContains(resposta, "Remova do carrinho os produtos indisponíveis.")
        self.assertContains(resposta, "Produto indisponível")
        self.assertFalse(Pedido.objects.exists())
        self.verificar_estoque(self.produto, 5)
        self.assertIn(str(produto_id), self.client.session["carrinho"])

        self.client.post(reverse("loja:carrinho_remover", args=[produto_id]))
        self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 1})
