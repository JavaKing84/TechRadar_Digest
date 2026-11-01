# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] pipeline/runner.py
[职责] 一轮的编排：crawl -> 去重/配图门槛 -> 翻译摘要 -> 上传封面图 -> 入库 -> 聚合推送。
       贵的步骤（LLM、图床上传）永远排在便宜的门槛之后。
=====================================================================
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from news_ai_science.core.logger import get_logger
from news_ai_science.core.text import truncate, url_slug
from news_ai_science.stages.crawl.web import crawl_all
from news_ai_science.stages.generate.client import LLMClient
from news_ai_science.stages.generate.translator import (
    TITLE_CHARS,
    payload_problems,
    translate,
)
from news_ai_science.stages.push.card_builder import (
    MAX_CARD_ITEMS,
    build_multi_news_card,
)
from news_ai_science.stages.push.feishu_sender import FeishuSender, RateLimiter
from news_ai_science.stages.push.image_uploader import FeishuImageUploader
from news_ai_science.storage.db import NewsStore

logger = get_logger(__name__)

# [表面] 一轮默认备几条
# [本质] 3~5 条是飞书单卡渲染的舒适区；具体条数由 config 的 push.per_run 决定，
#        这里只是配置缺失时的兜底
# [后果] 不配 config 也不会退化成「一次一条」
DEFAULT_PER_RUN = 4

# [表面] 候选池最多放大到目标条数的几倍
# [本质] 翻译失败 / 非中文的条目会被丢弃，丢一条就少一条；只按 target 取候选，
#        出现两条坏数据本轮就只剩一半内容。多备候选才能补位
# [后果] 有坏条目时仍能凑满 per_run，坏条目也不会白白占掉名额
CANDIDATE_FACTOR = 3

# [表面] 轮换推送时附在卡片上的红线提示
# [本质] 抓取长期失败时，群里每天都在正常出卡，只是内容变旧——用户从卡片上
#        分辨不出「今天没新闻」和「程序抓不到」。这句提示把静默失败变成可见失败
# [后果] 看到这句话就知道该去查抓取源了，不用等别人发现内容不对
ROTATE_NOTICE = "⚠️ 本轮无新文章入库，本次推送内容为历史轮换，请检查抓取源。"

# [表面] 轮换池的两个上限（config 的 push.rotate_* 可覆盖）
# [本质] 只靠 cooldown 挡不住长期失败：同一批老新闻会无限重推。
#        7 天内的存货才值得推；同一篇最多重推 2 次
# [后果] 池子用尽时宁可断更，也不推古董；上限调成 0 即关闭该限制
DEFAULT_ROTATE_MAX_AGE_DAYS = 7
DEFAULT_ROTATE_MAX_TIMES = 2


def build_client(cfg: dict) -> LLMClient | None:
    """按配置造 LLM 客户端；任何一处缺失就返回 None，下游走兜底。"""
    llm = cfg.get("llm") or {}
    try:
        return LLMClient(
            base_url=str(llm.get("base_url") or ""),
            api_key=str(llm.get("api_key") or ""),
            model=str(llm.get("model") or ""),
            timeout=float(llm.get("timeout") or 90),
            max_retries=int(llm.get("retries") or 1),
            max_tokens=int(llm.get("max_tokens") or 6000),
            disable_thinking=bool(llm.get("disable_thinking", True)),
            thinking_off_style=str(llm.get("thinking_off_style") or "auto"),
        )
    except Exception as exc:
        logger.error("LLM 客户端初始化失败（将走兜底）: %s", exc)
        return None


def build_uploader(cfg: dict) -> FeishuImageUploader:
    """图片上传器；凭证没配时 enabled=False，上层自动走无图。"""
    app = cfg.get("feishu_app") or {}
    return FeishuImageUploader(
        app_id=str(app.get("app_id") or ""),
        app_secret=str(app.get("app_secret") or ""),
    )


def per_run(cfg: dict) -> int:
    """本轮要备几条：config 的 push.per_run，夹在 1~MAX_CARD_ITEMS 之间。"""
    raw = (cfg.get("push") or {}).get("per_run")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_PER_RUN
    return max(1, min(MAX_CARD_ITEMS, value))


