from django.urls import path

from . import api_views, pagamento_views, views
from .avaliacoes import avaliar_produto
from .painel_vendedor import painel_vendedor


app_name = "loja"

urlpatterns = [
    path("", views.catalogo, name="catalogo"),
    path("vendedor/", painel_vendedor, name="painel_vendedor"),
    path("carrinho/", views.visualizar_carrinho, name="carrinho"),
    path("carrinho/contagem/", views.contagem_carrinho, name="carrinho_contagem"),
    path("carrinho/adicionar/<int:produto_id>/", views.adicionar_ao_carrinho, name="carrinho_adicionar"),
    path("carrinho/alterar/<int:produto_id>/", views.alterar_quantidade, name="carrinho_alterar"),
    path("carrinho/remover/<int:produto_id>/", views.remover_do_carrinho, name="carrinho_remover"),
    path("checkout/", views.checkout, name="checkout"),
    path("pedidos/", pagamento_views.historico_pedidos, name="historico_pedidos"),
    path("pedidos/<int:pedido_id>/pagar/", pagamento_views.pagar_pedido, name="pagar_pedido"),
    path("pedidos/<int:pedido_id>/cancelar/", pagamento_views.cancelar_pedido_comprador, name="cancelar_pedido"),
    path("produtos/<int:produto_id>/avaliar/", avaliar_produto, name="avaliar_produto"),
    path("pedidos/<int:pedido_id>/pagamento/aprovado/", pagamento_views.retorno_pagamento, name="pagamento_aprovado"),
    path("pedidos/<int:pedido_id>/pagamento/cancelado/", pagamento_views.retorno_pagamento,
         {"cancelado": True}, name="pagamento_cancelado"),
    path("stripe/webhook/", pagamento_views.webhook_stripe, name="webhook_stripe"),
    path("api/produtos/", api_views.ProdutoListCreateView.as_view(), name="api_produtos"),
    path("api/pedidos/", api_views.PedidoCreateView.as_view(), name="api_pedidos"),
    path("api/avaliacoes/", api_views.AvaliacaoCreateView.as_view(), name="api_avaliacoes"),
]
