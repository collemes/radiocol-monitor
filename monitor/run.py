"""Execução diária: python -m monitor.run"""
import asyncio
import json
import os
import time
from datetime import date, datetime, timezone, timedelta

import aiohttp
import re
from urllib.parse import unquote, urljoin, urlparse, urlsplit

from . import config
from .blogger import BODY_RE, fetch_post_body, fetch_post_body_raw, fetch_posts, load_blogger_backup
from .checker import Checker
from .extract import (frequency, norm, classify_post, extract_contacts, extract_live_iframe, extract_player_links, extract_sites,
                      extract_location, extract_streams, norm, stream_score)
from .extras import check_old_urls, contact_pairs, register_old_url, scan_site_whatsapp
from . import radioscombr, tudoradio
from .finder import Finder, rb_confere, rb_mesma_praca, variants, SHOUT_HTTPS_RE, audio_ok
from .notify import build_report, send_email, write_public

BRT = timezone(timedelta(hours=-3))


def now():
    return datetime.now(BRT).strftime("%Y-%m-%d %H:%M")


def today():
    return datetime.now(BRT).strftime("%Y-%m-%d")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def log(*a):
    print(f"[{datetime.now(BRT):%H:%M:%S}]", *a, flush=True)


async def ler_tocou(session):
    """Avisos 'tocou / falhou' enviados pelo player dos visitantes (via Apps Script). {post_id: {...}}"""
    if not config.TOCOU_URL:
        return {}
    try:
        async with session.get(config.TOCOU_URL, timeout=aiohttp.ClientTimeout(total=40)) as r:
            if r.status != 200:
                return {}
            d = json.loads(await r.text())
            return d.get("posts") or {}
    except Exception as ex:
        log(f"Avisos 'tocou no Brasil' indisponíveis: {ex}")
        return {}


def situacao_brasil(aviso, stream, hoje):
    """'ok' se tocou para alguém no Brasil há poucos dias; 'off' se só falhou; None sem dados."""
    if not aviso or aviso.get("u") != stream:
        return None, None
    d0 = date.fromisoformat(hoje)
    dias = lambda k, n: {d: c for d, c in (aviso.get(k) or {}).items() if (d0 - date.fromisoformat(d)).days <= n}
    oks = dias("ok", config.TOCOU_DIAS_OK)
    if oks:
        return "ok", max(oks)
    if not dias("ok", 7) and sum(dias("er", 7).values()) >= config.TOCOU_ERROS:
        return "off", max(dias("er", 7))
    return None, None


def change(rec, new_url, source, note=""):
    hist = rec.setdefault("history", [])
    hist.append({"date": today(), "from": rec.get("stream"), "to": new_url, "source": source, "note": note})
    rec["history"] = hist[-6:]
    rec["stream"] = new_url
    rec["source"] = source


_GENERICAS = set("ouvir a o radio radios web fm am ao vivo e online de do da dos das em the mhz khz tv".split())
_UFS = set("ac al ap am ba ce df es go ma mt ms mg pa pb pr pe pi rj rn rs ro rr sc sp se to".split())


def _compacto(t):
    return re.sub(r"[^a-z0-9]", "", norm(t or ""))


def _palavras_nome(t, cidade=""):
    fora = set(norm(cidade or "").split())
    return {w for w in re.findall(r"[a-z]+", norm(t or "")) if len(w) >= 3
            and w not in _GENERICAS and w not in _UFS and w not in fora}


def tem_prova(rec, url, fonte, usos_site, usos_stream):
    """O endereço é da MESMA rádio do post? (sem isso o robô não troca o áudio sozinho)
    - Radio Browser: o nome da emissora no diretório bate em 2 de 3: cidade, frequência, nome da rádio.
    - Site oficial da própria emissora (site usado por menos de 3 posts): sim.
    - Site de rede/grupo: o endereço traz a cidade ou a frequência do post, não aparece em outro post e não é de
      provedor que nomeia pela marca (StreamTheWorld: 'MIXFM_JOAOPESSOA' não é a Correio AM de João Pessoa)."""
    fonte = fonte or ""
    cidade = (rec.get("loc") or [None])[0] or ""
    cid = _compacto(cidade)
    f = frequency(rec.get("title", ""))
    if fonte.startswith(("tudoradio", "radios.com.br")):
        return "conferem" in fonte                   # só entra com cidade, UF e frequência (ou nome) iguais
    if fonte.startswith("Radio Browser"):
        # cidade, UF e frequência precisam conferir (o nome da rádio é opcional)
        nome_rb = fonte.split(":", 1)[-1].split(" — ")[0]
        return rb_confere(rec, nome_rb)
    m = re.search(r"site oficial \(([^)]+)\)", fonte)
    if m and usos_site.get(m.group(1).replace("www.", ""), 0) < 3:
        return True
    if url and usos_stream.get(url, 0) <= 1 and "streamtheworld" not in url:
        u = _compacto(url)
        return bool((f and f.replace(".", "") in u) or (len(cid) >= 4 and cid in u))
    return False


