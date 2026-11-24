"""Diagnóstico: o player de um post toca no navegador? (e o que o endereço de áudio responde)"""
import asyncio, os, re, sys
sys.path.insert(0, os.getcwd())
import aiohttp
URLS = ["https://radio.saopaulo01.com.br/9704/stream", "http://stm7.painelvox.com:8290/;"]
POST = "https://www.radiocol.com.br/2020/02/ouvir-rede-sim-fm-1053-de-santa-teresa-es-ao-vivo.html"
SITE = "https://www.simnoticias.com.br/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
async def main():
    async with aiohttp.ClientSession() as s:
        for u in URLS:
            try:
                async with s.get(u, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    b = await r.content.read(4096)
                    print("##", u, r.status, dict(r.headers), b[:40])
            except Exception as e:
                print("##", u, "ERRO", repr(e))
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        br = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        pg = await br.new_page(user_agent=UA)
        log = []
        pg.on("console", lambda m: log.append(m.text))
        pg.on("requestfailed", lambda rq: log.append("FALHOU " + rq.url + " " + str(rq.failure)))
        pg.on("response", lambda rs: log.append(f"RESP {rs.status} {rs.url[:120]} {rs.headers.get('content-type')}") if re.search(r"stream|saopaulo01|painelvox|/r/|\.json", rs.url) else None)
        await pg.goto(POST, wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(6000)
        info = await pg.evaluate("""() => [...document.querySelectorAll('audio,iframe,video')].map(a => ({tag:a.tagName, src:a.currentSrc||a.src, srcs:[...a.querySelectorAll('source')].map(x=>x.src), paused:a.paused, t:a.currentTime, err:a.error&&a.error.code, rs:a.readyState, ns:a.networkState}))""")
        print("ANTES DO PLAY:", info)
        await pg.evaluate("""() => { const a=document.querySelector('.post-body audio'); if(a){ a.muted=true; a.play().catch(e=>console.log('play erro '+e)); } }""")
        await pg.wait_for_timeout(15000)
        info = await pg.evaluate("""() => [...document.querySelectorAll('audio')].map(a => ({src:a.currentSrc||a.src, paused:a.paused, t:a.currentTime, err:a.error&&(a.error.code+' '+a.error.message), rs:a.readyState, ns:a.networkState}))""")
        print("DEPOIS DO PLAY:", info)
        print("\n".join(log[-40:]))
        # o mesmo endereço num <audio> limpo
        for u in URLS[:1]:
            await pg.set_content(f'<audio id=a src="{u}" muted autoplay></audio>')
            await pg.wait_for_timeout(12000)
            print("AUDIO LIMPO", u, await pg.evaluate("() => {const a=document.getElementById('a'); return {t:a.currentTime, err:a.error&&(a.error.code+' '+a.error.message), rs:a.readyState}}"))
        # o que o site da rádio toca
        media = []
        pg2 = await br.new_page(user_agent=UA)
        pg2.on("request", lambda rq: media.append(rq.url) if rq.resource_type == "media" or re.search(r"stream|\.mp3|\.aac|m3u8|:\d{4}/", rq.url) else None)
        try:
            await pg2.goto(SITE, wait_until="domcontentloaded", timeout=45000); await pg2.wait_for_timeout(5000)
            for sel in ["[class*=play]", "button[aria-label*=lay i]", "a:has-text('Ao vivo')", "a:has-text('Ouça')"]:
                try:
                    el = pg2.locator(sel).first
                    if await el.count(): await el.click(timeout=3000); await pg2.wait_for_timeout(6000)
                except Exception: pass
            h = await pg2.content()
            print("SITE: mídia", media[:15]); print("SITE links de stream no HTML:", sorted(set(re.findall(r"https?://[^\"'\s<>]*(?:stream|:\d{4})[^\"'\s<>]*", h)))[:15])
        except Exception as e:
            print("SITE erro", e)
        await br.close()
asyncio.run(main())
