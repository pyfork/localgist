"""Self-check: python3 test_serve.py (no network, no token)."""
import base64
import hashlib
import json
import socket
import subprocess
import time as _t
from datetime import datetime

import serve

# dates, Finviz style in Malaysia time: '08:54AM' today, '04-Nov' before
now = datetime(2026, 11, 5, 15, 0, tzinfo=serve.MYT)
assert serve.stamp("2026-11-05T00:54:00Z", now) == "08:54AM"
assert serve.stamp("2026-11-04T06:05:00Z", now) == "04-Nov"
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
assert "</script" not in d and '"x<\\/script><b>"' in d and '"notes.md"' in d, d

# prefs cookie: defaults, valid values kept, junk dropped
assert serve.read_prefs(None) == {"font": "arial", "size": "m", "theme": "system", "open": "same", "in": "desc"}
assert serve.read_prefs("lg=font%3Dsource%26size%3Dl%26theme%3Ddark%26open%3Dnew%26in%3Dfile%252Ccontent") == \
    {"font": "source", "size": "l", "theme": "dark", "open": "new", "in": "file,content"}
assert serve.read_prefs('lg=font%3D%22%3E%3Cscript%3E%26time%3D7%26in%3Devil') == serve.DEFAULT_PREFS
assert serve.read_prefs("lg=\x00;;;===") == serve.DEFAULT_PREFS

# page renders with prefs applied server-side (no flash) and rows link to GitHub
serve._list.update(at=_t.time(), gists=[{"id": "g1", "html_url": "https://gist.github.com/g1", "description": "Pay <b>",
                                          "files": {"pay.md": {}}, "updated_at": "2026-11-04T06:05:00Z", "created_at": "2026-11-01T00:00:00Z"}])
page = serve.index_page(1, False, "updated", {"font": "source", "size": "l", "theme": "dark", "open": "new", "in": "desc,content"})
assert 'data-size="l" data-theme="dark"' in page and "<output id=sz-out aria-live=polite>Large</output>" in page
assert 'class="ib copy" type=button data-u="https://gist.github.com/g1"' in page and 'data-id="g1"' in page
assert f'data-csrf="{serve.CSRF}"' in page and "&quot;theme&quot;: &quot;system&quot;" in page  # Reset defaults ride on the dialog
assert serve.comments({"comments": 3}).endswith("3</span>") and "3 comments" in serve.comments({"comments": 3})
assert serve.comments({"comments": 0}) == "" and "1 comment\"" in serve.comments({"comments": 1})
assert 'data-font="source"' in page and "showtime" not in page and "fonts.googleapis.com" in page
assert 'href="https://gist.github.com/g1" target=_blank rel="noopener noreferrer"' in page and "Pay &lt;b&gt;" in page
assert 'value=content checked' in page and 'value=file>' in page
assert "<h2>Page one</h2>" in page and "<h2>Page two</h2>" in page and "Gists</span>" not in page
plain = serve.index_page(1, False)
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
assert call({**ok_headers, "X-CSRF": "wrong"})[0] == 403
assert call({**ok_headers, "Origin": "https://evil.example"})[0] == 403
assert call({**ok_headers, "Sec-Fetch-Site": "cross-site"})[0] == 403
assert call({k: v for k, v in ok_headers.items() if k != "Origin"})[0] == 403
assert call({**ok_headers, "Host": "evil.example", "Origin": "http://evil.example"})[0] == 403
assert deleted == []                                             # nothing reached GitHub so far
serve._list["gists"] = [{"id": "ab1", "updated_at": "1"}]
serve._content["ab1"] = ("x", "text")
status, body = call(ok_headers)
assert status == 200 and json.loads(body)["v"] == "0" and deleted == [("DELETE", "/gists/ab1")]
assert serve._list["gists"] == [] and "ab1" not in serve._content
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

# list cache: first load waits, a stale list is served at once and refreshed in the background
calls = []
serve._list.update(at=0.0, gists=[])
serve.fetch_all = lambda: calls.append(1) or [{"id": str(len(calls))}]
assert serve.gists()[0]["id"] == "1" and len(calls) == 1        # cold: fetched in-line
assert serve.gists()[0]["id"] == "1" and len(calls) == 1        # fresh: no fetch
serve._list["at"] -= serve.STALE_SECONDS + 1
assert serve.gists()[0]["id"] == "1"                            # stale: old list returned at once
for _ in range(50):
    if len(calls) == 2 and not serve._lock.locked():
        break
    _t.sleep(0.01)
assert serve.gists()[0]["id"] == "2" and len(calls) == 2        # ...and replaced behind it
assert serve.gists(refresh=True)[0]["id"] == "3"                # ↻ forces a fetch

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
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(serve.JS)
    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

# CSP: only the shipped script, no inline scripts, images only from this server
assert serve.JS_HASH == "sha256-" + base64.b64encode(hashlib.sha256(serve.JS.encode()).digest()).decode()
assert "'unsafe-inline'" not in serve.CSP.split("script-src")[1].split(";")[0]
assert "img-src 'self' data:" in serve.CSP

# public repo: nothing but code may ever be tracked
tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=serve.__file__.rsplit("/", 1)[0]).stdout.split()
assert set(tracked) <= {"README.md", "serve.py", "test_serve.py", ".gitignore"}, tracked

print("ok")
