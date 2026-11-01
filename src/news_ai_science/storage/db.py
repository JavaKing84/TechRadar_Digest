# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] news_ai_science/storage/db.py
[职责] 新闻条目的 SQLite 持久化：去重、待推队列、轮换历史。
      标准库 sqlite3，不引 ORM——本项目只有一张表。

[去重键] canonical = host + path（归一化后），**绝不用标题去重**。
      标题会因「多家媒体同题」「翻译抖动」「标题党改写」而误判，
      不同语言/不同媒体的标题更是必然不同；只有 URL 路径是稳定标识。
=====================================================================
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from news_ai_science.core.logger import get_logger
from news_ai_science.core.text import canonical_url

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url         TEXT PRIMARY KEY,          -- 原始 URL（保留，便于回溯与打开原文）
    canonical   TEXT,                      -- 去重键：host+path，无 query/fragment
    title       TEXT NOT NULL,             -- 中文标题
    source_name TEXT,
    payload     TEXT NOT NULL,             -- {title_zh, summary, full_translation, summaries, image_key, cover_image, is_fallback}
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending / pushed / rejected
    created_at  TEXT NOT NULL,
    pushed_at   TEXT,
    rotate_count INTEGER NOT NULL DEFAULT 0  -- 被「历史轮换」重推过几次
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status, created_at);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class NewsStore:
    """
    # [表面] 一张 articles 表 + 几个方法
    # [本质] canonical 上的唯一索引天然去重；status 做 pending/pushed 状态机，
    #        推送失败保持 pending，下一轮自动补发
    # [后果] 同一篇文章不会被推两次（哪怕 URL 带了不同的 utm 尾巴），也不会漏掉没推成功的
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_canonical()
        self._migrate_rotate_count()

    # ---------------------------------------------------------------
    # 迁移：老库没有 canonical 列 / 老行没填 canonical
    # ---------------------------------------------------------------
    def _migrate_canonical(self) -> None:
        """
        # [表面] 补列 + 回填 + 建唯一索引
        # [本质] 切换去重键不能要求用户删库：老行的 url 就是原始 URL，
        #        按同一套规则算一遍 canonical 填回去，历史记录的去重语义保持不变
        # [后果] 新版启动后老数据照常去重，不会把历史文章当成新文章再推一遍
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(articles)")}
        if "canonical" not in cols:
            self._conn.execute("ALTER TABLE articles ADD COLUMN canonical TEXT")
            self._conn.commit()
            logger.info("[DB] 已为老库补上 canonical 列")

        pending = self._conn.execute(
            "SELECT url FROM articles WHERE canonical IS NULL OR canonical = ''"
        ).fetchall()
        if pending:
            self._conn.executemany(
                "UPDATE articles SET canonical = ? WHERE url = ?",
                [(canonical_url(row["url"]), row["url"]) for row in pending],
            )
            self._conn.commit()
            logger.info("[DB] 回填 canonical %d 条历史记录", len(pending))

        try:
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_canonical ON articles(canonical)"
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # [表面] 历史里已存在同 canonical 的重复行，唯一索引建不上
            # [本质] 这是切换去重键之前的遗留脏数据；不能用删行解决（会丢历史），
            #        退化成普通索引，靠 is_seen 的等值查询照样能去重
            # [后果] 去重功能不受影响，只是少一层数据库级兜底，日志里有 WARNING
            logger.warning("[DB] canonical 存在重复历史行，唯一索引降级为普通索引")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_canonical_nu ON articles(canonical)"
            )
            self._conn.commit()

    def _migrate_rotate_count(self) -> None:
        """
        # [表面] 给老库补上 rotate_count 列
        # [本质] 轮换次数要能跨轮次累积，否则「同一篇最多重推 2 次」无从统计；
        #        老库没有这一列，CREATE TABLE IF NOT EXISTS 不会改已存在的表
        # [后果] 老库升级后历史条目 rotate_count 从 0 起算，轮换限制立即生效
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(articles)")}
        if "rotate_count" not in cols:
            self._conn.execute(
                "ALTER TABLE articles ADD COLUMN rotate_count INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.commit()
            logger.info("[DB] 已为老库补上 rotate_count 列")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------
    # 去重
    # ---------------------------------------------------------------
    def is_seen(self, url: str) -> bool:
        """
        # [表面] 这个 URL 是否已入库（含已推）
        # [本质] 用归一化后的 canonical 比对，而不是原始 URL 字符串；
        #        同时兼容老行（原始 url 相同也算见过）
        # [后果] 带 ?utm_source=... 的同一篇文章不会被当成新的
        """
        key = canonical_url(url)
        if not key:
            return False
        cur = self._conn.execute(
            "SELECT 1 FROM articles WHERE canonical = ? OR url = ? LIMIT 1", (key, url)
        )
        return cur.fetchone() is not None

    def save(self, url: str, title: str, source_name: str, payload: dict,
             status: str = "pending") -> bool:
        """入库；canonical 已存在则忽略。返回 True 表示新入库。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO articles (url, canonical, title, source_name, payload, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, canonical_url(url), title, source_name,
             json.dumps(payload, ensure_ascii=False), status, _now()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def reject(self, url: str, title: str, source_name: str, payload: dict) -> bool:
        """
        # [表面] 把一条「翻译失败/非中文」的文章以 rejected 状态记下来
        # [本质] 不能简单跳过不入库：is_seen 靠这张表，不记下来下一轮抓取
        #        会再次捞到同一篇、再次翻译失败，白烧 LLM 额度还占掉本轮名额
        # [后果] 该 URL 永久视为"已处理"，既不会重抓，也永远不会被推送
        """
        return self.save(url, title, source_name, payload, status="rejected")

    def mark_rejected_many(self, urls: list[str]) -> None:
        """
        # [表面] 把被判定为非中文的条目改成 rejected（pending 和已推的都改）
        # [本质] 推送前的二次体检可能拦下历史脏数据；不落状态的话
        #        pick_pending_many / pick_rotate_many 下一轮还会把它们捞出来，
        #        形成「每轮都拦、每轮都刷日志」的死循环
        # [后果] 被拦下的条目只报一次警，之后既不再推也不再刷日志
        """
        if not urls:
            return
        self._conn.executemany(
            "UPDATE articles SET status = 'rejected' WHERE url = ? AND status != 'rejected'",
            [(url,) for url in urls],
        )
        self._conn.commit()

    # ---------------------------------------------------------------
    # 取待推
    # ---------------------------------------------------------------
    def pick_pending_many(self, limit: int) -> list[dict]:
        """取最早入库的 limit 条待推新闻（一张聚合卡要放的量）。"""
        if limit <= 0:
            return []
        cur = self._conn.execute(
            "SELECT * FROM articles WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        return [self._row_to_dict(row) for row in cur.fetchall()]

    def pick_pending(self) -> dict | None:
        """取最早入库的一条待推新闻。"""
        rows = self.pick_pending_many(1)
        return rows[0] if rows else None

    def pick_rotate_many(self, limit: int, cooldown_hours: float = 4,
                         max_age_days: float = 7, max_times: int = 2) -> list[dict]:
        """
        # [表面] 没有新条目时，从历史里挑最久没推的 limit 条
        # [本质] 周末源站更新慢，一直等新的会让群里断更；cooldown 挡住
        #        「刚推完又推同一条」。但只有 cooldown 是不够的：
        #        抓取长期失败时，同一批老新闻会被无限重推，群里看几天就腻了。
        #        所以再叠两道限制——
        #          ① 入库超过 max_age_days 天的不再进池（古董新闻没有推送价值）
        #          ② rotate_count 达到 max_times 的不再进池（同一篇最多重推 N 次）
        #        max_age_days / max_times 传 0 或负数表示关掉该限制。
        # [后果] 轮换只在「最近一周的存货」里转，且每篇最多出现 max_times 次；
        #        用完就彻底退出，不会无限循环。池子空了宁可断更，也不推古董。
        """
        if limit <= 0:
            return []
        now = datetime.now()
        where = ["status = 'pushed'"]
        params: list[object] = []

        # [表面] 冷却期：上一次推送距今不足 cooldown 小时的不选
        where.append("(pushed_at IS NULL OR pushed_at <= ?)")
        params.append((now - timedelta(hours=cooldown_hours)).strftime("%Y-%m-%d %H:%M:%S"))

        # [表面] 年龄上限：入库太久的直接排除
        # [本质] 用 created_at（入库时间）而不是 pushed_at：
        #        pushed_at 每轮都会刷新，拿它算年龄会让老新闻永远「看起来是新的」
        if max_age_days and max_age_days > 0:
            where.append("created_at >= ?")
            params.append((now - timedelta(days=float(max_age_days))).strftime("%Y-%m-%d %H:%M:%S"))

        # [表面] 重推次数上限
        if max_times and max_times > 0:
            where.append("COALESCE(rotate_count, 0) < ?")
            params.append(int(max_times))

        sql = (
            "SELECT * FROM articles WHERE " + " AND ".join(where)
            + " ORDER BY pushed_at ASC LIMIT ?"
        )
        params.append(limit)
        cur = self._conn.execute(sql, tuple(params))
        return [self._row_to_dict(row) for row in cur.fetchall()]

    def pick_rotate(self, cooldown_hours: float = 4,
                    max_age_days: float = 7, max_times: int = 2) -> dict | None:
        """
        # [表面] 没有新条目时，从历史里挑最久没推的一条
        # [本质] 与 pick_rotate_many 同一套筛选（冷却 / 年龄 / 重推次数）
        # [后果] 单条轮换与整卡轮换的选条口径一致
        """
        rows = self.pick_rotate_many(1, cooldown_hours, max_age_days, max_times)
        return rows[0] if rows else None

    # ---------------------------------------------------------------
    # 状态流转
    # ---------------------------------------------------------------
    def mark_pushed_many(self, urls: list[str]) -> None:
        """一张聚合卡里可能有多条，全部标 pushed（否则下一轮会重复推）。"""
        if not urls:
            return
        now = _now()
        self._conn.executemany(
            "UPDATE articles SET status = 'pushed', pushed_at = ? WHERE url = ?",
            [(now, url) for url in urls],
        )
        self._conn.commit()

    def mark_pushed(self, url: str) -> None:
        self.mark_pushed_many([url])

    def mark_rotated_many(self, urls: list[str]) -> None:
        """
        # [表面] 轮换推送成功后，除了刷新 pushed_at，再把 rotate_count 加一
        # [本质] 新文章的首次推送与「旧文重推」必须分开记：
        #        前者 rotate_count 保持 0，后者累加，pick_rotate_many 才有依据
        #        把重推次数限制在 max_times 之内
        # [后果] 同一篇旧闻最多被轮换推送 max_times 次，之后永久退出轮换池
        """
        if not urls:
            return
        now = _now()
        self._conn.executemany(
            "UPDATE articles SET status = 'pushed', pushed_at = ?,"
            " rotate_count = COALESCE(rotate_count, 0) + 1 WHERE url = ?",
            [(now, url) for url in urls],
        )
        self._conn.commit()

    def stats(self) -> dict:
        cur = self._conn.execute("SELECT status, COUNT(*) AS n FROM articles GROUP BY status")
        data = {row["status"]: row["n"] for row in cur.fetchall()}
        return {
            "pending": data.get("pending", 0),
            "pushed": data.get("pushed", 0),
            "rejected": data.get("rejected", 0),
        }

    @staticmethod
    def _row_to_dict(row) -> dict:
        item = dict(row)
        try:
            item["info"] = json.loads(item.get("payload") or "{}")
        except ValueError:
            item["info"] = {}
        return item
