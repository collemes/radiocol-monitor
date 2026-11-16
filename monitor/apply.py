"""Aplica uma solicitação aprovada na planilha (disparada pelo Apps Script).

Entrada: variável REQUEST_JSON (client_payload do repository_dispatch), por ex.:
  {"id": "R-20261116-0042", "tipo": "atualizar", "post": "https://www.radiocol.com.br/2023/05/x.html",
   "stream": "https://...", "whatsapp": "84 98888-1111", "telefone": "84 3421-9999", "nota": "..."}
Uso: python -m monitor.apply
"""
import asyncio
import json
import os
import re
import sys
from urllib.parse import urlparse

import aiohttp

from . import config
from .checker import Checker
from .extract import digits
from .extras import contact_pairs
from .notify import write_public
from .run import load, save, today

ALLOWED = {"id", "tipo", "post", "post_id", "stream", "whatsapp", "telefone", "nota"}


def fail(msg):
    print(f"ERRO: {msg}")
    _summary(f"❌ {msg}")
    sys.exit(1)


def _summary(text):
    gh = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")


def validate(req):
    req = {k: v for k, v in req.items() if k in ALLOWED and v not in (None, "")}
    for k, v in req.items():
        if not isinstance(v, str) or len(v) > 500:
            fail(f"campo inválido: {k}")
    if req.get("stream"):
        u = req["stream"].strip()
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc or re.search(r"[\s<>\"']", u):
            fail(f"endereço de stream inválido: {u!r}")
        req["stream"] = u
    for k in ("whatsapp", "telefone"):
        if req.get(k):
            d = digits(req[k])
            if not 10 <= len(d) <= 13:
                fail(f"{k} inválido: {req[k]!r} (use DDD + número)")
            req[k] = d
    return req


def find_post(posts, req):
    if req.get("post_id") and req["post_id"] in posts:
        return req["post_id"], posts[req["post_id"]]
    path = urlparse(req.get("post", "")).path if req.get("post") else ""
    if path:
        path = path.split("?")[0]
        for pid, r in posts.items():
            if r.get("path") == path:
                return pid, r
        # URL antiga com redirecionamento já conhecido
        state = load(os.path.join(config.DATA_DIR, "state.json"), {})
        old = (state.get("old_urls") or {}).get(path) or {}
        target = old.get("target") or old.get("new_path")
        for pid, r in posts.items():
            if target and r.get("path") == target:
                return pid, r
    return None, None


async def main():
    try:
        req = json.loads(os.environ.get("REQUEST_JSON") or "{}")
    except json.JSONDecodeError:
        fail("REQUEST_JSON não é um JSON válido")
    if not isinstance(req, dict):
        fail("REQUEST_JSON deve ser um objeto")
    req = validate(req)
    tipo = (req.get("tipo") or "atualizar").lower()

    state_path = os.path.join(config.DATA_DIR, "state.json")
    state = load(state_path, {"posts": {}})
    posts = state["posts"]
    pid, rec = find_post(posts, req)
    if not rec:
        fail(f"post não encontrado: {req.get('post') or req.get('post_id')}")

    ov_path = os.path.join(config.DATA_DIR, "overrides.json")
    raw = load(ov_path, {})
    # localiza a chave já existente deste post (URL, caminho ou ID) para não duplicar
    key = next((k for k in raw if not k.startswith("_") and
                ((urlparse(k).path if k.startswith("http") else k) in (rec["path"], pid))), rec["path"])
    entry = raw.get(key, {})
    msgs = []

    if tipo.startswith("remov"):
        entry.update(ignore=True, note=f"remoção solicitada ({req.get('id', '')}) em {today()}")
        msgs.append("marcado como removido: o robô deixa de monitorar (apague ou despublique o post no Blogger)")
    else:
        if req.get("stream"):
            async with aiohttp.ClientSession() as s:
                res = await Checker(s).check(req["stream"])
            entry["stream"] = req["stream"]
            rec.update(stream=req["stream"], source="manual", fail_count=0, last_check=today(),
                       status="ok" if res.ok else "off", reason=res.reason, http_only=res.http_only)
            rec.pop("suggestion", None)
            rec.pop("alerted", None)
            msgs.append(f"stream {'OK' if res.ok else 'NÃO respondeu (' + res.reason + ')'}: {req['stream']}")
            if res.http_only:
                msgs.append("atenção: stream só em http — pode ser bloqueado no site https")
        post_contacts = rec.get("contacts") or {}
        for k, label in (("whatsapp", "WhatsApp"), ("telefone", "telefone")):
            if req.get(k):
                entry[k] = req[k]
                if not post_contacts.get(k):
                    msgs.append(f"{label} gravado, mas o post não tem link de {label} para trocar — "
                                f"inclua no post ou use o formato {{\"antigo\": \"novo\"}}")
                else:
                    msgs.append(f"{label}: {post_contacts[k][0]} → {req[k]}")
        entry.pop("ignore", None)
        if not msgs:
            fail("nada para aplicar (sem stream, WhatsApp ou telefone)")
    entry["solicitacao"] = req.get("id", "")
    entry["aprovado_em"] = today()
    raw[key] = entry
    save(ov_path, raw)

    pairs = contact_pairs(rec, entry)
    if pairs:
        rec["contact_overrides"] = pairs
    if entry.get("ignore"):
        rec.update(status="ignorado", reason=entry["note"])
    save(state_path, state)
    write_public(posts)

    text = f"✅ Solicitação {req.get('id', '')} aplicada em **{rec.get('title')}** ({rec.get('url')})\n\n" + \
           "\n".join(f"- {m}" for m in msgs)
    print(text)
    _summary(text)


if __name__ == "__main__":
    asyncio.run(main())
