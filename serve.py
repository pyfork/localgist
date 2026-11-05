#!/usr/bin/env python3
"""localgist: a list of your own gists in a Finviz-news layout (2 columns x 90 rows).
Each row opens the gist on GitHub in a new tab.

Local only: binds 127.0.0.1, rejects any other Host header, reads the token
from `gh auth token` at start and keeps everything in memory. Stops itself
after 60 minutes with no page loads (sleep does not count).

    python3 serve.py          # run in the foreground
    python3 serve.py --open   # also open the browser (what `pyup` does)
"""
import base64
import hashlib
import hmac
import secrets
import html
import json
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

PORT = 8770  # first choice; main() moves up to PORT+9 if it is taken
PORT_TRIES = 10
ROWS = 90  # rows per column, as on finviz.com/news
IDLE_SECONDS = 60 * 60
API = "https://api.github.com"
TOKEN = ""  # set in main()
# CLOCK_UPTIME_RAW (macOS) and CLOCK_MONOTONIC (Linux) both pause while the machine sleeps
CLOCK = getattr(time, "CLOCK_UPTIME_RAW", time.CLOCK_MONOTONIC)

PER_PAGE = 30  # smaller pages come back much faster and are fetched in parallel
STALE_SECONDS = 300
CHECK_SECONDS = 60  # how often an open page may trigger a GitHub update check
_checked = [0.0]

_list = {"at": 0.0, "gists": []}
_gone = {}  # id -> when we deleted it; a refresh that began before the delete must not bring it back
GONE_GRACE = 60  # a list fetch that starts this long after a delete no longer needs that id filtered
_lock = threading.Lock()  # one list fetch at a time
_wlock = threading.Lock()  # a refresh's write and a delete never interleave
_last_hit = [time.clock_gettime(CLOCK)]


def gh(path, body=None, with_headers=False, method=None):
    req = urllib.request.Request(
        API + path,
        method=method,
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
    started = time.time()
    fresh = fetch_all()
    with _wlock:
        _list.update(at=time.time(), gists=[g for g in fresh if g["id"] not in _gone])
        for gid in [k for k, t in _gone.items() if t < started - GONE_GRACE]:  # this fetch began well after that delete
            del _gone[gid]


def gists(refresh=False):
    """Only the very first load (or ↻) fetches here. Keeping the list fresh is the page's update check
    (version(), a POST with the token), so a page load from another site never spends GitHub quota."""
    if refresh or not _list["at"]:
        with _lock:
            if refresh or not _list["at"]:  # the start-up warm-up may have filled it while we waited
                _load()
    return _list["gists"]


CSRF = secrets.token_urlsafe(24)  # per run; a page from another site cannot read it, so cannot send it


def delete_gist(gid):
    """Delete on GitHub (permanent), then drop it from the list and the content index."""
    gh(f"/gists/{gid}", method="DELETE")
    with _wlock:
        _gone[gid] = time.time()
        _list["gists"] = [g for g in _list["gists"] if g["id"] != gid]
    _content.pop(gid, None)
    return signature(_list["gists"])


def same_origin(headers):
    """Only our own page may change anything: right Host, matching Origin, same-site fetch, CSRF token."""
    host = headers.get("Host", "")
    origin, site = headers.get("Origin"), headers.get("Sec-Fetch-Site")
    return (host_ok(host) and origin == f"http://{host}" and site in (None, "same-origin")
            and hmac.compare_digest(headers.get("X-CSRF", ""), CSRF))


def signature(items):
    """Changes when a gist is added, edited (it moves to the top) or deleted (count drops)."""
    return f'{len(items)}-{items[0]["id"]}-{items[0]["updated_at"]}' if items else "0"


def _check_for_update():
    try:
        cur = _list["gists"]
        if not cur or time.time() - _list["at"] > STALE_SECONDS:  # full reload also catches deletes and renames on github.com
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
    and only while a page is open and asking (the browser polls /api/version)."""
    if time.time() - _checked[0] > CHECK_SECONDS and _lock.acquire(blocking=False):
        _checked[0] = time.time()
        threading.Thread(target=_check_for_update, daemon=True).start()
    return signature(_list["gists"])



# ---------- content index (only built once someone searches contents) ----------

CONTENT_CAP = 1_000_000  # characters kept per gist for search; the API truncates files over 1 MB anyway
_content = {}  # gist id -> (updated_at, lowercase text of all files)
_ilock = threading.Lock()  # one index build at a time


def _fetch_content(g):
    files = json.loads(gh(f'/gists/{g["id"]}'))["files"].values()
    _content[g["id"]] = (g["updated_at"], "\n".join(f.get("content") or "" for f in files).lower()[:CONTENT_CAP])


def _stale(items):
    return [g for g in items if _content.get(g["id"], ("",))[0] != g["updated_at"]]


def _build_index(items):
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for fut in [pool.submit(_fetch_content, g) for g in _stale(items)]:
                try:
                    fut.result()
                except Exception as e:  # one bad gist must not stop the rest
                    print(f"localgist: content fetch failed: {e}", file=sys.stderr, flush=True)
    finally:
        _ilock.release()


def ensure_index(items):
    for gid in set(_content) - {g["id"] for g in items}:  # gists deleted on GitHub
        _content.pop(gid, None)
    if _stale(items) and _ilock.acquire(blocking=False):
        threading.Thread(target=_build_index, args=(items,), daemon=True).start()


SCOPES = {"desc": "Description", "file": "File name", "content": "Content"}  # first one is the default


def search(q, scopes):
    """Ids of gists where every word appears in the chosen fields, in list order.
    Content results grow while the index builds; `ready` says when it is complete."""
    words = q.lower().split()[:10]
    items = gists()
    if "content" in scopes:
        ensure_index(items)
    ids = []
    for g in items if words else ():
        hay = " ".join(
            ((g.get("description") or "").lower() if "desc" in scopes else "",
             " ".join(g["files"]).lower() if "file" in scopes else "",
             _content.get(g["id"], ("", ""))[1] if "content" in scopes else "")
        )
        if all(w in hay for w in words):
            ids.append(g["id"])
    done = len(items) - len(_stale(items))
    return {"ids": ids, "done": done, "total": len(items), "ready": "content" not in scopes or done == len(items)}


# ---------- display preferences (one cookie, read by the server so pages render with no flash) ----------

FONTS = {"arial": "Arial", "source": "Source Sans 3", "helvetica": "Helvetica", "plex": "IBM Plex Sans"}  # first one is the default
OPENS = {"same": "Same window", "new": "New tab"}  # first one is the default; same window keeps Back working
THEMES = {"system": "System", "light": "Light", "dark": "Dark"}  # first one is the default
TOPS = {"on": "Show", "off": "Hide"}  # back-to-top button; first one is the default
MASTS = {"on": "Masthead", "off": "Minimal"}  # first one is the default; Minimal hides the strip, nameplate and chart
# Colour themes: paper, ink, accent (>= 4.5:1 on paper), link, rule, and the three-colour strip shown at the top.
PALETTES = {
    "finviz": {"name": "Finviz", "note": "White, light or dark", "paper": "#ffffff", "ink": "#1e222d", "accent": "#2f6bd1", "link": "#1d4ea8", "rule": "#e7e9ee", "muted": "#6b7280", "wash": ("#2f6bd1", "#8fa3c2", "#e7ebf2")},
    "ft": {"name": "FT salmon", "note": "Financial Times feel", "paper": "#fff1e5", "ink": "#33302e", "accent": "#0d7680", "link": "#0f5499", "rule": "#f0dcc9", "muted": "#66605c", "wash": ("#990f3d", "#0d7680", "#f2dfce")},
    "economist": {"name": "Economist red", "note": "The Economist feel", "paper": "#ffffff", "ink": "#0d0d0d", "accent": "#e3120b", "link": "#1f2e7a", "rule": "#e5e5e5", "muted": "#595959", "wash": ("#e3120b", "#1f2e7a", "#d6d6d6")},
    "cover": {"name": "Cover Ink", "note": "New Yorker cover feel", "paper": "#f7f1e3", "ink": "#1a1a1a", "accent": "#b8401c", "link": "#26457a", "rule": "#e7dcc4", "muted": "#6b6355", "wash": ("#3f63a8", "#d4683f", "#efe2c6")},
    "newsprint": {"name": "Newsprint", "note": "NYT-like", "paper": "#fbf8f1", "ink": "#1d1b16", "accent": "#2f4b73", "link": "#1f3a5f", "rule": "#e6dfd0", "muted": "#6a6356", "wash": ("#7c95b8", "#c8a874", "#e9e2d0")},
    "sage": {"name": "Sage Ledger", "note": "Quiet, archival", "paper": "#f3f4ee", "ink": "#22261f", "accent": "#4a6741", "link": "#35553a", "rule": "#dfe3d6", "muted": "#5f6658", "wash": ("#8fa682", "#c9a95e", "#e6e8dd")},
    "night": {"name": "Night Edition", "note": "Dark first", "paper": "#16181d", "ink": "#ece6d8", "accent": "#d3ad7a", "link": "#a9c1e6", "rule": "#2a2c31", "muted": "#a59e8f", "wash": ("#3e5578", "#8a7350", "#23262c")},
}
DEFAULT_PREFS = {"palette": "finviz", "font": "arial", "theme": "system", "mast": "on", "top": "on", "open": "same", "in": "desc"}
DISPLAY_KEYS = ("palette", "font", "theme", "mast", "top", "open")  # what the settings window edits and Reset restores


def read_prefs(cookie_header):
    p = dict(DEFAULT_PREFS)
    try:
        c = SimpleCookie(cookie_header or "")
    except CookieError:
        return p
    if "lg" in c:  # untrusted input: keep only known values
        q = parse_qs(unquote(c["lg"].value))
        for key, allowed in (("font", FONTS), ("palette", PALETTES), ("theme", THEMES), ("mast", MASTS), ("top", TOPS), ("open", OPENS)):
            if (v := q.get(key, [""])[0]) in allowed:
                p[key] = v
        if s := [x for x in dict.fromkeys(q.get("in", [""])[0].split(",")) if x in SCOPES]:
            p["in"] = ",".join(s)
    return p


MYT = ZoneInfo("Asia/Kuala_Lumpur")


def stamp(iso, now=None):
    """Finviz style, Malaysia time: '08:54AM' if today, else '04-Nov'."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MYT)
    now = now or datetime.now(MYT)
    return f"{dt:%I:%M%p}" if dt.date() == now.date() else f"{dt:%b-%d}"


def columns(items, page):
    start = (page - 1) * 2 * ROWS
    chunk = items[start : start + 2 * ROWS]
    return chunk[:ROWS], chunk[ROWS:]


ORDINALS = "one two three four five six seven eight nine ten".split()


def column_name(i):
    """Column i (0-based, across pages) as 'Page one', 'Page two', ..."""
    return f"Page {ORDINALS[i] if i < len(ORDINALS) else i + 1}"


def host_ok(host):
    return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"localgist.localhost:{PORT}")


