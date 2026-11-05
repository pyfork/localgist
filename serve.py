#!/usr/bin/env python3
"""localgist: read your own gists in a Finviz-news layout (2 columns x 90 rows).

Local only: binds 127.0.0.1, rejects any other Host header, reads the token
from `gh auth token` at start and keeps everything in memory. Stops itself
after 60 minutes with no page loads (sleep does not count).
"""
import html
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = 8770
ROWS = 90  # rows per column, as on finviz.com/news
IDLE_SECONDS = 60 * 60
API = "https://api.github.com"
TOKEN = ""  # set in main()
# CLOCK_UPTIME_RAW (macOS) and CLOCK_MONOTONIC (Linux) both pause while the machine sleeps
CLOCK = getattr(time, "CLOCK_UPTIME_RAW", time.CLOCK_MONOTONIC)

_list = {"at": 0.0, "gists": []}
_pages = {}  # gist id -> (updated_at, rendered html)
_last_hit = [time.clock_gettime(CLOCK)]


def gh(path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "localgist",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def gists(refresh=False):
    # shortcut: list cached 5 min in memory; add ?refresh=1 to force a reload
    if refresh or time.time() - _list["at"] > 300:
        out, page = [], 1
        while True:
            batch = json.loads(gh(f"/gists?per_page=100&page={page}"))
            out += batch
            if len(batch) < 100:
                break
            page += 1
        _list.update(at=time.time(), gists=out)
    return _list["gists"]


def stamp(iso, now=None):
    """Finviz style: '09:12PM' if today (local time), else 'Nov-03'."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    now = now or datetime.now().astimezone()
    return dt.strftime("%I:%M%p") if dt.date() == now.date() else dt.strftime("%b-%d")


def columns(items, page):
    start = (page - 1) * 2 * ROWS
    chunk = items[start : start + 2 * ROWS]
    return chunk[:ROWS], chunk[ROWS:]


def host_ok(host):
    return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"localgist.localhost:{PORT}")


def title(g):
    return g.get("description") or next(iter(g["files"]), g["id"])


CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--link:#0b57d0;--line:#e5e5ea;--head:#f5f5f7;--seen:#6b3fa0}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#e8e8ed;--muted:#98989f;--link:#8ab4f8;--line:#2c2c30;--head:#1d1d20;--seen:#c39bf0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:12px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{display:flex;gap:16px;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--head)}
header b{font-size:14px}header span{color:var(--muted)}header nav{margin-left:auto;display:flex;gap:10px}
a{color:var(--link);text-decoration:none}a:visited{color:var(--seen)}a:hover{text-decoration:underline}
.cur{color:var(--fg);font-weight:600}
input[name=t]{position:absolute;opacity:0;pointer-events:none}
.tabs{display:none}
main{display:grid;grid-template-columns:1fr 1fr;gap:24px;padding:8px 16px 32px;max-width:1600px;margin:0 auto}
h2{font-size:12px;margin:8px 0 4px;padding-bottom:4px;border-bottom:2px solid var(--fg)}
table{width:100%;border-collapse:collapse;table-layout:fixed}
td{padding:2px 0;height:20px;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:top}
td.t{width:62px;color:var(--muted);font-variant-numeric:tabular-nums}
.empty{color:var(--muted);padding:8px 0}
@media (max-width:767px){
 header{padding:10px 16px;flex-wrap:wrap}
 .tabs{display:flex;position:sticky;top:0;z-index:1;background:var(--bg);border-bottom:1px solid var(--line)}
 .tabs label{flex:1;text-align:center;padding:12px 0;font-weight:600;color:var(--muted);border-bottom:2px solid transparent}
 #t1:checked~.tabs label[for=t1],#t2:checked~.tabs label[for=t2]{color:var(--fg);border-color:var(--fg)}
 #t1:focus-visible~.tabs label[for=t1],#t2:focus-visible~.tabs label[for=t2]{outline:2px solid var(--link)}
 main{display:block;padding:0 16px 32px}h2{display:none}
 .c2,#t2:checked~main .c1{display:none}#t2:checked~main .c2{display:block}
 td{height:44px;padding:6px 0;white-space:normal;font-size:14px}
 td a{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 td.t{width:64px;font-size:12px}
}
.doc{max-width:860px;margin:0 auto;padding:16px;font-size:15px;line-height:1.6}
.doc .back{display:inline-block;padding:10px 0;font-size:14px}
.doc h3.f{font:600 12px ui-monospace,monospace;color:var(--muted);margin:24px 0 0;padding-bottom:4px;border-bottom:1px solid var(--line)}
.doc pre{overflow-x:auto;background:var(--head);padding:12px;border-radius:6px}
.doc img{max-width:100%}.doc table{table-layout:auto}.doc td{white-space:normal;height:auto}
"""


def shell(name, body):
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(name)}</title><style>{CSS}</style></head><body>{body}</body></html>"
    )


