#!/usr/bin/env python3
"""Testa cada feed de feeds_antigos.txt e escreve um relatório em tabela.

Formato do ficheiro: uma linha por feed, "Nome|URL" ou "Nome|Secção|URL"
(o URL é sempre o último campo). Só usa a biblioteca padrão.
No GitHub Actions escreve também no resumo da execução ($GITHUB_STEP_SUMMARY).
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def classify(data):
    """Devolve (estado, nº de itens, data do item mais recente)."""
    head = data[:2000].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return "HTML (não é feed)", 0, None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return "XML inválido", 0, None
    kind = local(root.tag)
    if kind not in ("rss", "feed", "rdf"):
        return f"XML estranho ({kind})", 0, None
    items = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    dates = []
    for it in items:
        for c in it:
            if local(c.tag) in ("pubdate", "published", "updated", "date"):
                d = parse_date(c.text)
                if d:
                    dates.append(d)
                    break
    return "feed", len(items), (max(dates) if dates else None)


def check(entry):
    name, url = entry
    try:
        req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urlopen(req, timeout=25) as r:
            data = r.read(3_000_000)
            final = r.geturl()
    except Exception as e:  # HTTP 4xx/5xx, DNS, TLS, timeout...
        msg = str(e).replace("|", "/")[:60]
        return name, url, f"ERRO: {msg}", 0, None, ""
    estado, n, newest = classify(data)
    return name, url, estado, n, newest, final if final != url else ""


def verdict(estado, n, newest, now):
    if estado != "feed":
        return "❌ " + estado
    if n == 0:
        return "⚠️ feed vazio"
    if newest is None:
        return "✅ funciona (sem datas)"
    days = (now - newest).days
    if days <= 7:
        return "✅ ativo"
    if days <= 90:
        return f"🟡 parado há {days} dias"
    return f"❌ parado há {days} dias"


def main(path):
    entries = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        entries.append((" / ".join(p.strip() for p in parts[:-1]), parts[-1].strip()))
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check, entries))
    now = datetime.now(timezone.utc)
    rows = ["| # | Feed | Resultado | Itens | Mais recente | URL |", "|---|---|---|---|---|---|"]
    ok = 0
    for i, (name, url, estado, n, newest, final) in enumerate(results, 1):
        v = verdict(estado, n, newest, now)
        ok += v.startswith("✅")
        when = newest.strftime("%Y-%m-%d") if newest else "-"
        note = f" (→ {final})" if final else ""
        rows.append(f"| {i} | {name} | {v} | {n} | {when} | {url}{note} |")
    report = (f"## Verificação de {len(results)} feeds — {ok} ativos\n\n" + "\n".join(rows) + "\n")
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "feeds_antigos.txt")
