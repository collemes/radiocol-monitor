"""Encontra endereços de streaming (e o site oficial) dentro do HTML de um post."""
import html
import re
import unicodedata
from urllib.parse import urlparse

URL_RE = re.compile(r"""https?://[^\s"'<>()\\\[\]{}|^`]+""", re.I)
AUDIO_TAG_RE = re.compile(
    r"""<(?:audio|source|video)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I
)
DATA_ATTR_RE = re.compile(
    r"""\bdata-(?:stream|src|url|audio|radio|link|file|mp3)\s*=\s*["']([^"']+)["']""", re.I
)
HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)

AUDIO_EXT_RE = re.compile(r"\.(mp3|aac|aacp|m3u8|m3u|pls|ogg|oga|opus|flac|ts)(?:[?#;]|$)", re.I)
STREAM_PATH_RE = re.compile(
    r"(/;|;stream|/stream\b|/live\b|/listen\b|/aovivo|/ao-vivo|/radio\b|/hls/|/icecast|/autodj|"
    r"/proxy/|/play\b|/;?stream\.(?:mp3|nsv)|/mount|/\d{4,5}/stream)",
    re.I,
)
STREAM_HOST_HINTS = (
    "stream", "shoutcast", "icecast", "srvstm", "zeno.fm", "streamtheworld", "audio",
    "cast", "radio", "hls", "player", "aovivo", "live", "ice", "sonic", "stm", "brlogic",
    "ssl", "fm",
)
# provedores de streaming conhecidos (vale como forte indício)
STRONG_HOSTS = (
    "zeno.fm", "streamtheworld.com", "srvstm.com", "brlogic.com", "shoutcast", "icecast",
    "radio.co", "cdnstream", "stmxp", "streaming.com.br", "sonicpanel", "radiojar.com",
    "mixlr.com", "listen2myradio", "caster.fm", "fastcast4u", "ipstm", "stream.laut.fm",
    "ice.infomaniak", "audio-stream", "hdstreaming", "conectastm", "virtualcast", "suaradionanet",
    "voxhd", "svr.br", "1stream", "streamguys",
)
# domínios que nunca são streams
BLOCK_HOSTS = (
    "blogger.com", "blogspot.com", "googleusercontent.com", "google.com", "gstatic.com",
    "googleapis.com", "youtube.com", "youtu.be", "facebook.com", "fb.com", "instagram.com",
    "twitter.com", "x.com", "tiktok.com", "whatsapp.com", "wa.me", "t.me", "linkedin.com",
    "pinterest.com", "flickr.com", "maps.app.goo.gl", "goo.gl", "bit.ly", "wikipedia.org",
    "apple.com", "play.google.com", "spotify.com", "deezer.com", "jsdelivr.net", "cloudflare.com",
    "w3.org", "schema.org", "gravatar.com", "radiocol.com.br", "addthis.com", "sharethis.com",
    "servidor.com", "example.com", "exemplo.com", "suaradio.com.br", "seusite.com.br",
)
BLOCK_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|bmp|css|js|json|xml|pdf|docx?|zip|rar|woff2?|ttf|php)(?:[?#]|$)",
    re.I,
)
SOCIAL_RE = re.compile(r"(facebook|instagram|twitter|youtube|tiktok|whatsapp|wa\.me|linkedin|t\.me)", re.I)


def _clean(url: str) -> str:
    url = html.unescape(url).strip().rstrip(".,)]'\"")
    return url


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _blocked(url):
    h = _host(url)
    return not h or any(h == b or h.endswith("." + b) for b in BLOCK_HOSTS)


def stream_score(url: str) -> int:
    """Pontua a chance de a URL ser um stream de áudio (0 = não é)."""
    if _blocked(url) or BLOCK_EXT_RE.search(url):
        return 0
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return 0
    path = p.path or "/"
    score = 0
    if AUDIO_EXT_RE.search(path):
        score += 6
    if STREAM_PATH_RE.search(path) or path.endswith(";"):
        score += 4
    if port and port not in (80, 443):
        score += 3
    h = (p.hostname or "").lower()
    if any(k in h for k in STRONG_HOSTS):
        score += 4
    elif any(k in h for k in STREAM_HOST_HINTS):
        score += 1
    if path.lower().endswith((".html", ".htm", ".asp", ".aspx")):
        score -= 5
    # só host com dica no nome, sem nada no caminho: fraco demais
    return score if score >= 3 else 0


def extract_streams(content: str):
    """Lista de URLs de streaming encontradas, da mais para a menos provável."""
    if not content:
        return []
    found = {}
    order = 0

    def add(u, bonus):
        nonlocal order
        u = _clean(u)
        if not u.lower().startswith("http"):
            return
        s = stream_score(u)
        if s == 0 and bonus < 5:
            return
        if _blocked(u):
            return
        s += bonus
        if u not in found or found[u][0] < s:
            found[u] = (s, found.get(u, (0, order))[1])
        order += 1

    for m in AUDIO_TAG_RE.finditer(content):
        add(m.group(1), 6)
    for m in DATA_ATTR_RE.finditer(content):
        add(m.group(1), 3)
    text = html.unescape(content)
    for m in URL_RE.finditer(text):
        add(m.group(0), 0)
    # streams "embutidos" em links de player (?url=https%3A%2F%2F...)
    from urllib.parse import unquote
    if "%3A%2F%2F" in text.upper():
        for m in URL_RE.finditer(unquote(text)):
            add(m.group(0), 0)
    ranked = sorted(found.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    return [u for u, _ in ranked]


IFRAME_TAG_RE = re.compile(r"<iframe\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""\b(title|src)\s*=\s*["']([^"']*)["']""", re.I)


