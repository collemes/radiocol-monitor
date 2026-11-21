"""Procura um endereço de streaming novo para uma rádio que parou de tocar.

Ordem de busca (da mais para a menos confiável):
  1. variações do mesmo endereço (http -> https, /; , /stream ...)  -> aplica sozinho
  2. outros endereços que já estão no texto do post                 -> aplica sozinho
  3. site oficial da emissora (link no post) e iframes do player      -> aplica sozinho se só houver 1
  4. Radio Browser (base aberta e gratuita, radio-browser.info)       -> aplica só com nome+frequência batendo
"""
import asyncio
import random
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, quote, urljoin, urlparse, urlsplit

import aiohttp

from . import config
import json

from .navegador import Navegador
from .extract import band, desescapa, extract_streams, frequency, name_tokens, norm

IFRAME_RE = re.compile(r"""<iframe\b[^>]*?\b(?:data-)?src\s*=\s*["']([^"']+)["']""", re.I)
SCRIPT_SRC_RE = re.compile(r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
LINK_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
API_RE = re.compile(r"""["'`]((?:https?://[^"'`\s]+)?/(?:wp-json|api)/[^"'`\s]*|[^"'`\s]+\.json(?:\?[^"'`\s]*)?)["'`]""", re.I)
POPUP_RE = re.compile(r"""window\.open\(\s*["']([^"']+)["']""", re.I)
# páginas e scripts que costumam ter o player da rádio
PLAYER_HINT_RE = re.compile(r"ao[-_ ]?vivo|player|ou[çc]a|ouvir|escute|listen|live|radio[-_ ]?online|stream|tocador", re.I)
SKIP_HOST_RE = re.compile(r"youtube|youtu\.be|facebook|instagram|google|twitter|x\.com|tiktok|whatsapp|wa\.me|"
                          r"doubleclick|gstatic|cloudflare|jquery|bootstrap|wp-includes|recaptcha|cookie|analytics|"
                          r"hotjar|clarity|pixel|fonts\.|gravatar|addtoany|sharethis|onesignal", re.I)
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
}
RB_SERVERS = [
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://all.api.radio-browser.info",
]


# parâmetros que só servem de "anti-cache", rastreamento ou sessão: o servidor toca igual sem eles
RUIDO_QUERY = {"type", "nocache", "ver", "_", "id", "source", "t", "ts", "time", "cache", "rand", "r", "v"}
RASTREIO_QUERY = {"fbclid", "gclid", "lang", "dist", "tdsdk", "pname", "pversion", "banners", "burst-time", "sbmid",
                  "rj-ttl", "rj-tok"}
# pontos de montagem padrão dos painéis de streaming (não identificam a rádio; quem identifica é a porta)
PONTOS_GENERICOS = {"/;", "/stream", "/live", "/;stream.mp3", "/stream.mp3", "/live.mp3", "/aovivo", "/radio",
                    "/autodj"}


def _origem_no_proxy(url: str, p):
    """Proxies de player que embrulham o stream da rádio no próprio endereço
    (…/stream1234?http://IP:PORTA/;  …/proxy.php?ip=X&port=P&mount=/stream  …/?q=http://…).
    Quando o proxy cai, o servidor de origem — o MESMO stream — costuma continuar no ar."""
    q = p.query or ""
    if re.match(r"https?://", q, re.I):
        return q
    try:
        params = dict((k.lower(), v) for k, v in parse_qsl(q, keep_blank_values=True))
    except ValueError:
        return None
    for k in ("url", "q", "u", "stream", "src", "quest"):
        v = params.get(k) or ""
        if re.match(r"https?://[^/\s]+", v, re.I):
            return v
    ip, porta = params.get("ip") or params.get("host") or params.get("server"), params.get("port") or params.get("porta")
    if ip and porta and re.fullmatch(r"[A-Za-z0-9.-]+", ip) and porta.isdigit():
        mount = params.get("mount") or "/"
        if not mount.startswith("/"):
            mount = "/" + mount
        return f"http://{ip}:{porta}{mount}"
    return None


def _query_so_ruido(p):
    """O endereço tem query, mas só de anti-cache/rastreamento (?type=http&nocache=1, ?1794855577, ?fbclid=…)."""
    if not p.query or re.search(r"\.(php|aspx?|cgi|jsp)$", p.path or "", re.I):
        return False
    try:
        pares = parse_qsl(p.query, keep_blank_values=True)
    except ValueError:
        return False
    if not pares:
        return False
    # na raiz de um servidor compartilhado a query pode escolher a rádio: só com porta própria
    if (p.path or "/") == "/" and (p.port in (None, 80, 443)):
        return False
    for k, v in pares:
        k = k.lower()
        if k in RASTREIO_QUERY or k.startswith("utm_"):
            continue
        if re.fullmatch(r"[\d&]+", k) and not v:          # ?1794855577  ?&1630278471824
            continue
        # anti-cache: só com valor numérico/vazio ou tipo de arquivo (?type=http, ?id=1565655919529, ?_=3)
        # — um valor com nome (?id=radiox) pode escolher a rádio e nunca é removido
        if k in RUIDO_QUERY and re.fullmatch(r"\d*|https?|\.?(mp3|aac|m3u|ogg)|site|website|[0-9a-f]{20,}", v, re.I):
            continue
        return False
    return True


