const chaveContador = "vitrine:carrinho:quantidade";
const reais = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const notas = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const tonalidades = ["roxo", "laranja", "verde", "azul"];
const camposFiltro = ["categoria", "preco_min", "preco_max"];

export function configurarContador(elemento, navegador = window) {
    let revisao = 0;

    function atualizar(quantidade) {
        if (!Number.isSafeInteger(quantidade) || quantidade < 0) return;
        revisao += 1;
        elemento.textContent = String(quantidade);
        elemento.setAttribute("aria-label", `${quantidade} ${quantidade === 1 ? "item" : "itens"} no carrinho`);
        try {
            navegador.localStorage.setItem(chaveContador, String(quantidade));
        } catch {
            // O contador continua disponível quando o armazenamento está bloqueado.
        }
    }

    async function sincronizar() {
        const versao = ++revisao;
        try {
            const resposta = await navegador.fetch(elemento.dataset.contagemUrl, {
                headers: { Accept: "application/json" },
                credentials: "same-origin",
                cache: "no-store",
            });
            if (!resposta.ok) return;
            const { quantidade } = await resposta.json();
            if (versao === revisao) atualizar(quantidade);
        } catch {
            // O último valor recebido do servidor permanece visível.
        }
    }

    atualizar(Number(elemento.dataset.contador));
    navegador.addEventListener("pageshow", (evento) => {
        if (evento.persisted) sincronizar();
    });
    navegador.addEventListener("storage", (evento) => {
        if (evento.key === chaveContador || evento.key === null) sincronizar();
    });
    return { atualizar };
}

function normalizar(valor) {
    return valor.normalize("NFD").replace(/\p{Diacritic}/gu, "").trim().toLocaleLowerCase("pt-BR");
}

function iconeCategoria(categoria) {
    const nome = normalizar(categoria);
    if (/livr|papelaria/.test(nome)) return "livro";
    if (/eletr|tecnolog|audio|informat/.test(nome)) return "fone";
    if (/casa|decor|cozinha/.test(nome)) return "casa";
    return "caixa";
}

