# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] config/settings.py
[职责] 【公共·只读】路径常量 + 配置快照。给运维脚本 / pytest / 巡检用，
       业务代码统一走 core.config.load_config()，不要从这里拿可变配置。
[约定] 本文件不含密钥，可以进版本库；密钥在 config.yaml 或环境变量里。
=====================================================================
"""
from __future__ import annotations

import os

from news_ai_science.core.paths import (
    CONFIG_DIR,
    DATA_DIR,
    DEFAULT_CONFIG,
    LOG_DIR,
    PROJECT_ROOT,
    SRC_DIR,
)

# ---- 路径（只读）----
PROJECT_DIR = PROJECT_ROOT
SRC_ROOT = SRC_DIR
CONFIG_ROOT = CONFIG_DIR
CONFIG_FILE = DEFAULT_CONFIG
DB_DIR = DATA_DIR
LOG_ROOT = LOG_DIR

# ---- 默认值（可被 config.yaml / 环境变量覆盖）----
DEFAULT_DB_NAME = "news_ai_science.db"
DEFAULT_DB_PATH = DATA_DIR / DEFAULT_DB_NAME
DEFAULT_LOG_LEVEL = "INFO"

# ---- 部署环境变量清单（.env.example 与此一一对应）----
ENV_KEYS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "FEISHU_WEBHOOK_URL",
    "FEISHU_WEBHOOK_SECRET",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
)


def env_report() -> dict:
    """巡检用：哪些环境变量已注入（只报有没有，不打印值）。"""
    return {key: bool(os.environ.get(key)) for key in ENV_KEYS}
