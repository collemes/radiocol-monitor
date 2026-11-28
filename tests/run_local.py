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
PAINEL = 8768     # servidor de painel com porta própria da rádio (ponto /live trocado por /stream)
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
from monitor import navegador  # noqa: E402


async def _toca_no_radiocol_teste(self, url_post):
    """Os posts do teste não existem no Radiocol de verdade: o player do site "não toca" (troca autorizada).
    Sem isto o teste dependia de o navegador alcançar radiocol.com.br (fora da rede, dava None = não troca)."""
    return False


navegador.Navegador.toca_no_radiocol = _toca_no_radiocol_teste

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
    # servidor recusa o robô (403 = "bloqueio"), mas uma variação do MESMO endereço toca: deixa de ser bloqueio
    entry(12, "Rádio Bloqueio FM", f'<audio src="{B}/bloq/1234"></audio> <a href="http://localhost:{P}/x.html">Site</a>'),
    # porta própria no painel: o ponto /live sumiu e o /stream só aparece no dia 3, quando a rádio já foi pesquisada
    # nesta semana (fora da busca completa): a passada diária de variações tem de corrigir
    entry(13, "Rádio Painel FM", f'<audio src="http://127.0.0.1:{PAINEL}/live"></audio>'),
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


async def painel_stream(req):
    if not STATE.get("painel_stream"):
        raise web.HTTPNotFound()
    return await audio(req)


async def proibido(req):
    raise web.HTTPForbidden()


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
    app.router.add_get("/bloq/1234", proibido)
    app.router.add_get("/bloq/1234/stream", audio)
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
    app_p = web.Application()
    app_p.router.add_get("/stream", painel_stream)
    runner_p = web.AppRunner(app_p)
    await runner_p.setup()
    await web.TCPSite(runner_p, "127.0.0.1", PAINEL).start()
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
        if dia == 3:   # dia 3: o painel passou a usar /stream (post 13 já pesquisado nesta semana)
            STATE["painel_stream"] = True
            st_path = f"{TMP}/data/state.json"
            stj = json.load(open(st_path))
            r13 = stj["posts"]["13"]
            print("post 13 antes do dia 3:", r13.get("status"), r13.get("fail_count"), r13.get("search_tried"))
            r13["fail_count"] = 9
            json.dump(stj, open(st_path, "w"))
        if dia == 3:   # dia 3: o robô tinha gravado no post 1 o vídeo de propaganda que o navegador "tocou" no site
            st_path = f"{TMP}/data/state.json"
            stj = json.load(open(st_path))
            r1 = stj["posts"]["1"]
            r1["stream"] = "https://rr3---sn-o097znzd.c.2mdn.net/videoplayback/id/9f7b/itag/342/file/file.mp4"
            r1["source"] = "site oficial (x.com.br) — tocou no navegador [navegador]"
            json.dump(stj, open(st_path, "w"))
        if dia == 2:   # migração única pendente com o mutirão já feito (24/11: o robô caiu com UnboundLocalError)
            st_path = f"{TMP}/data/state.json"
            stj = json.load(open(st_path))
            stj.pop("migr_outra_emissora", None)
            json.dump(stj, open(st_path, "w"))
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
    await runner_p.cleanup()


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
    "/2026/11/post-12.html": ("ok", "/bloq/1234/stream"),   # bloqueio -> variação do mesmo endereço
    "/2026/11/post-13.html": ("ok", f"127.0.0.1:{PAINEL}/stream"),   # passada diária de variações
}
bad = 0
for p, (st, frag) in exp.items():
    r = state[p]
    good = r["status"] == st and (frag is None or frag in (r.get("stream") or ""))
    bad += not good
    print(("OK  " if good else "FALHOU ") + p)
pub = json.load(open(f"{TMP}/docs/radios.json"))
r1 = state["/2026/11/post-1-nova.html"]
print("post 1 (vídeo de propaganda desfeito):", r1.get("source"), (r1.get("history") or [{}])[-1].get("note"))
if r1.get("source") != "post" or "2mdn" in json.dumps(r1.get("stream")) or \
        not any("vídeo/anúncio" in (h.get("note") or "") for h in r1.get("history") or []):
    bad += 1; print("FALHOU: vídeo/anúncio gravado como stream deve voltar ao link do post")
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
if finder.variants("https://ice.fabricahost.com.br/arenafm2") != ["https://play.wisestream.io/arenafm2"]:  # só a migração do provedor
    bad += 1; print("FALHOU: Icecast compartilhado sem porta não deve gerar variações de Shoutcast")
