# -*- coding: utf-8 -*-
"""翻译层的中文硬约束：模型没翻译 / 翻译失败时，宁可丢弃也绝不推英文。

线上故障（推送内容纯英文）的直接原因有两个，这里逐条钉住：
1. 旧版兜底把外文原文当译文返回，模型一挂就整批英文进群；
2. 模型返回的是合法 JSON，但内容是英文照抄，旧代码没有任何中文校验。
"""
from __future__ import annotations

import json

import pytest

from news_ai_science.contracts.models import Article
from news_ai_science.core.errors import LLMError
from news_ai_science.core.text import cjk_stats, is_chinese_text
from news_ai_science.pipeline import runner
from news_ai_science.stages.generate import translator as tr
from news_ai_science.stages.generate.translator import (
    chinese_problems,
    payload_problems,
    translate,
)
from news_ai_science.storage.db import NewsStore

EN_TITLE = "Google unveils Gemini 4 Carbon, retaking the world's No.1 spot"
EN_SUMMARY = (
    "Google has unveiled Gemini 4 Carbon, which retakes the top spot on the "
    "updated and harder benchmark suite, but access remains limited for now."
)
EN_BODY = (
    "Google announced Gemini 4 Carbon on Thursday, saying the model retakes the "
    "top spot on a harder benchmark suite. Access is limited to a small group of "
    "enterprise customers for the first month, the company said. "
) * 3

EN_JSON = json.dumps(
    {
        "translated_title": EN_TITLE,
        "summary": EN_SUMMARY,
        "full_translation": "Paragraph one is still English.\n\nParagraph two as well.",
    },
    ensure_ascii=False,
)

ZH_JSON = json.dumps(
    {
        "translated_title": "谷歌发布 Gemini 4 Carbon，重回世界第一",
        "summary": "谷歌发布新一代模型 Gemini 4 Carbon，在更难的新基准上重回第一，首月仅向部分企业客户开放。",
        "full_translation": "谷歌于周四发布 Gemini 4 Carbon，称该模型在难度更高的基准测试中重回第一。\n\n"
                           "公司表示，首月仅向少数企业客户开放访问权限。",
    },
    ensure_ascii=False,
)


