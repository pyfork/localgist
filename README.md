# localgist

A list of your own GitHub gists in a Finviz-news layout: 2 columns × 90 rows, newest first, on your own machine only. Each row opens the gist on GitHub.

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

- Desktop: Finviz-sized. The list runs the full width (up to 1440px) in two boxed tables (1px border, 6px corners, 27px rows) of 90 rows each, then page 2.
- Under 1024px (phones, portrait tablets): never a masthead; the Layout choice is hidden.
- Under 768px: full width, one column, sticky search and tabs, rows at least 48px tall.
- Sort: **Updated** (default, most recently edited first) or **Created** (newest first). GitHub itself lists by creation date; localgist re-sorts.
- Columns are headed **Page one**, **Page two** (then Page three, Page four on page 2).
- Row: Finviz-style stamp in Malaysia time, `08:54AM` if today, else `Nov-04`, right-aligned, then the description. Click to open the gist on GitHub (same window by default, so Back returns to the list with your search still in place).
- While the list page is open and visible, it checks for new or edited gists every minute and shows "New update available. Click to refresh".
- Light and dark follow the system setting.
- The header band has its own tint, a light grey in light mode and a lighter panel in dark mode, with the list area below it plain, as on Finviz.

## Search

- `⌘F` (Ctrl F elsewhere) focuses the search box. Press it again inside the box for the browser's own find.
- **Search in** (one or more): Description (default), File name, Content. Every word must appear somewhere in the ticked fields.
- Description and file-name search runs in the browser, instantly.
- Content search runs on the server. The first time you tick Content, it fetches each gist once (about 20 s for 264 gists, results fill in as it goes, with a progress note). After that a search takes about 30 ms, and only edited gists are fetched again.
- `Enter` opens the first match, `Esc` clears.

## Rows

- Each row: stamp, description with a small superscript dot when the gist has comments (hover it for the count; on phones the dot sits just before the title), then two buttons that float over the end of the row on hover, so titles keep the full width (always shown on touch screens 768px and wider; hidden under 768px, so phones only read and open):
  - **Copy link** (2.5× as wide as Delete, so the safe action is the easy one to hit): copies the GitHub URL; the icon turns into a tick and "Link copied" shows.
  - **Delete**: the row turns red and asks, in place, "Delete for good? **Yes** / **No**". No is focused; Esc or clicking elsewhere also means No. Yes deletes it from GitHub for good and removes the row.
- Delete only works from this page: the server requires a matching `Origin`, a same-origin fetch and a per-run secret token, so other websites and stray tools cannot trigger it.

## Display settings

The gear button (top right), or **⌘K** (Ctrl K elsewhere), opens a wide dark settings window (60% of the screen, Gmail-help style):

| Setting | Options | Default |
|---|---|---|
| Theme | System · Light · Dark | System |
| Font | Arial · Source Sans 3 · Helvetica · IBM Plex Sans (the Finviz font) | Arial |

Text is 12px in every font, the same as Finviz's news rows (14px on phones). Corners follow Finviz: 6px on controls and tables, 4px on small items.
| Layout (1024px and wider: desktops, landscape tablets) | Masthead · Minimal (hides the colour strip, nameplate and chart) | Masthead |
| Back to top button | Show · Hide (bottom right, appears after the first screen) | Show |
| Clicks open | Same window · New tab | Same window (Back works) |

- Choices preview live behind the window. **Save** (or ⌘S) keeps them. **Reset** puts the defaults back (Save to keep).
- **Esc** closes once saved. With unsaved changes, Esc or ✕ asks: **Keep editing** or **Exit anyway** (which undoes the preview).
- Saved settings and the Search-in ticks live in one cookie on this browser for a year. The server reads it on every page load, so they survive `pydown` / `pyup`, restarts and port changes. They do not follow you to another browser or a private window.
- Source Sans 3 and IBM Plex Sans are loaded from Google Fonts, only while picked.
- Icons are Heroicons only.

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

## Masthead and colour themes

- **Colour themes** in the settings window, shown as mini page previews: **Finviz** (default; white in light mode, charcoal in dark mode), FT salmon, Economist red, Cover Ink, Newsprint, Sage Ledger, Night Edition.
- A thin three-colour strip of the theme runs along the top. No images.
- Serif nameplate and dateline, section rules, and a **Gists written, by month** chart: hover a bar for its count, click it to list that month, drag the two handles to resize the period or the middle to move it, ✕ to reset.

## Check

```bash
python3 test_serve.py
```
