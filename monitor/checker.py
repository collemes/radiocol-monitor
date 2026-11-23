"""Testa se um endereço de streaming está realmente entregando áudio."""
import asyncio
import re
import ssl
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import aiohttp

from . import config
from .extract import parece_stream, parece_video

PLAYLIST_CT = ("mpegurl", "x-scpls", "pls+xml")
AUDIO_CT = ("audio/", "application/ogg", "video/mp2t", "application/octet-stream", "video/mp4")


@dataclass
class Result:
    ok: bool
    reason: str = ""
    url: str = ""
    content_type: str = ""

    @property
    def http_only(self):
        return self.ok and self.url.lower().startswith("http://")


_MP3_BR = {  # kbps por (versão, layer) — só o necessário para medir o quadro
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],   # MPEG1 layer III
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],  # MPEG1 layer II
    (2, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],       # MPEG2/2.5 layer III/II
}
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _mp3_frame_len(b, i):
    if b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return 0
    ver, layer = (b[i + 1] >> 3) & 3, (b[i + 1] >> 1) & 3
    bri, sri, pad = b[i + 2] >> 4, (b[i + 2] >> 2) & 3, (b[i + 2] >> 1) & 1
    if ver == 1 or layer == 0 or bri in (0, 15) or sri == 3:
        return 0
    table = _MP3_BR.get((ver, layer)) or (_MP3_BR[(2, 1)] if ver in (0, 2) else None)
    if not table:
        return 0
    br, sr = table[bri] * 1000, _MP3_SR[ver][sri]
    coef = 144 if ver == 3 else 72
    return int(coef * br / sr) + pad


def _adts_frame_len(b, i):
    if b[i] != 0xFF or (b[i + 1] & 0xF6) != 0xF0 or ((b[i + 2] >> 2) & 0xF) > 12:
        return 0
    return ((b[i + 3] & 0x03) << 11) | (b[i + 4] << 3) | (b[i + 5] >> 5)


def _looks_audio(data: bytes) -> bool:
    """Reconhece áudio de verdade: exige DOIS quadros MP3/AAC seguidos e válidos
    (bytes soltos 0xFFEx aparecem em qualquer imagem ou arquivo binário)."""
    if data[:4] == b"OggS" or data[:4] == b"fLaC":
        return True
    if len(data) > 376 and data[0] == 0x47 and data[188] == 0x47:
        return True  # MPEG-TS
    start = 0
    if data[:3] == b"ID3" and len(data) > 10:  # pula a tag ID3
        start = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    end = min(len(data) - 8, start + 16384)
    for i in range(start, max(start, end)):
        for measure in (_mp3_frame_len, _adts_frame_len):
            n = measure(data, i)
            if n >= 7 and i + n + 6 < len(data) and measure(data, i + n):
                return True
    return False


def _is_html(data: bytes, ct: str) -> bool:
    head = data[:600].lstrip().lower()
    return "text/html" in ct or head.startswith((b"<!doctype", b"<html", b"<?xml", b"<head"))


async def _read_some(resp, limit):
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(4096):
        buf.extend(chunk)
        if len(buf) >= limit:
            break
    return bytes(buf)


