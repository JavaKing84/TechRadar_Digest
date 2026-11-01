# -*- coding: utf-8 -*-
"""
[文件] tests/test_crawl_web.py
[职责] 覆盖爬虫里「不依赖浏览器」的纯逻辑：链接规范化、正文抽取、
      标题清洗、文章链接判定、导航重试。全部离线，秒级跑完，
      不需要 Camoufox 内核，因此可以在服务器/CI 上稳定执行。
"""
from __future__ import annotations

import pytest

from news_ai_science.core.errors import CrawlError
from news_ai_science.stages.crawl import web


# ---------------------------------------------------------------------
# 链接规范化
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # 追踪参数与片段必须被丢掉
        (
            "https://techcrunch.com/2026/10/20/ai-startup-raises-100m/?utm_source=x&utm_medium=y#comments",
            "https://techcrunch.com/2026/10/20/ai-startup-raises-100m",
        ),
        # 主机名与协议小写
        ("HTTPS://WWW.Example.COM/news/story/", "https://www.example.com/news/story"),
        # 无追踪参数的查询串保留，但按 key 排序以便稳定去重
        ("https://a.com/p?b=2&a=1", "https://a.com/p?a=1&b=2"),
        # 根路径的 "/" 不能剥
        ("https://a.com/", "https://a.com/"),
        # fbclid / msclkid 这类也算追踪
        ("https://a.com/x?id=9&fbclid=zzz", "https://a.com/x?id=9"),
    ],
)
def test_canonical_url(raw: str, expected: str) -> None:
    assert web.canonical_url(raw) == expected


def test_canonical_url_empty() -> None:
    assert web.canonical_url("") == ""


def test_canonical_url_merges_duplicates() -> None:
    """同一篇文章的三种写法必须归一到同一个键，否则跨源去重失效。"""
    a = web.canonical_url("https://Site.com/2026/10/ai-news/?utm_source=rss")
    b = web.canonical_url("https://site.com/2026/10/ai-news#top")
    c = web.canonical_url("https://site.com/2026/10/ai-news/")
    assert a == b == c


# ---------------------------------------------------------------------
# 文章链接判定
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://techcrunch.com/2026/10/20/openai-launches-something/", True),
        ("https://techcrunch.com/tag/openai/", False),
        ("https://techcrunch.com/about/", False),
        ("https://other-site.com/2026/10/20/x/", False),
        ("https://techcrunch.com/", False),
    ],
)
def test_is_article_url(url: str, ok: bool) -> None:
    assert web._is_article_url(url, "techcrunch.com") is ok


# ---------------------------------------------------------------------
# 标题清洗
# ---------------------------------------------------------------------

def test_clean_title_strips_site_suffix() -> None:
    assert web._clean_title("AI startup raises $100M | TechCrunch", "TechCrunch") == \
        "AI startup raises $100M"


def test_clean_title_keeps_inner_dash() -> None:
    title = "AI - the next frontier"
    assert web._clean_title(title, "Wired") == title


# ---------------------------------------------------------------------
# 正文抽取
# ---------------------------------------------------------------------

def test_extract_main_text_prefers_article() -> None:
    para = "人工智能公司今天宣布了一轮新的融资，用于扩大模型训练规模。" * 8
    html = f"""
    <html><body>
      <nav><a href='/'>首页</a><a href='/tag/ai'>AI</a></nav>
      <article><h1>标题</h1><p>{para}</p><p>{para}</p></article>
      <footer>版权所有</footer>
    </body></html>
    """
    text = web.extract_main_text(html)
    assert "融资" in text
    assert "版权所有" not in text


def test_extract_main_text_too_short_returns_empty() -> None:
    assert web.extract_main_text("<html><body><p>短</p></body></html>") == ""


def test_extract_main_text_empty_input() -> None:
    assert web.extract_main_text("") == ""


# ---------------------------------------------------------------------
# 重试与限速
# ---------------------------------------------------------------------

