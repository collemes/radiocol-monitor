"""Navegador de verdade (Chromium via Playwright) para achar o player no site da emissora.

Muitos sites montam o player por JavaScript (lista de praças carregada por API, player que só
aparece depois de clicar). A leitura simples do HTML não enxerga isso. Aqui o robô:
  1. abre o site como um visitante (pt-BR), espera os scripts;
  2. clica nos botões de play / "ouvir" / "ao vivo" (na página e dentro de iframes);
  3. anota tudo o que o navegador pediu de áudio (pedidos do tipo "media", .m3u8, /stream, /listen...)
     e o que o player ficou tocando (<audio>.currentSrc);
  4. lê as respostas JSON das APIs do site (listas de praças com endereços).
Devolve [(endereço, contexto, tocou_no_navegador)].
"""
import asyncio
import re
import time

from . import config
from .extract import desescapa, extract_streams, norm, parece_video

try:
    from playwright.async_api import async_playwright
except Exception:  # Playwright não instalado: o robô segue só com a leitura de HTML
    async_playwright = None

AUDIO_URL_RE = re.compile(
    r"\.(mp3|aac|aacp|ogg|opus|m3u8|pls|m3u)(\?|$)|/(stream|listen|live|radio|;)(/|\?|$|\.)|:\d{4,5}/|"
    r"icecast|shoutcast|zeno\.fm|radio\.co|azuracast|/proxy/", re.I)
AUDIO_CT_RE = re.compile(r"audio/|mpegurl|application/ogg|video/mp2t|application/octet-stream", re.I)
IGNORAR_RE = re.compile(r"google|doubleclick|facebook|youtube|ytimg|gstatic|analytics|hotjar|clarity|"
                        r"scdn\.co|mp3-preview|dzcdn|itunes\.apple|mzstatic|sndcdn|status(-json)?\.xsl|"
                        r"\.(png|jpe?g|gif|webp|svg|css|woff2?|ttf|ico)(\?|$)", re.I)
ABRE_PLAYER_RE = re.compile(r"ou[çc]a|ouvir|escute|ao\s*vivo|player|tocar|r[áa]dio\s*online|clique\s*(aqui\s*)?para\s*ouvir|"
                            r"listen|play\b|\u25b6|►", re.I)
ABRE_PLAYER_HREF_RE = re.compile(r"player|aovivo|ao-vivo|ouvir|ouca|popup|listen|radio\.php|tocador", re.I)
TROCAR_RE = re.compile(r"(trocar|mudar|escolher|selecionar|alterar|outras?)\s+(a\s+)?(pra[çc]a|cidade|emissora|regi[ãa]o|r[áa]dio)", re.I)
PLAY_SELETORES = [
    "[aria-label*='play' i]", "[aria-label*='ouvir' i]", "[aria-label*='tocar' i]", "[title*='play' i]",
    "[title*='ouvir' i]", "button[class*='play' i]", "a[class*='play' i]", "div[class*='play' i]",
    "[id*='play' i]", ".mejs-playpause-button button", ".jp-play", ".vjs-play-control", ".plyr__control",
    "text=/ouvir ao vivo/i", "text=/ou[çc]a ao vivo/i", "text=/^\\s*ao vivo\\s*$/i", "text=/^\\s*play\\s*$/i",
]


