# -*- coding: utf-8 -*-
"""storage.db 与 pipeline.runner 的本地逻辑（不联网）。"""
from __future__ import annotations

import json
import sqlite3

from news_ai_science.pipeline import runner
from news_ai_science.storage.db import NewsStore


def test_save_dedupes_by_url(tmp_path):
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        assert store.save("u1", "标题", "TechCrunch AI", {"summaries": ["a"]}) is True
        assert store.save("u1", "标题", "TechCrunch AI", {}) is False
        assert store.is_seen("u1") is True
    finally:
        store.close()


def test_dedupe_ignores_tracking_query_and_scheme(tmp_path):
    """同一篇文章换入口地址（utm/协议/www/尾斜杠）必须判为已存在。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        url = "https://techcrunch.com/2026/10/26/openai-model/"
        assert store.save(url, "标题", "TechCrunch AI", {"summaries": ["a"]}) is True
        for variant in (
            url + "?utm_source=rss",
            "http://www.techcrunch.com/2026/10/26/openai-model",
            url.replace("https://", "https://www."),
        ):
            assert store.is_seen(variant) is True, variant
        assert store.is_seen("https://techcrunch.com/2026/10/26/other-model/") is False
    finally:
        store.close()


def test_dedupe_is_not_title_based(tmp_path):
    """不同 URL 的同一标题必须各自入库：标题去重会误杀多家媒体的报道。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        assert store.save("https://a.com/x", "同一条新闻", "A", {}) is True
        assert store.save("https://b.com/y", "同一条新闻", "B", {}) is True
        assert store.stats()["pending"] == 2
    finally:
        store.close()


def test_pending_many_and_mark_all(tmp_path):
    """一张聚合卡装多条，推送成功后必须每条都标 pushed，否则下一轮重复推。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        for i in range(1, 6):
            store.save(f"https://a.com/p{i}", f"标题{i}", "A", {"summaries": ["a"]})
        batch = store.pick_pending_many(4)
        assert len(batch) == 4
        store.mark_pushed_many([row["url"] for row in batch])
        assert store.stats() == {"pending": 1, "pushed": 4, "rejected": 0}
    finally:
        store.close()


def test_migrates_old_db_without_canonical(tmp_path):
    """老库（无 canonical 列）升级后仍能正确去重，不能把历史文章当新文章。"""
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE articles (
            url TEXT PRIMARY KEY, title TEXT NOT NULL, source_name TEXT,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, pushed_at TEXT
        );
        INSERT INTO articles (url, title, source_name, payload, status, created_at)
        VALUES ('https://techcrunch.com/2026/10/26/openai-model/', '旧闻', 'TC', '{}', 'pushed',
                '2026-10-20 08:00:00');
        """
    )
    conn.commit()
    conn.close()

    store = NewsStore(db_path)
    try:
        assert store.is_seen("https://techcrunch.com/2026/10/26/openai-model/?utm_source=rss") is True
        assert store.stats()["pushed"] == 1
    finally:
        store.close()


def test_pending_before_rotate(tmp_path):
    """--push-only 时队列有货就必须先推队列，不能报「没有可推的新闻」。"""
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        store.save("u1", "标题", "src", {"summaries": ["a"]})
        assert store.pick_pending()["url"] == "u1"
        store.mark_pushed("u1")
        assert store.pick_pending() is None
        assert store.pick_rotate(4) is None, "冷却期内不应轮换"
        assert store.pick_rotate(0)["url"] == "u1"
    finally:
        store.close()


def test_push_failure_does_not_fall_back_to_text(tmp_path, monkeypatch):
    """卡片发不出去时只能保持 pending，绝不允许再补发一条纯文本（会像重复推送）。"""
    from news_ai_science.stages.push import feishu_sender

    calls = {"card": 0, "text": 0}

    def fake_card(self, card):
        calls["card"] += 1
        return {"code": 200861, "msg": "unsupported tag"}

    def fake_text(self, content):
        calls["text"] += 1
        return {"code": 0, "msg": "success"}

    monkeypatch.setattr(feishu_sender.FeishuSender, "send_card", fake_card)
    monkeypatch.setattr(feishu_sender.FeishuSender, "send_text", fake_text)

    store = NewsStore(str(tmp_path / "t.db"))
    try:
        store.save("https://a.com/x", "标题", "A", {"summaries": ["a"]})
        cfg = {
            "logging": {"dir": str(tmp_path / "logs")},
            "push": {"cooldown_hours": 4, "per_run": 4},
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake",
            "chat_name": "iDeal工作室",
        }
        result = runner.push(cfg, store, None)
        assert result["ok"] is False
        assert calls == {"card": 1, "text": 0}, "失败后不许发纯文本"
        assert store.stats()["pending"] == 1, "失败的条目必须留待下一轮重发"
    finally:
        store.close()


def test_push_dry_run_saves_card_but_not_state(tmp_path, capsys):
    store = NewsStore(str(tmp_path / "t.db"))
    try:
        store.save(
            "u1",
            "OpenAI 新模型",
            "TechCrunch AI",
            {"title_zh": "OpenAI 新模型", "summaries": ["a", "b", "c"], "image_key": ""},
        )
        out_dir = tmp_path / "logs"
        cfg = {"logging": {"dir": str(out_dir)}, "push": {"cooldown_hours": 4}}
        result = runner.push(cfg, store, None, dry_run=True)

        assert result["ok"] is True
        assert list(out_dir.glob("card_dryrun_news_*.json"))
        assert json.loads(capsys.readouterr().out)["header"]["template"] == "green"
        assert store.stats()["pushed"] == 0
    finally:
        store.close()
