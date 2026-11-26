from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Categoria, ItemPedido, Pedido, Produto


class MarketplaceAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        usuarios = get_user_model().objects
        cls.vendedor = usuarios.create_user(username="vendedor")
        cls.comprador = usuarios.create_user(username="comprador")
        cls.categoria = Categoria.objects.create(nome="Livros")
        cls.livro = Produto.objects.create(
            vendedor=cls.vendedor, categoria=cls.categoria, nome="Livro", preco="12.50", estoque=5
        )
        cls.revista = Produto.objects.create(
            vendedor=cls.vendedor, categoria=cls.categoria, nome="Revista", preco="3.75", estoque=2
        )

    def setUp(self):
        self.url_produtos = reverse("loja:api_produtos")
        self.url_pedidos = reverse("loja:api_pedidos")
        self.dados_produto = {
            "nome": "Produto novo",
            "descricao": "Descrição",
            "categoria": self.categoria.pk,
            "preco": "19.90",
            "estoque": 2,
        }

    def criar_pedido(self, *itens, **campos):
        return self.client.post(self.url_pedidos, {"itens": itens, **campos}, format="json")

    def verificar_estoque(self, produto, estoque):
        produto.refresh_from_db()
        self.assertEqual(produto.estoque, estoque)

    def test_lista_produtos_sem_autenticacao(self):
        resposta = self.client.get(self.url_produtos)

        self.assertEqual(resposta.status_code, status.HTTP_200_OK)
        self.assertEqual([produto["id"] for produto in resposta.data], [self.livro.pk, self.revista.pk])
        self.assertEqual(resposta.data[0]["preco"], "12.50")

    def test_escrita_exige_autenticacao(self):
        produto = self.client.post(self.url_produtos, self.dados_produto, format="json")
        pedido = self.criar_pedido({"produto": self.livro.pk, "quantidade": 1})

        self.assertEqual(produto.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(pedido.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Produto.objects.count(), 2)
        self.assertFalse(Pedido.objects.exists())

    def test_cria_produto_com_usuario_autenticado_como_vendedor(self):
        self.client.force_authenticate(self.comprador)
        self.dados_produto["vendedor"] = self.vendedor.pk

        resposta = self.client.post(self.url_produtos, self.dados_produto, format="json")

        self.assertEqual(resposta.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resposta.data["vendedor"], self.comprador.pk)
        self.assertEqual(Produto.objects.get(pk=resposta.data["id"]).vendedor, self.comprador)

    def test_rejeita_preco_negativo_de_produto(self):
        self.client.force_authenticate(self.vendedor)
        self.dados_produto["preco"] = "-0.01"

        resposta = self.client.post(self.url_produtos, self.dados_produto, format="json")

        self.assertEqual(resposta.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("preco", resposta.data)
        self.assertEqual(Produto.objects.count(), 2)

    def test_cria_pedido_aninhado_com_preco_do_banco_e_comprador_autenticado(self):
        self.client.force_authenticate(self.comprador)
        resposta = self.criar_pedido(
            {"produto": self.livro.pk, "quantidade": 2, "preco_unitario": "-1.00", "subtotal": "0.01"},
            {"produto": self.revista.pk, "quantidade": 1, "preco_unitario": "0.01"},
            comprador=self.vendedor.pk,
            total="0.01",
            status=Pedido.Status.PAGO,
        )

        self.assertEqual(resposta.status_code, status.HTTP_201_CREATED)
        pedido = Pedido.objects.get(pk=resposta.data["id"])
        self.assertEqual(pedido.comprador, self.comprador)
        self.assertEqual(resposta.data["comprador"], self.comprador.pk)
        self.assertEqual(resposta.data["status"], Pedido.Status.ABERTO)
        self.assertEqual(resposta.data["total"], "28.75")
        itens = {item["produto"]: item for item in resposta.data["itens"]}
        self.assertEqual(set(itens), {self.livro.pk, self.revista.pk})
        self.assertEqual(itens[self.livro.pk]["preco_unitario"], "12.50")
        self.assertEqual(itens[self.livro.pk]["subtotal"], "25.00")
        self.assertEqual(itens[self.revista.pk]["preco_unitario"], "3.75")
        self.verificar_estoque(self.livro, 3)
        self.verificar_estoque(self.revista, 1)

        Produto.objects.filter(pk=self.livro.pk).update(preco=Decimal("17.50"))
        self.assertEqual(pedido.itens.get(produto=self.livro).preco_unitario, Decimal("12.50"))
        self.assertEqual(pedido.total, Decimal("28.75"))

    def test_rejeita_pedido_vazio_ou_sem_itens(self):
        self.client.force_authenticate(self.comprador)
        for dados in ({}, {"itens": []}):
            with self.subTest(dados=dados):
                resposta = self.client.post(self.url_pedidos, dados, format="json")
                self.assertEqual(resposta.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("itens", resposta.data)
        self.assertFalse(Pedido.objects.exists())

    def test_rejeita_quantidade_invalida_e_produto_inexistente(self):
        self.client.force_authenticate(self.comprador)
        itens = [
            {"produto": self.livro.pk, "quantidade": quantidade}
            for quantidade in (0, -1, 1.5, True, None, "texto", 2_147_483_648)
        ]
        itens.append({"produto": self.revista.pk + 1, "quantidade": 1})
        for item in itens:
            with self.subTest(item=item):
                resposta = self.criar_pedido(item)
                self.assertEqual(resposta.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("itens", resposta.data)
        self.assertFalse(Pedido.objects.exists())
        self.verificar_estoque(self.livro, 5)

    def test_soma_itens_repetidos_do_mesmo_produto(self):
        self.client.force_authenticate(self.comprador)
        resposta = self.criar_pedido(
            {"produto": self.livro.pk, "quantidade": 2},
            {"produto": self.livro.pk, "quantidade": 3},
        )

        self.assertEqual(resposta.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ItemPedido.objects.get().quantidade, 5)
        self.verificar_estoque(self.livro, 0)

    def test_rejeita_estoque_insuficiente_pela_soma_dos_itens_repetidos(self):
        self.client.force_authenticate(self.comprador)
        resposta = self.criar_pedido(
            {"produto": self.livro.pk, "quantidade": 1},
            {"produto": self.revista.pk, "quantidade": 2},
            {"produto": self.revista.pk, "quantidade": 1},
        )

        self.assertEqual(resposta.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Estoque insuficiente", resposta.data["itens"][0])
        self.assertFalse(Pedido.objects.exists())
        self.assertFalse(ItemPedido.objects.exists())
        self.verificar_estoque(self.livro, 5)
        self.verificar_estoque(self.revista, 2)

    def test_erro_no_segundo_item_reverte_pedido_e_estoque(self):
        self.client.force_authenticate(self.comprador)
        self.client.raise_request_exception = False
        criar_item = ItemPedido.objects.create

        def falhar_no_segundo_item(**campos):
            if campos["produto"] == self.revista:
                self.assertTrue(ItemPedido.objects.filter(produto=self.livro).exists())
                self.verificar_estoque(self.livro, 4)
                self.verificar_estoque(self.revista, 1)
                raise IntegrityError("Falha simulada no segundo item.")
            return criar_item(**campos)

        with patch.object(ItemPedido.objects, "create", side_effect=falhar_no_segundo_item) as criar:
            resposta = self.criar_pedido(
                {"produto": self.livro.pk, "quantidade": 1},
                {"produto": self.revista.pk, "quantidade": 1},
            )

        self.assertEqual(resposta.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(criar.call_count, 2)
        self.assertIsInstance(resposta.exc_info[1], IntegrityError)
        self.assertFalse(Pedido.objects.exists())
        self.assertFalse(ItemPedido.objects.exists())
        self.verificar_estoque(self.livro, 5)
        self.verificar_estoque(self.revista, 2)
