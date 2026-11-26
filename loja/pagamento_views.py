import logging

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Exists, OuterRef, Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_safe

from .checkout import CheckoutInvalido, cancelar_pedido, limite_reserva
from .models import Avaliacao, ItemPedido, Pagamento, Pedido
from .pagamentos import EventoInvalido, PagamentoIndisponivel, erro_segredo, iniciar_checkout, processar_evento


logger = logging.getLogger(__name__)


@never_cache
@require_safe
@login_required
def historico_pedidos(request):
    itens = ItemPedido.objects.select_related("produto").annotate(ja_avaliado=Exists(
        Avaliacao.objects.filter(comprador=request.user, produto_id=OuterRef("produto_id")),
    ))
    pedidos = Pedido.objects.filter(comprador=request.user).prefetch_related(
        Prefetch("itens", queryset=itens),
        Prefetch("pagamentos", queryset=Pagamento.objects.order_by("-criado_em", "pk")),
    ).annotate(pagamento_pendente=Exists(
        Pagamento.objects.filter(pedido_id=OuterRef("pk"), status=Pagamento.Status.PENDENTE),
    )).order_by("-criado_em", "-pk")
    pagina = Paginator(pedidos, 12).get_page(request.GET.get("pagina"))
    return render(request, "loja/pedidos.html", {"pagina": pagina, "limite_reserva": limite_reserva()})


@never_cache
@require_POST
@login_required
def cancelar_pedido_comprador(request, pedido_id):
    try:
        cancelado = cancelar_pedido(pedido_id, comprador=request.user)
    except Pedido.DoesNotExist:
        return HttpResponse(status=404)
    except CheckoutInvalido as erro:
        messages.error(request, str(erro))
    except DatabaseError:
        logger.warning("Falha ao cancelar o pedido %s.", pedido_id)
        messages.error(request, "Não foi possível cancelar o pedido. Tente novamente.")
    else:
        if cancelado:
            messages.success(request, "Pedido cancelado.")
    return redirect("loja:historico_pedidos")


@never_cache
@require_POST
@login_required
def pagar_pedido(request, pedido_id):
    pedido = get_object_or_404(Pedido, pk=pedido_id, comprador=request.user)
    try:
        url = iniciar_checkout(pedido, request.user)
    except PagamentoIndisponivel as erro:
        messages.error(request, str(erro))
    except stripe.StripeError as erro:
        status = erro.http_status if type(erro.http_status) is int else "sem resposta"
        codigo = erro.code if erro.code in {
            "api_key_expired", "resource_missing", "parameter_unknown", "parameter_missing",
            "parameter_invalid_empty", "parameter_invalid_string", "url_invalid", "idempotency_key_in_use",
        } else "não informado"
        parametro = getattr(erro, "param", None)
        if parametro not in (
            "adaptive_pricing", "adaptive_pricing[enabled]", "success_url", "cancel_url",
            "mode", "line_items", "line_items[0][price_data][unit_amount]", "payment_method_types",
            "allowed_payment_method_types",
        ):
            parametro = "não informado"
        logger.warning(
            "Stripe Checkout sandbox: pedido=%s, SDK=%s, API=%s, erro=%s, HTTP=%s, código=%s, parâmetro=%s.",
            pedido.pk, stripe.VERSION, stripe.api_version, type(erro).__name__, status, codigo, parametro,
        )
        messages.error(request, "Não foi possível abrir o pagamento. Tente novamente em instantes.")
    except DatabaseError as erro:
        logger.warning("Falha ao abrir o Stripe Checkout para o pedido %s (%s).", pedido.pk, type(erro).__name__)
        messages.error(request, "Não foi possível abrir o pagamento. Tente novamente em instantes.")
    except Pedido.DoesNotExist:
        messages.error(request, "Este pedido não está mais disponível para pagamento.")
    else:
        return redirect(url)
    return redirect("loja:historico_pedidos")


@never_cache
@require_safe
@login_required
def retorno_pagamento(request, pedido_id, cancelado=False):
    pedido = get_object_or_404(Pedido, pk=pedido_id, comprador=request.user)
    confirmado = pedido.pagamentos.filter(status=Pagamento.Status.PAGO).exists()
    return render(request, "loja/retorno_pagamento.html", {
        "pedido": pedido, "cancelado": cancelado, "confirmado": confirmado, "limite_reserva": limite_reserva(),
    })


@csrf_exempt
@require_POST
def webhook_stripe(request):
    segredo = settings.STRIPE_WEBHOOK_SECRET
    falha = erro_segredo(segredo, "whsec_")
    if falha:
        logger.warning("Webhook Stripe bloqueado antes da assinatura (STRIPE_WEBHOOK_SECRET: %s).", falha)
        return HttpResponse(status=503)
    try:
        evento = stripe.Webhook.construct_event(request.body, request.headers.get("Stripe-Signature", ""), segredo)
        processar_evento(evento.to_dict())
    except stripe.SignatureVerificationError:
        logger.warning("Webhook Stripe rejeitado: assinatura ausente ou inválida.")
        return HttpResponse(status=400)
    except EventoInvalido:
        logger.warning("Webhook Stripe rejeitado: evento incompatível com a sessão ou pedido sandbox.")
        return HttpResponse(status=400)
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.warning("Webhook Stripe rejeitado: formato do evento inválido.")
        return HttpResponse(status=400)
    except DatabaseError as erro:
        logger.warning("Falha ao processar webhook Stripe (%s).", type(erro).__name__)
        return HttpResponse(status=503)
    return HttpResponse(status=200)
