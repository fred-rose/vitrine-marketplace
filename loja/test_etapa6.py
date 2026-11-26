from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import Avaliacao, Categoria, ItemPedido, Pedido, Produto


class CatalogoEAvaliacoesTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        usuarios = get_user_model().objects
        cls.vendedor = usuarios.create_user(username="vendedor")
        cls.comprador = usuarios.create_user(username="comprador")
        cls.outro_comprador = usuarios.create_user(username="outro_comprador")
        cls.livros = Categoria.objects.create(nome="Livros")
        casa = Categoria.objects.create(nome="Casa")
        cls.livro = Produto.objects.create(
            vendedor=cls.vendedor, categoria=cls.livros, nome="Manual de jardinagem",
            preco="45.90", imagem_url="https://example.com/manual.jpg",
        )
        cls.outro_livro = Produto.objects.create(
            vendedor=cls.vendedor, categoria=cls.livros, nome="Manual de culinária", preco="85.90",
        )
        cls.vaso = Produto.objects.create(
            vendedor=cls.vendedor, categoria=casa, nome="Vaso", descricao="Manual incluso", preco="45.90",
        )

    def setUp(self):
        self.produtos_url = reverse("loja:api_produtos")
        self.avaliacoes_url = reverse("loja:api_avaliacoes")

    def pedido(self, comprador, produto, status):
        pedido = Pedido.objects.create(comprador=comprador, status=status)
        ItemPedido.objects.create(pedido=pedido, produto=produto, quantidade=1, preco_unitario=produto.preco)
        return pedido

    def avaliar(self, **campos):
        dados = {"produto": self.livro.pk, "nota": 4, "comentario": "Boa leitura.", **campos}
        return self.client.post(self.avaliacoes_url, dados, format="json")

    def test_api_combina_filtros_e_pesquisa_somente_no_nome(self):
        casos = (
            ({"q": "MANUAL"}, [self.livro, self.outro_livro]),
            ({"categoria": self.livros.pk}, [self.livro, self.outro_livro]),
            ({"preco_min": "85.90"}, [self.outro_livro]),
            ({"preco_max": "45.90"}, [self.livro, self.vaso]),
            ({"categoria": self.livros.pk, "preco_min": "45.90", "preco_max": "45.90", "q": "manual"}, [self.livro]),
            ({"preco_max": "0"}, []),
        )
        for filtros, esperados in casos:
            with self.subTest(filtros=filtros):
                resposta = self.client.get(self.produtos_url, filtros)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual([item["id"] for item in resposta.data], [produto.pk for produto in esperados])

    def test_filtros_invalidos_retornam_400(self):
        for filtros, campo in (
            ({"categoria": "abc"}, "categoria"),
            ({"preco_min": "NaN"}, "preco_min"),
            ({"preco_max": "-1"}, "preco_max"),
            ({"preco_min": "80", "preco_max": "20"}, "preco_max"),
        ):
            with self.subTest(filtros=filtros):
                resposta = self.client.get(self.produtos_url, filtros)
                self.assertEqual(resposta.status_code, 400)
                self.assertIn(campo, resposta.data)

    def test_api_aceita_imagem_opcional_e_rejeita_protocolo_inadequado(self):
        self.client.force_authenticate(self.vendedor)
        dados = {"nome": "Catálogo", "descricao": "Descrição", "categoria": self.livros.pk, "preco": "20.00"}
        for imagem_url in ("", "https://example.com/catalogo.jpg"):
            resposta = self.client.post(self.produtos_url, {**dados, "imagem_url": imagem_url}, format="json")
            self.assertEqual(resposta.status_code, 201)
            self.assertEqual(resposta.data["imagem_url"], imagem_url)
            self.assertIsNone(resposta.data["media_avaliacoes"])
        resposta = self.client.post(
            self.produtos_url, {**dados, "imagem_url": "ftp://example.com/catalogo.jpg"}, format="json"
        )
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("imagem_url", resposta.data)

    def test_catalogo_sem_javascript_aplica_filtros_e_mostra_imagem_e_media(self):
        self.pedido(self.comprador, self.livro, Pedido.Status.CONCLUIDO)
        Avaliacao.objects.create(comprador=self.comprador, produto=self.livro, nota=5, comentario="Ótimo")
        resposta = self.client.get(reverse("loja:catalogo"), {
            "categoria": self.livros.pk, "preco_max": "55", "q": "livros",
        })
        self.assertContains(resposta, f'value="{self.livros.pk}" selected')
        self.assertContains(resposta, 'src="https://example.com/manual.jpg"')
        self.assertContains(resposta, "Avaliação 5,0 / 5")
        self.assertContains(resposta, '<use href="#icon-caixa"></use>')
        self.assertNotContains(resposta, "Manual de culinária")
        self.assertNotContains(resposta, "<h3>Vaso</h3>")

    def test_avaliacao_exige_autenticacao_e_compra_concluida_do_comprador(self):
        self.pedido(self.outro_comprador, self.livro, Pedido.Status.CONCLUIDO)
        self.pedido(self.comprador, self.outro_livro, Pedido.Status.CONCLUIDO)
        for status in (Pedido.Status.ABERTO, Pedido.Status.PAGO, Pedido.Status.ENVIADO, Pedido.Status.CANCELADO):
            self.pedido(self.comprador, self.livro, status)
        self.assertEqual(self.avaliar().status_code, 403)
        self.client.force_authenticate(self.comprador)
        resposta = self.avaliar(comprador=self.outro_comprador.pk)
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("produto", resposta.data)
        self.assertFalse(Avaliacao.objects.exists())
        with self.assertRaises(ValidationError):
            Avaliacao.objects.create(comprador=self.comprador, produto=self.livro, nota=4, comentario="Teste")

    def test_avaliacao_valida_com_comprador_da_sessao_e_media_publica(self):
        self.pedido(self.comprador, self.livro, Pedido.Status.CONCLUIDO)
        self.client.force_authenticate(self.comprador)
        for invalida in (0, 6, 3.5):
            resposta = self.avaliar(nota=invalida)
            self.assertEqual(resposta.status_code, 400)
            self.assertIn("nota", resposta.data)

        self.assertEqual(self.avaliar(comprador=self.outro_comprador.pk).status_code, 201)
        self.assertEqual(Avaliacao.objects.get().comprador, self.comprador)
        self.assertEqual(self.avaliar().status_code, 400)
        self.pedido(self.outro_comprador, self.livro, Pedido.Status.CONCLUIDO)
        self.client.force_authenticate(self.outro_comprador)
        self.assertEqual(self.avaliar(nota=5).status_code, 201)

        self.client.force_authenticate(None)
        produtos = self.client.get(self.produtos_url).data
        self.assertEqual(produtos[0]["media_avaliacoes"], 4.5)
        self.assertIsNone(produtos[1]["media_avaliacoes"])

    def test_banco_impede_duplicatas_e_notas_fora_da_faixa(self):
        self.pedido(self.comprador, self.livro, Pedido.Status.CONCLUIDO)
        dados = {"comprador": self.comprador, "produto": self.livro, "comentario": "Teste"}
        with self.assertRaises(IntegrityError), transaction.atomic():
            Avaliacao.objects.create(nota=6, **dados)
        Avaliacao.objects.create(nota=1, **dados)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Avaliacao.objects.create(nota=5, **dados)


