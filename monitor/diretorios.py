"""Diretórios brasileiros consultados SOB DEMANDA, pela cidade do post (tudoradio.com e radios.com.br).

Em vez de varrer o diretório inteiro, o robô lê só a cidade do post:

tudoradio.com
  sitemaps/dials-cidades.xml  -> endereço da página "Dials" de cada cidade (/dials/cidade/102-rio-de-janeiro)
  página Dials da cidade      -> dados de cada emissora no próprio HTML (Next.js): nome, estação (104.5),
                                 faixa (fm/am), cidade-sede, endereço de áudio ("url") e site.
                                 Só entram as emissoras com sede NA cidade da página.

radios.com.br
  sitemap_cidades1..3.xml     -> endereço da lista de cada cidade (/radio/cidade/rio-de-janeiro/12339)
  lista da cidade (?pg=N)     -> nome de cada rádio ("Rádio Positividade 104.5 FM") e link /aovivo/<nome>/<id>
  página da rádio             -> título "Nome - Cidade / UF - Brasil", áudio (MRP.insert({'url': ...})), site,
                                 WhatsApp e a marca "Arquivada" (rádio encerrada ou duplicada).
                                 Só abre as páginas das rádios com a MESMA frequência do post (webradio: nome).

Tudo fica em data/diretorios.json e cada cidade é relida a cada DIRETORIO_DIAS dias.
"""
import asyncio
import html as htmlmod
import json
import os
import re
from datetime import date

from . import config
from .extract import band, frequency, norm
from .tudoradio import CAMPOS_RE as TR_CAMPOS, freq_norm, mesmo_nome

ARQUIVO = "diretorios.json"
TR = "https://tudoradio.com"
RCB = "https://www.radios.com.br"
LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
TITULO_RE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.I | re.S)
RCB_CAMPOS = re.compile(r"^(?P<nome>.+?)\s+-\s+(?P<cidade>[^/]+?)\s*/\s*(?P<uf>[A-Z]{2})\s+-\s+Brasil\b", re.I)
RCB_ITEM_RE = re.compile(r"<h3>\s*<a\s+href=\"(https?://www\.radios\.com\.br/aovivo/[^\"]+/\d+)\"[^>]*>(.*?)</a>\s*</h3>"
                         r"\s*<p class=\"localizacao\">(.*?)</p>", re.I | re.S)
