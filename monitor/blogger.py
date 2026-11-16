"""Lê todos os posts do Blogger pelo feed JSON público (150 posts por página).

O feed traz o HTML completo de cada post, então ~22 requisições cobrem os 3.160
posts — muito mais leve do que abrir as páginas uma a uma a partir do sitemap.
Requisito: Configurações do Blogger > Feed do site > "Permitir feed do blog" = Completo.
"""
import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse

import aiohttp

from . import config


@dataclass
class Post:
    id: str
    path: str
    url: str
    title: str
    content: str
    labels: list = field(default_factory=list)
    updated: str = ""


async def _get(session, url, as_json=True, tries=3):
    for i in range(tries):
        try:
            async with session.get(url, headers={"User-Agent": config.USER_AGENT},
                                   timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                return await (r.json(content_type=None) if as_json else r.text())
        except Exception:
            if i == tries - 1:
                raise
            await asyncio.sleep(3 * (i + 1))


def post_path(link: str) -> str:
    return quote(urlparse(link).path, safe="/%-._~")


async def fetch_posts(session):
    posts, start, total = [], 1, None
    while True:
        url = (f"{config.SITE_URL}/feeds/posts/default?alt=json&max-results=150"
               f"&start-index={start}")
        feed = (await _get(session, url))["feed"]
        if total is None:
            total = int(feed.get("openSearch$totalResults", {}).get("$t", 0))
        entries = feed.get("entry", [])
        for e in entries:
            link = next((l["href"] for l in e.get("link", []) if l.get("rel") == "alternate"), None)
            if not link:
                continue  # rascunho/sem URL
            content = (e.get("content") or e.get("summary") or {}).get("$t", "")
            posts.append(Post(
                id=e["id"]["$t"].split("post-")[-1],
                path=post_path(link),
                url=link,
                title=e.get("title", {}).get("$t", ""),
                content=content,
                labels=[c.get("term", "") for c in e.get("category", [])],
                updated=e.get("updated", {}).get("$t", ""),
            ))
        start += len(entries)
        if not entries or start > total:
            break
        if config.LIMIT_POSTS and len(posts) >= config.LIMIT_POSTS:
            break
    if config.LIMIT_POSTS:
        posts = posts[: config.LIMIT_POSTS]
    return posts, total


BODY_RE = re.compile(r"""class=["'][^"']*post-body[^"']*["'][^>]*>(.*?)(?:class=["'][^"']*post-footer|</article>)""",
                     re.I | re.S)


async def fetch_post_body(session, url):
    """Plano B: abre a página do post (quando o feed vem resumido)."""
    try:
        html = await _get(session, url, as_json=False, tries=2)
    except Exception:
        return ""
    m = BODY_RE.search(html)
    return m.group(1) if m else html


async def fetch_post_body_raw(session, url):
    """Baixa uma página qualquer (ex.: página do iframe) sem recortar."""
    try:
        async with session.get(url, headers={"User-Agent": config.USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status >= 400 or "html" not in r.headers.get("Content-Type", "html"):
                return ""
            return (await r.content.read(400_000)).decode("utf-8", "ignore")
    except Exception:
        return ""
