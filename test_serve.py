"""Self-check: python3 test_serve.py (no network, no token)."""
import base64
import contextlib
import io
import urllib.error
import hashlib
import json
import socket
import subprocess
import time as _t
from datetime import datetime

import serve

# dates, Finviz style in Malaysia time: '08:54AM' today, 'Nov-04' before
now = datetime(2026, 11, 5, 15, 0, tzinfo=serve.MYT)
assert serve.stamp("2026-11-05T00:54:00Z", now) == "08:54AM"
assert serve.stamp("2026-11-04T06:05:00Z", now) == "Nov-04"
assert serve.stamp("2026-11-04T17:30:00Z", now) == "01:30AM"  # UTC evening of the 4th is today in MYT

# columns
items = [{"id": str(i)} for i in range(261)]
l, r = serve.columns(items, 1)
assert (len(l), len(r), l[0]["id"], r[0]["id"]) == (90, 90, "0", "90")
l, r = serve.columns(items, 2)
assert (len(l), len(r), l[0]["id"]) == (81, 0, "180")

# host allow-list
assert serve.host_ok("127.0.0.1:8770") and serve.host_ok("localhost:8770") and serve.host_ok("localgist.localhost:8770")
assert not serve.host_ok("evil.example:8770") and not serve.host_ok("127.0.0.1:80") and not serve.host_ok("")

# titles and links: rows go straight to GitHub; a strange html_url falls back to the gist id
assert serve.title({"id": "x", "description": "", "files": {"a.md": {}}}) == "a.md"
assert serve.gist_url({"id": "ab12", "html_url": "https://gist.github.com/melbne/ab12"}) == "https://gist.github.com/melbne/ab12"
assert serve.gist_url({"id": "ab12", "html_url": "javascript:alert(1)"}) == "https://gist.github.com/ab12"

# search data: a description cannot close the <script> block it is embedded in
evil = [{"id": "a1", "updated_at": "2026-11-03T10:00:00Z", "html_url": "https://gist.github.com/a1",
         "description": "x</script><b>", "files": {"Notes.md": {}}}]
d = serve.search_data(evil)
assert "<" not in d and '"x\\u003c/script>\\u003cb>"' in d and '"notes.md"' in d, d
assert json.loads(d)[0][4] == "x</script><b>"  # the page's JSON.parse gets the text back unchanged
evil[0]["description"] = "<!--<script> hi"  # this one makes a browser swallow the closing </script> if any < is left
assert "<" not in serve.search_data(evil)

# prefs cookie: defaults, valid values kept, junk dropped
assert serve.read_prefs(None) == {"palette": "finviz", "font": "arial", "theme": "system", "mast": "on", "top": "on", "open": "same", "in": "desc"}
assert serve.read_prefs("lg=palette%3Dsage")["palette"] == "sage" and serve.read_prefs("lg=palette%3Devil")["palette"] == "finviz"
assert serve.read_prefs("lg=font%3Dsource%26size%3Dl%26theme%3Ddark%26open%3Dnew%26in%3Dfile%252Ccontent") == \
    {"palette": "finviz", "font": "source", "theme": "dark", "mast": "on", "top": "on", "open": "new", "in": "file,content"}
assert serve.read_prefs('lg=font%3D%22%3E%3Cscript%3E%26time%3D7%26in%3Devil') == serve.DEFAULT_PREFS
assert serve.read_prefs("lg=\x00;;;===") == serve.DEFAULT_PREFS

# page renders with prefs applied server-side (no flash) and rows link to GitHub
serve._list.update(at=_t.time(), gists=[{"id": "g1", "html_url": "https://gist.github.com/g1", "description": "Pay <b>",
                                          "files": {"pay.md": {}}, "updated_at": "2026-11-04T06:05:00Z", "created_at": "2026-11-01T00:00:00Z"}])
