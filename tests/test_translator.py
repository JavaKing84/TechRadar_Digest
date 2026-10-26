# -*- coding: utf-8 -*-
"""generate.translator：摘要解析的硬约束（条数、字数、去列表符）。"""
from __future__ import annotations

from news_ai_science.stages.generate.translator import SUMMARY_CHARS, parse_news_json


def test_parse_plain_json():
    raw = '{"translated_title":"OpenAI 发布新模型","summaries":["要点1","要点2","要点3"]}'
    title, summaries = parse_news_json(raw)
    assert title == "OpenAI 发布新模型"
    assert summaries == ["要点1", "要点2", "要点3"]


def test_strips_markdown_fence():
    raw = '```json\n{"translated_title":"T","summaries":["a","b","c"]}\n```'
    assert parse_news_json(raw)[0] == "T"


def test_list_markers_removed():
    """模型爱在摘要前加「- 」或「1. 」，卡片里会和 markdown 列表叠加成双符号。"""
    title, summaries = parse_news_json('{"translated_title":"T","summaries":["- 要点","1. 第二条","• 第三条"]}')
    assert summaries == ["要点", "第二条", "第三条"]


def test_summaries_capped_at_ten():
    items = ",".join(f'"s{i}"' for i in range(15))
    assert len(parse_news_json('{"translated_title":"T","summaries":[' + items + "]}")[1]) == 10


def test_long_summary_truncated():
    long_text = "字" * (SUMMARY_CHARS + 40)
    assert len(parse_news_json(f'{{"translated_title":"T","summaries":["{long_text}"]}}')[1][0]) <= SUMMARY_CHARS + 1


def test_garbage_returns_none():
    assert parse_news_json("模型表示无法回答") is None
    assert parse_news_json('{"translated_title":"T","summaries":[]}') is None
