"""Teste ponta a ponta com um "Blogger" e emissoras simuladas em localhost.
Uso: python tests/run_local.py"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp()
P = 8765          # site/feed/streams
ICY = 8766        # servidor Shoutcast v1 (ICY 200 OK)
TLS = 8767        # servidor https (certificado de teste)
CERT = os.path.join(TMP, "c.pem")
KEY = os.path.join(TMP, "k.pem")
os.system(f'openssl req -x509 -newkey rsa:2048 -nodes -keyout {KEY} -out {CERT} -days 2 '
          f'-subj "/CN=127.0.0.1" -addext "subjectAltName=IP:127.0.0.1" 2>/dev/null')
os.system(f"cat {CERT} /etc/ssl/certs/ca-certificates.crt > {TMP}/bundle.pem")
os.environ["SSL_CERT_FILE"] = f"{TMP}/bundle.pem"
os.environ.update(
    SITE_URL=f"http://127.0.0.1:{P}", DATA_DIR=f"{TMP}/data", PUBLIC_DIR=f"{TMP}/docs",
    DRY_RUN_EMAIL="1", CONFIRM_RUNS="2", READ_TIMEOUT="4", CONNECT_TIMEOUT="3",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

from monitor import finder, run  # noqa: E402

AUDIO = b"ID3\x03\x00\x00\x00\x00\x00\x00" + (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 60
B = f"http://127.0.0.1:{P}"
STATE = {"dead_fixed": False}
WHATS, WHATS_AUTO = [], []


def entry(i, title, content, labels=()):
    return {"id": {"$t": f"tag:blogger.com,1999:blog-1.post-{i}"},
            "title": {"$t": title}, "content": {"$t": content},
            "updated": {"$t": "2026-11-01T00:00:00Z"},
            "category": [{"term": l} for l in labels],
            "link": [{"rel": "alternate", "href": f"https://www.radiocol.com.br/2026/11/post-{i}.html"}]}


POSTS = [
    entry(1, "Rádio Boa FM 99.1", f'<audio controls src="{B}/ok.mp3"></audio> <a href="tel:+558434210000">(84) 3421-0000</a>'),
    entry(2, "Rádio Site FM", f'<audio src="{B}/morto.mp3"></audio> <a href="http://localhost:{P}/index.html">Site oficial</a>'
                              f' <a href="https://wa.me/5584999887766">WhatsApp</a>'),
    entry(3, "Rádio Html FM", f'Ouça: {B}/pagina-html/stream'),
    entry(4, "Rádio Shout FM", f'<a data-stream="http://127.0.0.1:{ICY}/;">play</a>'),
    entry(5, "Rádio HLS", f'<source src="{B}/hls/master.m3u8">'),
    entry(6, "Rádio Só Na Página", "texto sem link no feed"),
    entry(7, "Rádio Sem Nada", "nenhum link aqui"),
    entry(8, "Rádio Coluna FM 87.9 - Coluna MG", f'<audio src="{B}/coluna-morta.mp3"></audio>', ["Minas Gerais"]),
    entry(9, "Rádio Desconhecida XYZ", f'<audio src="{B}/xyz-morta.mp3"></audio>'),
    entry(10, "Rádio Iframe Antiga", f'Gênero: Sertanejo<br><iframe title="Ao vivo" src="{B}/player-antigo.html"></iframe>'),
    entry(11, "Rádio Iframe Só Http", f'Gênero: Gospel<br><iframe width="300" title="AO VIVO" '
              f'src="{B}/player?url=http%3A%2F%2F127.0.0.1%3A{P}%2Fsohttp.mp3"></iframe>'),
]


async def feed(req):
    start = int(req.query.get("start-index", 1))
    n = int(req.query.get("max-results", 150))
    n = min(n, 4)  # força paginação no teste
    return web.json_response({"feed": {"openSearch$totalResults": {"$t": str(len(POSTS))},
                                       "entry": POSTS[start - 1:start - 1 + n]}})


async def audio(req):
    resp = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
    await resp.prepare(req)
    for _ in range(4):
        await resp.write(AUDIO)
        await asyncio.sleep(0.05)
    return resp


async def html_page(req):
    return web.Response(text="<!doctype html><html><body>Página</body></html>", content_type="text/html")


async def site(req):  # site oficial da rádio 2, com player em iframe e WhatsApp novo
    return web.Response(text=f'<html><iframe src="/player-frame"></iframe>'
                             f'<a href="https://api.whatsapp.com/send?phone=5584977776666">Zap</a></html>',
                        content_type="text/html")


async def player_antigo(req):
    return web.Response(text=f'<audio src="https://127.0.0.1:{TLS}/seguro.mp3"></audio>', content_type="text/html")


async def player_q(req):
    return web.Response(text="<html>player</html>", content_type="text/html")


async def frame(req):
    return web.Response(text=f'<audio src="{B}/novo-site.mp3"></audio>', content_type="text/html")


async def master(req):
    return web.Response(text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nmedia.m3u8\n",
                        content_type="application/vnd.apple.mpegurl")


async def media(req):
    return web.Response(text="#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg1.aac\n",
                        content_type="application/vnd.apple.mpegurl")


async def seg(req):
    return web.Response(body=AUDIO, content_type="audio/aac")


async def post_page(req):  # plano B para o post 6 + URLs antigas
    if req.match_info["n"] == "1":   # URL antiga do post 1, com redirecionamento personalizado
        raise web.HTTPMovedPermanently("/2026/11/post-1-nova.html")
    if req.match_info["n"] == "9":   # post 9 apagado, sem redirecionamento
        raise web.HTTPNotFound()
    if req.match_info["n"] == "6":
        return web.Response(text=f'<div class="post-body"><audio src="{B}/pagina.mp3"></audio></div>'
                                 f'<div class="post-footer"></div>', content_type="text/html")
    return web.Response(text='<div class="post-body">nada</div><div class="post-footer"></div>',
                        content_type="text/html")


async def rb(req):
    name = req.query.get("name", "")
    if "coluna" in name:
        return web.json_response([
            {"stationuuid": "a", "name": "Rádio Coluna AM 1450", "url_resolved": f"{B}/errada.mp3", "state": ""},
            {"stationuuid": "b", "name": "Radio Coluna FM 87.9", "url_resolved": f"{B}/coluna-nova.mp3",
             "state": "Minas Gerais"},
        ])
    if "xyz" in name or "desconhecida" in name:
        return web.json_response([{"stationuuid": "c", "name": "XYZ Web Rádio Gospel", "url_resolved": f"{B}/xyz-sug.mp3"}])
    return web.json_response([])


async def icy_server(reader, writer):
    try:
        await reader.readuntil(b"\r\n\r\n")
    except Exception:  # ex.: tentativa https (TLS) no servidor http
        writer.close()
        return
    writer.write(b"ICY 200 OK\r\nicy-name: Shout\r\nContent-Type: audio/mpeg\r\n\r\n" + AUDIO * 2)
    await writer.drain()
    writer.close()


async def main():
    app = web.Application()
    app.router.add_get("/feeds/posts/default", feed)
    app.router.add_get("/player-antigo.html", player_antigo)
    app.router.add_get("/player", player_q)
    for p in ("/sohttp.mp3", "/seguro.mp3", "/ok.mp3", "/novo-site.mp3", "/pagina.mp3", "/coluna-nova.mp3", "/errada.mp3", "/xyz-sug.mp3"):
        app.router.add_get(p, audio)
    app.router.add_get("/pagina-html/stream", html_page)
    app.router.add_get("/", site)
    app.router.add_get("/player-frame", frame)
    app.router.add_get("/hls/master.m3u8", master)
    app.router.add_get("/hls/media.m3u8", media)
    app.router.add_get("/hls/seg1.aac", seg)
    app.router.add_get("/2026/11/post-{n}.html", post_page)
    app.router.add_get("/json/stations/search", rb)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", P).start()
    icy = await asyncio.start_server(icy_server, "127.0.0.1", ICY)
    import ssl as _ssl
    sctx = _ssl.create_default_context(_ssl.Purpose.CLIENT_AUTH)
    sctx.load_cert_chain(CERT, KEY)
    await web.TCPSite(runner, "127.0.0.1", TLS, ssl_context=sctx).start()
    finder.RB_SERVERS[:] = [B]
    # posts do feed apontam para radiocol.com.br; o plano B precisa abrir localhost
    import monitor.blogger as bl
    orig = bl.fetch_post_body

    async def local_body(session, url):
        return await orig(session, url.replace("https://www.radiocol.com.br", B))
    run.fetch_post_body = local_body

    run.asyncio.sleep = _fast_sleep
    import monitor.extras as ex
    ex.todays_slice = lambda pid: True
    os.makedirs(f"{TMP}/data", exist_ok=True)
    json.dump({"https://www.radiocol.com.br/2026/11/post-1.html": {"telefone": {"(84) 3421-0000": "(84) 3421-9999"}}},
              open(f"{TMP}/data/overrides.json", "w"))
    for dia in (1, 2, 3):
        print(f"\n===== execução {dia} =====")
        if dia == 2:   # dia 2: post 1 mudou de URL (frequência nova) e post 9 foi apagado
            POSTS[0]["link"][0]["href"] = "https://www.radiocol.com.br/2026/11/post-1-nova.html"
            del POSTS[8]
        if dia == 3:   # dia 3: você trocou à mão o link do post 8 (que o robô tinha corrigido) — mesmo fora do ar, vale o seu
            POSTS[7]["content"]["$t"] = f'<audio src="{B}/editado-morto.mp3"></audio>'
        await run.main()
        st2 = json.load(open(f"{TMP}/data/state.json"))["posts"]["2"]
        WHATS.append((st2.get("contact_suggestion") or {}).get("score"))
        WHATS_AUTO.append(st2.get("contact_auto"))

    # Solicitações aprovadas na planilha
    from monitor import apply
    print("\n===== solicitações aprovadas =====")
    for req in (
        {"id": "R-1", "tipo": "Atualizar dados", "post": "https://www.radiocol.com.br/2026/11/post-3.html",
         "stream": f"{B}/ok.mp3", "whatsapp": "(84) 98888-1111", "nome_do_solicitante": "não deve passar"},
        {"id": "R-2", "post": "https://www.radiocol.com.br/2026/11/post-2.html", "whatsapp": "84 98888-2222"},
        {"id": "R-3", "tipo": "Remover / corrigir", "post_id": "7"},
    ):
        os.environ["REQUEST_JSON"] = json.dumps(req)
        await apply.main()
    for bad_req in ({"id": "X", "post": "https://www.radiocol.com.br/nao-existe.html", "stream": f"{B}/ok.mp3"},
                    {"id": "Y", "post_id": "1", "stream": "javascript:alert(1)"}):
        os.environ["REQUEST_JSON"] = json.dumps(bad_req)
        try:
            await apply.main()
            print("ERRO: deveria ter recusado", bad_req["id"])
        except SystemExit:
            print("recusado corretamente:", bad_req["id"])
    icy.close()
    await runner.cleanup()


_real_sleep = asyncio.sleep


async def _fast_sleep(t, *a, **k):
    return await _real_sleep(min(t, 0.1))


asyncio.run(main())

full_state = json.load(open(f"{TMP}/data/state.json"))
state = {r["path"]: r for r in full_state["posts"].values()}
print("\n===== estado final =====")
for path, r in sorted(state.items()):
    print(f"{path}: {r.get('status'):9} {r.get('stream')}  [{r.get('source')}] {r.get('reason')}"
          + (f"  SUGESTÃO={r['suggestion']['url']}" if r.get("suggestion") else ""))

exp = {
    "/2026/11/post-1-nova.html": ("ok", "/ok.mp3"),
    "/2026/11/post-2.html": ("ok", "/novo-site.mp3"),
    "/2026/11/post-3.html": ("ok", "/ok.mp3"),   # corrigido por solicitação aprovada
    "/2026/11/post-4.html": ("ok", f":{ICY}/;"),
    "/2026/11/post-5.html": ("ok", "master.m3u8"),
    "/2026/11/post-6.html": ("ok", "/pagina.mp3"),
    "/2026/11/post-7.html": ("ignorado", None),  # remoção aprovada
    "/2026/11/post-8.html": ("off", "/editado-morto.mp3"),   # editado à mão: o robô não troca
    "/2026/11/post-10.html": ("ok", f"https://127.0.0.1:{TLS}/seguro.mp3"),
    "/2026/11/post-11.html": ("ok", "/sohttp.mp3"),
}
bad = 0
for p, (st, frag) in exp.items():
    r = state[p]
    good = r["status"] == st and (frag is None or frag in (r.get("stream") or ""))
    bad += not good
    print(("OK  " if good else "FALHOU ") + p)
pub = json.load(open(f"{TMP}/docs/radios.json"))
print("\nradios.json exemplo:", json.dumps(pub["8"], ensure_ascii=False))
print("post 10 (iframe -> player https):", pub["10"])
print("post 11 (iframe só http):", pub["11"])
if pub["10"].get("f") != 1 or pub["11"].get("h") != 1:
    bad += 1
print("WhatsApp do site (post 2) — confiança por dia:", WHATS, "| troca automática:", WHATS_AUTO)
# dia 1: só sugestão (0,85); dia 2: mesmo número de novo -> 0,95 -> passa a aparecer no post
if not (WHATS[0] <= 0.9 and WHATS_AUTO[0] is None and WHATS[1] > 0.9 and WHATS_AUTO[1] == ["5584999887766", "5584977776666", "w"]):
    bad += 1; print("FALHOU WhatsApp automático")
r8 = state["/2026/11/post-8.html"]
print("post 8 editado à mão:", pub["8"], "| editado_em:", r8.get("editado_em"), "| sugestão:", r8.get("suggestion"))
if pub["8"].get("p") != 1 or "o" in pub["8"] or not r8.get("editado_em"):
    bad += 1; print("FALHOU link editado no post deve prevalecer")
olds = full_state.get("old_urls", {})
print("URLs antigas:", json.dumps(olds, ensure_ascii=False))
if olds.get("/2026/11/post-1.html", {}).get("status") != "ok" or olds.get("/2026/11/post-9.html", {}).get("status") != "sem_redirect":
    bad += 1; print("FALHOU redirecionamentos")
print("contatos post 1:", pub["1"].get("c"))
if pub["1"].get("c") != [["8434210000", "8434219999", "t"]]:
    bad += 1; print("FALHOU contatos")
sug = state["/2026/11/post-2.html"].get("contact_suggestion")
print("sugestão WhatsApp post 2:", sug)
if not sug or sug["site"] != "5584977776666":
    bad += 1; print("FALHOU WhatsApp do site")
ovs = json.load(open(f"{TMP}/data/overrides.json"))
print("overrides após aprovações:", json.dumps(ovs, ensure_ascii=False))
if pub.get("3", {}).get("u", "").endswith("/ok.mp3") is False or pub.get("2", {}).get("c") != [["5584999887766", "84988882222", "w"]] \
        or "7" in pub or "nome_do_solicitante" in json.dumps(ovs):
    bad += 1; print("FALHOU aprovações")
print("paths.json:", list(json.load(open(f"{TMP}/docs/paths.json")).items())[:2])
print("arquivos de shard:", len(os.listdir(f"{TMP}/docs/r")))
print("prévia do e-mail:", os.path.exists(f"{TMP}/data/email_preview.html"))
shutil.copy(f"{TMP}/data/email_preview.html", "/tmp/email_preview.html")
shutil.copy(f"{TMP}/docs/relatorio.html", "/tmp/relatorio.html")

# ----- regras novas (2026-11-18): variações Shoutcast de painel, mesma conta no servidor, página do player
print("\n===== regras de busca (unitárias) =====")
v = finder.variants("https://player.roxcast.com.br/proxy/7440")
if "https://player.roxcast.com.br/proxy/7440/;" not in v or "https://player.roxcast.com.br/proxy/7440/stream" not in v:
    bad += 1; print("FALHOU variação /proxy/PORTA -> /;", v)
v = finder.variants("https://stream02.ouveai.com.br/8010/stream")
if "https://stream02.ouveai.com.br/8010/;" not in v:
    bad += 1; print("FALHOU variação /PORTA/stream -> /PORTA/;", v)
v = finder.variants("https://player.srvstm.com/proxy/6798%20/;")
if "https://player.srvstm.com/proxy/6798/;" not in v:
    bad += 1; print("FALHOU espaço no endereço", v)
if finder.variants("https://ice.fabricahost.com.br/arenafm2") != []:
    bad += 1; print("FALHOU: Icecast compartilhado sem porta não deve gerar variações de Shoutcast")
F = finder.Finder.__new__(finder.Finder)
if F._conta("https://ice.fabricahost.com.br/arenafm2") is not None or \
        F._conta("https://stm01.virtualcast.com.br:8168/live") != ("stm01.virtualcast.com.br", 8168):
    bad += 1; print("FALHOU chave da conta no servidor")
pg = finder.Finder._paginas({"sites": ["https://a.com.br/"], "stream": "https://livecasthd.com.br/play.php?porta=1",
                             "reason": "responde página HTML, não áudio"})
if pg != ["https://a.com.br/", "https://livecasthd.com.br/play.php?porta=1"] or \
        finder.Finder._paginas({"sites": [], "stream": "https://x/1", "reason": "HTTP 404"}) != []:
    bad += 1; print("FALHOU página do player do post", pg)
a = finder.Finder._ajusta_pagina_player({"url": "u", "source": "site oficial (x)", "confidence": "suggest", "score": 0.9}, True)
if a["score"] >= 0.6:
    bad += 1; print("FALHOU: página do player com vários endereços deve ser só sugestão", a)


class _Chk:
    def __init__(self, bons):
        self.bons = bons

    async def check(self, u):
        return type("R", (), {"ok": u in self.bons, "reason": "ok"})()


async def _conta_testes():
    global bad
    lista = [("http://outro-nome:8168/stream", "radio a"), ("http://outro-nome:8170/live", "radio b")]
    f = finder.Finder.__new__(finder.Finder)
    f.c = _Chk({"https://stm01.virtualcast.com.br:8168/stream"})
    f._listas = {"https://stm01.virtualcast.com.br:8168": lista}
    r = await f.from_mesma_conta({"stream": "https://stm01.virtualcast.com.br:8168/live", "reason": "HTTP 404"})
    print("mesma conta (404):", r)
    if not r or r["url"] != "https://stm01.virtualcast.com.br:8168/stream" or r["confidence"] != "high":
        bad += 1; print("FALHOU ponto de montagem novo na mesma porta")
    # servidor compartilhado (porta padrão): nunca troca por outra rádio do mesmo servidor
    f._listas = {"https://ice.fabricahost.com.br": [("https://ice.fabricahost.com.br/outra", "outra")]}
    f.c = _Chk({"https://ice.fabricahost.com.br/outra"})
    if await f.from_mesma_conta({"stream": "https://ice.fabricahost.com.br/arenafm2", "reason": "HTTP 404"}):
        bad += 1; print("FALHOU: servidor compartilhado não pode trocar de rádio")
    # dois pontos na mesma porta: ambíguo, não troca
    f._listas = {"https://s.x.com:9000": [("http://s.x.com:9000/a", ""), ("http://s.x.com:9000/b", "")]}
    f.c = _Chk({"https://s.x.com:9000/a", "https://s.x.com:9000/b"})
    if await f.from_mesma_conta({"stream": "https://s.x.com:9000/live", "reason": "HTTP 404"}):
        bad += 1; print("FALHOU: mais de um ponto na porta deve ficar sem troca")

asyncio.run(_conta_testes())
print("RESULTADO:", "TUDO CERTO" if not bad else f"{bad} FALHA(S)")
