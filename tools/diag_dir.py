"""Diagnóstico: por que o robô não acha endereço novo para as rádios fora do ar nos servidores com mais casos
(ciclano, fabricahost, azioncdn/Rede Aleluia, paineldj, brlogic, streamlock). Roda o próprio Finder em cada uma e
mostra o que o site da emissora entrega e as listas dos servidores."""
import asyncio, json, os, sys
sys.path.insert(0, os.getcwd())
import aiohttp
from monitor import config
from monitor.checker import Checker
from monitor.finder import Finder, variants

ALVOS = ("ciclano.io", "ice.fabricahost.com.br", "azioncdn.net", "paineldj.com.br", "brlogic.com",
         "5a2b083e9f360.streamlock.net", "brasilstream.com.br", "srvstm.com", "painelvox.com")

LISTAS = ["https://station.azrmcsoftbroadcast.com.br/api/stations",
          "https://ice.fabricahost.com.br/status-json.xsl", "https://play.wisestream.io/status-json.xsl",
          "https://r13.ciclano.io/", "https://r13.ciclano.io/api/stations", "https://r13.ciclano.io/status-json.xsl",
          "https://r10.ciclano.io/proxy/rdocolinafm/stream", "https://live.paineldj.com.br/",
          "https://live.paineldj.com.br/status-json.xsl", "https://servidor32.brlogic.com:8184/status-json.xsl",
          "https://servidor32.brlogic.com:8184/7.html"]


async def main():
    s = json.load(open("data/state.json"))["posts"]
    recs = [r for r in s.values() if r.get("status") == "off" and any(a in (r.get("stream") or "") for a in ALVOS)]
    connector = aiohttp.TCPConnector(limit=60, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        checker = Checker(session)
        f = Finder(session, checker)
        for u in LISTAS:
            txt = await f._get_text(u, limit=3_000_000)
            print("LISTA", u, len(txt), f.erros_texto.get(u, ""))
            if txt:
                try:
                    j = json.loads(txt)
                    if isinstance(j, list):
                        for st in j:
                            print("   st", st.get("shortcode"), "|", st.get("name"), "|", st.get("listen_url"))
                    else:
                        src = (j.get("icestats") or {}).get("source") or []
                        for so in ([src] if isinstance(src, dict) else src):
                            print("   mt", so.get("listenurl"), "|", so.get("server_name"))
                except Exception:
                    print("   ", txt[:600].replace("\n", " "))
        for r in recs[:45]:
            print("\n==", r["title"][:70], "|", r.get("stream"), "|", r.get("reason"), "| sites", r.get("sites"))
            for v in variants(r["stream"])[:6]:
                res = await checker.check(v)
                print("   var", v, res.ok, res.reason)
            for site in (r.get("sites") or [])[:1]:
                cands = await f._site_candidates(site)
                print("   site", site, len(cands), "candidatos", f.erros_texto.get(site, ""))
                for u, ctx in cands[:12]:
                    res = await checker.check(u)
                    print("     cand", u, res.ok, res.reason, "|", ctx[:80])
            try:
                achado = await asyncio.wait_for(f.find(dict(r)), 240)
            except Exception as ex:
                achado = repr(ex)
            print("   FIND ->", achado)
        await f.nav.fechar()

asyncio.run(main())
