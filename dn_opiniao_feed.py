#!/usr/bin/env python3
"""Gera um feed Atom a partir da página de Opinião do DN (https://www.dn.pt/opiniao-dn/opiniao).

Só usa a biblioteca padrão (Python 3.9+).

Uso:
    python3 dn_opiniao_feed.py                     # escreve opiniao-dn.atom
    python3 dn_opiniao_feed.py -o /var/www/opiniao.xml
    python3 dn_opiniao_feed.py --exact-dates       # vai buscar a data exata a cada artigo (mais lento)

A página só mostra tempos relativos ("28 Minutos", "7 Horas") ou datas
("23 Ago 2026"), por isso, sem --exact-dates, as datas são aproximadas.
Com --exact-dates lê-se a data de publicação de cada artigo e guarda-se em
cache (dn_dates_cache.json), pelo que só os artigos novos geram pedidos.
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

PAGE_URL = "https://www.dn.pt/opiniao-dn/opiniao"
UA = "Mozilla/5.0 (compatible; dn-opiniao-feed/1.0)"
LISBON = ZoneInfo("Europe/Lisbon")

MONTHS = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
          "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}
REL_RE = re.compile(r"^(\d+)\s+(minutos?|horas?|dias?|semanas?)$", re.I)
ABS_RE = re.compile(r"^(\d{1,2})\s+([A-Za-zçÇ]{3})\.?\s+(\d{4})$")


# Ligações que parecem artigos de opinião (exclui a própria página-índice e subsecções)
ARTICLE_RE = re.compile(
    r"(/opiniao-dn/(?!opiniao/?$|sociedade-aberta/?$|cartas-dos-leitores/?$)[^/?#]+"
    r"|/opiniao-dn/sociedade-aberta/[^/?#]+"
    r"|dinheirovivo\.dn\.pt/opiniao/[^/?#]+)"
)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-PT,pt;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


class OpinionParser(HTMLParser):
    """Recolhe, por ordem de aparição: autores, títulos (h3 > a) e textos soltos."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []          # ("author"|"title"|"text", valor, href)
        self._in_h3 = False
        self._a_href = None
        self._a_text = []
        self._text = []

    def _flush_text(self):
        t = " ".join("".join(self._text).split())
        self._text = []
        if t:
            self.events.append(("text", t, None))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h3":
            self._flush_text()
            self._in_h3 = True
        elif tag == "a":
            self._flush_text()
            self._a_href = a.get("href")
            self._a_text = []

    def handle_endtag(self, tag):
        if tag == "h3":
            self._in_h3 = False
        elif tag == "a" and self._a_href is not None:
            text = " ".join("".join(self._a_text).split())
            href = self._a_href
            self._a_href = None
            if not text:
                return
            if self._in_h3:
                self.events.append(("title", text, href))
            elif "/author/" in href:
                self.events.append(("author", text, href))
            elif ARTICLE_RE.search(href) and len(text) >= 12:
                self.events.append(("link", text, href))
        elif tag in ("div", "span", "p", "li", "section", "article"):
            self._flush_text()

    def handle_data(self, data):
        if self._a_href is not None:
            self._a_text.append(data)
        else:
            self._text.append(data)


def parse_when(label, now):
    label = label.strip()
    m = REL_RE.match(label)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = (timedelta(minutes=n) if unit.startswith("min") else
                 timedelta(hours=n) if unit.startswith("hor") else
                 timedelta(days=n) if unit.startswith("dia") else
                 timedelta(weeks=n))
        return now - delta
    m = ABS_RE.match(label)
    if m and m.group(2).lower() in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(2).lower()],
                        int(m.group(1)), 12, 0, tzinfo=LISBON)
    return None


def extract_items(html, base=PAGE_URL):
    p = OpinionParser()
    p.feed(html)
    ev = p.events
    now = datetime.now(LISBON)
    titles = [i for i, e in enumerate(ev) if e[0] == "title"]
    if not titles:  # estrutura diferente: usa as ligações que parecem artigos
        titles = [i for i, e in enumerate(ev) if e[0] == "link"]
    items, seen = [], set()
    for n, i in enumerate(titles):
        end = titles[n + 1] if n + 1 < len(titles) else len(ev)
        title, href = ev[i][1], urljoin(base, ev[i][2])
        if href in seen:
            continue
        seen.add(href)
        author = next((e[1] for e in ev[i + 1:end] if e[0] == "author"), None)
        if author is None:  # o autor também aparece antes do título
            author = next((e[1] for e in reversed(ev[max(0, i - 3):i]) if e[0] == "author"), None)
        when = None
        for e in ev[i + 1:end]:
            if e[0] == "text":
                when = parse_when(e[1], now)
                if when:
                    break
        items.append({"title": title, "url": href, "author": author or "DN",
                      "date": when or now})
    return items


