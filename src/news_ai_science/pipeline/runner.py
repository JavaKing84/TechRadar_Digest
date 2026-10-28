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
from news_ai_science.core.text import url_slug
from news_ai_science.stages.crawl.web import crawl_all
from news_ai_science.stages.generate.client import LLMClient
from news_ai_science.stages.generate.translator import translate
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


def collect(cfg: dict, store: NewsStore, client: LLMClient | None) -> list[dict]:
    """
    # [表面] 抓一轮新闻，备齐 N 条可推送的记录（已入库）
    # [本质] 顺序是「链接层去重 -> 必须有配图 -> 翻译摘要 -> 上传图 -> 入库」：
    #        最贵的两步排在最便宜的两道门之后；target 由 push.per_run 决定
    # [后果] 返回 0~N 条记录；一条都没有时返回空列表
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

    fresh = fresh[:target]
    uploader = build_uploader(cfg)
    if not uploader.enabled:
        logger.warning("[News] 飞书应用凭证缺失，本轮无法上传配图（卡片将无图）")

    records: list[dict] = []
    for index, art in enumerate(fresh, start=1):
        info = translate(art, client)
        payload = {
            "title_zh": info.get("title_zh") or art.title,
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
        record = {
            "url": art.url,
            "title": payload["title_zh"],
            "source_name": art.source_name,
            "info": payload,
        }
        if art.cover_image:
            img_key = uploader.upload_from_url(art.cover_image) or ""
            if not img_key:
                # [表面] 上传失败仍继续推，只是没图
                # [本质] 「文章没有配图」和「图床上传出故障」是两件事：前者按规则跳过，
                #        后者属于基础设施故障，为此整轮不推等于把一次 500 变成一次断更
                # [后果] 图挂了信息照样到，日志里有 WARNING 可查
                logger.warning("[News] 配图上传失败，本条将无图推送: %s", art.url[:70])
            payload["image_key"] = img_key

        store.save(art.url, payload["title_zh"], art.source_name, payload)
        records.append(record)
        logger.info(
            "[News] 备好 %d/%d: %s | 摘要 %d 字 | 译文 %d 字 | 配图 %s | 兜底 %s | slug=%s",
            index, len(fresh),
            payload["title_zh"][:36],
            len(payload["summary"]),
            len(payload["full_translation"]),
            "有" if payload["image_key"] else "无",
            "是" if payload["is_fallback"] else "否",
            url_slug(art.url),
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

    # [表面] 没传记录时，先把队列里的待推条目捞出来
    # [本质] --push-only 或本轮采集为空时 records 为空，直接走轮换会漏掉
    #        「pending 里明明有货」，报出「没有可推的新闻」这种自相矛盾的错
    # [后果] 待推队列永远优先于历史轮换
    if not records:
        records = store.pick_pending_many(limit)
    if not records:
        records = store.pick_rotate_many(limit, cooldown)
        from_rotate = bool(records)
    if not records:
        logger.error("[News] 没有可推的新闻（队列空且冷却期内无历史可轮换）")
        return {"ok": False, "reason": "no_record", "count": 0}

    records = records[:limit]
    card = build_multi_news_card(records)
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
        store.mark_pushed_many([record["url"] for record in records])
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
        }

    logger.error("[News] 推送失败 code=%s msg=%s", result.get("code"), result.get("msg"))
    return {
        "ok": False,
        "reason": result.get("msg"),
        "count": len(records),
        "titles": titles,
    }
