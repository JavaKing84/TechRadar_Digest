# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/llm_translator.py
[职责] 把一篇外文/中文新闻变成「中文标题 + 3~10 条中文摘要」的严格 JSON。
      本文件只关心新闻，不掺任何岗位职责/待遇抽取（与 news_ai_job 的
      提示词完全独立，两套模板不共享）。

[铁律] 摘要只陈述原文明确出现的事实，禁止推断、禁止补充原文没有的信息。
=====================================================================
"""
from __future__ import annotations

import json
import re

from news_ai_science.contracts.models import Article
from news_ai_science.core.errors import LLMError
from news_ai_science.core.logger import get_logger
from news_ai_science.core.text import looks_english, truncate
from news_ai_science.stages.generate.client import LLMClient

logger = get_logger(__name__)

SUMMARIES_MIN = 3
SUMMARIES_MAX = 10
SUMMARY_CHARS = 60      # 单条摘要字数上限
TITLE_CHARS = 200
FALLBACK_CHARS = 200    # LLM 彻底失败时截取正文前 200 字
FORMAT_RETRIES = 2

# [表面] 摘要开头的列表符号：- • · * 或 1. 1、 1)
# [本质] 卡片里每条摘要都会自己加「- 」，模型再带一层就变成「- - 要点」，
#        "1. 第二条" 更是会渲染成「- 1. 第二条」这种两层编号
# [后果] 不管模型怎么排版，卡片上永远是一层统一的项目符号
_LIST_MARK_RE = re.compile(r"^\s*(?:[-•·*]+|\d+\s*[.、)）])\s*")



# =====================================================================
# 提示词
# =====================================================================

SYSTEM_PROMPT = (
    "你是一名严谨的科技新闻编辑。"
    "你的回答必须是一个 JSON 对象，不要输出任何解释、寒暄或 markdown 代码块标记。"
)


def build_messages(article: Article) -> list[dict]:
    """
    # [表面] 拼 system + user
    # [本质] 正文截断到 3000 字：实测 3500 字英文正文配 6000 token 预算时，
    #        模型仍可能把正文挤成空串；缩短输入比无限加预算更稳，
    #        10 条摘要也用不完更多信息；语言判断决定要不要翻译标题
    # [后果] token 花费可控，外文一定走翻译路径
    """
    body = truncate(article.body, 3000, suffix="…")
    language = "英文" if looks_english(article.title + " " + article.body) else "中文"
    user_content = (
        f"原始标题：{article.title}\n"
        f"来源媒体：{article.source_name}\n"
        f"原始正文：\n{body}\n"
        f"来源语言：{language}\n\n"
        "【任务要求】\n"
        f"1. translated_title：地道、准确的中文标题。原文是英文必须翻译并润色，"
        "不要直译腔，不要加书名号或引号。\n"
        f"2. summaries：{SUMMARIES_MIN} 到 {SUMMARIES_MAX} 条中文要点，每条不超过 {SUMMARY_CHARS} 字。"
        "信息量大给 8~10 条，短文给 3~5 条。\n"
        "3. summaries 只陈述原文中明确出现的事实、数据、观点。"
        "严禁推断、严禁补充原文没有的信息、严禁加入你自己的评价。\n"
        "4. 剔除广告、推广、引导关注、版权声明等废话。\n"
        "5. 每条要点用完整短句，不要用「本文」「作者」开头，直接说事实。\n"
        "6. 只返回标准 JSON，不要用 ```json 包裹。\n\n"
        "【返回格式】\n"
        '{"translated_title": "中文标题", "summaries": ["要点1", "要点2", "要点3"]}'
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# =====================================================================
# 解析与兜底
# =====================================================================

def _strip_fences(raw: str) -> str:
    """剥掉 ```json ... ``` 围栏；模型再怎么被叮嘱也可能加。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_news_json(raw: str) -> tuple[str, list[str]] | None:
    """
    # [表面] 从模型回复里抠出 (中文标题, 摘要列表)
    # [本质] 先剥围栏，再取第一个 { 到最后一个 }；解析不出来返回 None 交给上层重试，
    #        绝不用正则硬猜字段
    # [后果] 脏回复要么被正确救回，要么明确失败，不会产出半截摘要
    """
    text = _strip_fences(raw)
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    title = str(data.get("translated_title") or "").strip()
    raw_items = data.get("summaries")
    if not isinstance(raw_items, list):
        return None

    # [表面] 清洗每条摘要：去列表符号、按字数截断
    # [本质] 卡片用 markdown 列表渲染，摘要自带 "- " 会变成双层圆点
    # [后果] 折叠面板里只有一个层级的列表
    cleaned = []
    for entry in raw_items:
        line = truncate(_LIST_MARK_RE.sub("", str(entry)).strip(), SUMMARY_CHARS)
        if line:
            cleaned.append(line)
    if not cleaned:
        return None
    if len(cleaned) > SUMMARIES_MAX:
        logger.warning("模型给了 %d 条摘要，截到 %d 条", len(cleaned), SUMMARIES_MAX)
        cleaned = cleaned[:SUMMARIES_MAX]
    elif len(cleaned) < SUMMARIES_MIN:
        logger.warning("模型只给了 %d 条摘要（要求 %d~%d），照原样使用",
                       len(cleaned), SUMMARIES_MIN, SUMMARIES_MAX)
    return truncate(title, TITLE_CHARS) if title else "", cleaned


