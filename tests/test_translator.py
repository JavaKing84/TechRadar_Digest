# -*- coding: utf-8 -*-
"""generate.translator：摘要 + 全文翻译解析的硬约束。"""
from __future__ import annotations

from news_ai_science.stages.generate.translator import (
    SUMMARY_CHARS,
    TRANSLATION_MAX_CHARS,
    parse_news_json,
)


def test_parse_new_format():
    raw = (
        '{"translated_title":"OpenAI 发布新模型",'
        '"summary":"OpenAI 发布新一代推理模型，主打长链推理能力。",'
        '"full_translation":"第一段译文。\\n\\n第二段译文。"}'
    )
    data = parse_news_json(raw)
    assert data["translated_title"] == "OpenAI 发布新模型"
    assert data["summary"] == "OpenAI 发布新一代推理模型，主打长链推理能力。"
    assert data["full_translation"] == "第一段译文。\n\n第二段译文。"


def test_strips_markdown_fence():
    raw = '```json\n{"translated_title":"T","summary":"s","full_translation":"t"}\n```'
    assert parse_news_json(raw)["translated_title"] == "T"


def test_raw_newline_inside_string_is_tolerated():
    """模型直接在 JSON 字符串里写裸换行是长译文最常见的坏格式。

    JSON 规范禁止字符串内出现控制字符，严格模式会判非法；这里用
    json.loads(strict=False) 放宽，避免整条翻译因为排版习惯被丢弃。
    """
    raw = '{"translated_title":"T","summary":"s","full_translation":"第一段\n第二段"}'
    data = parse_news_json(raw)
    assert data is not None
    assert "第一段" in data["full_translation"]


def test_summary_truncated_to_limit():
    long_text = "字" * (SUMMARY_CHARS + 60)
    data = parse_news_json(
        f'{{"translated_title":"T","summary":"{long_text}","full_translation":"t"}}'
    )
    assert len(data["summary"]) <= SUMMARY_CHARS + 1


def test_summary_cut_at_sentence_boundary():
    """超长摘要应优先在句号处断开，而不是切出半句话。"""
    sentence = "这是一句完整的话。"          # 9 字
    long_text = sentence * 20              # 180 字，共 20 句
    data = parse_news_json(
        f'{{"translated_title":"T","summary":"{long_text}","full_translation":"t"}}'
    )
    assert len(data["summary"]) <= SUMMARY_CHARS
    assert data["summary"].endswith("。")


def test_summary_strips_list_marker_and_newlines():
    data = parse_news_json(
        '{"translated_title":"T","summary":"- 第一行\\n第二行","full_translation":"t"}'
    )
    assert not data["summary"].startswith("-")
    assert "\n" not in data["summary"]


def test_translation_truncated():
    long_text = "字" * (TRANSLATION_MAX_CHARS + 500)
    data = parse_news_json(
        f'{{"translated_title":"T","summary":"s","full_translation":"{long_text}"}}'
    )
    assert len(data["full_translation"]) <= TRANSLATION_MAX_CHARS + 1


def test_blank_lines_collapsed():
    data = parse_news_json(
        '{"translated_title":"T","summary":"s","full_translation":"A\\n\\n\\n\\nB"}'
    )
    assert data["full_translation"] == "A\n\nB"


def test_legacy_summaries_still_accepted():
    """改造前入库的旧格式只有 summaries，仍要能解析（轮换推送时会复用）。"""
    data = parse_news_json(
        '{"translated_title":"T","summaries":["- 要点","1. 第二条"]}'
    )
    assert data["summaries"] == ["要点", "第二条"]
    assert data["summary"] == "要点"          # 降级：第一条当摘要
    assert data["full_translation"] == ""     # 无译文，卡片层再降级


def test_garbage_returns_none():
    assert parse_news_json("模型表示无法回答") is None
    assert parse_news_json('{"translated_title":"T"}') is None
