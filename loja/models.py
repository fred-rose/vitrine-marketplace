from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models
from django.templatetags.static import static


class Categoria(models.Model):
    nome = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.nome


class Produto(models.Model):
    vendedor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="produtos",
    )
    categoria = models.ForeignKey(
        Categoria,
        on_delete=models.PROTECT,
        related_name="produtos",
    )
    nome = models.CharField(max_length=200)
    descricao = models.TextField()
    preco = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    estoque = models.PositiveIntegerField(default=0)
    imagem_url = models.URLField(
        max_length=500, blank=True, validators=[URLValidator(schemes=["http", "https"])]
    )
    imagem_local = models.CharField(max_length=200, blank=True, editable=False)

    @property
    def imagem_exibicao(self):
        return self.imagem_url or (static(self.imagem_local) if self.imagem_local else "")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(preco__gte=0), name="produto_preco_nao_negativo"
            ),
        ]

    def __str__(self):
        return self.nome


class Pedido(models.Model):
    class Status(models.TextChoices):
        ABERTO = "ABERTO", "Aberto"
        PAGO = "PAGO", "Pago"
        ENVIADO = "ENVIADO", "Enviado"
        CONCLUIDO = "CONCLUIDO", "Concluído"
        CANCELADO = "CANCELADO", "Cancelado"

    comprador = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="pedidos",
    )
    status = models.CharField(max_length=9, choices=Status, default=Status.ABERTO)
    criado_em = models.DateTimeField(auto_now_add=True)
    estoque_reservado = models.BooleanField(default=False, editable=False)
    referencia_demo = models.CharField(max_length=40, unique=True, null=True, blank=True, editable=False)

    @property
    def total(self):
        if self._state.adding:
            return Decimal("0.00")
        return sum((item.subtotal for item in self.itens.all()), Decimal("0.00"))

    def __str__(self):
        return f"Pedido {self.pk}"


class ItemPedido(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="itens")
    produto = models.ForeignKey(
        Produto,
        on_delete=models.PROTECT,
        related_name="itens_pedido",
    )
    quantidade = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    preco_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantidade__gte=1), name="item_quantidade_positiva"
            ),
            models.CheckConstraint(
                condition=models.Q(preco_unitario__gte=0),
                name="item_preco_unitario_nao_negativo",
            ),
        ]

    @property
    def subtotal(self):
        return self.quantidade * self.preco_unitario

    def __str__(self):
        return f"Item {self.pk} do pedido {self.pedido_id}"


class Avaliacao(models.Model):
    comprador = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="avaliacoes"
    )
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE, related_name="avaliacoes")
    nota = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comentario = models.TextField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(nota__range=(1, 5)), name="avaliacao_nota_valida"),
            models.UniqueConstraint(fields=("comprador", "produto"), name="avaliacao_comprador_produto_unico"),
        ]

    def clean(self):
        super().clean()
        if self.comprador_id and self.produto_id and not ItemPedido.objects.filter(
            produto_id=self.produto_id,
            pedido__comprador_id=self.comprador_id,
            pedido__status=Pedido.Status.CONCLUIDO,
        ).exists():
            raise ValidationError({"produto": "A avaliação exige uma compra deste produto em um pedido concluído."})

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class Pagamento(models.Model):
    class Status(models.TextChoices):
        PENDENTE = "PENDENTE", "Aguardando pagamento"
        PAGO = "PAGO", "Confirmado"
        EXPIRADO = "EXPIRADO", "Sessão expirada"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    pedido = models.ForeignKey(Pedido, on_delete=models.PROTECT, related_name="pagamentos")
    status = models.CharField(max_length=8, choices=Status, default=Status.PENDENTE)
    valor_centavos = models.PositiveIntegerField()
    moeda = models.CharField(max_length=3, default="brl")
    sessao_stripe = models.CharField(max_length=255, unique=True, null=True, editable=False)
    evento_stripe = models.CharField(max_length=255, unique=True, null=True, editable=False)
    intencao_stripe = models.CharField(max_length=255, unique=True, null=True, editable=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    confirmado_em = models.DateTimeField(null=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("pedido",), condition=models.Q(status__in=("PENDENTE", "PAGO")),
                name="pedido_um_pagamento_ativo",
            ),
        ]

    @property
    def valor(self):
        return Decimal(self.valor_centavos).scaleb(-2)
