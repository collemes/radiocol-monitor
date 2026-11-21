"""Índice do tudoradio.com: rádios por cidade, UF e frequência, com o endereço de áudio do player deles.

O tudoradio tem um mapa com todas as páginas de rádio (/player/sitemap) e cada página traz no título
"Nome - Cidade / UF - 99.9 FM". O robô monta um índice local (data/tudoradio.json), aos poucos e com
educação (poucas páginas por vez, mapa 1x por semana, cada página relida a cada 30 dias), e procura nele
a rádio do post pela MESMA cidade, UF e frequência (e faixa AM/FM).
"""
import asyncio
import json
import os
import re
from datetime import date

from . import config
from .extract import desescapa, extract_streams, norm

BASE = "https://tudoradio.com"
SITEMAP = BASE + "/player/sitemap"
PAGINA_RE = re.compile(r"https?://(?:www\.)?tudoradio\.com/player/radio/\d+-[a-z0-9-]+", re.I)
TITULO_RE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.I | re.S)
# "Rádio Mix FM - Uberlândia / MG - 106.5 FM"   |   "Rádio Planalto - São Benedito / CE - 1510 AM"
CAMPOS_RE = re.compile(r"^(?P<nome>.+?)\s+-\s+(?P<cidade>[^/]+?)\s*/\s*(?P<uf>[A-Z]{2})\s+-\s+"
                       r"(?P<freq>\d{2,4}(?:[.,]\d)?)\s*(?P<banda>FM|AM|OM|OC)\b", re.I)
ARQUIVO = "tudoradio.json"


def _compacto(t):
    return re.sub(r"[^a-z0-9]", "", norm(t or ""))


def freq_norm(f):
    """'106,5' / '106.5' -> '106.5'; '1270' -> '1270'."""
    return (f or "").replace(",", ".").strip()


def ler_titulo(html):
    m = TITULO_RE.search(html or "")
    if not m:
        return None
    titulo = re.sub(r"\s+", " ", desescapa(m.group(1))).strip()
    c = CAMPOS_RE.match(titulo)
    if not c:
        return None
    return {"nome": c.group("nome").strip(), "cidade": c.group("cidade").strip(), "uf": c.group("uf").lower(),
            "freq": freq_norm(c.group("freq")), "banda": c.group("banda").lower()}


def _dias(d, hoje):
    try:
        return (date.fromisoformat(hoje) - date.fromisoformat(d)).days
    except Exception:
        return 999


def carregar():
    caminho = os.path.join(config.DATA_DIR, ARQUIVO)
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sitemap": None, "radios": {}}


def salvar(indice):
    caminho = os.path.join(config.DATA_DIR, ARQUIVO)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


async def atualizar(fetch_text, hoje, log=print):
    """Atualiza o índice: mapa de páginas 1x/semana e até TUDORADIO_PAGINAS páginas por execução."""
    indice = carregar()
    radios = indice.setdefault("radios", {})
    if _dias(indice.get("sitemap") or "", hoje) >= 7:
        txt = await fetch_text(SITEMAP, limit=8_000_000)
        achadas = set(PAGINA_RE.findall(txt or ""))
        if achadas:
            for u in achadas:
                radios.setdefault(u.replace("://www.", "://"), {})
            indice["sitemap"] = hoje
            log(f"tudoradio: mapa com {len(achadas)} páginas de rádio")
        else:
            log("tudoradio: mapa indisponível hoje")
    fila = [u for u, r in radios.items() if not r.get("lido") or _dias(r["lido"], hoje) >= 30]
    fila.sort(key=lambda u: radios[u].get("lido") or "")
    fila = fila[: config.TUDORADIO_PAGINAS]
    sem = asyncio.Semaphore(4)

    async def ler(u):
        async with sem:
            html = await fetch_text(u, limit=600_000)
            await asyncio.sleep(0.3)                       # educação com o servidor deles
        r = radios[u]
        r["lido"] = hoje
        dados = ler_titulo(html)
        if dados:
            r.update(dados)
            r["streams"] = [s for s in extract_streams(html) if "tudoradio.com" not in s][:4]
        elif html:
            r["sem_dados"] = True

    if fila:
        await asyncio.gather(*(ler(u) for u in fila))
        log(f"tudoradio: {len(fila)} páginas lidas; índice com "
            f"{sum(1 for r in radios.values() if r.get('cidade'))} rádios identificadas")
    salvar(indice)
    return indice


_GENERICAS = set("ouvir a o radio radios web webradio fm am ao vivo e online de do da dos das em the mhz khz tv "
                 "net digital".split())


def palavras_nome(texto, cidade=""):
    """Palavras que identificam a rádio (sem 'Rádio', 'FM', 'Web', cidade, UF…)."""
    from .extract import norm
    fora = set(norm(cidade or "").split())
    return {w for w in re.findall(r"[a-z0-9]+", norm(texto or "")) if len(w) >= 3 and not w.isdigit()
            and w not in _GENERICAS and w not in fora}


def mesmo_nome(titulo_post, nome_dir, cidade):
    """Webradio (sem frequência): o nome do diretório tem as mesmas palavras próprias do post."""
    a = palavras_nome(re.split(r"\b(?:de|do|da)\s+" + re.escape(cidade or "@@"), titulo_post, flags=re.I)[0], cidade)
    b = palavras_nome(nome_dir, cidade)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.6


def candidatos(indice, rec, frequency, band):
    """Páginas do diretório da MESMA cidade e UF do post e: mesma frequência e faixa (AM/FM) — ou, se o post
    for de webradio (sem frequência), o mesmo nome. Páginas 'Arquivada' (rádio encerrada) ficam de fora."""
    loc = rec.get("loc") or [None, None]
    cidade, uf = loc[0] or "", (loc[1] if len(loc) > 1 else None) or ""
    titulo = rec.get("title", "")
    f, b = freq_norm(frequency(titulo) or ""), band(titulo)
    if not (cidade and uf):
        return []
    cid = _compacto(cidade)
    out = []
    for u, r in (indice or {}).get("radios", {}).items():
        if r.get("arquivada") or r.get("uf") != uf or _compacto(r.get("cidade")) != cid:
            continue
        if f:
            if r.get("freq") != f or (b and r.get("banda") and r["banda"] != b):
                continue
        else:
            # sem frequência: só webradio (o post não fala em AM/FM e o diretório também não tem frequência)
            if b or r.get("freq") or not mesmo_nome(titulo, r.get("nome"), cidade):
                continue
        out.append((u, r))
    return out
