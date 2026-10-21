#!/usr/bin/env python3
"""Sonda os sites de feeds.json ("sondagem" e "paginas") e escreve um relatório.

Para cada site: se a página inicial abre, que feeds oficiais anuncia, que secções tem,
e se os candidatos a feed oficial funcionam. Para cada página de "paginas": quantos
artigos o gerador consegue extrair. Serve para decidir o que vale a pena publicar.
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

import check_feeds
import gerar_feeds as g

SECTIONS = {"economia", "opiniao", "politica", "mundo", "internacional", "pais", "portugal",
            "sociedade", "desporto", "cultura", "ultimas", "ultima-hora", "noticias", "nacional"}
BLOCK = ("cf-chl", "just a moment", "captcha", "enable javascript", "access denied",
         "attention required", "are you a robot")


def probe_page(url):
    try:
        html, final = g.fetch(url)
    except Exception as e:
        return None, None, str(e).replace("|", "/")[:80]
    return html, final, ""


def hints(html):
    low = html.lower()
    out = []
    if any(b in low for b in BLOCK):
        out.append("possível bloqueio/captcha")
    if html.count("<a ") < 15:
        out.append("poucas ligações (pode carregar por JavaScript)")
    return ", ".join(out) or "-"


def sections(html, base):
    p = g.LinkParser()
    p.feed(html)
    host = urlparse(base).netloc.lower().removeprefix("www.")
    found = []
    for href, _ in p.links:
        u = urlparse(urldefrag(urljoin(base, href))[0])
        if not g.same_site(u.netloc, [host]):
            continue
        segs = [s for s in u.path.split("/") if s]
        if segs and len(segs) <= 2 and segs[-1].lower().replace(".html", "") in SECTIONS:
            link = f"{u.scheme}://{u.netloc}{u.path}"
            if link not in found:
                found.append(link)
    return found[:14]


def alt_feeds(html, base):
    out = []
    for m in re.finditer(r"<link[^>]+>", html, re.I):
        tag = m.group(0)
        if re.search(r"type=[\"']application/(rss|atom)\+xml", tag, re.I):
            h = re.search(r"href=[\"']([^\"']+)", tag, re.I)
            if h:
                out.append(urljoin(base, h.group(1)))
    return list(dict.fromkeys(out))[:6]


def probe_site(site):
    html, final, err = probe_page(site["home"])
    lines = []
    if html is None:
        lines.append(f"| {site['nome']} | ❌ não abre: {err} | - | - | - |")
    else:
        n = len(g.extract(html, final, {}))
        lines.append(f"| {site['nome']} | ✅ abre ({len(html)//1000} kB) | {n} artigos na home; {hints(html)} "
                     f"| {', '.join(alt_feeds(html, final)) or '-'} | {', '.join(sections(html, final)) or '-'} |")
    cand = []
    for url in site.get("feeds", []):
        name, u, estado, n, newest, fin = check_feeds.check((site["nome"], url))
        v = check_feeds.verdict(estado, n, newest, datetime.now(timezone.utc))
        when = newest.strftime("%Y-%m-%d") if newest else "-"
        cand.append(f"| {site['nome']} | {v} | {n} itens, último {when} | {url} |")
    return lines, cand


def probe_pagina(f):
    html, final, err = probe_page(f["url"])
    if html is None:
        return f"| {f['slug']} | ❌ {err} | - | {f['url']} |"
    items = g.extract(html, final, f)
    mark = "✅" if len(items) >= f.get("min_items", 5) else "⚠️"
    sample = items[0]["title"][:60] if items else "-"
    return f"| {f['slug']} | {mark} {len(items)} artigos | {sample} | {f['url']} |"


def main(path="feeds.json"):
    cfg = json.load(open(path, encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=6) as ex:
        sites = list(ex.map(probe_site, cfg.get("sondagem", [])))
        pages = list(ex.map(probe_pagina, cfg.get("paginas", [])))
    out = ["## 1. Páginas iniciais dos sites", "",
           "| Site | Abre? | Home | Feeds anunciados no HTML | Secções encontradas |", "|---|---|---|---|---|"]
    out += [l for s in sites for l in s[0]]
    out += ["", "## 2. Feeds oficiais candidatos", "", "| Site | Resultado | Itens | URL |", "|---|---|---|---|"]
    out += [l for s in sites for l in s[1]]
    out += ["", "## 3. Páginas de secção (o que o gerador consegue ler)", "",
            "| Slug | Artigos | Exemplo de título | URL |", "|---|---|---|---|"]
    out += pages
    report = "\n".join(out) + "\n"
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "feeds.json")
