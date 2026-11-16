"""Execução diária: python -m monitor.run"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta

import aiohttp
import re
from urllib.parse import unquote, urljoin, urlparse

from . import config
from .blogger import BODY_RE, fetch_post_body, fetch_post_body_raw, fetch_posts, load_blogger_backup
from .checker import Checker
from .extract import (classify_post, extract_contacts, extract_live_iframe, extract_player_links, extract_sites,
                      extract_streams, stream_score)
from .extras import check_old_urls, contact_pairs, register_old_url, scan_site_whatsapp
from .finder import Finder, variants
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


def change(rec, new_url, source, note=""):
    hist = rec.setdefault("history", [])
    hist.append({"date": today(), "from": rec.get("stream"), "to": new_url, "source": source, "note": note})
    rec["history"] = hist[-6:]
    rec["stream"] = new_url
    rec["source"] = source


async def main():
    t0 = time.time()
    state_path = os.path.join(config.DATA_DIR, "state.json")
    state = load(state_path, {"posts": {}})
    posts_state = state["posts"]
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
                if rec.get("post_stream") is not None or not rec.get("stream"):
                    rec["stream"], rec["source"] = new_post_stream, "post"
                    rec["fail_count"] = 0
                    rec.pop("suggestion", None)
                    rec.pop("alerted", None)
                rec["post_stream"] = new_post_stream
            rec["post_candidates"] = cands[:6]
            if not rec.get("stream") and new_post_stream:
                rec["stream"], rec["source"] = new_post_stream, "post"

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
            if tipo_post and not ov.get("stream"):
                rec["status"] = "ignorado"
                rec["reason"] = ("não é post de rádio (automático)" if tipo_post == "nao_radio"
                                 else "rádio encerrada, segundo o título (automático)")
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

        off = []
        for rec, res in zip(to_check, results):
            rec["last_check"] = today()
            if res.ok:
                if rec.get("status") == "off":
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
        http_ok = [r for r in to_check if r.get("http_only") and r.get("source") != "manual"
                   and (r.get("https_tried") or "") < week_ago]
        for r in http_ok:
            r["https_tried"] = today()
        if http_ok:
            log(f"{len(http_ok)} streams só em http; testando https…")

            async def upgrade(rec):
                for v in [v for v in variants(rec["stream"]) if v.startswith("https")][:2]:
                    if (await checker.check(v)).ok:
                        events["fixed"].append({"url": rec["url"], "title": rec["title"], "from": rec["stream"],
                                                "to": v, "source": "https automático"})
                        change(rec, v, "https automático")
                        rec["http_only"] = False
                        return
            await asyncio.gather(*(upgrade(r) for r in http_ok))

        # 3c) posts com iframe que ainda só têm http: procura uma versão https em
        #     outras fontes (site oficial, Radio Browser) para poder trocar pelo player
        finder = Finder(session, checker)
        need_https = [r for r in to_check if (r.get("iframe") or r.get("popup")) and r.get("status") == "ok"
                      and r.get("http_only") and r.get("source") != "manual"
                      and (r.get("https_search") or "") < week_ago]
        if need_https:
            log(f"{len(need_https)} iframes/popups só com http; procurando versão https…")
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

        # 4) procura substitutos para os que caíram ---------------------------------
        off.sort(key=lambda r: -r.get("fail_count", 0))
        search = off[: config.MAX_SEARCH]
        log(f"{len(off)} fora do ar; procurando substitutos para {len(search)}…")
        sem = asyncio.Semaphore(15)

        async def fix(rec):
            async with sem:
                found = await finder.find(rec)
            if not found:
                rec.pop("suggestion", None)
                return
            if found["confidence"] == "high" and rec.get("source") != "manual":
                events["fixed"].append({"url": rec["url"], "title": rec["title"], "from": rec["stream"],
                                        "to": found["url"], "source": found["source"]})
                change(rec, found["url"], found["source"])
                rec.update(status="ok", reason="corrigido automaticamente", fail_count=0, last_ok=today(),
                           http_only=found["url"].startswith("http://"))
                rec.pop("suggestion", None)
                rec.pop("alerted", None)
            else:
                rec["suggestion"] = {k: found[k] for k in ("url", "source", "score") if k in found}
        await asyncio.gather(*(fix(r) for r in search))

        # 5) URLs antigas: confere o redirecionamento 301 ---------------------------
        if state.get("old_urls"):
            log(f"Conferindo {len(state['old_urls'])} URLs antigas (redirecionamento 301)…")
            await check_old_urls(session, state, posts_state, today())

        # 6) WhatsApp no site oficial (cada post 1x por semana) ----------------------
        if config.USE_OFFICIAL_SITE:
            await scan_site_whatsapp(session, list(posts_state.values()), ov_for, today(), finder._get_text,
                                     force=bool(config.LIMIT_POSTS))

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
