import logging
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
logger = logging.getLogger(__name__)


class PagamentoIndisponivel(Exception):
    pass


class EventoInvalido(Exception):
    pass


def total_centavos(pedido):
    return int(pedido.total.scaleb(2))


def erro_segredo(valor, prefixo):
    if not valor:
        return "ausente"
    if any(caractere.isspace() for caractere in valor):
        return "contém espaço em branco interno"
    if valor.startswith(("'", '"')) or valor.endswith(("'", '"')):
        return "contém aspas literais nas bordas"
    if prefixo == "sk_test_" and valor.startswith(("sk_live_", "rk_live_")):
        return "chave live bloqueada"
    if prefixo == "sk_test_" and valor.startswith("rk_test_"):
        return "chave restrita rk_test_ não aceita pela configuração atual"
    if not valor.startswith(prefixo):
        return f"prefixo esperado: {prefixo}"
    if valor == prefixo:
        return "valor incompleto após o prefixo"
    if valor in ("sk_test_substitua_pela_chave_do_sandbox", "whsec_substitua_pelo_segredo_do_endpoint"):
        return "valor fictício do .env.example"
    return None


def erro_origem_stripe(origem):
    if not origem:
        return "ausente"
    if any(caractere.isspace() for caractere in origem):
        return "contém espaço em branco interno"
    if origem.startswith(("'", '"')) or origem.endswith(("'", '"')):
        return "contém aspas literais nas bordas"
    try:
        url = urlsplit(origem)
    except ValueError:
        return "URL malformada"
    try:
        if url.port == 0 or (url.port is None and url.netloc.endswith(":")):
            return "porta inválida"
    except ValueError:
        return "porta inválida"
    if not url.hostname:
        return "hostname ausente"
    if url.username is not None or url.password is not None:
        return "contém credenciais na URL"
    if url.path or "?" in origem or "#" in origem:
        return "contém caminho, consulta ou fragmento"
    if url.scheme not in {"http", "https"}:
        return "esquema deve ser HTTPS ou HTTP de loopback"
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return "HTTP só é permitido em loopback"
    return None


def configuracao_stripe():
    chave = settings.STRIPE_SECRET_KEY
    origem = settings.STRIPE_PUBLIC_ORIGIN.rstrip("/")
    falhas = {
        "STRIPE_SECRET_KEY": erro_segredo(chave, "sk_test_"),
        "STRIPE_WEBHOOK_SECRET": erro_segredo(settings.STRIPE_WEBHOOK_SECRET, "whsec_"),
        "STRIPE_PUBLIC_ORIGIN": erro_origem_stripe(origem),
    }
    motivos = [f"{nome}: {motivo}" for nome, motivo in falhas.items() if motivo]
    if motivos:
        logger.warning("Stripe sandbox bloqueado antes da API (%s).", "; ".join(motivos))
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


def criar_sessao_checkout(sessoes, parametros, chave):
    opcoes = {"idempotency_key": chave}
    try:
        return sessoes.create(parametros, options=opcoes)
    except stripe.IdempotencyError:
        # A API pode ter registrado a requisição antiga sob a mesma chave.
        anteriores = parametros.copy()
        anteriores["payment_method_types"] = anteriores.pop("allowed_payment_method_types")
        try:
            return sessoes.create(anteriores, options=opcoes)
        except stripe.InvalidRequestError as erro:
            if not (
                erro.http_status == 400 and erro.param == "payment_method_types"
                and "no longer supported" in str(erro)
            ):
                raise
        return sessoes.create(parametros, options={"idempotency_key": f"{chave}-endive"})


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
        resposta = criar_sessao_checkout(sessoes, {
            "mode": "payment",
            "allowed_payment_method_types": ["card"],
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
        }, f"vitrine-checkout-{pagamento.pk}")

    sessao = resposta.to_dict()
    if not sessao_corresponde(sessao, pagamento):
        logger.warning("Stripe Checkout: sessão retornada não corresponde ao pedido sandbox %s.", pedido.pk)
        raise PagamentoIndisponivel("Não foi possível validar a sessão de pagamento.")

    # O webhook pode confirmar ou expirar a sessão antes do retorno da chamada.
    atualizados = Pagamento.objects.filter(
        Q(sessao_stripe__isnull=True) | Q(sessao_stripe=sessao["id"]),
        pk=pagamento.pk, status=Pagamento.Status.PENDENTE, pedido__status=Pedido.Status.ABERTO,
    ).update(sessao_stripe=sessao["id"])
    if not atualizados or sessao.get("status") != "open":
        logger.warning("Stripe Checkout: sessão já encerrada ou pedido alterado (pedido %s).", pedido.pk)
        raise PagamentoIndisponivel("Esta sessão encerrou. Consulte o histórico para acompanhar a confirmação.")

    destino = sessao.get("url") or ""
    url = urlsplit(destino)
    if url.scheme != "https" or url.netloc != "checkout.stripe.com":
        logger.warning("Stripe Checkout: endereço de redirecionamento fora do destino esperado (pedido %s).", pedido.pk)
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
