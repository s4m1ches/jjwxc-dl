# jjwxc_dl

A resumable downloader that saves JJWXC (晋江文学城) chapters you have access to
into an EPUB, built on Playwright.

A rewrite of [tracywong117/jjwxc-downloader](https://github.com/tracywong117/jjwxc-downloader)
(MIT). See [Credits](#credits) for what changed and why.

## What it does and does not do

It reads chapters **your own account can already open**, and turns them into an
EPUB with a cover and a table of contents.

- Free chapters need no account at all.
- VIP chapters require you to be signed in **and to have bought them**. The tool
  signs in by opening a real browser for you to log into by hand; it never
  handles your password.
- Chapters you have not bought are recorded as `paywalled`, reported at the end,
  and left out of the EPUB. They are not filled in with placeholder text.

It is not a paywall bypass and it does not touch any protection mechanism. If
you want a book, buy it — the `fetch` output prints what the VIP part of a given
novel costs so you know in advance.

> **Keep your output files to yourself.** JJWXC embeds watermarks in VIP chapter
> text that are tied to the purchasing account. A file you share identifies you.

## Requirements

Python 3.9+ and a machine Playwright can install Chromium on. Tested on
Python 3.14 / Windows 11 and Python 3.10.

```bash
pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

## Usage

The `novelid` is in the URL of the novel's page:
`jjwxc.net/onebook.php?novelid=`**`1234567`**.

### 1. Download

```bash
python jjwxc_dl.py fetch 1234567
```

```
某本书 - 某作者
305 chapters: 21 free (65,284 chars), 284 VIP (1,071,596 chars)
VIP portion costs roughly 5358 coins (~53.6 CNY) at the standard 5 fen/1k rate
305 chapters to fetch

[1/305] 第一章 (free) ... ok, 3021 chars
[22/305] 第二十二章 (VIP) ... paywalled
```

Each chapter is checkpointed to its own JSON file, so Ctrl+C is safe: re-running
the same command skips what already succeeded and retries only what did not.

### 2. Sign in (once, for VIP chapters)

```bash
python jjwxc_dl.py login
```

A browser opens on jjwxc.net. Log in yourself, then press Enter in the terminal.
The session is kept in `.jjwxc-profile/` next to the script and reused by later
`fetch` runs. Do not run two copies of the tool against one profile — Chromium
locks it to a single process.

### 3. Build the EPUB

```bash
python jjwxc_dl.py build 1234567
```

Writes `downloads/<novelid>/<title>.epub` and lists any chapters that are not in
it.

### Options

| Flag | Default | Notes |
| --- | --- | --- |
| `--delay` | `2.0` | Base seconds between chapters; actual wait is `delay` to `2×delay`. |
| `--timeout` | `20.0` | Per-page timeout in seconds. Raise it on a slow link. |
| `--retries` | `3` | Attempts per chapter, with backoff. |
| `--headless` | off | Hide the browser. Leave it off for your first run. |
| `--out` | `downloads` | Where checkpoints and the EPUB go. |
| `--profile` | `.jjwxc-profile` | Browser profile holding your session. |

**Please do not lower `--delay`.** 300-odd sequential requests at full speed is
the quickest way to get rate-limited, and it is rude to a site you are paying.

## Layout

```
downloads/<novelid>/
├─ meta.json              title, author
├─ cover.jpg
├─ <title>.epub
└─ chapters/
   └─ 0001.json           index, title, url, vip, words, text, status
```

`status` is one of `ok`, `paywalled`, `error`.

## Troubleshooting

**Chapters you bought come back `paywalled`** — the session did not load. Re-run
`login` and confirm the browser window actually shows you as signed in. Check
that `fetch` and `login` use the same `--profile`.

**Lots of `error`** — raise `--timeout`, then `--delay`. Re-running retries only
the failures.

**Every chapter errors** — the page markup probably changed and the
`div.noveltext` selector no longer matches. Run without `--headless` and look at
the page; please open an issue with what you see.

**`UnicodeEncodeError` printing titles** — should not happen, the tool forces
UTF-8 on its own streams. If it does, report your OS and console.

## Credits

Rewritten from [tracywong117/jjwxc-downloader](https://github.com/tracywong117/jjwxc-downloader),
which established the approach: parse the index page for the chapter list, and
read each chapter body from the child of `div.noveltext` holding the most text.

Notable changes:

- **Playwright instead of Selenium.** A persistent browser context means you log
  in once and VIP chapters become reachable at all; no `chromedriver.exe` to
  version-match; locators wait on their own.
- **VIP rows are parsed.** Paid chapters carry no `href` — the target lives in
  `rel=` and points at `my.jjwxc.net`, with the click handled by JS. Matching
  only on `href` silently skips every paid chapter in the book.
- **Rows are walked, not zipped.** The original paired two independent XPath
  result lists, which mismatches titles and URLs as soon as they diverge.
- **Resumable.** Per-chapter JSON checkpoints instead of appending to one growing
  `.txt`, which duplicated the whole book on a second run.
- **Paywalled chapters are tracked** rather than silently written into the book.
- **Valid EPUB.** Chapter text is escaped before interpolation, so a stray `<` or
  `&` no longer produces a broken file. Language metadata is set.
- **Per-chapter word counts** are read from the index, which is where the cost
  estimate comes from.
- UTF-8 forced on stdout/stderr so non-UTF-8 consoles do not kill a long run.

## License

MIT. See [LICENSE](LICENSE); the original copyright notice is retained.
