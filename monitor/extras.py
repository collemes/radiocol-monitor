"""Troca de URL dos posts (redirecionamento 301) e contatos (WhatsApp / telefone)."""
import asyncio
import hashlib
from urllib.parse import urlparse

import aiohttp

from . import config
from .extract import digits, extract_contacts, same_number
from .finder import similarity


# ---------------------------------------------------------------- URLs antigas
def register_old_url(state, path, rec, today, new_path=None):
    olds = state.setdefault("old_urls", {})
    if path in olds:
        return
    olds[path] = {"since": today, "title": rec.get("title", ""), "post_id": rec.get("id"),
                  "new_path": new_path, "labels": rec.get("labels", []), "status": "pendente"}


def suggest_target(old, posts):
    """Sugere o post novo que substitui o antigo (mesmo post com URL nova ou título parecido)."""
    if old.get("new_path"):
        return old["new_path"], 1.0
    best, best_sc = None, 0.0
    old_labels = set(old.get("labels") or [])
    for r in posts.values():
        sc = similarity(old.get("title", ""), r.get("title", ""))
        if old_labels and old_labels & set(r.get("labels") or []):
            sc += 0.1   # mesma cidade/estado/gênero
        if sc > best_sc:
            best, best_sc = r.get("path"), sc
    return (best, round(best_sc, 2)) if best_sc >= 0.5 else (None, 0)


async def check_old_urls(session, state, posts, today):
    """Confere se cada URL antiga responde 301 (redirecionamento personalizado do Blogger)."""
    olds = state.get("old_urls", {})
    pend = [(p, o) for p, o in olds.items() if o.get("status") != "ok"]
    sem = asyncio.Semaphore(8)

    async def one(path, o):
        url = config.SITE_URL + path
        try:
            async with sem, session.get(url, allow_redirects=False,
                                        headers={"User-Agent": config.USER_AGENT},
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
                status, loc = r.status, r.headers.get("Location", "")
        except Exception as e:
            o["last_error"] = type(e).__name__
            return
        o["last_check"] = today
        if status in (301, 308):
            o.update(status="ok", target=urlparse(loc).path or loc, http=status)
        elif status in (302, 307):
            o.update(status="temporario", target=urlparse(loc).path or loc, http=status)
        elif status == 200:
            o.update(status="ok", http=200, note="a URL antiga voltou a existir")
        else:
            o.update(status="sem_redirect", http=status)
            o["suggested"], o["suggested_score"] = suggest_target(o, posts)
    await asyncio.gather(*(one(p, o) for p, o in pend))
    # limpa: confirmados há 90 dias e pendências antigas (180 dias)
    for p in [p for p, o in olds.items()
              if (o.get("status") == "ok" and o.get("last_check", "") < _days_ago(today, 90))
              or o.get("since", "") < _days_ago(today, 180)]:
        del olds[p]


def _days_ago(today, n):
    from datetime import date, timedelta
    return (date.fromisoformat(today) - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- contatos
def contact_pairs(rec, ov):
    """Lista de [antigo, novo, tipo] a partir do overrides.json.
    Aceita "whatsapp": "5584999990000" (troca o WhatsApp do post) ou um mapa
    {"antigo": "novo", ...} para casos com vários números ou números sem link."""
    pairs = []
    post = rec.get("contacts") or {}
    for kind, key in (("w", "whatsapp"), ("t", "telefone")):
        val = ov.get(key)
        if not val:
            continue
        if isinstance(val, dict):
            for old, new in val.items():
                pairs.append([digits(old), digits(new), kind])
        else:
            olds = post.get(key) or []
            if olds:
                pairs.append([olds[0], digits(val), kind])
    return [p for p in pairs if p[0] and p[1] and not same_number(p[0], p[1])]


def todays_slice(pid: str) -> bool:
    """Cada post tem o site oficial lido 1x por semana (1/7 dos posts por dia)."""
    from datetime import date
    return int(hashlib.md5(pid.encode()).hexdigest(), 16) % 7 == date.today().weekday()


async def scan_site_whatsapp(session, recs, overrides_for, today, fetch_text, force=False):
    """Procura WhatsApp no site oficial; se for diferente do post, vira sugestão no e-mail."""
    sem = asyncio.Semaphore(10)

    async def one(rec):
        if not rec.get("sites") or (not force and not todays_slice(rec["id"])):
            return
        if overrides_for(rec).get("whatsapp"):
            rec.pop("contact_suggestion", None)
            return
        async with sem:
            page = await fetch_text(rec["sites"][0])
        site_w = extract_contacts(page)["whatsapp"]
        post_w = (rec.get("contacts") or {}).get("whatsapp") or []
        if not site_w or not post_w or any(same_number(s, p) for s in site_w for p in post_w):
            rec.pop("contact_suggestion", None)
            return
        sug = {"kind": "whatsapp", "post": post_w[0], "site": site_w[0], "url": rec["sites"][0]}
        if (rec.get("contact_suggestion") or {}).get("site") != sug["site"]:
            sug["since"] = today
            rec["contact_suggestion"] = sug
    await asyncio.gather(*(one(r) for r in recs))
