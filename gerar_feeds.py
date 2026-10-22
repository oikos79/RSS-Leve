#!/usr/bin/env python3
"""Gera um feed Atom por página listada em feeds.json, mais index.html e feeds.opml.

Para cada página: descarrega-a, extrai as ligações que parecem artigos e escreve
<slug>.atom. As datas vêm da meta-informação de cada artigo (article:published_time,
JSON-LD ou <time>); se não existir, usa-se a data em que o artigo foi visto pela
primeira vez. O estado (datas já descobertas) fica em estado.json.

Uma página que falhe não impede as outras: fica registada como erro no index.html.
Só usa a biblioteca padrão (Python 3.9+).
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

BASE = os.environ.get("SITE_BASE", "https://oikos79.github.io/RSS-Leve").rstrip("/")
LISBON = ZoneInfo("Europe/Lisbon")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def norm(s):
    return " ".join((s or "").split())


def fetch(url, timeout=25):
    req = urlopen_request(url)
    with urlopen(req, timeout=timeout) as r:
        raw = r.read(3_000_000)
        enc = r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, "replace"), r.geturl()


def urlopen_request(url):
    return Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.5",
    })


class LinkParser(HTMLParser):
    """Recolhe todas as ligações com o seu texto (dando preferência a títulos h1-h4)."""
    HEAD = {"h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._a = None
        self._in_head = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a" and a.get("href"):
            self._a = {"href": a["href"], "text": [], "head": [],
                       "attr": a.get("title") or a.get("aria-label") or ""}
            self._in_head = False
        elif tag in self.HEAD and self._a is not None:
            self._in_head = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.HEAD:
            self._in_head = False
        elif tag == "a" and self._a is not None:
            a, self._a = self._a, None
            title = norm("".join(a["head"])) or norm("".join(a["text"])) or norm(a["attr"])
            self.links.append((a["href"], title))

    def handle_data(self, data):
        if self._a is not None and not self._skip:
            self._a["text"].append(data)
            if self._in_head:
                self._a["head"].append(data)


def looks_like_article(path):
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    last = segs[-1]
    if re.search(r"\d{5,}", path):
        return True
    return len(last) >= 15 and last.count("-") >= 2


def same_site(netloc, domains):
    n = netloc.lower().removeprefix("www.")
    return any(n == d or n.endswith("." + d) for d in domains)


def extract(html, page_url, cfg):
    p = LinkParser()
    p.feed(html)
    host = urlparse(page_url).netloc.lower().removeprefix("www.")
    domains = [d.lower().removeprefix("www.") for d in (cfg.get("domains") or [host])]
    include = re.compile(cfg["include"]) if cfg.get("include") else None
    exclude = re.compile(cfg["exclude"]) if cfg.get("exclude") else None
    min_title = cfg.get("min_title", 25)
    page_path = urlparse(page_url).path.rstrip("/")
    out, seen = [], set()
    for href, title in p.links:
        url = urldefrag(urljoin(page_url, href))[0]
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not same_site(u.netloc, domains):
            continue
        if u.path.rstrip("/") == page_path or url in seen:
            continue
        if include:
            if not include.search(u.path):
                continue
        elif not looks_like_article(u.path):
            continue
        if exclude and exclude.search(u.path):
            continue
        if len(title) < min_title or len(title.split()) < 4:
            continue
        seen.add(url)
        out.append({"title": title, "url": url})
        if len(out) >= cfg.get("max_items", 30):
            break
    return out


DATE_PATTERNS = [
    r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
    r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time["\']',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)',
    r'<time[^>]*datetime=["\']([^"\']+)',
]


def parse_iso(s):
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=LISBON)


def article_date(url):
    text, _ = fetch(url, timeout=15)
    text = text[:600_000]
    for pat in DATE_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            d = parse_iso(m.group(1))
            if d:
                return d
    return None


def utc(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_atom(name, slug, page_url, items):
    ns = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", ns)
    q = lambda t: f"{{{ns}}}{t}"
    feed = ET.Element(q("feed"), {"{http://www.w3.org/XML/1998/namespace}lang": "pt"})
    ET.SubElement(feed, q("title")).text = name
    ET.SubElement(feed, q("subtitle")).text = "Feed não oficial, gerado a partir da página " + page_url
    ET.SubElement(feed, q("id")).text = page_url
    ET.SubElement(feed, q("link"), {"href": page_url, "rel": "alternate", "type": "text/html"})
    ET.SubElement(feed, q("link"), {"href": f"{BASE}/{slug}.atom", "rel": "self",
                                    "type": "application/atom+xml"})
    items = sorted(items, key=lambda i: i["date"], reverse=True)
    ET.SubElement(feed, q("updated")).text = utc(items[0]["date"])
    ET.SubElement(ET.SubElement(feed, q("author")), q("name")).text = name
    for it in items:
        e = ET.SubElement(feed, q("entry"))
        ET.SubElement(e, q("title")).text = it["title"]
        ET.SubElement(e, q("link"), {"href": it["url"], "rel": "alternate", "type": "text/html"})
        ET.SubElement(e, q("id")).text = it["url"]
        ET.SubElement(e, q("published")).text = utc(it["date"])
        ET.SubElement(e, q("updated")).text = utc(it["date"])
    ET.indent(feed)
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def build_opml(feeds):
    opml = ET.Element("opml", {"version": "2.0"})
    ET.SubElement(ET.SubElement(opml, "head"), "title").text = "RSS Leve"
    body = ET.SubElement(opml, "body")
    for name, xml_url, page in feeds:
        ET.SubElement(body, "outline", {"text": name, "title": name, "type": "rss",
                                         "xmlUrl": xml_url, "htmlUrl": page})
    ET.indent(opml)
    return ET.tostring(opml, encoding="utf-8", xml_declaration=True)


def build_index(rows, now):
    trs = []
    for name, href, page, status, ok in rows:
        link = f'<a href="{escape(href)}">{escape(href.replace(BASE + "/", ""))}</a>' if ok else "—"
        mark = "✅" if ok else "❌"
        trs.append(f"<tr><td>{mark}</td><td>{escape(name)}</td><td>{link}</td>"
                   f'<td><a href="{escape(page)}">página original</a></td><td>{escape(status)}</td></tr>')
    return ("<!doctype html><html lang=\"pt\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>RSS Leve</title>"
            "<style>body{font:16px system-ui;margin:24px;max-width:1000px}"
            "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #8884;"
            "padding:6px 8px;text-align:left}</style></head><body>"
            f"<h1>RSS Leve</h1><p>Atualizado em {now:%Y-%m-%d %H:%M} UTC. "
            "<a href=\"feeds.opml\">feeds.opml</a> (importa todos os feeds de uma vez).</p>"
            "<table><tr><th></th><th>Feed</th><th>Ficheiro</th><th>Origem</th><th>Estado</th></tr>"
            + "".join(trs) + "</table></body></html>")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("items", {})
        return s
    except (OSError, ValueError):
        return {"items": {}}


def process(cfg, state, max_dates, now):
    html, final = fetch(cfg["url"])
    found = extract(html, final, cfg)
    need = cfg.get("min_items", 5)
    if len(found) < need:
        raise RuntimeError(f"só {len(found)} artigos encontrados (mínimo {need})")
    st = state["items"]
    for it in found:
        s = st.setdefault(it["url"], {"first_seen": now.isoformat(), "published": None, "tries": 0})
        s["last_seen"] = now.isoformat()
    todo = [it for it in found if st[it["url"]]["published"] is None and st[it["url"]]["tries"] < 2]
    for it in todo[:max_dates]:
        s = st[it["url"]]
        s["tries"] += 1
        try:
            d = article_date(it["url"])
            if d:
                s["published"] = d.isoformat()
        except Exception:
            pass
        time.sleep(0.3)
    for it in found:
        s = st[it["url"]]
        it["date"] = datetime.fromisoformat(s["published"] or s["first_seen"])
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="feeds.json")
    ap.add_argument("--out", default="site")
    ap.add_argument("--state", default="estado.json")
    ap.add_argument("--max-dates", type=int, default=12, help="artigos novos a abrir por feed e por execução")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    os.makedirs(args.out, exist_ok=True)
    state = load_state(args.state)
    now = datetime.now(timezone.utc)
    rows, good = [], []

    for fx in cfg.get("fixos", []):  # feeds feitos por outro script
        mine = f"{BASE}/{fx['slug']}.atom"
        good.append((fx["nome"], mine, fx["url"]))
        rows.append((fx["nome"], mine, fx["url"], "feito por dn_opiniao_feed.py", True))

    for of in cfg.get("oficiais", []):  # feeds oficiais que já funcionam: só se listam
        good.append((of["nome"], of["url"], of.get("pagina", of["url"])))
        rows.append((of["nome"], of["url"], of.get("pagina", of["url"]), "feed oficial do site", True))

    for f in cfg.get("paginas", []):
        name, slug, page = f["nome"], f["slug"], f["url"]
        try:
            items = process(f, state, args.max_dates, now)
            with open(os.path.join(args.out, f"{slug}.atom"), "wb") as fh:
                fh.write(build_atom(name, slug, page, items))
            mine = f"{BASE}/{slug}.atom"
            rows.append((name, mine, page, f"{len(items)} artigos", True))
            good.append((name, mine, page))
        except Exception as e:
            rows.append((name, "", page, f"{type(e).__name__}: {str(e)[:120]}", False))
        print(("OK   " if rows[-1][4] else "FALHA"), slug, "-", rows[-1][3])

    cutoff = now - timedelta(days=90)  # esquece artigos antigos
    state["items"] = {u: s for u, s in state["items"].items()
                      if datetime.fromisoformat(s.get("last_seen", s["first_seen"])) > cutoff}
    with open(args.state, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    with open(os.path.join(args.out, "feeds.opml"), "wb") as fh:
        fh.write(build_opml(good))
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(build_index(rows, now))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Feeds gerados\n\n| | Feed | Estado |\n|---|---|---|\n")
            for name, href, page, status, ok in rows:
                fh.write(f"| {'✅' if ok else '❌'} | {name} | {status} |\n")
    print(f"{sum(r[4] for r in rows)}/{len(rows)} feeds gerados")


if __name__ == "__main__":
    main()
