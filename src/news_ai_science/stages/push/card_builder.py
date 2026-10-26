# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/card_builder.py
[职责] 把「多条新闻」组装成一张飞书 2.0 interactive 卡片 JSON。纯函数，不发请求。
      版式对齐 Perplexity Discover：一张大卡内纵向排列 N 条，每条是
        左图右文（column_set） -> 可点击中文标题 -> 折叠要点 -> 点击查看原文。
=====================================================================
"""
from __future__ import annotations

from datetime import datetime

# [表面] 卡片头模板色
# [本质] 新闻卡用绿色，岗位卡用蓝色，群里一眼分清两个机器人
TEMPLATE = "green"

# [表面] 一张卡最多装几条、每条最多露几条要点
# [本质] 飞书客户端对单卡元素数/渲染量有上限，超过 5 条图文会明显卡顿甚至拒收；
#        摘要默认收起，露出条数控制在 5 条，展开后仍能看全（折叠面板里给全量）
# [后果] 4~5 条图文流滚动顺滑，不会因为「一屏塞十篇」而被客户端拖死
MAX_CARD_ITEMS = 5
MAX_SUMMARIES_SHOWN = 5


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
    # [本质] 飞书卡片 2.0 的 collapsible_panel：摘要藏在面板里，点击才展开。
    #        只保留官方 schema 认可的字段（tag/expanded/header/elements 及少量排版
    #        字段）；background_style / max_height / collapse_action 这些属性
    #        飞书侧会直接判 unknown property 整卡拒收
    # [后果] 卡片能通过校验，一条新闻只占「标题 + 图」的固定行高
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


def _title_block(record: dict) -> list[dict]:
    """中文标题（可点击）+ 来源。图挂了也读得到「今天讲了什么」。"""
    info = record.get("info") or {}
    url = str(record.get("url") or "")
    title = str(info.get("title_zh") or record.get("title") or "未命名新闻")
    source_name = str(record.get("source_name") or "")

    blocks = [{"tag": "markdown", "content": f"**[{_link_text(title)}]({url})**"}]
    if source_name:
        blocks.append(
            {"tag": "markdown", "content": f"<font color='grey'>来源：{source_name}</font>"}
        )
    return blocks


def _item_blocks(record: dict) -> list[dict]:
    """
    # [表面] 一条新闻的完整版式：左图右文 + 折叠要点 + 查看原文
    # [本质] Perplexity Discover 的密度靠 column_set 实现——图占左侧固定比例，
    #        右侧放标题与来源，纵向空间省一半；没有 img_key 时退化为上下结构
    #        （单列 column_set 在移动端会留白，不如直接铺满）
    # [后果] 有图是图文流，没图是纯文字流，两种都完整可读
    """
    info = record.get("info") or {}
    url = str(record.get("url") or "")
    image_key = str(info.get("image_key") or "")
    summaries = [str(s).strip() for s in (info.get("summaries") or []) if str(s).strip()]

    blocks: list[dict] = []
    title_blocks = _title_block(record)

    if image_key:
        blocks.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 2,
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
                        "weight": 3,
                        "vertical_align": "top",
                        "elements": title_blocks,
                    },
                ],
            }
        )
    else:
        blocks.extend(title_blocks)

    if summaries:
        shown = summaries[:MAX_SUMMARIES_SHOWN]
        body = "\n".join(f"- {text}" for text in shown)
        label = f"展开查看要点（{len(summaries)} 条）👇"
        blocks.append(_collapsible_panel(body, label=label))
    else:
        blocks.append(
            {"tag": "markdown", "content": "<font color='grey'>（本条暂无摘要，详情请点开原文）</font>"}
        )

    blocks.append(
        {"tag": "markdown", "content": f"[📖 点击查看原文]({url})"}
    )
    return blocks


def build_multi_news_card(records: list[dict], headline: str = "📰 AI 科技头条") -> dict:
    """
    # [表面] 一张聚合卡：按顺序排 N 条新闻（每条左图右文 + 折叠要点）
    # [本质] 一次推送只发一条消息。发 N 张独立卡会刷屏，且群里消息一多，
    #        用户根本划不到第二条；聚合卡在视觉上就是「一屏资讯流」
    # [后果] 每次推送 = 1 条消息，含 1~5 条新闻，滚动自然
    """
    items = [rec for rec in (records or []) if rec][:MAX_CARD_ITEMS]
    count = len(items)

    # [表面] 正文第一行不再写时间/条数
    # [本质] 时间与条数已经在 header 的 subtitle 里（绿色栏那一行），
    #        正文再写一遍就是同一句话印两次，白占一屏第一行
    # [后果] 卡片打开第一眼就是第一条新闻的图与标题
    elements: list[dict] = []

    for index, record in enumerate(items):
        if index:
            elements.append({"tag": "hr"})
        elements.extend(_item_blocks(record))

    elements.append({"tag": "hr"})
    # [表面] 页脚用小号灰字
    # [本质] 卡片 2.0 **不支持 note 组件**（实测报 unsupported tag note，
    #        ErrCode 200861），整卡会被拒；markdown 是 2.0 里唯一稳妥的落款方式
    # [后果] 页脚能正常渲染，不会把整张卡拖进纯文本降级
    elements.append(
        {"tag": "markdown", "content": "<font color='grey'>🤖 由 AI 自动筛选、翻译并生成摘要</font>"}
    )

    subtitle = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} · 今日 {count} 条"
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": _header(title=headline, subtitle=subtitle),
        "body": {"elements": elements},
    }

