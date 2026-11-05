"""Self-check: python3 test_serve.py (no network, no token)."""
import json
import subprocess
from datetime import datetime

import serve

now = datetime(2026, 11, 5, 15, 0).astimezone()
today = now.replace(hour=9, minute=12).isoformat()
assert serve.stamp(today, now) == "09:12AM", serve.stamp(today, now)
assert serve.stamp("2026-11-03T10:00:00Z", now) == "Nov-03"

items = [{"id": str(i)} for i in range(261)]
l, r = serve.columns(items, 1)
assert (len(l), len(r), l[0]["id"], r[0]["id"]) == (90, 90, "0", "90")
l, r = serve.columns(items, 2)
assert (len(l), len(r), l[0]["id"]) == (81, 0, "180")

assert serve.host_ok("127.0.0.1:8770") and serve.host_ok("localhost:8770") and serve.host_ok("localgist.localhost:8770")
assert not serve.host_ok("evil.example:8770") and not serve.host_ok("127.0.0.1:80") and not serve.host_ok("")

assert serve.title({"id": "x", "description": "", "files": {"a.md": {}}}) == "a.md"

# search data: a description cannot close the <script> block it is embedded in
evil = [{"id": "a1", "updated_at": "2026-11-03T10:00:00Z", "description": "x</script><b>", "files": {"Notes.md": {}}}]
d = serve.search_data(evil)
assert "</script" not in d and '"x<\\/script><b> notes.md"' in d, d

# port fallback: with 8770 busy, bind() takes the next free port
import socket
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

# page count from GitHub's Link header
link = '<https://api.github.com/gists?per_page=30&page=2>; rel="next", <https://api.github.com/gists?per_page=30&page=9>; rel="last"'
assert serve.last_page(link) == 9 and serve.last_page(None) == 1

# fetch_all: pages in parallel, repeats dropped, sorted by last update (GitHub sorts by creation)
pages = {1: [{"id": "old-edited", "updated_at": "2026-11-05T09:00:00Z"}, {"id": "x", "updated_at": "2026-11-04T00:00:00Z"}],
         2: [{"id": "x", "updated_at": "2026-11-04T00:00:00Z"}, {"id": "y", "updated_at": "2026-11-05T08:00:00Z"}]}
real_gh = serve.gh
def fake_gh(path, body=None, with_headers=False):
    text = json.dumps(pages[int(path.rsplit("page=", 1)[1])])
    return (text, {"Link": '<https://api.github.com/gists?page=2>; rel="last"'}) if with_headers else text
serve.gh = fake_gh
assert [g["id"] for g in serve.fetch_all()] == ["old-edited", "y", "x"]
serve.gh = real_gh

# list cache: first load waits, a stale list is served at once and refreshed in the background
import threading, time as _t
calls = []
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

# update check: a new top gist on GitHub changes the version the page polls
def wait_unlocked():
    for _ in range(100):
        if not serve._lock.locked():
            return
        _t.sleep(0.01)
serve._list["gists"] = [{"id": "a", "updated_at": "2026-11-05T01:00:00Z"}]
serve.fetch_all = lambda: [{"id": "b", "updated_at": "2026-11-05T02:00:00Z"}, {"id": "a", "updated_at": "2026-11-05T01:00:00Z"}]
serve.gh = lambda path, *a, **k: json.dumps([{"id": "a", "updated_at": "2026-11-05T01:00:00Z"}] if "since=2026-11-05T01:00:00Z" in path else [])
v0 = serve.version(); wait_unlocked()
assert serve.version() == v0 == "1-a-2026-11-05T01:00:00Z"      # same top gist: no reload
serve._checked[0] = 0
serve.gh = lambda path, *a, **k: json.dumps([{"id": "b", "updated_at": "2026-11-05T02:00:00Z"}, {"id": "a", "updated_at": "2026-11-05T01:00:00Z"}])
serve.version(); wait_unlocked()
assert serve.version() == "2-b-2026-11-05T02:00:00Z"            # new gist: list reloaded, page sees a new version
serve._checked[0] = _t.time()
serve.gh = lambda *a, **k: (_ for _ in ()).throw(AssertionError("checked GitHub too soon"))
serve.version()                                                  # within 60 s: no GitHub call

# sort: created re-sorts a copy; unknown values fall back to updated
lst = [{"id": "a", "updated_at": "2026-11-05T09:00:00Z", "created_at": "2026-01-01T00:00:00Z"},
       {"id": "b", "updated_at": "2026-11-04T09:00:00Z", "created_at": "2026-11-04T08:00:00Z"}]
assert [g["id"] for g in serve.sort_items(lst, "created")] == ["b", "a"]
assert serve.sort_items(lst, "updated") is lst and [g["id"] for g in lst] == ["a", "b"]

# CSP hash must match the script actually shipped
import base64, hashlib
assert serve.JS_HASH == "sha256-" + base64.b64encode(hashlib.sha256(serve.JS.encode()).digest()).decode()
assert "'unsafe-inline'" not in serve.CSP.split("script-src")[1].split(";")[0]

# public repo: nothing but code may ever be tracked
tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=serve.__file__.rsplit("/", 1)[0]).stdout.split()
assert set(tracked) <= {"README.md", "serve.py", "test_serve.py", ".gitignore"}, tracked

print("ok")
