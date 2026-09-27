"""Recovering the characters JJWXC hides behind a substituted font.

VIP chapter pages ship a per-chapter webfont (jjwxcfont_*) and rewrite some
characters in the body to Private Use Area codepoints. The browser renders them
correctly because the font's glyph for U+Ennn draws the real character; saved as
text they are holes. Each substituted position is also tagged with a zero-width
character.

Every chapter font observed so far defines the SAME 200 glyph outlines and only
shuffles which PUA codepoint maps to which outline. So the outline is the stable
identity: hash it once, identify those 200 shapes once, and every chapter then
decodes by lookup. That is what this module does.

Identification combines two independent signals, because neither is enough:

  shape  - render the glyph, render candidate characters in reference fonts,
           compare. Gets most of them, but confuses visually near-identical
           pairs (有/冇, 大/犬, 白/由) where the margin is a few thousandths.
  n-gram - substitute each candidate into the places the glyph actually occurs
           and score the resulting bigrams/trigrams against the clean text of
           the same book. This decides the near-ties, and in testing it
           overrode the shape winner correctly every time it disagreed.

Nothing here touches access control: unpurchased chapters are never retrieved
in the first place, so this only makes text you already bought readable.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

PUA_LO, PUA_HI = 0xE000, 0xF8FF
ZERO_WIDTH = frozenset("\u200b\u200c\u200d\ufeff")

BOX = 64
RENDER = 160
TOPK = 40

DEFAULT_REFS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/msjh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def is_pua(ch: str) -> bool:
    return PUA_LO <= ord(ch) <= PUA_HI


def available_refs(refs=DEFAULT_REFS) -> list:
    return [r for r in refs if Path(r).exists()]


# --------------------------------------------------------------------------- #
# outline hashing
# --------------------------------------------------------------------------- #

def outline_hash(glyf, gname: str):
    """Stable id for a glyph's contours, independent of the codepoint it is
    filed under. This is what survives the per-chapter shuffling."""
    g = glyf[gname]
    if not hasattr(g, "coordinates"):
        g.expand(glyf)
    if not hasattr(g, "coordinates"):
        return None
    data = (bytes(g.flags) + b"|"
            + ",".join("%d:%d" % (x, y) for x, y in g.coordinates).encode()
            + b"|" + ",".join(str(e) for e in g.endPtsOfContours).encode())
    return hashlib.sha1(data).hexdigest()[:16]


def font_code_map(path: Path) -> dict:
    """{pua codepoint -> outline hash} for one chapter font."""
    from fontTools.ttLib import TTFont

    f = TTFont(str(path))
    glyf = f["glyf"]
    out = {}
    for cp, gname in f.getBestCmap().items():
        if PUA_LO <= cp <= PUA_HI:
            h = outline_hash(glyf, gname)
            if h:
                out[cp] = h
    return out


def to_ttf(woff: Path) -> Path:
    """Pillow/FreeType will not open woff2; keep a converted copy beside it."""
    from fontTools.ttLib import TTFont

    ttf = woff.with_suffix(".ttf")
    if not ttf.exists():
        f = TTFont(str(woff))
        f.flavor = None
        f.save(str(ttf))
    return ttf


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _render(ch: str, font):
    """Ink-cropped, aspect preserved, centred in a BOX square. Aspect matters:
    it is part of what separates 十 from 干."""
    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.new("L", (RENDER * 2, RENDER * 2), 0)
    ImageDraw.Draw(img).text((RENDER // 2, RENDER // 2), ch, fill=255, font=font)
    bb = img.getbbox()
    if not bb:
        return None
    img = img.crop(bb)
    w, h = img.size
    scale = (BOX - 4) / max(w, h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("L", (BOX, BOX), 0)
    canvas.paste(img, ((BOX - nw) // 2, (BOX - nh) // 2))
    return np.asarray(canvas, dtype=np.uint8)


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #

def strip_markers(text: str) -> str:
    return "".join(c for c in text if not is_pua(c) and c not in ZERO_WIDTH)


def build_corpus(texts) -> tuple:
    """Bigram and trigram counts over the clean part of the book."""
    bi, tri = Counter(), Counter()
    for t in texts:
        c = strip_markers(t)
        for i in range(len(c) - 1):
            bi[c[i:i + 2]] += 1
        for i in range(len(c) - 2):
            tri[c[i:i + 3]] += 1
    return bi, tri


def collect_contexts(chapters) -> dict:
    """outline hash -> [(left clean char, right clean char), ...]

    `chapters` is an iterable of (text, {pua codepoint -> hash}).
    """
    ctx = {}
    for text, code_map in chapters:
        for i, ch in enumerate(text):
            if not is_pua(ch):
                continue
            h = code_map.get(ord(ch))
            if not h:
                continue
            j = i - 1
            while j >= 0 and (is_pua(text[j]) or text[j] in ZERO_WIDTH):
                j -= 1
            left = text[j] if j >= 0 else ""
            j = i + 1
            while j < len(text) and (is_pua(text[j]) or text[j] in ZERO_WIDTH):
                j += 1
            right = text[j] if j < len(text) else ""
            ctx.setdefault(h, []).append((left, right))
    return ctx


def ngram_score(ch: str, ctxs, bi, tri) -> float:
    s = 0.0
    for left, right in ctxs:
        if left:
            s += math.log1p(bi.get(left + ch, 0))
        if right:
            s += math.log1p(bi.get(ch + right, 0))
        if left and right:
            s += 2.0 * math.log1p(tri.get(left + ch + right, 0))
    return s / max(len(ctxs), 1)


# --------------------------------------------------------------------------- #
# identification
# --------------------------------------------------------------------------- #

def shape_candidates(font_paths, refs=None, log=print) -> dict:
    """outline hash -> ranked candidate characters by shape similarity."""
    import numpy as np
    from fontTools.ttLib import TTFont
    from PIL import ImageFont

    refs = refs or available_refs()
    if not refs:
        raise RuntimeError("no reference CJK font found; pass --ref <path>")

    shapes = {}
    for w in font_paths:
        ttf = to_ttf(Path(w))
        for cp, h in font_code_map(Path(w)).items():
            shapes.setdefault(h, (ttf, cp))
    log("canonical shapes: %d (from %d chapter fonts)" % (len(shapes), len(font_paths)))

    order, targets = [], []
    for h, (ttf, cp) in sorted(shapes.items()):
        arr = _render(chr(cp), ImageFont.truetype(str(ttf), RENDER))
        if arr is not None:
            order.append(h)
            targets.append(arr)
    T = np.stack(targets)

    ref0 = TTFont(refs[0], fontNumber=0)
    cands = sorted(chr(cp) for cp in ref0.getBestCmap() if 0x4E00 <= cp <= 0x9FFF)
    log("candidates: %d, reference fonts: %d" % (len(cands), len(refs)))

    f0 = ImageFont.truetype(refs[0], RENDER)
    ok, arrs = [], []
    for c in cands:
        a = _render(c, f0)
        if a is not None:
            ok.append(c)
            arrs.append(a)
    C = np.stack(arrs)

    Tb = (T > 96).reshape(len(order), -1).astype(np.float32)
    Cb = (C > 96).reshape(len(ok), -1).astype(np.float32)
    t_area = Tb.sum(1, keepdims=True)
    c_area = Cb.sum(1)[None, :]
    iou = np.empty((len(order), len(ok)), dtype=np.float32)
    for i in range(0, len(ok), 4096):
        blk = Cb[i:i + 4096]
        inter = Tb @ blk.T
        iou[:, i:i + 4096] = inter / np.maximum(
            t_area + c_area[:, i:i + 4096] - inter, 1)
    top = np.argsort(-iou, axis=1)[:, :TOPK]

    ref_fonts = [ImageFont.truetype(r, RENDER) for r in refs]
    Tg = T.reshape(len(order), -1).astype(np.float32) / 255.0
    cache = {}
    out = {}
    for i, h in enumerate(order):
        per = []
        for j in top[i]:
            ch = ok[j]
            sims = []
            for k, rf in enumerate(ref_fonts):
                key = (ch, k)
                if key not in cache:
                    a = _render(ch, rf)
                    cache[key] = (None if a is None
                                  else a.reshape(-1).astype(np.float32) / 255.0)
                g = cache[key]
                if g is not None:
                    sims.append(1.0 - float(np.abs(Tg[i] - g).mean()))
            if sims:
                per.append({"char": ch,
                            "shape": round(float(np.mean(sims)), 5),
                            "iou": round(float(iou[i, j]), 5)})
        per.sort(key=lambda d: -d["shape"])
        out[h] = per[:10]
    return out


def resolve(cands: dict, contexts: dict, bi, tri, log=print) -> dict:
    """Combine shape ranking with n-gram evidence into one decision per shape.

    Returns hash -> {char, confidence, method, alternatives}.
    """
    resolved = {}
    for h, per in cands.items():
        if not per:
            continue
        ctxs = contexts.get(h, [])
        shape_gap = per[0]["shape"] - per[1]["shape"] if len(per) > 1 else 1.0

        if not ctxs:
            # Shape alone is not evidence, however wide the margin: the matcher
            # reliably prefers rare characters over the common ones they
            # resemble (冇 over 有, 犬 over 大). Without corpus support this is
            # a guess, and it is labelled as one.
            resolved[h] = {"char": per[0]["char"],
                           "confidence": "unverified",
                           "method": "shape-only",
                           "shape_gap": round(shape_gap, 5),
                           "occurrences": 0,
                           "alternatives": [c["char"] for c in per[1:4]]}
            continue

        scored = sorted(((ngram_score(c["char"], ctxs, bi, tri), c["char"])
                         for c in per[:6]), reverse=True)
        best_n, best_c = scored[0]
        next_n = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_n - next_n
        agrees = best_c == per[0]["char"]

        if best_n <= 0.0:
            # the glyph occurs, but no candidate produces a bigram the book
            # itself ever uses - so the corpus says nothing either way
            conf, method, char = "unverified", "shape-only", per[0]["char"]
        elif agrees and (shape_gap >= 0.010 or margin >= 1.0):
            conf, method, char = "high", "shape+ngram", best_c
        elif not agrees and margin >= 1.0:
            conf, method, char = "high", "ngram-override", best_c
        else:
            conf, method, char = "low", "ambiguous", best_c

        resolved[h] = {"char": char,
                       "confidence": conf,
                       "method": method,
                       "shape_gap": round(shape_gap, 5),
                       "ngram_margin": round(margin, 3),
                       "occurrences": len(ctxs),
                       "alternatives": [c for _, c in scored[1:4]]}

    by_conf = Counter(v["confidence"] for v in resolved.values())
    by_method = Counter(v["method"] for v in resolved.values())
    log("resolved %d shapes: %s" % (len(resolved), dict(by_conf)))
    log("  methods: %s" % dict(by_method))
    return resolved


# --------------------------------------------------------------------------- #
# applying the map
# --------------------------------------------------------------------------- #

def decode(text: str, code_map: dict, table: dict):
    """Substitute PUA characters and drop the zero-width markers.

    Returns (decoded text, stats). Unresolved holes are left in place rather
    than guessed, and counted, so a caller can report them instead of shipping
    a book with silent damage.
    """
    out = []
    stats = {"pua_seen": 0, "filled": 0, "unmapped": 0, "low_conf": 0,
             "unverified": 0, "markers_removed": 0, "chars": Counter()}
    for ch in text:
        if ch in ZERO_WIDTH:
            stats["markers_removed"] += 1
            continue
        if not is_pua(ch):
            out.append(ch)
            continue
        stats["pua_seen"] += 1
        h = code_map.get(ord(ch))
        entry = table.get(h) if h else None
        if not entry:
            stats["unmapped"] += 1
            out.append(ch)
            continue
        conf = entry.get("confidence")
        if conf == "low":
            stats["low_conf"] += 1
        elif conf == "unverified":
            stats["unverified"] += 1
        out.append(entry["char"])
        stats["filled"] += 1
        stats["chars"][entry["char"]] += 1
    return "".join(out), stats


def load_table(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("shapes", data)


def save_table(path: Path, resolved: dict, meta=None) -> None:
    path.write_text(json.dumps(
        {"meta": meta or {}, "shapes": resolved}, ensure_ascii=False, indent=1),
        encoding="utf-8")