def _discard(store: NewsStore, art, reason: str, transient: bool = False) -> None:
    """
    # [表面] 把一条不要的新闻丢弃；内容问题记成 rejected，环境问题不入库
    # [本质] 两种丢弃必须分开处理：
    #          · 内容永久不合格（模型反复吐英文）-> 记 rejected。
    #            不记的话 is_seen 认不出它，下一轮会重抓、重翻，白烧额度；
    #          · LLM 整轮不可用（额度/网络/没配 key）-> 什么都不记。
    #            这是环境故障，记成 rejected 会让恢复后也永远补不回来
    # [后果] 坏文章只烧一次额度；故障恢复后同一批新闻还能正常补推
    """
    if transient:
        logger.error("[News] 本轮丢弃（LLM 不可用，不推，下轮重试）: %s | %s",
                     art.title[:40], reason)
        return
    logger.error("[News] 丢弃本条（非中文 / 翻译失败，不推送）: %s | %s",
                 art.title[:40], reason)
    try:
        store.reject(
            art.url,
            truncate(art.title, TITLE_CHARS),
            art.source_name,
            {"rejected": True, "reason": reason, "is_fallback": False},
        )
    except Exception as exc:
        # [表面] 记不上 rejected 就算了，不能因此中断整轮
        # [本质] 代价只是下一轮可能重抓这篇再失败一次（浪费一次 LLM 调用），
        #        比整轮崩掉轻得多
        # [后果] 库异常时退化成「不记录地丢弃」，流程继续
        logger.warning("[News] 丢弃状态写库失败，本轮不记录: %s | %s", art.url[:70], exc)


def collect(cfg: dict, store: NewsStore, client: LLMClient | None) -> list[dict]:
    """
    # [表面] 抓一轮新闻，备齐 N 条可推送的记录（已入库）
    # [本质] 顺序是「链接层去重 -> 必须有配图 -> 翻译摘要 -> 上传图 -> 入库」：
    #        最贵的两步排在最便宜的两道门之后；target 由 push.per_run 决定
    #        翻译结果必须过中文体检：不合格的条目直接丢弃并从候选池补位，
    #        所以候选池取 target 的若干倍，而不是刚好 target 条
    # [后果] 返回 0~N 条记录；一条都没有时返回空列表（宁可少推，绝不推英文）
    """
    crawl_cfg = cfg.get("crawl") or {}
    require_image = bool(crawl_cfg.get("require_image", True))
    target = per_run(cfg)

    articles, errors = crawl_all(cfg, seen=store.is_seen)
    logger.info("[News] 本轮抓到 %d 篇正文，失败源 %d 个", len(articles), len(errors))

    fresh = [art for art in articles if not store.is_seen(art.url)]
    if require_image:
        # [表面] 无图直接跳过
        # [本质] 用户明确要求：配图必须与内容相关，取不到就不推，绝不用随机图凑数
        # [后果] 宁可少推一条，也不发一张不相关的图
        no_image = [art for art in fresh if not art.cover_image]
        for art in no_image:
            logger.warning("[News] 无可用配图，按规则跳过: %s", art.title[:40])
        fresh = [art for art in fresh if art.cover_image]

    if not fresh:
        logger.warning("[News] 没有未推送且有配图的新文章")
        return []

    candidates = fresh[: target * CANDIDATE_FACTOR]
    uploader = build_uploader(cfg)
    if not uploader.enabled:
        logger.warning("[News] 飞书应用凭证缺失，本轮无法上传配图（卡片将无图）")

    records: list[dict] = []
    for art in candidates:
        if len(records) >= target:
            break

        info = translate(art, client)
        if info.get("rejected"):
            _discard(store, art, str(info.get("reason") or "翻译彻底失败"),
                     transient=bool(info.get("transient")))
            continue

        payload = {
            "title_zh": info.get("title_zh") or "",
            # [表面] 卡片右侧摘要（≤100 字）
            # [本质] 与 full_translation 一起入库：轮换推送时无需再调 LLM，
            #        历史条目也能直接渲染成新版卡片
            "summary": info.get("summary") or "",
            "full_translation": info.get("full_translation") or "",
            # 旧字段保留：改造前入库的记录只有 summaries，
            # 卡片层用它做"无译文"时的降级展示
            "summaries": info.get("summaries") or [],
            "image_key": "",
            "cover_image": art.cover_image,
            "is_fallback": bool(info.get("is_fallback")),
        }

        # [表面] 入库前的最后一道闸：翻译层说合格，这里再独立量一次
        # [本质] 翻译层将来若被改动（换模型、改提示词），这道闸仍能兜住；
        #        两道闸用同一套阈值（payload_problems），不会互相打架
        # [后果] 任何英文内容都进不了库，也就永远进不了群
        problems = payload_problems(payload)
        if problems:
            _discard(store, art, "内容非中文：" + "、".join(problems))
            continue

        if art.cover_image:
            img_key = uploader.upload_from_url(art.cover_image) or ""
            if not img_key:
                # [表面] 上传失败仍继续推，只是没图
                # [本质] 「文章没有配图」和「图床上传出故障」是两件事：前者按规则跳过，
                #        后者属于基础设施故障，为此整轮不推等于把一次 500 变成一次断更
                # [后果] 图挂了信息照样到，日志里有 WARNING 可查
                logger.warning("[News] 配图上传失败，本条将无图推送: %s", art.url[:70])
            payload["image_key"] = img_key

        # [表面] 写库失败不中断整轮，跳过这一条
        # [本质] 磁盘满 / 库文件损坏是这台机器的事，不是这条新闻的事；
        #        入库失败还硬推出去，会被当成正常推送，下一轮又因 is_seen 认不出
        #        而重抓重翻——宁可不推，也不要制造「推了但没记录」的幽灵条目
        # [后果] 单条写库失败只丢一条，其余条目与推送流程照常
        try:
            store.save(art.url, payload["title_zh"], art.source_name, payload)
        except Exception as exc:
            logger.error("[News] 入库失败（磁盘满 / 库损坏？），本条跳过不推: %s | %s",
                         art.url[:70], exc)
            continue
        records.append({
            "url": art.url,
            "title": payload["title_zh"],
            "source_name": art.source_name,
            "info": payload,
        })
        logger.info(
            "[News] 备好 %d/%d: %s | 摘要 %d 字 | 译文 %d 字 | 配图 %s | 兜底 %s | slug=%s",
            len(records), target,
            payload["title_zh"][:36],
            len(payload["summary"]),
            len(payload["full_translation"]),
            "有" if payload["image_key"] else "无",
            "是" if payload["is_fallback"] else "否",
            url_slug(art.url),
        )

    if len(records) < target:
        logger.warning(
            "[News] 本轮只备好 %d/%d 条（其余候选非中文或翻译失败，已丢弃）",
            len(records), target,
        )
    return records


