# -*- coding: utf-8 -*-
"""
[文件] tests/test_fallback_guards.py
[职责] 锁死本轮四项兜底改造，防止日后被改回去：
      ① crawl_all 只在「所有跑过的源都抛异常」时才抛 CrawlError；
        单纯「0 篇新文章（全被去重挡掉）」和「个别源失败」都不算整轮失败；
      ② main 对 NewsStore 构造失败返回退出码 3，不再裸崩；
      ③ 触发历史轮换时，卡片必须带红字告警 + subtitle 标记；
      ④ 轮换池受「7 天年龄 + 同一篇最多重推 2 次」限制，退出后不再回来。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from news_ai_science.core.errors import CrawlError
from news_ai_science.pipeline import runner
from news_ai_science.stages.crawl import web
from news_ai_science.stages.push.card_builder import build_multi_news_card
from news_ai_science.storage.db import NewsStore

ZH_TITLE = "英伟达发布新一代推理芯片"
ZH_SUMMARY = "英伟达在开发者大会上发布了面向推理的新一代芯片，称同等功耗下吞吐提升明显。"
ZH_BODY = "英伟达在开发者大会上发布了新一代推理芯片，官方称在同等功耗下推理吞吐有明显提升，预计明年初量产。"


# ---------------------------------------------------------------------
# ① crawl_all 的抛错条件
# ---------------------------------------------------------------------
class _FakeBrowser:
    """只提供 crawl_source 会用到的入口；本组测试把 crawl_source 整个替换掉。"""

    def new_page(self):                     # pragma: no cover - 被替换后不会走到
        raise AssertionError("测试里应替换 crawl_source，不应真的开页面")

    def close(self) -> None:
        pass


class _FakeCamoufox:
    """替身浏览器上下文：绕开 1.3GB 内核，让 crawl_all 的流程逻辑可离线验证。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return _FakeBrowser()

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture()
def fake_browser(monkeypatch):
    import camoufox.sync_api as cf

    monkeypatch.setattr(cf, "Camoufox", _FakeCamoufox)
    return None


def _cfg(sources: list[dict]) -> dict:
    return {
        "enabled_sources": sources,
        "crawl": {"max_items_per_source": 2, "total_limit": 6, "headless": True,
                  "min_body_chars": 100},
    }


def test_crawl_all_raises_when_every_source_fails(fake_browser, monkeypatch) -> None:
    """两个源全部超时 = 真故障，必须抛 CrawlError 让人知道。"""
    def boom(*a, **k):
        raise CrawlError("[techcrunch] 列表页抓取失败: Timeout 15000ms exceeded")

    monkeypatch.setattr(web, "crawl_source", boom)
    cfg = _cfg([{"id": "a", "url": "https://a.com"}, {"id": "b", "url": "https://b.com"}])

    with pytest.raises(CrawlError):
        web.crawl_all(cfg)


