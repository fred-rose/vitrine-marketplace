from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .checkout import limite_reserva
from .models import Pagamento, Pedido


MOEDA = "brl"
CHAVE_METADATA = "vitrine_pagamento_id"
STATUS_RELEVANTES = (Pagamento.Status.PENDENTE, Pagamento.Status.PAGO)
EVENTO_CONCLUIDO = "checkout.session.completed"
EVENTO_EXPIRADO = "checkout.session.expired"


class PagamentoIndisponivel(Exception):
    pass


class EventoInvalido(Exception):
    pass


def total_centavos(pedido):
    return int(pedido.total.scaleb(2))


def configuracao_stripe():
    chave = settings.STRIPE_SECRET_KEY
    origem = settings.STRIPE_PUBLIC_ORIGIN.rstrip("/")
    try:
        url = urlsplit(origem)
        porta_valida = url.port is None or url.port > 0
    except ValueError:
        porta_valida = False
    if not (
        chave.startswith("sk_test_") and settings.STRIPE_WEBHOOK_SECRET.startswith("whsec_")
        and porta_valida and url.hostname and not (url.username or url.password or url.path or url.query or url.fragment)
        and (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))
    ):
        raise PagamentoIndisponivel("O pagamento de teste está indisponível no momento.")
    return chave, origem


@transaction.atomic
def reservar_pagamento(pedido_id, comprador):
    pedido = Pedido.objects.select_for_update().get(pk=pedido_id, comprador=comprador)
    if pedido.status != Pedido.Status.ABERTO:
        raise PagamentoIndisponivel("Este pedido não está aberto para pagamento.")
    if pedido.criado_em <= limite_reserva():
        raise PagamentoIndisponivel("O prazo para iniciar o pagamento deste pedido terminou.")
    centavos = total_centavos(pedido)
    if not 50 <= centavos <= 99_999_999:
        raise PagamentoIndisponivel("O total do pedido está fora dos limites do pagamento por cartão em reais.")

    pagamento, _ = pedido.pagamentos.filter(status__in=STATUS_RELEVANTES).get_or_create(
        pedido=pedido,
        defaults={"valor_centavos": centavos, "moeda": MOEDA, "status": Pagamento.Status.PENDENTE},
    )
    if pagamento.status == Pagamento.Status.PAGO:
        raise PagamentoIndisponivel("Este pedido já tem um pagamento confirmado.")
    if pagamento.valor_centavos != centavos or pagamento.moeda != MOEDA:
        raise PagamentoIndisponivel("O total do pedido mudou desde o início do pagamento.")
    return pagamento


def sessao_corresponde(sessao, pagamento):
    sessao_id = sessao.get("id")
    metadata = sessao.get("metadata") or {}
    return (
        isinstance(sessao_id, str) and sessao_id.startswith("cs_test_")
        and pagamento.sessao_stripe in (None, sessao_id)
        and sessao.get("object") == "checkout.session" and sessao.get("livemode") is False
        and sessao.get("mode") == "payment"
        and sessao.get("client_reference_id") == str(pagamento.pedido_id)
        and metadata.get(CHAVE_METADATA) == str(pagamento.pk)
        and sessao.get("currency") == pagamento.moeda == MOEDA
        and type(sessao.get("amount_total")) is int and sessao["amount_total"] == pagamento.valor_centavos
    )


