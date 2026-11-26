from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import Client, TestCase
from django.urls import reverse

from .models import Categoria, Produto


class FrontendTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        vendedor = get_user_model().objects.create_user(username="vendedor")
        categoria = Categoria.objects.create(nome="Livros")
        cls.produto = Produto.objects.create(
            vendedor=vendedor, categoria=categoria, nome="Livro", preco="12.50", estoque=2
        )

    def test_catalogo_referencia_api_e_recursos_locais(self):
        resposta = self.client.get(reverse("loja:catalogo"))
        self.assertTemplateUsed(resposta, "loja/catalogo.html")
        self.assertContains(resposta, f'data-api="{reverse("loja:api_produtos")}"')
        self.assertContains(resposta, 'id="categorias-catalogo"')
        self.assertContains(resposta, 'src="/static/loja/js/loja.mjs"')
        for arquivo in ("loja/css/loja.css", "loja/js/loja.mjs", "loja/img/hero.jpg"):
            self.assertIsNotNone(finders.find(arquivo))

    def test_pesquisa_funciona_sem_javascript(self):
        url = reverse("loja:catalogo")
        self.assertContains(self.client.get(url, {"q": "livros"}), "<h3>Livro</h3>", html=True)
        self.assertNotContains(self.client.get(url, {"q": "revista"}), "<h3>Livro</h3>", html=True)

    def test_login_personalizado_e_carrinho_vazio(self):
        login = self.client.get(reverse("rest_framework:login"))
        self.assertTemplateUsed(login, "rest_framework/login.html")
        self.assertContains(login, "Entre na sua conta")
        self.assertContains(login, 'name="csrfmiddlewaretoken"')
        carrinho = self.client.get(reverse("loja:carrinho"))
        self.assertContains(carrinho, "O carrinho está vazio.")
        self.assertContains(carrinho, 'data-contador="0"')

    def test_adicao_fetch_exige_csrf_e_retorna_contagem_da_sessao(self):
        cliente = Client(enforce_csrf_checks=True)
        adicionar = reverse("loja:carrinho_adicionar", args=[self.produto.pk])
        contagem = reverse("loja:carrinho_contagem")
        self.assertEqual(cliente.post(adicionar, HTTP_ACCEPT="application/json").status_code, 403)
        cliente.get(reverse("loja:catalogo"))
        token = cliente.cookies["csrftoken"].value

        resposta = cliente.post(adicionar, HTTP_ACCEPT="application/json", HTTP_X_CSRFTOKEN=token)

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json()["quantidade"], 1)
        resposta_contagem = cliente.get(contagem)
        self.assertEqual(resposta_contagem.json(), {"quantidade": 1})
        self.assertIn("no-store", resposta_contagem.headers["Cache-Control"])
        self.assertEqual(Client().get(contagem).json(), {"quantidade": 0})
        self.assertContains(cliente.get(reverse("loja:carrinho")), 'data-contador="1"')

    def test_adicao_fetch_rejeita_estoque_insuficiente_e_preserva_sessao(self):
        adicionar = reverse("loja:carrinho_adicionar", args=[self.produto.pk])
        cabecalhos = {"HTTP_ACCEPT": "application/json"}
        self.client.post(adicionar, {"quantidade": 2}, **cabecalhos)
        resposta = self.client.post(adicionar, **cabecalhos)

        self.assertEqual(resposta.status_code, 409)
        self.assertIn("Estoque insuficiente", resposta.json()["mensagem"])
        self.assertEqual(self.client.session["carrinho"], {str(self.produto.pk): 2})
