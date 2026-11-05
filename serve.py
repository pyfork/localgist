#!/usr/bin/env python3
"""localgist: read your own gists in a Finviz-news layout (2 columns x 90 rows).

Local only: binds 127.0.0.1, rejects any other Host header, reads the token
from `gh auth token` at start and keeps everything in memory. Stops itself
after 60 minutes with no page loads (sleep does not count).

    python3 serve.py          # run in the foreground
    python3 serve.py --open   # also open the browser (what `pyup` does)
"""
import base64
import hashlib
import html
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = 8770  # first choice; main() moves up to PORT+9 if it is taken
PORT_TRIES = 10
ROWS = 90  # rows per column, as on finviz.com/news
IDLE_SECONDS = 60 * 60
PAGE_CACHE = 50  # rendered gists kept in memory, oldest dropped first
API = "https://api.github.com"
TOKEN = ""  # set in main()
# CLOCK_UPTIME_RAW (macOS) and CLOCK_MONOTONIC (Linux) both pause while the machine sleeps
CLOCK = getattr(time, "CLOCK_UPTIME_RAW", time.CLOCK_MONOTONIC)

PER_PAGE = 30  # smaller pages come back much faster and are fetched in parallel
STALE_SECONDS = 300
CHECK_SECONDS = 60  # how often an open page may trigger a GitHub update check
_checked = [0.0]

_list = {"at": 0.0, "gists": []}
_lock = threading.Lock()  # one list fetch at a time
_pages = {}  # gist id -> (updated_at, rendered html)
_last_hit = [time.clock_gettime(CLOCK)]


def gh(path, body=None, with_headers=False):
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
        text = r.read().decode()
        return (text, r.headers) if with_headers else text


def last_page(link):
    m = re.search(r'[?&]page=(\d+)>; rel="last"', link or "")
    return int(m[1]) if m else 1


def fetch_all():
    """Every gist, most recently updated first. GitHub takes ~2 s per 100-gist page but ~0.8 s per 30,
    so read page 1 for the page count, then fetch the rest side by side."""
    text, headers = gh(f"/gists?per_page={PER_PAGE}&page=1", with_headers=True)
    pages = [json.loads(text)]
    rest = range(2, last_page(headers.get("Link")) + 1)
    with ThreadPoolExecutor(max_workers=8) as pool:  # map keeps page order
        pages += pool.map(lambda p: json.loads(gh(f"/gists?per_page={PER_PAGE}&page={p}")), rest)
    seen, out = set(), []
    for g in (g for page in pages for g in page):  # a gist edited mid-fetch can shift pages: drop repeats
        if g["id"] not in seen:
            seen.add(g["id"])
            out.append(g)
    # GitHub lists gists by creation date; sort by last update so an edited gist comes to the top
    return sorted(out, key=lambda g: g["updated_at"], reverse=True)


def _load():
    _list.update(at=time.time(), gists=fetch_all())


def _load_in_background():
    try:
        _load()
    except Exception as e:  # keep serving the old list; next stale request retries
        print(f"localgist: background refresh failed: {e}", file=sys.stderr, flush=True)
    finally:
        _lock.release()


def gists(refresh=False):
    """Only the very first load (or ↻) waits. A stale list is served at once and refreshed behind it."""
    if refresh or not _list["at"]:
        with _lock:
            if refresh or not _list["at"]:  # the start-up warm-up may have filled it while we waited
                _load()
    elif time.time() - _list["at"] > STALE_SECONDS and _lock.acquire(blocking=False):
        old = _list["gists"]
        threading.Thread(target=_load_in_background, daemon=True).start()
        return old
    return _list["gists"]


def signature(items):
    """Changes when a gist is added, edited (it moves to the top) or deleted (count drops)."""
    return f'{len(items)}-{items[0]["id"]}-{items[0]["updated_at"]}' if items else "0"


