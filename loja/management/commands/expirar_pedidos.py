import stripe
from django.core.management.base import BaseCommand
from django.db import DatabaseError

from loja.checkout import CheckoutInvalido, cancelar_pedido, limite_reserva
from loja.models import Pagamento, Pedido
from loja.pagamentos import PagamentoIndisponivel, reconciliar_sessao_expirada


class Command(BaseCommand):
    help = "Cancela pedidos abertos há pelo menos 24 horas e devolve o estoque de forma segura."

    def handle(self, *args, **options):
        limite = limite_reserva()
        pedidos = Pedido.objects.filter(status=Pedido.Status.ABERTO, criado_em__lte=limite).order_by("pk")
        cancelados = pendentes = 0
        for pedido_id in list(pedidos.values_list("pk", flat=True)):
            pagamento = Pagamento.objects.filter(pedido_id=pedido_id, status=Pagamento.Status.PENDENTE).first()
            try:
                if pagamento:
                    reconciliar_sessao_expirada(pagamento)
                cancelados += cancelar_pedido(pedido_id, anterior_a=limite)
            except (CheckoutInvalido, PagamentoIndisponivel, stripe.StripeError, DatabaseError):
                pendentes += 1
                self.stderr.write(self.style.WARNING(f"Pedido #{pedido_id} requer nova verificação."))
            except Pedido.DoesNotExist:
                continue
        self.stdout.write(f"Pedidos cancelados: {cancelados}. Pendentes de verificação: {pendentes}.")
