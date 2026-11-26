import assert from "node:assert/strict";
import { test } from "node:test";
import { adicionarProduto, buscarProdutos, configurarContador, formatarAvaliacao, parametrosFiltros } from "./static/loja/js/loja.mjs";

const chave = "vitrine:carrinho:quantidade";

function ambienteContador(quantidade) {
    const estoqueLocal = new Map([[chave, "99"]]);
    const eventos = {};
    const contador = {
        dataset: { contador: String(quantidade), contagemUrl: "/carrinho/contagem/" },
        setAttribute(nome, valor) { this[nome] = valor; },
    };
    const navegador = {
        addEventListener(nome, callback) { eventos[nome] = callback; },
        localStorage: { setItem: estoqueLocal.set.bind(estoqueLocal) },
    };
    return { estoqueLocal, eventos, contador, navegador };
}

test("carrega produtos via fetch da API em JSON", async () => {
    const produtos = [{ id: 1, nome: "Livro", preco: "12.50" }];
    const requisicao = async (url, opcoes) => {
        assert.equal(url, "/api/produtos/");
        assert.equal(opcoes.headers.Accept, "application/json");
        return Response.json(produtos);
    };
    assert.deepEqual(await buscarProdutos("/api/produtos/", requisicao), produtos);
    await assert.rejects(buscarProdutos("/api/produtos/", async () => new Response("", { status: 503 })));
});

test("envia categoria e preço zero à API e preserva a busca ampla do catálogo", async () => {
    const dados = new FormData();
    dados.set("q", "livros");
    dados.set("categoria", "3");
    dados.set("preco_min", "0");
    dados.set("preco_max", "");
    const url = new URL("https://example.com/api/produtos/");
    url.search = parametrosFiltros(dados).toString();
    await buscarProdutos(url, async (endereco) => {
        assert.equal(endereco.search, "?categoria=3&preco_min=0");
        return Response.json([]);
    });
});

test("formata a média em português e identifica produtos sem avaliações", () => {
    assert.equal(formatarAvaliacao(4.5), "Avaliação 4,5 / 5");
    assert.equal(formatarAvaliacao(5), "Avaliação 5,0 / 5");
    assert.equal(formatarAvaliacao(null), "Sem avaliações");
});

test("envia CSRF no POST e recebe a contagem oficial", async () => {
    const dados = new FormData();
    dados.append("csrfmiddlewaretoken", "token-de-teste");
    const requisicao = async (url, opcoes) => {
        assert.equal(url, "/carrinho/adicionar/1/");
        assert.equal(opcoes.method, "POST");
        assert.equal(opcoes.credentials, "same-origin");
        assert.equal(opcoes.headers["X-CSRFToken"], "token-de-teste");
        assert.equal(opcoes.body, dados);
        return Response.json({ quantidade: 3, mensagem: "Produto adicionado." });
    };
    assert.equal((await adicionarProduto("/carrinho/adicionar/1/", dados, requisicao)).quantidade, 3);
});

test("mostra a falta de estoque e trata falhas de rede", async () => {
    const url = "/carrinho/adicionar/1/";
    const dados = new FormData();
    const semEstoque = async () => Response.json({ mensagem: "Estoque insuficiente" }, { status: 409 });
    const semRede = async () => { throw new TypeError("Failed to fetch"); };
    await assert.rejects(adicionarProduto(url, dados, semEstoque), /Estoque insuficiente/);
    await assert.rejects(adicionarProduto(url, dados, semRede), /Tente novamente/);
});

test("grava no localStorage somente o valor recebido do servidor", () => {
    const { estoqueLocal, contador, navegador } = ambienteContador(0);
    const controle = configurarContador(contador, navegador);
    assert.equal(estoqueLocal.get(chave), "0");
    controle.atualizar(4);
    assert.equal(estoqueLocal.get(chave), "4");
    assert.equal(contador.textContent, "4");
    controle.atualizar(-1);
    assert.equal(estoqueLocal.get(chave), "4");
    configurarContador({ ...contador, dataset: { contador: "0" } }, navegador);
    assert.equal(estoqueLocal.get(chave), "0");
});

test("ao voltar de outra página ou receber alteração de outra aba, consulta a sessão", async () => {
    const { estoqueLocal, eventos, contador, navegador } = ambienteContador(2);
    const respostas = [1, 0];
    navegador.fetch = async (url) => {
        assert.equal(url, "/carrinho/contagem/");
        return Response.json({ quantidade: respostas.shift() });
    };
    configurarContador(contador, navegador);
    eventos.pageshow({ persisted: true });
    await new Promise(setImmediate);
    assert.equal(estoqueLocal.get(chave), "1");
    eventos.storage({ key: chave, newValue: "74" });
    await new Promise(setImmediate);
    assert.equal(estoqueLocal.get(chave), "0");
    assert.equal(contador.textContent, "0");
});
