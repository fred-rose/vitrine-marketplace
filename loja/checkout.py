from datetime import timedelta

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import ItemPedido, Pagamento, Pedido, Produto


class CheckoutInvalido(Exception):
    pass


def realizar_checkout(comprador, carrinho):
    if not carrinho:
        raise CheckoutInvalido("O carrinho está vazio.")

    with transaction.atomic():
        produtos = list(
            Produto.objects.select_for_update().filter(pk__in=carrinho).order_by("pk")
        )
        if len(produtos) != len(carrinho):
            raise CheckoutInvalido("Remova do carrinho os produtos indisponíveis.")

        for produto in produtos:
            if produto.estoque < carrinho[str(produto.pk)]:
                raise CheckoutInvalido(f"Estoque insuficiente para {produto.nome}.")

        pedido = Pedido.objects.create(comprador=comprador, estoque_reservado=True)
        for produto in produtos:
            quantidade = carrinho[str(produto.pk)]
            atualizados = Produto.objects.filter(pk=produto.pk, estoque__gte=quantidade).update(
                estoque=F("estoque") - quantidade,
            )
            if not atualizados:
                raise CheckoutInvalido(f"Estoque insuficiente para {produto.nome}.")

            ItemPedido.objects.create(
                pedido=pedido,
                produto=produto,
                quantidade=quantidade,
                preco_unitario=produto.preco,
            )

    return pedido


def limite_reserva():
    return timezone.now() - timedelta(hours=24)


@transaction.atomic
def cancelar_pedido(pedido_id, *, comprador=None, anterior_a=None):
    pedidos = Pedido.objects.select_for_update()
    if comprador is not None:
        pedidos = pedidos.filter(comprador=comprador)
    pedido = pedidos.get(pk=pedido_id)
    if pedido.status == Pedido.Status.CANCELADO:
        return False
    if pedido.status != Pedido.Status.ABERTO or (anterior_a and pedido.criado_em > anterior_a):
        raise CheckoutInvalido("Este pedido não está disponível para cancelamento.")
    if pedido.pagamentos.filter(status__in=(Pagamento.Status.PENDENTE, Pagamento.Status.PAGO)).exists():
        raise CheckoutInvalido("Aguarde a confirmação ou a expiração do pagamento antes de cancelar o pedido.")

    alterados = Pedido.objects.filter(pk=pedido.pk, status=Pedido.Status.ABERTO).update(
        status=Pedido.Status.CANCELADO, estoque_reservado=False,
    )
    if not alterados:
        raise CheckoutInvalido("A situação do pedido mudou. Consulte seu histórico.")
    if pedido.estoque_reservado:
        quantidades = pedido.itens.values("produto_id").annotate(total=Sum("quantidade")).order_by("produto_id")
        for item in quantidades:
            Produto.objects.filter(pk=item["produto_id"]).update(estoque=F("estoque") + item["total"])
    return True
