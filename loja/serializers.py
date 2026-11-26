from collections import Counter

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .avaliacoes import registrar_avaliacao
from .checkout import CheckoutInvalido, realizar_checkout
from .models import Avaliacao, ItemPedido, Pedido, Produto


class ProdutoSerializer(serializers.ModelSerializer):
    media_avaliacoes = serializers.FloatField(read_only=True, default=None)

    class Meta:
        model = Produto
        fields = (
            "id", "vendedor", "categoria", "nome", "descricao", "preco", "estoque",
            "imagem_url", "imagem_exibicao", "media_avaliacoes",
        )
        read_only_fields = ("vendedor",)
        extra_kwargs = {"imagem_url": {"validators": Produto._meta.get_field("imagem_url").validators}}


class AvaliacaoSerializer(serializers.ModelSerializer):
    comprador = serializers.HiddenField(default=serializers.CurrentUserDefault())

    class Meta:
        model = Avaliacao
        fields = ("id", "comprador", "produto", "nota", "comentario")

    def create(self, validated_data):
        try:
            return registrar_avaliacao(**validated_data)
        except DjangoValidationError as erro:
            raise serializers.ValidationError(erro.message_dict) from erro


class ItemPedidoSerializer(serializers.ModelSerializer):
    subtotal = serializers.DecimalField(max_digits=None, decimal_places=2, read_only=True)

    class Meta:
        model = ItemPedido
        fields = ("id", "produto", "quantidade", "preco_unitario", "subtotal")
        read_only_fields = ("preco_unitario",)


class PedidoSerializer(serializers.ModelSerializer):
    itens = ItemPedidoSerializer(many=True, allow_empty=False)
    total = serializers.DecimalField(max_digits=None, decimal_places=2, read_only=True)

    class Meta:
        model = Pedido
        fields = ("id", "comprador", "status", "criado_em", "itens", "total")
        read_only_fields = ("comprador", "status", "criado_em")

    def create(self, validated_data):
        quantidades = Counter()
        for item in validated_data["itens"]:
            quantidades[str(item["produto"].pk)] += item["quantidade"]

        try:
            return realizar_checkout(validated_data["comprador"], quantidades)
        except CheckoutInvalido as erro:
            raise serializers.ValidationError({"itens": [str(erro)]}) from erro
