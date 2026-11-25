"""Diagnóstico: servidores com mais rádios fora do ar (kshost, play.radio.br, azioncdn, fabricahost, maxcast)."""
import asyncio, os, re, socket, sys
sys.path.insert(0, os.getcwd())
import aiohttp
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

DNS = ["player-ssl.kshost.com.br", "player.kshost.com.br", "kshost.com.br", "www.kshost.com.br", "hts01.kshost.com.br",
       "hts02.kshost.com.br", "ice01.kshost.com.br", "player-br.kshost.com.br", "stream.kshost.com.br",
       "stream.live.novotempo.com", "live.novotempo.com", "www.radioaovivo.online", "play.radio.br", "stream1.play.radio.br"]

URLS = [
    "https://play.radio.br:8089/sarandi/sarandi/playlist.m3u8",
    "https://play.radio.br:8089/sarandi/sarandi/icecast.audio",
    "https://play.radio.br:8089/sarandi/sarandi.stream/playlist.m3u8",
    "https://play.radio.br/sarandi/sarandi/playlist.m3u8",
    "https://play.radio.br:8443/sarandi/sarandi/playlist.m3u8",
    "https://stream1.play.radio.br:8089/Solaris97/Solaris97/playlist.m3u8",
    "https://www.tuaradio.com.br/Sarandi/",
    "https://14398c.ha.azioncdn.net/primary/sc_criciuma.sdp/playlist.m3u8",
    "https://redealeluia.com.br/",
    "https://ice.fabricahost.com.br/status-json.xsl",
    "https://ice.fabricahost.com.br/difusorathathi",
    "https://difusora.thathi.com.br/",
    "https://s20.maxcast.com.br:8036/live",
    "http://s20.maxcast.com.br:8036/live",
    "https://s20.maxcast.com.br/8036/stream",
    "http://www.audiorock.net/",
    "https://www.radioconquistar.com/",
    "https://www.californiawebradio.net/",
    "https://cast.youngtech.radio.br/api/stations",
    "https://5a2b083e9f360.streamlock.net/stereosomhd/stereosomhd.stream/playlist.m3u8",
]
ST_RE = re.compile(r"https?://[^\"'\s<>\\]*(?:stream|:\d{4}|\.m3u8|\.mp3|\.aac|/live|listen|kshost|maxcast|azion|play\.radio\.br|fabricahost|sonic|zeno)[^\"'\s<>\\]*", re.I)


async def main():
    for h in DNS:
        try:
            print("DNS", h, sorted({a[4][0] for a in socket.getaddrinfo(h, 443)})[:3])
        except Exception as e:
            print("DNS", h, "ERRO", e)
    async with aiohttp.ClientSession() as s:
        for u in URLS:
            try:
                async with s.get(u, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=15), ssl=False) as r:
                    ct = r.headers.get("content-type", "")
                    if "html" in ct or "json" in ct or "text" in ct or "mpegurl" in ct:
                        b = (await r.content.read(600_000)).decode("utf-8", "replace")
                        print("GET", u, r.status, ct, str(r.url)[:120], "|", b[:300].replace("\n", " ") if "mpegurl" in ct or "json" in ct else "")
                        print("    streams:", sorted(set(ST_RE.findall(b)))[:25])
                    else:
                        b = await r.content.read(3000)
                        print("GET", u, r.status, ct, len(b), b[:20])
            except Exception as e:
                print("GET", u, "ERRO", repr(e)[:200])

asyncio.run(main())
