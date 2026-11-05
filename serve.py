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
_lock = threading.Lock()  # one list fetch at a time
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

FONTS = {"arial": "Arial", "source": "Source Sans 3", "helvetica": "Helvetica"}  # first one is the default
OPENS = {"same": "Same window", "new": "New tab"}  # first one is the default; same window keeps Back working
DEFAULT_PREFS = {"font": "arial", "time": "1", "open": "same", "in": "desc"}


def read_prefs(cookie_header):
    p = dict(DEFAULT_PREFS)
    try:
        c = SimpleCookie(cookie_header or "")
    except CookieError:
        return p
    if "lg" in c:  # untrusted input: keep only known values
        q = parse_qs(unquote(c["lg"].value))
        if (f := q.get("font", [""])[0]) in FONTS:
            p["font"] = f
        if (t := q.get("time", [""])[0]) in ("0", "1"):
            p["time"] = t
        if (o := q.get("open", [""])[0]) in OPENS:
            p["open"] = o
        if s := [x for x in dict.fromkeys(q.get("in", [""])[0].split(",")) if x in SCOPES]:
            p["in"] = ",".join(s)
    return p


MYT = ZoneInfo("Asia/Kuala_Lumpur")


def stamp(iso, now=None):
    """('4 Nov', '14:05') in Malaysia time; the year is added when it is not this year."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MYT)
    now = now or datetime.now(MYT)
    return f"{dt.day} {dt:%b}" + ("" if dt.year == now.year else f" {dt.year}"), f"{dt:%H:%M}"


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
    """[id, url, date, time, title, desc, files] per gist (desc and files lowercased),
    safe to embed in a <script> block."""
    rows = [
        [g["id"], gist_url(g), *stamp(g[field]), title(g), (g.get("description") or "").lower(), " ".join(g["files"]).lower()]
        for g in items
    ]
    return json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")


CSS = """
:root{color-scheme:light;
 --page:#eef0f3;--surface:#fff;--fg:#16181d;--muted:#5f6673;--faint:#8a919d;--line:#e7e9ed;--rule:#16181d;
 --link:#173a7a;--seen:#5b3f8c;--hover:#f3f6fb;--field:#f6f7f9;--field-line:#d9dce2;--focus:#2f6feb;--kbd:#fff;--shadow:0 1px 2px rgb(16 24 40 / .05),0 8px 24px rgb(16 24 40 / .06);
 --pop:0 12px 40px rgb(16 24 40 / .16),0 2px 6px rgb(16 24 40 / .08)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;
 --page:#0b0d10;--surface:#14171c;--fg:#e8eaef;--muted:#a0a7b4;--faint:#6f7683;--line:#242830;--rule:#e8eaef;
 --link:#a9c4f6;--seen:#c7adf2;--hover:#1b2028;--field:#0f1216;--field-line:#2c313a;--focus:#6b96ff;--kbd:#1d2129;--shadow:0 1px 2px rgb(0 0 0 / .4),0 12px 32px rgb(0 0 0 / .35);
 --pop:0 16px 48px rgb(0 0 0 / .55),0 2px 6px rgb(0 0 0 / .4)}}