# ----- regras novas (2026-11-19)
v = finder.variants("http://servidor21.dnip.com.br:9940/;")
if "http://servidor21.dnip.com.br:9940/stream" not in v or "https://servidor21.dnip.com.br:9940/;stream.mp3" not in v \
        or any("/;/" in x for x in v):
    bad += 1; print("FALHOU Shoutcast '/;' (o ';' final não pode sumir)", v)
v = finder.variants("https://node-30.zeno.fm/n2s14rugfv8uv?rj-ttl=5&rj-tok=AAABeuqk8qAA")
v2 = finder.variants("http://ample-zeno-05.radiojar.com/4xa2ects7a5tv")
if "https://stream.zeno.fm/n2s14rugfv8uv" not in v or "https://stream.zeno.fm/4xa2ects7a5tv" not in v2 \
        or "https://stream.zeno.fm/qfau0echa8zuv" not in finder.variants("https://node-04.zeno.fm/qfau0echa8zuv.aac"):
    bad += 1; print("FALHOU Zeno.FM nó antigo/token vencido -> stream.zeno.fm/ID", v, v2)
v = finder.variants("https://cdn.radioscast.com.br/bs/proxy/stream.php?ip=stm3.xcast.com.br&port=7244&mount=/stream")
v2 = finder.variants("https://cdn.radioscast.com.br/stream1234?http://stm7.betelhost.com.br:11824/;")
if "http://stm3.xcast.com.br:7244/stream" not in v or "http://stm7.betelhost.com.br:11824/;" not in v2 \
        or "http://stm7.betelhost.com.br:11824/stream" not in v2:
    bad += 1; print("FALHOU origem embrulhada no proxy do player", v, v2)
v = finder.variants("https://live.paineldj.com.br/proxy/tparanavai?mp=/live")
if "https://live.paineldj.com.br/proxy/tparanavai?mp=/stream" not in v or any("paineldj" not in x for x in v):
    bad += 1; print("FALHOU painel /proxy/NOME?mp=/live -> /stream", v)
v = finder.variants("https://5a1c76baf08c0.streamlock.net/difusora/difusora/chunklist_w1355963099.m3u8")
if v != ["https://5a1c76baf08c0.streamlock.net/difusora/difusora/playlist.m3u8"]:
    bad += 1; print("FALHOU Wowza chunklist de sessão -> playlist.m3u8", v)
v = finder.variants("https://r13.ciclano.io:15219/stream?type=http&nocache=40")
if "https://r13.ciclano.io:15219/stream" not in v:
    bad += 1; print("FALHOU query de anti-cache", v)
# query que escolhe a rádio (?id=nome, stream.php?porta=) nunca é removida; raiz de servidor compartilhado não vira /;
if finder.variants("https://host.com/live?id=radiox") or finder.variants("https://host.com/?id=radiox") \
        or finder.variants("https://svrstream2.svreua.com/stream.php?porta=8380"):
    bad += 1; print("FALHOU: query que escolhe a rádio não pode ser removida")
# ----- regras novas (2026-11-20)
v = finder.variants("https://21933.live.streamtheworld.com/RT_SALAAC.aac?")
v2 = finder.variants("http://1061.live.streamtheworld.com:3690/SP_STUDIOC_SC")
if not v or v[0] != "https://playerservices.streamtheworld.com/api/livestream-redirect/RT_SALAAC.aac" \
        or "https://playerservices.streamtheworld.com/api/livestream-redirect/SP_STUDIOC_SC" not in v2 \
        or finder.variants("https://24253.live.streamtheworld.com/RADIO_ELDORADOAAC/HLS/playlist.m3u8"):
    bad += 1; print("FALHOU StreamTheWorld nó numerado -> redirecionador com o mesmo ponto", v, v2)
v = finder.variants("https://cast.youngtech.radio.br/radio/8850/radio?fbclid=IwAR2")
if "https://cast.youngtech.radio.br:8850/radio" not in v or "https://cast.youngtech.radio.br:9850/radio" in v \
        or any(":8850" in x for x in finder.variants("https://host.com:8000/radio/8850/radio")):
    bad += 1; print("FALHOU AzuraCast /radio/PORTA/ponto -> porta direta", v)
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