class Navegador:
    def __init__(self):
        self._pw = self._browser = None
        self._cache = {}
        self._lock = asyncio.Semaphore(config.BROWSER_CONCURRENCY)
        self.usados = 0
        self._t0 = time.time()

    @property
    def disponivel(self):
        return async_playwright is not None and config.USE_BROWSER

    async def abrir(self):
        if self._browser or not self.disponivel:
            return self._browser is not None
        try:
            self._pw = await async_playwright().start()
            kw = {"headless": True, "args": ["--autoplay-policy=no-user-gesture-required", "--mute-audio"]}
            if config.BROWSER_PATH:
                kw["executable_path"] = config.BROWSER_PATH
            self._browser = await self._pw.chromium.launch(**kw)
            return True
        except Exception as ex:
            print(f"[navegador] não abriu: {ex}", flush=True)
            self._browser = None
            return False

    async def fechar(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    async def capturar(self, site, cidade=None):
        """[(url, contexto, tocou)] encontrados abrindo o site num navegador. Cache por site + cidade.
        Com 'cidade' (sites de rede), tenta trocar a praça do player para essa cidade antes do play."""
        chave = (site, norm(cidade or ""))
        if chave in self._cache:
            return self._cache[chave]
        if time.time() - self._t0 > config.BROWSER_BUDGET_S:
            return []
        if self.usados >= config.MAX_BROWSER or not await self.abrir():
            return []
        self.usados += 1
        async with self._lock:
            try:
                achados = await asyncio.wait_for(self._capturar(site, cidade), timeout=config.BROWSER_SITE_S)
            except Exception as ex:
                print(f"[navegador] {site}: {type(ex).__name__}", flush=True)
                achados = []
        self._cache[chave] = achados
        return achados

    async def _trocar_praca(self, page, cidade, marca):
        """Sites de rede: abre o seletor de praça/cidade, busca a cidade e clica nela."""
        alvo = re.compile(re.escape(cidade), re.I)
        for frame in page.frames[:4]:
            try:
                botao = frame.get_by_text(TROCAR_RE).first
                if await botao.count() and await botao.is_visible(timeout=500):
                    await botao.click(timeout=2000, force=True)
                    await page.wait_for_timeout(1500)
            except Exception:
                pass
            try:
                caixa = frame.locator("input[type=search], input[type=text], input:not([type])").first
                if await caixa.count() and await caixa.is_visible(timeout=500):
                    await caixa.fill(cidade, timeout=2000)
                    await page.wait_for_timeout(1500)
            except Exception:
                pass
            try:
                item = frame.get_by_text(alvo).first
                if await item.count() and await item.is_visible(timeout=800):
                    await item.click(timeout=2000, force=True)
                    marca["ctx"] = norm(cidade)            # o que tocar daqui em diante é da praça do post
                    await page.wait_for_timeout(3000)
                    return True
            except Exception:
                pass
        return False

    async def _abrir_player(self, page):
        """Clica em até 3 botões/links que levam ao player ("Ouça ao vivo", "Ouvir agora", "Player", "Rádio
        online", "Clique para ouvir"…), inclusive os que abrem popup ou outra aba (outro site)."""
        clicados = 0
        try:
            candidatos = page.locator("a, button, [onclick], [role=button]")
            n = min(await candidatos.count(), 150)
        except Exception:
            return
        for i in range(n):
            if clicados >= 3:
                break
            try:
                el = candidatos.nth(i)
                texto = ((await el.inner_text(timeout=300)) or "") + " " + ((await el.get_attribute("title")) or "") + \
                    " " + ((await el.get_attribute("aria-label")) or "")
                href = (await el.get_attribute("href")) or ""
                if not ABRE_PLAYER_RE.search(texto) and not ABRE_PLAYER_HREF_RE.search(href):
                    continue
                if IGNORAR_RE.search(href) or re.search(r"whatsapp|wa\.me|instagram|facebook|youtube|mailto:|tel:", href, re.I):
                    continue
                if not await el.is_visible(timeout=300):
                    continue
                await el.click(timeout=2000, force=True, modifiers=[])
                clicados += 1
                await page.wait_for_timeout(4000)
            except Exception:
                continue

    async def _capturar(self, site, cidade=None):
        ctx = await self._browser.new_context(locale="pt-BR", user_agent=config.USER_AGENT,
                                              viewport={"width": 1280, "height": 900}, ignore_https_errors=True)
        achados = {}   # url -> [contexto, tocou]
        marca = {"ctx": ""}

        def anota(url, contexto="", tocou=False):
            if not url or not url.startswith("http") or IGNORAR_RE.search(url) or parece_video(url):
                return
            contexto = (marca["ctx"] + " " + contexto).strip()
            a = achados.setdefault(url, ["", False])
            if contexto and len(a[0]) < 600:
                a[0] = (a[0] + " " + contexto)[:600]
            a[1] = a[1] or tocou

        async def ao_pedir(req):
            if req.resource_type == "media" or AUDIO_URL_RE.search(req.url):
                anota(req.url)

        async def ao_responder(resp):
            try:
                url, ct = resp.url, resp.headers.get("content-type", "")
                if ct.lower().startswith("video/") and "mp2t" not in ct.lower():
                    return                       # vídeo (vinheta/anúncio) que o navegador tocou: não é a rádio
                if resp.request.resource_type == "media" or AUDIO_CT_RE.search(ct):
                    anota(url, tocou=resp.status in (200, 206))      # o navegador recebeu áudio de verdade
                elif resp.request.resource_type in ("xhr", "fetch") and ("json" in ct or "text" in ct):
                    corpo = await resp.text()
                    if len(corpo) < 2_000_000:
                        texto = desescapa(corpo)
                        for u in extract_streams(texto):
                            i = texto.find(u)
                            anota(u, norm(texto[max(0, i - 300): i + len(u) + 200]) if i >= 0 else "")
            except Exception:
                pass

        page = None
        # escuta o CONTEXTO inteiro: pedidos de popups e novas abas abertos pelo botão do player também contam
        ctx.on("request", lambda r: asyncio.ensure_future(ao_pedir(r)))
        ctx.on("response", lambda r: asyncio.ensure_future(ao_responder(r)))
        page = await ctx.new_page()
        try:
            try:
                await page.goto(site, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
            if cidade:
                await self._trocar_praca(page, cidade, marca)
            # clica no play (página e iframes); para no 1º clique que fizer o navegador tocar algo
            for frame in page.frames[:6]:
                for sel in PLAY_SELETORES:
                    if any(a[1] for a in achados.values()):
                        break
                    try:
                        el = frame.locator(sel).first
                        if await el.count() and await el.is_visible(timeout=500):
                            await el.click(timeout=2000, force=True)
                            await page.wait_for_timeout(3500)
                    except Exception:
                        continue
            # botão/link do tipo "Ouça ao vivo", "Ouvir", "Player": muitos abrem o player num popup ou em outra aba
            if not any(a[1] for a in achados.values()):
                await self._abrir_player(page)
                for nova in [p for p in ctx.pages if p is not page][:3]:
                    try:
                        await nova.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    await nova.wait_for_timeout(2500)
                    for frame in nova.frames[:6]:
                        for sel in PLAY_SELETORES:
                            if any(a[1] for a in achados.values()):
                                break
                            try:
                                el = frame.locator(sel).first
                                if await el.count() and await el.is_visible(timeout=500):
                                    await el.click(timeout=2000, force=True)
                                    await nova.wait_for_timeout(3500)
                            except Exception:
                                continue
            # o que os players de todas as janelas (e dos iframes) têm como fonte
            for frame in [f for pg in ctx.pages[:4] for f in pg.frames[:8]]:
                try:
                    fontes = await frame.evaluate(
                        """() => [...document.querySelectorAll('audio,video,source')]
                              .map(e => [e.currentSrc || e.src || '', e.closest('[class],[id]') ?
                                         (e.closest('[class],[id]').innerText || '').slice(0, 200) : '',
                                         !!(e.currentTime > 0 && !e.paused)])""")
                    for src, texto, tocando in fontes:
                        anota(src, norm(texto), tocando)
                    html = await frame.content()
                    for u in extract_streams(html):
                        anota(u)
                except Exception:
                    continue
        finally:
            await ctx.close()
        # fora: blob:, o endereço do próprio site/página (um <audio> sem fonte devolve o endereço da página)
        paginas = {site.rstrip("/"), page.url.rstrip("/") if page else ""}
        origem = re.match(r"https?://[^/]+", site)
        if origem:
            paginas.add(origem.group(0))
        return [(u, c, t) for u, (c, t) in achados.items()
                if not u.startswith("blob:") and u.rstrip("/") not in paginas]


async def _toca_no_radiocol(self, url_post):
    """Abre o post no Radiocol, dá play no player do site e diz se o áudio TOCA de verdade (tempo andando).
    True = toca; False = não tocou; None = não deu para testar (navegador indisponível/erro)."""
    if not await self.abrir():
        return None
    ctx = page = None
    try:
        ctx = await self._browser.new_context(user_agent=config.USER_AGENT, locale="pt-BR",
                                              timezone_id="America/Sao_Paulo", ignore_https_errors=False)
        page = await ctx.new_page()
        await page.goto(url_post, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        for sel in (".mejs-playpause-button button", ".mejs-overlay-play", "#rc-janela button"):
            try:
                el = page.locator(sel).first
                if await el.count():
                    await el.click(timeout=2000, force=True)
                    break
            except Exception:
                pass
        await page.evaluate("""() => document.querySelectorAll('.post-body audio, .post-body video')
                                 .forEach(a => { try { a.muted = true; a.play(); } catch (e) {} })""")
        for _ in range(8):                               # até ~16 s esperando o áudio andar
            await page.wait_for_timeout(2000)
            andou = await page.evaluate("""() => Array.from(document.querySelectorAll('.post-body audio, .post-body video'))
                .some(a => !a.paused && a.currentTime > 3 && !a.hasAttribute('data-radiocol-sem-stream'))""")
            if andou:
                return True
        return False
    except Exception:
        return None
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass


Navegador.toca_no_radiocol = _toca_no_radiocol


async def _html(self, url, espera=2500):
    """Abre a página no navegador (como um visitante) e devolve o HTML já montado pelos scripts.
    Para diretórios que recusam leitura simples (HTTP 403/404 para robôs) ou montam a lista por script."""
    if self._html_usados >= config.DIRETORIO_NAV_MAX or not await self.abrir():
        return ""
    self._html_usados += 1
    ctx = None
    try:
        ctx = await self._browser.new_context(user_agent=config.USER_AGENT, locale="pt-BR",
                                              timezone_id="America/Sao_Paulo")
        page = await ctx.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,mp3,aac,m3u8}", lambda r: r.abort())
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(espera)
        return await page.content()
    except Exception:
        return ""
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass


Navegador._html_usados = 0
Navegador.html = _html