def title(g):
    return g.get("description") or next(iter(g["files"]), g["id"])


def gist_url(g):
    u = g.get("html_url") or ""
    return u if u.startswith("https://gist.github.com/") else f'https://gist.github.com/{g["id"]}'


def search_data(items, field="updated_at"):
    """[id, url, stamp, title, desc, files, comments, created YYYY-MM] per gist (desc and files lowercased),
    safe to embed in a <script> block."""
    rows = [
        [g["id"], gist_url(g), stamp(g[field]), title(g), (g.get("description") or "").lower(), " ".join(g["files"]).lower(),
         int(g.get("comments") or 0), created_month(g)]
        for g in items
    ]
    return json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")  # every <, so "<!--<script>" cannot end the block early


DARK = ("color-scheme:dark;--page:#0f1012;--surface:#17181b;--fg:#ece6d8;--muted:#a59e8f;--faint:#746e62;--line:#2a2b2f;--rule:#ece6d8;"
        "--link:#a9c1e6;--seen:#c9b3ea;--hover:#1f2024;--field:#121315;--field-line:#34363b;--focus:#7aa2e3;--kbd:#1f2024;--accent:#8fb0dc;"
        "--shadow:0 1px 2px rgb(0 0 0 / .4),0 12px 32px rgb(0 0 0 / .35);--pop:0 16px 48px rgb(0 0 0 / .55),0 2px 6px rgb(0 0 0 / .4)")