# ----- regras novas (2026-11-21): ponto de montagem genérico trocado na porta própria da rádio
v = finder.variants("https://stm01.virtualcast.com.br:8168/live")
if "https://stm01.virtualcast.com.br:8168/stream" not in v or "https://stm01.virtualcast.com.br:8168/;" not in v:
    bad += 1; print("FALHOU porta própria /live -> /stream e /;", v)
if "http://servidor21.dnip.com.br:9940/live" not in finder.variants("http://servidor21.dnip.com.br:9940/;"):
    bad += 1; print("FALHOU porta própria /; -> /live")
# ponto com nome da rádio ou servidor compartilhado (sem porta própria): nunca troca o ponto
if any(x.endswith(("/stream", "/live")) for x in finder.variants("https://stm01.virtualcast.com.br:8000/massasaopaulo")) \
        or any(x.endswith(("/stream", "/;")) for x in finder.variants("https://ice.fabricahost.com.br/live")):
    bad += 1; print("FALHOU: ponto com nome da rádio / servidor compartilhado não pode trocar o ponto")

asyncio.run(_conta_testes())


# ----- regra nova (2026-11-22): diretórios que não abrem dizem no log o MOTIVO (HTTP 403, tipo, erro)
async def _diretorios_motivo():
    global bad
    import tempfile
    from monitor import config as cfg, radioscombr, tudoradio

    async def recusa(req):
        return web.Response(status=403, text="bloqueado")
    app = web.Application()
    app.router.add_get("/{x:.*}", recusa)
    rn = web.AppRunner(app)
    await rn.setup()
    await web.TCPSite(rn, "127.0.0.1", 8799).start()
    async with aiohttp.ClientSession() as s:
        f = finder.Finder(s, None)
        u = "http://127.0.0.1:8799/player/sitemap"
        if await f._get_text(u) != "" or f.erros_texto.get(u) != "HTTP 403":
            bad += 1; print("FALHOU motivo da leitura vazia", f.erros_texto)
    await rn.cleanup()
    antigo = cfg.DATA_DIR
    cfg.DATA_DIR = tempfile.mkdtemp()
    linhas = []

    async def vazio(url, limit=0):
        return ""
    try:
        await tudoradio.atualizar(vazio, "2026-11-22", linhas.append, motivo=lambda u: "HTTP 403")
        ind = await radioscombr.atualizar(vazio, "2026-11-22", linhas.append, motivo=lambda u: "HTTP 403")
    finally:
        cfg.DATA_DIR = antigo
    print("diretórios:", linhas)
    if not any("mapa indisponível hoje (HTTP 403)" in l for l in linhas) or \
            not any("listas sem resposta" in l and "HTTP 403" in l for l in linhas):
        bad += 1; print("FALHOU: log dos diretórios sem o motivo")
    if ind["listas"][radioscombr.INICIO].get("lido"):
        bad += 1; print("FALHOU: lista que não abriu deve ser tentada de novo na próxima execução")

import aiohttp  # noqa: E402
asyncio.run(_diretorios_motivo())