# Shoutcast em http://host:porta/ não toca no site (https). O mesmo servidor costuma aceitar https na mesma
# porta: testa https://host:porta/;stream/1 e depois https://host:porta/; — só vale se vier Content-Type de áudio.
SHOUT_HTTPS_RE = re.compile(r"^https://[^/]+:\d+/;(?:stream/1)?$", re.I)
AUDIO_CT_SHOUT = ("audio/mpeg", "audio/aacp", "audio/aac", "audio/x-aac", "audio/mp3")


def shoutcast_https(url: str):
    """http://host:porta/ (ou /;, /stream, /;stream/1) -> [https://host:porta/;stream/1, https://host:porta/;]"""
    try:
        p = urlsplit(url)
        porta = p.port
    except ValueError:
        return []
    caminho = (p.path or "/").lower()
    if p.scheme != "http" or porta in (None, 80, 443) or p.query or \
            caminho not in ("", "/", "/;", "/;stream/1", "/stream", "/;stream.mp3"):
        return []
    raiz = f"https://{p.hostname}:{porta}"
    return [raiz + "/;stream/1", raiz + "/;"]


def audio_ok(url: str, res) -> bool:
    """Para os endereços da regra Shoutcast https, exige Content-Type de áudio (audio/mpeg ou audio/aacp)."""
    if not res.ok:
        return False
    if SHOUT_HTTPS_RE.match(url):
        return (res.content_type or "").split(";")[0].strip().lower() in AUDIO_CT_SHOUT
    return True


