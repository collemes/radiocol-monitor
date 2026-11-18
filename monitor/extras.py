"""Troca de URL dos posts (redirecionamento 301) e contatos (WhatsApp / telefone)."""
import asyncio
import hashlib
import html
import re
from urllib.parse import urlparse

import aiohttp

from . import config
from .extract import WA_RE, digits, same_number
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
    if not ov.get("whatsapp") and rec.get("contact_auto") and not rec.get("wa_editado_em"):
        pairs.append(list(rec["contact_auto"]))   # WhatsApp do site oficial (confiança > 0,9)
    return [p for p in pairs if p[0] and p[1] and not same_number(p[0], p[1])]


def todays_slice(pid: str) -> bool:
    """Cada post tem o site oficial lido 1x por semana (1/7 dos posts por dia)."""
    from datetime import date
    return int(hashlib.md5(pid.encode()).hexdigest(), 16) % 7 == date.today().weekday()


# DDDs válidos por estado (para conferir se o número do site é da mesma região da rádio)
DDD_UF = {
    "sp": "11 12 13 14 15 16 17 18 19", "rj": "21 22 24", "es": "27 28", "mg": "31 32 33 34 35 37 38",
    "pr": "41 42 43 44 45 46", "sc": "47 48 49", "rs": "51 53 54 55", "df": "61", "go": "62 64", "to": "63",
    "mt": "65 66", "ms": "67", "ac": "68", "ro": "69", "ba": "71 73 74 75 77", "se": "79", "pe": "81 87",
    "al": "82", "pb": "83", "rn": "84", "ce": "85 88", "pi": "86 89", "pa": "91 93 94", "am": "92 97",
    "rr": "95", "ap": "96", "ma": "98 99",
}
DDD_VALIDOS = {d for v in DDD_UF.values() for d in v.split()}
ESTUDIO_RE = re.compile(r"est[uú]dio|ao vivo|locu[cç]|locutor|pe[cç]a sua|pedido|participe|ouvinte|"
                        r"fale (com|conosco|com a)|whats ?app d[ao] r[aá]dio|programa[cç][aã]o|interativ", re.I)
COMERCIAL_RE = re.compile(r"comercial|anuncie|an[uú]ncio|publicidade|vendas|departamento|financeiro|"
                          r"jornalismo|reda[cç][aã]o|ag[eê]ncia|suporte|desenvolvid|criado por|site por|hospedagem", re.I)
_TAGS_RE = re.compile(r"<[^>]+>")


def _nacional(d):
    d = digits(d)
    return d[2:] if len(d) in (12, 13) and d.startswith("55") else d


def numero_valido(d):
    """(ddd, celular?) de um número brasileiro válido, ou None (0800, zeros, DDD inexistente…)."""
    n = _nacional(d)
    if len(n) not in (10, 11) or n[:2] not in DDD_VALIDOS or len(set(n[2:])) <= 2:
        return None
    if len(n) == 11:
        return (n[:2], True) if n[2] == "9" else None
    return (n[:2], n[2] in "6789")   # 8 dígitos começando com 6-9: celular antigo (sem o 9º dígito)


def whatsapp_no_site(page):
    """[(número, texto em volta)] dos links de WhatsApp da página."""
    out = []
    texto = html.unescape(page or "")
    for m in WA_RE.finditer(texto):
        num = digits(m.group(1))
        trecho = _TAGS_RE.sub(" ", texto[max(0, m.start() - 160): m.end() + 120])
        out.append((num, re.sub(r"\s+", " ", trecho)))
    return out