# ----- regras novas (2026-11-23): vídeo/anúncio nunca é stream; certificado https com defeito -> o mesmo em http
async def _video_e_ssl():
    global bad
    from monitor.extract import parece_video, extract_streams
    from monitor.checker import Checker
    videos = ["https://rr1---sn-qja5m5-5o.c.2mdn.net/videoplayback/id/2dc8/file/file.mp4",
              "https://v.adsrvr.org/dx2n68l/ntvhf5uu_1_52.mp4", "https://t6.al/1x5l.mp4",
              "https://www.colinasfm.com.br/video/sound.mp4",
              "https://vid.cdn-website.com/f716eb66/videos/Composicao-v.mp4"]
    audios = ["https://stream.zeno.fm/abc12345", "https://s.x.com:8000/live.mp3", "https://h.com/radio/playlist.m3u8",
              "http://servidor21.dnip.com.br:9940/;", "https://cast.mp4radio.com.br/stream", "https://h.com/hls/seg.ts"]
    if not all(parece_video(u) for u in videos) or any(parece_video(u) for u in audios):
        bad += 1; print("FALHOU parece_video", [u for u in videos if not parece_video(u)], [u for u in audios if parece_video(u)])
    achados = extract_streams('<audio src="https://www.colinasfm.com.br/video/sound.mp4"></audio> '
                              '<video src="https://t6.al/1x5l.mp4"></video> https://stm.radiox.com.br:8000/live')
    if achados != ["https://stm.radiox.com.br:8000/live"]:
        bad += 1; print("FALHOU: vídeo não pode sair como stream do HTML", achados)
    async with aiohttp.ClientSession() as s:
        r = await Checker(s).check("https://v.adsrvr.org/dx2n68l/ntvhf5uu_1_52.mp4")
    if r.ok or "vídeo" not in r.reason:
        bad += 1; print("FALHOU: checker deve recusar vídeo/anúncio", r)
    # o navegador "tocou" a propaganda do site: não vale como prova
    f = finder.Finder.__new__(finder.Finder)
    f.rede = set()
    f.c = _Chk(set())
    ad = "https://rr3---sn-o097znzd.c.2mdn.net/videoplayback/id/9f7b/file/file.mp4"
    if await f._escolhe({"stream": "http://morto:8000/live"}, "https://radiox.com.br/", [(ad, "")], frozenset([ad])):
        bad += 1; print("FALHOU: vídeo que tocou no navegador não pode virar stream")
    # certificado https inválido / porta que não fala https: testa o MESMO endereço em http
    f.c = _Chk({"http://audio01.viaflux.com:5499/live", "http://59e4b6c1beddc.streamlock.net/c/c/playlist.m3u8"})
    r = await f.from_variants({"stream": "https://audio01.viaflux.com:5499/live", "post_candidates": [],
                               "reason": "sem conexão (SSLError)"})
    r2 = await f.from_variants({"stream": "https://59e4b6c1beddc.streamlock.net:443/c/c/playlist.m3u8",
                                "post_candidates": [], "reason": "certificado SSL inválido"})
    print("https com defeito -> http:", r and r["url"], "|", r2 and r2["url"])
    if not r or r["url"] != "http://audio01.viaflux.com:5499/live" or not r["source"].startswith("variação") \
            or not r2 or r2["url"] != "http://59e4b6c1beddc.streamlock.net/c/c/playlist.m3u8":
        bad += 1; print("FALHOU https com certificado com defeito -> mesmo endereço em http", r, r2)
    # outro motivo (404, recusado) ou rádio que precisa de https: não desce para http
    if await f.from_variants({"stream": "https://audio01.viaflux.com:5499/live", "post_candidates": [],
                              "reason": "sem conexão (ConnectionRefusedError)"}) or \
            await f.from_variants({"stream": "https://audio01.viaflux.com:5499/live", "post_candidates": [],
                                   "reason": "certificado SSL inválido", "_need_https": True}):
        bad += 1; print("FALHOU: http só quando o problema é o certificado e a rádio aceita http")

asyncio.run(_video_e_ssl())

# ----- regra nova (2026-11-25): "radio.co" (provedor) não casa com o site "…radio.com.br" da emissora
from monitor.extract import extract_sites as _es, extract_streams as _xs, stream_score as _ss
_post = ('<a href="https://www.tuaradio.com.br/Sarandi/">site</a> <a href="https://cacapavaradio.com.br/">site</a> '
         'https://play.radio.br:8089/sarandi/sarandi/playlist.m3u8')
if _es(_post) != ["https://www.tuaradio.com.br/", "https://cacapavaradio.com.br/"] or \
        _xs(_post) != ["https://play.radio.br:8089/sarandi/sarandi/playlist.m3u8"]:
    bad += 1; print("FALHOU: site '…radio.com.br' tem de ser site, não stream", _es(_post), _xs(_post))
for _u in ("https://minharadio.radio.co/live", "https://streaming.radio.co/s1234/listen",
           "https://shout25.crossradio.com.br:18202/1", "https://stream.zeno.fm/abc12345"):
    if not _ss(_u):
        bad += 1; print("FALHOU: stream de provedor conhecido deixou de valer", _u)

# ----- regra nova (2026-11-25): AzuraCast anuncia a porta direta (host:8040/fm) e o post usa o proxy /radio/8040/am
async def _azuracast_porta():
    global bad
    f = finder.Finder.__new__(finder.Finder)
    f._listas = {"https://cast.y.radio.br": [("https://cast.y.radio.br:8040/fm", "a"), ("https://cast.y.radio.br:8020/radio", "b")]}
    f.c = _Chk({"https://cast.y.radio.br/radio/8040/fm"})
    r = await f.from_mesma_conta({"stream": "https://cast.y.radio.br/radio/8040/am", "reason": "HTTP 404"})
    if not r or r["url"] != "https://cast.y.radio.br/radio/8040/fm":
        bad += 1; print("FALHOU AzuraCast: ponto novo na mesma porta", r)
    # porta que não está na lista (emissora removida do servidor): nada
    if await f.from_mesma_conta({"stream": "https://cast.y.radio.br/radio/8035/radio", "reason": "HTTP 404"}):
        bad += 1; print("FALHOU AzuraCast: porta de outra emissora não pode ser usada")

