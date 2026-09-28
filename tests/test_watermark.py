import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jjwxc_dl import strip_watermarks  # noqa: E402


def test_watermark_is_removed_and_counted():
    text = "了。@无限好文，尽在晋江文学城\n\n　　天亮了。@无限好文，尽在晋江文学城"
    clean, count = strip_watermarks(text)
    assert clean == "了。\n\n　　天亮了。" and count == 2


def test_text_without_watermark_is_unchanged():
    assert strip_watermarks("天亮了。") == ("天亮了。", 0)