def variants(url: str, _interno=False):
    out = []
    try:
        # urlsplit, não urlparse: o urlparse corta o ";" final do Shoutcast (".../9940/;" virava path "/"
        # e gerava ".../;/;" e ".../;/stream" em vez de ".../stream" e ".../;stream.mp3")
        p = urlsplit(url)
        p.port
    except ValueError:
        return out
    host = (p.hostname or "").lower()
    # Zeno.FM: nó antigo (node-XX.zeno.fm) ou link com token vencido (?rj-tok=…) -> endereço estável
    # stream.zeno.fm/ID (mesmo ID = mesma rádio). As rádios da Radiojar migraram para a Zeno com o mesmo ID.
    mz = re.match(r"^/([a-z0-9]{8,})(?:\.(?:aac|mp3))?/?$", p.path or "", re.I)
    if mz and (host.endswith(".zeno.fm") or host.endswith(".radiojar.com")) and host != "stream.zeno.fm":
        out.append(f"https://stream.zeno.fm/{mz.group(1)}")
    if host == "stream.zeno.fm" and p.query:
        out.append(url.split("?")[0])
    # proxy de player fora do ar: testa o servidor de origem embrulhado no endereço (o mesmo stream)
    if not _interno:
        origem = _origem_no_proxy(url, p)
        if origem and origem != url:
            out.append(origem)
            out += [v for v in variants(origem, _interno=True) if v != url]
    # painel com proxy por nome (…/proxy/NOME?mp=/live): o ponto de montagem do painel mudou (/live <-> /stream)
    mm = re.search(r"([?&]mp=)(/[^&]*)", url)
    if re.match(r"^/proxy/[^/]+/?$", p.path or "") and mm:
        for m in ("/stream", "/live", "/;"):
            if m != mm.group(2):
                out.append(url[: mm.start(2)] + m + url[mm.end(2):])
    # Wowza/HLS: chunklist com número de sessão (chunklist_w123….m3u8) vence; a playlist.m3u8 é a estável
    if re.search(r"/chunklist[^/]*\.m3u8$", p.path or "", re.I):
        out.append(re.sub(r"/chunklist[^/]*\.m3u8(\?.*)?$", "/playlist.m3u8", url))
    # query só de anti-cache/rastreamento (?type=http&nocache=1, ?1794855577, ?fbclid=…): sem ela
    if _query_so_ruido(p):
        out.append(url.split("?")[0])
    if p.scheme == "http":
        out += shoutcast_https(url)                      # regra Shoutcast: ;stream/1 e ; em https, primeiro
        out.append("https://" + url[len("http://"):])
        if p.port == 80 or p.port is None:
            pass
    path = p.path or ""
    base = url.split("?")[0]
    # espaço digitado por engano no endereço (".../proxy/6798%20/;")
    if re.search(r"%20|\s", path):
        limpo = re.sub(r"(%20|\s)+", "", base)
        out.append(limpo)
        base, path = limpo, re.sub(r"(%20|\s)+", "", path)
    # raiz com query que não é só anti-cache (?id=radiox) num servidor compartilhado: a query escolhe a rádio
    raiz_ok = not p.query or _query_so_ruido(p) or p.port not in (None, 80, 443)
    if path in ("", "/") and raiz_ok:
        root = base.rstrip("/")
        out += [root + "/;", root + "/stream", root + "/;stream.mp3"]
    elif path.endswith("/;"):
        out += [base[:-2] + "/stream", base + "stream.mp3"]
    elif path.endswith(";"):
        out.append(base[:-1])
    # porta própria da rádio (Icecast/Shoutcast de painel) com ponto de montagem de nome GENÉRICO (/live, /stream…):
    # o painel trocou o ponto padrão (/live <-> /stream <-> /;). Mesma porta = mesma conta = mesmo stream.
    # Ponto com nome da rádio (/radiox) ou servidor sem porta própria (compartilhado) nunca entra aqui.
    if p.port not in (None, 80, 443) and path.lower() in PONTOS_GENERICOS:
        root = base[: -len(path)]
        out += [root + m for m in ("/;", "/stream", "/live", "/;stream.mp3") if m != path.lower()]
    # Shoutcast atrás de proxy https dos painéis (player.xxx/proxy/PORTA, /player-ssl/PORTA, /PORTA/stream):
    # sem o ";" o Shoutcast devolve a página HTML de status em vez do áudio
    if re.search(r"/\d{3,5}/?$", path):
        root = base.rstrip("/")
        out += [root + "/;", root + "/stream", root + "/;stream.mp3"]
    elif re.search(r"/\d{3,5}/stream$", path):
        root = base[: -len("stream")]
        out += [root + ";", root + ";stream.mp3"]
    host = (p.hostname or "").lower()
    # StreamTheWorld: nó numerado (12345.live.streamtheworld.com) é temporário — some (sem DNS), dá 404 ou para de
    # responder. O redirecionador oficial com o MESMO ponto de montagem manda para um nó vivo (mesma rádio/praça).
    mt = re.match(r"^/([A-Za-z0-9_]+?)(\.(?:aac|mp3))?/?$", path)
    if re.match(r"^\d+\.live\.streamtheworld\.com$", host) and mt and not _interno:
        red = "https://playerservices.streamtheworld.com/api/livestream-redirect/" + mt.group(1) + (mt.group(2) or "")
        out.append(red)
        out += variants(red, _interno=True)
    # StreamTheWorld: o ponto de montagem mudou de nome (…AAC.aac -> …AAC_SC, sem extensão, versão MP3)
    m = re.search(r"(/api/livestream-redirect/)([A-Za-z0-9_]+?)(\.(?:aac|mp3))?$", path)
    if "streamtheworld.com" in host and m:
        pre = url[: url.index(m.group(1))] + m.group(1)
        nome = m.group(2)
        base_nome = nome[:-3] if nome.upper().endswith("AAC") else nome
        for n in (nome + "_SC", nome, base_nome + "AAC_SC", base_nome + "_SC", base_nome + ".mp3", base_nome + "AAC.aac"):
            if n != m.group(2) + (m.group(3) or ""):
                out.append(pre + n)
    # play.radio.br (Wowza/Icecast): HLS fora do ar, mas o mesmo ponto em icecast.audio no ar (e vice-versa)
    if host.endswith("play.radio.br"):
        if path.endswith("/playlist.m3u8"):
            out.append(base[: -len("playlist.m3u8")] + "icecast.audio")
        elif path.endswith("/icecast.audio"):
            out.append(base[: -len("icecast.audio")] + "playlist.m3u8")
    # AzuraCast antigo /radio/PORTA/ponto -> com extensão
    if re.match(r"^/radio/\d+/[^/.]+$", path):
        out += [base + ".mp3", base + ".aac"]
    # AzuraCast: /radio/PORTA/ponto é o proxy do painel para a porta da própria emissora; quando o proxy dá 404,
    # a porta direta (host:PORTA/ponto) é o MESMO stream da mesma conta
    ma = re.match(r"^/radio/(\d{4,5})/([^/]+)$", path)
    if ma and p.port in (None, 80, 443):
        for esq in dict.fromkeys((p.scheme, "http")):
            out.append(f"{esq}://{host}:{ma.group(1)}/{ma.group(2)}")
    if p.scheme == "http":
        out += ["https://" + v[len("http://"):] for v in list(out) if v.startswith("http://")]
    seen, res = set(), []
    for v in out:
        if v != url and v not in seen:
            seen.add(v)
            res.append(v)
    # https primeiro (toca no site sem bloqueio de "conteúdo misto")
    return sorted(res, key=lambda v: 0 if v.startswith("https") else 1)