asyncio.run(_azuracast_porta())
# ----- regra nova (2026-11-25): play.radio.br migrou para painel.sintonizar.tv.br com o mesmo nome de ponto
if "https://painel.sintonizar.tv.br/stream/cacique" not in finder.variants("https://play.radio.br:8089/cacique/cacique/playlist.m3u8") \
        or "https://painel.sintonizar.tv.br/stream/Solaris97" not in finder.variants("https://stream1.play.radio.br:8089/Solaris97/Solaris97/icecast.audio"):
    bad += 1; print("FALHOU play.radio.br -> sintonizar")
if any("sintonizar" in x for x in finder.variants("https://play.radio.br:8089/Redesul/RSCultura/playlist.m3u8")
       + finder.variants("https://play.radio.br:8089/live/live/playlist.m3u8")
       + finder.variants("https://outro.com.br:8089/cacique/cacique/playlist.m3u8")):
    bad += 1; print("FALHOU: sintonizar só com nome próprio igual nos dois pedaços e só para play.radio.br")
# ----- regra nova (2026-11-25): link para OUTRO post do Radiocol nunca é o player desta rádio
from monitor.extract import extract_player_links as _epl
from monitor import run as _run
_base = "https://www.radiocol.com.br/2015/01/ouvir-radio-sucesso-fm-899-de-sao-luis.html"
_html = ('<a href="https://www.radiocol.com.br/2020/02/ouvir-super-radio-boa-vontade-am-1210-brasilia-df-ao-vivo.html">'
         'Ouvir a Super Rádio Boa Vontade</a> <a href="/2014/05/ouvir-radio-pelotense-am-620-de-pelotas.html">Ouça</a> '
         '<a href="https://radiosaovivo.radiocol.com.br/2019/01/sucesso.html">Clique aqui para ouvir</a>')
if _epl(_html, _base) != ["https://radiosaovivo.radiocol.com.br/2019/01/sucesso.html"]:
    bad += 1; print("FALHOU: post vizinho do blog virou player", _epl(_html, _base))
_r = {"url": _base, "popup": "https://www.radiocol.com.br/2020/02/x.html", "popup_streams": ["https://8802.b.com.br/stream"],
      "post_stream": "https://8802.b.com.br/stream", "stream": "https://8802.b.com.br/stream", "source": "post",
      "editado_em": "2026-11-20"}
_run.corrigir_popup_de_outro_post(_r, None)
if _r.get("stream") or _r.get("post_stream") or _r.get("popup") or _run.protegido(_r):
    bad += 1; print("FALHOU: áudio vindo do post vizinho tem de sair", _r)
_r = {"url": _base, "popup": "https://www.radiocol.com.br/2020/02/x.html", "post_stream": "https://8802.b.com.br/stream",
      "stream": "https://s03.t.com:7582/stream", "source": "tudoradio: Rádio X (Cidade/UF, 89.9 FM) — conferem"}
_run.corrigir_popup_de_outro_post(_r, None)
if _r.get("stream") != "https://s03.t.com:7582/stream" or _r.get("post_stream"):
    bad += 1; print("FALHOU: áudio com prova própria (diretório) fica", _r)
_r = {"url": _base, "popup": "https://www.radiocol.com.br/2020/02/x.html", "post_stream": "https://8802.b.com.br/stream",
      "stream": "https://8802.b.com.br/stream", "source": "post"}
_run.corrigir_popup_de_outro_post(_r, "https://novo.link.com.br:8000/live")
if _r.get("post_stream") != "https://8802.b.com.br/stream" or _r.get("popup"):
    bad += 1; print("FALHOU: com link próprio no post, o fluxo normal (edição do dono) decide", _r)
# ----- regra nova (2026-11-26): KSHost -> Brascast (mesmo servidor htsNN, mesma porta) e Fábrica Host -> Wisestream
_v = finder.variants("http://hts02.kshost.com.br:8842/live")
if "https://hts02.brascast.com:8842/live" not in _v or \
        "https://hts06.brascast.com:10482/live" not in finder.variants("https://app.kshost.com.br/player-ssl/hts06/10482"):
    bad += 1; print("FALHOU htsNN.kshost -> htsNN.brascast", _v)
