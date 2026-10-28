# -*- coding: utf-8 -*-
"""push.card_builder：聚合图文卡的硬性约束（飞书 2.0）。"""
from __future__ import annotations

import json

from news_ai_science.stages.push.card_builder import (
    IMAGE_WEIGHT,
    MAX_CARD_ITEMS,
    TEXT_WEIGHT,
    build_multi_news_card,
)

TRANSLATION = "第一段译文。\n\n第二段译文。"


def _record(index: int = 1, image_key: str = "img_v3_test", **override) -> dict:
    info = {
        "title_zh": f"OpenAI 发布新一代推理模型 {index}",
        "summary": "OpenAI 发布新一代推理模型，主打长链推理与更低成本。",
        "full_translation": TRANSLATION,
        "summaries": ["要点一", "要点二"],
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


def test_title_and_summary_both_live_in_right_column():
    """左 column 放图，右 column 依次是标题、来源、摘要。"""
    card = build_multi_news_card([_record()])
    column_set = next(el for el in card["body"]["elements"] if el["tag"] == "column_set")
    left, right = column_set["columns"]

    assert left["elements"][0]["tag"] == "img"
    assert right["elements"][0]["tag"] == "markdown"
    assert "[OpenAI 发布新一代推理模型 1](https://techcrunch.com/2026/10/26/model-1/)" in (
        right["elements"][0]["content"]
    )
    # 摘要必须出现在右列，而不是折叠面板里
    right_text = " ".join(el.get("content", "") for el in right["elements"])
    assert "主打长链推理与更低成本" in right_text


def test_image_column_is_narrower_than_text_column():
    """窄屏适配：图片列权重必须小于文字列，否则移动端右列被压成竖条。"""
    card = build_multi_news_card([_record()])
    column_set = next(el for el in card["body"]["elements"] if el["tag"] == "column_set")
    assert column_set["flex_mode"] == "none"
    assert column_set["columns"][0]["weight"] == IMAGE_WEIGHT
    assert column_set["columns"][1]["weight"] == TEXT_WEIGHT
    assert IMAGE_WEIGHT < TEXT_WEIGHT


def test_panel_shows_full_translation_not_bullets():
    """折叠面板标题必须是「全文翻译」，内容必须是整段译文。"""
    card = build_multi_news_card([_record()])
    panel = next(el for el in card["body"]["elements"] if el["tag"] == "collapsible_panel")
    assert "全文翻译" in panel["header"]["title"]["content"]
    assert panel["elements"][0]["content"] == TRANSLATION
    assert "要点一" not in panel["elements"][0]["content"]


def test_panel_label_carries_char_count():
    card = build_multi_news_card([_record()])
    panel = next(el for el in card["body"]["elements"] if el["tag"] == "collapsible_panel")
    assert f"{len(TRANSLATION)} 字" in panel["header"]["title"]["content"]


def test_legacy_record_without_translation_degrades_to_bullets():
    """改造前入库的记录没有 full_translation，轮换推送时必须降级而不是发空内容。"""
    card = build_multi_news_card([_record(full_translation="", summary="")])
    panel = next(el for el in card["body"]["elements"] if el["tag"] == "collapsible_panel")
    assert "要点" in panel["header"]["title"]["content"]
    assert "- 要点一" in panel["elements"][0]["content"]


def test_no_content_at_all_renders_placeholder():
    """译文与要点都缺时给灰字占位，绝不下发空 markdown（会被飞书拒收）。"""
    card = build_multi_news_card(
        [_record(full_translation="", summary="", summaries=[])]
    )
    text = _dump(card)
    assert "collapsible_panel" not in text
    assert "译文生成失败" in text


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
