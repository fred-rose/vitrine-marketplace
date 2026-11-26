from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from .models import Categoria, Produto


class ProdutoAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        usuarios = get_user_model().objects
        cls.vendedor = usuarios.create_user(username="vendedor", is_staff=True)
        cls.outro_vendedor = usuarios.create_user(username="outro", is_staff=True)
        cls.superusuario = usuarios.create_superuser(username="administrador")
        permissoes = Permission.objects.filter(
            content_type__app_label="loja",
            codename__in=("add_produto", "change_produto", "delete_produto", "view_produto"),
        )
        for vendedor in (cls.vendedor, cls.outro_vendedor):
            vendedor.user_permissions.add(*permissoes)

        cls.categoria = Categoria.objects.create(nome="Livros")
        cls.produto = Produto.objects.create(
            vendedor=cls.vendedor, categoria=cls.categoria, nome="Produto próprio", preco="10.00"
        )
        cls.outro_produto = Produto.objects.create(
            vendedor=cls.outro_vendedor,
            categoria=cls.categoria,
            nome="Produto do outro vendedor",
            preco="20.00",
        )

    def dados_produto(self, nome):
        return {
            "nome": nome,
            "descricao": "Descrição do produto",
            "categoria": self.categoria.pk,
            "preco": "14.50",
            "estoque": 3,
            "vendedor": self.outro_vendedor.pk,
            "_save": "Salvar",
        }

    def test_cada_vendedor_lista_apenas_os_proprios_produtos(self):
        url = reverse("admin:loja_produto_changelist")
        for vendedor, produto in (
            (self.vendedor, self.produto),
            (self.outro_vendedor, self.outro_produto),
        ):
            with self.subTest(vendedor=vendedor.username):
                self.client.force_login(vendedor)
                resposta = self.client.get(url)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(list(resposta.context["cl"].result_list), [produto])

    def test_vendedor_nao_acessa_produto_alheio_por_url_direta(self):
        self.client.force_login(self.vendedor)
        url_lista = reverse("admin:loja_produto_changelist")
        url_edicao = reverse("admin:loja_produto_change", args=[self.outro_produto.pk])
        url_exclusao = reverse("admin:loja_produto_delete", args=[self.outro_produto.pk])

        self.assertRedirects(self.client.get(url_edicao), reverse("admin:index"))
        self.assertRedirects(
            self.client.post(url_edicao, self.dados_produto("Alteração indevida")),
            reverse("admin:index"),
        )
        self.assertRedirects(
            self.client.post(url_exclusao, {"post": "yes"}), reverse("admin:index")
        )
        self.outro_produto.refresh_from_db()
        self.assertEqual(self.outro_produto.nome, "Produto do outro vendedor")
        self.assertRedirects(
            self.client.post(
                url_lista,
                {
                    "action": "delete_selected",
                    "_selected_action": self.outro_produto.pk,
                    "post": "yes",
                },
            ),
            url_lista,
        )
        self.assertTrue(Produto.objects.filter(pk=self.outro_produto.pk).exists())

    def test_cadastro_atribui_vendedor_autenticado(self):
        self.client.force_login(self.vendedor)
        url = reverse("admin:loja_produto_add")
        resposta = self.client.get(url)
        self.assertNotIn("vendedor", resposta.context["adminform"].form.fields)

        resposta = self.client.post(url, self.dados_produto("Novo produto"))

        self.assertRedirects(resposta, reverse("admin:loja_produto_changelist"))
        produto = Produto.objects.get(nome="Novo produto")
        self.assertEqual(produto.vendedor, self.vendedor)

    def test_vendedor_nao_altera_proprietario_de_produto_proprio(self):
        self.client.force_login(self.vendedor)
        url = reverse("admin:loja_produto_change", args=[self.produto.pk])
        resposta = self.client.get(url)
        self.assertNotIn("vendedor", resposta.context["adminform"].form.fields)

        resposta = self.client.post(url, self.dados_produto("Produto renomeado"))

        self.assertRedirects(resposta, reverse("admin:loja_produto_changelist"))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.nome, "Produto renomeado")
        self.assertEqual(self.produto.vendedor, self.vendedor)

    def test_superusuario_lista_todos_os_produtos(self):
        self.client.force_login(self.superusuario)
        resposta = self.client.get(reverse("admin:loja_produto_changelist"))

        self.assertEqual(resposta.status_code, 200)
        self.assertCountEqual(resposta.context["cl"].result_list, [self.produto, self.outro_produto])
