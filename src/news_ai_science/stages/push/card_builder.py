# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/card_builder.py
[职责] 把「多条新闻」组装成一张飞书 2.0 interactive 卡片 JSON。纯函数，不发请求。
      版式对齐 Perplexity Discover：一张大卡内纵向排列 N 条，每条是
        左图 + 右文（标题 / 来源 / 100 字摘要）
        -> 折叠面板「展开查看全文翻译」
        -> 点击查看原文

[多端一致性] 飞书移动端的卡片内容区宽度约为桌面端的一半，column_set 的
      加权列在窄屏上会被等比压缩：图片占比一旦过大，右列文字就会被压成
      一行几个字。因此图片列权重固定为 1、文字列 3（≈25% : 75%），并显式
      写 flex_mode: "none" 锁定「不换行、按权重分配」的行为。
      注意：只使用飞书 2.0 schema 明确支持的字段，任何未文档化的属性都会
      导致整卡被拒（ErrCode 200861），而不是被忽略。
=====================================================================
"""
from __future__ import annotations

from datetime import datetime

# [表面] 卡片头模板色
# [本质] 新闻卡用绿色，岗位卡用蓝色，群里一眼分清两个机器人
TEMPLATE = "green"

# [表面] 一张卡最多装几条、折叠面板里降级时最多露几条要点
# [本质] 飞书客户端对单卡元素数/渲染量有上限，超过 5 条图文会明显卡顿甚至拒收
MAX_CARD_ITEMS = 5
MAX_SUMMARIES_SHOWN = 5

# [表面] 左图右文的列宽权重（1 : 3 ≈ 25% : 75%）
# [本质] 早期用 2 : 3（图占 40%），桌面端好看，但移动端把右列挤到只剩
#        两三个字一行，标题被折成四五行的"竖条"。图片缩到 25% 后，
#        窄屏上右列仍有足够宽度容纳 100 字摘要（约 4~6 行）
IMAGE_WEIGHT = 1
TEXT_WEIGHT = 3


def _link_text(text: str) -> str:
    """
    # [表面] 把标题里的英文方括号换成全角
    # [本质] markdown 的 [文字](链接) 语法里，文字部分的 [ ] 会被当成嵌套链接的
    #        起止，导致整段链接渲染失败甚至显示原文
    # [后果] 标题里带 [ ] 也能正常渲染成可点击链接
    """
    return (text or "").replace("[", "【").replace("]", "】")


def _header(title: str, subtitle: str, template: str = TEMPLATE) -> dict:
    return {
        "title": {"tag": "plain_text", "content": title},
        "subtitle": {"tag": "plain_text", "content": subtitle},
        "template": template,
    }


def _collapsible_panel(content: str, label: str) -> dict:
    """
    # [表面] 一个默认收起的折叠面板
    # [本质] 飞书卡片 2.0 的 collapsible_panel：译文藏在面板里，点击才展开。
    #        只保留官方 schema 认可的字段（tag/expanded/header/elements 及少量排版
    #        字段）；background_style / max_height / collapse_action 这些属性
    #        飞书侧会直接判 unknown property 整卡拒收
    # [后果] 卡片能通过校验，一条新闻在收起态只占「图 + 标题 + 摘要」的高度
    """
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "plain_text", "content": label},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_icon",
                "color": "grey",
            },
        },
        "border": {"color": "grey", "corner_radius": "8px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [{"tag": "markdown", "content": content}],
    }


def _right_column_blocks(record: dict) -> list[dict]:
    """
    # [表面] 图片右侧的竖排内容：中文标题（可点击）→ 来源 → 摘要
    # [本质] 这三段是"右列"的完整语义单元。图片只负责视觉锚点，
    #        信息全部落在文字列，因此窄屏下图变小也不会丢失关键信息
    # [后果] 桌面端是「图 + 三行文字」，移动端是「小图 + 可读的标题与摘要」
    """
    info = record.get("info") or {}
    url = str(record.get("url") or "")
    title = str(info.get("title_zh") or record.get("title") or "未命名新闻")
    source_name = str(record.get("source_name") or "")
    summary = str(info.get("summary") or "").strip()

    blocks: list[dict] = [{"tag": "markdown", "content": f"**[{_link_text(title)}]({url})**"}]
    if source_name:
        blocks.append(
            {"tag": "markdown", "content": f"<font color='grey'>来源：{source_name}</font>"}
        )
    if summary:
        # [表面] 摘要用正文样式，不加粗、不缩进
        # [本质] 加粗会让 100 字的段落变成"标题"，与真正的标题抢层级；
        #        前缀用细体符号做视觉分隔，不额外占一行
        # [后果] 收起态一眼能读完要点，与标题区分明确
        blocks.append({"tag": "markdown", "content": f"<font color='grey'>摘要</font>　{summary}"})
    return blocks


def _translation_panel(info: dict) -> dict:
    """
    # [表面] 折叠面板：有译文放全文翻译，没译文降级为要点列表
    # [本质] 旧库记录（改造前入库的 payload）只有 summaries，没有 full_translation。
    #        轮换推送时会再次上卡，若直接取空字符串，飞书会因 markdown 内容为空
    #        而整卡校验失败。这里按「译文 → 要点 → 灰字占位」三级降级
    # [后果] 新旧数据都能上卡，最差情况也不会因为空内容被拒
    """
    translation = str(info.get("full_translation") or "").strip()
    if translation:
        label = f"展开查看全文翻译（{len(translation)} 字）👇"
        return _collapsible_panel(translation, label=label)

    summaries = [str(s).strip() for s in (info.get("summaries") or []) if str(s).strip()]
    if summaries:
        shown = summaries[:MAX_SUMMARIES_SHOWN]
        body = "\n".join(f"- {text}" for text in shown)
        label = f"展开查看要点（{len(summaries)} 条）👇"
        return _collapsible_panel(body, label=label)

    return {"tag": "markdown", "content": "<font color='grey'>（本条译文生成失败，详情请点开原文）</font>"}


def _item_blocks(record: dict) -> list[dict]:
    """
    # [表面] 一条新闻的完整版式：左图右文 + 折叠译文 + 查看原文
    # [本质] 没有 img_key 时不能下发 img 组件（空 img_key 会被飞书拒收），
    #        此时退化为单列铺满，标题/来源/摘要直接纵向排列
    # [后果] 有图是图文流，没图是纯文字流，两种都完整可读
    """
    info = record.get("info") or {}
    url = str(record.get("url") or "")
    image_key = str(info.get("image_key") or "")
    right_blocks = _right_column_blocks(record)

    blocks: list[dict] = []
    if image_key:
        blocks.append(
            {
                "tag": "column_set",
                # 锁定"不换行、按 weight 分宽"，避免移动端把两列折叠成上下堆叠
                "flex_mode": "none",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": IMAGE_WEIGHT,
                        "vertical_align": "top",
                        "elements": [
                            {
                                "tag": "img",
                                "img_key": image_key,
                                "alt": {"tag": "plain_text", "content": "新闻配图"},
                                "preview": True,
                            }
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": TEXT_WEIGHT,
                        "vertical_align": "top",
                        "elements": right_blocks,
                    },
                ],
            }
        )
    else:
        blocks.extend(right_blocks)

    blocks.append(_translation_panel(info))
    blocks.append({"tag": "markdown", "content": f"[📖 点击查看原文]({url})"})
    return blocks


def build_multi_news_card(records: list[dict], headline: str = "📰 AI 科技头条") -> dict:
    """
    # [表面] 一张聚合卡：按顺序排 N 条新闻
    # [本质] 一次推送只发一条消息。发 N 张独立卡会刷屏，且群里消息一多，
    #        用户根本划不到第二条；聚合卡在视觉上就是「一屏资讯流」
    # [后果] 每次推送 = 1 条消息，含 1~5 条新闻，滚动自然
    """
    items = [rec for rec in (records or []) if rec][:MAX_CARD_ITEMS]
    count = len(items)

    # [表面] 正文第一行直接是第一条新闻
    # [本质] 时间与条数已经在 header 的 subtitle 里，正文再写一遍就是同一句话印两次
    elements: list[dict] = []

    for index, record in enumerate(items):
        if index:
            elements.append({"tag": "hr"})
        elements.extend(_item_blocks(record))

    elements.append({"tag": "hr"})
    # [表面] 页脚用小号灰字
    # [本质] 卡片 2.0 **不支持 note 组件**（实测报 unsupported tag note，
    #        ErrCode 200861），整卡会被拒；markdown 是 2.0 里唯一稳妥的落款方式
    elements.append(
        {"tag": "markdown", "content": "<font color='grey'>🤖 由 AI 自动筛选、全文翻译并生成摘要</font>"}
    )

    subtitle = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} · 今日 {count} 条"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": _header(title=headline, subtitle=subtitle),
        "body": {"elements": elements},
    }