CSS = """
:root{color-scheme:light;--r:6px;--rs:4px;--band:var(--surface);  /* Finviz: 6px controls and tables, 4px small items */
 --page:#ebe5d8;--surface:#fbf8f1;--fg:#1d1b16;--muted:#6a6356;--faint:#968e7e;--line:#e6dfd0;--rule:#1d1b16;
 --link:#1f3a5f;--seen:#5b4a7a;--hover:#f4efe3;--field:#f5f0e5;--field-line:#d8cfbc;--focus:#2f5f9e;--kbd:#fbf8f1;--accent:#2f4b73;--shadow:0 1px 2px rgb(16 24 40 / .05),0 8px 24px rgb(16 24 40 / .06);
 --pop:0 12px 40px rgb(16 24 40 / .16),0 2px 6px rgb(16 24 40 / .08)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){DARK}}
:root[data-theme=dark]{DARK}
html{--w1:#7c95b8;--w2:#c8a874;--w3:#e9e2d0}
html[data-palette=cover]{--w1:#3f63a8;--w2:#d4683f;--w3:#efe2c6}html[data-palette=sage]{--w1:#8fa682;--w2:#c9a95e;--w3:#e6e8dd}html[data-palette=night]{--w1:#3e5578;--w2:#8a7350;--w3:#3a3d44}
html[data-palette=cover]{--page:#ece2cc;--surface:#f7f1e3;--fg:#1a1a1a;--muted:#6b6355;--faint:#978d7b;--line:#e7dcc4;--rule:#1a1a1a;--link:#26457a;--seen:#6a3f6b;--hover:#f0e7d3;--field:#f2e9d7;--field-line:#d8caac;--kbd:#f7f1e3;--accent:#b8401c}
html[data-palette=sage]{--page:#e4e7dc;--surface:#f3f4ee;--fg:#22261f;--muted:#5f6658;--faint:#8c9285;--line:#dfe3d6;--rule:#22261f;--link:#35553a;--seen:#5a4a6e;--hover:#eceee5;--field:#eef0e8;--field-line:#cdd2c1;--kbd:#f3f4ee;--accent:#4a6741}
html[data-palette=night]{DARK;--accent:#d3ad7a}
html[data-palette=ft]{--page:#f2e5da;--surface:#fff1e5;--fg:#33302e;--muted:#66605c;--faint:#8f8780;--line:#f0dcc9;--rule:#33302e;--link:#0f5499;--seen:#593380;--hover:#f8e6d6;--field:#fbe8d8;--field-line:#e3cdb9;--kbd:#fff1e5;--accent:#0d7680;--w1:#990f3d;--w2:#0d7680;--w3:#f2dfce}
html[data-palette=economist]{--page:#f2f2f2;--surface:#ffffff;--fg:#0d0d0d;--muted:#595959;--faint:#8c8c8c;--line:#e5e5e5;--rule:#0d0d0d;--link:#1f2e7a;--seen:#5a2d82;--hover:#f7f7f7;--field:#f5f5f5;--field-line:#d4d4d4;--kbd:#ffffff;--accent:#e3120b;--w1:#e3120b;--w2:#1f2e7a;--w3:#d6d6d6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light])[data-palette=ft]{--accent:#45b3bd;--w1:#c2185b;--w2:#45b3bd;--w3:#3a3d44}:root:not([data-theme=light])[data-palette=economist]{--accent:#ff6b62;--w1:#ff6b62;--w2:#6f7fd6;--w3:#3a3d44}}
:root[data-theme=dark][data-palette=ft]{--accent:#45b3bd;--w1:#c2185b;--w2:#45b3bd;--w3:#3a3d44}:root[data-theme=dark][data-palette=economist]{--accent:#ff6b62;--w1:#ff6b62;--w2:#6f7fd6;--w3:#3a3d44}
html[data-palette=finviz]{--page:#f3f4f6;--surface:#ffffff;--fg:#1e222d;--muted:#6b7280;--faint:#9aa0ab;--line:#e7e9ee;--rule:#1e222d;--link:#1d4ea8;--seen:#6b3fa0;--hover:#f3f6fb;--field:#f5f6f8;--field-line:#d7dbe1;--kbd:#ffffff;--accent:#2f6bd1;--w1:#2f6bd1;--w2:#8fa3c2;--w3:#e7ebf2}
html[data-palette=finviz] .nameplate h1,html[data-palette=finviz] h2{font-family:var(--ff);letter-spacing:-.01em}  /* Finviz is all sans */
@media (prefers-color-scheme:dark){:root:not([data-theme=light])[data-palette=finviz]{--page:#191c23;--surface:#22262f;--fg:#dbdee6;--muted:#9aa1b0;--faint:#6f7686;--line:#333845;--rule:#dbdee6;--link:#8cb4ff;--seen:#c3a6f0;--hover:#2a2f3a;--field:#1b1e25;--field-line:#3a4050;--kbd:#2a2f3a;--accent:#4f8ff7;--w1:#4f8ff7;--w2:#4a5a78;--w3:#2a2f3a}}
:root[data-theme=dark][data-palette=finviz]{--page:#191c23;--surface:#22262f;--fg:#dbdee6;--muted:#9aa1b0;--faint:#6f7686;--line:#333845;--rule:#dbdee6;--link:#8cb4ff;--seen:#c3a6f0;--hover:#2a2f3a;--field:#1b1e25;--field-line:#3a4050;--kbd:#2a2f3a;--accent:#4f8ff7;--w1:#4f8ff7;--w2:#4a5a78;--w3:#2a2f3a}
@media (prefers-color-scheme:dark){:root:not([data-theme=light])[data-palette=cover]{--accent:#e48a62}:root:not([data-theme=light])[data-palette=sage]{--accent:#9cbf8f}:root:not([data-theme=light])[data-palette=night]{--accent:#d3ad7a}}
:root[data-theme=dark][data-palette=cover]{--accent:#e48a62}:root[data-theme=dark][data-palette=sage]{--accent:#9cbf8f}:root[data-theme=dark][data-palette=night]{--accent:#d3ad7a}
html{--ff:Arial,"Helvetica Neue",sans-serif;--fs:12px;--serif:Georgia,"Iowan Old Style","Times New Roman",serif}
html[data-font=helvetica]{--ff:Helvetica,"Helvetica Neue",Arial,sans-serif}
html[data-font=source]{--ff:"Source Sans 3",Arial,sans-serif}
html[data-font=plex]{--ff:"IBM Plex Sans",Arial,sans-serif;--fw:420}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--surface)}
body{margin:0;background:var(--surface);color:var(--fg);font:var(--fw,400) var(--fs)/1.45 var(--ff);-webkit-font-smoothing:antialiased}
button,input{font-family:inherit}
a{color:var(--link);text-decoration:none}a:visited{color:var(--seen)}a:hover{text-decoration:underline}
a:focus-visible,label:focus-visible,button:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:var(--rs)}
.wrap{width:min(1440px,100%);min-height:100vh;margin:0 auto;background:var(--surface);overflow:clip}  /* Finviz: list runs the full band, no card */
.top{background:var(--band)}.top .search input{background:var(--surface)}  /* Finviz: the header band has its own tint; the search box stays white */
/* Finviz layout: the header runs the full window width; only the list below sits in the centred column */
.bar{border-bottom:1px solid var(--line)}
.bar>header,.bar>.scope{max-width:1440px;margin:0 auto;border-bottom:0}
.bar.band{border-bottom:1px solid var(--line)}.band>.nameplate{max-width:1440px;margin:0 auto;border-bottom:0}
header{position:relative;display:flex;align-items:center;gap:16px;padding:14px 24px;border-bottom:1px solid var(--line)}
.brand{font-weight:700;font-size:15px;letter-spacing:-.01em;color:var(--fg)}.brand:visited{color:var(--fg)}
.search{position:relative;flex:1 1 220px;max-width:380px;min-width:0}
.search svg{position:absolute;left:10px;top:50%;translate:0 -50%;width:14px;height:14px;stroke:var(--faint);fill:none;stroke-width:2;pointer-events:none}
.search input{width:100%;height:34px;padding:0 52px 0 32px;font-size:13px;color:var(--fg);background:var(--field);border:1px solid var(--field-line);border-radius:var(--r);outline:none;transition:border-color .12s,box-shadow .12s}
.search input::placeholder{color:var(--faint)}
.search input:focus{border-color:var(--focus);box-shadow:0 0 0 3px color-mix(in srgb,var(--focus) 22%,transparent);background:var(--surface)}
.search input::-webkit-search-cancel-button{display:none}
kbd{position:absolute;right:8px;top:50%;translate:0 -50%;font:500 11px/1 ui-monospace,SFMono-Regular,monospace;color:var(--muted);background:var(--kbd);border:1px solid var(--field-line);border-bottom-width:2px;border-radius:var(--rs);padding:3px 6px;pointer-events:none}
.search input:focus+kbd{opacity:0}
.sort{display:inline-flex;padding:2px;gap:2px;background:var(--field);border:1px solid var(--field-line);border-radius:var(--r);flex:none}
.sort a,.sort b{height:28px;padding:0 11px;display:inline-grid;place-items:center;border-radius:var(--rs);font-size:12px;font-weight:500;color:var(--muted)}
.sort a:visited{color:var(--muted)}.sort a:hover{color:var(--fg);text-decoration:none;background:var(--hover)}
.sort b{background:var(--surface);color:var(--fg);font-weight:600;box-shadow:0 1px 2px rgb(0 0 0 / .12)}
.meta{margin-left:auto;display:flex;align-items:center;gap:12px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.meta nav{display:flex;gap:4px;align-items:center}
.meta nav a,.meta nav b{min-width:24px;height:24px;display:inline-grid;place-items:center;border-radius:var(--rs);padding:0 6px}
.meta nav b{background:var(--fg);color:var(--surface);font-weight:600}
.meta nav a:hover{background:var(--hover);text-decoration:none}
.icon-btn{flex:none;width:34px;height:34px;display:inline-grid;place-items:center;padding:0;border:1px solid var(--field-line);background:var(--field);border-radius:var(--r);color:var(--muted);cursor:pointer}
.hi{width:18px;height:18px;flex:none}
.icon-btn:hover{color:var(--fg)}.icon-btn.ghost{border-color:transparent;background:none}.icon-btn.ghost:hover{background:var(--hover)}
.refresh{color:var(--muted)}.refresh .hi{width:16px;height:16px}.refresh:visited{color:var(--muted)}.refresh:hover{color:var(--fg)}
.search svg.hi{width:16px;height:16px;stroke:var(--faint)}
dialog.modal{DARK;width:min(60vw,1100px);min-width:min(640px,calc(100% - 32px));max-height:calc(100% - 48px);padding:0;border:1px solid rgb(255 255 255 / .08);border-radius:var(--r);
 --surface:#2b2d31;--field:#1f2124;background:rgb(32 33 36 / .96);color:var(--fg);box-shadow:0 24px 80px rgb(0 0 0 / .55);overflow:auto}  /* Gmail-help style: always a dark panel */
dialog.modal::backdrop{background:rgb(0 0 0 / .62)}
dialog.modal[open]{animation:pop .16s ease-out}
@keyframes pop{from{opacity:0;transform:translateY(6px) scale(.98)}}
.m-head{display:flex;align-items:center;justify-content:space-between;padding:18px 18px 16px 32px;border-bottom:1px solid var(--line)}
.m-title{font-size:20px;font-weight:600;letter-spacing:-.01em}
.m-body{padding:20px 24px 22px;text-align:center}
.m-body fieldset{border:0;margin:0;padding:0;min-width:0}
.m-body.grid{display:grid;grid-template-columns:1fr 1fr;gap:34px 48px;padding:34px 40px 36px}
.m-body.grid .seg{max-width:420px;margin:0 auto}
.m-body.grid fieldset.wide{grid-column:1/-1}
.pcards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.pcard{position:relative;cursor:pointer;text-align:left}
.pcard input{position:absolute;opacity:0;pointer-events:none}
.pv{display:block;border-radius:var(--r);overflow:hidden;border:1px solid var(--field-line);transition:border-color .12s,transform .12s}
.pcard:hover .pv{border-color:var(--muted);transform:translateY(-1px)}
.pcard:has(input:checked) .pv{border-color:var(--focus);box-shadow:0 0 0 2px var(--focus)}
.pcard:has(input:focus-visible) .pv{outline:2px solid var(--focus);outline-offset:3px}
.pstrip{display:flex;height:18px}.pstrip i{flex:1}
.ppage{display:block;padding:7px 9px 6px}
.pname{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:4px;margin-bottom:3px;border-bottom:1px solid}
.pname strong{font:600 14px/1 Georgia,serif;letter-spacing:-.02em}
.pbars{display:flex;align-items:flex-end;gap:1.5px;height:24px}.pbars b{display:block;width:3px}
.ppage u{display:flex;gap:6px;text-decoration:none;font-size:9.5px;line-height:1.5;border-bottom:1px solid;white-space:nowrap;overflow:hidden}
.ppage em{font-style:normal;flex:none;width:38px}
.plabel{display:block;padding:7px 9px 8px;font-size:12.5px;font-weight:600;color:var(--fg);background:rgb(0 0 0 / .2)}
.plabel small{display:block;font-weight:400;font-size:11px;color:var(--muted)}.m-body .seg span{height:38px;font-size:14px}
.m-body legend{padding:0;margin:0 auto 14px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.m-foot{position:sticky;bottom:0;padding:14px 20px 14px 32px;border-top:1px solid var(--line);background:#1a1b1e}  /* Save stays in view on short screens */
.m-row{display:flex;align-items:center;gap:8px}
.m-msg{margin-right:auto;display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12.5px}
.m-msg.ok{color:#1f8a4c}.m-msg.dirty{color:var(--fg);font-weight:600}
.m-msg .hi{width:16px;height:16px}
.ask .m-msg{color:var(--fg);font-weight:600}
.btn{height:34px;padding:0 14px;border-radius:var(--r);border:1px solid var(--field-line);background:var(--surface);color:var(--fg);font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--fg)}
.btn.primary{background:var(--fg);color:var(--surface);border-color:var(--fg)}.btn.primary:hover{filter:brightness(1.15)}
.btn.primary:disabled{opacity:.35;cursor:default;filter:none}
.btn.danger{background:#c62828;border-color:#c62828;color:#fff}.btn.danger:hover{filter:brightness(1.1)}
.seg{display:flex;padding:2px;gap:2px;background:var(--field);border:1px solid var(--field-line);border-radius:var(--r)}
.seg label{flex:1;position:relative;cursor:pointer}
.seg input,.chip input{position:absolute;opacity:0;pointer-events:none}
.f-same,.f-new{font-family:inherit}
.seg span{display:grid;place-items:center;height:30px;padding:0 6px;border-radius:var(--rs);font-size:13px;color:var(--muted);white-space:nowrap}
.seg label:hover span{color:var(--fg)}
.seg label:has(input:checked) span{background:var(--surface);color:var(--fg);font-weight:600;box-shadow:0 1px 2px rgb(0 0 0 / .12)}
.seg label:has(input:focus-visible) span{outline:2px solid var(--focus)}
.f-arial{font-family:Arial,sans-serif}.f-source{font-family:"Source Sans 3",Arial,sans-serif}.f-helvetica{font-family:Helvetica,Arial,sans-serif}.f-plex{font-family:"IBM Plex Sans",Arial,sans-serif}
.hint{margin:8px 0 0;color:var(--faint);font-size:11.5px}
.scope{display:flex;align-items:center;gap:8px;padding:8px 24px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted)}
.chip{position:relative;display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px 0 8px;border:1px solid var(--field-line);border-radius:var(--r);cursor:pointer;color:var(--muted);user-select:none;white-space:nowrap}
.chip .hi{width:14px;height:14px;padding:1px;border-radius:var(--rs);border:1.5px solid var(--faint);color:transparent}
.chip:hover{color:var(--fg)}
.chip:has(input:checked){color:var(--fg)}  /* the tick alone shows the choice; no dark outline */
.chip:has(input:checked) .hi{background:var(--fg);border-color:var(--fg);color:var(--surface)}
.chip:has(input:focus-visible){outline:2px solid var(--focus);outline-offset:2px}
#st{margin-left:auto;color:var(--faint);font-variant-numeric:tabular-nums}
input[name=t]{position:absolute;opacity:0;pointer-events:none}
.tabs{display:none}
main{display:grid;grid-template-columns:1fr 1fr;gap:28px;padding:12px 20px 28px}
h2{margin:14px 0 2px;padding:8px 0 6px;font:700 18px/1.15 var(--serif);letter-spacing:-.01em}
html[data-palette=economist] h2::before{content:"";display:block;width:40px;height:4px;margin-bottom:8px;background:var(--accent)}  /* Economist: short red bar over section names */
html:not([data-palette=finviz]){--r:0;--rs:0}  /* editorial themes are square */
html[data-palette=finviz]{--band:#eceef2}:root[data-theme=dark][data-palette=finviz]{--band:#2a2f3a}
@media (prefers-color-scheme:dark){:root:not([data-theme=light])[data-palette=finviz]{--band:#2a2f3a}}  /* Finviz only: tinted header band; editorial themes keep one paper colour */
.strip{display:flex;height:8px}.strip i{flex:1}
.strip i:nth-child(1){background:var(--w1)}.strip i:nth-child(2){background:var(--w2)}.strip i:nth-child(3){background:var(--w3)}
.nameplate{display:grid;grid-template-columns:1fr auto;align-items:end;gap:28px;padding:14px 24px 12px;border-bottom:1px solid var(--line)}
.nameplate h1{margin:0;font:700 40px/.95 var(--serif);letter-spacing:-.03em}
.dateline{margin:10px 0 0;font:italic 14px/1.3 var(--serif);color:var(--muted)}
.chart-wrap{position:relative;margin:0;display:grid;gap:4px;justify-items:end}
.chart .hit{fill:transparent;cursor:pointer;outline:none}.chart .hit:focus-visible{fill:color-mix(in srgb,var(--accent) 14%,transparent)}
.chart .bar.sel{fill:var(--fg);opacity:1}
.tip{position:absolute;z-index:3;translate:-50% calc(-100% - 6px);padding:4px 8px;border-radius:var(--rs);background:var(--fg);color:var(--surface);font:600 11px/1.2 var(--ff);white-space:nowrap;pointer-events:none}
.period{display:flex;align-items:center;gap:12px;margin-top:2px}
.overview{position:relative;width:372px;height:22px}
.preset{width:22px;height:22px;display:grid;place-items:center;padding:0;border:1px solid var(--field-line);border-radius:var(--rs);background:var(--surface);color:var(--muted);cursor:pointer}
.preset .hi{width:13px;height:13px}.preset:hover{color:var(--fg);border-color:var(--fg)}
.chart g.m:hover .bar,.chart g.m:focus-within .bar{fill:var(--fg);opacity:1}
.chart g.m:hover .hit{fill:color-mix(in srgb,var(--accent) 9%,transparent)}
.overview svg{position:absolute;left:0;right:0;top:3px;width:100%;height:16px;fill:var(--faint);opacity:.7}
.win{position:absolute;top:0;bottom:0;background:color-mix(in srgb,var(--accent) 16%,transparent);border-block:1px solid var(--accent);cursor:grab;touch-action:none;outline:none}
.win:hover{background:color-mix(in srgb,var(--accent) 26%,transparent)}.win.drag{cursor:grabbing;background:color-mix(in srgb,var(--accent) 32%,transparent)}
.win:focus-visible{box-shadow:0 0 0 2px var(--focus)}
.grip{position:absolute;top:-3px;bottom:-3px;width:12px;translate:-50% 0;display:grid;place-items:center;border:1px solid var(--accent);border-radius:3px;background:var(--surface);cursor:ew-resize;touch-action:none;outline:none}
.grip::after{content:"";width:3px;height:10px;border-inline:1px solid var(--accent)}
.grip:hover,.grip:focus-visible{background:var(--accent)}.grip:hover::after,.grip:focus-visible::after{border-color:var(--surface)}
.grip:focus-visible{box-shadow:0 0 0 3px color-mix(in srgb,var(--focus) 35%,transparent)}
.chart-hint{margin:2px 0 0;font:italic 11px/1.3 var(--serif);color:var(--faint)}
.chart-row{display:flex;align-items:center;gap:12px}.preset.off{visibility:hidden}  /* each ✕ sits beside what it resets and keeps its space, so rows never shift */
@media (min-width:1024px){html:not([data-mast=off]) .scope .mchip{display:none}}  /* the chart has its own clear button */
.mchip{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px;border:1px solid var(--accent);border-radius:var(--r);background:color-mix(in srgb,var(--accent) 12%,var(--surface));color:var(--fg);font:600 12px var(--ff);cursor:pointer}
.mchip::after{content:"\\00d7";font-size:14px;color:var(--muted)}
.chart-wrap figcaption{font:700 10px/1 var(--ff);letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.chart{width:372px;height:74px;color:var(--accent);overflow:visible}
.chart .bar{fill:currentColor;opacity:.5}.chart .pk{opacity:1}
.chart text{font:10px var(--serif);fill:var(--muted)}.chart .ann{font-style:italic;fill:var(--fg)}
.chart line{stroke:var(--rule);stroke-width:1}
header .brandrow{display:none}.brandrow{align-items:center;gap:8px}.np-row{display:flex;align-items:center;gap:10px}.np-row .refresh .hi{width:20px;height:20px}  /* refresh sits right of the title */
html[data-mast=off] .strip,html[data-mast=off] .band{display:none}html[data-mast=off] header .brandrow{display:inline-flex}  /* Minimal layout */
@media (max-width:1023px){.strip,.band,.m-mast{display:none}header .brandrow{display:inline-flex}}  /* phones and portrait tablets: never a masthead, so no Layout choice */
.empty.quiet{padding:28px 0 8px;text-align:center;font:italic 16px/1.4 var(--serif)}
table{width:100%;border-collapse:separate;border-spacing:0;table-layout:fixed;border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
tr:last-child td{border-bottom:0}
tr:hover td{background:var(--hover)}
td{height:27px;padding:4px 4px 3px;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:top}
td.t{width:68px;padding:5px 8px 0 4px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.empty{color:var(--muted);padding:14px 0;margin:0}
td.act{width:0;padding:0;overflow:visible;white-space:nowrap}
.acts{position:absolute;right:0;top:0;bottom:0;display:flex;align-items:center;padding:0 2px 0 18px;background:linear-gradient(90deg,transparent,var(--hover) 16px);opacity:0}
tr:hover .acts,tr:focus-within .acts{opacity:1}
td a{display:inline-block;max-width:calc(100% - 8px);overflow:hidden;text-overflow:ellipsis;vertical-align:top}
.cd{display:inline-block;width:5px;height:5px;margin-left:3px;border-radius:50%;background:var(--link);vertical-align:top}  /* superscript dot: this gist has comments */
.ib{width:26px;height:22px;display:inline-grid;place-items:center;padding:0;border:0;border-radius:var(--rs);background:none;color:var(--faint);cursor:pointer;opacity:0}
.ib .hi{width:15px;height:15px}
.stamp{padding:0;border:0;background:none;color:inherit;font:inherit;cursor:pointer}.stamp:hover{color:var(--link);text-decoration:underline}.stamp.done{color:#1f8a4c;text-decoration:none}  /* the date is the copy-link control */
tr:hover .ib,tr:focus-within .ib,.ib:focus-visible{opacity:1}
.ib:hover{background:var(--surface);color:var(--fg)}.ib.del:hover{color:#c62828}
@media (hover:none){.ib{opacity:.7}.acts{opacity:1}}
td.act{position:relative}
tr.confirming .acts{opacity:1}tr.confirming td{background:color-mix(in srgb,#c62828 9%,var(--surface))}
.rc{position:absolute;right:0;top:0;bottom:0;z-index:1;display:flex;align-items:center;gap:6px;padding:0 4px 0 12px;white-space:nowrap;
 background:linear-gradient(90deg,transparent,color-mix(in srgb,#c62828 9%,var(--surface)) 10px)}
.rc-q{font-weight:600;color:#c62828;font-size:12px}
.rc button{height:20px;padding:0 9px;border-radius:var(--rs);font:600 12px/1 var(--ff);cursor:pointer;border:1px solid var(--field-line);background:var(--surface);color:var(--fg)}
.rc .rc-yes{background:#c62828;border-color:#c62828;color:#fff}.rc-q{font-variant-numeric:tabular-nums}.rc button:hover{filter:brightness(1.08)}.rc button:disabled{opacity:.6;cursor:default}
.m-row.end{justify-content:flex-end}
.m-body .m-body .m-body .btn.link{border-color:transparent;background:none;color:var(--muted);padding:0 8px}.btn.link:hover{color:var(--fg);border-color:transparent;text-decoration:underline}
.totop{position:fixed;right:24px;bottom:24px;z-index:4;width:40px;height:40px;display:grid;place-items:center;padding:0;border:1px solid var(--field-line);border-radius:var(--r);background:var(--surface);color:var(--muted);box-shadow:var(--shadow);cursor:pointer}
.totop:hover{color:var(--fg);border-color:var(--fg)}.totop .hi{width:20px;height:20px}html[data-top=off] .totop{display:none}
.note{position:fixed;left:50%;bottom:24px;translate:-50% 0;z-index:7;padding:9px 14px;border-radius:var(--r);background:var(--fg);color:var(--surface);font-weight:600;font-size:13px;box-shadow:0 8px 28px rgb(0 0 0 / .25)}
.toast{position:fixed;left:50%;bottom:24px;translate:-50% 0;z-index:5;display:flex;align-items:center;gap:10px;padding:10px 16px;border:0;border-radius:var(--r);background:var(--fg);color:var(--surface);font-weight:600;font-size:13px;line-height:1.2;box-shadow:0 8px 28px rgb(0 0 0 / .25);cursor:pointer;animation:rise .2s ease-out}
.toast::before{content:"";width:8px;height:8px;border-radius:50%;background:#3ccf7a}
.toast:hover{filter:brightness(1.1)}
@keyframes rise{from{opacity:0;translate:-50% 8px}}
@media (prefers-reduced-motion:reduce){.toast{animation:none}}
@media (max-width:767px){
 html{--fs:14px}
 .wrap{width:auto;margin:0;border:0;border-radius:0;box-shadow:none;min-height:100vh}
 .top{position:sticky;top:0;z-index:2}
 header{flex-wrap:wrap;gap:10px 10px;padding:12px 16px}
 .search{order:3;flex:1 1 140px;max-width:none}.search input{height:40px;font-size:16px;padding-right:10px}
 .sort{order:4}.sort a,.sort b{height:34px;padding:0 10px;font-size:13px}
 .meta{gap:8px}
 kbd{display:none}
 .m-row.ask{flex-wrap:wrap}
 .seg:has(input[name=font]){flex-wrap:wrap}.seg:has(input[name=font]) label{flex:1 1 45%}  /* four fonts: 2 x 2 on phones */
 dialog.modal{width:calc(100% - 24px);min-width:0}.m-body.grid{grid-template-columns:1fr;gap:26px;padding:24px 18px}
 .m-head{padding:14px 12px 12px 18px}.m-foot{padding:12px 14px 12px 18px}
 .pcards{grid-template-columns:repeat(2,minmax(0,1fr))}.ask .m-msg{flex-basis:100%;margin-bottom:4px}
 .scope{display:none;padding:8px 16px;gap:6px;overflow-x:auto}.scope>span:first-child{display:none}
 .top:focus-within .scope,.top:has(#q:not(:placeholder-shown)) .scope{display:flex}
 .tabs{display:flex;border-bottom:1px solid var(--line)}
 .tabs label{flex:1;text-align:center;padding:11px 0;font-weight:600;font-size:13px;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;font-variant-numeric:tabular-nums}
 #t1:checked~.top label[for=t1],#t2:checked~.top label[for=t2]{color:var(--fg);border-color:var(--fg)}
 #t1:focus-visible~.top label[for=t1],#t2:focus-visible~.top label[for=t2]{outline:2px solid var(--focus);outline-offset:-2px}
 main{display:block;padding:0 16px 32px}h2{display:none}
 header .meta{flex:1 1 auto;justify-content:space-between}
 header .search{flex:1 1 180px}
 .scope .mchip{display:inline-flex}
 .c2,#t2:checked~.wrap .c1{display:none}#t2:checked~.wrap .c2{display:block}
 td{height:48px;padding:8px 0;white-space:normal}
 td a{display:-webkit-box;max-width:none;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 td:has(>.cd){position:relative;padding-left:9px}.cd{position:absolute;left:0;top:15px;margin:0}  /* phones: dot sits just before the title */
 td.t{width:68px;padding:0;font-size:12px}.stamp{display:flex;justify-content:flex-end;align-items:flex-start;width:100%;min-height:48px;padding:10px 8px 0 4px}  /* whole cell is the tap target */
 td.act{display:none}  /* delete is desktop-only */
 .toast{bottom:16px;width:calc(100% - 32px);justify-content:center}
 .brandrow .refresh{display:grid;place-items:center;padding:14px 22px 14px 6px;margin:-14px -22px -14px -6px}  /* 44px tap area, same layout */
 .totop{right:16px;bottom:16px;width:48px;height:48px}.totop .hi{width:24px;height:24px}  /* 48px: Apple and Google tap minimums */
}
"""

