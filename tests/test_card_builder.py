# -*- coding: utf-8 -*-
"""push.card_builder：聚合图文卡的硬性约束（飞书 2.0）。"""
from __future__ import annotations

import json

from news_ai_science.stages.push.card_builder import (
    MAX_CARD_ITEMS,
    build_multi_news_card,
)


def _record(index: int = 1, image_key: str = "img_v3_test", **override) -> dict:
    info = {
        "title_zh": f"OpenAI 发布新一代推理模型 {index}",
        "summaries": ["要点一", "要点二", "要点三"],
        "image_key": image_key,
    }
    info.update(override)
    return {
        "url": f"https://techcrunch.com/2026/10/26/model-{index}/",
        "title": "raw title",
        "source_name": "TechCrunch AI",
        "info": info,
    }


def _dump(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False)


def test_schema_is_v2_and_has_all_blocks():
    text = _dump(build_multi_news_card([_record()]))
    assert '"schema": "2.0"' in text
    assert '"tag": "column_set"' in text
    assert '"tag": "img"' in text
    assert "collapsible_panel" in text
    assert "点击查看原文" in text


def test_title_lives_in_right_column_beside_image():
    """Perplexity 范式：左 column 放图，右 column 放可点击中文标题。"""
    card = build_multi_news_card([_record()])
    column_set = next(el for el in card["body"]["elements"] if el["tag"] == "column_set")
    left, right = column_set["columns"]
    assert left["elements"][0]["tag"] == "img"
    assert right["elements"][0]["tag"] == "markdown"
    assert "[OpenAI 发布新一代推理模型 1](https://techcrunch.com/2026/10/26/model-1/)" in (
        right["elements"][0]["content"]
    )


def test_one_card_holds_many_news():
    card = build_multi_news_card([_record(i) for i in range(1, 5)])
    blocks = [el["tag"] for el in card["body"]["elements"]]
    assert blocks.count("column_set") == 4
    assert blocks.count("collapsible_panel") == 4
    assert blocks.count("hr") == 4  # 条目之间 3 个 + 结尾 1 个
    assert "今日 4 条" in card["header"]["subtitle"]["content"]


def test_items_capped_at_max_card_items():
    card = build_multi_news_card([_record(i) for i in range(1, 10)])
    assert [el["tag"] for el in card["body"]["elements"]].count("column_set") == MAX_CARD_ITEMS


def test_no_image_key_falls_back_to_stacked_layout():
    """没有 img_key 时不下发 img 组件（空 img_key 会被飞书拒收）。"""
    card = build_multi_news_card([_record(image_key="")])
    text = _dump(card)
    assert '"tag": "img"' not in text
    assert '"tag": "column_set"' not in text
    assert "OpenAI 发布新一代推理模型 1" in text


def test_summaries_capped_and_rendered_as_list():
    card = build_multi_news_card([_record(summaries=[f"要点{i}" for i in range(20)])])
    panel = next(el for el in card["body"]["elements"] if el["tag"] == "collapsible_panel")
    lines = panel["elements"][0]["content"].split("\n")
    assert len(lines) == 5
    assert "20 条" in panel["header"]["title"]["content"]


def test_fallback_text_builder_is_gone():
    """纯文本兜底已废弃：只允许 interactive 卡片进群。"""
    import news_ai_science.stages.push.card_builder as module

    assert not hasattr(module, "build_fallback_text")


def test_no_duplicate_timestamp_in_body():
    """时间/条数只在 header 出现一次；正文第一行必须是内容本身。"""
    card = build_multi_news_card([_record(1), _record(2)])
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "column_set"
    assert "今日精选" not in json.dumps(elements, ensure_ascii=False)
    assert "今日 2 条" in card["header"]["subtitle"]["content"]


def test_no_note_component():
    """卡片 2.0 不支持 note（实测 ErrCode 200861 整卡被拒），页脚必须用 markdown。"""
    text = _dump(build_multi_news_card([_record()]))
    assert '"tag": "note"' not in text