class _FlakyPage:
    """前 n 次 goto 抛异常，之后成功——模拟网络抖动。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def goto(self, url, timeout=None, wait_until=None):  # noqa: ANN001
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("模拟超时")
        return None


def test_goto_retries_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(web, "RETRY_BASE_DELAY", 0.0)
    page = _FlakyPage(fail_times=2)
    web._goto(page, "https://a.com/x", "[t]", attempts=3)
    assert page.calls == 3


def test_goto_raises_after_attempts(monkeypatch) -> None:
    monkeypatch.setattr(web, "RETRY_BASE_DELAY", 0.0)
    page = _FlakyPage(fail_times=5)
    with pytest.raises(TimeoutError):
        web._goto(page, "https://a.com/x", "[t]", attempts=2)
    assert page.calls == 2


def test_pace_disabled_is_noop() -> None:
    import time as _time

    started = _time.time()
    web._pace(started, 0)
    assert _time.time() - started < 0.2


def test_crawl_source_rejects_missing_url() -> None:
    with pytest.raises(CrawlError):
        web.crawl_source(browser=None, source={"id": "x"}, max_items=1, min_body_chars=1)


# ---------------------------------------------------------------------
# 超时与单源预算（线上 techcrunch 拖垮整轮之后的回归）
# ---------------------------------------------------------------------

class _TimeoutRecorder:
    """记录每次 goto 实际用的 timeout 值。"""

    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def goto(self, url, timeout=None, wait_until=None):  # noqa: ANN001
        self.timeouts.append(timeout)
        return None


def test_goto_default_timeout_is_15000() -> None:
    page = _TimeoutRecorder()
    web._goto(page, "https://a.com/x")
    assert page.timeouts == [15000]
    assert web.PAGE_TIMEOUT_MS == 15000


def test_goto_timeout_is_overridable() -> None:
    page = _TimeoutRecorder()
    web._goto(page, "https://a.com/x", timeout_ms=30000)
    assert page.timeouts == [30000]


class _StubPage:
    def close(self) -> None:
        pass

    def wait_for_timeout(self, ms) -> None:  # noqa: ANN001
        pass

    def content(self) -> str:
        return "<html><body></body></html>"


class _StubBrowser:
    def __init__(self) -> None:
        self.pages = 0

    def new_page(self):
        self.pages += 1
        return _StubPage()


class _TimeStub:
    """每次取时间推进 10 秒，让预算必然超时。"""

    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        self.t += 10.0
        return self.t


def test_crawl_source_budget_stops_before_article_pages(monkeypatch) -> None:
    """预算耗尽时保留已抓到的、不再开新的正文页——而不是把整轮拖到超时。"""
    monkeypatch.setattr(web, "time", _TimeStub())
    monkeypatch.setattr(web, "_goto", lambda *a, **k: None)
    monkeypatch.setattr(
        web, "_collect_links",
        lambda page, url, selector, limit: ["https://a.com/1", "https://a.com/2"],
    )
    browser = _StubBrowser()
    got = web.crawl_source(
        browser, {"id": "t", "url": "https://a.com/list"}, max_items=2, min_body_chars=1,
        retries=1, interval=0, source_budget_s=5,
    )
    assert got == []
    assert browser.pages == 1        # 只开了列表页，正文页一个都没开


def test_crawl_source_without_budget_keeps_going(monkeypatch) -> None:
    """预算是可选项：不传（0 = 不限）时行为不变。"""
    browser = _StubBrowser()
    monkeypatch.setattr(web, "time", _TimeStub())
    monkeypatch.setattr(web, "_goto", lambda *a, **k: None)
    monkeypatch.setattr(web, "_collect_links", lambda *_args: ["https://a.com/1"])
    got = web.crawl_source(
        browser, {"id": "t", "url": "https://a.com/list"}, max_items=1, min_body_chars=9999,
        retries=1, interval=0, source_budget_s=0,
    )
    assert got == []
    assert browser.pages == 2        # 列表页 + 1 个正文页（正文过短被丢，但确实抓了）
