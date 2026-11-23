"""Diagnóstico 2: estrutura do HTML de tudoradio e radios.com.br (impresso no log)."""
import asyncio, re, collections
import aiohttp
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
URLS = ["https://tudoradio.com/sitemap.xml", "https://tudoradio.com/robots.txt",
        "https://tudoradio.com/radios/estado/RJ?page=2", "https://tudoradio.com/radios/cidade/102-rio-de-janeiro",
        "https://tudoradio.com/dials/cidade/102-rio-de-janeiro/local", "https://tudoradio.com/player/radio/7266-953-fm",
        "https://www.radios.com.br/sitemap.xml", "https://www.radios.com.br/robots.txt",
        "https://www.radios.com.br/aovivo/radio-positividade-1045-fm/181824",
        "https://www.radios.com.br/radio/cidade/rio-de-janeiro/12339", "https://www.radios.com.br/lista/pais/brasil/33"]
def ctx(t, pat, n=4, w=220):
    out = []
    for m in list(re.finditer(pat, t, re.I))[:n]:
        out.append(re.sub(r"\s+", " ", t[max(0, m.start()-w): m.end()+w]))
    return out
async def main():
    async with aiohttp.ClientSession() as s:
        for u in URLS:
            try:
                async with s.get(u, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=40)) as r:
                    t = await r.text(errors="ignore")
            except Exception as e:
                print("##", u, "ERRO", e); continue
            print("\n########", u, len(t))
            if u.endswith((".xml", ".txt")):
                print(t[:3000]); continue
            hrefs = re.findall(r"""href=["']([^"'#]+)""", t)
            pref = collections.Counter(re.sub(r"(https?://[^/]+)?(/[^/?]+(/[^/?]+)?).*", r"\2", h) for h in hrefs)
            print("HREF prefixos:", pref.most_common(25))
            for p in (r"/player/radio/\d+", r"/aovivo/[^/]+/\d+", r"/radio/cidade/", r"/radios/cidade/", r"/lista/", r"page=", r"/dials/"):
                ex = sorted(set(h for h in hrefs if re.search(p, h)))
                print(f"  {p}: {len(ex)} ex: {ex[:6]}")
            print("TITLE:", ctx(t, r"<title", 1, 120))
            for pat in (r"104[.,]5", r"Positividade", r"\.m3u8|\.mp3|\.aac|/stream|;stream|:\d{4,5}/", r"stream_?url|streamUrl|\"url\"\s*:|data-stream|data-src|audio", r"Site\s*:", r"WhatsApp", r"Arquivad", r"__NEXT_DATA__|window\.__|application/ld\+json", r"api/|ajax|fetch\("):
                c = ctx(t, pat, 3, 160)
                if c: print(f"  [{pat}]", *["\n     · " + x for x in c])
asyncio.run(main())