def rows(items):
    if not items:
        return '<p class="empty">No more gists.</p>'
    trs = "".join(
        f'<tr><td class="t">{stamp(g["updated_at"])}</td>'
        f'<td><a href="/g/{g["id"]}" title="{html.escape(title(g))}">{html.escape(title(g))}</a></td></tr>'
        for g in items
    )
    return f"<table>{trs}</table>"


def index_page(page, refresh):
    items = gists(refresh)
    pages = max(1, -(-len(items) // (2 * ROWS)))
    page = min(max(page, 1), pages)
    left, right = columns(items, page)
    base = (page - 1) * 2 * ROWS
    r1 = f"{base + 1}–{base + len(left)}" if left else "—"
    r2 = f"{base + ROWS + 1}–{base + ROWS + len(right)}" if right else "—"
    nav = " ".join(
        f'<span class="cur">{p}</span>' if p == page else f'<a href="/?p={p}">{p}</a>'
        for p in range(1, pages + 1)
    )
    return shell(
        "localgist",
        f'<header><b>localgist</b><span>{len(items)} gists · newest first</span>'
        f'<nav>Page {nav}<a href="/?p={page}&refresh=1">refresh</a></nav></header>'
        '<input type=radio name=t id=t1 checked aria-label="Show first column">'
        '<input type=radio name=t id=t2 aria-label="Show second column">'
        f'<div class="tabs"><label for=t1>{r1}</label><label for=t2>{r2}</label></div>'
        f'<main><section class="c1"><h2>Gists {r1}</h2>{rows(left)}</section>'
        f'<section class="c2"><h2>Gists {r2}</h2>{rows(right)}</section></main>',
    )


def gist_page(gid):
    g = json.loads(gh(f"/gists/{gid}"))
    cached = _pages.get(gid)
    if cached and cached[0] == g["updated_at"]:
        return cached[1]
    parts = []
    for name, f in g["files"].items():
        # shortcut: files over 1 MB arrive truncated from the API; fetch raw_url if that ever matters
        text = f.get("content") or ""
        if name.lower().endswith((".md", ".markdown")):
            body = gh("/markdown", {"text": text, "mode": "gfm"})  # GitHub returns sanitised HTML
        else:
            body = f"<pre>{html.escape(text)}</pre>"
        parts.append(f'<h3 class="f">{html.escape(name)}</h3>{body}')
    out = shell(
        title(g),
        f'<div class="doc"><a class="back" href="/">← All gists</a>'
        f'<p style="color:var(--muted);margin:0">{stamp(g["updated_at"])} · '
        f'<a href="{html.escape(g["html_url"])}">open on GitHub</a></p>{"".join(parts)}</div>',
    )
    _pages[gid] = (g["updated_at"], out)
    return out


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # no scripts at all, no framing; images and styles only
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not host_ok(self.headers.get("Host", "")):  # blocks DNS-rebinding pages
            return self.send(403, "forbidden", "text/plain")
        _last_hit[0] = time.clock_gettime(CLOCK)
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                p = q.get("p", ["1"])[0]
                return self.send(200, index_page(int(p) if p.isdigit() else 1, "refresh" in q))
            m = re.fullmatch(r"/g/([0-9a-f]{1,64})", url.path)
            if m:
                return self.send(200, gist_page(m[1]))
            return self.send(404, "not found", "text/plain")
        except Exception as e:  # GitHub down, token expired, etc.
            return self.send(502, shell("error", f"<p style='padding:16px'>GitHub fetch failed: {html.escape(str(e))}</p>"))


def watch_idle(server):
    while True:
        time.sleep(60)
        if time.clock_gettime(CLOCK) - _last_hit[0] > IDLE_SECONDS:
            server.shutdown()
            return


def main():
    global TOKEN
    try:
        TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("localgist: no GitHub token. Run `gh auth login` first.")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=watch_idle, args=(server,), daemon=True).start()
    print(f"localgist on http://127.0.0.1:{PORT} (stops after 60 min idle)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
