# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] main.py
[职责] 新闻机器人 CLI：解析参数 -> 加载配置 -> 起一轮 Pipeline。
       业务逻辑全在 pipeline/runner.py，本文件只管「怎么被调用」。

用法：
  python main.py --once               # 跑一轮：抓取 + 摘要 + 推送
  python main.py --once --dry-run     # 只生成卡片 JSON，不发送（调试用）
  python main.py --once --no-push     # 只抓取入库，不推送
  python main.py --push-only          # 不抓取，只把队列里的待推条目推出去
  python main.py --stats              # 看队列状态

[硬约束] 只用 Camoufox 抓取；无配图的新闻直接跳过，绝不用乱图凑数；
      失败抛 CrawlError，不降级 RSS/requests。
=====================================================================
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from news_ai_science.core.config import load_config
from news_ai_science.core.errors import ConfigError, CrawlError
from news_ai_science.core.logger import get_logger, setup_logging
from news_ai_science.pipeline import runner
from news_ai_science.stages.push.feishu_sender import FeishuSender, RateLimiter
from news_ai_science.storage.db import NewsStore

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 科技新闻机器人（news_ai_science）")
    parser.add_argument("--once", action="store_true", help="跑一轮：抓取 + 摘要 + 推送")
    parser.add_argument("--dry-run", action="store_true", help="只生成卡片 JSON，不发送")
    parser.add_argument("--no-push", action="store_true", help="只抓取入库，不推送")
    parser.add_argument("--push-only", action="store_true", help="不抓取，只把队列里的待推条目推出去")
    parser.add_argument("--stats", action="store_true", help="打印队列状态后退出")
    parser.add_argument(
        "--test-push",
        action="store_true",
        help="只往目标群发一条连通性测试文本，验证 webhook 到底落在哪个群",
    )
    parser.add_argument("--config", default=None, help="指定配置文件路径")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"[FATAL] 配置错误: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg["logging"]["dir"], cfg["logging"].get("level", "INFO"))

    # [表面] 数据库初始化失败就直接退出，不往下走
    # [本质] NewsStore 构造会 mkdir + executescript + 两轮迁移，任何一步失败
    #        （库文件损坏、目录不可写、磁盘满）都抛 sqlite3.Error / OSError；
    #        不包住的话进程裸崩，日志里只剩 traceback，cron 也只看到非零退出码
    # [后果] 打出明确 ERROR + 退出码 3，运维一眼看出是存储层而不是抓取
    try:
        store = NewsStore(cfg["storage"]["db_path"])
    except Exception as exc:
        logger.error("[News] 数据库初始化失败（文件损坏 / 目录不可写 / 磁盘满）: %s",
                     exc, exc_info=True)
        return 3

    try:
        if args.stats:
            print(json.dumps(store.stats(), ensure_ascii=False))
            return 0

        # [表面] 发一条纯文本探针，确认消息实际落在哪个群
        # [本质] webhook 绑群是飞书侧的绑定关系，代码里看不到群名；
        #        只有真发一条，人才能在群里确认，比看日志可靠
        # [后果] 换机器人/换群后先跑一次 --test-push，避免整卡推错群
        if args.test_push:
            chat_name = cfg.get("chat_name") or ""
            sender = FeishuSender(
                cfg["webhook_url"],
                cfg.get("webhook_secret") or "",
                RateLimiter(0.5),
                chat_name=chat_name,
            )
            text = (
                "链路自检成功，后续新闻将推送到本群。\n"
                f"[{cfg.get('display_name', '机器人')}] 目标群「{chat_name or '未命名群'}」"
                f" | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "若本群不是 iDeal工作室，请把项目根 .env 里的 FEISHU_WEBHOOK_URL "
                "换成 iDeal工作室群的机器人地址。"
            )
            result = sender.send_text(text)
            print(
                "[TEST] 目标群「%s」 code=%s msg=%s message_id=%s"
                % (chat_name or "未命名群", result.get("code"), result.get("msg"),
                   result.get("message_id"))
            )
            return 0 if result.get("code") == 0 else 1

        if not (args.once or args.dry_run or args.no_push or args.push_only):
            parser.print_help()
            return 2

        logger.info("=" * 60)
        logger.info("[News] 开始运行 %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        client = runner.build_client(cfg)

        # [表面] 采集失败不阻断推送
        # [本质] 硬约束要求抓不到就抛 CrawlError，但「抛」不等于「本轮什么都不做」——
        #        库里还有没推完的，先推出去再报错
        # [后果] 抓取故障时用户仍收到内容，日志里有明确 ERROR 堆栈
        crawl_failed = False
        records: list[dict] = []
        if args.push_only:
            logger.info("[News] --push-only：跳过采集，直接推队列里的待推条目")
        else:
            try:
                records = runner.collect(cfg, store, client)
            except CrawlError as exc:
                crawl_failed = True
                logger.error("[News] 采集失败（CrawlError）: %s", exc, exc_info=True)
            except Exception as exc:
                # [表面] 采集阶段的任何未预期异常都不许冒到最外层
                # [本质] 走到这里说明是代码缺陷或存储故障；裸崩会让「本轮已经
                #        入库的条目」失去推送机会，而它们本来是能推的
                # [后果] 记全栈 + 继续走推送（推队列里的存量），机器人不停摆
                crawl_failed = True
                logger.error("[News] 采集阶段未预期异常，本轮跳过采集: %s", exc, exc_info=True)

        pushed = {"ok": False, "reason": "skipped"}
        if not args.no_push:
            try:
                pushed = runner.push(cfg, store, records, dry_run=args.dry_run)
            except Exception as exc:
                # [表面] 推送阶段异常兜住，不让进程崩
                # [本质] 条目此刻仍是 pending（状态写库在发送成功之后），
                #        下一轮会自动重发，不会因此丢内容
                # [后果] 本轮返回失败码，但数据不丢
                logger.error("[News] 推送阶段未预期异常: %s", exc, exc_info=True)
                pushed = {"ok": False, "reason": f"exception: {exc}", "count": 0}

        stats = store.stats()
        logger.info(
            "[News] 完成 | 新入库 %d 条 | 推送 %s(%s 条%s) | 队列 pending=%d pushed=%d rejected=%d%s",
            len(records),
            "成功" if pushed.get("ok") else f"失败({pushed.get('reason')})",
            pushed.get("count", 0),
            "，历史轮换" if pushed.get("rotate") else "",
            stats["pending"],
            stats["pushed"],
            stats["rejected"],
            " | 采集异常" if crawl_failed else "",
        )
        return 0 if pushed.get("ok") else 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