def iniciar_checkout(pedido, comprador):
    if pedido.status != Pedido.Status.ABERTO:
        raise PagamentoIndisponivel("Este pedido não está aberto para pagamento.")
    chave, origem = configuracao_stripe()
    pagamento = reservar_pagamento(pedido.pk, comprador)
    sessoes = stripe.StripeClient(chave, max_network_retries=2).v1.checkout.sessions

    if pagamento.sessao_stripe:
        resposta = sessoes.retrieve(pagamento.sessao_stripe)
    else:
        # A Stripe pode descartar chaves de idempotência após 24 horas.
        if timezone.now() - pagamento.criado_em >= timedelta(hours=23):
            raise PagamentoIndisponivel("A tentativa anterior precisa ser verificada antes de iniciar outra.")
        resposta = sessoes.create({
            "mode": "payment",
            "payment_method_types": ["card"],
            "adaptive_pricing": {"enabled": False},
            "client_reference_id": str(pedido.pk),
            "metadata": {CHAVE_METADATA: str(pagamento.pk)},
            "line_items": [{
                "quantity": 1,
                "price_data": {
                    "currency": pagamento.moeda,
                    "unit_amount": pagamento.valor_centavos,
                    "product_data": {"name": f"Pedido Vitrine #{pedido.pk}"},
                },
            }],
            "success_url": origem + reverse("loja:pagamento_aprovado", args=[pedido.pk]),
            "cancel_url": origem + reverse("loja:pagamento_cancelado", args=[pedido.pk]),
        }, options={"idempotency_key": f"vitrine-checkout-{pagamento.pk}"})

    sessao = resposta.to_dict()
    if not sessao_corresponde(sessao, pagamento):
        raise PagamentoIndisponivel("Não foi possível validar a sessão de pagamento.")

    # O webhook pode confirmar ou expirar a sessão antes do retorno da chamada.
    atualizados = Pagamento.objects.filter(
        Q(sessao_stripe__isnull=True) | Q(sessao_stripe=sessao["id"]),
        pk=pagamento.pk, status=Pagamento.Status.PENDENTE, pedido__status=Pedido.Status.ABERTO,
    ).update(sessao_stripe=sessao["id"])
    if not atualizados or sessao.get("status") != "open":
        raise PagamentoIndisponivel("Esta sessão encerrou. Consulte o histórico para acompanhar a confirmação.")

    destino = sessao.get("url") or ""
    url = urlsplit(destino)
    if url.scheme != "https" or url.netloc != "checkout.stripe.com":
        raise PagamentoIndisponivel("Não foi possível validar o endereço do Stripe Checkout.")
    return destino


@transaction.atomic
def processar_evento(evento):
    if evento.get("object") != "event" or evento.get("livemode") is not False:
        raise EventoInvalido
    tipo = evento.get("type")
    if tipo not in (EVENTO_CONCLUIDO, EVENTO_EXPIRADO):
        return

    sessao = evento["data"]["object"]
    identificador = (sessao.get("metadata") or {}).get(CHAVE_METADATA)
    if not identificador:
        return
    evento_id = evento.get("id")
    if not isinstance(evento_id, str) or not evento_id.startswith("evt_"):
        raise EventoInvalido

    try:
        pagamento_id = UUID(identificador)
        pedido_id = Pagamento.objects.values_list("pedido_id", flat=True).get(pk=pagamento_id)
        pedido = Pedido.objects.select_for_update().get(pk=pedido_id)
        pagamento = Pagamento.objects.get(pk=pagamento_id)
    except (ValueError, Pagamento.DoesNotExist, Pedido.DoesNotExist) as erro:
        raise EventoInvalido from erro

    if not sessao_corresponde(sessao, pagamento):
        raise EventoInvalido
    if pagamento.status != Pagamento.Status.PENDENTE:
        return

    if tipo == EVENTO_EXPIRADO:
        if sessao.get("status") != "expired" or sessao.get("payment_status") != "unpaid":
            raise EventoInvalido
        pagamento.status = Pagamento.Status.EXPIRADO
    else:
        intencao = sessao.get("payment_intent")
        if not (
            sessao.get("status") == "complete" and sessao.get("payment_status") == "paid"
            and isinstance(intencao, str) and intencao.startswith("pi_")
            and pedido.status == Pedido.Status.ABERTO and total_centavos(pedido) == pagamento.valor_centavos
        ):
            raise EventoInvalido
        if not Pedido.objects.filter(pk=pedido.pk, status=Pedido.Status.ABERTO).update(status=Pedido.Status.PAGO):
            raise EventoInvalido
        pagamento.status = Pagamento.Status.PAGO
        pagamento.confirmado_em = timezone.now()
        pagamento.intencao_stripe = intencao

    pagamento.evento_stripe = evento_id
    pagamento.sessao_stripe = sessao["id"]
    pagamento.save(update_fields=("status", "evento_stripe", "sessao_stripe", "confirmado_em", "intencao_stripe"))


def reconciliar_sessao_expirada(pagamento):
    if not pagamento.sessao_stripe:
        return False
    chave, _ = configuracao_stripe()
    sessoes = stripe.StripeClient(chave, max_network_retries=2).v1.checkout.sessions
    sessao = sessoes.retrieve(pagamento.sessao_stripe).to_dict()
    if not sessao_corresponde(sessao, pagamento) or sessao.get("status") != "expired" or sessao.get("payment_status") != "unpaid":
        return False
    with transaction.atomic():
        Pedido.objects.select_for_update().get(pk=pagamento.pedido_id)
        return bool(Pagamento.objects.filter(pk=pagamento.pk, status=Pagamento.Status.PENDENTE).update(
            status=Pagamento.Status.EXPIRADO,
        ))
