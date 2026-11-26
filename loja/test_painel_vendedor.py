from datetime import datetime, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from .models import Categoria, ItemPedido, Pedido, Produto


class PainelVendedorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        usuarios = get_user_model().objects
        cls.vendedor = usuarios.create_user(username="vendedor", is_staff=True)
        cls.outro_vendedor = usuarios.create_user(username="outro_vendedor", is_staff=True)
        cls.comprador = usuarios.create_user(username="comprador")
        cls.staff_sem_permissao = usuarios.create_user(username="staff", is_staff=True)
        cls.admin = usuarios.create_superuser(username="admin")
        permissoes = Permission.objects.filter(content_type__app_label="loja", content_type__model="produto")
        visualizar = permissoes.get(codename="view_produto")
        cls.vendedor.user_permissions.add(visualizar)
        cls.outro_vendedor.user_permissions.add(permissoes.get(codename="change_produto"))
        cls.comprador.user_permissions.add(visualizar)

        livros = Categoria.objects.create(nome="Livros")
        casa = Categoria.objects.create(nome="Casa")
        cls.livro = Produto.objects.create(vendedor=cls.vendedor, categoria=livros, nome="Livro próprio", preco="199.99", estoque=7)
        cls.vaso = Produto.objects.create(vendedor=cls.vendedor, categoria=casa, nome="Vaso próprio", preco="35.00")
        cls.esgotado = Produto.objects.create(vendedor=cls.vendedor, categoria=livros, nome="Livro esgotado", preco="20.00")
        cls.alheio = Produto.objects.create(vendedor=cls.outro_vendedor, categoria=livros, nome="Produto de outro vendedor", preco="200.00", estoque=99)
        Produto.objects.create(vendedor=cls.comprador, categoria=casa, nome="Produto da API", preco="10.00")

        # Em São Paulo, 02:30 UTC ainda pertence ao dia anterior.
        cls.pago = cls.criar_pedido(Pedido.Status.PAGO, 10, 3, (cls.livro, 2, "12.35"), (cls.vaso, 1, "10.10"), (cls.alheio, 9, "111.11"))
        cls.enviado = cls.criar_pedido(Pedido.Status.ENVIADO, 11, 2, (cls.livro, 3, "0.10"))
        cls.concluido = cls.criar_pedido(Pedido.Status.CONCLUIDO, 10, 2, (cls.vaso, 2, "14.99"))
        cls.aberto = cls.criar_pedido(Pedido.Status.ABERTO, 10, 5, (cls.livro, 80, "100.00"))
        cls.cancelado = cls.criar_pedido(Pedido.Status.CANCELADO, 11, 3, (cls.livro, 70, "100.00"))
        cls.pedido_alheio = cls.criar_pedido(Pedido.Status.PAGO, 10, 7, (cls.alheio, 1, "8.75"))

    @classmethod
    def criar_pedido(cls, status, dia, hora, *itens):
        pedido = Pedido.objects.create(comprador=cls.comprador, status=status)
        Pedido.objects.filter(pk=pedido.pk).update(criado_em=datetime(2026, 3, dia, hora, 30, tzinfo=timezone.utc))
        for produto, quantidade, preco in itens:
            ItemPedido.objects.create(pedido=pedido, produto=produto, quantidade=quantidade, preco_unitario=preco)
        return pedido

    def setUp(self):
        self.url = reverse("loja:painel_vendedor")

    def test_autenticacao_e_permissoes_de_vendedor(self):
        self.assertRedirects(self.client.get(self.url), f"{reverse('rest_framework:login')}?next={self.url}")
        for usuario in (self.comprador, self.staff_sem_permissao):
            with self.subTest(usuario=usuario.username):
                self.client.force_login(usuario)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.assertNotContains(self.client.get(reverse("loja:catalogo")), f'href="{self.url}"')
        for usuario in (self.vendedor, self.outro_vendedor):
            with self.subTest(usuario=usuario.username):
                self.client.force_login(usuario)
                resposta = self.client.get(self.url)
                self.assertContains(resposta, f'href="{reverse("admin:loja_produto_changelist")}"')
                self.assertContains(resposta, f'href="{self.url}" aria-current="page"')
                self.assertIn("no-store", resposta.headers["Cache-Control"])

    def test_faturamento_usa_preco_historico_e_apenas_status_de_venda(self):
        self.client.force_login(self.vendedor)
        resposta = self.client.get(self.url)
        self.assertEqual(resposta.context["estoque"], {"cadastrados": 3, "disponivel": 7, "esgotados": 2})
        resumo = resposta.context["resumo"]
        self.assertEqual(resumo["faturamento"], Decimal("65.08"))
        self.assertEqual(resumo["unidades"], 8)
        self.assertEqual(resumo["pedidos"], 5)
        por_produto = resposta.context["por_produto"]
        self.assertEqual([(linha["produto_id"], linha["unidades"], linha["faturamento"]) for linha in por_produto], [
            (self.livro.pk, 5, Decimal("25.00")), (self.vaso.pk, 3, Decimal("40.08")),
        ])
        self.assertEqual(resposta.context["ranking"], por_produto)
        self.assertEqual({linha["produto__categoria__nome"]: linha["faturamento"] for linha in resposta.context["por_categoria"]}, {
            "Livros": Decimal("25.00"), "Casa": Decimal("40.08"),
        })
        self.assertEqual([item.pedido_id for item in resposta.context["historico"]], [
            self.enviado.pk, self.pago.pk, self.pago.pk, self.concluido.pk,
        ])
        self.assertContains(resposta, "R$ 65,08")
        self.assertContains(resposta, "R$ 24,70")
        self.assertContains(resposta, "<meter", count=4)
        self.assertNotContains(resposta, self.alheio.nome)

    def test_outro_vendedor_so_ve_sua_parte_do_pedido_compartilhado(self):
        self.client.force_login(self.outro_vendedor)
        resposta = self.client.get(self.url, {"vendedor": self.vendedor.pk})
        self.assertEqual(resposta.context["resumo"]["faturamento"], Decimal("1008.74"))
        self.assertEqual(resposta.context["resumo"]["pedidos"], 2)
        self.assertEqual(resposta.context["resumo"]["unidades"], 10)
        self.assertEqual(resposta.context["estoque"], {"cadastrados": 1, "disponivel": 99, "esgotados": 0})
        self.assertEqual({item.produto_id for item in resposta.context["historico"]}, {self.alheio.pk})
        self.assertNotContains(resposta, self.livro.nome)
        self.assertNotContains(resposta, self.vaso.nome)

    def test_superusuario_tambem_tem_escopo_proprio_e_estado_vazio(self):
        self.client.force_login(self.admin)
        resposta = self.client.get(self.url)
        self.assertContains(resposta, "Nenhuma venda no período.")
        self.assertEqual(resposta.context["resumo"]["pedidos"], 0)
        self.assertEqual(resposta.context["resumo"]["faturamento"], Decimal("0.00"))
        self.assertEqual(resposta.context["estoque"], {"cadastrados": 0, "disponivel": 0, "esgotados": 0})

    def test_periodo_inclusivo_no_fuso_local_e_estoque_atual(self):
        self.client.force_login(self.vendedor)
        resposta = self.client.get(self.url, {"inicio": "2026-03-10", "fim": "2026-03-10"})
        self.assertEqual(resposta.context["resumo"]["faturamento"], Decimal("35.10"))
        self.assertEqual(resposta.context["resumo"]["unidades"], 6)
        self.assertEqual(resposta.context["resumo"]["pedidos"], 3)
        self.assertEqual(resposta.context["estoque"]["disponivel"], 7)
        self.assertEqual([item.pedido_id for item in resposta.context["historico"]], [self.enviado.pk, self.pago.pk, self.pago.pk])
        for filtros, esperado in (({"inicio": "2026-03-10"}, "35.10"), ({"fim": "2026-03-09"}, "29.98")):
            with self.subTest(filtros=filtros):
                resumo = self.client.get(self.url, filtros).context["resumo"]
                self.assertEqual(resumo["faturamento"], Decimal(esperado))

    def test_filtro_invalido_retorna_erros_sem_exibir_vendas(self):
        self.client.force_login(self.vendedor)
        for filtros, campo in (({"inicio": "data inválida"}, "inicio"), ({"inicio": "2026-03-11", "fim": "2026-03-10"}, "fim")):
            with self.subTest(filtros=filtros):
                resposta = self.client.get(self.url, filtros)
                self.assertEqual(resposta.status_code, 400)
                self.assertIn(campo, resposta.context["periodo"].errors)
                self.assertNotIn("resumo", resposta.context)

    def test_historico_pagina_com_filtro_preservado(self):
        self.client.force_login(self.vendedor)
        for _ in range(18):
            self.criar_pedido(Pedido.Status.PAGO, 10, 6, (self.livro, 1, "1.00"))
        filtros = {"inicio": "2026-03-10", "fim": "2026-03-10"}
        primeira = self.client.get(self.url, filtros)
        segunda = self.client.get(self.url, {**filtros, "pagina": 2})
        self.assertContains(primeira, "?inicio=2026-03-10&amp;fim=2026-03-10&amp;pagina=2#historico-titulo")
        self.assertEqual(len(primeira.context["historico"].object_list), 20)
        self.assertEqual(len(segunda.context["historico"].object_list), 1)
        self.assertEqual(primeira.context["resumo"], segunda.context["resumo"])