def similarity(post_title: str, station_name: str) -> float:
    a, b = name_tokens(post_title), name_tokens(station_name)
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    jacc = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    cover = len(sa & sb) / len(sa)  # quanto do nome do post aparece na estação
    return round(0.4 * seq + 0.3 * jacc + 0.3 * cover, 3)


LIMITE_OUTRA_PRACA = 0.59   # abaixo de APLICAR_MIN_SCORE: vira só sugestão, nunca entra sozinho


def rb_mesma_praca(rec, texto):
    """A estação do Radio Browser é da cidade do post? (sem cidade conhecida: não dá para garantir)"""
    cidade = norm((rec.get("loc") or [""])[0] or "")
    return bool(cidade) and cidade in norm(texto or "")


def _uf_do_texto(texto):
    """UFs citadas no texto ('Paracatu, MG', '(Juiz de Fora/MG)', 'Minas Gerais')."""
    from .extract import UF_NOMES
    t = norm(texto or "")
    achadas = {u for u in re.findall(r"(?<![a-z])([a-z]{2})(?![a-z])", t) if u in UF_NOMES}
    for u, nome in UF_NOMES.items():
        if norm(nome.split(" ", 1)[1]) in t:
            achadas.add(u)
    return achadas


def rb_confere(rec, nome, st=None):
    """Estação do Radio Browser = rádio do post? Cidade E frequência E UF conferem (o nome é opcional).
    'nome' é o nome da estação; 'st' o registro completo (estado, tags, homepage), quando houver."""
    st = st or {}
    loc = rec.get("loc") or [None, None]
    cidade, uf = loc[0] or "", (loc[1] if len(loc) > 1 else None) or ""
    texto = " ".join([nome or "", st.get("tags") or "", st.get("homepage") or ""])
    cid = re.sub(r"[^a-z0-9]", "", norm(cidade))
    if len(cid) < 4 or cid not in re.sub(r"[^a-z0-9]", "", norm(texto)):
        return False                                             # cidade
    f = frequency(rec.get("title", ""))
    if not f or frequency(texto) != f:
        return False                                             # frequência
    ufs = _uf_do_texto((st.get("state") or "") + " " + (nome or ""))
    if ufs and uf and uf not in ufs:
        return False                                             # UF diferente
    if not uf:
        return False
    return True