page = serve.index_page(1, "updated", {"palette": "sage", "font": "source", "theme": "dark", "mast": "off", "top": "off", "open": "new", "in": "desc,content"})
assert '{reload}' not in page and "refresh=1" not in page and page.count('class="refresh"') == 2  # beside the title and beside the toolbar name
assert 'data-top="off"' in page and 'name=top value=off checked' in page and 'id=totop' in page
assert serve.read_prefs("lg=top%3Doff")["top"] == "off" and serve.read_prefs("lg=top%3Devil")["top"] == "on"
assert 'data-mast="off"' in page and 'name=mast value=off checked' in page
assert serve.read_prefs("lg=mast%3Doff")["mast"] == "off" and serve.read_prefs("lg=mast%3Devil")["mast"] == "on"
assert '<div class="strip"' in page and "<img" not in page and "/static/" not in page
assert 'data-palette="sage"' in page and 'name=palette value=sage checked' in page
assert page.count('class="pcard"') == len(serve.PALETTES) == 7
assert serve.read_prefs("lg=palette%3Dft")["palette"] == "ft" and serve.read_prefs("lg=palette%3Deconomist")["palette"] == "economist"
assert 'data-theme="dark"' in page and 'data-size' not in page and 'sizeseg' not in page
assert 'class="stamp" type=button data-u="https://gist.github.com/g1"' in page and 'data-id="g1"' in page and 'ib copy' not in page
assert f'data-csrf="{serve.CSRF}"' in page and "&quot;theme&quot;: &quot;system&quot;" in page  # Reset defaults ride on the dialog
assert serve.comments({"comments": 3}).startswith('<sup class="cd"') and "3 comments" in serve.comments({"comments": 3})
assert serve.comments({"comments": 0}) == "" and "1 comment\"" in serve.comments({"comments": 1})
assert 'data-font="source"' in page and "showtime" not in page and "fonts.googleapis.com" in page
assert 'href="https://gist.github.com/g1" target=_blank rel="noopener noreferrer"' in page and "Pay &lt;b&gt;" in page
assert 'value=content checked' in page and 'value=file>' in page
assert "<h2>Page one</h2>" in page and "<h2>Page two</h2>" in page and "Gists</span>" not in page
plain = serve.index_page(1)
assert "<link id=fsrc" not in plain and 'href="https://gist.github.com/g1" rel=' in plain  # same window by default
assert "<dialog id=settings" in plain and "value=same checked" in plain
assert serve.column_name(2) == "Page three" and serve.column_name(11) == "Page 12"