def confianca_whatsapp(num, achados, rec, rede=False, visto_antes=False):
    """0..1: quanto o número do site parece ser o WhatsApp do estúdio desta rádio."""
    v = numero_valido(num)
    if not v:
        return 0.0
    ddd, celular = v
    if not celular:
        sc = 0.25                          # fixo pode ter WhatsApp Business, mas é menos provável
    elif len(_nacional(num)) == 10:
        sc = 0.4                           # celular no formato antigo (sem o 9º dígito): número desatualizado?
    else:
        sc = 0.5
    post_w = (rec.get("contacts") or {}).get("whatsapp") or []
    ddds_post = {numero_valido(p)[0] for p in post_w if numero_valido(p)}
    uf = (rec.get("loc") or [None, None])[1] or ""
    if ddd in ddds_post or (not ddds_post and ddd in DDD_UF.get(uf, "").split()):
        sc += 0.2
    elif ddd not in DDD_UF.get(uf, "").split():
        sc -= 0.3                          # outra região: provavelmente agência/rede
    distintos = {_nacional(n)[-8:] for n, _ in achados if numero_valido(n)}
    if len(distintos) == 1:
        sc += 0.15
    contexto = " ".join(t for n, t in achados if _nacional(n)[-8:] == _nacional(num)[-8:])
    if ESTUDIO_RE.search(contexto):
        sc += 0.1
    if COMERCIAL_RE.search(contexto):
        sc -= 0.3
    if visto_antes:
        sc += 0.1                          # mesmo número na leitura da semana anterior
    if rede:
        sc = min(sc, 0.6)                  # site usado por várias rádios (rede/portal)
    return round(max(0.0, min(sc, 1.0)), 2)


async def scan_site_whatsapp(session, recs, overrides_for, today, fetch_text, force=False, rede=None):
    """Procura WhatsApp no site oficial. Se for diferente do post:
    - confiança > config.WHATS_AUTO_MIN (0,9): o site passa a mostrar o número do site oficial;
    - senão: vira sugestão no e-mail/relatório.
    Número de WhatsApp que você trocou à mão no post nunca é substituído automaticamente."""
    sem = asyncio.Semaphore(10)
    rede = rede or set()

    async def one(rec):
        post_w = (rec.get("contacts") or {}).get("whatsapp") or []
        # você trocou o WhatsApp no post: vale o seu (e cancela a troca automática anterior)
        visto = rec.get("wa_post_visto")
        if post_w and visto and not same_number(post_w[0], visto):
            rec["wa_editado_em"] = today
            rec.pop("contact_auto", None)
            rec.pop("contact_suggestion", None)
        if post_w:
            rec["wa_post_visto"] = post_w[0]
        if not rec.get("sites") or (not force and not todays_slice(rec["id"])):
            return
        if overrides_for(rec).get("whatsapp"):
            rec.pop("contact_suggestion", None)
            rec.pop("contact_auto", None)
            return
        async with sem:
            page = await fetch_text(rec["sites"][0])
        if not page:
            return                                    # site fora do ar: mantém o que já havia
        achados = whatsapp_no_site(page)
        site_w = [n for n in dict.fromkeys(n for n, _ in achados) if numero_valido(n)]
        if not site_w or not post_w or any(same_number(s, p) for s in site_w for p in post_w):
            rec.pop("contact_suggestion", None)
            rec.pop("contact_auto", None)
            return
        dominio = urlparse(rec["sites"][0]).netloc.replace("www.", "")
        anterior = rec.get("contact_suggestion") or {}
        melhor, melhor_sc = None, -1.0
        for n in site_w:
            sc = confianca_whatsapp(n, achados, rec, rede=dominio in rede,
                                    visto_antes=same_number(n, anterior.get("site") or ""))
            if sc > melhor_sc:
                melhor, melhor_sc = n, sc
        sug = {"kind": "whatsapp", "post": post_w[0], "site": melhor, "url": rec["sites"][0], "score": melhor_sc,
               "since": anterior.get("since") if same_number(melhor, anterior.get("site") or "") else today}
        rec["contact_suggestion"] = sug
        if melhor_sc > config.WHATS_AUTO_MIN and not rec.get("wa_editado_em"):
            auto = [digits(post_w[0]), digits(melhor), "w"]
            if rec.get("contact_auto") != auto:
                rec["contact_auto"] = auto
                rec["contact_auto_em"] = today
        else:
            rec.pop("contact_auto", None)
    await asyncio.gather(*(one(r) for r in recs))
