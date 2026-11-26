def contar_itens(sessao):
    return sum(sessao.get("carrinho", {}).values())


def contexto_carrinho(request):
    return {"quantidade_carrinho": contar_itens(request.session)}
