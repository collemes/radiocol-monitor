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
from .extract import band, extract_streams, frequency, name_tokens, norm

IFRAME_RE = re.compile(r"""<iframe\b[^>]*?\b(?:data-)?src\s*=\s*["']([^"']+)["']""", re.I)
SCRIPT_SRC_RE = re.compile(r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
LINK_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
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
    async def _site_candidates(self, site):
        """Raspa o site oficial: página inicial + iframes de player + scripts de player +
        janelas popup + até 3 páginas internas 'ao vivo/player/ouça'. Devolve os endereços achados."""
        cands, vistos = [], set()

        def junta(lista):
            for c in lista:
                if c not in cands:
                    cands.append(c)

        async def le(url, profundidade):
            if url in vistos or len(vistos) >= 14:
                return
            vistos.add(url)
            page = await self._get_text(url)
            if not page:
                return
            junta(extract_streams(page))
            if profundidade == 0:
                return
            host = urlparse(url).netloc.replace("www.", "")
            extras = []
            for ifr in IFRAME_RE.findall(page)[:6]:                       # player em iframe
                extras.append(urljoin(url, ifr))
            for pop in POPUP_RE.findall(page)[:3]:                        # botão que abre janelinha do player
                extras.append(urljoin(url, pop))
            for js in SCRIPT_SRC_RE.findall(page):                        # script do player (ex.: player.js)
                js_url = urljoin(url, js)
                if PLAYER_HINT_RE.search(urlparse(js_url).path) or "radio" in js_url.lower():
                    extras.append(js_url)
            internas = 0
            for href, texto in LINK_RE.findall(page):                     # página "Ao vivo", "Ouça", "Player"
                u = urljoin(url, href.strip())
                if internas >= 3 or not u.startswith("http"):
                    continue
                if urlparse(u).netloc.replace("www.", "") != host:
                    continue
                if PLAYER_HINT_RE.search(urlparse(u).path) or PLAYER_HINT_RE.search(re.sub(r"<[^>]+>", " ", texto)):
                    extras.append(u)
                    internas += 1
            for u in extras:
                if SKIP_HOST_RE.search(urlparse(u).netloc) or u in cands:
                    continue
                await le(u, profundidade - 1)

        await le(site, 2)
        return cands

    async def from_official_site(self, rec):
        if not config.USE_OFFICIAL_SITE:
            return None
        for site in rec.get("sites", [])[:2]:
            cands = await self._site_candidates(site)
            if rec.get("_need_https"):
                cands = [c for c in cands if c.startswith("https")]
            working = []
            for u in cands[:12]:
                if u == rec.get("stream"):
                    continue
                r = await self.c.check(u)
                if r.ok:
                    working.append(u)
            if working:
                https = [w for w in working if w.startswith("https")]
                best = (https or working)[0]
                return {"url": best, "source": f"site oficial ({urlparse(site).netloc})",
                        "confidence": "high" if len(set(working)) == 1 else "suggest",
                        "score": 0.9, "alternatives": working[1:4]}
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
