from django import forms
from django.contrib import admin

from .checkout import cancelar_pedido
from .models import Categoria, ItemPedido, Pagamento, Pedido, Produto


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    search_fields = ("nome",)


@admin.register(Produto)
class ProdutoAdmin(admin.ModelAdmin):
    list_display = ("nome", "categoria", "vendedor", "preco", "estoque")
    list_filter = ("categoria",)
    search_fields = ("nome", "descricao")

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset
        return queryset.filter(vendedor=request.user)

    def get_exclude(self, request, obj=None):
        if obj is None or not request.user.is_superuser:
            return ("vendedor",)
        return super().get_exclude(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.vendedor = request.user
        super().save_model(request, obj, form, change)


class ItemPedidoInline(admin.TabularInline):
    model = ItemPedido
    readonly_fields = ("preco_unitario",)
    extra = 0

    def has_add_permission(self, request, obj):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.itens.exists():
            return ("produto", "quantidade", "preco_unitario")
        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.itens.exists())


class PedidoAdminForm(forms.ModelForm):
    def clean_status(self):
        status = self.cleaned_data["status"]
        pedido = self.instance
        if status == Pedido.Status.PAGO and (not pedido.pk or pedido.status != status):
            raise forms.ValidationError("O pagamento é confirmado pelo webhook do Stripe.")
        if status in (Pedido.Status.ABERTO, Pedido.Status.CANCELADO) and pedido.pk and pedido.pagamentos.filter(
            status=Pagamento.Status.PAGO,
        ).exists():
            raise forms.ValidationError("Este pedido tem um pagamento confirmado.")
        if status != pedido.status:
            transicoes = {
                Pedido.Status.ABERTO: (Pedido.Status.CANCELADO,),
                Pedido.Status.PAGO: (Pedido.Status.ENVIADO, Pedido.Status.CONCLUIDO),
                Pedido.Status.ENVIADO: (Pedido.Status.CONCLUIDO,),
            }
            if status not in transicoes.get(pedido.status, ()):
                raise forms.ValidationError("Esta transição de status não está disponível.")
        return status


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    form = PedidoAdminForm
    list_display = ("id", "comprador", "status", "criado_em", "total")
    inlines = (ItemPedidoInline,)
    actions = None

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.method == "POST" and getattr(request.resolver_match, "url_name", None) == "loja_pedido_change":
            # O formulário de alteração do Admin roda em uma transação.
            return queryset.select_for_update()
        return queryset

    def get_readonly_fields(self, request, obj=None):
        if obj and (obj.itens.exists() or obj.pagamentos.exists()):
            if obj.pagamentos.filter(status=Pagamento.Status.PENDENTE).exists():
                return ("comprador", "status")
            return ("comprador",)
        return super().get_readonly_fields(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and (obj.itens.exists() or obj.pagamentos.exists()):
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if change and "status" in form.changed_data and obj.status == Pedido.Status.CANCELADO:
            cancelar_pedido(obj.pk)
            obj.estoque_reservado = False
        super().save_model(request, obj, form, change)
