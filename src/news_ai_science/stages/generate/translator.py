# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/translator.py
[职责] 把一篇外文/中文新闻变成
        「中文标题 + 100 字内摘要 + 全文中文翻译」的严格 JSON。
      本文件只关心新闻，不掺任何岗位职责/待遇抽取（与 news_ai_job 的
      提示词完全独立，两套模板不共享）。

[铁律] 摘要与译文只陈述原文明确出现的事实，禁止推断、禁止补充原文没有的信息。

[演进] 早期输出版本是 summaries（3~10 条零散要点），卡片折叠面板里展示要点列表；
      现改为 summary（一段话摘要，卡片右侧可见）+ full_translation（全文翻译，
      折叠面板内展示）。旧库记录仍只有 summaries，卡片层做降级兼容。
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

# 卡片右侧摘要区的字数上限。超过就会被 <font> 语义挤压成三行以上，
# 破坏「左图 + 右文」的等高布局，因此硬截断。
SUMMARY_CHARS = 100

# 送进模型的正文上限。译文长度与输入近似线性相关：4000 字符英文
# 约产出 1800~2200 字中文，配 max_tokens=8000 仍有余量。
INPUT_BODY_CHARS = 4000

# 译文最终入库/上卡的长度上限。飞书卡片对单卡 JSON 体积有上限，
# 一段 6000 字的译文会把整卡推到 30KB 附近，触发校验失败。
TRANSLATION_MAX_CHARS = 3000

TITLE_CHARS = 200
FALLBACK_CHARS = 200    # LLM 彻底失败时截取正文前 200 字
FORMAT_RETRIES = 2

# [表面] 段落首尾的列表符号：- • · * 或 1. 1、 1)
# [本质] 旧版 summaries 走 markdown 列表渲染，模型自带「- 」会叠成「- - 要点」；
#        新版摘要是一段话，这里主要用于清洗兜底路径与旧格式兼容
_LIST_MARK_RE = re.compile(r"^\s*(?:[-•·*]+|\d+\s*[.、)）])\s*")


# =====================================================================
# 提示词
# =====================================================================

SYSTEM_PROMPT = (
    "你是一名严谨的科技新闻编辑兼译者。"
    "你的回答必须是一个 JSON 对象，不要输出任何解释、寒暄或 markdown 代码块标记。"
)


