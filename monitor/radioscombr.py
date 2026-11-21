"""Índice do radios.com.br: rádios AM/FM e webradios por cidade, UF e frequência (ou nome, nas webradios).

Não há mapa público das páginas de rádio: o robô parte da lista do Brasil (/lista/pais/brasil/33), segue as
listas por estado, região e cidade e anota as páginas /aovivo/<nome>/<id>. Cada página traz no título
"Rádio Cidade FM 99.1 - Fortaleza / CE - Brasil" e, nas webradios, a marca "(Webradio)". Páginas "Arquivada"
(rádio encerrada ou duplicada) ficam de fora. Tudo aos poucos e com educação: listas 1x por semana,
até RADIOSCOMBR_PAGINAS páginas de rádio por execução, cada uma relida a cada 30 dias.
"""
import asyncio
import json
import os
import re
from urllib.parse import urljoin, urlsplit

from . import config
from .extract import band, desescapa, extract_streams, frequency
from .tudoradio import _dias, extrai_site, freq_norm

BASE = "https://www.radios.com.br"
INICIO = BASE + "/lista/pais/brasil/33"
ARQUIVO = "radioscombr.json"
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
LISTA_RE = re.compile(r"^/(?:lista|radio)/(?:pais|estado|regiao|cidade|segmento)?/?[^?]*", re.I)
RADIO_RE = re.compile(r"^/aovivo/[^/?&]+/\d+")
TITULO_RE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.I | re.S)
# "Rádio Cidade FM 99.1 - Fortaleza / CE - Brasil"  |  "Web Rádio MAP - Rio de Janeiro / RJ - Brasil"
CAMPOS_RE = re.compile(r"^(?P<nome>.+?)\s+-\s+(?P<cidade>[^/]+?)\s*/\s*(?P<uf>[A-Z]{2})\s+-\s+Brasil\b", re.I)


def ler_pagina(html):
    m = TITULO_RE.search(html or "")
    if not m:
        return None
    titulo = re.sub(r"\s+", " ", desescapa(m.group(1))).strip()
    c = CAMPOS_RE.match(titulo)
    if not c:
        return None
    nome = c.group("nome").strip()
    web = "(webradio)" in (html or "").lower()
    return {"nome": nome, "cidade": c.group("cidade").strip(), "uf": c.group("uf").lower(),
            "freq": None if web else freq_norm(frequency(nome) or ""), "banda": None if web else band(nome),
            "web": web, "arquivada": bool(re.search(r"\bArquivada\b", html or ""))}


def carregar():
    try:
        with open(os.path.join(config.DATA_DIR, ARQUIVO), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"listas": {}, "radios": {}}


def salvar(indice):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(os.path.join(config.DATA_DIR, ARQUIVO), "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normaliza(base, href):
    u = urljoin(base, href.strip())
    p = urlsplit(u)
    if p.netloc.replace("www.", "") != urlsplit(BASE).netloc.replace("www.", ""):
        return None, None
    caminho = p.path.split("&")[0]                        # tira "&gsessionid=…" colado no caminho
    if RADIO_RE.match(caminho):
        return "radio", BASE + RADIO_RE.match(caminho).group(0)
    if LISTA_RE.match(caminho) and caminho.startswith(("/lista/", "/radio/")):
        return "lista", BASE + caminho + (("?" + p.query) if p.query and "pag" in p.query.lower() else "")
    return None, None


async def atualizar(fetch_text, hoje, log=print):
    indice = carregar()
    listas, radios = indice.setdefault("listas", {}), indice.setdefault("radios", {})
    listas.setdefault(INICIO, {})
    sem = asyncio.Semaphore(3)

    # 1) listas (estado, região, cidade, páginas seguintes): cada uma 1x por semana
    fila = [u for u, r in listas.items() if _dias(r.get("lido") or "", hoje) >= 7][: config.RADIOSCOMBR_LISTAS]

    async def ler_lista(u):
        async with sem:
            html = await fetch_text(u, limit=1_500_000)
            await asyncio.sleep(0.4)
        listas[u]["lido"] = hoje
        for href in HREF_RE.findall(html or ""):
            tipo, alvo = _normaliza(u, href)
            if tipo == "radio":
                radios.setdefault(alvo, {})
            elif tipo == "lista":
                listas.setdefault(alvo, {})

    rodada = 0
    while fila and rodada < 4:                           # segue as listas novas que aparecem na mesma execução
        await asyncio.gather(*(ler_lista(u) for u in fila))
        rodada += 1
        restante = config.RADIOSCOMBR_LISTAS - sum(1 for r in listas.values() if r.get("lido") == hoje)
        fila = [u for u, r in listas.items() if not r.get("lido")][: max(0, restante)]

    # 2) páginas de rádio: até RADIOSCOMBR_PAGINAS por execução, cada uma relida a cada 30 dias
    fila = [u for u, r in radios.items() if not r.get("lido") or _dias(r["lido"], hoje) >= 30]
    fila.sort(key=lambda u: radios[u].get("lido") or "")
    fila = fila[: config.RADIOSCOMBR_PAGINAS]

    async def ler_radio(u):
        async with sem:
            html = await fetch_text(u, limit=800_000)
            await asyncio.sleep(0.3)
        r = radios[u]
        r["lido"] = hoje
        dados = ler_pagina(html)
        if dados:
            r.update(dados)
            r["streams"] = [s for s in extract_streams(html) if "radios.com.br" not in s][:4]
            r["site"] = extrai_site(html, "radios.com.br")

    if fila:
        await asyncio.gather(*(ler_radio(u) for u in fila))
    log(f"radios.com.br: {len(listas)} listas, {len(radios)} páginas de rádio conhecidas, "
        f"{sum(1 for r in radios.values() if r.get('cidade'))} identificadas "
        f"({sum(1 for r in radios.values() if r.get('web'))} webradios)")
    salvar(indice)
    return indice