def hero(d, size=24):
    """A Heroicons v2 outline icon (24px) or solid mini icon (16px). Heroicons are the only icons used."""
    if size == 16:
        return f'<svg class="hi" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">{d}</svg>'
    return (f'<svg class="hi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{d}</svg>')


CSS = CSS.replace("DARK", DARK)

ICON = hero('<path d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z"/>')  # magnifying-glass
GEAR = hero(  # cog-6-tooth
    '<path d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"/>'
    '<path d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"/>'
)
REFRESH = hero('<path d="M19.5 12c0-1.232-.046-2.453-.138-3.662a4.006 4.006 0 0 0-3.7-3.7 48.678 48.678 0 0 0-7.324 0 4.006 4.006 0 0 0-3.7 3.7c-.017.22-.032.441-.046.662M19.5 12l3-3m-3 3-3-3m-12 3c0 1.232.046 2.453.138 3.662a4.006 4.006 0 0 0 3.7 3.7 48.656 48.656 0 0 0 7.324 0 4.006 4.006 0 0 0 3.7-3.7c.017-.22.032-.441.046-.662M4.5 12l3 3m-3-3-3 3"/>')  # arrow-path-rounded-square
TRASH = hero('<path d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"/>')  # trash
XMARK = hero('<path d="M6 18 18 6M6 6l12 12"/>')  # x-mark
CHEVRON_UP = hero('<path d="m4.5 15.75 7.5-7.5 7.5 7.5"/>')  # chevron-up
CHECK = hero('<path fill-rule="evenodd" d="M12.416 3.376a.75.75 0 0 1 .208 1.04l-5 7.5a.75.75 0 0 1-1.154.114l-3-3a.75.75 0 0 1 1.06-1.06l2.353 2.353 4.493-6.74a.75.75 0 0 1 1.04-.207Z" clip-rule="evenodd"/>', 16)  # check (16 solid)
FONT_CSS = {"source": "https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap",
            "plex": "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400..700&display=swap"}  # Plex is what Finviz uses