class Finder:
    def __init__(self, session: aiohttp.ClientSession, checker):
        self.s = session
        self.c = checker
        self.rb = None
        self.rede = set()          # domínios usados por 3+ posts (sites de rede): o run.py preenche
        self._sites = {}           # cache da raspagem por site (a rede é raspada 1x por execução)
        self._listas = {}          # cache das listas de emissoras dos servidores (AzuraCast/Icecast)
        self.nav = Navegador()     # Chromium: sites com player montado por JavaScript
        self.tudo = None           # índice do tudoradio.com (o run.py preenche)
        self.rcb = None            # índice do radios.com.br (o run.py preenche)

    async def _get_text(self, url, limit=400_000):
        try:
            async with self.s.get(url, headers={"User-Agent": config.USER_AGENT, **BROWSER_HEADERS},
                                  timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status >= 400:
                    return ""
                ct = r.headers.get("Content-Type", "")
                if not any(k in ct for k in ("html", "text", "json", "javascript", "ecmascript", "xml")):
                    return ""
                return (await r.content.read(limit)).decode("utf-8", "ignore")
        except Exception:
            return ""

    async def _first_ok(self, urls, limit=6):
        for u in urls[:limit]:
            r = await self.c.check(u)
            if r.ok:
                return u, r
        return None, None

    # ------------------------------------------------------------------ 1 e 2
    async def from_variants(self, rec):
        bases = [rec.get("stream"), rec.get("post_stream")] + rec.get("post_candidates", [])
        tried, cands = set(), []
        for b in bases:
            if b and b not in tried:
                tried.add(b)
                if b != rec.get("stream"):
                    cands.append((b, "outro link do post"))
                cands += [(v, "variação do endereço") for v in variants(b)]
        seen = set()
        for u, src in cands:
            if u in seen or u == rec.get("stream"):
                continue
            if rec.get("_need_https") and not u.startswith("https"):
                continue
            seen.add(u)
            r = await self.c.check(u)
            if audio_ok(u, r):
                if SHOUT_HTTPS_RE.match(u):
                    src = "Shoutcast em https (mesmo servidor e porta)"
                return {"url": u, "source": src, "confidence": "high", "score": 1.0}
        return await self.from_mesma_conta(rec)

    @staticmethod
    def _conta(url):
        """Chave da 'conta' da rádio no servidor de streaming: host + porta própria (Shoutcast/Icecast de painel)
        ou host + /radio/PORTA (AzuraCast). Sem porta própria o servidor é compartilhado: None."""
        try:
            p = urlparse(url)
            porta = p.port
        except ValueError:
            return None
        host = (p.hostname or "").lower()
        m = re.match(r"/radio/(\d{4,5})(/|$)", p.path or "")
        if m:
            return (host, "radio", m.group(1))
        if porta and porta not in (80, 443):
            return (host, porta)
        return None

    async def from_mesma_conta(self, rec):
        """Ponto de montagem renomeado (HTTP 404) no MESMO servidor e MESMA porta/conta da rádio: pergunta ao
        próprio servidor (Icecast status-json / AzuraCast api) qual é o ponto atual. Só aplica se houver um só."""
        url = rec.get("stream") or ""
        if not (rec.get("reason") or "").startswith("HTTP 404"):
            return None
        conta = self._conta(url)
        if not conta:
            return None
        pu = urlparse(url)
        achados = []
        for u, _ in await self._lista_servidor(url):
            try:
                pv = urlparse(u)
                if conta[1] == "radio":
                    ok = re.match(rf"/radio/{conta[2]}(/|$)", pv.path or "")
                else:
                    ok = pv.port == conta[1]   # Icecast pode anunciar outro nome de host: vale a porta
            except ValueError:
                continue
            if ok:
                # usa o host e o esquema do endereço do post (o nome anunciado pode não ter certificado)
                achados.append(f"{pu.scheme}://{pu.netloc}{pv.path}" + (f"?{pv.query}" if pv.query else ""))
        achados = [a for a in dict.fromkeys(achados) if a != url]
        if rec.get("_need_https"):
            achados = [a for a in achados if a.startswith("https")]
        if len(achados) != 1:
            return None
        if (await self.c.check(achados[0])).ok:
            return {"url": achados[0], "source": "mesmo servidor e porta da rádio (ponto de montagem novo)",
                    "confidence": "high", "score": 0.95}
        return None

    # ------------------------------------------------------------------ 3
    @staticmethod
    def _contextos(texto, urls):
        """Para cada endereço achado, o trecho do texto em volta (onde aparece o nome da praça)."""
        t = desescapa(texto)
        out = []
        for u in urls:
            i = t.find(u)
            if i < 0:
                i = t.find(u.split("//", 1)[-1])
            if i < 0:
                out.append((u, ""))
                continue
            # o trecho é o item da lista onde o endereço está: objeto JSON {...} ou <li>/<option>/<a>/<tr>
            antes, depois = t[max(0, i - 400): i], t[i + len(u): i + len(u) + 300]
            ini = max(antes.rfind(k) for k in ("{", "<li", "<option", "<tr", "<a ", "<button", "<div"))
            antes = antes[ini:] if ini >= 0 else antes[-200:]
            fins = [j for j in (depois.find(k) for k in ("}", "</li", "</option", "</tr", "</a", "</button", "</div")) if j >= 0]
            depois = depois[:min(fins)] if fins else depois[:150]
            ctx = antes + u + depois
            out.append((u, norm(re.sub(r"<[^>]+>", " ", ctx))))
        return out

    async def _lista_servidor(self, url):
        """AzuraCast (/listen/... ou /radio/...): pede /api/stations e devolve todas as emissoras do servidor,
        cada uma com o nome/descrição como contexto. Icecast: /status-json.xsl. Uma vez por servidor."""
        pu = urlparse(url)
        base = f"{pu.scheme}://{pu.netloc}"
        if base in self._listas:
            return self._listas[base]
        self._listas[base] = []
        achados = []
        if re.match(r"/(listen|radio|public)/", pu.path):
            txt = await self._get_text(base + "/api/stations", limit=2_000_000)
            try:
                for st in json.loads(txt) if txt else []:
                    ctx = norm(" ".join(str(st.get(k) or "") for k in ("name", "shortcode", "description")))
                    urls = [st.get("listen_url")] + [m.get("url") for m in st.get("mounts") or []]
                    for u in urls:
                        if u and u.startswith("http"):
                            achados.append((u, ctx))
            except Exception:
                pass
        if not achados:
            txt = await self._get_text(base + "/status-json.xsl", limit=2_000_000)
            try:
                src = (json.loads(txt).get("icestats") or {}).get("source") if txt else None
                for so in ([src] if isinstance(src, dict) else src or []):
                    ctx = norm(" ".join(str(so.get(k) or "") for k in ("server_name", "server_description", "listenurl")))
                    if so.get("listenurl"):
                        achados.append((so["listenurl"].replace("http://", pu.scheme + "://", 1)
                                        if pu.scheme == "https" else so["listenurl"], ctx))
            except Exception:
                pass
        self._listas[base] = achados
        return achados

    async def _site_candidates(self, site):
        """Raspa o site oficial e devolve [(endereço, contexto)]: página inicial, iframes de player,
        janelas popup, scripts (do próprio site e de players), listas JSON/API e até 3 páginas
        internas 'ao vivo/player/ouça'. Sites de rede são raspados uma vez por execução."""
        if site in self._sites:
            return self._sites[site]
        cands, vistos = [], set()
        host = urlparse(site).netloc.replace("www.", "")

        def junta(pares):
            for u, ctx in pares:
                if u not in [c[0] for c in cands]:
                    cands.append((u, ctx))

        async def le(url, profundidade, limite=400_000):
            if url in vistos or len(vistos) >= 24:
                return
            vistos.add(url)
            page = await self._get_text(url, limit=limite)
            if not page:
                return
            junta(self._contextos(page, extract_streams(page)))
            if profundidade == 0:
                return
            texto = desescapa(page)
            extras = []
            for ifr in IFRAME_RE.findall(page)[:6]:                       # player em iframe
                extras.append((urljoin(url, ifr), 400_000))
            for pop in POPUP_RE.findall(page)[:3]:                        # botão que abre janelinha do player
                extras.append((urljoin(url, pop), 400_000))
            for js in SCRIPT_SRC_RE.findall(page):                        # scripts: do site (qualquer nome) e de players
                js_url = urljoin(url, js)
                mesmo = urlparse(js_url).netloc.replace("www.", "") == host
                if mesmo or PLAYER_HINT_RE.search(urlparse(js_url).path) or "radio" in js_url.lower():
                    extras.append((js_url, 2_000_000))
            for api in API_RE.findall(texto)[:8]:                         # listas de praças em JSON / API
                api_url = urljoin(url, api)
                if urlparse(api_url).netloc.replace("www.", "") == host:
                    extras.append((api_url, 2_000_000))
            internas = 0
            for href, txt in LINK_RE.findall(page):                       # página "Ao vivo", "Ouça", "Player"
                u = urljoin(url, href.strip())
                if internas >= 3 or not u.startswith("http") or urlparse(u).netloc.replace("www.", "") != host:
                    continue
                if PLAYER_HINT_RE.search(urlparse(u).path) or PLAYER_HINT_RE.search(re.sub(r"<[^>]+>", " ", txt)):
                    extras.append((u, 400_000))
                    internas += 1
            scripts = 0
            for u, lim in extras:
                if SKIP_HOST_RE.search(urlparse(u).netloc) or u in [c[0] for c in cands]:
                    continue
                if lim > 400_000:
                    scripts += 1
                    if scripts > 10:
                        continue
                await le(u, 1 if lim > 400_000 else profundidade - 1, lim)   # script: ainda segue as APIs que ele chama

        await le(site, 2)
        # servidores de rede (AzuraCast / Icecast): a lista completa de emissoras, com nome de cada praça
        for u, _ in list(cands)[:6]:
            junta(await self._lista_servidor(u))
        self._sites[site] = cands
        return cands

    @staticmethod
    def _pontos_praca(rec, url, ctx):
        """Quanto o endereço/trecho combina com a cidade e a frequência do post."""
        pts = 0
        compacto = lambda t: re.sub(r"[^a-z0-9]", "", norm(t))
        alvo = compacto(url) + " " + compacto(ctx)
        loc = rec.get("loc") or []
        if loc and loc[0] and len(compacto(loc[0])) >= 4 and compacto(loc[0]) in alvo:
            pts += 3
        f = frequency(rec.get("title", ""))
        if f and "." in f:
            a, b = f.split(".")
            if re.search(rf"(?<!\d){a}[.,_ ]?{b}(?!\d)", norm(url) + " " + ctx):
                pts += 2
        return pts

    async def _escolhe(self, rec, site, pares, tocou=frozenset()):
        """Escolhe o endereço certo entre os achados no site. 'tocou' = endereços que o navegador
        recebeu como áudio de verdade (vale como teste, mesmo que o teste do robô falhe)."""
        if rec.get("_need_https"):
            pares = [p for p in pares if p[0].startswith("https")]
        pares = [p for p in pares if p[0] != rec.get("stream")]
        dominio = urlparse(site).netloc.replace("www.", "")

        async def funciona(u):
            return u in tocou or (await self.c.check(u)).ok

        if dominio in self.rede or len({p[0] for p in pares}) > 3:
            # site de rede: só serve o player da praça do post (cidade e/ou frequência no endereço ou ao lado dele)
            pontuados = sorted(((self._pontos_praca(rec, u, c), u) for u, c in pares), key=lambda x: -x[0])
            pontuados = [x for x in pontuados if x[0] > 0][:8]
            bons = []
            for pts, u in pontuados:
                if bons and pts < bons[0][0]:
                    break                                 # já achou o melhor nível de pontos
                if await funciona(u):
                    bons.append((pts, u))
            if bons:
                pts, u = bons[0]
                empate = len({x[1] for x in bons}) > 1    # mais de um endereço QUE TOCA com os mesmos pontos
                return {"url": u, "source": f"site da rede ({dominio}, praça do post)",
                        "confidence": "high" if pts >= 3 and not empate else "suggest",
                        "score": 0.85 if pts >= 3 and not empate else 0.65}
            return None
        working = []
        for u, _ in pares[:12]:
            if await funciona(u):
                working.append(u)
        if working:
            prefer = [w for w in working if w in tocou] + [w for w in working if w.startswith("https")]
            best = (prefer or working)[0]
            return {"url": best, "source": f"site oficial ({dominio})" + (" — tocou no navegador" if best in tocou else ""),
                    "confidence": "high" if len(set(working)) == 1 or best in tocou else "suggest",
                    "score": 0.9, "alternatives": [w for w in working if w != best][:3]}
        return None

    @staticmethod
    def _paginas(rec):
        """Sites oficiais do post (até 2) e, se o 'stream' do post é na verdade a página do player da rádio
        (responde HTML: player.xxx/proxy/PORTA, play.php?porta=…, site/radio/nome), essa página também —
        o áudio de verdade está dentro dela."""
        paginas = list(rec.get("sites", [])[:2])
        st = rec.get("stream") or ""
        if (rec.get("reason") or "").startswith("responde página HTML") and st.startswith("http") \
                and st not in paginas:
            paginas.append(st)
        return paginas

    @staticmethod
    def _ajusta_pagina_player(achado, pagina_player):
        """Página do player que estava no post: só entra sozinho se um único endereço tocar;
        com vários, vira sugestão (abaixo de APLICAR_MIN_SCORE) — pode ser página de rede com outras praças."""
        if achado and pagina_player:
            achado["source"] = achado["source"].replace("site oficial", "página do player do post")
            if achado["confidence"] != "high":
                achado["score"] = min(achado.get("score") or 0, 0.55)
        return achado

    async def from_official_site(self, rec):
        if not config.USE_OFFICIAL_SITE:
            return None
        for site in self._paginas(rec):
            if not site.startswith("http") or "tel+" in site:
                continue
            pagina_player = site == rec.get("stream")
            # 1) leitura rápida do HTML (+ scripts, APIs, servidores de rede)
            achado = self._ajusta_pagina_player(
                await self._escolhe(rec, site, await self._site_candidates(site)), pagina_player)
            if achado:
                return achado
            # 2) navegador de verdade: abre o site (e troca a praça, em site de rede), clica no play
            #    e vê o que o player toca
            dominio = urlparse(site).netloc.replace("www.", "")
            rede = dominio in self.rede
            cidade = (rec.get("loc") or [None])[0] if rede else None
            capt = await self.nav.capturar(site, cidade)
            if capt:
                pares = [(u, c) for u, c, _ in capt]
                for u, _ in list(pares)[:6]:                     # servidor de rede: lista de emissoras
                    pares += [x for x in await self._lista_servidor(u) if x[0] not in {p[0] for p in pares}]
                achado = self._ajusta_pagina_player(
                    await self._escolhe(rec, site, pares, frozenset(u for u, _, t in capt if t)), pagina_player)
                if achado:
                    achado["source"] += " [navegador]"
                    return achado
                # o player do site usa um servidor que não responde ao robô (fora do Brasil): aceita o endereço
                # que o PRÓPRIO site toca, se for um só (em rede: o da praça do post). Ouvintes confirmam depois.
                bloq = []
                for u, c in pares[:10]:
                    if u == rec.get("stream") or (rec.get("_need_https") and not u.startswith("https")):
                        continue
                    if rede and self._pontos_praca(rec, u, c) < 3:
                        continue
                    r = await self.c.check(u)
                    if not r.ok and re.search(r"tempo esgotado|Refused|Reset|Disconnected", r.reason or ""):
                        bloq.append(u)
                if len(set(bloq)) == 1:
                    return {"url": bloq[0], "source": f"player do site {dominio} (não testável daqui) [navegador]",
                            "confidence": "high", "score": 0.8, "nao_testavel": True}
        return None

    # ------------------------------------------------------------------ 4
    async def _rb_get(self, path):
        servers = [self.rb] if self.rb else random.sample(RB_SERVERS, len(RB_SERVERS))
        for srv in servers:
            try:
                async with self.s.get(srv + path, headers={"User-Agent": "RadiocolMonitor/1.0"},
                                      timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        self.rb = srv
                        return await r.json(content_type=None)
            except Exception:
                continue
            self.rb = None
        return []

    async def from_radio_browser(self, rec):
        if not config.USE_RADIO_BROWSER:
            return None
        title = rec.get("title", "")
        toks = name_tokens(title)
        if not toks:
            return None
        queries = [" ".join(toks)]
        if len(toks) > 1:
            queries.append(max(toks, key=len))
        freq = frequency(title)
        post_band = band(title)
        labels = " ".join(norm(l) for l in rec.get("labels", []))
        stations = {}
        for q in queries:
            data = await self._rb_get(
                f"/json/stations/search?name={quote(q)}&countrycode={config.COUNTRY_CODE}"
                f"&hidebroken=true&order=votes&reverse=true&limit=20")
            for st in data or []:
                stations.setdefault(st.get("stationuuid"), st)
            if len(stations) >= 5:
                break
        scored = []
        for st in stations.values():
            name = st.get("name", "")
            sc = similarity(title, name)
            sfreq = frequency(name + " " + (st.get("tags") or ""))
            st_band = band(name)
            if freq and sfreq:
                sc += 0.15 if sfreq == freq else -0.4
            elif freq and not sfreq:
                sc -= 0.05
            if post_band and st_band and post_band != st_band:
                sc -= 0.4          # AM x FM: emissoras diferentes
            freq_ok = not freq or sfreq == freq
            state = norm(st.get("state") or "")
            if state and state in labels:
                sc += 0.05
            url = st.get("url_resolved") or st.get("url")
            if rec.get("_need_https") and url and not url.startswith("https"):
                continue
            # rádio de rede (Jovem Pan, Transamérica, Nova Brasil…): só vale a estação da MESMA cidade do post
            if not rb_mesma_praca(rec, name + " " + (st.get("tags") or "") + " " + (st.get("homepage") or "")):
                sc = min(sc, LIMITE_OUTRA_PRACA)
            if url and sc >= config.SUGGEST_SCORE:
                scored.append((round(min(sc, 1.0), 3), url, name, freq_ok))
        scored.sort(key=lambda x: (-x[0], 0 if x[1].startswith("https") else 1))
        for sc, url, name, freq_ok in scored[:5]:
            r = await self.c.check(url)
            if r.ok:
                st = next((x for x in stations.values() if (x.get("url_resolved") or x.get("url")) == url), {})
                if rb_confere(rec, name, st):
                    # cidade, UF e frequência da estação conferem com o post (o nome é opcional): entra
                    return {"url": url, "site": st.get("homepage") or None,
                            "source": f"Radio Browser: {name} — cidade, UF e frequência conferem",
                            "score": max(sc, 0.9), "confidence": "high", "rb_confere": True}
                return {"url": url, "source": f"Radio Browser: {name}", "score": min(sc, 0.59), "confidence": "suggest"}
        return None

    async def from_tudoradio(self, rec):
        """Diretórios brasileiros (tudoradio.com e radios.com.br): página da MESMA cidade e UF do post e mesma
        frequência e faixa (webradio: mesmo nome) -> endereço do player deles."""
        from . import tudoradio
        for rotulo, indice in (("tudoradio", self.tudo), ("radios.com.br", self.rcb)):
            if not indice:
                continue
            for pagina, r in tudoradio.candidatos(indice, rec, frequency, band)[:3]:
                origem = re.match(r"https?://[^/]+", pagina).group(0)
                lidos = [u for u in r.get("streams") or [] if u.rstrip("/") != origem]
                for tentativa in ("página", "navegador"):
                    if tentativa == "navegador":     # player montado por script: abre a página e dá play
                        lidos = [u for u, _, _ in await self.nav.capturar(pagina)
                                 if urlparse(u).netloc.replace("www.", "") != urlparse(pagina).netloc.replace("www.", "")
                                 and u not in lidos]
                    for u in lidos[:6]:
                        if u == rec.get("stream") or (rec.get("_need_https") and not u.startswith("https")):
                            continue
                        if audio_ok(u, await self.c.check(u)):
                            if r.get("web"):
                                prova = "webradio: cidade, UF e nome conferem"
                                faixa = "webradio"
                            else:
                                prova = "cidade, UF e frequência conferem"
                                faixa = f"{r.get('freq')} {(r.get('banda') or '').upper()}"
                            return {"url": u, "confidence": "high", "score": 0.95, "pagina": pagina, "site": r.get("site"),
                                    "source": f"{rotulo}: {r.get('nome')} ({r.get('cidade')}/"
                                              f"{(r.get('uf') or '').upper()}, {faixa}) — {prova}"}
        return None

    async def find(self, rec):
        for step in (self.from_variants, self.from_official_site, self.from_tudoradio, self.from_radio_browser):
            try:
                res = await step(rec)
            except Exception:
                res = None
            if res:
                return res
        return None
