"""Diagnóstico: consulta real dos diretórios (tudoradio e radios.com.br) para alguns posts, sem mexer na base."""
import asyncio, json, os, sys, tempfile
sys.path.insert(0, os.getcwd())
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import aiohttp
from monitor import config
config.DATA_DIR = os.environ["DATA_DIR"]
from monitor.checker import Checker
from monitor.finder import Finder
from monitor.diretorios import Diretorios
POSTS = [
    ("Ouvir a Rádio Família FM 104,5 do Rio de Janeiro RJ Ao Vivo e Online", "Rio de Janeiro", "rj"),
    ("Ouvir a Rádio Fanática FM 104,5 do Rio de Janeiro RJ Ao Vivo e Online", "Rio de Janeiro", "rj"),
    ("Ouvir a Rádio Atlântica FM 93,7 de Rio das Ostras RJ Ao Vivo e Online", "Rio das Ostras", "rj"),
    ("Ouvir a Rádio Band FM 96,7 de Ribeirão Preto SP Ao Vivo e Online", "Ribeirão Preto", "sp"),
    ("Ouvir a Rádio Cidade AM 890 de Matão SP Ao Vivo e Online", "Matão", "sp"),
    ("Ouvir a Rádio Santa Bárbara FM 95,9 de Santa Bárbara d'Oeste SP Ao Vivo e Online", "Santa Bárbara d'Oeste", "sp"),
    ("Ouvir a Rádio Tubá FM 104,9 de Tubarão SC Ao Vivo e Online", "Tubarão", "sc"),
    ("Ouvir a Rádio Cajuru Web de Cajuru SP ao Vivo e Online", "Cajuru", "sp"),
]
async def main():
    async with aiohttp.ClientSession() as s:
        c = Checker(s); f = Finder(s, c); f.rede = set()
        f.dirs = Diretorios(f._get_text, print)
        await f.dirs.preparar()
        for t, cid, uf in POSTS:
            rec = {"title": t, "loc": [cid, uf], "url": "x"}
            tr, rcb = await f.dirs.para(rec)
            r = await f.from_tudoradio(rec)
            print(f"\n== {t}\n   tudoradio: {[(v['nome'], v['freq'], v['streams'][:1]) for v in tr['radios'].values() if v.get('freq') == (None if 'Web' in t else v.get('freq'))][:0] or len(tr['radios'])} emissoras da cidade"
                  f" | radios.com.br candidatas: {[(v['nome'], v.get('arquivada')) for v in rcb['radios'].values()]}"
                  f"\n   ACHADO: {r and (r['source'], r['url'], r.get('site'), r.get('whatsapp'))}")
        print("\n" + f.dirs.resumo())
        await f.nav.fechar()
asyncio.run(main())
