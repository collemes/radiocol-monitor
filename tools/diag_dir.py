import asyncio, os, re, sys
sys.path.insert(0, os.getcwd())
import aiohttp
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PL = "https://radiomassa.simnoticias.com.br/"
async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get(PL, headers={"User-Agent": UA}) as r:
            h = await r.text()
        print("STATIC len", len(h), sorted(set(re.findall(r"https?://[^\"'\s<>\\]*(?:stream|:\d{4}|\.m3u8|\.mp3|\.aac|/live|listen|radio\.|sonic|zeno|painel)[^\"'\s<>\\]*", h, re.I)))[:30])
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        br = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(user_agent=UA)
        resps = []
        ctx.on("response", lambda rs: resps.append((rs.status, rs.headers.get("content-type", ""), rs.url[:200])) if re.search(r"audio|mpegurl|mp2t|octet", rs.headers.get("content-type", "")) or rs.request.resource_type == "media" else None)
        pg = await ctx.new_page()
        await pg.goto(PL, wait_until="domcontentloaded", timeout=45000); await pg.wait_for_timeout(5000)
        for sel in ["[class*=play]", "button[aria-label*=lay i]", "[id*=play]", "button", "audio"]:
            try:
                el = pg.locator(sel).first
                if await el.count():
                    await el.click(timeout=3000); await pg.wait_for_timeout(8000); break
            except Exception:
                pass
        print("MÍDIA:", resps[:20])
        print("AUDIO:", await pg.evaluate("() => [...document.querySelectorAll('audio,video,source,iframe')].map(a=>[a.tagName,a.currentSrc||a.src,a.paused,a.currentTime])"))
        async def pr(u):
            async with aiohttp.ClientSession() as s:
                try:
                    async with s.get(u, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        b = await r.content.read(2000); return (r.status, r.headers.get("content-type"), b[:50])
                except Exception as e:
                    return repr(e)
        for st, ct, u in resps[:3]:
            print("TESTE", u, await pr(u))
        await br.close()
asyncio.run(main())
