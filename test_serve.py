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

assert serve.host_ok("127.0.0.1:8770") and serve.host_ok("localhost:8770")
assert not serve.host_ok("evil.example:8770") and not serve.host_ok("127.0.0.1:80") and not serve.host_ok("")

assert serve.title({"id": "x", "description": "", "files": {"a.md": {}}}) == "a.md"

# public repo: nothing but code may ever be tracked
tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=serve.__file__.rsplit("/", 1)[0]).stdout.split()
assert set(tracked) <= {"README.md", "serve.py", "test_serve.py", ".gitignore"}, tracked

print("ok")