export function buscarProdutos(url, requisicao = fetch) {
    return requisicao(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then((resposta) => {
            if (!resposta.ok) throw new Error("Falha ao carregar o catálogo.");
            return resposta.json();
        })
        .then((produtos) => {
            if (!Array.isArray(produtos)) throw new Error("Formato inválido do catálogo.");
            return produtos;
        });
}

export function parametrosFiltros(dados) {
    const parametros = new URLSearchParams();
    for (const nome of camposFiltro) {
        const valor = dados.get(nome);
        if (valor) parametros.set(nome, valor);
    }
    return parametros;
}

export function formatarAvaliacao(media) {
    return media == null ? "Sem avaliações" : `Avaliação ${notas.format(media)} / 5`;
}

export async function adicionarProduto(url, dados, requisicao = fetch) {
    const erroGenerico = "Não foi possível adicionar o produto. Tente novamente.";
    const resposta = await requisicao(url, {
        method: "POST",
        headers: { Accept: "application/json", "X-CSRFToken": dados.get("csrfmiddlewaretoken") },
        credentials: "same-origin",
        body: dados,
    }).catch(() => {
        throw new Error(erroGenerico);
    });
    const resultado = await resposta.json().catch(() => null);
    if (!resposta.ok || !resultado) {
        throw new Error(resultado?.mensagem || erroGenerico);
    }
    return resultado;
}

function iniciarCatalogo(catalogo, contador) {
    const grade = catalogo.querySelector("[data-produtos]");
    const estado = catalogo.querySelector("[data-estado]");
    const tituloEstado = catalogo.querySelector("[data-estado-titulo]");
    const detalheEstado = catalogo.querySelector("[data-estado-detalhe]");
    const recarregar = catalogo.querySelector("[data-recarregar]");
    const resultado = catalogo.querySelector("[data-resultado]");
    const feedback = catalogo.querySelector("[data-feedback]");
    const busca = document.querySelector("[data-pesquisa]");
    const campo = busca.elements.q;
    const precoMin = busca.elements.preco_min;
    const precoMax = busca.elements.preco_max;
    const errosFiltros = catalogo.querySelector("[data-erros-filtros]");
    const categorias = JSON.parse(document.getElementById("categorias-catalogo").textContent);
    const modelo = document.getElementById("modelo-produto");
    let produtos = null;
    let adicionando = false;
    let filtrosAplicados = parametrosFiltros(new FormData(busca));
    let requisicaoAtual = 0;

    function categoriaDe(produto) {
        return categorias[produto.categoria] || "Sem categoria";
    }

    function apresentarEstado(titulo, detalhe, { carregando = false, repetir = false } = {}) {
        estado.hidden = false;
        estado.toggleAttribute("data-carregando", carregando);
        tituloEstado.textContent = titulo;
        detalheEstado.textContent = detalhe;
        recarregar.hidden = !repetir;
    }

    function criarCartao(produto) {
        const artigo = modelo.content.firstElementChild.cloneNode(true);
        const categoria = categoriaDe(produto);
        const disponivel = produto.estoque > 0;
        const arte = artigo.querySelector(".product-art");
        const indice = Math.abs(Number(produto.categoria) || 0) % tonalidades.length;
        arte.dataset.tonalidade = tonalidades[indice];
        arte.querySelector("use").setAttribute("href", `#icon-${iconeCategoria(categoria)}`);
        const imagemUrl = produto.imagem_exibicao ?? produto.imagem_url;
        if (imagemUrl) {
            const imagem = arte.querySelector("img");
            imagem.addEventListener("error", () => { imagem.remove(); }, { once: true });
            imagem.hidden = false;
            imagem.src = imagemUrl;
        }
        artigo.querySelector(".product-category").textContent = categoria;
        artigo.querySelector("h3").textContent = produto.nome;
        artigo.querySelector(".product-description").textContent = produto.descricao;
        artigo.querySelector(".product-rating").textContent = formatarAvaliacao(produto.media_avaliacoes);
        artigo.querySelector(".product-price").textContent = reais.format(produto.preco);
        const estoque = artigo.querySelector(".product-stock");
        estoque.textContent = disponivel ? "Em estoque" : "Esgotado";
        estoque.classList.toggle("is-unavailable", !disponivel);
        const formulario = artigo.querySelector("form");
        formulario.action = catalogo.dataset.adicionar.replace(/\/0\/$/, `/${produto.id}/`);
        const botao = formulario.querySelector("button");
        botao.disabled = !disponivel || adicionando;
        botao.toggleAttribute("data-esgotado", !disponivel);
        if (!disponivel) botao.querySelector("span").textContent = "Indisponível";
        return artigo;
    }

    function renderizar() {
        if (produtos === null) return;
        const termos = normalizar(campo.value).split(/\s+/).filter(Boolean);
        const encontrados = produtos.filter((produto) => {
            const texto = normalizar(`${produto.nome} ${produto.descricao} ${categoriaDe(produto)}`);
            return termos.every((termo) => texto.includes(termo));
        });
        grade.replaceChildren(...encontrados.map(criarCartao));
        const quantidade = encontrados.length;
        resultado.textContent = `${quantidade} ${quantidade === 1 ? "produto" : "produtos"}`;
        estado.hidden = quantidade > 0;
        if (quantidade === 0) {
            if (termos.length || filtrosAplicados.size) {
                apresentarEstado("Nenhum produto encontrado", "Tente ajustar a busca ou os filtros.");
            } else {
                apresentarEstado("Nenhum produto cadastrado", "Os produtos aparecerão aqui quando estiverem disponíveis.");
            }
        }
    }

    async function carregar() {
        const versao = ++requisicaoAtual;
        const url = new URL(catalogo.dataset.api, window.location.href);
        url.search = filtrosAplicados.toString();
        produtos = null;
        grade.replaceChildren();
        resultado.textContent = "";
        grade.setAttribute("aria-busy", "true");
        apresentarEstado("Carregando produtos", "Aguarde enquanto buscamos o catálogo.", { carregando: true });
        try {
            const recebidos = await buscarProdutos(url);
            if (versao !== requisicaoAtual) return;
            produtos = recebidos;
            produtos.sort((a, b) => a.nome.localeCompare(b.nome, "pt-BR"));
            renderizar();
        } catch {
            if (versao === requisicaoAtual) {
                apresentarEstado("Não foi possível carregar os produtos", "Verifique sua conexão e tente novamente.", { repetir: true });
            }
        } finally {
            if (versao === requisicaoAtual) grade.setAttribute("aria-busy", "false");
        }
    }

    function mostrarFeedback(mensagem, tipo) {
        feedback.dataset.tipo = tipo;
        feedback.hidden = false;
        feedback.textContent = mensagem;
        if (tipo !== "carregando") feedback.scrollIntoView({ block: "nearest" });
    }

    function bloquearAdicao(bloqueado) {
        adicionando = bloqueado;
        for (const botao of grade.querySelectorAll("button")) {
            botao.disabled = bloqueado || botao.hasAttribute("data-esgotado");
        }
    }

    grade.addEventListener("submit", async (evento) => {
        const formulario = evento.target;
        if (!formulario.matches("[data-adicionar-form]")) return;
        evento.preventDefault();
        if (adicionando) return;
        bloquearAdicao(true);
        mostrarFeedback("Adicionando ao carrinho…", "carregando");
        try {
            const { quantidade, mensagem } = await adicionarProduto(formulario.action, new FormData(formulario));
            contador.atualizar(quantidade);
            mostrarFeedback(mensagem, "sucesso");
        } catch (erro) {
            mostrarFeedback(erro.message, "erro");
        } finally {
            bloquearAdicao(false);
        }
    });

    campo.addEventListener("input", renderizar);
    for (const preco of [precoMin, precoMax]) {
        preco.addEventListener("input", () => precoMax.setCustomValidity(""));
    }
    busca.addEventListener("submit", (evento) => {
        evento.preventDefault();
        if (precoMin.valueAsNumber > precoMax.valueAsNumber) {
            precoMax.setCustomValidity("O preço máximo deve ser maior ou igual ao mínimo.");
            precoMax.reportValidity();
            return;
        }
        const url = new URL(window.location.href);
        if (campo.value.trim()) url.searchParams.set("q", campo.value.trim());
        else url.searchParams.delete("q");
        const filtros = parametrosFiltros(new FormData(busca));
        for (const nome of camposFiltro) {
            if (filtros.has(nome)) url.searchParams.set(nome, filtros.get(nome));
            else url.searchParams.delete(nome);
        }
        window.history.replaceState(null, "", url);
        if (errosFiltros) errosFiltros.hidden = true;
        if (filtros.toString() !== filtrosAplicados.toString() || produtos === null) {
            filtrosAplicados = filtros;
            carregar();
        } else {
            renderizar();
        }
        catalogo.scrollIntoView();
    });
    recarregar.addEventListener("click", carregar);
    if (errosFiltros) grade.setAttribute("aria-busy", "false");
    else carregar();
}

if (typeof document !== "undefined") {
    const contador = configurarContador(document.querySelector("[data-contador]"));
    const catalogo = document.querySelector("[data-catalogo]");
    if (catalogo) iniciarCatalogo(catalogo, contador);
}