RCB_URL_RE = re.compile(r"""MRP\.insert\(\s*\{[^}]*?['"]url['"]\s*:\s*['"]([^'"]+)['"]""", re.I | re.S)
RCB_SITE_RE = re.compile(r"<b>\s*Site:\s*</b>\s*<a[^>]+href=\"([^\"]+)\"", re.I)
TR_ITEM_RE = re.compile(r'\{"id":"?(\d+)"?,"nome":"((?:[^"\\]|\\.)*)","estacao":"?([\d.]+)"?,"frequencia":"(fm|am)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _hoje():
    return date.today().isoformat()


def _dias(d):
    try:
        return (date.today() - date.fromisoformat(d)).days
    except Exception:
        return 999


def chave(cidade):
    """'São José dos Campos' -> 'sao-jose-dos-campos' (como nos endereços dos diretórios)."""
    return re.sub(r"[^a-z0-9]+", "-", norm(cidade or "").replace("'", "")).strip("-")


def chaves(cidade):
    """Formas do nome da cidade nos endereços: 'Santa Bárbara d'Oeste' -> santa-barbara-doeste / -d-oeste."""
    c = htmlmod.unescape(cidade or "")
    return list(dict.fromkeys([chave(c), re.sub(r"[^a-z0-9]+", "-", norm(c)).strip("-")]))


def _urls(mapa, cidade):
    out = []
    for k in chaves(cidade):
        out += mapa.get(k, [])
    return list(dict.fromkeys(out))


def _txt(t):
    return re.sub(r"\s+", " ", htmlmod.unescape(TAG_RE.sub(" ", t or ""))).strip()


def ler_dials(pagina_html, url):
    """Emissoras com sede na cidade da página Dials do tudoradio."""
    m = re.search(r"/dials/cidade/(\d+)-", url)
    id_cidade = m.group(1) if m else None
    mt = TITULO_RE.search(pagina_html or "")
    tit = _txt(mt.group(1)) if mt else ""
    mc = re.search(r"\bem\s+(.+?),\s*([A-Z]{2})\b", tit)
    cidade, uf = (mc.group(1).strip(), mc.group(2).lower()) if mc else (None, None)
    texto = (pagina_html or "").replace('\\"', '"').replace("\\\\/", "/").replace("\\/", "/")
    radios = {}
    for m in TR_ITEM_RE.finditer(texto):
        rid, nome, estacao, faixa = m.group(1), m.group(2), m.group(3), m.group(4).lower()
        bloco = texto[m.end(): m.end() + 1500]
        fim = bloco.find('{"id":')
        if fim > 0:
            bloco = bloco[:fim]
        campo = lambda k: (re.search(r'"%s":"((?:[^"\\]|\\.)*)"' % k, bloco) or [None, None])[1]
        sede = campo("cidadeSedeId")
        if id_cidade and sede and sede != id_cidade:
            continue                       # emissora de outra cidade que pega aqui
        url_audio = campo("url")
        pagina = f"{TR}/player/radio/{rid}"
        radios[pagina] = {"nome": htmlmod.unescape(nome), "cidade": cidade, "uf": uf,
                          "freq": freq_norm(estacao), "banda": faixa, "web": False,
                          "streams": [url_audio] if url_audio and url_audio.startswith("http") else [],
                          "site": campo("site") if (campo("site") or "").startswith("http") else None}
    return {"cidade": cidade, "uf": uf, "radios": radios}


def ler_lista_rcb(pagina_html, url_cidade):
    """[(link da rádio, nome)] da lista de uma cidade do radios.com.br (só as rádios DA cidade) e nº de páginas."""
    itens = []
    base_cidade = url_cidade.split("?")[0]
    for m in RCB_ITEM_RE.finditer(pagina_html or ""):
        if base_cidade.split("/radio/cidade/")[-1] not in m.group(3):
            continue                       # "rádios sugeridas" de outras cidades
        itens.append((m.group(1), _txt(m.group(2))))
    pags = [int(x) for x in re.findall(r"[?&]pg=(\d+)", pagina_html or "")]
    return itens, max(pags) if pags else 1


def ler_radio_rcb(pagina_html):
    tit = TITULO_RE.search(pagina_html or "")
    if not tit:
        return None
    c = RCB_CAMPOS.match(_txt(tit.group(1)))
    if not c:
        return None
    nome = c.group("nome").strip()
    web = "(webradio)" in (pagina_html or "").lower() or not frequency(nome)
    url = RCB_URL_RE.search(pagina_html or "")
    site = RCB_SITE_RE.search(pagina_html or "")
    return {"nome": nome, "cidade": c.group("cidade").strip(), "uf": c.group("uf").lower(),
            "freq": None if web else freq_norm(frequency(nome) or ""), "banda": None if web else band(nome),
            "web": web, "arquivada": bool(re.search(r"\bArquivada\b", pagina_html or "")),
            "streams": [htmlmod.unescape(url.group(1))] if url else [],
            "site": htmlmod.unescape(site.group(1)) if site else None}


class Diretorios:
    def __init__(self, fetch_text, log=print):
        self.fetch = fetch_text
        self.log = log
        self.caminho = os.path.join(config.DATA_DIR, ARQUIVO)
        try:
            with open(self.caminho, encoding="utf-8") as f:
                self.d = json.load(f)
        except Exception:
            self.d = {}
        for k in ("tr_mapa", "rcb_mapa", "tr", "rcb", "rcb_radio"):
            self.d.setdefault(k, {})
        self.sem = asyncio.Semaphore(4)
        self.locks = {}
        self.lidas = {"tudoradio": 0, "radios.com.br": 0, "paginas": 0}
        self.erros = {}

    def salvar(self):
        os.makedirs(os.path.dirname(self.caminho), exist_ok=True)
        with open(self.caminho, "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    async def _get(self, url, limit=2_500_000):
        async with self.sem:
            t = await self.fetch(url, limit=limit)
            await asyncio.sleep(0.3)                       # educação com o servidor deles
            self.lidas["paginas"] += 1
            return t or ""

    # ------------------------------------------------------------------ mapas (1x por mês)
    async def preparar(self):
        tr = self.d["tr_mapa"]
        if _dias(tr.get("_lido")) >= 30 or len(tr) < 50:
            xml = await self._get(f"{TR}/sitemaps/dials-cidades.xml")
            novo = {}
            for u in LOC_RE.findall(xml):
                m = re.search(r"/dials/cidade/\d+-([a-z0-9-]+)", u)
                if m:
                    novo.setdefault(m.group(1), []).append(u.split("?")[0])
            if novo:
                novo["_lido"] = _hoje()
                self.d["tr_mapa"] = tr = novo
            self.log(f"tudoradio: {len(tr) - 1 if '_lido' in tr else 0} cidades no mapa de dials"
                     + ("" if novo else " (mapa não lido hoje)"))
        rcb = self.d["rcb_mapa"]
        if _dias(rcb.get("_lido")) >= 30 or len(rcb) < 50:
            novo = {}
            for i in (1, 2, 3, 4, 5):
                xml = await self._get(f"{RCB}/sitemap_cidades{i}.xml")
                achou = False
                for u in LOC_RE.findall(xml):
                    m = re.search(r"/radio/cidade/([a-z0-9-]+)/\d+", u)
                    if m:
                        achou = True
                        novo.setdefault(m.group(1), []).append(u.split("?")[0])
                if not achou and i > 3:
                    break
            if novo:
                novo["_lido"] = _hoje()
                self.d["rcb_mapa"] = rcb = novo
            self.log(f"radios.com.br: {len(rcb) - 1 if '_lido' in rcb else 0} cidades no mapa"
                     + ("" if novo else " (mapa não lido hoje)"))

    # ------------------------------------------------------------------ por cidade
    async def _tudoradio(self, cidade, uf):
        out = {}
        for url in _urls(self.d["tr_mapa"], cidade)[:4]:
            c = self.d["tr"].get(url)
            if not c or _dias(c.get("lido")) >= config.DIRETORIO_DIAS:
                h = await self._get(url)
                c = ler_dials(h, url) if h else {"radios": {}}
                c["lido"] = _hoje()
                if h:
                    self.d["tr"][url] = c
                    self.lidas["tudoradio"] += 1
            if c.get("uf") and uf and c["uf"] != uf:
                continue                                   # cidade de mesmo nome em outro estado
            out.update(c.get("radios") or {})
        return out

    async def _rcb(self, cidade, uf, rec):
        f = freq_norm(frequency(rec.get("title", "")) or "")
        b = band(rec.get("title", ""))
        out = {}
        for url in _urls(self.d["rcb_mapa"], cidade)[:4]:
            c = self.d["rcb"].get(url)
            if not c or _dias(c.get("lido")) >= config.DIRETORIO_DIAS:
                itens, paginas = [], 1
                h = await self._get(url, limit=1_500_000)
                if h:
                    itens, paginas = ler_lista_rcb(h, url)
                    for pg in range(2, min(paginas, config.DIRETORIO_RCB_PAGINAS) + 1):
                        mais, _ = ler_lista_rcb(await self._get(f"{url}?pg={pg}", limit=1_500_000), url)
                        itens += mais
                    self.d["rcb"][url] = c = {"lido": _hoje(), "itens": itens}
                    self.lidas["radios.com.br"] += 1
                else:
                    c = {"itens": []}
            for link, nome in c.get("itens") or []:
                fn = freq_norm(frequency(nome) or "")
                if f:
                    if fn != f:
                        continue
                    bn = band(nome)
                    if b and bn and b != bn:
                        continue
                elif fn or not mesmo_nome(rec.get("title", ""), nome, cidade):
                    continue
                r = self.d["rcb_radio"].get(link)
                if not r or _dias(r.get("lido")) >= config.DIRETORIO_DIAS:
                    h = await self._get(link, limit=800_000)
                    r = ler_radio_rcb(h) if h else None
                    if r is None:
                        continue
                    r["lido"] = _hoje()
                    contato = re.findall(r"api\.whatsapp\.com/send\?phone=(\d{10,13})", h)
                    if contato:
                        r["whatsapp"] = contato[0]
                    self.d["rcb_radio"][link] = r
                if r.get("uf") and uf and r["uf"] != uf:
                    continue
                out[link] = r
        return out

    async def para(self, rec):
        """(índice tudoradio, índice radios.com.br) só com as rádios da cidade do post."""
        loc = rec.get("loc") or [None, None]
        cidade, uf = loc[0] or "", ((loc[1] if len(loc) > 1 else None) or "").lower()
        if not cidade:
            return {"radios": {}}, {"radios": {}}
        lock = self.locks.setdefault(chave(cidade), asyncio.Lock())
        async with lock:                     # dois posts da mesma cidade: lê a cidade uma vez só
            try:
                tr = await self._tudoradio(cidade, uf) if config.USE_TUDORADIO else {}
            except Exception as ex:
                self.erros["tudoradio"] = repr(ex)
                tr = {}
            try:
                rcb = await self._rcb(cidade, uf, rec) if config.USE_RADIOSCOMBR else {}
            except Exception as ex:
                self.erros["radios.com.br"] = repr(ex)
                rcb = {}
        return {"radios": tr}, {"radios": rcb}

    def resumo(self):
        return (f"Diretórios: {self.lidas['tudoradio']} cidades lidas no tudoradio, {self.lidas['radios.com.br']} no "
                f"radios.com.br ({self.lidas['paginas']} páginas)"
                + (f"; erros: {self.erros}" if self.erros else ""))
