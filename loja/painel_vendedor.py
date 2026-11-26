from decimal import Decimal

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import BigIntegerField, Count, F, Q, Sum
from django.db.models.functions import Cast, Round
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import ItemPedido, Pedido, Produto


STATUS_VENDA = (Pedido.Status.PAGO, Pedido.Status.ENVIADO, Pedido.Status.CONCLUIDO)
# Soma em centavos inteiros para evitar imprecisão monetária no SQLite.
SUBTOTAL_CENTAVOS = F("quantidade") * Cast(Round(F("preco_unitario") * 100), BigIntegerField())


class PeriodoForm(forms.Form):
    inicio = forms.DateField(
        label="Data inicial", required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    fim = forms.DateField(
        label="Data final", required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def clean(self):
        dados = super().clean()
        inicio, fim = dados.get("inicio"), dados.get("fim")
        if inicio and fim and inicio > fim:
            self.add_error("fim", "A data final deve ser igual ou posterior à data inicial.")
        return dados


def relatorio_vendas(vendas, *campos):
    linhas = list(vendas.values(*campos).annotate(
        unidades=Sum("quantidade"), centavos=Sum(SUBTOTAL_CENTAVOS),
    ).order_by("-unidades", *campos))
    for linha in linhas:
        linha["faturamento"] = Decimal(linha.pop("centavos")).scaleb(-2)
    return linhas


@never_cache
@require_safe
@login_required
def painel_vendedor(request):
    usuario = request.user
    if not (usuario.is_active and usuario.is_staff and (
        usuario.has_perm("loja.view_produto") or usuario.has_perm("loja.change_produto")
    )):
        raise PermissionDenied

    periodo = PeriodoForm(request.GET)
    estoque = Produto.objects.filter(vendedor=usuario).aggregate(
        cadastrados=Count("pk"), disponivel=Sum("estoque", default=0),
        esgotados=Count("pk", filter=Q(estoque=0)),
    )
    contexto = {"periodo": periodo, "estoque": estoque}
    if not periodo.is_valid():
        return render(request, "loja/painel_vendedor.html", contexto, status=400)

    recebidos = ItemPedido.objects.filter(produto__vendedor=usuario)
    inicio, fim = periodo.cleaned_data["inicio"], periodo.cleaned_data["fim"]
    if inicio:
        recebidos = recebidos.filter(pedido__criado_em__date__gte=inicio)
    if fim:
        recebidos = recebidos.filter(pedido__criado_em__date__lte=fim)

    venda_confirmada = Q(pedido__status__in=STATUS_VENDA)
    resumo = recebidos.aggregate(
        pedidos=Count("pedido", distinct=True),
        unidades=Sum("quantidade", filter=venda_confirmada, default=0),
        centavos=Sum(SUBTOTAL_CENTAVOS, filter=venda_confirmada, default=0),
    )
    resumo["faturamento"] = Decimal(resumo.pop("centavos")).scaleb(-2)

    vendas = recebidos.filter(venda_confirmada)
    por_produto = relatorio_vendas(vendas, "produto__nome", "produto_id", "produto__categoria__nome")
    por_categoria = relatorio_vendas(vendas, "produto__categoria__nome")
    historico = vendas.select_related("pedido", "produto").order_by("-pedido__criado_em", "-pedido_id", "pk")
    contexto.update(
        resumo=resumo,
        vendas_demo=vendas.filter(pedido__referencia_demo__isnull=False).exists(),
        por_produto=por_produto,
        por_categoria=por_categoria,
        ranking=por_produto[:5],
        max_unidades=por_produto[0]["unidades"] if por_produto else 0,
        max_faturamento=max((linha["faturamento"] for linha in por_categoria), default=0),
        historico=Paginator(historico, 20).get_page(request.GET.get("pagina")),
    )
    return render(request, "loja/painel_vendedor.html", contexto)
