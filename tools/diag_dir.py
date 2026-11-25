"""Diagnóstico: servidores com mais rádios fora do ar (kshost, play.radio.br, azioncdn, fabricahost, maxcast)."""
import asyncio, os, re, socket, sys
sys.path.insert(0, os.getcwd())
import aiohttp
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

DNS = ["play.radio.br", "painel.sintonizar.tv.br", "stream1.play.radio.br", "sintonizar.tv.br", "s20.maxcast.com.br",
       "14398c.ha.azioncdn.net"]

NOMES = ["saofrancisco", "sarandi", "cacique", "Solaris97", "mncaxias", "RSCultura", "globofeira", "aurora", "rosario"]
URLS = [f"https://painel.sintonizar.tv.br/stream/{n}" for n in NOMES] + [
    "https://painel.sintonizar.tv.br/", "https://painel.sintonizar.tv.br/api/stations",
    "https://www.tuaradio.com.br/", "https://play.radio.br:8089/",
    "https://www.redealeluia.com.br/radios", "https://redealeluia.com.br/aovivo", "https://www.novotempo.com/radio/",
    "https://s20.maxcast.com.br/", "https://www.maxcast.com.br/",
    "https://www.radiosolaris.com.br/", "https://www.maisnova.com.br/",
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
                        print("GET", u, r.status, ct, str(r.url)[:120], "|", b[:700].replace("\n", " ") if "mpegurl" in ct or "json" in ct else "")
                        print("    streams:", sorted(set(ST_RE.findall(b)))[:60])
                    else:
                        b = await r.content.read(3000)
                        print("GET", u, r.status, ct, len(b), b[:20])
            except Exception as e:
                print("GET", u, "ERRO", repr(e)[:200])

asyncio.run(main())
