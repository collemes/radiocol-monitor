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
from urllib.parse import quote, urljoin, urlparse

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


def variants(url: str):
    out = []
    try:
        p = urlparse(url)
    except ValueError:
        return out
    if p.scheme == "http":
        out.append("https://" + url[len("http://"):])
        if p.port == 80 or p.port is None:
            pass
    path = p.path or ""
    base = url.split("?")[0]
    if path in ("", "/"):
        root = base.rstrip("/")
        out += [root + "/;", root + "/stream", root + "/;stream.mp3"]
    elif path.endswith("/;"):
        out.append(base[:-2] + "/stream")
    elif path.endswith(";"):
        out.append(base[:-1])
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


class Finder:
    def __init__(self, session: aiohttp.ClientSession, checker):
        self.s = session
        self.c = checker
        self.rb = None
        self.rede = set()          # domínios usados por 3+ posts (sites de rede): o run.py preenche
        self._sites = {}           # cache da raspagem por site (a rede é raspada 1x por execução)
        self._listas = {}          # cache das listas de emissoras dos servidores (AzuraCast/Icecast)
        self.nav = Navegador()     # Chromium: sites com player montado por JavaScript

    async def _get_text(self, url, limit=400_000):
        try:
            async with self.s.get(url, headers={"User-Agent": config.USER_AGENT, **BROWSER_HEADERS},
                                  timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status >= 400:
                    return ""
                ct = r.headers.get("Content-Type", "")
                if not any(k in ct for k in ("html", "text", "json", "javascript", "ecmascript")):
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
            if r.ok:
                return {"url": u, "source": src, "confidence": "high", "score": 1.0}
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

    async def from_official_site(self, rec):
        if not config.USE_OFFICIAL_SITE:
            return None
        for site in rec.get("sites", [])[:2]:
            if not site.startswith("http") or "tel+" in site:
                continue
            # 1) leitura rápida do HTML (+ scripts, APIs, servidores de rede)
            achado = await self._escolhe(rec, site, await self._site_candidates(site))
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
                achado = await self._escolhe(rec, site, pares, frozenset(u for u, _, t in capt if t))
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
            if url and sc >= config.SUGGEST_SCORE:
                scored.append((round(min(sc, 1.0), 3), url, name, freq_ok))
        scored.sort(key=lambda x: (-x[0], 0 if x[1].startswith("https") else 1))
        for sc, url, name, freq_ok in scored[:5]:
            r = await self.c.check(url)
            if r.ok:
                # só aplica sozinho se o nome bate E a frequência do post confere com a da estação
                return {"url": url, "source": f"Radio Browser: {name}", "score": sc,
                        "confidence": "high" if sc >= config.AUTO_APPLY_SCORE and freq_ok else "suggest"}
        return None

    async def find(self, rec):
        for step in (self.from_variants, self.from_official_site, self.from_radio_browser):
            try:
                res = await step(rec)
            except Exception:
                res = None
            if res:
                return res
        return None