def extract_live_iframe(content: str):
    """src do iframe de player antigo (title="Ao vivo"), se houver."""
    for tag in IFRAME_TAG_RE.findall(content or ""):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(tag)}
        if "ao vivo" in norm(attrs.get("title", "")) and attrs.get("src"):
            src = _clean(attrs["src"])
            if src.startswith("//"):
                src = "http:" + src
            return src
    return None


def extract_sites(content: str, limit=2):
    """Links para o site oficial da emissora (para procurar o player lá)."""
    sites = []
    for m in HREF_RE.finditer(content or ""):
        u = _clean(m.group(1))
        if not u.lower().startswith("http") or _blocked(u) or SOCIAL_RE.search(u):
            continue
        if stream_score(u) or BLOCK_EXT_RE.search(u):
            continue
        p = urlparse(u)
        root = f"{p.scheme}://{p.netloc}/"
        if root not in sites:
            sites.append(root)
        if len(sites) >= limit:
            break
    return sites


# ---------- nomes / frequências (para comparar com o Radio Browser) ----------
STOP = {
    "radio", "radios", "fm", "am", "web", "webradio", "ao", "vivo", "online", "ouvir", "ouca",
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "the", "mhz", "khz", "tv",
    "oficial", "net", "com", "br", "www", "hd",
}
UF = {
    "ac", "al", "ap", "am", "ba", "ce", "df", "es", "go", "ma", "mt", "ms", "mg", "pa", "pb",
    "pr", "pe", "pi", "rj", "rn", "rs", "ro", "rr", "sc", "sp", "se", "to",
}
FREQ_RE = re.compile(r"\b(\d{2,3})[.,](\d)\b")
AM_RE = re.compile(r"\b(\d{3,4})\s*(?:am|khz)\b", re.I)
AM_BEFORE_RE = re.compile(r"\bam\s*(\d{3,4})\b", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9.,]+", " ", s).strip()


def frequency(s: str):
    s2 = norm(s)
    m = FREQ_RE.search(s2)
    if m and 87 <= int(m.group(1)) <= 108:
        return f"{int(m.group(1))}.{m.group(2)}"
    for rx in (AM_RE, AM_BEFORE_RE):
        m = rx.search(s2)
        if m and 500 <= int(m.group(1)) <= 1800:
            return m.group(1)
    return None


def band(s: str):
    """'am', 'fm' ou None, conforme o nome."""
    t = f" {norm(s)} "
    if " am " in t:
        return "am"
    if " fm " in t:
        return "fm"
    f = frequency(s)
    return ("fm" if "." in f else "am") if f else None


def name_tokens(s: str):
    s2 = FREQ_RE.sub(" ", norm(s))
    s2 = re.sub(r"\b\d+\b", " ", s2)
    toks = [t for t in re.split(r"[\s.,]+", s2) if t and t not in STOP and len(t) > 1]
    # remove sigla de UF no final ("... - Coluna MG")
    while toks and toks[-1] in UF:
        toks.pop()
    return toks


# ---------- contatos (WhatsApp / telefone) ----------
WA_RE = re.compile(r"(?:wa\.me/|whatsapp\.com/send/?\?(?:[^\"'<>]*?&(?:amp;)?)?phone=|whatsapp://send\?phone=)\+?(\d[\d%\-\s().]{7,20}\d)", re.I)
TEL_RE = re.compile(r"""href\s*=\s*["']tel:([+\d\s\-().]{8,22})["']""", re.I)


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def same_number(a: str, b: str) -> bool:
    """Compara pelos 8 últimos dígitos + DDD quando houver (ignora +55 e o 9º dígito)."""
    a, b = digits(a), digits(b)
    if len(a) < 8 or len(b) < 8:
        return False
    if a[-8:] != b[-8:]:
        return False
    ddd_a = a[-11:-9] if len(a) >= 11 else a[-10:-8] if len(a) >= 10 else ""
    ddd_b = b[-11:-9] if len(b) >= 11 else b[-10:-8] if len(b) >= 10 else ""
    return not ddd_a or not ddd_b or ddd_a == ddd_b


