"""Self-check: python3 test_serve.py (no network, no token)."""
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

# CSP hash must match the script actually shipped
import base64, hashlib
assert serve.JS_HASH == "sha256-" + base64.b64encode(hashlib.sha256(serve.JS.encode()).digest()).decode()
assert "'unsafe-inline'" not in serve.CSP.split("script-src")[1].split(";")[0]

# public repo: nothing but code may ever be tracked
tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=serve.__file__.rsplit("/", 1)[0]).stdout.split()
assert set(tracked) <= {"README.md", "serve.py", "test_serve.py", ".gitignore"}, tracked

print("ok")