def _parse_playlist(text: str, base: str):
    """Devolve (tipo, lista de URLs). tipo: master|media|simple."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    urls = []
    if any(l.upper().startswith("[PLAYLIST]") for l in lines[:3]):
        for l in lines:
            if l.lower().startswith("file") and "=" in l:
                urls.append(l.split("=", 1)[1].strip())
        return "simple", urls
    kind = "master" if any("#EXT-X-STREAM-INF" in l for l in lines) else (
        "media" if any(l.startswith("#EXTINF") or l.startswith("#EXT-X-TARGETDURATION") for l in lines)
        else "simple")
    for l in lines:
        if not l.startswith("#"):
            urls.append(urljoin(base, l))
    return kind, urls


class Checker:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.sem = asyncio.Semaphore(config.CONCURRENCY)
        self.host_sems = defaultdict(lambda: asyncio.Semaphore(config.PER_HOST))
        self.cache = {}

    async def check(self, url: str) -> Result:
        if not url:
            return Result(False, "sem endereço")
        if url in self.cache:
            return self.cache[url]
        try:
            hp = urlparse(url)
            host_key = f"{hp.hostname}:{hp.port}"
        except ValueError:
            host_key = url
        # primeiro espera a vez do servidor, depois uma vaga geral; só então o relógio começa
        async with self.host_sems[host_key], self.sem:
            try:
                r = await asyncio.wait_for(self._check(url, 0), config.READ_TIMEOUT + config.CONNECT_TIMEOUT + 8)
            except asyncio.TimeoutError:
                r = Result(False, "tempo esgotado", url)
            except Exception as e:  # nunca derruba a execução
                r = Result(False, f"erro: {type(e).__name__}", url)
        self.cache[url] = r
        return r

    async def _check(self, url, depth) -> Result:
        if depth > 3:
            return Result(False, "playlist em excesso de níveis", url)
        if not parece_stream(url):
            return Result(False, "não é endereço de transmissão (página de status, prévia de música ou arquivo)", url)
        if parece_video(url):
            # vinheta/propaganda em vídeo do site: tem áudio dentro, mas não é a rádio ao vivo
            return Result(False, "arquivo de vídeo/anúncio, não é a transmissão ao vivo", url)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=config.CONNECT_TIMEOUT,
                                        sock_read=config.READ_TIMEOUT)
        headers = {"User-Agent": config.USER_AGENT, "Icy-MetaData": "0", "Accept": "*/*",
                   "Referer": config.SITE_URL + "/"}  # como o player do site faria
        try:
            async with self.s.get(url, headers=headers, timeout=timeout, allow_redirects=True,
                                  max_redirects=6) as resp:
                ct = (resp.headers.get("Content-Type") or "").lower()
                final = str(resp.url)
                if resp.status >= 400:
                    return Result(False, f"HTTP {resp.status}", final, ct)
                # arquivo com tamanho fechado e sem cabeçalho de rádio (icy-*): é uma música/prévia (ex.: Spotify
                # p.scdn.co/mp3-preview, 30 s), não a transmissão ao vivo. HLS tem a sua própria verificação.
                tamanho = resp.headers.get("Content-Length")
                icy = any(k.lower().startswith("icy-") for k in resp.headers.keys())
                if (tamanho and tamanho.isdigit() and int(tamanho) < 25_000_000 and not icy
                        and ct.startswith("audio/") and not final.lower().split("?")[0].endswith((".m3u8", ".m3u", ".pls"))):
                    return Result(False, "arquivo de áudio de tamanho fechado (música/prévia), não é transmissão ao vivo",
                                  final, ct)
                data = await asyncio.wait_for(_read_some(resp, 32 * 1024), config.READ_TIMEOUT)
        except aiohttp.ClientConnectorCertificateError:
            return Result(False, "certificado SSL inválido", url)
        except aiohttp.ClientConnectorError as e:
            return Result(False, f"sem conexão ({e.os_error.__class__.__name__})", url)
        except (aiohttp.ClientResponseError, aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError) as e:
            # Shoutcast v1 responde "ICY 200 OK", que não é HTTP padrão
            return await self._raw_icy(url, str(e))
        except asyncio.TimeoutError:
            return Result(False, "tempo esgotado", url)

        lower = final.lower().split("?")[0]
        head = data[:64].lstrip()
        if (any(p in ct for p in PLAYLIST_CT) or lower.endswith((".m3u8", ".m3u", ".pls"))
                or head.startswith(b"#EXTM3U") or head.lower().startswith(b"[playlist]")):
            return await self._playlist(final, data, depth)
        if _is_html(data, ct):
            return Result(False, "responde página HTML, não áudio", final, ct)
        if ct.startswith(("image/", "text/", "application/json", "application/javascript", "font/")):
            return Result(False, f"não é áudio ({ct.split(';')[0]})", final, ct)
        if len(data) < config.MIN_AUDIO_BYTES:
            return Result(False, f"poucos dados ({len(data)} bytes)", final, ct)
        if ct.startswith(("audio/", "application/ogg", "video/mp2t")) or _looks_audio(data):
            return Result(True, "ok", final, ct)
        return Result(False, f"conteúdo não reconhecido ({ct or 'sem tipo'})", final, ct)

    async def _playlist(self, url, data, depth):
        text = data.decode("utf-8", "ignore")
        kind, urls = _parse_playlist(text, url)
        if not urls:
            return Result(False, "playlist vazia", url)
        if kind == "media":
            # HLS: basta o primeiro segmento responder
            seg = urls[-1]
            try:
                async with self.s.get(seg, headers={"User-Agent": config.USER_AGENT},
                                      timeout=aiohttp.ClientTimeout(total=config.READ_TIMEOUT + 5)) as r:
                    if r.status >= 400:
                        return Result(False, f"segmento HLS HTTP {r.status}", url)
                    chunk = await r.content.read(2048)
                    if not chunk:
                        return Result(False, "segmento HLS vazio", url)
            except Exception as e:
                return Result(False, f"segmento HLS falhou ({type(e).__name__})", url)
            return Result(True, "ok (HLS)", url, "application/vnd.apple.mpegurl")
        # master HLS ou .m3u/.pls simples: testa o primeiro item que funcionar
        last = None
        for u in urls[:3]:
            r = await self._check(u, depth + 1)
            if r.ok:
                # Para HLS master mantemos a URL da playlist (é ela que vai no player)
                return Result(True, r.reason, url if kind == "master" else r.url, r.content_type)
            last = r
        return Result(False, f"playlist sem item válido ({last.reason if last else ''})", url)

    async def _raw_icy(self, url, err):
        p = urlparse(url)
        host = p.hostname
        tls = p.scheme == "https"
        port = p.port or (443 if tls else 80)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        try:
            ctx = ssl.create_default_context() if tls else None
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ctx), config.CONNECT_TIMEOUT)
            writer.write((f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: {config.USER_AGENT}\r\n"
                          f"Referer: {config.SITE_URL}/\r\n"
                          "Icy-MetaData: 0\r\nAccept: */*\r\n\r\n").encode())
            await writer.drain()
            first = await asyncio.wait_for(reader.readline(), config.READ_TIMEOUT)
            if not (first.startswith(b"ICY 200") or (first.startswith(b"HTTP/") and b" 200" in first)):
                writer.close()
                return Result(False, f"resposta inválida ({first[:40]!r})", url)
            data = b""
            while len(data) < 32 * 1024:
                chunk = await asyncio.wait_for(reader.read(4096), config.READ_TIMEOUT)
                if not chunk:
                    break
                data += chunk
            writer.close()
            body = data.split(b"\r\n\r\n", 1)[-1]
            if _is_html(body, ""):
                return Result(False, "responde página HTML, não áudio", url)
            if len(body) >= config.MIN_AUDIO_BYTES:
                return Result(True, "ok (ICY)", url, "audio/mpeg")
            return Result(False, "poucos dados (ICY)", url)
        except Exception as e:
            return Result(False, f"erro de protocolo ({type(e).__name__})", url)