def test_crawl_all_tolerates_partial_failure(fake_browser, monkeypatch) -> None:
    """1 个源挂了、其余源正常但 0 篇（全被去重挡掉）——这是常态，不该抛错。"""
    calls = {"n": 0}

    def half(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CrawlError("[techcrunch] 列表页抓取失败")
        return []

    monkeypatch.setattr(web, "crawl_source", half)
    cfg = _cfg([{"id": "a", "url": "https://a.com"}, {"id": "b", "url": "https://b.com"}])

    articles, errors = web.crawl_all(cfg)
    assert articles == []
    assert len(errors) == 1, "失败源仍要如实记录，只是不再升级为整轮失败"


def test_crawl_all_zero_articles_without_errors_is_normal(fake_browser, monkeypatch) -> None:
    """所有源都正常、只是今天没有新文章：返回空列表，不抛错。"""
    monkeypatch.setattr(web, "crawl_source", lambda *a, **k: [])
    cfg = _cfg([{"id": "a", "url": "https://a.com"}, {"id": "b", "url": "https://b.com"}])

    articles, errors = web.crawl_all(cfg)
    assert (articles, errors) == ([], [])


def test_crawl_all_no_sources_no_raise(fake_browser) -> None:
    """一个源都没配：不算故障，直接返回空。"""
    assert web.crawl_all(_cfg([])) == ([], [])


# ---------------------------------------------------------------------
# ④ 轮换池的两道上限
# ---------------------------------------------------------------------
def _seed_pushed(store: NewsStore, url: str, days_ago: float = 0.0,
                 rotate_count: int = 0) -> None:
    store.save(url, ZH_TITLE, "test",
               {"title_zh": ZH_TITLE, "summary": ZH_SUMMARY, "full_translation": ZH_BODY})
    store.mark_pushed(url)
    created = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    pushed = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    store._conn.execute(
        "UPDATE articles SET created_at = ?, pushed_at = ?, rotate_count = ? WHERE url = ?",
        (created, pushed, rotate_count, url),
    )
    store._conn.commit()


def test_rotate_excludes_articles_older_than_max_age(tmp_path) -> None:
    """入库 8 天的古董不进轮换池（默认上限 7 天）。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        _seed_pushed(store, "https://ex.com/old", days_ago=8)
        _seed_pushed(store, "https://ex.com/fresh", days_ago=1)

        picked = store.pick_rotate_many(4, cooldown_hours=4, max_age_days=7, max_times=2)
        assert [r["url"] for r in picked] == ["https://ex.com/fresh"]
    finally:
        store.close()


def test_rotate_excludes_articles_pushed_too_many_times(tmp_path) -> None:
    """同一篇已轮换 2 次后彻底退出轮换池。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        _seed_pushed(store, "https://ex.com/used", days_ago=1, rotate_count=2)
        _seed_pushed(store, "https://ex.com/ok", days_ago=1, rotate_count=1)

        picked = store.pick_rotate_many(4, cooldown_hours=4, max_age_days=7, max_times=2)
        assert [r["url"] for r in picked] == ["https://ex.com/ok"]
    finally:
        store.close()


def test_mark_rotated_increments_counter(tmp_path) -> None:
    """mark_rotated_many 才累加次数；mark_pushed_many 不累加（新文章首次推送）。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        _seed_pushed(store, "https://ex.com/new", days_ago=0)
        store.mark_pushed_many(["https://ex.com/new"])
        row = store._conn.execute(
            "SELECT rotate_count FROM articles WHERE url = ?", ("https://ex.com/new",)
        ).fetchone()
        assert row["rotate_count"] == 0, "新文章推送不该计入轮换次数"

        store.mark_rotated_many(["https://ex.com/new"])
        store.mark_rotated_many(["https://ex.com/new"])
        row = store._conn.execute(
            "SELECT rotate_count FROM articles WHERE url = ?", ("https://ex.com/new",)
        ).fetchone()
        assert row["rotate_count"] == 2
    finally:
        store.close()


def test_old_db_gets_rotate_count_column(tmp_path) -> None:
    """老库（无 rotate_count 列）启动时自动补列，不需要删库重来。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE articles (url TEXT PRIMARY KEY, canonical TEXT, title TEXT NOT NULL,"
        " source_name TEXT, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
        " created_at TEXT NOT NULL, pushed_at TEXT);"
    )
    conn.commit()
    conn.close()

    store = NewsStore(str(path))
    try:
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(articles)")}
        assert "rotate_count" in cols
    finally:
        store.close()


# ---------------------------------------------------------------------
# ③ 轮换卡片的告警
# ---------------------------------------------------------------------
def _record(url: str, title: str = ZH_TITLE) -> dict:
    return {"url": url, "title": title, "source_name": "test",
            "info": {"title_zh": title, "summary": ZH_SUMMARY,
                     "full_translation": ZH_BODY, "is_fallback": False}}


def test_card_without_notice_has_no_warning() -> None:
    card = build_multi_news_card([_record("https://ex.com/a")])
    text = json.dumps(card, ensure_ascii=False)
    assert "历史轮换" not in text
    assert "本轮无新文章入库" not in text


def test_card_with_notice_marks_subtitle_and_body() -> None:
    card = build_multi_news_card([_record("https://ex.com/a")], notice=runner.ROTATE_NOTICE)
    text = json.dumps(card, ensure_ascii=False)
    assert "历史轮换" in text, "subtitle 需要标记，卡片折叠时也看得见"
    assert "本轮无新文章入库" in text, "正文要有醒目红字"
    assert "color='red'" in text


def test_push_from_rotate_attaches_notice_and_counts(tmp_path, monkeypatch) -> None:
    """端到端：队列空 → 走轮换 → 卡片带告警 → rotate_count 累加。"""
    from news_ai_science.stages.push import feishu_sender

    captured: dict = {}

    def fake_card(self, card):
        captured["card"] = card
        return {"code": 0, "msg": "success"}

    monkeypatch.setattr(feishu_sender.FeishuSender, "send_card", fake_card)

    store = NewsStore(str(tmp_path / "t.db"))
    try:
        _seed_pushed(store, "https://ex.com/a", days_ago=1)
        cfg = {
            "logging": {"dir": str(tmp_path / "logs")},
            "push": {"cooldown_hours": 4, "per_run": 4,
                     "rotate_max_age_days": 7, "rotate_max_times": 2},
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake",
            "chat_name": "iDeal工作室",
        }
        result = runner.push(cfg, store, None)

        assert result["ok"] is True
        assert result["rotate"] is True, "本轮无新入库时必须标记为历史轮换"
        body = json.dumps(captured["card"], ensure_ascii=False)
        assert "本轮无新文章入库" in body

        row = store._conn.execute(
            "SELECT rotate_count FROM articles WHERE url = ?", ("https://ex.com/a",)
        ).fetchone()
        assert row["rotate_count"] == 1
    finally:
        store.close()


def test_push_new_article_has_no_rotation_notice(tmp_path, monkeypatch) -> None:
    """正常抓到新文章时不能误报「本轮无新文章」。"""
    from news_ai_science.stages.push import feishu_sender

    captured: dict = {}

    def fake_card(self, card):
        captured["card"] = card
        return {"code": 0, "msg": "success"}

    monkeypatch.setattr(feishu_sender.FeishuSender, "send_card", fake_card)

    store = NewsStore(str(tmp_path / "t.db"))
    try:
        cfg = {
            "logging": {"dir": str(tmp_path / "logs")},
            "push": {"cooldown_hours": 4, "per_run": 4},
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake",
            "chat_name": "iDeal工作室",
        }
        result = runner.push(cfg, store, [_record("https://ex.com/new")])

        assert result["ok"] is True
        assert result["rotate"] is False
        assert "本轮无新文章入库" not in json.dumps(captured["card"], ensure_ascii=False)
    finally:
        store.close()


def test_push_reports_no_record_when_rotate_pool_exhausted(tmp_path) -> None:
    """池子只剩超龄/超次数条目时返回 no_record，而不是硬推古董。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        _seed_pushed(store, "https://ex.com/old", days_ago=30)
        cfg = {
            "logging": {"dir": str(tmp_path / "logs")},
            "push": {"cooldown_hours": 4, "per_run": 4,
                     "rotate_max_age_days": 7, "rotate_max_times": 2},
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake",
        }
        assert runner.push(cfg, store, None) == {"ok": False, "reason": "no_record", "count": 0}
    finally:
        store.close()


# ---------------------------------------------------------------------
# ② main 的崩溃保护
# ---------------------------------------------------------------------
def test_main_returns_3_on_broken_db(tmp_path, monkeypatch) -> None:
    """库文件损坏时给出退出码 3，而不是抛 sqlite3.DatabaseError 裸崩。"""
    import news_ai_science.main as M

    bad = tmp_path / "bad.db"
    bad.write_text("this is definitely not a sqlite database", encoding="utf-8")
    cfg = {"logging": {"dir": str(tmp_path / "logs"), "level": "CRITICAL"},
           "storage": {"db_path": str(bad)}}

    monkeypatch.setattr(M, "load_config", lambda *a, **k: cfg)
    assert M.main(["--stats"]) == 3


def test_main_survives_unexpected_collect_error(tmp_path, monkeypatch) -> None:
    """采集阶段抛非 CrawlError 的异常也不许裸崩，推送照常进行。"""
    import news_ai_science.main as M

    cfg = {"logging": {"dir": str(tmp_path / "logs"), "level": "CRITICAL"},
           "storage": {"db_path": str(tmp_path / "ok.db")}}

    monkeypatch.setattr(M, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(M.runner, "build_client", lambda c: None)

    def boom(*a, **k):
        raise RuntimeError("模拟未预期异常")

    monkeypatch.setattr(M.runner, "collect", boom)
    monkeypatch.setattr(M.runner, "push",
                        lambda *a, **k: {"ok": True, "count": 0, "rotate": False})

    assert M.main(["--once"]) == 0
