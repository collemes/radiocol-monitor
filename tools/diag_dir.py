import asyncio, os, re, sys, json
sys.path.insert(0, os.getcwd())
import aiohttp
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SITES = ["https://www.simnoticias.com.br/", "https://www.radios.com.br/busca/?q=massa+fm+santa+teresa"]
async def main():
    async with aiohttp.ClientSession() as s:
        for u in ["https://tudoradio.com/sitemaps/dials-cidades.xml"]:
            async with s.get(u, headers={"User-Agent": UA}) as r:
                t = await r.text()
            st = [l for l in re.findall(r"<loc>([^<]+)", t) if "santa-teresa" in l]
            print("tudoradio santa teresa:", st)
            for d in st:
                async with s.get(d, headers={"User-Agent": UA}) as r:
                    h = (await r.text()).replace('\\"', '"')
                for m in re.finditer(r'\{"id":"?\d+"?,"nome":"[^"]*","estacao":"?105\.3', h):
                    print("   ", h[m.start(): m.start() + 900])
        async with s.get("https://www.radios.com.br/sitemap_cidades1.xml", headers={"User-Agent": UA}) as r:
            t = await r.text()
        for c in [l for l in re.findall(r"<loc>([^<]+)", t) if "/santa-teresa/" in l]:
            async with s.get(c, headers={"User-Agent": UA}) as r:
                h = await r.text()
            for link, nome in re.findall(r'<h3>\s*<a href="(https://www.radios.com.br/aovivo/[^"]+)"[^>]*>(.*?)</a>', h):
                if "105" in nome or "sim" in nome.lower() or "massa" in nome.lower():
                    async with s.get(link, headers={"User-Agent": UA}) as r2:
                        p = await r2.text()
                    print("RCB", c, nome, link, re.findall(r"MRP\.insert\(\{[^}]*\}", p)[:3], re.findall(r"<title>(.*?)</title>", p), "Arquivada" in p)
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        br = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(user_agent=UA)
        reqs = []
        ctx.on("request", lambda rq: reqs.append((rq.resource_type, rq.url[:200])))
        resps = []
        ctx.on("response", lambda rs: resps.append((rs.status, rs.headers.get("content-type", ""), rs.url[:200])) if re.search(r"audio|mpegurl|octet|mp2t|aac|mpeg", rs.headers.get("content-type", "")) else None)
        pg = await ctx.new_page()
        await pg.goto(SITES[0], wait_until="domcontentloaded", timeout=45000); await pg.wait_for_timeout(5000)
        for sel in ["[class*=play]", "button[aria-label*=lay i]", "[id*=play]", "a:has-text('Ao vivo')", "a:has-text('Rádio')", "a:has-text('Ouça')"]:
            try:
                el = pg.locator(sel).first
                if await el.count():
                    await el.click(timeout=3000); await pg.wait_for_timeout(7000)
            except Exception as e:
                pass
        print("PÁGINAS:", [x.url for x in ctx.pages])
        print("RESPOSTAS DE MÍDIA:", resps[:20])
        print("REQ não-imagem:", [r for r in reqs if r[0] not in ("image", "font", "stylesheet") and not re.search(r"google|facebook|doubleclick|gstatic|analytics|clarity|hotjar", r[1])][:60])
        h = await pg.content()
        print("players no HTML:", sorted(set(re.findall(r"(?:src|href|data-[a-z-]+)=[\"']([^\"']*(?:radio|player|stream|ao-?vivo|live)[^\"']*)", h, re.I)))[:30])
        await br.close()
asyncio.run(main())
