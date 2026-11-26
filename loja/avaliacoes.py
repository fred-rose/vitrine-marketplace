from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Avaliacao, Pedido, Produto


def registrar_avaliacao(**dados):
    try:
        with transaction.atomic():
            return Avaliacao.objects.create(**dados)
    except IntegrityError as erro:
        if Avaliacao.objects.filter(comprador=dados["comprador"], produto=dados["produto"]).exists():
            raise ValidationError({"produto": "Você já avaliou este produto."}) from erro
        raise


class AvaliacaoForm(forms.Form):
    nota = forms.TypedChoiceField(
        choices=[("", "Selecione uma nota"), *((nota, f"{nota} de 5") for nota in range(1, 6))], coerce=int,
    )
    comentario = forms.CharField(label="Comentário", widget=forms.Textarea(attrs={"rows": 4}))


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@login_required
def avaliar_produto(request, produto_id):
    comprados = Produto.objects.filter(
        itens_pedido__pedido__comprador=request.user, itens_pedido__pedido__status=Pedido.Status.CONCLUIDO,
    ).distinct()
    produto = get_object_or_404(comprados, pk=produto_id)
    if Avaliacao.objects.filter(comprador=request.user, produto=produto).exists():
        messages.info(request, "Você já avaliou este produto.")
        return redirect("loja:historico_pedidos")

    form = AvaliacaoForm(request.POST if request.method == "POST" else None)
    if form.is_valid():
        try:
            registrar_avaliacao(comprador=request.user, produto=produto, **form.cleaned_data)
        except ValidationError as erro:
            form.add_error(None, erro.messages)
        else:
            messages.success(request, "Avaliação publicada.")
            return redirect("loja:historico_pedidos")
    return render(request, "loja/avaliar_produto.html", {"form": form, "produto": produto})
