from django import forms


class FiltrosProdutoForm(forms.Form):
    q = forms.CharField(required=False)
    categoria = forms.IntegerField(required=False, min_value=1, max_value=2**63 - 1)
    preco_min = forms.DecimalField(required=False, min_value=0, max_digits=12, decimal_places=2)
    preco_max = forms.DecimalField(required=False, min_value=0, max_digits=12, decimal_places=2)

    def clean(self):
        dados = super().clean()
        minimo, maximo = dados.get("preco_min"), dados.get("preco_max")
        if minimo is not None and maximo is not None and minimo > maximo:
            self.add_error("preco_max", "O preço máximo deve ser maior ou igual ao mínimo.")
        return dados

    def filtrar(self, produtos):
        campos = {"categoria": "categoria_id", "preco_min": "preco__gte", "preco_max": "preco__lte"}
        return produtos.filter(**{
            lookup: self.cleaned_data[campo]
            for campo, lookup in campos.items()
            if self.cleaned_data.get(campo) is not None
        })
