# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] core/logger.py
[职责] 日志初始化：控制台 + 按天轮转文件。幂等，重复调用不叠加 handler。
=====================================================================
"""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from news_ai_science.core.paths import LOG_DIR

_CONFIGURED = False


def setup_logging(log_dir: str | Path | None = None, level: str = "INFO") -> None:
    """
    # [表面] 配 root logger：控制台 + 按天轮转的文件
    # [本质] 幂等保护挡住重复调用，否则一条日志打三遍
    # [后果] main.py / 定时任务 / pytest 各自调用都不会叠加 handler
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    target = Path(log_dir or LOG_DIR)
    target.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = TimedRotatingFileHandler(
        filename=str(target / "bot.log"), when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(console)
    root.addHandler(file_handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """取子 logger；setup 未调用时兜底 basicConfig，保证不哑火。"""
    if not _CONFIGURED:
        logging.basicConfig(level=logging.INFO)
    return logging.getLogger(name)