def exact_date(url):
    html = fetch(url)
    for pat in (r'"datePublished"\s*:\s*"([^"]+)"',
                r'property="article:published_time"\s+content="([^"]+)"',
                r'content="([^"]+)"\s+property="article:published_time"'):
        m = re.search(pat, html)
        if m:
            try:
                return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def apply_exact_dates(items, cache_path):
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    for it in items:
        if it["url"] in cache:
            it["date"] = datetime.fromisoformat(cache[it["url"]])
            continue
        try:
            d = exact_date(it["url"])
        except Exception as e:  # rede, 403, etc.: mantém a data aproximada
            print(f"aviso: sem data exata para {it['url']}: {e}", file=sys.stderr)
            continue
        if d:
            it["date"] = d
            cache[it["url"]] = d.isoformat()
        time.sleep(0.5)
    json.dump(cache, open(cache_path, "w", encoding="utf-8"), indent=1)


def build_atom(items):
    ns = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", ns)
    q = lambda t: f"{{{ns}}}{t}"
    feed = ET.Element(q("feed"), {"{http://www.w3.org/XML/1998/namespace}lang": "pt"})
    ET.SubElement(feed, q("title")).text = "DN – Opinião"
    ET.SubElement(feed, q("subtitle")).text = "Artigos de opinião do Diário de Notícias (feed não oficial)"
    ET.SubElement(feed, q("id")).text = PAGE_URL
    ET.SubElement(feed, q("link"), {"href": PAGE_URL, "rel": "alternate", "type": "text/html"})
    items = sorted(items, key=lambda i: i["date"], reverse=True)
    newest = items[0]["date"] if items else datetime.now(timezone.utc)
    ET.SubElement(feed, q("updated")).text = newest.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for it in items:
        e = ET.SubElement(feed, q("entry"))
        ET.SubElement(e, q("title")).text = it["title"]
        ET.SubElement(e, q("link"), {"href": it["url"], "rel": "alternate", "type": "text/html"})
        ET.SubElement(e, q("id")).text = it["url"]
        ts = it["date"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ET.SubElement(e, q("published")).text = ts
        ET.SubElement(e, q("updated")).text = ts
        ET.SubElement(ET.SubElement(e, q("author")), q("name")).text = it["author"]
        ET.SubElement(e, q("summary")).text = f"{it['author']} – {it['title']}"
    ET.indent(feed)
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="opiniao-dn.atom")
    ap.add_argument("--url", default=PAGE_URL)
    ap.add_argument("--input", help="ler HTML de um ficheiro local em vez de ir à rede (testes)")
    ap.add_argument("--exact-dates", action="store_true")
    ap.add_argument("--cache", default="dn_dates_cache.json")
    args = ap.parse_args()

    html = open(args.input, encoding="utf-8").read() if args.input else fetch(args.url)
    items = extract_items(html, args.url)
    if not items:
        snippet = " ".join(re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S).split())
        print("--- DIAGNÓSTICO ---", file=sys.stderr)
        print(f"tamanho do HTML: {len(html)} caracteres", file=sys.stderr)
        print(f"<a href>: {html.count('<a ')}, /author/: {html.count('/author/')}, "
              f"/opiniao-dn/: {html.count('/opiniao-dn/')}, <h3: {html.count('<h3')}", file=sys.stderr)
        print(f"início do HTML (sem scripts): {snippet[:600]}", file=sys.stderr)
        sys.exit("Nenhum artigo encontrado: a estrutura da página pode ter mudado.")
    if args.exact_dates:
        apply_exact_dates(items, args.cache)
    with open(args.output, "wb") as f:
        f.write(build_atom(items))
    print(f"{len(items)} artigos -> {args.output}")


if __name__ == "__main__":
    main()