def translate(article: Article, client: LLMClient | None) -> dict:
    """
    # [表面] 一条新闻 -> {title_zh, summaries, is_fallback}
    # [本质] 三条路径：无客户端直接兜底；调用成功严格解析；
    #        解析失败最多重试 FORMAT_RETRIES 次，并把上一轮脏输出回喂纠正
    # [后果] 模型挂掉那一轮照常推送（内容降级但不缺席）
    """
    if client is None:
        return _fallback(article, "无 LLM 客户端")

    messages = build_messages(article)
    last_reason = "未知原因"
    for attempt in range(FORMAT_RETRIES + 1):
        try:
            # [表面] 不再硬编码 max_tokens，用客户端的配置值
            # [本质] 原来这里写死 3500，把 config.yaml 的 llm.max_tokens 顶掉了；
            #        该模型会先吃 reasoning 预算，改配置却不生效是典型的暗坑
            # [后果] 调参只需改 config.yaml
            raw = client.chat(messages, temperature=0.2)
        except LLMError as exc:
            # [表面] 客户端内部已重试过，这里不再重试
            # [本质] 走到 LLMError 说明网络/鉴权层面已失败，再打一次只是延后降级
            # [后果] 模型不可用时整轮秒级完成
            last_reason = str(exc)
            break

        parsed = parse_news_json(raw)
        if parsed is not None:
            title_zh, summaries = parsed
            return {
                "title_zh": title_zh or article.title,
                "summaries": summaries,
                "is_fallback": False,
            }

        last_reason = "模型返回的不是合法 JSON"
        logger.warning("JSON 解析失败(第 %d 次)，%s", attempt + 1, article.title[:30])
        if attempt < FORMAT_RETRIES:
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "你上次的输出不是合法 JSON。请只输出 JSON 对象本身，不要任何其他文字。"},
            ]

    return _fallback(article, last_reason)


def _fallback(article: Article, reason: str) -> dict:
    """
    # [表面] 截取正文前 200 字当摘要
    # [本质] 需求钉死的降级：模型不可用 ≠ 机器人停摆；正文为空时退化为截断标题
    # [后果] 推送 100% 可用；is_fallback=True 让排查时一眼认出兜底条目
    """
    logger.error("新闻摘要失败，走兜底: %s | %s", article.title[:30], reason)
    return {
        "title_zh": article.title,
        "summaries": [truncate(article.body or article.title, FALLBACK_CHARS)],
        "is_fallback": True,
    }