class SeedDemoTests(TestCase):
    def test_seed_cria_catalogo_e_usuarios_sem_duplicar_nem_repor_estoque(self):
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Produto.objects.count(), 16)
        por_categoria = Produto.objects.values_list("categoria__nome").annotate(total=Count("pk"))
        self.assertEqual(dict(por_categoria), {"Eletrônicos": 4, "Casa": 4, "Livros": 4, "Acessórios": 4})
        self.assertEqual(Produto.objects.filter(estoque=0).count(), 3)
        self.assertEqual(Produto.objects.values("vendedor").distinct().count(), 2)
        usuarios = get_user_model().objects.all()
        self.assertEqual(usuarios.count(), 3)
        self.assertTrue(usuarios.filter(username="demo_comprador").exists())
        self.assertTrue(all(not usuario.has_usable_password() for usuario in usuarios))

        produto = Produto.objects.order_by("pk").first()
        Produto.objects.filter(pk=produto.pk).update(estoque=1)
        saida = StringIO()
        call_command("seed_demo", stdout=saida)
        self.assertEqual(Produto.objects.count(), 16)
        self.assertEqual(usuarios.count(), 3)
        self.assertEqual(Categoria.objects.count(), 4)
        self.assertIn("Produtos novos: 0", saida.getvalue())
        produto.refresh_from_db()
        self.assertEqual(produto.estoque, 1)