# delete: only our own page (same origin + CSRF token) may delete; the gist leaves the list and index
import http.client, threading
deleted = []
serve.gh = lambda path, *a, method=None, **k: deleted.append((method, path)) or ""
serve.Handler.log_message = lambda *a: None  # quiet test output
srv = serve.bind(); port = srv.server_address[1]
old_port, serve.PORT = serve.PORT, port
threading.Thread(target=srv.serve_forever, daemon=True).start()
def call(headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("DELETE", "/api/gists/ab1", headers=headers); r = c.getresponse(); body = r.read(); c.close()
    return r.status, body
host = f"localhost:{port}"
ok_headers = {"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin", "X-CSRF": serve.CSRF}
with contextlib.redirect_stderr(io.StringIO()) as err:
    assert call({**ok_headers, "X-CSRF": "wrong"})[0] == 403
    assert call({**ok_headers, "Origin": "https://evil.example"})[0] == 403
    assert call({**ok_headers, "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call({k: v for k, v in ok_headers.items() if k != "Origin"})[0] == 403
    assert call({**ok_headers, "Host": "evil.example", "Origin": "http://evil.example"})[0] == 403
assert err.getvalue().count("localgist: refused DELETE '/api/gists/ab1'") == 5, err.getvalue()  # each refusal leaves one log line
assert "Origin 'https://evil.example'" in err.getvalue()
assert deleted == []                                             # nothing reached GitHub so far
serve._list["gists"] = [{"id": "ab1", "updated_at": "1"}]
serve._content["ab1"] = ("x", "text")
status, body = call(ok_headers)
assert status == 200 and json.loads(body)["v"] == "0" and deleted == [("DELETE", "/gists/ab1")]
assert serve._list["gists"] == [] and "ab1" not in serve._content
# a refresh in flight during a delete lands after it: the deleted gist must stay gone
started, go = threading.Event(), threading.Event()
both = [{"id": "ef3", "updated_at": "1"}, {"id": "cd2", "updated_at": "1"}]
serve._list["gists"] = list(both)
real_fetch = serve.fetch_all
serve.fetch_all = lambda: (started.set(), go.wait(5), list(both))[-1]  # GitHub answers with the old list, slowly
try:
    t = threading.Thread(target=serve._load); t.start()
    assert started.wait(5)
    serve.delete_gist("ef3")                                     # the delete finishes while the fetch is out
    go.set(); t.join(5)
finally:
    serve.fetch_all = real_fetch
assert [g["id"] for g in serve._list["gists"]] == ["cd2"], serve._list["gists"]
# the deleted-id list is pruned: a fetch that began over a minute after a delete drops that id from it
serve._gone["old"] = _t.time() - serve.GONE_GRACE - 5
serve.fetch_all = lambda: [{"id": "cd2", "updated_at": "1"}]
try:
    serve._load()
finally:
    serve.fetch_all = real_fetch
assert "old" not in serve._gone and "ef3" in serve._gone, serve._gone      # ef3 was deleted just now: still filtered
serve._gone.clear()  # later tests start with no deleted ids
# GET serves pages only: bad page numbers are page 1 (not a false "GitHub fetch failed"); ?refresh does nothing
seen, real_index = [], serve.index_page
serve.index_page = lambda p, *a: seen.append(p) or "page"
def req(method, path, headers):
    seen.clear()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers); r = c.getresponse(); body = r.read(); c.close()
    return r.status, body, list(seen)
try:
    assert req("GET", "/?p=%C2%B2", {"Host": host})[::2] == (200, [1])                # "²"
    assert req("GET", "/?p=" + "9" * 5000, {"Host": host})[::2] == (200, [1])         # past int()'s digit limit
    assert req("GET", "/?p=2", {"Host": host})[::2] == (200, [2])
    assert req("GET", "/search?q=x", {"Host": host})[0] == req("GET", "/version", {"Host": host})[0] == 404
    # only GitHub trouble is called a GitHub failure; our own bug is a 500 with its real name
    def boom(e):
        def f(*a): raise e
        return f
    serve.index_page = boom(urllib.error.URLError("down"))
    st, body, _ = req("GET", "/", {"Host": host})
    assert st == 502 and b"GitHub fetch failed" in body, body
    serve.index_page = boom(KeyError("oops"))
    with contextlib.redirect_stderr(io.StringIO()) as err:
        st, body, _ = req("GET", "/", {"Host": host})
    assert st == 500 and b"localgist error: KeyError" in body and b"GitHub" not in body and "Traceback" in err.getvalue(), body
finally:
    serve.index_page = real_index
# POST: refresh, update check and search spend GitHub quota, so only our page (same origin + CSRF token) may ask
fetches, real_fetch = [], serve.fetch_all
serve.fetch_all = lambda: fetches.append(1) or [{"id": "cd2", "updated_at": "1"}]
serve._checked[0] = _t.time()  # the update check stays quiet: no GitHub call from /api/version
try:
    with contextlib.redirect_stderr(io.StringIO()):  # each refusal logs a line; keep test output quiet
        for bad in ({**ok_headers, "X-CSRF": "wrong"}, {**ok_headers, "Sec-Fetch-Site": "same-site"},
                    {**ok_headers, "Sec-Fetch-Site": "cross-site"}, {"Host": host},                # curl, or a browser with no Fetch Metadata
                    {k: v for k, v in ok_headers.items() if k != "Origin"}):
            for path in ("/api/refresh", "/api/search?q=x&in=content", "/api/version"):
                assert req("POST", path, bad)[0] == 403, (path, bad)
    assert fetches == [] and not serve._ilock.locked()                                    # nothing reached GitHub
    assert req("POST", "/api/refresh", ok_headers)[:2] == (200, b'{"v": "1-cd2-1"}') and fetches == [1]
    assert json.loads(req("POST", "/api/search?q=x", ok_headers)[1])["ids"] == []
    assert req("POST", "/api/version", ok_headers)[:2] == (200, b"1-cd2-1")
    assert req("POST", "/api/nope", ok_headers)[0] == 404
    serve.fetch_all = boom(TimeoutError("slow"))
    assert req("POST", "/api/refresh", ok_headers)[:2] == (502, b"GitHub fetch failed: slow")
finally:
    serve.fetch_all = real_fetch
srv.shutdown(); srv.server_close(); serve.PORT = old_port

# content search: fields combine, words must all match, results grow while the index builds
serve._list["gists"] = [
    {"id": "a", "description": "Pay policy", "files": {"pay.md": {}}, "updated_at": "1"},
    {"id": "b", "description": "Hiring", "files": {"notes.md": {}}, "updated_at": "1"},
]
serve._content.clear()
serve.gh = lambda path, *a, **k: json.dumps({"files": {"f": {"content": "Overtime PAY rules" if path.endswith("/a") else "dialysis leave"}}})
assert serve.search("pay", ["desc"])["ids"] == ["a"]
assert serve.search("notes", ["desc"])["ids"] == [] and serve.search("notes", ["file"])["ids"] == ["b"]
first = serve.search("dialysis", ["content"])
for _ in range(100):
    if not serve._ilock.locked():
        break
    _t.sleep(0.01)
done = serve.search("dialysis", ["content"])
assert done["ready"] and done["done"] == 2 and done["ids"] == ["b"], (first, done)
assert serve.search("hiring dialysis", ["desc", "content"])["ids"] == ["b"]  # one word per field is fine
assert serve.search("hiring dialysis", ["desc"])["ids"] == []
assert serve.search("", ["content"])["ids"] == []  # empty query only primes the index
serve._list["gists"] = serve._list["gists"][:1]  # gist b deleted on GitHub
serve.search("x", ["content"])
for _ in range(100):
    if not serve._ilock.locked():
        break
    _t.sleep(0.01)
assert set(serve._content) == {"a"}

# page count from GitHub's Link header
link = '<https://api.github.com/gists?per_page=30&page=2>; rel="next", <https://api.github.com/gists?per_page=30&page=9>; rel="last"'
assert serve.last_page(link) == 9 and serve.last_page(None) == 1

# fetch_all: pages in parallel, repeats dropped, sorted by last update (GitHub sorts by creation)
pages = {1: [{"id": "old-edited", "updated_at": "2026-11-05T09:00:00Z"}, {"id": "x", "updated_at": "2026-11-04T00:00:00Z"}],
         2: [{"id": "x", "updated_at": "2026-11-04T00:00:00Z"}, {"id": "y", "updated_at": "2026-11-05T08:00:00Z"}]}
def fake_gh(path, body=None, with_headers=False):
    text = json.dumps(pages[int(path.rsplit("page=", 1)[1])])
    return (text, {"Link": '<https://api.github.com/gists?page=2>; rel="last"'}) if with_headers else text
serve.gh = fake_gh
assert [g["id"] for g in serve.fetch_all()] == ["old-edited", "y", "x"]

# list cache: first load waits; after that only ↻ or the page's update check fetches
calls = []
serve._list.update(at=0.0, gists=[])
serve.fetch_all = lambda: calls.append(1) or [{"id": str(len(calls))}]
assert serve.gists()[0]["id"] == "1" and len(calls) == 1        # cold: fetched in-line
assert serve.gists()[0]["id"] == "1" and len(calls) == 1        # fresh: no fetch
serve._list["at"] -= serve.STALE_SECONDS + 1
assert serve.gists()[0]["id"] == "1" and len(calls) == 1        # stale: a page load still never fetches
assert serve.gists(refresh=True)[0]["id"] == "2"                # ↻ forces a fetch

# update check: a new or edited gist on GitHub changes the version the page polls
def wait_unlocked():
    for _ in range(100):
        if not serve._lock.locked():
            return
        _t.sleep(0.01)
serve._list["gists"] = [{"id": "a", "updated_at": "2026-11-05T01:00:00Z"}]
serve.fetch_all = lambda: [{"id": "b", "updated_at": "2026-11-05T02:00:00Z"}, {"id": "a", "updated_at": "2026-11-05T01:00:00Z"}]
serve.gh = lambda path, *a, **k: json.dumps([{"id": "a", "updated_at": "2026-11-05T01:00:00Z"}] if "since=2026-11-05T01:00:00Z" in path else [])
v0 = serve.version(); wait_unlocked()
assert serve.version() == v0 == "1-a-2026-11-05T01:00:00Z"      # nothing newer: no reload
serve._checked[0] = 0
serve.gh = lambda path, *a, **k: json.dumps([{"id": "b", "updated_at": "2026-11-05T02:00:00Z"}, {"id": "a", "updated_at": "2026-11-05T01:00:00Z"}])
serve.version(); wait_unlocked()
assert serve.version() == "2-b-2026-11-05T02:00:00Z"            # new gist: list reloaded, page sees a new version
serve._list.update(at=_t.time() - serve.STALE_SECONDS - 1, gists=[{"id": "a", "updated_at": "2026-11-05T01:00:00Z"}])
serve._checked[0] = 0
serve.gh = lambda path, *a, **k: "[]"                            # nothing newer, but the list is stale
serve.version(); wait_unlocked()
assert serve.version() == "2-b-2026-11-05T02:00:00Z"            # stale: full reload (catches deletes on github.com)
serve._checked[0] = _t.time()
serve.gh = lambda *a, **k: (_ for _ in ()).throw(AssertionError("checked GitHub too soon"))
serve.version()                                                  # within 60 s: no GitHub call

# sort: created re-sorts a copy
lst = [{"id": "a", "updated_at": "2026-11-05T09:00:00Z", "created_at": "2026-01-01T00:00:00Z"},
       {"id": "b", "updated_at": "2026-11-04T09:00:00Z", "created_at": "2026-11-04T08:00:00Z"}]
assert [g["id"] for g in serve.sort_items(lst, "created")] == ["b", "a"]
assert serve.sort_items(lst, "updated") is lst

# port fallback: with 8770 busy, bind() takes the next free port
busy = socket.socket()
try:
    busy.bind(("127.0.0.1", serve.PORT)); busy.listen()
    s = serve.bind()
    assert s.server_address[1] == serve.PORT + 1, s.server_address
    s.server_close()
except OSError:
    pass  # 8770 already taken by a running localgist; fallback then shown live instead
finally:
    busy.close()

# page script must parse (a duplicate name once broke the whole page); needs node, skipped without it
import shutil, tempfile
if shutil.which("node"):
    with tempfile.TemporaryDirectory() as d:  # removed afterwards; one stray file per run used to pile up in $TMPDIR
        js = f"{d}/page.js"
        with open(js, "w") as f:
            f.write(serve.JS)
        r = subprocess.run(["node", "--check", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

# CSP: only the shipped script, no inline scripts, images only from this server
assert serve.JS_HASH == "sha256-" + base64.b64encode(hashlib.sha256(serve.JS.encode()).digest()).decode()
assert "'unsafe-inline'" not in serve.CSP.split("script-src")[1].split(";")[0]
assert "img-src 'none'" in serve.CSP

# public repo: nothing but code may ever be tracked
tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=serve.__file__.rsplit("/", 1)[0]).stdout.split()
allowed = {"README.md", "serve.py", "test_serve.py", ".gitignore"}
assert set(tracked) <= allowed, tracked

# theme: monthly chart counts and labels the busiest month
mc = serve.month_counts([{"created_at": "2026-11-01T00:00:00Z"}, {"created_at": "2026-11-02T00:00:00Z"},
                         {"created_at": "2025-12-31T17:00:00Z"}, {"created_at": "2020-01-01T00:00:00Z"}], now=now)
assert len(mc) == 24 and mc[-1] == (2026, 11, 2) and (2026, 1, 1) in mc and sum(n for *_, n in mc) == 3  # UTC Dec 31 17:00 is Jan 1 in MYT
am = serve.all_months([{"created_at": "2026-09-15T00:00:00Z"}, {"created_at": "2026-11-01T00:00:00Z"}], now=now)
assert am == [["2026-09", 1], ["2026-10", 0], ["2026-11", 1]], am
assert serve.created_month({"created_at": "2025-12-31T17:00:00Z"}) == "2026-01"
svg = serve.month_chart([{"created_at": "2026-11-01T00:00:00Z"}, {"created_at": "2026-11-02T00:00:00Z"}], now=now)
assert "2 in Nov \u201926" in svg and svg.count("<rect") == 24

print("ok")
