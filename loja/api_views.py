from django.db.models import Avg
from rest_framework.exceptions import ValidationError
from rest_framework.generics import CreateAPIView, ListCreateAPIView
from rest_framework.permissions import IsAuthenticated, IsAuthenticatedOrReadOnly

from .filtros import FiltrosProdutoForm
from .models import Produto
from .serializers import AvaliacaoSerializer, PedidoSerializer, ProdutoSerializer


class ProdutoListCreateView(ListCreateAPIView):
    queryset = Produto.objects.annotate(media_avaliacoes=Avg("avaliacoes__nota")).order_by("pk")
    serializer_class = ProdutoSerializer
    permission_classes = (IsAuthenticatedOrReadOnly,)

    def get_queryset(self):
        filtros = FiltrosProdutoForm(self.request.query_params)
        if not filtros.is_valid():
            raise ValidationError(filtros.errors)
        produtos = filtros.filtrar(super().get_queryset())
        if filtros.cleaned_data["q"]:
            produtos = produtos.filter(nome__icontains=filtros.cleaned_data["q"])
        return produtos

    def perform_create(self, serializer):
        serializer.save(vendedor=self.request.user)


class PedidoCreateView(CreateAPIView):
    serializer_class = PedidoSerializer
    permission_classes = (IsAuthenticated,)

    def perform_create(self, serializer):
        serializer.save(comprador=self.request.user)


class AvaliacaoCreateView(CreateAPIView):
    serializer_class = AvaliacaoSerializer
    permission_classes = (IsAuthenticated,)