class FakeClient:
    """按顺序回放的假客户端：字符串当正常回复，异常实例直接抛出。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, **kwargs):  # noqa: ARG002 - 接口对齐
        self.calls += 1
        index = min(self.calls - 1, len(self.replies) - 1)
        reply = self.replies[index]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _article(lang: str = "en", index: int = 1) -> Article:
    if lang == "en":
        return Article(
            source="venturebeat",
            source_name="VentureBeat",
            title=f"{EN_TITLE} ({index})",
            url=f"https://venturebeat.com/ai/gemini-{index}",
            body=EN_BODY,
            cover_image="https://cdn.example.com/a.jpg",
        )
    return Article(
        source="36kr",
        source_name="36氪",
        title=f"国产大模型发布新版本（{index}）",
        url=f"https://36kr.com/p/{index}",
        body="国内厂商今天发布了新一代大模型，参数规模与上一代持平，推理成本下降约四成。" * 5,
        cover_image="https://cdn.example.com/b.jpg",
    )


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """把退避等待压成 0，避免测试因重试机制空转十几秒。"""
    monkeypatch.setattr(tr, "RETRY_BACKOFF", 0)


# ---------------------------------------------------------------- 文本判定
def test_cjk_stats_and_chinese_detection():
    cjk, latin, ratio = cjk_stats("谷歌发布 Gemini 4 Carbon")
    assert cjk == 4 and latin > 0 and 0 < ratio < 1
    assert cjk_stats("English only text here")[0] == 0
    assert cjk_stats("") == (0, 0, 0.0)

    assert is_chinese_text("谷歌发布 Gemini 4 Carbon 重回第一") is True
    assert is_chinese_text(EN_TITLE) is False
    assert is_chinese_text("") is False


def test_chinese_problems_flags_english_fields():
    assert chinese_problems("中文标题", "这是一段足够长的中文摘要内容说明发生了什么。") == []
    assert "标题不是中文" in chinese_problems(EN_TITLE)
    assert "标题为空" in chinese_problems("")
    assert "摘要不是中文" in chinese_problems("中文标题", EN_SUMMARY)


# ---------------------------------------------------------------- translate
def test_english_json_output_is_retried_then_rejected():
    """模型把英文原样抄回来（合法 JSON）也必须判失败，最终丢弃而不是推英文。"""
    client = FakeClient([EN_JSON])
    info = translate(_article("en"), client)

    assert info["rejected"] is True
    assert info["title_zh"] == "" and info["summary"] == ""
    assert client.calls > 1, "内容非中文必须重试，不能一次就认"


def test_english_after_fix_is_accepted():
    """第一次吐英文、纠正后给中文 —— 应当接受，且不浪费更多调用。"""
    client = FakeClient([EN_JSON, ZH_JSON])
    info = translate(_article("en"), client)

    assert info["rejected"] is False
    assert info["title_zh"].startswith("谷歌发布")
    assert client.calls == 2


def test_llm_failure_on_foreign_article_is_dropped():
    """模型彻底不可用 + 外文原文 = 没有可推的中文内容 -> 丢弃（旧版的 bug 源头）。"""
    client = FakeClient([LLMError("额度耗尽"), LLMError("额度耗尽"), LLMError("额度耗尽"),
                         LLMError("额度耗尽"), LLMError("额度耗尽"), LLMError("额度耗尽")])
    info = translate(_article("en"), client)

    assert info["rejected"] is True
    assert info["transient"] is True, "环境故障要允许下轮重试，不能永久拉黑"
    assert "LLM" in info["reason"] or "额度" in info["reason"]


def test_llm_failure_on_chinese_article_degrades_to_source():
    """中文源不受影响：模型挂了推中文原文节选，is_fallback 标记可查。"""
    info = translate(_article("zh"), None)

    assert info["rejected"] is False
    assert info["is_fallback"] is True
    assert info["title_zh"].startswith("国产大模型")
    assert payload_problems({"title_zh": info["title_zh"],
                             "summary": info["summary"],
                             "full_translation": info["full_translation"]}) == []


def test_no_client_on_foreign_article_is_dropped():
    """服务器漏配 LLM 凭证是最容易整批推英文的场景，必须整条丢弃。"""
    assert translate(_article("en"), None)["rejected"] is True


# ---------------------------------------------------------------- payload 体检
def test_payload_problems_accepts_legacy_short_records():
    """旧库记录只有很短的中文要点，不能因为"太短判不准"被误杀。"""
    assert payload_problems({"title_zh": "标题", "summaries": ["a"]}) == []
    assert payload_problems({"title_zh": "OpenAI 新模型", "summaries": ["a", "b", "c"]}) == []


def test_payload_problems_rejects_english_payload():
    problems = payload_problems({"title_zh": EN_TITLE, "summary": EN_SUMMARY})
    assert "标题不是中文" in problems


# ---------------------------------------------------------------- 流水线
def test_collect_discards_and_backfills(tmp_path, monkeypatch):
    """第一条翻译失败必须丢弃并让后一条补位，不能拿英文凑数。"""
    store = NewsStore(str(tmp_path / "t.db"))
    arts = [_article("en", 1), _article("zh", 2)]

    monkeypatch.setattr(runner, "crawl_all", lambda cfg, seen: (arts, []))
    monkeypatch.setattr(
        runner,
        "translate",
        lambda art, client: (
            {"rejected": True, "reason": "外文翻译失败", "title_zh": "", "summary": "",
             "full_translation": "", "summaries": [], "is_fallback": False}
            if art.url.endswith("/1")
            else {"rejected": False, "title_zh": art.title, "summary": "国内厂商发布新一代大模型，推理成本下降约四成。",
                  "full_translation": "国内厂商今天发布了新一代大模型。" * 3,
                  "summaries": [], "is_fallback": False}
        ),
    )
    cfg = {"crawl": {"require_image": False}, "push": {"per_run": 2},
           "logging": {"dir": str(tmp_path / "logs")}}
    try:
        records = runner.collect(cfg, store, client=None)

        assert [r["url"] for r in records] == ["https://36kr.com/p/2"]
        assert store.is_seen("https://venturebeat.com/ai/gemini-1") is True, "丢弃的条目也要记库防重抓"
        assert store.stats() == {"pending": 1, "pushed": 0, "rejected": 1}
    finally:
        store.close()


def test_collect_does_not_quarantine_on_llm_outage(tmp_path, monkeypatch):
    """LLM 整体不可用是环境故障：本轮不推，但不能记成 rejected，否则恢复后也补不回来。"""
    store = NewsStore(str(tmp_path / "t.db"))
    arts = [_article("en", 1)]
    monkeypatch.setattr(runner, "crawl_all", lambda cfg, seen: (arts, []))
    monkeypatch.setattr(
        runner,
        "translate",
        lambda art, client: {"rejected": True, "transient": True, "reason": "LLM 连续失败",
                             "title_zh": "", "summary": "", "full_translation": "",
                             "summaries": [], "is_fallback": False},
    )
    cfg = {"crawl": {"require_image": False}, "push": {"per_run": 2},
           "logging": {"dir": str(tmp_path / "logs")}}
    try:
        assert runner.collect(cfg, store, client=None) == []
        assert store.is_seen(arts[0].url) is False, "环境故障不该把文章永久拉黑"
        assert store.stats() == {"pending": 0, "pushed": 0, "rejected": 0}
    finally:
        store.close()


def test_push_blocks_english_legacy_records(tmp_path, monkeypatch):
    """库里躺着旧版兜底写入的英文条目：轮换到它时也必须拦下，不能上卡。"""
    from news_ai_science.stages.push import feishu_sender

    calls = {"card": 0}

    def fake_card(self, card):
        calls["card"] += 1
        return {"code": 0, "msg": "success"}

    monkeypatch.setattr(feishu_sender.FeishuSender, "send_card", fake_card)

    store = NewsStore(str(tmp_path / "t.db"))
    try:
        store.save(
            "https://www.technologyreview.com/2026/10/20/english-piece/",
            EN_TITLE,
            "MIT Technology Review",
            {"title_zh": EN_TITLE, "summary": EN_SUMMARY,
             "full_translation": "English paragraph one.\n\nEnglish paragraph two."},
        )
        cfg = {"logging": {"dir": str(tmp_path / "logs")},
               "push": {"cooldown_hours": 4, "per_run": 4},
               "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fake",
               "chat_name": "iDeal工作室"}
        result = runner.push(cfg, store, None)

        assert result == {"ok": False, "reason": "all_rejected", "count": 0}
        assert calls["card"] == 0, "英文条目一条都不许发出去"
        assert store.stats()["rejected"] == 1
    finally:
        store.close()
