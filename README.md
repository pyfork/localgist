# localgist

Read your own GitHub gists in a Finviz-news layout: 2 columns × 90 rows, newest first, on your own machine only.

## Run

```bash
pyup     # start in the background and open it in your browser
pydown   # stop
```

Without the aliases: `python3 serve.py` (Ctrl+C to stop), or `python3 serve.py --open`.

- Starts on port 8770. If 8770 is taken, it uses the next free port up to 8779 and opens that.
- If localgist is already running, `pyup` just opens the running one.
- Also answers at `localgist.localhost:<port>` in Chrome and Firefox (Safari cannot resolve `*.localhost`).
- Stops by itself after 60 minutes with no page loads. Sleep does not count.

Needs Python 3 and a logged-in `gh` (`gh auth login`). No installs.

## Layout

- Desktop: a centered 1180px page (not full width), two columns of 90 rows, then page 2.
- Under 768px: full width, one column, sticky search and tabs, rows at least 48px tall.
- Row: `09:12PM` if updated today, else `Nov-03`, then the description. Click to read the rendered Markdown.
- Light and dark follow the system setting.

## Search

- `⌘F` (Ctrl F elsewhere) focuses the search box. Press it again inside the box for the browser's own find.
- Searches every gist's description and file names (not file contents); all words must match.
- `Enter` opens the first match, `Esc` clears.

## Resources

Measured on this Mac with 262 gists: about 20 MB idle, about 29 MB peak while loading the list, about 1 s of CPU in total for the list plus 60 gist pages. Zero CPU while idle.

- Rendered gists in memory are capped at 50 (oldest dropped first).
- Stalled connections are dropped after 30 s.
- `pyup` runs it at low priority (`nice -n 10`).
- The 60-minute idle stop frees everything.

## Security

- Listens on `127.0.0.1` only; any other `Host` header gets 403 (blocks DNS rebinding).
- Token comes from `gh auth token` at start, held in memory, never written to disk.
- Gist list and pages are cached in memory only; nothing about your gists is stored in this repo.
- Strict CSP: only the one search script may run (pinned by hash), no framing, no forms.

## Check

```bash
python3 test_serve.py
```
