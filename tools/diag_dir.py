"""Diagnóstico: como tudoradio e radios.com.br respondem ao robô no GitHub (leitura simples e navegador)."""
import asyncio, json, os, re, sys
import aiohttp
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
URLS = [
    "https://tudoradio.com/radios/estado/RJ", "https://tudoradio.com/radios/estado/RJ?page=2",
    "https://tudoradio.com/radios/cidade/102-rio-de-janeiro", "https://tudoradio.com/dials/cidade/102-rio-de-janeiro/local",
    "https://tudoradio.com/player/radio/7266-953-fm", "https://tudoradio.com/player/radio/7266",
    "https://tudoradio.com/sitemap.xml", "https://tudoradio.com/robots.txt",
    "https://www.radios.com.br/aovivo/radio-positividade-1045-fm/181824", "https://www.radios.com.br/aovivo/x/181824",
    "https://www.radios.com.br/radio/cidade/rio-de-janeiro/12339", "https://www.radios.com.br/lista/pais/brasil/33",
    "https://www.radios.com.br/robots.txt", "https://www.radios.com.br/sitemap.xml",
]
os.makedirs("diag", exist_ok=True)
def nome(u): return re.sub(r"[^a-z0-9]+", "_", u.lower())[8:90]
async def main():
    out = {}
    async with aiohttp.ClientSession() as s:
        for u in URLS:
            try:
                async with s.get(u, headers={"User-Agent": UA, "Accept-Language": "pt-BR"}, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    t = await r.text(errors="ignore")
                    out[u] = {"status": r.status, "final": str(r.url), "len": len(t)}
                    open(f"diag/static_{nome(u)}.html", "w").write(t[:3_000_000])
            except Exception as e:
                out[u] = {"erro": repr(e)}
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(user_agent=UA, locale="pt-BR")
        for u in URLS[:6] + URLS[8:12]:
            pg = await ctx.new_page(); media = []
            pg.on("request", lambda rq: media.append(rq.url) if rq.resource_type == "media" or re.search(r"\.(mp3|aac|m3u8)|stream|/live|:\d{4}", rq.url) else None)
            try:
                resp = await pg.goto(u, wait_until="domcontentloaded", timeout=40000)
                await pg.wait_for_timeout(6000)
                for sel in ["button[aria-label*=lay i]", ".play", "#play", "[class*=play]"]:
                    try:
                        el = pg.locator(sel).first
                        if await el.count(): await el.click(timeout=2000); await pg.wait_for_timeout(5000); break
                    except Exception: pass
                h = await pg.content()
                out["NAV " + u] = {"status": resp.status if resp else None, "len": len(h), "title": await pg.title(), "media": media[:15],
                                   "links_player": len(set(re.findall(r"/player/radio/\d+[^\"'#?]*", h))),
                                   "links_aovivo": len(set(re.findall(r"/aovivo/[^\"'#?]+/\d+", h)))}
                open(f"diag/nav_{nome(u)}.html", "w").write(h[:3_000_000])
            except Exception as e:
                out["NAV " + u] = {"erro": repr(e)}
            await pg.close()
        await b.close()
    json.dump(out, open("diag/resumo.json", "w"), indent=1, ensure_ascii=False)
    print(json.dumps(out, indent=1, ensure_ascii=False))
asyncio.run(main())
