import logging
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.db import DatabaseError, transaction
from django.db.models import Avg, Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from .carrinho import contar_itens
from .checkout import CheckoutInvalido, realizar_checkout
from .filtros import FiltrosProdutoForm
from .models import Categoria, Produto


logger = logging.getLogger(__name__)
MAX_QUANTIDADE = 2_147_483_647


class QuantidadeForm(forms.Form):
    quantidade = forms.IntegerField(min_value=1, max_value=MAX_QUANTIDADE)


@require_safe
def catalogo(request):
    filtros = FiltrosProdutoForm(request.GET)
    pesquisa = request.GET.get("q", "").strip()
    produtos = Produto.objects.select_related("categoria").annotate(
        media_avaliacoes=Avg("avaliacoes__nota")
    ).order_by("nome", "pk")
    produtos = filtros.filtrar(produtos) if filtros.is_valid() else produtos.none()
    if pesquisa:
        produtos = produtos.filter(
            Q(nome__icontains=pesquisa)
            | Q(descricao__icontains=pesquisa)
            | Q(categoria__nome__icontains=pesquisa)
        )
    return render(
        request,
        "loja/catalogo.html",
        {
            "produtos": produtos,
            "categorias": dict(Categoria.objects.order_by("nome").values_list("pk", "nome")),
            "filtros": filtros,
            "pesquisa": pesquisa,
        },
    )


@never_cache
@require_safe
def contagem_carrinho(request):
    return JsonResponse({"quantidade": contar_itens(request.session)})


@require_safe
def visualizar_carrinho(request):
    carrinho = request.session.get("carrinho", {})
    produtos = Produto.objects.in_bulk(carrinho)
    itens = []
    total = Decimal("0.00")
    for chave, quantidade in carrinho.items():
        produto_id = int(chave)
        produto = produtos.get(produto_id)
        subtotal = None
        if produto is not None:
            subtotal = produto.preco * quantidade
            total += subtotal
        itens.append(
            {
                "produto_id": produto_id,
                "produto": produto,
                "quantidade": quantidade,
                "subtotal": subtotal,
            }
        )

    return render(
        request,
        "loja/carrinho.html",
        {"itens": itens, "total": total},
    )


@require_POST
def adicionar_ao_carrinho(request, produto_id):
    resposta_json = request.headers.get("Accept") == "application/json"
    try:
        produto = Produto.objects.get(pk=produto_id)
    except Produto.DoesNotExist:
        if resposta_json:
            return JsonResponse({"mensagem": "Este produto está indisponível."}, status=404)
        raise Http404("Produto indisponível.")
    carrinho = request.session.get("carrinho", {})
    chave = str(produto.pk)
    formulario = QuantidadeForm({"quantidade": request.POST.get("quantidade", "1")})
    if formulario.is_valid():
        quantidade = carrinho.get(chave, 0) + formulario.cleaned_data["quantidade"]
        if quantidade <= MAX_QUANTIDADE:
            if resposta_json and quantidade > produto.estoque:
                return JsonResponse(
                    {"mensagem": f"Estoque insuficiente para {produto.nome}."}, status=409
                )
            carrinho[chave] = quantidade
            request.session["carrinho"] = carrinho
            if resposta_json:
                return JsonResponse(
                    {
                        "quantidade": contar_itens(request.session),
                        "mensagem": f"{produto.nome} adicionado ao carrinho.",
                    }
                )
            messages.success(request, f"{produto.nome} adicionado ao carrinho.")
            return redirect("loja:carrinho")

    if resposta_json:
        return JsonResponse({"mensagem": "Informe uma quantidade válida."}, status=400)
    messages.error(request, "Informe uma quantidade válida.")
    return redirect("loja:carrinho")


@require_POST
def alterar_quantidade(request, produto_id):
    carrinho = request.session.get("carrinho", {})
    chave = str(produto_id)
    formulario = QuantidadeForm(request.POST)
    if chave not in carrinho:
        messages.error(request, "O produto não está no carrinho.")
    elif not formulario.is_valid():
        messages.error(request, "Informe uma quantidade válida.")
    else:
        carrinho[chave] = formulario.cleaned_data["quantidade"]
        request.session["carrinho"] = carrinho
        messages.success(request, "Quantidade atualizada.")

    return redirect("loja:carrinho")


@require_POST
def remover_do_carrinho(request, produto_id):
    carrinho = request.session.get("carrinho", {})
    if carrinho.pop(str(produto_id), None) is not None:
        request.session["carrinho"] = carrinho
        messages.success(request, "Produto removido do carrinho.")
    return redirect("loja:carrinho")


@require_POST
def checkout(request):
    if not request.user.is_authenticated:
        return redirect_to_login(reverse("loja:carrinho"))

    carrinho = request.session.get("carrinho", {})
    try:
        pedido = realizar_checkout(request.user, carrinho)
    except CheckoutInvalido as erro:
        messages.error(request, str(erro))
    except DatabaseError:
        logger.exception("Erro ao registrar pedido no checkout.")
        messages.error(request, "Não foi possível registrar o pedido. Tente novamente.")
    else:
        transaction.on_commit(lambda: request.session.pop("carrinho", None))
        messages.success(request, f"Pedido {pedido.pk} registrado com sucesso.")

    return redirect("loja:carrinho")
