"""Gera os arquivos públicos (radios.json fatiado), o relatório e o e-mail."""
import html
import json
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config
from .extract import UF_NOMES, cidade_chave, nome_curto


def shard_of(path: str, n: int) -> int:
    """Mesmo cálculo do script do Blogger (hash 31 de 32 bits)."""
    h = 0
    for ch in path:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % n


def public_entry(rec):
    e = {"u": rec["stream"]}
    if rec.get("post_stream") and rec["post_stream"] != rec["stream"]:
        e["o"] = rec["post_stream"]  # endereço antigo que está no texto do post
    elif rec.get("post_stream"):
        e["p"] = 1  # "u" é o próprio link do post: se o post mudou depois do robô, vale o post
    if rec.get("status") == "off":
        e["s"] = 0
    if rec.get("status") in ("off", "bloqueio") or rec.get("br_ok"):
        e["v"] = 1  # o robô não consegue confirmar: o player dos visitantes avisa se tocou (Apps Script)
    if rec.get("http_only"):
        e["h"] = 1
    if rec.get("contact_overrides"):
        e["c"] = rec["contact_overrides"]  # [[antigo, novo, "w"|"t"], ...]
    if rec.get("iframe") or rec.get("popup"):
        e["f"] = 1  # post tem iframe "Ao vivo" ou botão de popup: o script do tema troca por player
    return e


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_public(posts):
    full = {}
    for pid, r in posts.items():
        if r.get("status") == "ignorado" or not (r.get("stream") or r.get("contact_overrides")):
            continue
        e = public_entry(r) if r.get("stream") else {"c": r["contact_overrides"]}
        full[pid] = e
    _dump(os.path.join(config.PUBLIC_DIR, "radios.json"), {k: v for k, v in full.items() if "u" in v or "c" in v})
    # caminho do post -> ID (usado pela planilha de aprovação para achar o post)
    _dump(os.path.join(config.PUBLIC_DIR, "paths.json"),
          {r["path"]: pid for pid, r in posts.items() if r.get("path")})
    # rádios da mesma cidade / UF ("Ouça também"): um arquivo por estado em docs/v/<uf>.json
    locs = write_vizinhas(posts)
    for pid, l in locs.items():
        full.setdefault(pid, {})["l"] = l
    shards = [{} for _ in range(config.SHARDS)]
    for p, e in full.items():
        shards[shard_of(p, config.SHARDS)][p] = e
    for i, sh in enumerate(shards):
        _dump(os.path.join(config.PUBLIC_DIR, "r", f"{i:02d}.json"), sh)
    nojekyll = os.path.join(config.PUBLIC_DIR, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()


def _vizinha_ok(r):
    """Entra na lista "Ouça também": post de rádio, com áudio e sem falha confirmada."""
    if r.get("status") in ("ignorado", "sem_link") or not r.get("path") or not r.get("loc"):
        return False
    return not (r.get("status") == "off" and r.get("fail_count", 0) >= 3)


def write_vizinhas(posts):
    """Gera docs/v/<uf>.json = {"n": nome do estado, "c": {chave: [nome, [[rádio, caminho], ...]]}}.
    Devolve {post_id: "uf|chave"} para o script do tema saber qual arquivo e cidade usar."""
    # cidade sem UF no post: usa a UF que os outros posts da mesma cidade têm
    uf_da_cidade = {}
    for r in posts.values():
        loc = r.get("loc")
        if loc and loc[1]:
            uf_da_cidade.setdefault(cidade_chave(loc[0]), {}).setdefault(loc[1], 0)
            uf_da_cidade[cidade_chave(loc[0])][loc[1]] += 1
    estados, locs = {}, {}
    for pid, r in sorted(posts.items(), key=lambda kv: (kv[1].get("title") or "").lower()):
        if r.get("status") == "ignorado" or not r.get("loc") or not r.get("path"):
            continue
        cidade, uf = r["loc"][0], r["loc"][1]
        chave = cidade_chave(cidade)
        if not uf:
            votos = uf_da_cidade.get(chave)
            if not votos:
                continue
            uf = max(votos, key=votos.get)
        locs[pid] = f"{uf}|{chave}"   # o bloco aparece também em rádio fora do ar (o ouvinte acha outra)
        grupo = estados.setdefault(uf, {}).setdefault(chave, [cidade, []])
        if _vizinha_ok(r):            # mas só rádio com áudio entra na lista
            grupo[1].append([nome_curto(r.get("title") or ""), r["path"]])
    pasta = os.path.join(config.PUBLIC_DIR, "v")
    for uf, cidades in estados.items():
        _dump(os.path.join(pasta, f"{uf}.json"), {"n": UF_NOMES.get(uf, uf.upper()), "c": cidades})
    return locs


# ---------------------------------------------------------------- relatório
def _row(r, extra=""):
    t = html.escape(r.get("title") or r.get("url"))
    u = html.escape(r.get("url", ""))
    s = html.escape(r.get("stream") or "—")
    reason = html.escape(r.get("reason", ""))
    return (f'<tr><td><a href="{u}">{t}</a></td><td style="word-break:break-all;font-size:12px">{s}</td>'
            f"<td>{reason}</td><td>{extra}</td></tr>")


def _table(title, rows, cols=("Post", "Stream atual", "Motivo", "")):
    if not rows:
        return ""
    head = "".join(f"<th align=left>{c}</th>" for c in cols)
    return (f"<h3>{title} ({len(rows)})</h3><table cellpadding=6 cellspacing=0 border=1 "
            f'style="border-collapse:collapse;border-color:#ddd;font-family:Arial;font-size:13px">'
            f"<tr style='background:#f3f3f3'>{head}</tr>{''.join(rows)}</table>")


def _sugg(r):
    s = r.get("suggestion")
    if not s:
        return ""
    return (f'<b>Sugestão:</b> <span style="word-break:break-all">{html.escape(s["url"])}</span><br>'
            f'<small>{html.escape(s.get("source", ""))} — confiança {s.get("score", "")}</small>')


def _fmt_phone(d):
    d = d[2:] if len(d) > 11 and d.startswith("55") else d
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return d


def _url_rows(items):
    rows = []
    for path, o in items:
        sug = o.get("suggested") or o.get("new_path")
        sug_html = (f'<a href="{config.SITE_URL}{html.escape(sug)}">{html.escape(sug)}</a>'
                    + (f' <small>(semelhança {o.get("suggested_score")})</small>' if o.get("suggested_score") and o.get("suggested_score") < 1 else "")
                    if sug else "—")
        rows.append(f'<tr><td>{html.escape(o.get("title", ""))}</td><td style="font-size:12px">{html.escape(path)}</td>'
                    f'<td>HTTP {o.get("http", "?")}</td><td>{sug_html}</td></tr>')
    return rows


def _contact_rows(recs):
    rows = []
    for r in recs:
        c = r["contact_suggestion"]
        rows.append(f'<tr><td><a href="{html.escape(r["url"])}">{html.escape(r["title"])}</a></td>'
                    f'<td>{_fmt_phone(c["post"])}</td><td><b>{_fmt_phone(c["site"])}</b></td>'
                    f'<td><a href="{html.escape(c["url"])}">site oficial</a></td></tr>')
    return rows


def build_report(posts, events, today, old_urls=None):
    old_urls = old_urls or {}
    no_redirect = sorted([(p, o) for p, o in old_urls.items() if o.get("status") in ("sem_redirect", "temporario")],
                         key=lambda x: x[1].get("since", ""))
    new_no_redirect = [(p, o) for p, o in no_redirect if not o.get("alerted")]
    for _, o in new_no_redirect:
        o["alerted"] = today
    contact_sugs = [r for r in posts.values() if r.get("contact_suggestion")]
    new_contacts = [r for r in contact_sugs if r.get("contact_alerted") != r["contact_suggestion"]["site"]]
    for r in new_contacts:
        r["contact_alerted"] = r["contact_suggestion"]["site"]
    recs = list(posts.values())
    off = [r for r in recs if r.get("status") == "off"]
    confirmed = [r for r in off if r.get("fail_count", 0) >= config.CONFIRM_RUNS]
    nolink = [r for r in recs if r.get("status") == "sem_link"]
    bloqueio = [r for r in recs if r.get("status") == "bloqueio"]
    http_only = [r for r in recs if r.get("status") == "ok" and r.get("http_only")]
    iframes = [r for r in recs if r.get("iframe")]
    iframe_ok = [r for r in iframes if r.get("status") == "ok" and not r.get("http_only")]
    iframe_http = [r for r in iframes if r.get("status") == "ok" and r.get("http_only")]
    popups = [r for r in recs if r.get("popup")]
    popup_ok = [r for r in popups if r.get("status") == "ok" and not r.get("http_only")]
    popup_http = [r for r in popups if r.get("status") == "ok" and r.get("http_only")]
    need = confirmed + nolink
    new = [r for r in need if not r.get("alerted")]
    for r in new:
        r["alerted"] = today

    summary = {
        "data": today,
        "posts": len(recs),
        "ok": sum(1 for r in recs if r.get("status") == "ok"),
        "fora_do_ar": len(off),
        "fora_confirmado": len(confirmed),
        "sem_link": len(nolink),
        "bloqueio": len(bloqueio),
        "confirmadas_brasil": sum(1 for r in recs if r.get("status") == "ok" and r.get("br_ok")),
        "so_http": len(http_only),
        "corrigidos_hoje": len(events["fixed"]),
        "voltaram_hoje": len(events["recovered"]),
        "posts_novos": len(events["new_posts"]),
        "iframes": len(iframes),
        "iframes_trocados_por_player": len(iframe_ok),
        "iframes_so_http": len(iframe_http),
        "popups": len(popups),
        "popups_trocados_por_player": len(popup_ok),
        "popups_so_http": len(popup_http),
        "alertas_novos": len(new),
        "urls_sem_301": len(no_redirect),
        "whatsapp_divergente": len(contact_sugs),
        "ignorados": sum(1 for r in recs if r.get("status") == "ignorado"),
    }

    fixed_rows = [
        f'<tr><td><a href="{html.escape(f["url"])}">{html.escape(f["title"])}</a></td>'
        f'<td style="word-break:break-all;font-size:12px">{html.escape(f["from"] or "")}</td>'
        f'<td style="word-break:break-all;font-size:12px">{html.escape(f["to"])}</td>'
        f'<td>{html.escape(f["source"])}</td></tr>' for f in events["fixed"]]

    def page(sections, title):
        return (f"<html><head><meta charset=utf-8><meta name=robots content=noindex>"
                f"<title>{title}</title></head><body style='font-family:Arial;max-width:1100px;margin:auto'>"
                f"<h2>{title}</h2>"
                f"<p>{summary['posts']} posts · <b style=color:green>{summary['ok']} tocando</b> · "
                f"<b style=color:#c00>{summary['fora_do_ar']} fora do ar</b> "
                f"({summary['fora_confirmado']} confirmados) · {summary['bloqueio']} não testáveis daqui · {summary['confirmadas_brasil']} confirmadas por ouvintes no Brasil · {summary['sem_link']} sem link · "
                f"{summary['so_http']} só em http · {summary['corrigidos_hoje']} corrigidos hoje · "
                f"{summary['voltaram_hoje']} voltaram sozinhos · {summary['ignorados']} ignorados "
                f"(não são rádio, encerradas ou marcados por você) · iframes 'Ao vivo': "
                f"{summary['iframes_trocados_por_player']} viram player, {summary['iframes_so_http']} só http · "
                f"botões de popup: {summary['popups']} ({summary['popups_trocados_por_player']} viram player, "
                f"{summary['popups_so_http']} só http)</p>{''.join(sections)}"
                f"<p style='color:#888;font-size:12px'>Gerado pelo radiocol-monitor em {today}.</p>"
                f"</body></html>")

    cap = 150  # o Gmail corta e-mails muito longos; o resto fica no relatório completo
    new_off = [r for r in new if r.get("status") == "off"]
    new_nolink = [r for r in new if r.get("status") == "sem_link"]
    email_sections = [
        _table("🔴 Precisam de revisão manual (novos)", [_row(r, _sugg(r)) for r in new_off[:cap]]),
        _table("⚪ Posts sem endereço de áudio (novos)", [_row(r) for r in new_nolink[:cap]]),
        _table("✅ Endereço novo gravado na base hoje (o site usa via script do tema; o post não é alterado)", fixed_rows[:cap],
               ("Post", "Endereço antigo", "Endereço novo", "Origem")),
    ]
    if len(new_off) > cap or len(new_nolink) > cap or len(fixed_rows) > cap:
        email_sections.append(f"<p>Lista cortada em {cap} itens por seção — veja o relatório completo.</p>")
    url_cols = ("Post antigo", "URL antiga", "Resposta", "Redirecionar para (sugestão)")
    ct_cols = ("Post", "WhatsApp no post", "WhatsApp no site oficial", "")
    email_sections += [
        _table("↪️ URLs antigas sem redirecionamento 301 (novas)", _url_rows(new_no_redirect), url_cols),
        _table("📱 WhatsApp diferente no site oficial (novos)", _contact_rows(new_contacts), ct_cols),
    ]
    pending_old = len(need) - len(new)
    if pending_old:
        email_sections.append(f"<p>Além desses, <b>{pending_old}</b> posts continuam pendentes de dias anteriores.</p>")
    if config.REPORT_URL:
        email_sections.append(f'<p><a href="{config.REPORT_URL}">Ver relatório completo</a></p>')

    full_sections = [
        _table("🔴 Fora do ar (confirmado) — revisar", [_row(r, _sugg(r)) for r in
                                                      sorted(confirmed, key=lambda r: r.get("title", ""))]),
        _table("🟡 Falharam hoje pela 1ª vez (aguardando confirmação)",
               [_row(r, _sugg(r)) for r in off if r not in confirmed]),
        _table("⚪ Sem endereço de áudio no post", [_row(r) for r in nolink]),
        _table("🌎 Não deu para testar daqui — o servidor não responde ao robô (que roda fora do Brasil). "
               "Provável bloqueio de acesso estrangeiro: confira tocando no site; o site não mostra aviso de fora do ar",
               [_row(r) for r in sorted(bloqueio, key=lambda r: r.get("title", ""))]),
        _table("🟠 Só funcionam em http:// (o navegador pode bloquear no site https)",
               [_row(r, _sugg(r)) for r in http_only]),
        _table("🔁 Iframes 'Ao vivo' ainda sem stream https (continuam com o iframe)",
               [_row(r, _sugg(r)) for r in iframe_http]),
        _table("🔁 Botões de popup ainda sem stream https (continuam abrindo o popup)",
               [_row(r, _sugg(r)) for r in popup_http]),
        _table("✅ Endereço novo gravado na base hoje (o site usa via script do tema; o post não é alterado)", fixed_rows,
               ("Post", "Endereço antigo", "Endereço novo", "Origem")),
        _table("⏸️ Ignorados (não são post de rádio, rádios encerradas ou marcados no overrides.json)",
               [_row(r) for r in sorted(recs, key=lambda r: r.get("title", "")) if r.get("status") == "ignorado"]),
        _table("↪️ URLs antigas sem redirecionamento 301", _url_rows(no_redirect), url_cols),
        _table("📱 WhatsApp diferente no site oficial", _contact_rows(contact_sugs), ct_cols),
    ]
    report_html = page(full_sections, "Radiocol — situação dos streams")
    with open(os.path.join(config.PUBLIC_DIR, "relatorio.html"), "w", encoding="utf-8") as f:
        f.write(report_html)

    # resumo no painel do GitHub Actions
    gh = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("## Radiocol monitor\n\n| item | qtd |\n|---|---|\n")
            for k, v in summary.items():
                f.write(f"| {k} | {v} |\n")

    subject = (f"Radiocol: {len(new)} rádio(s) para revisar"
               + (f", {len(events['fixed'])} corrigida(s)" if events["fixed"] else "")
               + (f", {len(new_no_redirect)} URL(s) sem 301" if new_no_redirect else "")
               + (f", {len(new_contacts)} WhatsApp divergente(s)" if new_contacts else "")
               + f" — {today}")
    return {
        "summary": summary,
        "subject": subject,
        "email_html": page(email_sections, subject),
        "should_send": bool(new or events["fixed"] or new_no_redirect or new_contacts) or config.EMAIL_ALWAYS,
    }


def send_email(report):
    if not report["should_send"]:
        print("Nada novo — e-mail não enviado.")
        return
    if config.DRY_RUN_EMAIL or not (config.SMTP_USER and config.SMTP_PASS and config.MAIL_TO):
        path = os.path.join(config.DATA_DIR, "email_preview.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report["email_html"])
        print(f"E-mail não configurado/dry-run — prévia salva em {path}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = report["subject"]
    msg["From"] = config.MAIL_FROM
    msg["To"] = config.MAIL_TO
    msg.attach(MIMEText(json.dumps(report["summary"], ensure_ascii=False, indent=1), "plain", "utf-8"))
    msg.attach(MIMEText(report["email_html"], "html", "utf-8"))
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ctx, timeout=30) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
    print(f"E-mail enviado para {config.MAIL_TO}")