# One script for the list page, pinned in the CSP by its hash.
# Search: Cmd/Ctrl+F focuses it (again inside the box: the browser's own find). Description and
# file-name matches run here; content matches come from /api/search. Rows are built with textContent only.
JS = r"""(()=>{const $=i=>document.getElementById(i),q=$("q");if(!q)return;
// anything that can spend GitHub quota is a POST with the per-run token, which another site cannot read or send
const H={"X-CSRF":$("d").dataset.csrf},post=u=>fetch(u,{method:"POST",headers:H,cache:"no-store"});
let sel=null;  // month picked on the chart, "YYYY-MM"
const root=document.documentElement,D=JSON.parse($("d").textContent),N=90,box=[$("r1"),$("r2")],cnt=$("cnt"),pg=$("pg"),st=$("st");
const keep=box.map(e=>e.innerHTML);let keepCnt=cnt.textContent;
const prefs={palette:root.dataset.palette,font:root.dataset.font,theme:root.dataset.theme,mast:root.dataset.mast,top:root.dataset.top,open:root.dataset.open,in:root.dataset.in};
// one cookie, read by the server on every page load, so settings survive restarts of localgist
const store=()=>{document.cookie="lg="+encodeURIComponent(new URLSearchParams(prefs))+";path=/;max-age=31536000;samesite=strict"};
const targets=o=>document.querySelectorAll("main a").forEach(a=>{if(o==="new")a.target="_blank";else a.removeAttribute("target")});
const boxes=[...document.querySelectorAll(".scope input")],scope=()=>boxes.filter(b=>b.checked).map(b=>b.value);
function table(list,msg){if(!list.length){const p=document.createElement("div");p.className="empty";
 if(msg.startsWith("No gists match")){p.className="empty quiet";p.textContent=msg.replace("No gists match","Nothing matches")}else p.textContent=msg;return p}
 const t=document.createElement("table");for(const g of list){const r=t.insertRow(),c=r.insertCell(),a=document.createElement("a");
 c.className="t";const sb=document.createElement("button");sb.className="stamp";sb.type="button";sb.dataset.u=g[1];sb.title="Copy link";sb.ariaLabel="Copy link to "+g[3];sb.textContent=g[2];c.append(sb);
 a.href=g[1];if(prefs.open==="new")a.target="_blank";a.rel="noopener noreferrer";a.textContent=a.title=g[3];r.insertCell().append(a);
 if(g[6]){const d=document.createElement("sup");d.className="cd";d.role="img";d.title=d.ariaLabel=`${g[6]} comment${g[6]===1?"":"s"}`;a.after(d)}
 const x=r.insertCell();x.className="act";x.append($("act-tpl").content.cloneNode(true));
 const dl=x.querySelector("button");dl.dataset.id=g[0];dl.ariaLabel="Delete "+g[3]}return t}
function show(m,s){const L=m.slice(0,N),R=m.slice(N,2*N);
 box[0].replaceChildren(table(L,`No gists match “${s}”.`));box[1].replaceChildren(table(R,m.length?"No more matches.":""));
 cnt.textContent=`${m.length} of ${D.length}`+(m.length>2*N?" (first 180)":"");pg.hidden=true;$("t1").checked=true}
let seq=0,timer;
function remember(raw){const u=new URL(location.href);if(raw)u.searchParams.set("q",raw);else u.searchParams.delete("q");history.replaceState(null,"",u)}
function run(){const raw=q.value.trim(),s=raw.toLowerCase(),sc=scope(),my=++seq;clearTimeout(timer);st.textContent="";remember(raw);
 const base=sel?D.filter(g=>g[7]===sel):D,mc=$("mchip");
 if(mc){mc.hidden=!sel;if(sel)mc.textContent="Created "+new Date(sel+"-01T00:00").toLocaleString("en",{month:"long",year:"numeric"})}
 if(!s&&sel){show(base,"");return}
 if(!s){box.forEach((e,i)=>e.innerHTML=keep[i]);targets(prefs.open);cnt.textContent=keepCnt;pg.hidden=false;return}
 const w=s.split(/\s+/);
 if(!sc.includes("content")){show(base.filter(g=>{const h=(sc.includes("desc")?g[4]:"")+" "+(sc.includes("file")?g[5]:"");return w.every(x=>h.includes(x))}),raw);return}
 st.textContent="Searching contents…";
 timer=setTimeout(()=>post("/api/search?"+new URLSearchParams({q:raw,in:sc.join(",")})).then(r=>r.json()).then(j=>{if(my!==seq)return;
  const ids=new Set(j.ids);show(base.filter(g=>ids.has(g[0])),raw);
  st.textContent=j.ready?"":`Indexing contents… ${j.done} of ${j.total}`;if(!j.ready)timer=setTimeout(run,800)})
  .catch(()=>{if(my===seq)st.textContent="Content search failed"}),150)}
q.addEventListener("input",run);
q.addEventListener("keydown",e=>{if(e.key==="Escape"){q.value="";run();q.blur()}
 else if(e.key==="Enter"&&q.value.trim()){const a=document.querySelector("main a");if(a){if(prefs.open==="new")window.open(a.href,"_blank","noopener,noreferrer");else location.href=a.href}}});
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="f"&&document.activeElement!==q&&!$("settings").open){e.preventDefault();q.focus();q.select()}});
boxes.forEach(b=>b.addEventListener("change",()=>{if(!scope().length)boxes[0].checked=true;prefs.in=scope().join(",");store();
 if(b.value==="content"&&b.checked)post("/api/search?in=content").catch(()=>{});run()}));
// Settings modal: choices preview live; Save keeps them; leaving with unsaved changes asks first.
const dlg=$("settings"),gear=$("gear"),ask=$("m-ask"),norm=$("m-normal"),msg=$("m-msg"),btn=$("m-save");
const KEYS=["palette","font","theme","mast","top","open"];
let saved=Object.fromEntries(KEYS.map(k=>[k,prefs[k]])),justSaved=false;
const pick=n=>dlg.querySelector(`input[name=${n}]:checked`).value;
const form=()=>({palette:pick("palette"),font:pick("font"),theme:pick("theme"),mast:pick("mast"),top:pick("top"),open:pick("open")});
const dirty=()=>{const f=form();return KEYS.some(k=>f[k]!==saved[k])};
function apply(p){root.dataset.palette=p.palette;root.dataset.mast=p.mast;root.dataset.top=p.top;root.dataset.font=p.font;root.dataset.theme=p.theme;targets(p.open);
 const u=$("d-fonts").dataset[p.font];if(u&&!$("f-"+p.font)){const l=document.createElement("link");l.id="f-"+p.font;l.rel="stylesheet";l.href=u;document.head.append(l)}}
function status(){const d=dirty();btn.disabled=!d;msg.className="m-msg"+(d?" dirty":justSaved?" ok":"");
 if(justSaved&&!d){msg.innerHTML=$("ok-icon").innerHTML;msg.append(" Saved. Esc to close")}else msg.textContent=d?"Unsaved changes":"Choices preview live. Save to keep them."}
function setForm(p){for(const k of KEYS)dlg.querySelector(`input[name=${k}][value=${p[k]}]`).checked=true}
const changed=()=>{apply(form());justSaved=false;status();asking(false)};
function asking(o){ask.hidden=!o;norm.hidden=o;if(o)$("m-keep").focus()}
function close(){asking(false);dlg.close();gear.focus()}
function tryClose(){if(!ask.hidden)return asking(false);if(dirty())asking(true);else close()}
function save(){saved=form();Object.assign(prefs,saved);store();close();say("Settings saved")}
gear.addEventListener("click",()=>{setForm(saved);apply(saved);justSaved=false;status();asking(false);dlg.showModal()});
dlg.addEventListener("change",changed);
$("m-reset").addEventListener("click",()=>{setForm(JSON.parse(dlg.dataset.defaults));changed()});
dlg.addEventListener("cancel",e=>{e.preventDefault();tryClose()});
dlg.addEventListener("click",e=>{if(e.target===dlg)tryClose()});
dlg.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="s"){e.preventDefault();if(dirty())save()}});
// Cmd/Ctrl+K opens the settings window from anywhere; pressed again it closes it (asking first if unsaved)
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){e.preventDefault();if(dlg.open)tryClose();else gear.click()}});
$("m-close").addEventListener("click",tryClose);btn.addEventListener("click",save);
$("m-keep").addEventListener("click",()=>asking(false));
$("m-discard").addEventListener("click",()=>{setForm(saved);apply(saved);close()});
if(!/Mac|iPhone|iPad/.test(navigator.platform)){$("k").textContent="Ctrl F";gear.title="Display settings (Ctrl K)"}
// Row actions: the date copies the link; delete (asks first; the server checks this page's CSRF token)
let noteT;const note=$("note"),say=m=>{note.textContent=m;note.hidden=false;clearTimeout(noteT);noteT=setTimeout(()=>note.hidden=true,2200)};
// clipboard API first; the old execCommand path covers browsers that refuse it
const copyText=t=>(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).catch(()=>{const a=document.createElement("textarea");
 a.value=t;a.style.position="fixed";a.style.opacity="0";document.body.append(a);a.select();const ok=document.execCommand("copy");a.remove();if(!ok)throw new Error("copy")});
// Delete asks right in the row: "Delete for good? Yes / No". No is focused; Esc or clicking elsewhere also means No.
let pending=null;
function cancel(){if(!pending)return;const{tr,box,btn}=pending;box.remove();tr.classList.remove("confirming");pending=null;btn.focus()}
function askDelete(btn){if(queued.has(btn.dataset.id))return;cancel();const tr=btn.closest("tr");const box=$("confirm-tpl").content.firstElementChild.cloneNode(true);
 btn.parentElement.append(box);tr.classList.add("confirming");pending={tr,box,btn,id:btn.dataset.id,name:tr.querySelector("a").textContent};box.querySelector(".rc-no").focus()}
// Yes starts a 9 s countdown with Undo; leaving or reloading the page sends the delete at once (keepalive).
const queued=new Map(),WAIT=9;
const send=(o,keep)=>fetch("/api/gists/"+encodeURIComponent(o.id),{method:"DELETE",headers:H,keepalive:!!keep});
function confirmDelete(){const o=pending;if(!o)return;pending=null;queued.set(o.id,o);o.left=WAIT;
 o.box.innerHTML='<span class="rc-q"></span><button class="rc-undo" type=button>Undo</button>';
 const qq=o.box.querySelector(".rc-q"),tick=()=>{qq.textContent=`Deleting in ${o.left}s`};tick();o.box.querySelector(".rc-undo").focus();
 o.t=setInterval(()=>{if(--o.left>0)tick();else{clearInterval(o.t);fire(o)}},1000)}
function undo(o){clearInterval(o.t);queued.delete(o.id);o.box.remove();o.tr.classList.remove("confirming");if(o.btn.isConnected)o.btn.focus();say(`Kept \u201c${o.name}\u201d`)}
function fire(o){const qq=o.box.querySelector(".rc-q");qq.textContent="Deleting\u2026";o.box.querySelector(".rc-undo").disabled=true;
 send(o).then(r=>r.ok?r.json():r.text().then(t=>{throw new Error(t||r.status)}))
 .then(j=>{V=j.v;queued.delete(o.id);drop(o.id);say(`Deleted \u201c${o.name}\u201d`)})
 .catch(()=>{queued.delete(o.id);o.box.remove();o.tr.classList.remove("confirming");say("Could not delete. Try again.")})}
addEventListener("pagehide",()=>{for(const o of queued.values()){clearInterval(o.t);send(o,true).catch(()=>{})}queued.clear()});
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&pending){e.preventDefault();cancel()}});
document.addEventListener("click",e=>{if(pending&&!pending.box.contains(e.target)&&!e.target.closest(".ib.del"))cancel()});
document.querySelector("main").addEventListener("click",e=>{const s=e.target.closest(".stamp");
 if(s){copyText(s.dataset.u).then(()=>{const old=s.textContent;s.textContent="Copied";s.classList.add("done");say("Link copied");
  setTimeout(()=>{s.textContent=old;s.classList.remove("done")},1400)},()=>say("Could not copy"));return}
 const b=e.target.closest(".ib");if(b)askDelete(b)});
document.querySelector("main").addEventListener("click",e=>{if(e.target.closest(".rc-no"))cancel();else if(e.target.closest(".rc-yes"))confirmDelete();
 else if(e.target.closest(".rc-undo")){const o=[...queued.values()].find(x=>x.box.contains(e.target));if(o)undo(o)}});
function drop(id){const sel=`button[data-id="${CSS.escape(id)}"]`;document.querySelectorAll(sel).forEach(b=>b.closest("tr").remove());
 for(let i=0;i<keep.length;i++){const t=document.createElement("div");t.innerHTML=keep[i];t.querySelectorAll(sel).forEach(b=>b.closest("tr").remove());keep[i]=t.innerHTML}
 const k=D.findIndex(g=>g[0]===id);if(k>=0)D.splice(k,1);keepCnt=`${D.length} gists`;if(!q.value.trim())cnt.textContent=keepCnt;q.placeholder=`Search ${D.length} gists`}
{const p=new URLSearchParams(location.search).get("q");if(p){q.value=p;run()}else if(q.value)run()}
// Timeline chart: hover a bar for its count, click it to list that month, drag the two grippers to set the period.
const fig=document.querySelector(".chart-wrap");
if(fig){const M=JSON.parse(fig.dataset.months),svg=fig.querySelector("svg.chart"),tip=fig.querySelector(".tip"),ov=fig.querySelector(".overview"),
 gl=fig.querySelector(".grip.l"),gr=fig.querySelector(".grip.r"),win=fig.querySelector(".win"),W=372,BASE=54,TALL=40,NS="http://www.w3.org/2000/svg";
 const A0=Math.max(0,M.length-24),B0=M.length-1,rs=fig.querySelector(".period .preset"),mx=fig.querySelector(".mclear");let a=A0,b=B0;
 const dt=k=>new Date(k+"-01T00:00"),lab=k=>dt(k).toLocaleString("en",{month:"short"})+" \u2019"+k.slice(2,4),
  full=k=>dt(k).toLocaleString("en",{month:"long",year:"numeric"}),
  el=(n,at)=>{const e=document.createElementNS(NS,n);for(const k in at)e.setAttribute(k,at[k]);return e},
  txt=(x,y,s,cl,an)=>{const e=el("text",{x,y,class:cl,"text-anchor":an});e.textContent=s;svg.append(e)};
 function draw(){const S=M.slice(a,b+1),n=S.length,bw=W/n,pad=Math.min(1,bw*.15),top=Math.max(1,...S.map(x=>x[1])),
   pk=S.reduce((p,x,i)=>x[1]>S[p][1]?i:p,0),h=c=>c?Math.max(2,Math.round(c*TALL/top)):1;svg.replaceChildren();
  S.forEach(([k,c],i)=>{const gm=el("g",{class:"m"});gm.append(el("rect",{class:"bar"+(i===pk?" pk":"")+(k===sel?" sel":""),x:(i*bw+pad).toFixed(2),y:BASE-h(c),width:Math.max(.5,bw-2*pad).toFixed(2),height:h(c)}));
   const hit=el("rect",{class:"hit",x:(i*bw).toFixed(2),y:0,width:bw.toFixed(2),height:BASE+2,tabindex:0,role:"button","aria-label":`${full(k)}: ${c} gist${c===1?"":"s"}`});
   hit.dataset.k=k;hit.dataset.c=c;gm.append(hit);svg.append(gm)});
  svg.append(el("line",{x1:0,x2:W,y1:BASE+.5,y2:BASE+.5}));
  txt(Math.min(Math.max(pk*bw+bw/2,40),W-40).toFixed(1),BASE-h(S[pk][1])-5,`${S[pk][1]} in ${lab(S[pk][0])}`,"ann","middle");
  txt(0,68,lab(S[0][0]),"","start");txt(W,68,lab(S[n-1][0]),"","end");
  svg.setAttribute("aria-label",`Gists written per month, ${full(S[0][0])} to ${full(S[n-1][0])}. Busiest: ${full(S[pk][0])}, ${S[pk][1]}.`);
  const L=a/M.length*100,R=(b+1)/M.length*100;win.style.left=L+"%";win.style.width=(R-L)+"%";gl.style.left=L+"%";gr.style.left=R+"%";
  for(const[g,v]of[[gl,a],[gr,b]]){g.setAttribute("aria-valuenow",v);g.setAttribute("aria-valuetext",full(M[v][0]))}
  win.setAttribute("aria-valuetext",`${full(M[a][0])} to ${full(M[b][0])}`);rs.classList.toggle("off",a===A0&&b===B0);
  mx.classList.toggle("off",!sel);if(sel)mx.ariaLabel=mx.title=`Clear ${full(sel)}`}
 {const top=Math.max(1,...M.map(x=>x[1])),bw=372/M.length,o=ov.querySelector("svg");
  M.forEach(([k,c],i)=>{const hh=c?Math.max(1,Math.round(c*15/top)):0;if(hh)o.append(el("rect",{x:(i*bw).toFixed(2),y:16-hh,width:Math.max(.6,bw-.5).toFixed(2),height:hh}))})}
 function hover(e){const t=e.target.closest&&e.target.closest(".hit");if(!t){tip.hidden=true;return}const r=fig.getBoundingClientRect(),hr=t.getBoundingClientRect(),c=+t.dataset.c;
  tip.textContent=`${full(t.dataset.k)} \u00b7 ${c} gist${c===1?"":"s"}`;tip.hidden=false;tip.style.left=(hr.left+hr.width/2-r.left)+"px";tip.style.top=(hr.top-r.top+4)+"px"}
 svg.addEventListener("pointermove",hover);svg.addEventListener("pointerleave",()=>tip.hidden=true);svg.addEventListener("focusin",hover);svg.addEventListener("focusout",()=>tip.hidden=true);
 const pick=k=>{sel=sel===k?null:k;draw();run()};
 svg.addEventListener("click",e=>{const t=e.target.closest(".hit");if(t)pick(t.dataset.k)});
 svg.addEventListener("keydown",e=>{const t=e.target.closest(".hit");if(t&&(e.key==="Enter"||e.key===" ")){e.preventDefault();pick(t.dataset.k)}});
 const mc=$("mchip"),clear=()=>{sel=null;draw();run()};if(mc)mc.addEventListener("click",clear);mx.addEventListener("click",clear);
 // the left gripper reaches back to the oldest gist, the right one forward to this month; bars rescale to the period
 const setg=(g,v)=>{v=Math.round(v);if(g===gl)a=Math.min(Math.max(0,v),b);else b=Math.max(Math.min(M.length-1,v),a);draw()};
 for(const g of[gl,gr]){
  g.addEventListener("pointerdown",e=>{g.setPointerCapture(e.pointerId);e.preventDefault();g.focus()});
  g.addEventListener("pointermove",e=>{if(!g.hasPointerCapture(e.pointerId))return;const r=ov.getBoundingClientRect(),f=(e.clientX-r.left)/r.width*M.length;setg(g,g===gl?f:f-1)});
  g.addEventListener("keydown",e=>{const d={ArrowLeft:-1,ArrowDown:-1,ArrowRight:1,ArrowUp:1,PageDown:-12,PageUp:12,Home:-1e4,End:1e4}[e.key];
   if(d){e.preventDefault();setg(g,(g===gl?a:b)+(e.shiftKey&&Math.abs(d)===1?d*12:d))}})}
 // the middle of the window moves the whole period: its length stays fixed
 const slide=to=>{const n=b-a;a=Math.min(Math.max(0,Math.round(to)),M.length-1-n);b=a+n;draw()};
 let drag=null;
 win.addEventListener("pointerdown",e=>{win.setPointerCapture(e.pointerId);e.preventDefault();win.focus();win.classList.add("drag");drag={x:e.clientX,a}});
 win.addEventListener("pointermove",e=>{if(!drag)return;const r=ov.getBoundingClientRect();slide(drag.a+(e.clientX-drag.x)/r.width*M.length)});
 const stop=()=>{drag=null;win.classList.remove("drag")};win.addEventListener("pointerup",stop);win.addEventListener("pointercancel",stop);
 win.addEventListener("keydown",e=>{const d={ArrowLeft:-1,ArrowRight:1,PageDown:-12,PageUp:12,Home:-1e4,End:1e4}[e.key];if(d){e.preventDefault();slide(a+(e.shiftKey&&Math.abs(d)===1?d*12:d))}});
 rs.addEventListener("click",()=>{a=A0;b=B0;if(sel){sel=null;run()}draw();gl.focus()});
 draw()}
let V=$("d").dataset.v;const u=$("upd");
const check=()=>{if(document.hidden||!u.hidden)return;post("/api/version").then(r=>r.ok?r.text():V).then(v=>{if(v!==V)u.hidden=false}).catch(()=>{})};
setInterval(check,60000);check();document.addEventListener("visibilitychange",check);
u.addEventListener("click",()=>location.reload());
document.addEventListener("click",e=>{const a=e.target.closest(".refresh");if(!a)return;e.preventDefault();say("Reloading from GitHub\u2026");
 post("/api/refresh").then(r=>{if(!r.ok)throw new Error(r.status);location.href=a.href}).catch(()=>say("Could not reload from GitHub"))});
// back to top: shows once the first screen has scrolled away
const tt=$("totop");addEventListener("scroll",()=>{tt.hidden=scrollY<innerHeight},{passive:true});
tt.addEventListener("click",()=>{scrollTo({top:0,behavior:matchMedia("(prefers-reduced-motion:reduce)").matches?"auto":"smooth"});const f=document.querySelector("main a");if(f)f.focus({preventScroll:true})})})();  // focus moves to the first gist, not the search box (no phone keyboard)"""
JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
CSP = (
    f"default-src 'none'; script-src '{JS_HASH}'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'none'; connect-src 'self'; "
    "frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
)