def push(cfg: dict, store: NewsStore, records: list[dict] | None,
         dry_run: bool = False) -> dict:
    """
    # [表面] 把 1~N 条新闻装进一张聚合卡推给飞书
    # [本质] 本轮没抓到新东西时，从历史里轮换最久没推的几条，避免群里断更；
    #        卡片被拒则退回纯文本（多条一起降级，不丢内容）
    # [后果] 返回 {ok, count, titles, message}
    """
    push_cfg = cfg.get("push") or {}
    cooldown = float(push_cfg.get("cooldown_hours") or 4)
    limit = per_run(cfg)
    from_rotate = False

    # [表面] 轮换池的两道限制，可配置、可关闭
    # [本质] 用 is None 判断而不是 `or`：0 是合法取值（关闭限制），
    #        `x or 默认` 会把 0 悄悄换成 7 天——config 里 cooldown_hours: 0 踩过
    age_raw = push_cfg.get("rotate_max_age_days")
    times_raw = push_cfg.get("rotate_max_times")
    rotate_age = DEFAULT_ROTATE_MAX_AGE_DAYS if age_raw is None else float(age_raw)
    rotate_times = DEFAULT_ROTATE_MAX_TIMES if times_raw is None else int(times_raw)

    # [表面] 没传记录时，先把队列里的待推条目捞出来
    # [本质] --push-only 或本轮采集为空时 records 为空，直接走轮换会漏掉
    #        「pending 里明明有货」，报出「没有可推的新闻」这种自相矛盾的错
    # [后果] 待推队列永远优先于历史轮换
    if not records:
        records = store.pick_pending_many(limit)
    if not records:
        records = store.pick_rotate_many(limit, cooldown, rotate_age, rotate_times)
        from_rotate = bool(records)
    if not records:
        logger.error(
            "[News] 没有可推的新闻（队列空，且无符合轮换条件的旧闻："
            "入库 ≤ %.0f 天、同一篇重推 < %d 次）",
            rotate_age, rotate_times,
        )
        return {"ok": False, "reason": "no_record", "count": 0}

    # [表面] 上卡前的第二道闸：逐条做中文体检，非中文的一条都不许进卡
    # [本质] 记录可能来自两处：本轮 collect（已验过）和历史轮换（库里躺着
    #        旧版兜底留下的英文条目）。collect 只覆盖第一条路径，
    #        轮换路径必须在这里再拦一次，否则英文会隔几小时又冒出来一次
    # [后果] 群里不会出现任何英文条目；被拦下的历史条目落 rejected，不再重复冒头
    kept: list[dict] = []
    dropped: list[dict] = []
    for record in records:
        problems = payload_problems(
            record.get("info") or {}, fallback_title=str(record.get("title") or "")
        )
        if problems:
            logger.error(
                "[News] 上卡前拦下非中文条目（不推送）: %s | %s",
                str(record.get("title") or "")[:40], "、".join(problems),
            )
            dropped.append(record)
            continue
        kept.append(record)
    if dropped:
        store.mark_rejected_many([str(record.get("url") or "") for record in dropped])
    records = kept

    if not records:
        logger.error("[News] 待推条目全部非中文，本轮不推送（已标记 rejected）")
        return {"ok": False, "reason": "all_rejected", "count": 0}

    records = records[:limit]
    # [表面] 轮换推送时把红线提示直接钉进卡片（subtitle + 正文红字）
    # [本质] 群里只发一条消息，不额外发文本——历史上的「卡片 + 纯文本」双发
    #        在群里长得像重复推送，排查成本远高于收益
    # [后果] 每轮仍然只有 1 条 interactive 卡片，非常规推送自带可见告警
    notice = ROTATE_NOTICE if from_rotate else None
    card = build_multi_news_card(records, notice=notice)
    titles = [str(record.get("title") or "") for record in records]
    summary_title = " / ".join(titles)[:120]

    if dry_run:
        out_dir = Path(cfg["logging"]["dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"card_dryrun_news_{datetime.now():%Y%m%d_%H%M%S}.json"
        out_file.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        logger.info("[News] dry-run：聚合卡（%d 条）已写入 %s（未发送）", len(records), out_file)
        return {"ok": True, "dry_run": True, "count": len(records), "titles": titles}

    sender = FeishuSender(
        cfg["webhook_url"],
        cfg.get("webhook_secret") or "",
        RateLimiter(1.0),
        chat_name=cfg.get("chat_name") or "",
    )
    result = sender.send_card(card)
    # [表面] 卡片发不出去就是失败，绝不降级成纯文本
    # [本质] 曾经的「卡片失败 -> send_text 兜底」在群里会变成
    #        「一份纯文本 + 一份卡片」两条消息，看起来像重复推送，
    #        实际是两条独立的 API 调用（排查成本远高于收益）。
    #        失败时记录保持 pending，下一轮自动重发，比发一份乱码版更有价值
    # [后果] 群里只可能出现 interactive 卡片，永远不会再有纯文本资讯流
    if result.get("code") != 0:
        logger.error(
            "[News] 聚合卡发送失败 code=%s msg=%s（%d 条保持 pending，下一轮重发；不发纯文本）",
            result.get("code"), result.get("msg"), len(records),
        )
        return {
            "ok": False,
            "reason": result.get("msg"),
            "count": len(records),
            "titles": titles,
        }

    if result.get("code") == 0:
        urls = [record["url"] for record in records]
        # [表面] 轮换成功累加 rotate_count，新文章的首次推送只刷时间
        # [本质] 两者混用会让新文章也被计数，2 次之后新文章反而进不了轮换池
        # [后果] 重推次数只在「旧闻重推」时增长，语义准确
        try:
            if from_rotate:
                store.mark_rotated_many(urls)
            else:
                store.mark_pushed_many(urls)
        except Exception as exc:
            # [表面] 状态写不进去只报错，不改推送结果
            # [本质] 卡片已经发出去了，此时返回失败没有意义；但库里状态没更新，
            #        下一轮会把这批当待推/可轮换再捞一次 —— 重复推送比丢推更糟，
            #        所以日志必须写明「需人工确认」
            # [后果] 库故障时可能重复推送，但绝不会漏推，且日志可见
            logger.error(
                "[News] 推送已发出但状态写库失败（下一轮可能重复推送，需人工确认）: %s | %s",
                len(urls), exc,
            )
        logger.info(
            "[News] 推送成功%s：%d 条 | %s | message_id=%s",
            "（历史轮换）" if from_rotate else "",
            len(records),
            summary_title[:80],
            result.get("message_id"),
        )
        return {
            "ok": True,
            "count": len(records),
            "titles": titles,
            "message_id": result.get("message_id"),
            "rotate": from_rotate,
        }

    logger.error("[News] 推送失败 code=%s msg=%s", result.get("code"), result.get("msg"))
    return {
        "ok": False,
        "reason": result.get("msg"),
        "count": len(records),
        "titles": titles,
    }
