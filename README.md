# localgist

A list of your own GitHub gists in a Finviz-news layout: 2 columns × 90 rows, newest first, on your own machine only. Each row opens the gist on GitHub in a new tab.

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

- Desktop: a centered card at most 1040px wide, with at least 32px of page showing on each side (never full width), two columns of 90 rows, then page 2.
- Under 768px: full width, one column, sticky search and tabs, rows at least 48px tall.
- Sort: **Updated** (default, most recently edited first) or **Created** (newest first). GitHub itself lists by creation date; localgist re-sorts.
- Row: date and time in Malaysia time (`4 Nov 14:05`; the year shows when it is not this year), then the description. Click to open the gist on GitHub in a new tab.
- While the list page is open and visible, it checks for new or edited gists every minute and shows "New update available. Click to refresh".
- Light and dark follow the system setting.

## Search

- `⌘F` (Ctrl F elsewhere) focuses the search box. Press it again inside the box for the browser's own find.
- **Search in** (one or more): Description (default), File name, Content. Every word must appear somewhere in the ticked fields.
- Description and file-name search runs in the browser, instantly.
- Content search runs on the server. The first time you tick Content, it fetches each gist once (about 20 s for 264 gists, results fill in as it goes, with a progress note). After that a search takes about 30 ms, and only edited gists are fetched again.
- `Enter` opens the first match, `Esc` clears.

## Display settings

The ⚙ button (top right) opens:

- **Font:** Arial (default), Source Sans 3, Helvetica. Source Sans 3 is loaded from Google Fonts, only when you pick it.
- **Dates:** Show time (default on). Off shows only `4 Nov`.

Settings and the Search-in ticks are kept in one cookie on this browser, and the server applies them when it draws the page, so nothing flickers.

## Resources

Measured on this Mac with 264 gists: first page in about 0.6 s after `pyup` (list fetched in parallel while the browser opens), about 20 MB idle, about 34 MB with the full content index (2.7 MB of gist text). Zero CPU while idle.

- Content index: up to 1 MB of text per gist, deleted gists dropped.
- Stalled connections are dropped after 30 s.
- `pyup` runs it at low priority (`nice -n 10`).
- The 60-minute idle stop frees everything.

## Security

- Listens on `127.0.0.1` only; any other `Host` header gets 403 (blocks DNS rebinding).
- Token comes from `gh auth token` at start, held in memory, never written to disk.
- Gist list and content index live in memory only; nothing about your gists is stored in this repo.
- Strict CSP: only the one page script may run (pinned by hash), it can only talk to localgist, images only from localgist, fonts only from Google Fonts, no framing, no forms.
- The settings cookie is read as untrusted input: unknown values fall back to the defaults.

## Check

```bash
python3 test_serve.py
```
