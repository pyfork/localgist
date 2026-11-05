# localgist

Read your own GitHub gists in a Finviz-news layout: 2 columns × 90 rows, newest first, on your own machine only.

## Run

```bash
pyup     # start in the background and open http://localhost:8770 in your browser
pydown   # stop
```

Also answers at `http://localgist.localhost:8770` in Chrome and Firefox (Safari cannot resolve `*.localhost`).

Without the aliases: `python3 serve.py` (Ctrl+C to stop).

Needs Python 3 and a logged-in `gh` (`gh auth login`). No installs.

It stops by itself after 60 minutes with no page loads. Sleep does not count.

## Layout

- Desktop: two columns of 90 rows (gists 1–90 left, 91–180 right), then page 2.
- Under 768px: one column, two sticky tabs to switch, 44px rows.
- Row: `09:12PM` if updated today, else `Nov-03`, then the description. Click to read the rendered Markdown.

## Security

- Listens on `127.0.0.1` only; any other `Host` header gets 403 (blocks DNS rebinding).
- Token comes from `gh auth token` at start, held in memory, never written to disk.
- Gist list and pages are cached in memory only; nothing about your gists is stored in this repo.
- Strict CSP: no scripts, no framing, no forms.

## Check

```bash
python3 test_serve.py
```