def _check_for_update():
    try:
        cur = _list["gists"]
        if not cur:
            return _load()
        # one tiny call: only gists updated at or after our newest one come back
        newer = json.loads(gh(f'/gists?per_page=5&since={cur[0]["updated_at"]}'))
        if any((g["id"], g["updated_at"]) != (cur[0]["id"], cur[0]["updated_at"]) for g in newer):
            _load()
    except Exception as e:
        print(f"localgist: update check failed: {e}", file=sys.stderr, flush=True)
    finally:
        _lock.release()


def version():
    """Signature of the list the server holds now. Checks GitHub at most once a minute,
    and only while a page is open and asking (the browser polls /version)."""
    if time.time() - _checked[0] > CHECK_SECONDS and _lock.acquire(blocking=False):
        _checked[0] = time.time()
        threading.Thread(target=_check_for_update, daemon=True).start()
    return signature(_list["gists"])


def stamp(iso, now=None):
    """Finviz style: '09:12PM' if today (local time), else 'Nov-03'."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    now = now or datetime.now().astimezone()
    return dt.strftime("%I:%M%p") if dt.date() == now.date() else dt.strftime("%b-%d")


def columns(items, page):
    start = (page - 1) * 2 * ROWS
    chunk = items[start : start + 2 * ROWS]
    return chunk[:ROWS], chunk[ROWS:]


def span(start, n):
    return f"{start + 1}–{start + n}" if n else "—"


def host_ok(host):
    return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"localgist.localhost:{PORT}")


def title(g):
    return g.get("description") or next(iter(g["files"]), g["id"])


def search_data(items, field="updated_at"):
    """[id, stamp, title, lowercase haystack] per gist, safe to embed in a <script> block."""
    rows = [[g["id"], stamp(g[field]), title(g), " ".join([title(g), *g["files"]]).lower()] for g in items]
    return json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")


CSS = """
:root{color-scheme:light;
 --page:#eef0f3;--surface:#fff;--fg:#16181d;--muted:#5f6673;--faint:#8a919d;--line:#e7e9ed;--rule:#16181d;
 --link:#173a7a;--seen:#5b3f8c;--hover:#f3f6fb;--field:#f6f7f9;--field-line:#d9dce2;--focus:#2f6feb;--kbd:#fff;--shadow:0 1px 2px rgb(16 24 40 / .05),0 8px 24px rgb(16 24 40 / .06)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;
 --page:#0b0d10;--surface:#14171c;--fg:#e8eaef;--muted:#a0a7b4;--faint:#6f7683;--line:#242830;--rule:#e8eaef;
 --link:#a9c4f6;--seen:#c7adf2;--hover:#1b2028;--field:#0f1216;--field-line:#2c313a;--focus:#6b96ff;--kbd:#1d2129;--shadow:0 1px 2px rgb(0 0 0 / .4),0 12px 32px rgb(0 0 0 / .35)}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--page)}
body{margin:0;background:var(--page);color:var(--fg);font:12.5px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--link);text-decoration:none}a:visited{color:var(--seen)}a:hover{text-decoration:underline}
a:focus-visible,label:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:3px}
.wrap{width:min(1040px,100% - 64px);margin:32px auto 48px;background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:clip}
header{display:flex;align-items:center;gap:20px;padding:14px 24px;border-bottom:1px solid var(--line)}
.brand{font-weight:700;font-size:15px;letter-spacing:-.01em;color:var(--fg)}.brand:visited{color:var(--fg)}
.search{position:relative;flex:1;max-width:440px}
.search svg{position:absolute;left:10px;top:50%;translate:0 -50%;width:14px;height:14px;stroke:var(--faint);fill:none;stroke-width:2;pointer-events:none}
.search input{width:100%;height:34px;padding:0 52px 0 32px;font:inherit;font-size:13px;color:var(--fg);background:var(--field);border:1px solid var(--field-line);border-radius:8px;outline:none;transition:border-color .12s,box-shadow .12s}
.search input::placeholder{color:var(--faint)}
.search input:focus{border-color:var(--focus);box-shadow:0 0 0 3px color-mix(in srgb,var(--focus) 22%,transparent);background:var(--surface)}
.search input::-webkit-search-cancel-button{display:none}
kbd{position:absolute;right:8px;top:50%;translate:0 -50%;font:500 11px/1 ui-monospace,SFMono-Regular,monospace;color:var(--muted);background:var(--kbd);border:1px solid var(--field-line);border-bottom-width:2px;border-radius:5px;padding:3px 6px;pointer-events:none}
.search input:focus+kbd{opacity:0}
.meta{margin-left:auto;display:flex;align-items:center;gap:14px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.meta nav{display:flex;gap:4px;align-items:center}
.meta nav a,.meta nav b{min-width:24px;height:24px;display:inline-grid;place-items:center;border-radius:6px;padding:0 6px}
.meta nav b{background:var(--fg);color:var(--surface);font-weight:600}
.sort{display:inline-flex;padding:2px;gap:2px;background:var(--field);border:1px solid var(--field-line);border-radius:8px;flex:none}
.sort a,.sort b{height:28px;padding:0 11px;display:inline-grid;place-items:center;border-radius:6px;font-size:12px;font-weight:500;color:var(--muted)}
.sort a:visited{color:var(--muted)}.sort a:hover{color:var(--fg);text-decoration:none;background:var(--hover)}
.sort b{background:var(--surface);color:var(--fg);font-weight:600;box-shadow:0 1px 2px rgb(0 0 0 / .12)}
.meta nav a:hover{background:var(--hover);text-decoration:none}
input[name=t]{position:absolute;opacity:0;pointer-events:none}
.tabs{display:none}
main{display:grid;grid-template-columns:1fr 1fr;gap:40px;padding:12px 24px 28px}
h2{display:flex;justify-content:space-between;margin:10px 0 2px;padding-bottom:6px;border-bottom:2px solid var(--rule);font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
h2 span:last-child{color:var(--muted);font-weight:500;letter-spacing:0;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;table-layout:fixed}
tr:hover td{background:var(--hover)}
td{height:23px;padding:3px 0;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:top}
td.t{width:66px;padding-left:4px;color:var(--faint);font-variant-numeric:tabular-nums;font-size:11.5px;padding-top:4px}
.empty{color:var(--muted);padding:14px 0;margin:0}
.toast{position:fixed;left:50%;bottom:24px;translate:-50% 0;z-index:5;display:flex;align-items:center;gap:10px;padding:10px 16px;border:0;border-radius:999px;background:var(--fg);color:var(--surface);font:600 13px/1.2 inherit;font-family:inherit;box-shadow:0 8px 28px rgb(0 0 0 / .25);cursor:pointer;animation:rise .2s ease-out}
.toast::before{content:"";width:8px;height:8px;border-radius:50%;background:#3ccf7a}
.toast:hover{filter:brightness(1.1)}.toast:focus-visible{outline:2px solid var(--focus);outline-offset:3px}
@keyframes rise{from{opacity:0;translate:-50% 8px}}
@media (prefers-reduced-motion:reduce){.toast{animation:none}}
@media (max-width:767px){.toast{bottom:16px;width:calc(100% - 32px);justify-content:center}}
@media (max-width:767px){
 body{font-size:14px}
 .wrap{width:auto;margin:0;border:0;border-radius:0;box-shadow:none;min-height:100vh}
 .top{position:sticky;top:0;z-index:2;background:var(--surface)}
 header{flex-wrap:wrap;gap:10px 12px;padding:12px 16px}
 .search{order:3;flex:1 1 140px;max-width:none}.search input{height:40px;font-size:16px;padding-right:10px}
 .sort{order:4}.sort a,.sort b{height:34px;padding:0 10px;font-size:13px}
 kbd{display:none}
 .tabs{display:flex;border-bottom:1px solid var(--line)}
 .tabs label{flex:1;text-align:center;padding:11px 0;font-weight:600;font-size:13px;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;font-variant-numeric:tabular-nums}
 #t1:checked~.top label[for=t1],#t2:checked~.top label[for=t2]{color:var(--fg);border-color:var(--fg)}
 #t1:focus-visible~.top label[for=t1],#t2:focus-visible~.top label[for=t2]{outline:2px solid var(--focus);outline-offset:-2px}
 main{display:block;padding:0 16px 32px}h2{display:none}
 .c2,#t2:checked~main .c1{display:none}#t2:checked~main .c2{display:block}
 td{height:48px;padding:8px 0;white-space:normal}
 td a{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 td.t{width:64px;font-size:12px;padding-top:10px;padding-left:0}
}
.doc{max-width:820px;margin:0 auto;padding:8px 32px 48px;font-size:15px;line-height:1.65}
.doc .bar{display:flex;gap:12px;align-items:center;color:var(--muted);font-size:13px;padding:6px 0 4px}
.doc h3.f{font:600 12px ui-monospace,SFMono-Regular,monospace;color:var(--muted);margin:28px 0 0;padding-bottom:6px;border-bottom:1px solid var(--line)}
.doc pre{overflow-x:auto;background:var(--field);border:1px solid var(--line);padding:12px 14px;border-radius:8px;font-size:13px}
.doc code{font-size:.9em}.doc img{max-width:100%}
.doc table{table-layout:auto;margin:12px 0}.doc td,.doc th{white-space:normal;height:auto;padding:6px 10px;border:1px solid var(--line)}
@media (max-width:767px){.doc{padding:4px 16px 40px}}
"""

ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>'

# Live search over every gist's title and file names. Cmd/Ctrl+F focuses it; pressed again
# while it is focused, the browser's own find opens. Rows are built with textContent only.
JS = """(()=>{const $=i=>document.getElementById(i),q=$("q");if(!q)return;
const D=JSON.parse($("d").textContent),N=90,box=[$("r1"),$("r2")],lab=[$("h1"),$("h2"),$("l1"),$("l2")],cnt=$("cnt"),pg=$("pg");
const keep=box.map(e=>e.innerHTML),keepLab=lab.map(e=>e.textContent),keepCnt=cnt.textContent;
const span=(a,n)=>n?`${a+1}\\u2013${a+n}`:"\\u2014";
function table(list,msg){if(!list.length){const p=document.createElement("p");p.className="empty";p.textContent=msg;return p}
 const t=document.createElement("table");for(const g of list){const r=t.insertRow(),a=document.createElement("a"),c=r.insertCell();
 c.className="t";c.textContent=g[1];a.href="/g/"+g[0];a.textContent=a.title=g[2];r.insertCell().append(a)}return t}
function run(){const s=q.value.trim().toLowerCase();
 if(!s){box.forEach((e,i)=>e.innerHTML=keep[i]);lab.forEach((e,i)=>e.textContent=keepLab[i]);cnt.textContent=keepCnt;pg.hidden=false;return}
 const w=s.split(/\\s+/),m=D.filter(g=>w.every(x=>g[3].includes(x))),L=m.slice(0,N),R=m.slice(N,2*N);
 box[0].replaceChildren(table(L,`No gists match \\u201c${q.value.trim()}\\u201d.`));box[1].replaceChildren(table(R,m.length?"No more matches.":""));
 [lab[0].textContent,lab[1].textContent]=[span(0,L.length),span(N,R.length)];[lab[2].textContent,lab[3].textContent]=[lab[0].textContent,lab[1].textContent];
 cnt.textContent=`${m.length} of ${D.length}`+(m.length>2*N?" (first 180)":"");pg.hidden=true;$("t1").checked=true}
q.addEventListener("input",run);
q.addEventListener("keydown",e=>{if(e.key==="Escape"){q.value="";run();q.blur()}
 else if(e.key==="Enter"&&q.value.trim()){const a=document.querySelector("main a");if(a)location.href=a.getAttribute("href")}});
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="f"&&document.activeElement!==q){e.preventDefault();q.focus();q.select()}});
if(!/Mac|iPhone|iPad/.test(navigator.platform))$("k").textContent="Ctrl F";
if(q.value)run();
// New-content check: ask the server every 60 s while the tab is visible, and right away when it comes back into view.
const V=$("d").dataset.v,u=$("upd");
const check=()=>{if(document.hidden||!u.hidden)return;fetch("/version",{cache:"no-store"}).then(r=>r.ok?r.text():V).then(v=>{if(v!==V)u.hidden=false}).catch(()=>{})};
setInterval(check,60000);document.addEventListener("visibilitychange",check);
u.addEventListener("click",()=>location.reload())})();"""
JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
CSP = (
    f"default-src 'none'; script-src '{JS_HASH}'; style-src 'unsafe-inline'; img-src https: data:; connect-src 'self'; "
    "frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
)


def shell(name, body, script=False):
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=color-scheme content="light dark">'
        f"<title>{html.escape(name)}</title><style>{CSS}</style></head>"
        f'<body><div class="wrap">{body}</div>{f"<script>{JS}</script>" if script else ""}</body></html>'
    )


def rows(items, field="updated_at"):
    if not items:
        return '<p class="empty">No more gists.</p>'
    trs = "".join(
        f'<tr><td class="t">{stamp(g[field])}</td>'
        f'<td><a href="/g/{g["id"]}" title="{html.escape(title(g))}">{html.escape(title(g))}</a></td></tr>'
        for g in items
    )
    return f"<table>{trs}</table>"


SORTS = {"updated": "Updated", "created": "Created"}  # first one is the default


def sort_items(items, sort):
    """The cached list is already by last update; 'created' re-sorts a copy."""
    if sort == "created":
        return sorted(items, key=lambda g: g["created_at"], reverse=True)
    return items


def index_page(page, refresh, sort="updated"):
    sort = sort if sort in SORTS else "updated"
    field = f"{sort}_at"
    held = gists(refresh)  # the update check compares against this list, whatever the sort
    items = sort_items(held, sort)
    qs = "" if sort == "updated" else f"&sort={sort}"
    pages = max(1, -(-len(items) // (2 * ROWS)))
    page = min(max(page, 1), pages)
    left, right = columns(items, page)
    base = (page - 1) * 2 * ROWS
    r1, r2 = span(base, len(left)), span(base + ROWS, len(right))
    nav = "".join(
        f'<b aria-current="page">{p}</b>' if p == page else f'<a href="/?p={p}{qs}">{p}</a>' for p in range(1, pages + 1)
    )
    seg = "".join(
        f'<b aria-current="true">{label}</b>' if key == sort
        else f'<a href="/{"" if key == "updated" else "?sort=" + key}">{label}</a>'
        for key, label in SORTS.items()
    )
    return shell(
        "localgist",
        '<input type=radio name=t id=t1 checked aria-label="Show first column">'
        '<input type=radio name=t id=t2 aria-label="Show second column">'
        '<div class="top"><header><a class="brand" href="/">localgist</a>'
        f'<label class="search">{ICON}<input id=q type=search placeholder="Search {len(items)} gists" '
        'autocomplete=off spellcheck=false aria-label="Search gists"><kbd id=k>⌘F</kbd></label>'
        f'<nav class="sort" aria-label="Sort by date">{seg}</nav>'
        f'<div class="meta"><span id=cnt>{len(items)} gists</span>'
        f'<nav id=pg aria-label="Pages">{nav}<a href="/?p={page}{qs}&refresh=1" title="Reload from GitHub">↻</a></nav></div></header>'
        f'<div class="tabs"><label for=t1 id=l1>{r1}</label><label for=t2 id=l2>{r2}</label></div></div>'
        f'<main><section class="c1"><h2><span>Gists</span><span id=h1>{r1}</span></h2><div id=r1>{rows(left, field)}</div></section>'
        f'<section class="c2"><h2><span>Gists</span><span id=h2>{r2}</span></h2><div id=r2>{rows(right, field)}</div></section></main>'
        '<div role=status aria-live=polite><button id=upd class=toast type=button hidden>New update available. Click to refresh</button></div>'
        f'<script type="application/json" id=d data-v="{html.escape(signature(held))}">{search_data(items, field)}</script>',
        script=True,
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
        '<header><a class="brand" href="/">localgist</a><div class="meta"><a href="/">← All gists</a></div></header>'
        f'<div class="doc"><div class="bar"><span>{stamp(g["updated_at"])}</span>'
        f'<a href="{html.escape(g["html_url"])}">Open on GitHub ↗</a></div>{"".join(parts)}</div>',
    )
    if len(_pages) >= PAGE_CACHE:  # keep memory flat: drop the oldest rendered page
        _pages.pop(next(iter(_pages)))
    _pages[gid] = (g["updated_at"], out)
    return out


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # a stalled browser connection frees its thread after 30 s

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)  # only the one search script may run
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not host_ok(self.headers.get("Host", "")):  # blocks DNS-rebinding pages
            return self.send(403, "forbidden", "text/plain")
        url = urlsplit(self.path)
        if url.path == "/version":  # polled by an open page; does not count as use, so idle stop still works
            return self.send(200, version(), "text/plain")
        if url.path == "/ping":  # lets a second `pyup` find this server; does not count as use
            return self.send(200, "localgist", "text/plain")
        _last_hit[0] = time.clock_gettime(CLOCK)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                p = q.get("p", ["1"])[0]
                return self.send(200, index_page(int(p) if p.isdigit() else 1, "refresh" in q, q.get("sort", ["updated"])[0]))
            m = re.fullmatch(r"/g/([0-9a-f]{1,64})", url.path)
            if m:
                return self.send(200, gist_page(m[1]))
            return self.send(404, "not found", "text/plain")
        except Exception as e:  # GitHub down, token expired, etc.
            return self.send(502, shell("error", f"<p style='padding:24px'>GitHub fetch failed: {html.escape(str(e))}</p>"))


def watch_idle(server):
    while True:
        time.sleep(60)
        if time.clock_gettime(CLOCK) - _last_hit[0] > IDLE_SECONDS:
            server.shutdown()
            return


def warm():
    try:
        gists()
    except Exception as e:  # the first page request will retry and show the error
        print(f"localgist: warm-up failed: {e}", file=sys.stderr, flush=True)


def running_port():
    """Port of a localgist already serving on this machine, else None."""
    for p in range(PORT, PORT + PORT_TRIES):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{p}/ping", timeout=0.5) as r:
                if r.read() == b"localgist":
                    return p
        except OSError:
            pass
    return None


def bind():
    """First free port from PORT upward, so a taken 8770 never blocks startup."""
    for p in range(PORT, PORT + PORT_TRIES):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError:
            continue
    sys.exit(f"localgist: ports {PORT}–{PORT + PORT_TRIES - 1} are all taken.")


def main():
    global PORT, TOKEN
    want_open = "--open" in sys.argv
    if (p := running_port()) is not None:  # already up: just show it
        if want_open:
            webbrowser.open(f"http://localhost:{p}")
        print(f"localgist already running on http://localhost:{p}")
        return
    try:
        TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("localgist: no GitHub token. Run `gh auth login` first.")
    server = bind()
    PORT = server.server_address[1]  # host_ok() checks against the port actually bound
    threading.Thread(target=watch_idle, args=(server,), daemon=True).start()
    threading.Thread(target=warm, daemon=True).start()  # list loads while the browser opens
    print(f"localgist on http://localhost:{PORT} (stops after 60 min idle)", flush=True)
    if want_open:
        webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
