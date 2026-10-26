# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] core/paths.py
[职责] 全项目唯一的路径真相：项目根 / 配置 / 数据 / 日志。
[约定] 其它模块一律从这里取路径，不许自己 Path(__file__).parent 猜。
=====================================================================
"""
from __future__ import annotations

from pathlib import Path

# src/news_ai_science/core/paths.py -> core -> src/news_ai_science -> src -> 项目根
CORE_DIR = Path(__file__).resolve().parent
PKG_DIR = CORE_DIR.parent
SRC_DIR = PKG_DIR.parent
PROJECT_ROOT = SRC_DIR.parent

CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"


def resolve_path(value: str | Path | None, default: str) -> Path:
    """
    # [表面] 相对路径按项目根解析，绝对路径原样返回
    # [本质] 定时任务/手动运行的 cwd 未必是项目根，相对路径会飘到别处
    # [后果] 无论在哪个目录启动，数据库和日志都落在项目内
    """
    path = Path(value or default)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def ensure_dirs(*dirs: Path) -> None:
    for d in dirs or (DATA_DIR, LOG_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)
