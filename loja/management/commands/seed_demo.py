from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from loja.models import Categoria, ItemPedido, Pedido, Produto


PRODUTOS = {
    "Eletrônicos": (
        ("Fone Bluetooth Wave", "Fone sem fio com microfone e autonomia de até 24 horas.", "189.90", 17),
        ("Teclado compacto sem fio", "Teclado ABNT2 de perfil baixo com conexão por receptor USB.", "139.90", 12),
        ("Carregador USB-C 30 W", "Carregador bivolt com Power Delivery e uma porta USB-C.", "79.90", 26),
        ("Caixa de som portátil Mini", "Caixa Bluetooth de 10 W com proteção contra respingos.", "159.90", 0),
    ),
    "Casa": (
        ("Garrafa térmica de inox", "Garrafa de 750 ml com parede dupla e tampa de rosca.", "99.90", 14),
        ("Jogo de canecas de cerâmica", "Conjunto com quatro canecas de 300 ml em tons neutros.", "84.90", 9),
        ("Luminária de mesa articulada", "Luminária com base de metal e soquete E27.", "149.90", 6),
        ("Organizador de bambu", "Bandeja com três divisórias para bancada ou gaveta.", "59.90", 0),
    ),
    "Livros": (
        ("Horta em Apartamento", "Guia ilustrado para cultivar temperos e hortaliças em vasos.", "49.90", 21),
        ("Cadernos de uma Viagem", "Crônicas de estrada reunidas em uma edição de capa brochura.", "39.90", 11),
        ("Cozinha de Todo Dia", "Receitas para a semana com ingredientes fáceis de encontrar.", "64.90", 15),
        ("Fotografia na Cidade", "Exercícios de composição e luz para fotografar espaços urbanos.", "74.90", 0),
    ),
    "Acessórios": (
        ("Mochila urbana 18 L", "Mochila com costas acolchoadas e bolso para notebook de 14 polegadas.", "169.90", 8),
        ("Necessaire impermeável", "Estojo leve com fechamento em zíper e forro lavável.", "42.90", 18),
        ("Carteira slim de lona", "Carteira compacta com seis espaços para cartões.", "54.90", 13),
        ("Boné de algodão ajustável", "Boné de seis painéis com fivela de ajuste metálica.", "49.90", 7),
    ),
}

CONTAS_DEMO = {"demo_vendedor_1": "Lia", "demo_vendedor_2": "Rafael", "demo_comprador": "Marina"}

IMAGENS = {
    "Eletrônicos": ("fone-bluetooth", "teclado-sem-fio", "carregador-usb-c", "caixa-de-som"),
    "Casa": ("garrafa-termica", "canecas-ceramica", "luminaria-articulada", "organizador-bambu"),
    "Livros": ("horta-apartamento", "cadernos-viagem", "cozinha-todo-dia", "fotografia-cidade"),
    "Acessórios": ("mochila-urbana", "necessaire", "carteira-lona", "bone-algodao"),
}

VENDAS = (
    (2, Pedido.Status.CONCLUIDO, (("Eletrônicos", 0, 2), ("Casa", 1, 1), ("Livros", 0, 1))),
    (5, Pedido.Status.CONCLUIDO, (("Acessórios", 0, 1), ("Acessórios", 1, 2), ("Livros", 1, 1))),
    (9, Pedido.Status.ENVIADO, (("Eletrônicos", 1, 2), ("Casa", 0, 3), ("Acessórios", 3, 1))),
    (14, Pedido.Status.CONCLUIDO, (("Eletrônicos", 2, 1), ("Livros", 2, 2), ("Acessórios", 2, 2))),
)


class Command(BaseCommand):
    help = "Cadastra contas, produtos e vendas fictícias de demonstração."

    @transaction.atomic
    def handle(self, *args, **options):
        usuarios = get_user_model().objects
        permissoes = Permission.objects.filter(
            content_type__app_label="loja", content_type__model="produto",
            codename__in=("view_produto", "add_produto", "change_produto", "delete_produto"),
        )
        contas = {}
        for username, nome in CONTAS_DEMO.items():
            vendedor = username != "demo_comprador"
            usuario, criado = usuarios.select_for_update().get_or_create(
                username=username,
                defaults={"first_name": nome, "password": make_password(None), "is_staff": vendedor},
            )
            if criado and vendedor:
                usuario.user_permissions.add(*permissoes)
            contas[username] = usuario

        criados = 0
        catalogo = {}
        for nome_categoria, produtos in PRODUTOS.items():
            categoria, _ = Categoria.objects.get_or_create(nome=nome_categoria)
            for indice, (nome, descricao, preco, estoque) in enumerate(produtos):
                produto, criado = Produto.objects.get_or_create(
                    vendedor=contas[f"demo_vendedor_{indice % 2 + 1}"],
                    nome=nome,
                    defaults={
                        "categoria": categoria, "descricao": descricao,
                        "preco": Decimal(preco), "estoque": estoque,
                        "imagem_local": f"loja/img/produtos/{IMAGENS[nome_categoria][indice]}.jpg",
                    },
                )
                criados += criado
                catalogo[nome_categoria, indice] = produto

        vendas_criadas = 0
        agora = timezone.now()
        for indice, (dias, status, itens) in enumerate(VENDAS, start=1):
            pedido, criado = Pedido.objects.get_or_create(
                referencia_demo=f"vitrine-venda-{indice}",
                defaults={"comprador": contas["demo_comprador"], "status": status},
            )
            if criado:
                Pedido.objects.filter(pk=pedido.pk).update(criado_em=agora - timedelta(days=dias))
                for categoria, posicao, quantidade in itens:
                    produto = catalogo[categoria, posicao]
                    ItemPedido.objects.create(
                        pedido=pedido, produto=produto, quantidade=quantidade, preco_unitario=produto.preco,
                    )
                vendas_criadas += 1

        self.stdout.write(self.style.SUCCESS(
            f"Demonstração pronta. Produtos novos: {criados}. Vendas fictícias novas: {vendas_criadas}."
        ))
