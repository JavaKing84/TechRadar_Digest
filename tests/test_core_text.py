# -*- coding: utf-8 -*-
"""core.text 纯函数：不做 IO，直接断言。"""
from __future__ import annotations

from news_ai_science.core.text import (
    canonical_url,
    collapse_spaces,
    looks_english,
    strip_html,
    truncate,
    url_slug,
)


def test_collapse_spaces():
    assert collapse_spaces("  a \n\n b\t c  ") == "a b c"
    assert collapse_spaces("") == ""


def test_truncate_keeps_short_text():
    assert truncate("短文本", 10) == "短文本"
    assert truncate("x" * 20, 5) == "xxxxx…"
    assert truncate(None, 5) == ""


def test_strip_html():
    assert strip_html("<p>你好  世界</p>") == "你好 世界"


def test_looks_english():
    assert looks_english("OpenAI releases a new model today") is True
    assert looks_english("这是一条完全中文的标题，讲模型发布") is False
    assert looks_english("") is False


def test_canonical_url_drops_tracking_and_scheme():
    """去重键必须吃掉 utm 尾巴、协议、www、尾斜杠，否则同一篇会推两遍。"""
    base = "https://techcrunch.com/2026/10/26/openai-new-model/"
    for variant in (
        "http://techcrunch.com/2026/10/26/openai-new-model",
        "https://www.techcrunch.com/2026/10/26/openai-new-model/",
        "https://techcrunch.com/2026/10/26/openai-new-model/?utm_source=rss&utm_medium=feed",
        "https://techcrunch.com/2026/10/26/openai-new-model/#comments",
    ):
        assert canonical_url(variant) == canonical_url(base)


def test_canonical_url_keeps_different_paths_apart():
    """只归一化，不做标题级合并：不同文章必须仍是不同键。"""
    assert canonical_url("https://a.com/2026/10/26/x") != canonical_url("https://a.com/2026/10/26/y")
    assert canonical_url("") == ""


def test_url_slug():
    assert url_slug("https://techcrunch.com/2026/10/26/openai-new-model/?utm=x") == "openai-new-model"
    assert url_slug("https://a.com/") == ""