def shell(name, body, prefs=DEFAULT_PREFS, script=False):
    font_link = f'<link id=f-{prefs["font"]} rel=stylesheet href="{FONT_CSS[prefs["font"]]}">' if prefs["font"] in FONT_CSS else ""
    return (
        f'<!doctype html><html lang=en data-font="{prefs["font"]}" data-theme="{prefs["theme"]}" data-palette="{prefs["palette"]}" data-mast="{prefs["mast"]}" data-top="{prefs["top"]}" data-open="{prefs["open"]}" data-in="{prefs["in"]}"'
        '><head><meta charset=utf-8>'
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=color-scheme content="light dark">'
        f"<title>{html.escape(name)}</title>{font_link}<style>{CSS}</style></head>"
        f'<body>{body}{f"<script>{JS}</script>" if script else ""}</body></html>'
    )


def comments(g):
    n = int(g.get("comments") or 0)
    label = f'{n} comment{"s" * (n != 1)}'
    return f'<sup class="cd" role=img title="{label}" aria-label="{label}"></sup>' if n else ""


def actions(g):
    t = html.escape(title(g))
    return (f'<span class="acts"><button class="ib del" type=button data-id="{g["id"]}" aria-label="Delete {t}" title="Delete">{TRASH}</button></span>')


def rows(items, field="updated_at", open_in="same"):
    if not items:
        return '<p class="empty">No more gists.</p>'
    out, target = [], " target=_blank" if open_in == "new" else ""
    for g in items:
        t = html.escape(title(g))
        out.append(
            f'<tr><td class="t"><button class="stamp" type=button data-u="{html.escape(gist_url(g))}" title="Copy link" aria-label="Copy link to {t}">{stamp(g[field])}</button></td>'
            f'<td><a href="{html.escape(gist_url(g))}"{target} rel="noopener noreferrer" title="{t}">{t}</a>{comments(g)}</td>'
            f'<td class="act">{actions(g)}</td></tr>'
        )
    return f'<table>{"".join(out)}</table>'