if any("brascast" in x for x in finder.variants("https://ice01.kshost.com.br:8000/live")
       + finder.variants("https://hts02.kshost.com.br/live") + finder.variants("https://hts02.outro.com.br:8842/live")):
    bad += 1; print("FALHOU: brascast só para htsNN.kshost com porta própria")
if "https://play.wisestream.io/maximafmpr" not in finder.variants("https://ice.fabricahost.com.br/maximafmpr"):
    bad += 1; print("FALHOU ice.fabricahost -> play.wisestream.io")
if any("wisestream" in x for x in finder.variants("https://ice.fabricahost.com.br/live")
       + finder.variants("https://ice.fabricahost.com.br/radio/abc") + finder.variants("https://outro.com.br/maximafmpr")):
    bad += 1; print("FALHOU: wisestream só com nome próprio no ice.fabricahost")


async def _kshost_porta():
    global bad
    f = finder.Finder.__new__(finder.Finder)
    f.c = _Chk({"https://hts07.brascast.com:13492/live"})
    r = await f.from_kshost_porta({"stream": "https://player-ssl.kshost.com.br:13492/live.aac"})
    if not r or r["url"] != "https://hts07.brascast.com:13492/live":
        bad += 1; print("FALHOU player-ssl.kshost -> servidor brascast com a mesma porta", r)
    # dois servidores tocando na mesma porta: dúvida, não troca
    f.c = _Chk({"https://hts07.brascast.com:13492/live", "https://hts03.brascast.com:13492/stream"})
    if await f.from_kshost_porta({"stream": "https://player-ssl.kshost.com.br:13492/live"}):
        bad += 1; print("FALHOU: dois servidores na mesma porta não pode trocar")
    f.c = _Chk({"https://hts07.brascast.com:13492/live"})
    if await f.from_kshost_porta({"stream": "https://player-br.outro.com.br:13492/live"}):
        bad += 1; print("FALHOU: só o proxy player-ssl.kshost")

asyncio.run(_kshost_porta())

# ----- regra nova (2026-11-28): achado de confiança alta que só não foi aplicado porque a cota de "conferir no
# Radiocol" acabou fica PENDENTE e volta na frente da fila no dia seguinte (antes esperava 7 dias como sugestão)
_sg = lambda fonte, score=0.95, url="https://novo.com.br:8000/live": {"url": url, "source": fonte, "score": score}
_casos = [
    ({"status": "off", "suggestion": _sg("tudoradio: Rádio X (Cidade/UF, 89.9 FM) — cidade, UF e frequência conferem")}, True),
    ({"status": "off", "suggestion": _sg("site oficial (radiox.com.br)", 0.9)}, True),
    ({"status": "off", "suggestion": _sg("Radio Browser: X FM — estado, cidade e frequência conferem", 0.9)}, True),
    ({"status": "off", "suggestion": _sg("Radio Browser: X FM (estado, cidade ou frequência não conferem)", 0.59)}, False),
    ({"status": "off", "suggestion": _sg("site oficial (radiox.com.br)", 0.65)}, False),           # incerto
    ({"status": "off", "editado_em": "2026-11-20", "suggestion": _sg("site oficial (radiox.com.br)", 0.9)}, False),
    ({"status": "off", "source": "manual", "suggestion": _sg("site oficial (radiox.com.br)", 0.9)}, False),
    ({"status": "ok", "suggestion": _sg("site oficial (radiox.com.br)", 0.9)}, False),
    ({"status": "off", "encerrada": True, "suggestion": _sg("site oficial (radiox.com.br)", 0.9)}, False),
    ({"status": "off", "suggestion": _sg("site oficial (radiox.com.br) — não aplicado (só site oficial)", 0.9)}, False),
]
for _rec, _esperado in _casos:
    if _run.marcar_pendente_antiga(_rec) != _esperado or _run.pendente_conferir(_rec) != _esperado:
        bad += 1; print("FALHOU pendente de conferência (migração)", _esperado, _rec)
if _run.pendente_conferir({"status": "off"}) or _run.pendente_conferir({"suggestion": None}):
    bad += 1; print("FALHOU: sem sugestão não é pendente")
print("RESULTADO:", "TUDO CERTO" if not bad else f"{bad} FALHA(S)")
