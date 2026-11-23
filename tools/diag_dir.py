import asyncio, re, collections
import aiohttp
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
async def get(s, u):
    try:
        async with s.get(u, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=40)) as r:
            return r.status, await r.text(errors="ignore")
    except Exception as e:
        return repr(e), ""
async def main():
    async with aiohttp.ClientSession() as s:
        for u in ["https://tudoradio.com/sitemaps/dials-cidades.xml", "https://tudoradio.com/sitemaps/dials-estados.xml",
                  "https://tudoradio.com/sitemaps/players.xml", "https://www.radios.com.br/sitemap_cidades1.xml",
                  "https://www.radios.com.br/sitemap_cidades2.xml", "https://www.radios.com.br/sitemap_cidades3.xml",
                  "https://www.radios.com.br/sitemap_estados.xml"]:
            st, t = await get(s, u)
            locs = re.findall(r"<loc>\s*([^<\s]+)", t)
            print(f"\n## {u} status={st} len={len(t)} locs={len(locs)}")
            print("   primeiros:", locs[:5]); print("   últimos:", locs[-3:])
            print("   rio:", [l for l in locs if re.search(r"rio-de-janeiro|ribeirao-preto|tubarao|cajuru", l)][:8])
            print("   início do arquivo:", re.sub(r"\s+", " ", t[:400]))
        for u in ["https://tudoradio.com/dials/estado/RJ", "https://tudoradio.com/dials/estado/rj", "https://www.radios.com.br/lista/uf/rio-de-janeiro/19",
                  "https://www.radios.com.br/radio/uf/rio-de-janeiro/19"]:
            st, t = await get(s, u)
            links = re.findall(r"""href=["']([^"']+)""", t)
            cid = sorted(set(l for l in links if re.search(r"/(dials|radio)/cidade/", l)))
            print(f"\n## {u} status={st} len={len(t)} links cidade={len(cid)} ex={cid[:5]}")
            print("   pg:", sorted(set(l for l in links if re.search(r"[?&](pg|page)=", l)))[:6])
            print("   rio:", [l for l in cid if "rio-de-janeiro" in l or "rio-das-ostras" in l][:5])
            print("   outros prefixos:", collections.Counter(re.sub(r"(https?://[^/]+)?(/[^/?]+(/[^/?]+)?).*", r"\2", h) for h in links).most_common(12))
asyncio.run(main())