def build_messages(article: Article) -> list[dict]:
    """
    # [表面] 拼 system + user
    # [本质] 正文截断到 INPUT_BODY_CHARS：一是控 token，二是译文长度必须可预期
    #        （卡片要能装下）；语言判断决定标题与译文走「翻译」还是「润色」路径
    # [后果] 单条 token 花费可控，外文一定走翻译路径
    """
    body = truncate(article.body, INPUT_BODY_CHARS, suffix="…")
    is_en = looks_english(article.title + " " + article.body)
    language = "英文" if is_en else "中文"

    user_content = (
        f"原始标题：{article.title}\n"
        f"来源媒体：{article.source_name}\n"
        f"原始正文：\n{body}\n"
        f"来源语言：{language}\n\n"
        "【任务要求】\n"
        "1. translated_title：地道、准确的中文标题。原文是英文必须翻译并润色，"
        "不要直译腔，不要加书名号或引号。\n"
        f"2. summary：一段不超过 {SUMMARY_CHARS} 字的中文摘要，讲清「发生了什么 + 为什么重要」。"
        "必须是完整成句的一段话，不要分点、不要换行、不要以「本文」「作者」开头、"
        "不要出现「摘要：」这类前缀。\n"
        "3. full_translation：原文的完整中文翻译。"
        "逐段对应，保留原文的段落结构与顺序，不要总结、不要省略、不要合并段落、"
        "不要添加译者注或评价。段落之间用 \\n\\n 分隔。"
        "专有名词、产品名、公司名首次出现时保留英文原名并用括号附中文（如 Anthropic（安思罗匹克））。"
        "原文本身是中文时，full_translation 输出清洗排版后的中文全文（去掉导航、广告、版权声明）。\n"
        "4. 所有内容只陈述原文明确出现的事实、数据、观点。"
        "严禁推断、严禁补充原文没有的信息、严禁加入你自己的评价。\n"
        "5. 剔除广告、推广、引导关注、版权声明等废话。\n"
        "6. 只返回标准 JSON，不要用 ```json 包裹；字符串内部换行必须写成 \\n 转义。\n\n"
        "【返回格式】\n"
        '{"translated_title": "中文标题", '
        '"summary": "一段不超过100字的中文摘要", '
        '"full_translation": "第一段译文\\n\\n第二段译文"}'
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


def _clip_summary(text: str, limit: int = SUMMARY_CHARS) -> str:
    """
    # [表面] 把摘要压到 limit 字以内，尽量在句末断开
    # [本质] 硬截断会切在词中间，卡片上出现「……谷歌亦将获益，凸显科技…」这种
    #        半句话；优先回退到最近的句号/分号，读起来是完整的句子。
    #        若整段前 55% 都没有标点（长句），才退回按字截断加省略号。
    # [后果] 摘要要么是完整短句，要么明确标出被截断
    """
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    window = value[:limit]
    for mark in ("。", "！", "？", "；"):
        pos = window.rfind(mark)
        if pos >= int(limit * 0.55):
            return window[: pos + 1]
    return window.rstrip("，、,;；:：") + "…"


def _tidy_paragraphs(text: str) -> str:
    """
    # [表面] 规整译文排版
    # [本质] ① 模型常把段落分隔写成单个 \\n，飞书 markdown 会并成一行读不断；
    #        ② 连续三个以上空行、行尾空格、段首缩进都会让卡片出现莫名留白
    # [后果] 折叠面板里是干净的分段正文
    """
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in value.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line:
            # 只保留一个空行作为段落分隔
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


def parse_news_json(raw: str) -> dict | None:
    """
    # [表面] 从模型回复里抠出 {中文标题, 摘要, 全文翻译}
    # [本质] 先剥围栏，再取第一个 { 到最后一个 }；用 json.loads(strict=False)：
    #        译文里带裸换行符时标准严格模式会直接判非法（JSON 规范不允许字符串内
    #        出现控制字符），而模型偏偏最容易在长译文里犯这个错。
    #        放宽后成功率显著提高，且不会误收结构性错误。
    #        解析不出来返回 None 交给上层重试，绝不用正则硬猜字段
    # [后果] 脏回复要么被正确救回，要么明确失败，不会产出半截内容
    """
    text = _strip_fences(raw)
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1], strict=False)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    title = truncate(str(data.get("translated_title") or "").strip(), TITLE_CHARS)
    summary = _LIST_MARK_RE.sub("", str(data.get("summary") or "").strip())
    summary = _clip_summary(summary.replace("\n", " ").strip())
    translation = truncate(_tidy_paragraphs(str(data.get("full_translation") or "")),
                           TRANSLATION_MAX_CHARS)

    # 旧格式兼容：只有 summaries 时，第一条降级当摘要用
    raw_items = data.get("summaries")
    summaries: list[str] = []
    if isinstance(raw_items, list):
        for entry in raw_items:
            line = truncate(_LIST_MARK_RE.sub("", str(entry)).strip(), SUMMARY_CHARS)
            if line:
                summaries.append(line)

    if not summary and summaries:
        summary = summaries[0]
    if not translation and not summary:
        return None

    if not translation:
        logger.warning("模型未给出 full_translation，本条折叠面板将降级为要点列表")

    return {
        "translated_title": title,
        "summary": summary,
        "full_translation": translation,
        "summaries": summaries,
    }


def translate(article: Article, client: LLMClient | None) -> dict:
    """
    # [表面] 一条新闻 -> {title_zh, summary, full_translation, summaries, is_fallback}
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
            # [表面] 不硬编码 max_tokens，用客户端配置值
            # [本质] 该模型会先吃 reasoning 预算，写死会顶掉 config.yaml 的调参能力
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
            return {
                "title_zh": parsed["translated_title"] or article.title,
                "summary": parsed["summary"],
                "full_translation": parsed["full_translation"],
                "summaries": parsed["summaries"],
                "is_fallback": False,
            }

        last_reason = "模型返回的不是合法 JSON"
        logger.warning("JSON 解析失败(第 %d 次)，%s", attempt + 1, article.title[:30])
        if attempt < FORMAT_RETRIES:
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user",
                 "content": "你上次的输出不是合法 JSON（可能字符串里有未转义的换行）。"
                            "请只输出 JSON 对象本身，字符串内换行写成 \\n 转义，不要任何其他文字。"},
            ]

    return _fallback(article, last_reason)


def _fallback(article: Article, reason: str) -> dict:
    """
    # [表面] 截取正文前 200 字当摘要，前 3000 字当"译文"
    # [本质] 需求钉死的降级：模型不可用 ≠ 机器人停摆；
    #        没有真译文时宁可给原文节选，也不要给一句"翻译失败"
    # [后果] 推送 100% 可用；is_fallback=True 让排查时一眼认出兜底条目
    """
    logger.error("新闻翻译失败，走兜底: %s | %s", article.title[:30], reason)
    body = article.body or article.title
    return {
        "title_zh": article.title,
        "summary": truncate(body, SUMMARY_CHARS),
        "full_translation": truncate(_tidy_paragraphs(body), TRANSLATION_MAX_CHARS),
        "summaries": [truncate(body, FALLBACK_CHARS)],
        "is_fallback": True,
    }