async def checar_site(session, url):
    """Situação de um site: ("ok", endereço final) | ("fora", motivo). Segue redirecionamentos."""
    try:
        async with session.get(url, headers={"User-Agent": config.USER_AGENT}, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status >= 400:
                return "fora", f"HTTP {r.status}"
            return "ok", str(r.url)
    except aiohttp.ClientConnectorError as ex:
        return "fora", "domínio não existe mais" if "Name or service" in str(ex) or "gaierror" in repr(ex) \
            else "sem conexão"
    except asyncio.TimeoutError:
        return "fora", "tempo esgotado"
    except Exception as ex:
        return "fora", type(ex).__name__


def _dominio(u):
    return (urlparse(u or "").hostname or "").replace("www.", "")


async def verificar_sites(session, recs, hoje):
    """Site do post das rádios fora do ar / sem link / não testáveis, 1x por semana. Redirecionou para outro
    domínio = site novo da emissora; 404, domínio extinto ou sem conexão = site fora do ar."""
    fila = [r for r in recs if r.get("sites") and r.get("status") in ("off", "sem_link", "bloqueio")
            and (not r.get("site_checked") or
                 (date.fromisoformat(hoje) - date.fromisoformat(r["site_checked"])).days >= 7)][:600]
    sem = asyncio.Semaphore(10)

    async def um(rec):
        async with sem:
            situacao, info = await checar_site(session, rec["sites"][0])
        rec["site_checked"] = hoje
        if situacao == "ok":
            rec.pop("site_fora", None)
            if _dominio(info) and _dominio(info) != _dominio(rec["sites"][0]):
                rec["site_novo"] = info.split("?")[0]               # o site mudou de endereço (redirecionamento)
        else:
            rec["site_fora"] = info
    await asyncio.gather(*(um(r) for r in fila))
    return len(fila), sum(1 for r in fila if r.get("site_fora"))


async def adotar_site(session, rec, site):
    """Áudio veio de diretório/Radio Browser com o site da emissora: se o site do post está fora do ar (ou o
    post não tem site), adota o site do diretório — só se ele abrir."""
    if not site or not site.startswith("http"):
        return
    atual = (rec.get("sites") or [None])[0]
    if atual and not rec.get("site_fora"):
        return
    if atual and _dominio(atual) == _dominio(site):
        return
    situacao, final = await checar_site(session, site)
    if situacao == "ok":
        rec["site_novo"] = final.split("?")[0]


def formato_vizinho(url, https_por_host):
    """Outra rádio do MESMO servidor já toca em https: copia o formato dela para a porta desta.
    http://paineldj5.com.br:20033/  +  https://paineldj5.com.br:20015/stream  ->  https://paineldj5.com.br:20033/stream
    Painel com a porta no caminho (/proxy/20015, /20015/stream): troca a porta no caminho."""
    try:
        p = urlsplit(url)
        porta = p.port
    except ValueError:
        return []
    if p.scheme != "http" or not porta or porta in (80, 443):
        return []
    out = []
    for viz in https_por_host.get(p.hostname, [])[:20]:
        try:
            q = urlsplit(viz)
            qp = q.port
        except ValueError:
            continue
        caminho = q.path or "/"
        if qp and qp != 443:
            # porta própria no endereço: mesmo ponto de montagem genérico (/stream, /;, /1;, /live…)
            if re.fullmatch(r"/(?:;|\d?;|;stream/1|;stream\.mp3|stream|live|radio|aovivo|stream/1/?)", caminho, re.I):
                out.append(f"https://{p.hostname}:{porta}{caminho}")
        elif str(qp or "") == "" and re.search(r"/\d{4,5}(?=/|$|;)", caminho):
            out.append(f"https://{q.hostname}" + re.sub(r"/\d{4,5}(?=/|$|;)", f"/{porta}", caminho, count=1))
    vistos = []
    for u in out:
        if u not in vistos:
            vistos.append(u)
    return vistos[:3]


def protegido(rec):
    """Endereço que o robô não troca sozinho: fixado no overrides.json/planilha ou editado à mão no post."""
    return rec.get("source") == "manual" or bool(rec.get("editado_em"))


async def main():
    t0 = time.time()
    state_path = os.path.join(config.DATA_DIR, "state.json")
    state = load(state_path, {"posts": {}})
    posts_state = state["posts"]
    # para tem_prova(): quantos posts usam cada site e cada endereço de áudio (dados da execução anterior)
    usos_site, usos_stream = {}, {}
    for _r in posts_state.values():
        for _d in {urlparse(x).netloc.replace("www.", "") for x in _r.get("sites") or []}:
            usos_site[_d] = usos_site.get(_d, 0) + 1
        if _r.get("stream"):
            usos_stream[_r["stream"]] = usos_stream.get(_r["stream"], 0) + 1
    overrides = load(os.path.join(config.DATA_DIR, "overrides.json"), {})
    # chave do overrides.json pode ser a URL completa, o caminho (/2023/05/radio.html) ou o ID do post
    overrides = {(urlparse(k).path if k.startswith("http") else k): v
                 for k, v in overrides.items() if not k.startswith("_")}

    def ov_for(rec):
        return overrides.get(rec.get("path")) or overrides.get(rec.get("id")) or {}

    events = {"fixed": [], "recovered": [], "new_posts": []}
    connector = aiohttp.TCPConnector(limit=config.CONCURRENCY + 20, limit_per_host=config.PER_HOST * 2,
                                     ttl_dns_cache=600)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 1) posts do Blogger --------------------------------------------------------
        posts, total = await fetch_posts(session)
        log(f"{len(posts)} posts lidos do feed (total informado: {total})")
        if not posts:
            raise SystemExit("Feed vazio — verifique SITE_URL e se o feed do blog está ativo.")

        seen = set()
        need_page = []
        for p in posts:
            seen.add(p.id)
            rec = posts_state.get(p.id)
            if rec is None:
                rec = posts_state[p.id] = {"first_seen": today(), "fail_count": 0}
                events["new_posts"].append(p.path)
            elif rec.get("path") and rec["path"] != p.path:
                # mesmo post com URL nova (ex.: troca de frequência): a antiga precisa de 301
                register_old_url(state, rec["path"], rec, today(), new_path=p.path)
                events.setdefault("url_changed", []).append((rec["path"], p.path))
            rec.update(id=p.id, path=p.path, title=p.title, url=p.url, labels=p.labels, post_updated=p.updated)
            rec["contacts"] = extract_contacts(p.content)
            loc = extract_location(p.content, p.title, p.path)
            rec["loc"] = list(loc) if loc else None
            cands = extract_streams(p.content)
            if not cands:
                need_page.append(p)
            rec["_content_sites"] = extract_sites(p.content)
            rec["_cands"] = cands
            rec["_iframe"] = extract_live_iframe(p.content)
            rec["_content"] = p.content

        # URL de post mudou: leva junto os ajustes manuais do overrides.json
        moved = events.get("url_changed", [])
        if moved:
            ov_path = os.path.join(config.DATA_DIR, "overrides.json")
            raw = load(ov_path, {})
            changed = False
            for old, new in moved:
                for k in list(raw):
                    if k == old or (k.startswith("http") and urlparse(k).path == old):
                        raw[new] = raw.pop(k)
                        overrides[new] = overrides.pop(old, raw[new])
                        changed = True
            if changed:
                save(ov_path, raw)
                log("overrides.json: chaves atualizadas para as URLs novas")

        # Plano B: posts sem link no feed (feed resumido ou player montado por script)
        if need_page:
            log(f"{len(need_page)} posts sem link no feed; abrindo as páginas…")
            sem = asyncio.Semaphore(8)

            async def grab(p):
                async with sem:
                    body = await fetch_post_body(session, p.url)
                rec = posts_state[p.id]
                rec["_cands"] = extract_streams(body)
                rec["_content"] = (rec.get("_content") or "") + "\n" + (body or "")
                rec["_content_sites"] = rec["_content_sites"] or extract_sites(body)
            await asyncio.gather(*(grab(p) for p in need_page))

        # Iframes antigos (title="Ao vivo"): descobre o stream que está por trás deles
        iframe_recs = [posts_state[p] for p in seen if posts_state[p].get("_iframe")]
        if iframe_recs:
            log(f"{len(iframe_recs)} posts com iframe 'Ao vivo'; lendo os players…")
            sem = asyncio.Semaphore(10)
            week_ago_s = (datetime.now(BRT) - timedelta(days=7)).strftime("%Y-%m-%d")

            async def read_iframe(rec):
                src = rec["_iframe"]
                nested = extract_streams(unquote(src.split("?", 1)[1])) if "?" in src else []
                if nested:
                    found = nested[:5]          # player com o stream na URL (?url=...)
                elif stream_score(src):
                    found = [src]               # o iframe aponta direto para o stream
                elif (rec.get("iframe") == src and rec.get("iframe_streams")
                      and (rec.get("iframe_read") or "") >= week_ago_s):
                    found = rec["iframe_streams"]  # já lido nesta semana
                else:
                    async with sem:
                        page = await fetch_post_body_raw(session, src)
                    found = extract_streams(page)[:5]
                    rec["iframe_read"] = today()
                rec["iframe_streams"] = found
                # streams do iframe vêm primeiro (é o player daquele post); https antes de http
                found = sorted(found, key=lambda u: 0 if u.startswith("https") else 1)
                rec["_cands"] = found + [c for c in rec["_cands"] if c not in found]
            await asyncio.gather(*(read_iframe(r) for r in iframe_recs))

        # Player em popup ("Clique para ouvir ao vivo", window.open, página de player):
        # abre a página do popup e procura o stream lá dentro (1x por semana por post)
        popup_recs = [posts_state[p] for p in seen if not posts_state[p]["_cands"]]
        popup_recs = [r for r in popup_recs if extract_player_links(r.get("_content", ""), r["url"])]
        if popup_recs:
            log(f"{len(popup_recs)} posts com player em popup; abrindo os players…")
            sem_p = asyncio.Semaphore(10)
            sem_wb = asyncio.Semaphore(2)   # Internet Archive: poucas consultas por vez
            week_ago_p = (datetime.now(BRT) - timedelta(days=7)).strftime("%Y-%m-%d")
            month_ago_p = (datetime.now(BRT) - timedelta(days=30)).strftime("%Y-%m-%d")
            backup = {}
            for fn in config.POPUP_BACKUP_FILES:
                fp = os.path.join(config.DATA_DIR, fn)
                if os.path.exists(fp):
                    try:
                        backup.update(load_blogger_backup(fp))
                        log(f"backup {fn}: {len(backup)} páginas de player")
                    except Exception as e:
                        log(f"backup {fn} ilegível: {e}")

            def streams_in(html_text):
                m_b = BODY_RE.search(html_text or "")
                return extract_streams(m_b.group(1) if m_b else (html_text or ""))

            async def from_dead_popup(rec, link):
                """Popup de site desativado: backup do Blogger, depois Internet Archive (cache permanente)."""
                path = urlparse(link).path
                if path in backup:
                    rec["popup_source"] = "backup do Blogger"
                    return streams_in(backup[path])
                if rec.get("popup") == link and rec.get("popup_source") == "Internet Archive" and rec.get("popup_streams"):
                    return list(rec["popup_streams"])
                if not config.USE_WAYBACK or (rec.get("wayback_tried") or "") >= month_ago_p:
                    return []
                rec["wayback_tried"] = today()
                try:
                    async with sem_wb:
                        async with session.get(config.WAYBACK_API, params={"url": urlparse(link).netloc + path},
                                               timeout=aiohttp.ClientTimeout(total=30)) as r:
                            snap = ((await r.json(content_type=None)) or {}).get("archived_snapshots", {}).get("closest") or {}
                        if not snap.get("available") or not snap.get("timestamp"):
                            return []
                        raw = f"{config.WAYBACK_WEB}/{snap['timestamp']}id_/{link}"
                        page = await fetch_post_body_raw(session, raw)
                        await asyncio.sleep(1)
                except Exception:
                    return []
                found = streams_in(page)
                if found:
                    rec["popup_source"] = "Internet Archive"
                return found

            async def read_popup(rec):
                links = extract_player_links(rec.get("_content", ""), rec["url"])
                dead = (urlparse(links[0]).hostname or "").lower() in config.DEAD_POPUP_HOSTS
                if (rec.get("popup") == links[0] and rec.get("popup_streams")
                        and (dead or (rec.get("popup_read") or "") >= week_ago_p)):
                    rec["_cands"] = list(rec["popup_streams"])
                    return
                found = []
                for link in links:
                    if (urlparse(link).hostname or "").lower() in config.DEAD_POPUP_HOSTS:
                        found = await from_dead_popup(rec, link)
                        if found:
                            rec["popup"] = link
                            break
                        continue
                    if "?" in link:  # stream passado na própria URL do player (?url=..., ?radio=...)
                        found += [u for u in extract_streams(unquote(link.split("?", 1)[1])) if u not in found]
                    async with sem_p:
                        page = await fetch_post_body_raw(session, link)
                    # página de player feita no Blogger (ex.: radiosaovivo.radiocol.com.br):
                    # usa só o corpo do post, para não pegar rádios da barra lateral
                    m_body = BODY_RE.search(page or "")
                    if m_body:
                        page = m_body.group(1)
                    found += [u for u in extract_streams(page) if u not in found]
                    if not found:   # player montado dentro de um iframe da página do popup
                        for ifr in re.findall(r"""<iframe\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", page, re.I)[:2]:
                            async with sem_p:
                                sub = await fetch_post_body_raw(session, urljoin(link, ifr))
                            found += [u for u in extract_streams(sub) if u not in found]
                    if found:
                        rec["popup"] = link
                        break
                rec["popup_read"] = today()
                rec["popup_streams"] = found[:5]
                rec["_cands"] = sorted(found[:5], key=lambda u: 0 if u.startswith("https") else 1)
            await asyncio.gather(*(read_popup(r) for r in popup_recs))

        # posts apagados (ex.: emissora substituída por outra): a URL antiga precisa de 301
        for pid in [k for k in posts_state if k not in seen]:
            if not config.LIMIT_POSTS:
                old = posts_state.pop(pid)
                if old.get("path"):
                    register_old_url(state, old["path"], old, today())

        # 2) define o endereço atual de cada rádio ------------------------------------
        to_check = []
        for pid in seen:
            rec = posts_state[pid]
            cands = rec.pop("_cands")
            rec.pop("_content", None)
            rec["sites"] = rec.pop("_content_sites")
            iframe = rec.pop("_iframe", None)
            if iframe:
                rec["iframe"] = iframe
            else:
                for k in ("iframe", "iframe_streams", "iframe_read"):
                    rec.pop(k, None)
            new_post_stream = cands[0] if cands else None
            if new_post_stream != rec.get("post_stream"):
                # Você editou o post (ou é post novo): o link do post volta a valer
                editado = rec.get("post_stream") is not None
                if editado or not rec.get("stream"):
                    if editado and new_post_stream and rec.get("stream") != new_post_stream:
                        change(rec, new_post_stream, "post", "link editado no post")
                    rec["stream"], rec["source"] = new_post_stream, "post"
                    rec["fail_count"] = 0
                    for k in ("suggestion", "alerted", "provisorio", "confianca"):
                        rec.pop(k, None)
                # Link trocado à mão no post: daqui em diante ele prevalece. O robô continua testando e,
                # se cair, só SUGERE outro endereço por e-mail — nunca troca sozinho.
                if editado and new_post_stream:
                    rec["editado_em"] = today()
                else:
                    rec.pop("editado_em", None)
                rec["post_stream"] = new_post_stream
            rec["post_candidates"] = cands[:6]
            if not rec.get("stream") and new_post_stream:
                rec["stream"], rec["source"] = new_post_stream, "post"

            # Regra: o robô só troca o áudio por endereço do MESMO servidor (variações, https) ou achado no site
            # da emissora com confiança alta. Trocas antigas vindas do Radio Browser (diretório mundial: já
            # trouxe rádio estrangeira e de outra cidade) ou provisórias voltam ao link do post e viram sugestão.
            fonte = rec.get("source") or ""
            mesmo_servidor = fonte.startswith(("tudoradio", "radios.com.br", "https automático", "mesmo formato https", "variação", "mesmo servidor", "outro link do post",
                                               "Shoutcast", "post"))
            suspeito = fonte.startswith("Radio Browser") or (rec.get("provisorio") and not mesmo_servidor)
            if suspeito and not protegido(rec) and tem_prova(rec, rec.get("stream"), fonte, usos_site, usos_stream):
                rec.pop("provisorio", None)          # tem prova de ser a mesma rádio: fica
                suspeito = False
            if suspeito and not protegido(rec):
                rec["suggestion"] = {"url": rec.get("stream"), "source": fonte + " — não aplicado (só site oficial)",
                                     "score": min(rec.get("confianca") or 0, 0.59)}
                change(rec, rec.get("post_stream"), "post", "troca desfeita: não veio do site da emissora")
                for k in ("provisorio", "confianca", "alerted"):
                    rec.pop(k, None)
                rec["fail_count"] = 0
                if not rec.get("stream"):
                    rec.pop("stream", None)

            ov = ov_for(rec)
            pairs = contact_pairs(rec, ov)
            if pairs:
                rec["contact_overrides"] = pairs
            else:
                rec.pop("contact_overrides", None)
            if ov.get("ignore"):
                rec["status"], rec["reason"] = "ignorado", ov.get("note", "ignorado manualmente")
                continue
            # playlists, especiais, páginas institucionais e rádios encerradas saem do monitoramento
            tipo_post = classify_post(rec.get("title", ""))
            if not tipo_post and any(norm(l) in ("desativada", "desativado", "encerrada", "fora do ar")
                                     for l in rec.get("labels") or []):
                tipo_post = "encerrada"           # você marcou o post como desativado
            if tipo_post and not ov.get("stream"):
                rec["status"] = "ignorado"
                rec["reason"] = ("não é post de rádio (automático)" if tipo_post == "nao_radio"
                                 else "rádio encerrada/desativada, pelo título ou marcador (automático)")
                rec.pop("alerted", None)
                continue
            if ov.get("stream"):
                rec["stream"], rec["source"] = ov["stream"], "manual"
            elif rec.get("source") == "manual":  # override removido: volta ao link do post
                rec["stream"], rec["source"] = rec.get("post_stream"), "post"
            if not rec.get("stream"):
                rec["status"], rec["reason"] = "sem_link", "nenhum endereço de áudio no post"
                continue
            to_check.append(rec)

        # 3) testa todos os streams --------------------------------------------------
        checker = Checker(session)
        log(f"Testando {len(to_check)} streams…")
        results = await asyncio.gather(*(checker.check(r["stream"]) for r in to_check))
        failed = [i for i, res in enumerate(results) if not res.ok]
        if failed:
            log(f"{len(failed)} falharam; nova tentativa em 20 s…")
            await asyncio.sleep(20)
            for i in failed:
                checker.cache.pop(to_check[i]["stream"], None)
            retry = await asyncio.gather(*(checker.check(to_check[i]["stream"]) for i in failed))
            for i, res in zip(failed, retry):
                results[i] = res

        # Servidor que não responde para NENHUMA rádio (tempo esgotado / conexão recusada) e nunca tocou para o
        # robô: muitos servidores brasileiros bloqueiam acesso de fora do Brasil (o GitHub roda nos EUA).
        # Esses ficam como "bloqueio": não entram como fora do ar, o site não mostra aviso e o robô não troca o link.
        host_n, host_ok = {}, {}
        for rec, res in zip(to_check, results):
            h = urlparse(rec["stream"]).hostname or ""
            host_n[h] = host_n.get(h, 0) + 1
            host_ok[h] = host_ok.get(h, 0) + (1 if res.ok else 0)

        def parece_bloqueio(rec, res):
            h = urlparse(rec["stream"]).hostname or ""
            r = res.reason or ""
            if r in ("HTTP 401", "HTTP 403"):
                return True    # servidor recusa o robô (fora do Brasil / sem navegador); quem decide são os ouvintes
            if host_ok.get(h) or rec.get("last_ok"):
                return False
            if "tempo esgotado" in r or "Reset" in r or "Disconnected" in r:
                return True
            return "ConnectionRefused" in r and host_n.get(h, 0) >= 3

        tocou = await ler_tocou(session)
        if tocou:
            log(f"Avisos de ouvintes no Brasil: {len(tocou)} posts")

        # O endereço que o ROBÔ colocou falhou, mas o link original do post toca (no teste do robô ou para
        # ouvintes no Brasil)? Volta para o link do post. Ex.: Cultura FM Canguçu (sv12 -> ssl.hdradios.net).
        voltar = [i for i, (rec, res) in enumerate(zip(to_check, results))
                  if not res.ok and rec.get("post_stream") and rec["post_stream"] != rec["stream"]
                  and rec.get("source") != "manual"]
        if voltar:
            res_post = await asyncio.gather(*(checker.check(to_check[i]["post_stream"]) for i in voltar))
            for i, rp in zip(voltar, res_post):
                rec = to_check[i]
                br_post, _ = situacao_brasil(tocou.get(rec.get("id") or ""), rec["post_stream"], today())
                if rp.ok or br_post == "ok":
                    log(f"  link do post voltou a valer: {rec.get('title', rec['url'])}")
                    change(rec, rec["post_stream"], "post", "o endereço do robô falhou e o link do post toca")
                    for k in ("provisorio", "confianca", "suggestion"):
                        rec.pop(k, None)
                    if rp.ok:
                        results[i] = rp
        off = []
        for rec, res in zip(to_check, results):
            rec["last_check"] = today()
            br, quando = situacao_brasil(tocou.get(rec.get("id") or ""), rec["stream"], today()) if not res.ok else (None, None)
            if br == "ok":                     # o robô não alcança, mas tocou para ouvintes no Brasil
                if rec.get("status") in ("off", "bloqueio"):
                    events["recovered"].append(rec["url"])
                rec.update(status="ok", fail_count=0, br_ok=quando, http_only=rec["stream"].startswith("http://"),
                           reason=f"tocou para ouvintes no Brasil em {quando[8:10]}/{quando[5:7]} "
                                  f"(o robô, fora do Brasil, recebe: {res.reason})")
                rec.pop("suggestion", None)
                rec.pop("alerted", None)
                continue
            rec.pop("br_ok", None) if res.ok else None
            if br == "off" and not res.ok:     # falhou também para ouvintes no Brasil: fora do ar de verdade
                rec.update(status="off", reason=f"não tocou para ouvintes no Brasil (último erro {quando[8:10]}/{quando[5:7]})")
                rec["fail_count"] = max(rec.get("fail_count", 0) + 1, config.CONFIRM_RUNS)
                off.append(rec)
                continue
            if not res.ok and parece_bloqueio(rec, res):
                rec.update(status="bloqueio", fail_count=0,
                           reason=f"servidor não responde ao robô (fora do Brasil) — {res.reason}")
                rec.pop("alerted", None)
                continue
            if res.ok:
                if rec.get("status") in ("off", "bloqueio"):
                    events["recovered"].append(rec["url"])
                rec.update(status="ok", reason=res.reason, fail_count=0, last_ok=today())
                rec["http_only"] = res.http_only
                rec.pop("suggestion", None)
                rec.pop("alerted", None)
            else:
                rec.update(status="off", reason=res.reason)
                rec["fail_count"] = rec.get("fail_count", 0) + 1
                off.append(rec)

        # 3b) streams http:// que funcionam: tenta a versão https (o navegador bloqueia
        #     áudio http dentro de site https — "conteúdo misto")
        # (repete a tentativa no máximo 1x por semana para cada rádio)
        week_ago = (datetime.now(BRT) - timedelta(days=7)).strftime("%Y-%m-%d")
        http_ok = [r for r in to_check if r.get("http_only") and not protegido(r)
                   and (r.get("https_tried") or "") < week_ago]
        for r in http_ok:
            r["https_tried"] = today()
        if http_ok:
            log(f"{len(http_ok)} streams só em http; testando https…")

            # endereços https que já tocam, por servidor: mostram o formato https que aquele servidor aceita
            https_por_host = {}
            for r in posts_state.values():
                st_ = r.get("stream") or ""
                if r.get("status") == "ok" and st_.startswith("https://"):
                    https_por_host.setdefault(urlsplit(st_).hostname, []).append(st_)

            async def upgrade(rec):
                cands = [(v, None) for v in variants(rec["stream"]) if v.startswith("https")][:3]
                cands += [(v, "mesmo formato https de outra rádio do mesmo servidor")
                          for v in formato_vizinho(rec["stream"], https_por_host) if v not in {c[0] for c in cands}]
                for v, origem in cands:
                    res = await checker.check(v)
                    if origem and not (res.ok and (res.content_type or "").split(";")[0].strip().lower().startswith("audio/")):
                        continue                     # formato copiado de outra rádio: só com Content-Type de áudio
                    if audio_ok(v, res):
                        fonte = origem or ("Shoutcast em https (mesmo servidor e porta)" if SHOUT_HTTPS_RE.match(v)
                                           else "https automático")
                        events["fixed"].append({"url": rec["url"], "title": rec["title"], "from": rec["stream"],
                                                "to": v, "source": fonte})
                        change(rec, v, fonte)
                        rec["http_only"] = False
                        return
            await asyncio.gather(*(upgrade(r) for r in http_ok))

        # 3c) posts com iframe que ainda só têm http: procura uma versão https em
        #     outras fontes (site oficial, Radio Browser) para poder trocar pelo player
        finder = Finder(session, checker)
        if config.USE_TUDORADIO:
            try:
                finder.tudo = await tudoradio.atualizar(finder._get_text, today(), log)
            except Exception as ex:
                log(f"tudoradio indisponível: {ex}")
        if config.USE_RADIOSCOMBR:
            try:
                finder.rcb = await radioscombr.atualizar(finder._get_text, today(), log)
            except Exception as ex:
                log(f"radios.com.br indisponível: {ex}")
        # sites usados por 3+ posts são de rede: só vale o player da praça (cidade/frequência) do post
        usos = {}
        for r in posts_state.values():
            for d in {urlparse(x).netloc.replace("www.", "") for x in r.get("sites") or []}:
                usos[d] = usos.get(d, 0) + 1
        finder.rede = {d for d, n in usos.items() if n >= 3}
        # todas as rádios que só tocam em http (o player do site é https e não toca http): busca completa
        # exigindo https — site da emissora, tudoradio, radios.com.br e Radio Browser (1x por semana)
        need_https = [r for r in to_check if r.get("status") == "ok" and r.get("http_only")
                      and (r.get("stream") or "").startswith("http://") and not protegido(r)
                      and (r.get("https_search") or "") < week_ago]
        if need_https:
            log(f"{len(need_https)} rádios só com http; procurando versão https…")
            sem_h = asyncio.Semaphore(15)

            async def find_https(rec):
                rec["https_search"] = today()
                rec["_need_https"] = True
                async with sem_h:
                    found = await finder.find(rec)
                rec.pop("_need_https", None)
                if not found:
                    return
                if found["confidence"] == "high":
                    events["fixed"].append({"url": rec["url"], "title": rec["title"], "from": rec["stream"],
                                            "to": found["url"], "source": found["source"] + " (https p/ player)"})
                    change(rec, found["url"], found["source"])
                    rec["http_only"] = False
                else:
                    rec["suggestion"] = {k: found[k] for k in ("url", "source", "score") if k in found}
            await asyncio.gather(*(find_https(r) for r in need_https))

        # 3d) site do post das rádios com problema (1x por semana): mudou de endereço ou saiu do ar?
        n_sites, n_fora = await verificar_sites(session, list(posts_state.values()), today())
        if n_sites:
            log(f"Sites das emissoras: {n_sites} verificados, {n_fora} fora do ar")

        # 4) procura substitutos para os que caíram ---------------------------------
        # Fila de busca, revezada: cada post é pesquisado no máximo 1x por semana (quem acabou de cair entra
        # na hora), e a cota do dia é dividida entre "fora do ar" e "sem link" — antes os 1.000+ fora do ar
        # ocupavam toda a cota e os posts sem link nunca eram pesquisados.
        def dias_desde(d):
            return 999 if not d else (date.fromisoformat(today()) - date.fromisoformat(d)).days

        fila_off = [r for r in off if r.get("fail_count", 0) <= config.CONFIRM_RUNS
                    or dias_desde(r.get("search_tried")) >= 7]
        # "não testáveis daqui" e endereços provisórios (sugestão < alta confiança): procura no site 1x/semana
        fila_off += [r for r in posts_state.values()
                     if (r.get("status") == "bloqueio" or (r.get("status") == "ok" and r.get("provisorio")))
                     and r.get("sites") and not protegido(r) and r not in off
                     and dias_desde(r.get("search_tried")) >= 7]
        fila_off.sort(key=lambda r: (r.get("fail_count", 0) > config.CONFIRM_RUNS, r.get("search_tried") or ""))
        # posts sem nenhum endereço de áudio, mas com site oficial: procura o player no site
        sem_link = [r for r in posts_state.values()
                    if r.get("status") == "sem_link" and r.get("sites") and not protegido(r)
                    and dias_desde(r.get("site_tried")) >= 7]
        sem_link.sort(key=lambda r: r.get("site_tried") or "")
        cota_sem_link = min(len(sem_link), max(config.MAX_SEARCH * 2 // 5, config.MAX_SEARCH - len(fila_off)))
        search = fila_off[: config.MAX_SEARCH - cota_sem_link] + sem_link[:cota_sem_link]
        for r in search:
            r["site_tried" if r.get("status") == "sem_link" else "search_tried"] = today()
        n_sl = sum(1 for r in search if r.get("status") == "sem_link")
        log(f"{len(off)} fora do ar, {len(fila_off)} na fila (fora do ar, não testáveis e provisórios) e "
            f"{len(sem_link)} sem link com site; procurando para {len(search)} ({len(search) - n_sl} da fila + {n_sl} sem link)…")
        sem = asyncio.Semaphore(15)

        def aplicar_achado(rec, found):
            alta = found["confidence"] == "high"
            score = found.get("score") or 0
            events["fixed"].append({"url": rec["url"], "title": rec["title"], "from": rec.get("stream"),
                                    "to": found["url"], "source": found["source"] + ("" if alta else f" — provisório ({score})")})
            change(rec, found["url"], found["source"])
            rec["confianca"] = score
            if alta:
                rec.pop("provisorio", None)
            else:
                rec["provisorio"] = True                 # o robô segue procurando algo melhor no site da emissora
            if found.get("nao_testavel"):
                rec.update(status="bloqueio", fail_count=0,
                           reason="endereço do player do site da emissora; o servidor não responde ao robô (fora do Brasil)")
            else:
                rec.update(status="ok", reason="corrigido automaticamente" + ("" if alta else " (provisório)"),
                           fail_count=0, last_ok=today(), http_only=found["url"].startswith("http://"))
            rec.pop("suggestion", None)
            rec.pop("alerted", None)

        async def fix(rec):
            if rec.get("source") == "manual":
                return
            if protegido(rec):
                # link editado à mão no post: fora do ar, só procura uma SUGESTÃO para o e-mail
                if rec.get("status") == "off":
                    async with sem:
                        found = await finder.find(rec)
                    if found:
                        rec["suggestion"] = {k: found[k] for k in ("url", "source", "score") if k in found}
                return
            atual_ok = rec.get("status") in ("ok", "bloqueio")      # já tem endereço: só troca se for melhor
            if rec.get("status") == "bloqueio":
                # "não testável daqui": antes do site, testa variações do MESMO endereço (ex.: nó numerado da
                # StreamTheWorld que morreu -> redirecionador oficial; proxy AzuraCast -> porta direta).
                # Se uma delas toca para o robô, é o mesmo stream e deixa de ser bloqueio.
                # endereço https não vira http (o player do site bloquearia o áudio — "conteúdo misto")
                if (rec.get("stream") or "").startswith("https"):
                    rec["_need_https"] = True
                async with sem:
                    try:
                        found = await finder.from_variants(rec)
                    except Exception:
                        found = None
                rec.pop("_need_https", None)
                if found and found["confidence"] == "high":
                    aplicar_achado(rec, found)
                    return
            async with sem:
                if atual_ok:
                    found = await finder.from_official_site(rec)
                    if not found and rec.get("status") == "bloqueio":
                        found = await finder.from_tudoradio(rec)        # outro servidor da mesma rádio
                else:
                    found = await finder.find(rec)
            if not found:
                if not atual_ok:
                    rec.pop("suggestion", None)
                    # busca completa sem nada: site, tudoradio, radios.com.br e Radio Browser
                    if rec.get("busca_vazia_em") != today():
                        rec["buscas_vazias"] = rec.get("buscas_vazias", 0) + 1
                        rec["busca_vazia_em"] = today()
                return
            rec.pop("buscas_vazias", None)
            rec.pop("busca_vazia_em", None)
            score = found.get("score") or 0
            if atual_ok:
                # provisório / não testável: troca só por achado do site da emissora com confiança maior
                if found["confidence"] == "high" and score > (rec.get("confianca") or 0):
                    aplicar_achado(rec, found)
                return
            alta = found["confidence"] == "high" and not found["source"].startswith("Radio Browser")
            if alta or tem_prova(rec, found["url"], found["source"], usos_site, usos_stream):
                aplicar_achado(rec, dict(found, confidence="high"))
                await adotar_site(session, rec, found.get("site"))   # site informado pelo diretório
            else:                                   # Radio Browser / baixa confiança: só sugestão no relatório
                rec["suggestion"] = {k: found[k] for k in ("url", "source", "score") if k in found}

        await asyncio.gather(*(fix(r) for r in search))

        # 4b) variações do MESMO endereço, todo dia, para todos os fora do ar / não testáveis que ficaram fora da
        # busca completa (que é semanal). É barato (só testes de endereço) e faz as regras novas de variants()
        # valerem já no dia seguinte, em vez de esperar até 7 dias. Link editado à mão nunca entra aqui.
        ids_busca = {id(r) for r in search}
        diaria = [r for r in posts_state.values() if r.get("status") in ("off", "bloqueio") and r.get("stream")
                  and not protegido(r) and id(r) not in ids_busca
                  # servidor que só esgota o tempo: as variações no mesmo servidor também esgotariam (fica na semanal)
                  and not (r.get("status") == "bloqueio" and "tempo esgotado" in (r.get("reason") or ""))]

        async def so_variacoes(rec):
            # endereço https não vira http (o player do site bloquearia o áudio — "conteúdo misto")
            if rec.get("status") == "bloqueio" and rec["stream"].startswith("https"):
                rec["_need_https"] = True
            async with sem:
                try:
                    found = await finder.from_variants(rec)
                except Exception:
                    found = None
            rec.pop("_need_https", None)
            if found and found["confidence"] == "high":
                aplicar_achado(rec, found)
                return True
            return False
        if diaria:
            n_var = sum(await asyncio.gather(*(so_variacoes(r) for r in diaria)))
            log(f"Variações do próprio endereço (diário): {n_var} corrigidas de {len(diaria)} fora do ar/não testáveis")
        if finder.nav.usados:
            log(f"Navegador abriu {finder.nav.usados} sites")
        await finder.nav.fechar()

        # 5) URLs antigas: confere o redirecionamento 301 ---------------------------
        if state.get("old_urls"):
            log(f"Conferindo {len(state['old_urls'])} URLs antigas (redirecionamento 301)…")
            await check_old_urls(session, state, posts_state, today())

        # 6) WhatsApp no site oficial (cada post 1x por semana) ----------------------
        if config.USE_OFFICIAL_SITE:
            await scan_site_whatsapp(session, list(posts_state.values()), ov_for, today(), finder._get_text,
                                     force=bool(config.LIMIT_POSTS), rede=finder.rede)
            for rec in posts_state.values():         # inclui as trocas automáticas de WhatsApp
                if rec.get("status") == "ignorado":
                    continue
                pairs = contact_pairs(rec, ov_for(rec))
                if pairs:
                    rec["contact_overrides"] = pairs
                else:
                    rec.pop("contact_overrides", None)

    # 7) saídas -------------------------------------------------------------------
    state["updated"] = now()
    state["site"] = config.SITE_URL
    save(state_path, state)
    write_public(posts_state)
    report = build_report(posts_state, events, today(), state.get("old_urls", {}))
    save(os.path.join(config.DATA_DIR, "last_report.json"), report["summary"])
    send_email(report)
    save(state_path, state)  # grava marcações de "alertado"
    log(f"Concluído em {int(time.time() - t0)} s — {json.dumps(report['summary'], ensure_ascii=False)}")


if __name__ == "__main__":
    asyncio.run(main())