def _uniq(nums):
    out = []
    for n in nums:
        if len(n) >= 8 and not any(same_number(n, o) for o in out):
            out.append(n)
    return out


def extract_contacts(content: str):
    text = html.unescape(content or "")
    whats = _uniq([digits(m) for m in WA_RE.findall(text)])
    phones = _uniq([digits(m) for m in TEL_RE.findall(text)])
    phones = [p for p in phones if not any(same_number(p, w) for w in whats)]
    return {"whatsapp": whats, "telefone": phones}


# ---------- players em popup / página de player ----------
WINOPEN_RE = re.compile(r"""window\.open\s*\(\s*['"]([^'"]+)['"]""", re.I)
JS_URL_RE = re.compile(r"""['"]((?:https?:)?//[^'"\s]+|/[^'"\s]+)['"]""")
TAG_RE = re.compile(r"<[^>]+>")
PLAYER_TEXT_RE = re.compile(r"(ouvir|ou[cç]a|escut|ao vivo|aovivo|player|tocar|play|clique aqui|abrir r[aá]dio)", re.I)
PLAYER_HREF_RE = re.compile(r"(player|popup|pop-up|aovivo|ao-vivo|ouvir|/play\b|radio\.php|tocar)", re.I)
A_TAG_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)


def extract_player_links(content: str, base: str, limit=3):
    """Links que abrem o player da rádio (popup, window.open, "Clique para ouvir ao vivo")."""
    from urllib.parse import urljoin
    out = []

    def add(u):
        u = _clean(u)
        if not u or u.startswith(("#", "mailto:", "tel:", "whatsapp:")):
            return
        if u.startswith("//"):
            u = "https:" + u
        u = urljoin(base, u)
        if not u.lower().startswith("http") or BLOCK_EXT_RE.search(u) or SOCIAL_RE.search(u):
            return
        if "/search/label/" in u or u.split("#")[0] == base.split("#")[0]:
            return
        if u not in out:
            out.append(u)

    text = content or ""
    for u in WINOPEN_RE.findall(text):
        add(u)
    for attrs, inner in A_TAG_RE.findall(text):
        attrs_u = html.unescape(attrs)
        label = TAG_RE.sub(" ", inner)
        href = re.search(r"""\bhref\s*=\s*(["'])(.*?)\1""", attrs_u, re.I | re.S)
        href = href.group(2).strip() if href else ""
        onclick = " ".join(m[1] for m in re.findall(r"""\bon\w+\s*=\s*(["'])(.*?)\1""", attrs_u, re.I | re.S))
        is_player = PLAYER_TEXT_RE.search(label) or PLAYER_HREF_RE.search(href) or "window.open" in onclick \
            or re.search(r"""target\s*=\s*["']_blank""", attrs_u, re.I) and PLAYER_TEXT_RE.search(label)
        if not is_player:
            continue
        if href.lower().startswith("javascript:"):
            for u in JS_URL_RE.findall(href):
                add(u)
        elif href:
            add(href)
        for u in JS_URL_RE.findall(onclick):
            add(u)
    return out[:limit]


# ---------- posts que não são de rádio / rádios encerradas ----------
NAO_RADIO_RE = re.compile(
    r"^(especial\b|playlist\b|clipes?\b|anuncie\b|consultar\b|curta a p[aá]gina|colocando r[aá]dio|"
    r"rela[cç][aã]o das r[aá]dios|cadastrar\b|radiocol\s*-|ou[cç]a a sele[cç][aã]o|ou[cç]a as melhores)"
    r"|melhores m[uú]sicas|m[uú]sicas mais tocadas",
    re.I)
ENCERRADA_RE = re.compile(r"(foi extint|encerrou suas atividades|saiu do ar|deixou de existir)", re.I)


def classify_post(title: str):
    """'nao_radio' (playlist, especial, página institucional), 'encerrada' ou None (post de rádio)."""
    t = (title or "").strip()
    if ENCERRADA_RE.search(t):
        return "encerrada"
    if NAO_RADIO_RE.search(t) and not re.search(r"\b(fm|am)\s*\d", t, re.I):
        return "nao_radio"
    return None