SORTS = {"updated": "Updated", "created": "Created"}  # first one is the default


def sort_items(items, sort):
    """The cached list is already by last update; 'created' re-sorts a copy."""
    if sort == "created":
        return sorted(items, key=lambda g: g["created_at"], reverse=True)
    return items


def palette_cards(current):
    """Colour theme picker: one mini page preview per theme (wash strip, nameplate, bars, rows)."""
    bars = (3, 6, 2, 9, 4, 7, 12, 5, 8)
    out = []
    for k, p in PALETTES.items():
        strip = "".join(f'<i style="background:{c}"></i>' for c in p["wash"])
        bar = "".join(f'<b style="height:{h * 2}px;background:{p["accent"]};opacity:{1 if h == 12 else .5}"></b>' for h in bars)
        rows = "".join(f'<u style="border-color:{p["rule"]}"><em style="color:{p["muted"]}">{t}</em>'
                       f'<span style="color:{p["link"]}">{x}</span></u>' for t, x in (("12:02PM", "server setup spec"), ("Nov-04", "Lessons learned")))
        out.append(
            f'<label class="pcard"><input type=radio name=palette value={k}{" checked" if current == k else ""}>'
            f'<span class="pv"><span class="pstrip">{strip}</span>'
            f'<span class="ppage" style="background:{p["paper"]};color:{p["ink"]}">'
            f'<span class="pname" style="border-color:{p["ink"]}"><strong>localgist</strong><span class="pbars">{bar}</span></span>{rows}</span>'
            f'<span class="plabel">{p["name"]}<small>{p["note"]}</small></span></span></label>'
        )
    return f'<div class="pcards" role=radiogroup aria-label="Colour theme">{"".join(out)}</div>'


def settings_dialog(prefs):
    def seg(name, options):
        return "".join(
            f'<label><input type=radio name={name} value={k}{" checked" if prefs[name] == k else ""}>'
            f'<span class="f-{k}">{v}</span></label>'
            for k, v in options.items()
        )
    return (
        f'<template id=d-fonts {" ".join(f'data-{k}="{html.escape(u)}"' for k, u in FONT_CSS.items())}></template>'
        f'<template id=ok-icon>{CHECK}</template>'
        f'<template id=act-tpl>{actions({"id": "", "html_url": "", "description": "", "files": {}})}</template>'
        '<template id=confirm-tpl><div class="rc" role=group aria-label="Confirm delete">'
        '<span class="rc-q">Delete for good?</span>'
        '<button class="rc-yes" type=button>Yes</button><button class="rc-no" type=button>No</button></div></template>'
        '<div id=note class="note" role=status aria-live=polite hidden></div>'
        f'<button id=totop class="totop" type=button aria-label="Back to top" title="Back to top" hidden>{CHEVRON_UP}</button>'
        '<dialog id=settings class="modal" aria-labelledby=m-title'
        f' data-defaults="{html.escape(json.dumps({k: DEFAULT_PREFS[k] for k in DISPLAY_KEYS}))}">'
        f'<div class="m-head"><div id=m-title class="m-title">Display settings</div>'
        f'<button id=m-close class="icon-btn ghost" type=button aria-label="Close">{XMARK}</button></div>'
        '<div class="m-body grid">'
        f'<fieldset class="wide"><legend>Colour theme</legend>{palette_cards(prefs["palette"])}</fieldset>'
        f'<fieldset><legend>Light or dark</legend><div class="seg">{seg("theme", THEMES)}</div></fieldset>'
        f'<fieldset><legend>Font</legend><div class="seg">{seg("font", FONTS)}</div></fieldset>'
        f'<fieldset class="m-mast"><legend>Layout</legend><div class="seg">{seg("mast", MASTS)}</div>'
        '<p class="hint">Minimal hides the masthead and chart.</p></fieldset>'
        f'<fieldset><legend>Back to top button</legend><div class="seg">{seg("top", TOPS)}</div></fieldset>'
        f'<fieldset><legend>Clicks open</legend><div class="seg">{seg("open", OPENS)}</div>'
        '<p class="hint">Same window keeps the Back button working.</p></fieldset>'
        '</div>'
        '<div class="m-foot"><div id=m-normal class="m-row"><span id=m-msg class="m-msg" aria-live=polite></span>'
        '<button id=m-reset class="btn link" type=button title="Back to the defaults (Save to keep)">Reset</button>'
        '<button id=m-save class="btn primary" type=button disabled>Save</button></div>'
        '<div id=m-ask class="m-row ask" role=alert hidden><span class="m-msg">You have unsaved changes.</span>'
        '<button id=m-keep class="btn" type=button>Keep editing</button>'
        '<button id=m-discard class="btn danger" type=button>Exit anyway</button></div></div>'
        '</dialog>'
    )




def month_counts(items, months=24, now=None):
    """[(year, month, gists created that month)] for the last `months` months, oldest first (Malaysia time)."""
    now = now or datetime.now(MYT)
    keys, y, m = [], now.year, now.month
    for _ in range(months):
        keys.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    counts = dict.fromkeys(reversed(keys), 0)
    for g in items:
        dt = datetime.fromisoformat(g["created_at"].replace("Z", "+00:00")).astimezone(MYT)
        if (dt.year, dt.month) in counts:
            counts[(dt.year, dt.month)] += 1
    return [(y, m, n) for (y, m), n in counts.items()]


def all_months(items, now=None):
    """[["YYYY-MM", n], ...] from the month of the oldest gist to this month: the chart's full timeline."""
    now = now or datetime.now(MYT)
    if not items:
        return [[f"{now:%Y-%m}", 0]]
    first = min(datetime.fromisoformat(g["created_at"].replace("Z", "+00:00")).astimezone(MYT) for g in items)
    span = (now.year - first.year) * 12 + now.month - first.month + 1
    return [[f"{y}-{m:02d}", n] for y, m, n in month_counts(items, months=span, now=now)]


