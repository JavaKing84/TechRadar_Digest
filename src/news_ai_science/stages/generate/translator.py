# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/translator.py
[职责] 把一篇外文/中文新闻变成
        「中文标题 + 100 字内摘要 + 全文中文翻译」的严格 JSON。
      本文件只关心新闻，不掺任何岗位职责/待遇抽取（与 news_ai_job 的
      提示词完全独立，两套模板不共享）。

[铁律] 摘要与译文只陈述原文明确出现的事实，禁止推断、禁止补充原文没有的信息。

[铁律] 推到飞书的每一个字都必须是中文。模型没翻译、翻译失败、翻译出英文，
      一律不推：外文文章直接丢弃（返回 rejected），中文文章才允许降级为原文节选。
      早期版本的兜底是"模型挂了就推英文原文"，正是这个兜底把 MIT TR /
      Wired / VentureBeat 的英文标题原样发进了群。

[演进] 早期输出版本是 summaries（3~10 条零散要点），卡片折叠面板里展示要点列表；
      现改为 summary（一段话摘要，卡片右侧可见）+ full_translation（全文翻译，
      折叠面板内展示）。旧库记录仍只有 summaries，卡片层做降级兼容。
=====================================================================
"""
from __future__ import annotations

import json
import re
import time

from news_ai_science.contracts.models import Article
from news_ai_science.core.errors import LLMError
from news_ai_science.core.logger import get_logger
from news_ai_science.core.text import is_chinese_text, looks_english, truncate
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

# ---- 中文硬性校验 ------------------------------------------------------
# [表面] 标题/摘要/译文各自至少要多少个汉字，且汉字占比不低于多少
# [本质] 纯英文的 cjk 恒为 0，绝对数就能挡住；但还得挡住「英文正文里
#        掺一两个汉字」的伪中文，所以再加一道占比
#        min_share 放到 0.20 是刻意的：中文标题里夹 GitHub Copilot
#        Fusion / Opus 5.5 这类专名是常态，收得太紧会把正常译文误杀
# [后果] 外文原样回吐 100% 被拦，正常中英混排标题 0 误杀
ZH_MIN_SHARE = 0.20
TITLE_MIN_CJK = 2
SUMMARY_MIN_CJK = 6
TRANSLATION_MIN_CJK = 20

# [表面] 历史 payload 的体检门槛：文本太短就不判（判不准）
# [本质] 旧库记录只有 summaries、甚至只有一两条短要点，用正式阈值去量
#        会把正常中文记录误判成英文并永久拉黑；只对「长到能下结论」的
#        文本做判定，短文本一律放行
# [后果] 历史中文记录不会误伤，历史英文长文照样拦得住
PAYLOAD_JUDGE_MIN = 12

# [表面] LLM 整轮失败后的外层重试轮数与退避基数（秒）
# [本质] client.chat 内部已重试 3 次（网络抖动），这里管的是"一整轮都失败"：
#        额度打满、网关 502、模型空回复这类分钟级故障，隔几秒再打一次
#        往往就通了；不重试就等于当场放弃一批新闻
# [后果] 单条最坏多花 (ROUNDS × (1+2+3) × 90s)，但只在真出故障时才发生
TRANSLATE_ROUNDS = 2
RETRY_BACKOFF = 3.0

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
        "0. 【最高优先级】translated_title、summary、full_translation 三个字段必须全部是"
        "简体中文。原文是英文时必须真正翻译，严禁把英文原句照抄回来，严禁输出英文摘要。"
        "品牌名、公司名、产品名、模型名（如 OpenAI、Gemini、Copilot）可以保留英文原名，"
        "但句子的主体、谓语、叙述必须是中文；出现整句英文即视为不合格。\n"
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


def chinese_problems(title: str, summary: str = "", translation: str = "") -> list[str]:
    """
    # [表面] 检查一条译文的三个字段是不是"够中文"，返回问题清单（空 = 合格）
    # [本质] 这是全链路唯一的中文判定入口：翻译层用它决定要不要重试，
    #        流水线与推送层用它做最后一道闸。散落在各处各写一套阈值，
    #        迟早出现「翻译层放行、推送层拦下」的自相矛盾
    # [后果] 判定标准只改这一处，三层同步
    """
    problems: list[str] = []
    value = (title or "").strip()
    if not value:
        problems.append("标题为空")
    elif not is_chinese_text(value, min_cjk=TITLE_MIN_CJK, min_share=ZH_MIN_SHARE):
        problems.append("标题不是中文")
    if summary and not is_chinese_text(summary, min_cjk=SUMMARY_MIN_CJK, min_share=ZH_MIN_SHARE):
        problems.append("摘要不是中文")
    if translation and not is_chinese_text(
        translation, min_cjk=TRANSLATION_MIN_CJK, min_share=ZH_MIN_SHARE
    ):
        problems.append("译文不是中文")
    return problems


def payload_problems(info: dict, fallback_title: str = "") -> list[str]:
    """
    # [表面] 对一条已入库/待上卡的 payload 做同样的中文体检
    # [本质] 库里可能躺着历史英文记录（旧版兜底留下的），轮换推送会再次上卡；
    #        推送前必须重新验一遍，不能只信入库那一刻。
    #        旧格式只有 summaries、没有 summary，把要点拼起来一起验；
    #        文本太短的（旧记录常见）无法判断，直接放行，避免误杀中文记录
    #        fallback_title 是卡片层实际展示的标题（record.title），
    #        payload 里没有 title_zh 的旧记录要靠它才能被判到
    # [后果] 历史英文脏数据永远进不了群，历史中文记录不受影响
    """
    info = info or {}
    title = str(info.get("title_zh") or info.get("title") or fallback_title or "")
    problems = chinese_problems(title)

    summary = str(info.get("summary") or "")
    if not summary:
        legacy = [str(item).strip() for item in (info.get("summaries") or []) if str(item).strip()]
        summary = " ".join(legacy)
    if len(summary) >= PAYLOAD_JUDGE_MIN and not is_chinese_text(
        summary, min_cjk=SUMMARY_MIN_CJK, min_share=ZH_MIN_SHARE
    ):
        problems.append("摘要不是中文")

    translation = str(info.get("full_translation") or "")
    if len(translation) >= TRANSLATION_MIN_CJK and not is_chinese_text(
        translation, min_cjk=TRANSLATION_MIN_CJK, min_share=ZH_MIN_SHARE
    ):
        problems.append("译文不是中文")
    return problems


def _rejected(article: Article, reason: str, transient: bool = False) -> dict:
    """
    # [表面] 「这条不要了」的返回值，流水线见 rejected=True 直接丢弃
    # [本质] 外文翻译彻底失败时，唯一正确的处理是不推：
    #        推英文原文比不推更糟（群里看不懂，还占掉本轮名额）。
    #        transient 区分两种丢弃：
    #          · True  模型整轮不可用（额度/网络/没配 key）——是环境问题，
    #                  不该把文章永久拉黑，下次运行应当重试
    #          · False 模型给了回复但内容不是中文——这条本身就翻不好，永久拉黑
    # [后果] 环境故障恢复后内容能自动补推，真正的坏条目不会反复消耗额度
    """
    logger.error("[News] 翻译失败，本条丢弃（不推送）: %s | %s", article.title[:40], reason)
    return {
        "rejected": True,
        "transient": transient,
        "reason": reason,
        "title_zh": "",
        "summary": "",
        "full_translation": "",
        "summaries": [],
        "is_fallback": False,
    }


def translate(article: Article, client: LLMClient | None) -> dict:
    """
    # [表面] 一条新闻 -> {title_zh, summary, full_translation, summaries, is_fallback}
    #        翻译彻底失败且原文非中文 -> {rejected: True, reason}
    # [本质] 三条防线依次生效：
    #        ① 格式重试：回复不是 JSON / 字段为空，把脏回复回喂再要一次；
    #        ② 中文校验：是合法 JSON 但内容是英文（模型照抄原文）同样算失败，
    #           带上"必须输出中文"的纠正指令重试；
    #        ③ 外层轮次重试：整轮 LLM 调用失败（额度/网络/502）时隔几秒再打一轮；
    #        全挂了才收尾——中文原文降级推送，外文直接丢弃
    # [后果] 推到飞书的内容 100% 是中文；模型故障时群里静默而不是刷英文
    """
    if client is None:
        return _degrade(article, "无 LLM 客户端", transient=True)

    last_reason = "未知原因"
    problems: list[str] = []

    for round_index in range(TRANSLATE_ROUNDS + 1):
        # [表面] 每一轮都从干净的 prompt 开始，只把上一轮的"病历"附在后面
        # [本质] 带着上一轮的整段对话重开一轮会越滚越长（脏输出 3000 字 + 译文），
        #        反而更容易触发 token 上限；只保留结论（哪几项不合格）最省
        messages = build_messages(article)
        if problems:
            messages = messages + [{
                "role": "user",
                "content": f"注意：上一次输出不合格（{'、'.join(problems)}）。"
                           "本次必须输出简体中文的 translated_title / summary / full_translation，"
                           "严禁把英文原文照抄，只返回 JSON 对象。",
            }]

        llm_failed = False
        for attempt in range(FORMAT_RETRIES + 1):
            try:
                # [表面] 不硬编码 max_tokens，用客户端配置值
                # [本质] 该模型会先吃 reasoning 预算，写死会顶掉 config.yaml 的调参能力
                # [后果] 调参只需改 config.yaml
                raw = client.chat(messages, temperature=0.2)
            except LLMError as exc:
                # [表面] 客户端内部已重试过，跳出格式重试，交给外层轮次
                # [本质] LLMError 是网络/鉴权层面的失败，原地连打三次只会更慢；
                #        外层隔几秒重开一轮更有意义
                last_reason = str(exc)
                llm_failed = True
                break

            parsed = parse_news_json(raw)
            if parsed is None:
                last_reason = "模型返回的不是合法 JSON"
                logger.warning("JSON 解析失败(第 %d 次)，%s", attempt + 1, article.title[:30])
                if attempt < FORMAT_RETRIES:
                    messages = messages + [
                        {"role": "assistant", "content": raw},
                        {"role": "user",
                         "content": "你上次的输出不是合法 JSON（可能字符串里有未转义的换行）。"
                                    "请只输出 JSON 对象本身，字符串内换行写成 \\n 转义，不要任何其他文字。"},
                    ]
                continue

            # [表面] 标题为空时，只有"原文本身是中文"才允许拿原题顶上
            # [本质] 外文原文的标题是英文，拿它顶上来就是我们最初要修的那个 bug
            # [后果] 中文源不会因为模型漏了标题而丢条目，外文源绝不会露英文
            title = parsed["translated_title"]
            if not title:
                title = article.title if not looks_english(article.title) else ""
            problems = chinese_problems(title, parsed["summary"], parsed["full_translation"])
            if not problems:
                return {
                    "rejected": False,
                    "title_zh": title or article.title,
                    "summary": parsed["summary"],
                    "full_translation": parsed["full_translation"],
                    "summaries": parsed["summaries"],
                    "is_fallback": False,
                }

            last_reason = "、".join(problems)
            logger.warning(
                "译文非中文(第 %d 次)，%s | %s", attempt + 1, article.title[:30], last_reason
            )
            if attempt < FORMAT_RETRIES:
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user",
                     "content": "你上次的输出不合格：" + "、".join(problems) + "。"
                                "请把全部字段翻译成简体中文后重新输出（品牌名可保留英文），"
                                "只返回 JSON 对象。"},
                ]

        if llm_failed:
            if round_index < TRANSLATE_ROUNDS:
                wait = RETRY_BACKOFF * (round_index + 1)
                logger.warning(
                    "LLM 调用失败，%.1fs 后整轮重试 (%d/%d): %s",
                    wait, round_index + 1, TRANSLATE_ROUNDS, last_reason,
                )
                time.sleep(wait)
                continue
            break

    return _degrade(article, last_reason, transient=llm_failed)


def _degrade(article: Article, reason: str, transient: bool = False) -> dict:
    """
    # [表面] LLM 彻底失败后的收尾：中文原文可降级，外文原文一律丢弃
    # [本质] 旧版的兜底是「模型挂了就把原文当译文推」——中文源没事，
    #        外文源就把整篇英文发进了群，这就是线上那个 bug 的直接来源。
    #        正确做法是分语言：
    #          · 原文本身是中文 -> 推中文原文节选（内容降级但仍是中文）
    #          · 原文是外文     -> 没有译文就等于没有内容，丢弃
    # [后果] is_fallback=True 的条目 100% 是中文；英文条目一条都不会漏出去
    """
    body = article.body or ""
    title = article.title or ""

    # 原文是否外文：标题判一次 + 正文判一次，任一命中即视为外文
    foreign = looks_english(f"{title} {body}") or not is_chinese_text(
        title, min_cjk=TITLE_MIN_CJK, min_share=ZH_MIN_SHARE
    )
    if foreign:
        return _rejected(article, f"{reason}（外文原文无可用译文）", transient=transient)

    logger.warning("[News] 翻译失败，改用中文原文节选: %s | %s", title[:40], reason)
    summary = truncate(body, SUMMARY_CHARS)
    translation = truncate(_tidy_paragraphs(body), TRANSLATION_MAX_CHARS)
    # [表面] 正文里夹了大段英文引文时，摘要/译文可能整体偏英文
    # [本质] 宁可少给一块，也不能让英文混进卡片：哪块不合格就清空哪块
    # [后果] 极端情况下只剩中文标题，也不会出现英文段落
    if summary and not is_chinese_text(summary, min_cjk=SUMMARY_MIN_CJK, min_share=ZH_MIN_SHARE):
        summary = ""
    if translation and not is_chinese_text(
        translation, min_cjk=TRANSLATION_MIN_CJK, min_share=ZH_MIN_SHARE
    ):
        translation = ""
    if not summary and not translation:
        return _rejected(article, f"{reason}（原文节选也非中文）", transient=transient)

    return {
        "rejected": False,
        "title_zh": title,
        "summary": summary,
        "full_translation": translation,
        "summaries": [truncate(translation or summary, FALLBACK_CHARS)],
        "is_fallback": True,
    }
