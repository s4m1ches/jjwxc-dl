#!/usr/bin/env python3
"""
Resumable JJWXC downloader built on Playwright.

Subcommands:
  login   open a headed browser so you can sign in once; the profile persists
  fetch   walk the chapter list, save each chapter as its own JSON checkpoint
  build   assemble the saved chapters into an EPUB

Only chapters the account actually has access to are saved. Paywalled
chapters are recorded as such and reported at the end, never written into
the book as placeholder text.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import random
import re
import sys
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx
import lxml.html
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# A Windows console defaults to the ANSI codepage (cp1251 here), which raises
# UnicodeEncodeError the moment a Chinese chapter title is printed. Force UTF-8
# on our own streams so progress output never kills a long download.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

INDEX_URL = "https://www.jjwxc.net/onebook.php?novelid={novelid}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Shown in place of a chapter body when the account has not bought it.
PAYWALL_MARKERS = ("请先购买", "购买本章", "订阅本章", "尚未购买",
                   "需要购买", "先登录", "请登录")


@dataclass
class Chapter:
    index: int
    title: str
    url: str
    vip: bool
    words: int = 0        # wordCount advertised on the index page
    text: str = ""
    status: str = "pending"  # ok | paywalled | error | pending

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Novel:
    novelid: str
    title: str
    author: str
    cover_url: str
    chapters: list


def workdir(novelid: str, root: Path) -> Path:
    return root / novelid


# --------------------------------------------------------------------------- #
# index parsing
# --------------------------------------------------------------------------- #

def fetch_index(novelid: str) -> Novel:
    """Pull the table of contents. Plain HTTP suffices here; only the chapter
    bodies are rendered by JS."""
    r = httpx.get(INDEX_URL.format(novelid=novelid),
                  headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    # JJWXC serves GB18030; hand lxml the raw bytes so it honours meta charset.
    tree = lxml.html.fromstring(r.content)

    def one(xpath: str, what: str) -> str:
        got = tree.xpath(xpath)
        if not got:
            sys.exit("could not find %s on the index page - is novelid=%s correct?"
                     % (what, novelid))
        return got[0].strip()

    title = one('//span[@itemprop="articleSection"]/text()', "novel title")
    author = one('//span[@itemprop="author"]/text()', "author")

    cover = tree.xpath('//img[@class="noveldefaultimage"]/@src')
    cover_url = cover[0] if cover else ""

    # Walk rows instead of zipping two independent xpath result lists - that is
    # what pairs titles with the wrong URLs the moment the lists diverge.
    #
    # Free and VIP rows are marked up differently. A free row is an ordinary
    # link:   <a href="...onebook.php?...chapterid=N">title</a>
    # A VIP row has no href at all - the target sits in rel= and points at the
    # logged-in host, with the click handled by JS:
    #   <a itemprop="url" onclick="vip_buy('vip_N')"
    #      rel="http://my.jjwxc.net/onebook_vip.php?...chapterid=N">title</a>
    # Only matching on href silently skips every paid chapter.
    chapters = []
    for row in tree.xpath('//tr[contains(@itemprop, "chapter")]'):
        vip_rel = row.xpath('.//a[@itemprop="url"][contains(@rel, "onebook_vip.php")]')
        free_a = row.xpath('.//a[contains(@href, "chapterid=")]')
        if vip_rel:
            link, url, vip = vip_rel[0], vip_rel[0].get("rel", ""), True
        elif free_a:
            link, url, vip = free_a[0], free_a[0].get("href", ""), False
        else:
            continue

        label = " ".join(t.strip() for t in link.itertext() if t.strip())
        if not label:
            label = "Chapter %d" % (len(chapters) + 1)

        url = url.strip()
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        elif not url.startswith("https://"):
            url = "https://www.jjwxc.net/" + url.lstrip("/")

        words = 0
        wc = row.xpath('.//*[@itemprop="wordCount"]/text()')
        if wc and wc[0].strip().isdigit():
            words = int(wc[0].strip())

        chapters.append(Chapter(index=len(chapters) + 1, title=label,
                                url=url, vip=vip, words=words))
    if not chapters:
        sys.exit("no chapters found - the page layout may have changed")
    return Novel(novelid, title, author, cover_url, chapters)


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

EXTRACT_JS = """() => {
    const box = document.querySelector('div.noveltext');
    if (!box) return '';
    const kids = Array.from(box.children).filter(e => e.tagName === 'DIV');
    const pool = kids.length ? kids : [box];
    const best = pool.reduce((a, b) =>
        (b.innerText || '').trim().length > (a.innerText || '').trim().length ? b : a);
    return (best.innerText || '').trim();
}"""


async def read_chapter(page, ch: Chapter, timeout_ms: int) -> Chapter:
    await page.goto(ch.url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_selector("div.noveltext", timeout=timeout_ms)
    except PWTimeout:
        ch.status = "error"
        return ch

    # The body lives in whichever child div of .noveltext holds the most text;
    # its siblings are nav, ads and anti-scraping filler.
    text = await page.evaluate(EXTRACT_JS)

    if not text:
        ch.status = "error"
        return ch
    if len(text) < 200 and any(m in text for m in PAYWALL_MARKERS):
        ch.status = "paywalled"
        return ch

    ch.text = re.sub(r"\n{3,}", "\n\n", text)
    ch.status = "ok"
    return ch


async def cmd_fetch(args) -> None:
    novel = fetch_index(args.novelid)
    out = workdir(args.novelid, args.out)
    chdir = out / "chapters"
    chdir.mkdir(parents=True, exist_ok=True)

    free = [c for c in novel.chapters if not c.vip]
    paid = [c for c in novel.chapters if c.vip]
    print("%s - %s" % (novel.title, novel.author))
    print("%d chapters: %d free (%s chars), %d VIP (%s chars)"
          % (len(novel.chapters), len(free), format(sum(c.words for c in free), ","),
             len(paid), format(sum(c.words for c in paid), ",")))
    if paid:
        # VIP chapters bill per 1000 characters; 100 coins = 1 CNY.
        coins = sum(c.words for c in paid) / 1000.0 * 5
        print("VIP portion costs roughly %.0f coins (~%.1f CNY) at the "
              "standard 5 fen/1k rate" % (coins, coins / 100))

    (out / "meta.json").write_text(
        json.dumps({"novelid": novel.novelid, "title": novel.title,
                    "author": novel.author}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    if novel.cover_url and not (out / "cover.jpg").exists():
        try:
            img = httpx.get(novel.cover_url, headers={"User-Agent": UA}, timeout=30)
            img.raise_for_status()
            (out / "cover.jpg").write_bytes(img.content)
            print("cover saved")
        except Exception as e:
            print("cover download failed (%s) - continuing without it" % e)

    todo = []
    for ch in novel.chapters:
        f = chdir / ("%04d.json" % ch.index)
        if f.exists():
            prev = json.loads(f.read_text(encoding="utf-8"))
            if prev.get("status") == "ok":
                continue  # already have it, resume past it
        todo.append(ch)

    if not todo:
        print("everything already downloaded - nothing to do")
        return
    print("%d chapters to fetch\n" % len(todo))

    profile = args.profile.expanduser()
    profile.mkdir(parents=True, exist_ok=True)

    counts = {"ok": 0, "paywalled": 0, "error": 0}
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=args.headless,
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            for n, ch in enumerate(todo, 1):
                tag = "VIP" if ch.vip else "free"
                print("[%d/%d] %s (%s) ... " % (n, len(todo), ch.title, tag),
                      end="", flush=True)
                for attempt in range(1, args.retries + 1):
                    try:
                        ch = await read_chapter(page, ch, int(args.timeout * 1000))
                        break
                    except Exception as e:
                        if attempt == args.retries:
                            ch.status = "error"
                            print("failed (%s) " % e, end="")
                        else:
                            await asyncio.sleep(args.delay * attempt * 2)

                counts[ch.status] = counts.get(ch.status, 0) + 1
                print(ch.status + (", %d chars" % ch.chars if ch.status == "ok" else ""))
                (chdir / ("%04d.json" % ch.index)).write_text(
                    json.dumps(asdict(ch), ensure_ascii=False, indent=2),
                    encoding="utf-8")

                # Polite guest: serial, jittered, never hammering.
                await asyncio.sleep(args.delay + random.random() * args.delay)
        finally:
            await ctx.close()

    print("\nok: %d  paywalled: %d  errors: %d"
          % (counts["ok"], counts["paywalled"], counts["error"]))
    if counts["paywalled"]:
        print("Paywalled = chapters this account has not bought, or you are "
              "not signed in (run the login subcommand).")
    if counts["error"]:
        print("Re-run the same command to retry only what failed.")


async def cmd_login(args) -> None:
    profile = args.profile.expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, user_agent=UA,
            viewport={"width": 1280, "height": 900})
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.jjwxc.net/", wait_until="domcontentloaded")
            print("Sign in to your account in the window that just opened.")
            print("The session is kept in the profile dir, so this is one-time.")
            input("Press Enter here once you are signed in... ")
        finally:
            await ctx.close()
    print("profile saved: %s" % profile)


# --------------------------------------------------------------------------- #
# epub
# --------------------------------------------------------------------------- #

def cmd_build(args) -> None:
    from ebooklib import epub

    out = workdir(args.novelid, args.out)
    meta_f = out / "meta.json"
    if not meta_f.exists():
        sys.exit("no download found in %s - run fetch first" % out)
    meta = json.loads(meta_f.read_text(encoding="utf-8"))

    saved = [json.loads(f.read_text(encoding="utf-8"))
             for f in sorted((out / "chapters").glob("*.json"))]
    good = [c for c in saved if c.get("status") == "ok" and c.get("text")]
    missing = [c for c in saved if c.get("status") != "ok"]
    if not good:
        sys.exit("no chapter text available to build from")

    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(meta["title"])
    book.add_author(meta["author"])
    book.set_language("zh-Hans")

    cover = out / "cover.jpg"
    if cover.exists():
        book.set_cover(file_name="cover.jpg", content=cover.read_bytes())

    spine = ["nav"]
    toc = []
    for c in good:
        item = epub.EpubHtml(title=c["title"],
                             file_name="chap_%04d.xhtml" % c["index"],
                             lang="zh-Hans")
        # Escape before interpolating, or a stray < or & yields a broken EPUB.
        paras = "".join("<p>%s</p>" % html.escape(line)
                        for line in c["text"].split("\n") if line.strip())
        item.content = "<h1>%s</h1>%s" % (html.escape(c["title"]), paras)
        book.add_item(item)
        spine.append(item)
        toc.append(item)

    book.spine = spine
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    safe = re.sub(r'[\\/:*?"<>|]', "_", meta["title"])
    target = args.epub or (out / (safe + ".epub"))
    epub.write_epub(str(target), book)

    total = sum(len(c["text"]) for c in good)
    print("wrote %s" % target)
    print("%d chapters, %s characters" % (len(good), format(total, ",")))
    if missing:
        print("\n%d chapters are NOT in this file:" % len(missing))
        for c in missing[:10]:
            print("  %4d  %s  [%s]" % (c["index"], c["title"], c.get("status")))
        if len(missing) > 10:
            print("  ... and %d more" % (len(missing) - 10))


# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=Path("downloads"),
                   help="where checkpoints and output go (default: ./downloads)")
    p.add_argument("--profile", type=Path, default=Path("./.jjwxc-profile"),
                   help="persistent browser profile holding your session")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="sign in once; the session is reused afterwards")

    f = sub.add_parser("fetch", help="download chapters (resumable)")
    f.add_argument("novelid")
    f.add_argument("--delay", type=float, default=2.0,
                   help="base seconds between chapters, jittered (default: 2)")
    f.add_argument("--timeout", type=float, default=20.0)
    f.add_argument("--retries", type=int, default=3)
    f.add_argument("--headless", action="store_true",
                   help="hide the browser (omit while testing)")

    b = sub.add_parser("build", help="assemble the EPUB from saved chapters")
    b.add_argument("novelid")
    b.add_argument("--epub", type=Path, default=None)

    args = p.parse_args()
    if args.cmd == "fetch":
        asyncio.run(cmd_fetch(args))
    elif args.cmd == "login":
        asyncio.run(cmd_login(args))
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