def created_month(g):
    iso = g.get("created_at") or g.get("updated_at") or "1970-01-01T00:00:00Z"
    return f'{datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MYT):%Y-%m}'


def month_chart(items, now=None):
    """A small editorial bar chart: one bar a month, the busiest month labelled. Plain SVG, no script."""
    data = month_counts(items, now=now)
    W, base, tall = 372, 54, 40
    bw = W / len(data)
    top = max(n for *_, n in data) or 1
    peak = max(range(len(data)), key=lambda i: data[i][2])
    mon = lambda y, m: datetime(y, m, 1).strftime("%b") + f" \u2019{y % 100:02d}"
    bars = "".join(
        f'<rect class="bar{" pk" if i == peak else ""}" x="{i * bw + 1:.1f}" y="{base - max(1, round(n * tall / top))}" '
        f'width="{bw - 2:.1f}" height="{max(1, round(n * tall / top))}"><title>{mon(y, m)}: {n}</title></rect>'
        for i, (y, m, n) in enumerate(data)
    )
    py, pm, pn = data[peak]
    px = min(max(peak * bw + bw / 2, 40), W - 40)
    return (
        f'<svg class="chart" viewBox="0 0 {W} 74" role=img aria-label="Gists written per month over the last {len(data)} months. '
        f'Busiest: {mon(py, pm)}, {pn}.">{bars}<line x1="0" x2="{W}" y1="{base + .5}" y2="{base + .5}"/>'
        f'<text class="ann" x="{px:.1f}" y="{base - max(1, round(pn * tall / top)) - 5}" text-anchor="middle">{pn} in {mon(py, pm)}</text>'
        f'<text x="0" y="68">{mon(*data[0][:2])}</text><text x="{W}" y="68" text-anchor="end">{mon(*data[-1][:2])}</text></svg>'
    )




def index_page(page, sort="updated", prefs=DEFAULT_PREFS):
    sort = sort if sort in SORTS else "updated"
    field = f"{sort}_at"
    held = gists()  # the update check compares against this list, whatever the sort
    months = all_months(held)
    items = sort_items(held, sort)
    qs = "" if sort == "updated" else f"&sort={sort}"
    pages = max(1, -(-len(items) // (2 * ROWS)))
    page = min(max(page, 1), pages)
    left, right = columns(items, page)
    r1, r2 = column_name(2 * page - 2), column_name(2 * page - 1)
    nav = "".join(
        f'<b aria-current="page">{p}</b>' if p == page else f'<a href="/?p={p}{qs}">{p}</a>' for p in range(1, pages + 1)
    )
    seg = "".join(
        f'<b aria-current="true">{label}</b>' if key == sort
        else f'<a href="/{"" if key == "updated" else "?sort=" + key}">{label}</a>'
        for key, label in SORTS.items()
    )
    on = prefs["in"].split(",")
    chips = "".join(
        f'<label class="chip"><input type=checkbox value={k}{" checked" if k in on else ""}>{CHECK}{v}</label>'
        for k, v in SCOPES.items()
    )
    reload = f'<a class="refresh" href="/?p={page}{qs}" title="Reload from GitHub" aria-label="Reload from GitHub">{REFRESH}</a>'
    return shell(
        "localgist",
        '<input type=radio name=t id=t1 checked aria-label="Show first column">'
        '<input type=radio name=t id=t2 aria-label="Show second column">'
        '<div class="strip" aria-hidden=true><i></i><i></i><i></i></div>'
        f'<div class="bar band"><div class="nameplate"><div><div class="np-row"><h1>localgist</h1>{reload}</div><p class="dateline">{datetime.now(MYT):%A}, {datetime.now(MYT).day} '
        f'{datetime.now(MYT):%B %Y} \u00b7 {len(items)} gists \u00b7 Kuala Lumpur</p></div>'
        f'<figure class="chart-wrap" data-months="{html.escape(json.dumps(months))}">'
        '<figcaption>Gists written, by month</figcaption>'
        f'<div class="chart-row">{month_chart(held)}<button class="preset mclear off" type=button aria-label="Clear the month filter" title="Clear month">{XMARK}</button></div><div class="tip" role=tooltip hidden></div>'
        '<div class="period"><div class="overview"><svg viewBox="0 0 372 16" preserveAspectRatio="none" aria-hidden=true></svg>'
        '<div class="win" role=slider tabindex=0 aria-label="Move the period"></div>'
        f'<div class="grip l" role=slider tabindex=0 aria-label="Start of period" aria-valuemin=0 aria-valuemax={len(months) - 1}></div>'
        f'<div class="grip r" role=slider tabindex=0 aria-label="End of period" aria-valuemin=0 aria-valuemax={len(months) - 1}></div></div>'
        f'<button class="preset" type=button aria-label="Reset the period to the last 24 months" title="Reset period">{XMARK}</button></div>'
        '<p class="chart-hint">Drag the handles to resize the period, or the middle to move it. Click a bar to list that month.</p></figure></div></div>'
        f'<div class="top"><div class="bar"><header><span class="brandrow"><a class="brand" href="/">localgist</a>{reload}</span>'
        f'<label class="search">{ICON}<input id=q type=search placeholder="Search {len(items)} gists" '
        'autocomplete=off spellcheck=false aria-label="Search gists"><kbd id=k>⌘F</kbd></label>'
        f'<nav class="sort" aria-label="Sort by date">{seg}</nav>'
        f'<div class="meta"><span id=cnt>{len(items)} gists</span>'
        f'<nav id=pg aria-label="Pages">{nav}</nav></div>'
        f'<button id=gear class="icon-btn" type=button aria-haspopup=dialog aria-controls=settings aria-label="Display settings" aria-keyshortcuts="Meta+K Control+K" title="Display settings (⌘K)">{GEAR}</button></header></div>'
        f'<div class="bar"><div class="scope" role=group aria-label="Search in"><span>Search in</span>{chips}<button id=mchip class="mchip" type=button hidden></button><span id=st aria-live=polite></span></div></div>'
        f'<div class="tabs"><label for=t1>{r1}</label><label for=t2>{r2}</label></div></div>'
        f'<div class="wrap"><main><section class="c1"><h2>{r1}</h2><div id=r1>{rows(left, field, prefs["open"])}</div></section>'
        f'<section class="c2"><h2>{r2}</h2><div id=r2>{rows(right, field, prefs["open"])}</div></section></main></div>'
        f'{settings_dialog(prefs)}'
        '<div role=status aria-live=polite><button id=upd class=toast type=button hidden>New update available. Click to refresh</button></div>'
        f'<script type="application/json" id=d data-v="{html.escape(signature(held))}" data-csrf="{CSRF}">{search_data(items, field)}</script>',
        prefs,
        script=True,
    )


def page_num(s):
    """?p= as a number; anything int() rejects (a superscript 2, 5000 digits, junk) is page 1. index_page clamps the range."""
    try:
        return int(s)
    except ValueError:
        return 1


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # a stalled browser connection frees its thread after 30 s

    def forbid(self):
        """403, with one log line so a refused request is not silent."""
        print(f"localgist: refused {self.command} {urlsplit(self.path).path!r} (Origin {self.headers.get('Origin')!r}, "
              f"Sec-Fetch-Site {self.headers.get('Sec-Fetch-Site')!r})", file=sys.stderr, flush=True)
        return self.send(403, "forbidden", "text/plain")

    @staticmethod
    def failed(e):
        """(status, message) for an error. Only network trouble and bad JSON are GitHub's fault (502);
        anything else is a localgist bug (500), logged with its traceback so it is not passed off as GitHub."""
        if isinstance(e, (OSError, json.JSONDecodeError)):  # URLError, HTTPError, timeouts, refused connections
            return 502, f"GitHub fetch failed: {e}"
        traceback.print_exc()
        return 500, f"localgist error: {e!r}"

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)  # only the one page script may run
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """Pages only. Another site may load one; it serves the cached list and cannot force a GitHub fetch."""
        if not host_ok(self.headers.get("Host", "")):  # blocks DNS-rebinding pages
            return self.forbid()
        url = urlsplit(self.path)
        if url.path == "/ping":  # lets a second `pyup` find this server; does not count as use
            return self.send(200, "localgist", "text/plain")
        _last_hit[0] = time.clock_gettime(CLOCK)
        if url.path != "/":
            return self.send(404, "not found", "text/plain")
        q = parse_qs(url.query)
        p = page_num(q.get("p", ["1"])[0])  # parsed outside the try, so bad input is never called a GitHub failure
        try:
            return self.send(200, index_page(p, q.get("sort", ["updated"])[0], read_prefs(self.headers.get("Cookie"))))
        except Exception as e:
            code, msg = self.failed(e)
            return self.send(code, shell("error", f"<div class='wrap'><p style='padding:24px'>{html.escape(msg)}</p></div>"))

    def do_POST(self):
        """Everything that can spend GitHub quota (refresh, update check, content search): our own page only."""
        if not same_origin(self.headers):  # same checks as delete: Host, Origin, Sec-Fetch-Site, CSRF token
            return self.forbid()
        url = urlsplit(self.path)
        if url.path == "/api/version":  # polled by an open page; does not count as use, so idle stop still works
            return self.send(200, version(), "text/plain")
        if url.path not in ("/api/refresh", "/api/search"):
            return self.send(404, "not found", "text/plain")
        _last_hit[0] = time.clock_gettime(CLOCK)
        q = parse_qs(url.query)
        try:
            if url.path == "/api/refresh":
                return self.send(200, json.dumps({"v": signature(gists(refresh=True))}), "application/json")
            scopes = [s for s in q.get("in", ["desc"])[0].split(",") if s in SCOPES] or ["desc"]
            return self.send(200, json.dumps(search(q.get("q", [""])[0][:200], scopes)), "application/json")
        except Exception as e:
            return self.send(*self.failed(e), "text/plain")

    def do_DELETE(self):
        if not same_origin(self.headers):  # blocks other sites, rebinding pages and stray tools
            return self.forbid()
        m = re.fullmatch(r"/api/gists/([0-9a-f]{1,64})", urlsplit(self.path).path)
        if not m:
            return self.send(404, "not found", "text/plain")
        _last_hit[0] = time.clock_gettime(CLOCK)
        try:
            return self.send(200, json.dumps({"ok": True, "v": delete_gist(m[1])}), "application/json")
        except Exception as e:
            return self.send(*self.failed(e), "text/plain")


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