html{--ff:Arial,"Helvetica Neue",sans-serif;--fs:12.5px}
html[data-font=helvetica]{--ff:Helvetica,"Helvetica Neue",Arial,sans-serif}
html[data-font=source]{--ff:"Source Sans 3",Arial,sans-serif;--fs:13.5px}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--page)}
body{margin:0;background:var(--page);color:var(--fg);font:var(--fs)/1.45 var(--ff);-webkit-font-smoothing:antialiased}
button,input{font-family:inherit}
a{color:var(--link);text-decoration:none}a:visited{color:var(--seen)}a:hover{text-decoration:underline}
a:focus-visible,label:focus-visible,button:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
.wrap{width:min(1040px,100% - 64px);margin:32px auto 48px;background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:clip}
.top{background:var(--surface)}
header{position:relative;display:flex;align-items:center;gap:16px;padding:14px 24px;border-bottom:1px solid var(--line)}
.brand{font-weight:700;font-size:15px;letter-spacing:-.01em;color:var(--fg)}.brand:visited{color:var(--fg)}
.search{position:relative;flex:1 1 220px;max-width:380px;min-width:0}
.search svg{position:absolute;left:10px;top:50%;translate:0 -50%;width:14px;height:14px;stroke:var(--faint);fill:none;stroke-width:2;pointer-events:none}
.search input{width:100%;height:34px;padding:0 52px 0 32px;font-size:13px;color:var(--fg);background:var(--field);border:1px solid var(--field-line);border-radius:8px;outline:none;transition:border-color .12s,box-shadow .12s}
.search input::placeholder{color:var(--faint)}
.search input:focus{border-color:var(--focus);box-shadow:0 0 0 3px color-mix(in srgb,var(--focus) 22%,transparent);background:var(--surface)}
.search input::-webkit-search-cancel-button{display:none}
kbd{position:absolute;right:8px;top:50%;translate:0 -50%;font:500 11px/1 ui-monospace,SFMono-Regular,monospace;color:var(--muted);background:var(--kbd);border:1px solid var(--field-line);border-bottom-width:2px;border-radius:5px;padding:3px 6px;pointer-events:none}
.search input:focus+kbd{opacity:0}
.sort{display:inline-flex;padding:2px;gap:2px;background:var(--field);border:1px solid var(--field-line);border-radius:8px;flex:none}
.sort a,.sort b{height:28px;padding:0 11px;display:inline-grid;place-items:center;border-radius:6px;font-size:12px;font-weight:500;color:var(--muted)}
.sort a:visited{color:var(--muted)}.sort a:hover{color:var(--fg);text-decoration:none;background:var(--hover)}
.sort b{background:var(--surface);color:var(--fg);font-weight:600;box-shadow:0 1px 2px rgb(0 0 0 / .12)}
.meta{margin-left:auto;display:flex;align-items:center;gap:12px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.meta nav{display:flex;gap:4px;align-items:center}
.meta nav a,.meta nav b{min-width:24px;height:24px;display:inline-grid;place-items:center;border-radius:6px;padding:0 6px}
.meta nav b{background:var(--fg);color:var(--surface);font-weight:600}
.meta nav a:hover{background:var(--hover);text-decoration:none}
.icon-btn{flex:none;width:34px;height:34px;display:inline-grid;place-items:center;padding:0;border:1px solid var(--field-line);background:var(--field);border-radius:8px;color:var(--muted);cursor:pointer}
.hi{width:18px;height:18px;flex:none}
.icon-btn:hover{color:var(--fg)}.icon-btn.ghost{border-color:transparent;background:none}.icon-btn.ghost:hover{background:var(--hover)}
.refresh{color:var(--muted)}.refresh .hi{width:16px;height:16px}.refresh:visited{color:var(--muted)}.refresh:hover{color:var(--fg)}
.search svg.hi{width:16px;height:16px;stroke:var(--faint)}
dialog.modal{width:min(440px,calc(100% - 32px));max-height:calc(100% - 32px);padding:0;border:1px solid var(--line);border-radius:16px;background:var(--surface);color:var(--fg);box-shadow:var(--pop);overflow:auto}
dialog.modal::backdrop{background:rgb(10 12 16 / .5);backdrop-filter:blur(2px)}
dialog.modal[open]{animation:pop .16s ease-out}
@keyframes pop{from{opacity:0;transform:translateY(6px) scale(.98)}}
.m-head{display:flex;align-items:center;justify-content:space-between;padding:12px 12px 12px 20px;border-bottom:1px solid var(--line)}
.m-title{font-size:15px;font-weight:700}
.m-body{padding:20px 24px 22px;text-align:center}
.m-body fieldset{border:0;margin:0;padding:0;min-width:0}.m-body fieldset+fieldset{margin-top:18px;padding-top:18px;border-top:1px solid var(--line)}
.m-body legend{padding:0;margin:0 auto 10px;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.m-body .switch{display:inline-flex}
.m-foot{padding:12px 16px 12px 20px;border-top:1px solid var(--line);background:var(--field);border-radius:0 0 16px 16px}
.m-row{display:flex;align-items:center;gap:8px}
.m-msg{margin-right:auto;display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12.5px}
.m-msg.ok{color:#1f8a4c}.m-msg.dirty{color:var(--fg);font-weight:600}
.m-msg .hi{width:16px;height:16px}
.ask .m-msg{color:var(--fg);font-weight:600}
.btn{height:34px;padding:0 14px;border-radius:8px;border:1px solid var(--field-line);background:var(--surface);color:var(--fg);font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--fg)}
.btn.primary{background:var(--fg);color:var(--surface);border-color:var(--fg)}.btn.primary:hover{filter:brightness(1.15)}
.btn.primary:disabled{opacity:.35;cursor:default;filter:none}
.btn.danger{background:#c62828;border-color:#c62828;color:#fff}.btn.danger:hover{filter:brightness(1.1)}
.seg{display:flex;padding:2px;gap:2px;background:var(--field);border:1px solid var(--field-line);border-radius:8px}
.seg label{flex:1;position:relative;cursor:pointer}
.seg input,.switch input,.chip input{position:absolute;opacity:0;pointer-events:none}
.f-same,.f-new{font-family:inherit}
.seg span{display:grid;place-items:center;height:30px;padding:0 6px;border-radius:6px;font-size:13px;color:var(--muted);white-space:nowrap}
.seg label:hover span{color:var(--fg)}
.seg label:has(input:checked) span{background:var(--surface);color:var(--fg);font-weight:600;box-shadow:0 1px 2px rgb(0 0 0 / .12)}
.seg label:has(input:focus-visible) span{outline:2px solid var(--focus)}
.f-arial{font-family:Arial,sans-serif}.f-source{font-family:"Source Sans 3",Arial,sans-serif}.f-helvetica{font-family:Helvetica,Arial,sans-serif}
.switch{display:flex;align-items:center;gap:10px;cursor:pointer;font-size:13px;font-weight:500;position:relative}
.track{position:relative;flex:none;width:34px;height:20px;border-radius:999px;background:var(--field-line);transition:background .15s}
.track::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgb(0 0 0 / .3);transition:translate .15s}
.switch input:checked+.track{background:var(--focus)}.switch input:checked+.track::after{translate:14px 0}
.switch input:focus-visible+.track{outline:2px solid var(--focus);outline-offset:2px}
.hint{margin:8px 0 0;color:var(--faint);font-size:11.5px}
.scope{display:flex;align-items:center;gap:8px;padding:8px 24px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted)}
.chip{position:relative;display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px 0 8px;border:1px solid var(--field-line);border-radius:999px;cursor:pointer;color:var(--muted);user-select:none;white-space:nowrap}
.chip .hi{width:14px;height:14px;padding:1px;border-radius:4px;border:1.5px solid var(--faint);color:transparent}
.chip:hover{color:var(--fg)}
.chip:has(input:checked){color:var(--fg);border-color:var(--fg)}
.chip:has(input:checked) .hi{background:var(--fg);border-color:var(--fg);color:var(--surface)}
.chip:has(input:focus-visible){outline:2px solid var(--focus);outline-offset:2px}
#st{margin-left:auto;color:var(--faint);font-variant-numeric:tabular-nums}
input[name=t]{position:absolute;opacity:0;pointer-events:none}
.tabs{display:none}
main{display:grid;grid-template-columns:1fr 1fr;gap:40px;padding:12px 24px 28px}
h2{display:flex;justify-content:space-between;margin:10px 0 2px;padding-bottom:6px;border-bottom:2px solid var(--rule);font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
table{width:100%;border-collapse:collapse;table-layout:fixed}
tr:hover td{background:var(--hover)}
td{height:23px;padding:3px 0;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:top}
td.t{width:104px;padding:4px 8px 0 4px;color:var(--muted);font-variant-numeric:tabular-nums;font-size:11.5px}
td.t .h{color:var(--faint)}
html.notime td.t{width:64px}html.notime .h{display:none}
.empty{color:var(--muted);padding:14px 0;margin:0}
.toast{position:fixed;left:50%;bottom:24px;translate:-50% 0;z-index:5;display:flex;align-items:center;gap:10px;padding:10px 16px;border:0;border-radius:999px;background:var(--fg);color:var(--surface);font-weight:600;font-size:13px;line-height:1.2;box-shadow:0 8px 28px rgb(0 0 0 / .25);cursor:pointer;animation:rise .2s ease-out}
.toast::before{content:"";width:8px;height:8px;border-radius:50%;background:#3ccf7a}
.toast:hover{filter:brightness(1.1)}
@keyframes rise{from{opacity:0;translate:-50% 8px}}
@media (prefers-reduced-motion:reduce){.toast{animation:none}.track,.track::after{transition:none}}
@media (max-width:767px){
 html{--fs:14px}html[data-font=source]{--fs:15px}
 .wrap{width:auto;margin:0;border:0;border-radius:0;box-shadow:none;min-height:100vh}
 .top{position:sticky;top:0;z-index:2}
 header{flex-wrap:wrap;gap:10px 10px;padding:12px 16px}
 .search{order:3;flex:1 1 140px;max-width:none}.search input{height:40px;font-size:16px;padding-right:10px}
 .sort{order:4}.sort a,.sort b{height:34px;padding:0 10px;font-size:13px}
 .meta{gap:8px}
 kbd{display:none}
 .m-row.ask{flex-wrap:wrap}.ask .m-msg{flex-basis:100%;margin-bottom:4px}
 .scope{display:none;padding:8px 16px;gap:6px;overflow-x:auto}.scope>span:first-child{display:none}
 .top:focus-within .scope,.top:has(#q:not(:placeholder-shown)) .scope{display:flex}
 .tabs{display:flex;border-bottom:1px solid var(--line)}
 .tabs label{flex:1;text-align:center;padding:11px 0;font-weight:600;font-size:13px;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;font-variant-numeric:tabular-nums}
 #t1:checked~.top label[for=t1],#t2:checked~.top label[for=t2]{color:var(--fg);border-color:var(--fg)}
 #t1:focus-visible~.top label[for=t1],#t2:focus-visible~.top label[for=t2]{outline:2px solid var(--focus);outline-offset:-2px}
 main{display:block;padding:0 16px 32px}h2{display:none}
 .c2,#t2:checked~main .c1{display:none}#t2:checked~main .c2{display:block}
 td{height:48px;padding:8px 0;white-space:normal}
 td a{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 td.t,html.notime td.t{width:68px;font-size:12px;padding:9px 8px 0 0;line-height:1.35}
 td.t span{display:block}
 .toast{bottom:16px;width:calc(100% - 32px);justify-content:center}
}
"""

def hero(d, size=24):
    """A Heroicons v2 outline icon (24px) or solid mini icon (16px). Heroicons are the only icons used."""
    if size == 16:
        return f'<svg class="hi" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">{d}</svg>'
    return (f'<svg class="hi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{d}</svg>')


ICON = hero('<path d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z"/>')  # magnifying-glass
GEAR = hero(  # cog-6-tooth
    '<path d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"/>'
    '<path d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"/>'
)
REFRESH = hero('<path d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182m0-4.991v4.99"/>')  # arrow-path
XMARK = hero('<path d="M6 18 18 6M6 6l12 12"/>')  # x-mark
CHECK = hero('<path fill-rule="evenodd" d="M12.416 3.376a.75.75 0 0 1 .208 1.04l-5 7.5a.75.75 0 0 1-1.154.114l-3-3a.75.75 0 0 1 1.06-1.06l2.353 2.353 4.493-6.74a.75.75 0 0 1 1.04-.207Z" clip-rule="evenodd"/>', 16)  # check (16 solid)
SOURCE_SANS_CSS = "https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap"

# One script for the list page, pinned in the CSP by its hash.
# Search: Cmd/Ctrl+F focuses it (again inside the box: the browser's own find). Description and
# file-name matches run here; content matches come from /search. Rows are built with textContent only.
JS = r"""(()=>{const $=i=>document.getElementById(i),q=$("q");if(!q)return;
const root=document.documentElement,D=JSON.parse($("d").textContent),N=90,box=[$("r1"),$("r2")],cnt=$("cnt"),pg=$("pg"),st=$("st");
const keep=box.map(e=>e.innerHTML),keepCnt=cnt.textContent;
const prefs={font:root.dataset.font,time:root.dataset.time,open:root.dataset.open,in:root.dataset.in};
// one cookie, read by the server on every page load, so settings survive restarts of localgist
const store=()=>{document.cookie="lg="+encodeURIComponent(new URLSearchParams(prefs))+";path=/;max-age=31536000;samesite=strict"};
const targets=o=>document.querySelectorAll("main a").forEach(a=>{if(o==="new")a.target="_blank";else a.removeAttribute("target")});
const boxes=[...document.querySelectorAll(".scope input")],scope=()=>boxes.filter(b=>b.checked).map(b=>b.value);
function table(list,msg){if(!list.length){const p=document.createElement("p");p.className="empty";p.textContent=msg;return p}
 const t=document.createElement("table");for(const g of list){const r=t.insertRow(),c=r.insertCell(),a=document.createElement("a"),d=document.createElement("span"),h=document.createElement("span");
 c.className="t";d.textContent=g[2];h.className="h";h.textContent=g[3];c.append(d," ",h);
 a.href=g[1];if(prefs.open==="new")a.target="_blank";a.rel="noopener noreferrer";a.textContent=a.title=g[4];r.insertCell().append(a)}return t}
function show(m,s){const L=m.slice(0,N),R=m.slice(N,2*N);
 box[0].replaceChildren(table(L,`No gists match “${s}”.`));box[1].replaceChildren(table(R,m.length?"No more matches.":""));
 cnt.textContent=`${m.length} of ${D.length}`+(m.length>2*N?" (first 180)":"");pg.hidden=true;$("t1").checked=true}
let seq=0,timer;
function remember(raw){const u=new URL(location.href);if(raw)u.searchParams.set("q",raw);else u.searchParams.delete("q");history.replaceState(null,"",u)}
function run(){const raw=q.value.trim(),s=raw.toLowerCase(),sc=scope(),my=++seq;clearTimeout(timer);st.textContent="";remember(raw);
 if(!s){box.forEach((e,i)=>e.innerHTML=keep[i]);targets(prefs.open);cnt.textContent=keepCnt;pg.hidden=false;return}
 const w=s.split(/\s+/);
 if(!sc.includes("content")){show(D.filter(g=>{const h=(sc.includes("desc")?g[5]:"")+" "+(sc.includes("file")?g[6]:"");return w.every(x=>h.includes(x))}),raw);return}
 st.textContent="Searching contents…";
 timer=setTimeout(()=>fetch("/search?"+new URLSearchParams({q:raw,in:sc.join(",")}),{cache:"no-store"}).then(r=>r.json()).then(j=>{if(my!==seq)return;
  const ids=new Set(j.ids);show(D.filter(g=>ids.has(g[0])),raw);
  st.textContent=j.ready?"":`Indexing contents… ${j.done} of ${j.total}`;if(!j.ready)timer=setTimeout(run,800)})
  .catch(()=>{if(my===seq)st.textContent="Content search failed"}),150)}
q.addEventListener("input",run);
q.addEventListener("keydown",e=>{if(e.key==="Escape"){q.value="";run();q.blur()}
 else if(e.key==="Enter"&&q.value.trim()){const a=document.querySelector("main a");if(a){if(prefs.open==="new")window.open(a.href,"_blank","noopener,noreferrer");else location.href=a.href}}});
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="f"&&document.activeElement!==q&&!$("settings").open){e.preventDefault();q.focus();q.select()}});
boxes.forEach(b=>b.addEventListener("change",()=>{if(!scope().length)boxes[0].checked=true;prefs.in=scope().join(",");store();
 if(b.value==="content"&&b.checked)fetch("/search?in=content",{cache:"no-store"}).catch(()=>{});run()}));
// Settings modal: choices preview live; Save keeps them; leaving with unsaved changes asks first.
const dlg=$("settings"),gear=$("gear"),ask=$("m-ask"),norm=$("m-normal"),msg=$("m-msg"),btn=$("m-save");
let saved={font:prefs.font,time:prefs.time,open:prefs.open},justSaved=false;
const form=()=>({font:dlg.querySelector("input[name=font]:checked").value,open:dlg.querySelector("input[name=open]:checked").value,time:$("showtime").checked?"1":"0"});
const dirty=()=>{const f=form();return f.font!==saved.font||f.time!==saved.time||f.open!==saved.open};
function apply(p){root.dataset.font=p.font;root.classList.toggle("notime",p.time==="0");targets(p.open);
 if(p.font==="source"&&!$("fsrc")){const l=document.createElement("link");l.id="fsrc";l.rel="stylesheet";l.href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap";document.head.append(l)}}
function status(){const d=dirty();btn.disabled=!d;msg.className="m-msg"+(d?" dirty":justSaved?" ok":"");
 if(justSaved&&!d){msg.innerHTML=$("ok-icon").innerHTML;msg.append(" Saved. Esc to close")}else msg.textContent=d?"Unsaved changes":"Choices preview live. Save to keep them."}
function setForm(p){for(const k of["font","open"])dlg.querySelector(`input[name=${k}][value=${p[k]}]`).checked=true;$("showtime").checked=p.time==="1"}
function asking(o){ask.hidden=!o;norm.hidden=o;if(o)$("m-keep").focus()}
function close(){asking(false);dlg.close();gear.focus()}
function tryClose(){if(!ask.hidden)return asking(false);if(dirty())asking(true);else close()}
function save(){saved=form();Object.assign(prefs,saved);store();justSaved=true;status()}
gear.addEventListener("click",()=>{setForm(saved);apply(saved);justSaved=false;status();asking(false);dlg.showModal()});
dlg.addEventListener("change",()=>{apply(form());justSaved=false;status();asking(false)});
dlg.addEventListener("cancel",e=>{e.preventDefault();tryClose()});
dlg.addEventListener("click",e=>{if(e.target===dlg)tryClose()});
dlg.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="s"){e.preventDefault();if(dirty())save()}});
$("m-close").addEventListener("click",tryClose);btn.addEventListener("click",save);
$("m-keep").addEventListener("click",()=>asking(false));
$("m-discard").addEventListener("click",()=>{setForm(saved);apply(saved);close()});
if(!/Mac|iPhone|iPad/.test(navigator.platform))$("k").textContent="Ctrl F";
{const p=new URLSearchParams(location.search).get("q");if(p){q.value=p;run()}else if(q.value)run()}
const V=$("d").dataset.v,u=$("upd");
const check=()=>{if(document.hidden||!u.hidden)return;fetch("/version",{cache:"no-store"}).then(r=>r.ok?r.text():V).then(v=>{if(v!==V)u.hidden=false}).catch(()=>{})};
setInterval(check,60000);document.addEventListener("visibilitychange",check);
u.addEventListener("click",()=>location.reload())})();"""
JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
CSP = (
    f"default-src 'none'; script-src '{JS_HASH}'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
)


def shell(name, body, prefs=DEFAULT_PREFS, script=False):
    font_link = f'<link id=fsrc rel=stylesheet href="{SOURCE_SANS_CSS}">' if prefs["font"] == "source" else ""
    return (
        f'<!doctype html><html lang=en data-font="{prefs["font"]}" data-time="{prefs["time"]}" data-open="{prefs["open"]}" data-in="{prefs["in"]}"'
        f'{" class=notime" if prefs["time"] == "0" else ""}><head><meta charset=utf-8>'
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=color-scheme content="light dark">'
        f"<title>{html.escape(name)}</title>{font_link}<style>{CSS}</style></head>"
        f'<body><div class="wrap">{body}</div>{f"<script>{JS}</script>" if script else ""}</body></html>'
    )


def rows(items, field="updated_at", open_in="same"):
    if not items:
        return '<p class="empty">No more gists.</p>'
    out, target = [], " target=_blank" if open_in == "new" else ""
    for g in items:
        d, h = stamp(g[field])
        t = html.escape(title(g))
        out.append(
            f'<tr><td class="t"><span>{d}</span> <span class="h">{h}</span></td>'
            f'<td><a href="{html.escape(gist_url(g))}"{target} rel="noopener noreferrer" title="{t}">{t}</a></td></tr>'
        )
    return f'<table>{"".join(out)}</table>'


SORTS = {"updated": "Updated", "created": "Created"}  # first one is the default


def sort_items(items, sort):
    """The cached list is already by last update; 'created' re-sorts a copy."""
    if sort == "created":
        return sorted(items, key=lambda g: g["created_at"], reverse=True)
    return items


def settings_dialog(prefs):
    def seg(name, options):
        return "".join(
            f'<label><input type=radio name={name} value={k}{" checked" if prefs[name] == k else ""}>'
            f'<span class="f-{k}">{v}</span></label>'
            for k, v in options.items()
        )
    return (
        f'<template id=ok-icon>{CHECK}</template><dialog id=settings class="modal" aria-labelledby=m-title>'
        f'<div class="m-head"><div id=m-title class="m-title">Display settings</div>'
        f'<button id=m-close class="icon-btn ghost" type=button aria-label="Close">{XMARK}</button></div>'
        '<div class="m-body">'
        f'<fieldset><legend>Font</legend><div class="seg">{seg("font", FONTS)}</div></fieldset>'
        f'<fieldset><legend>Clicks open</legend><div class="seg">{seg("open", OPENS)}</div>'
        '<p class="hint">Same window keeps the Back button working.</p></fieldset>'
        '<fieldset><legend>Dates</legend><label class="switch"><input type=checkbox id=showtime'
        f'{" checked" if prefs["time"] == "1" else ""}><span class="track"></span>Show time</label>'
        '<p class="hint">Malaysia time (UTC+8), e.g. 4 Nov 14:05</p></fieldset></div>'
        '<div class="m-foot"><div id=m-normal class="m-row"><span id=m-msg class="m-msg" aria-live=polite></span>'
        '<button id=m-save class="btn primary" type=button disabled>Save</button></div>'
        '<div id=m-ask class="m-row ask" role=alert hidden><span class="m-msg">You have unsaved changes.</span>'
        '<button id=m-keep class="btn" type=button>Keep editing</button>'
        '<button id=m-discard class="btn danger" type=button>Exit anyway</button></div></div>'
        '</dialog>'
    )


def index_page(page, refresh, sort="updated", prefs=DEFAULT_PREFS):
    sort = sort if sort in SORTS else "updated"
    field = f"{sort}_at"
    held = gists(refresh)  # the update check compares against this list, whatever the sort
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
    return shell(
        "localgist",
        '<input type=radio name=t id=t1 checked aria-label="Show first column">'
        '<input type=radio name=t id=t2 aria-label="Show second column">'
        '<div class="top"><header><a class="brand" href="/">localgist</a>'
        f'<label class="search">{ICON}<input id=q type=search placeholder="Search {len(items)} gists" '
        'autocomplete=off spellcheck=false aria-label="Search gists"><kbd id=k>⌘F</kbd></label>'
        f'<nav class="sort" aria-label="Sort by date">{seg}</nav>'
        f'<div class="meta"><span id=cnt>{len(items)} gists</span>'
        f'<nav id=pg aria-label="Pages">{nav}<a class="refresh" href="/?p={page}{qs}&refresh=1" title="Reload from GitHub" aria-label="Reload from GitHub">{REFRESH}</a></nav></div>'
        f'<button id=gear class="icon-btn" type=button aria-haspopup=dialog aria-controls=settings aria-label="Display settings" title="Display settings">{GEAR}</button></header>'
        f'<div class="scope" role=group aria-label="Search in"><span>Search in</span>{chips}<span id=st aria-live=polite></span></div>'
        f'<div class="tabs"><label for=t1>{r1}</label><label for=t2>{r2}</label></div></div>'
        f'<main><section class="c1"><h2>{r1}</h2><div id=r1>{rows(left, field, prefs["open"])}</div></section>'
        f'<section class="c2"><h2>{r2}</h2><div id=r2>{rows(right, field, prefs["open"])}</div></section></main>'
        f'{settings_dialog(prefs)}'
        '<div role=status aria-live=polite><button id=upd class=toast type=button hidden>New update available. Click to refresh</button></div>'
        f'<script type="application/json" id=d data-v="{html.escape(signature(held))}">{search_data(items, field)}</script>',
        prefs,
        script=True,
    )


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
        self.send_header("Content-Security-Policy", CSP)  # only the one page script may run
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
                prefs = read_prefs(self.headers.get("Cookie"))
                return self.send(200, index_page(int(p) if p.isdigit() else 1, "refresh" in q, q.get("sort", ["updated"])[0], prefs))
            if url.path == "/search":
                scopes = [s for s in q.get("in", ["desc"])[0].split(",") if s in SCOPES] or ["desc"]
                return self.send(200, json.dumps(search(q.get("q", [""])[0][:200], scopes)), "application/json")
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
