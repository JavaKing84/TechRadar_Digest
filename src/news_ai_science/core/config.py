# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] core/config.py
[职责] 读配置：config/config.yaml（运行参数）+ config/sources.yaml（新闻源清单）。
        环境变量优先于 yaml，部署时密钥走 .env 或真实环境变量。
[要点] 新闻源单独一个文件：改源不改代码，也不用碰带密钥的 config.yaml。
=====================================================================
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from news_ai_science.core.errors import ConfigError
from news_ai_science.core.paths import CONFIG_DIR, DEFAULT_CONFIG, PROJECT_ROOT, resolve_path

SOURCES_FILE = CONFIG_DIR / "sources.yaml"

ENV_OVERRIDES = {
    "LLM_BASE_URL": ("llm", "base_url"),
    "LLM_API_KEY": ("llm", "api_key"),
    "LLM_MODEL": ("llm", "model"),
    "FEISHU_WEBHOOK_URL": (None, "webhook_url"),
    "FEISHU_WEBHOOK_SECRET": (None, "webhook_secret"),
    "FEISHU_APP_ID": ("feishu_app", "app_id"),
    "FEISHU_APP_SECRET": ("feishu_app", "app_secret"),
}


def load_config(path: str | Path | None = None) -> dict:
    """读 yaml -> env 覆盖 -> 合并新闻源 -> 校验 -> 路径解析。"""
    _load_dotenv()
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    data = _read_yaml(cfg_path, required=True)

    _apply_env(data)

    if not str(data.get("webhook_url") or "").strip():
        raise ConfigError("缺少 webhook_url（config.yaml 或 FEISHU_WEBHOOK_URL）")
    llm = data.get("llm") or {}
    for key in ("base_url", "model"):
        if not str(llm.get(key) or "").strip():
            raise ConfigError(f"缺少 llm.{key}")

    # [表面] 新闻源优先取 config/sources.yaml，没有才用 config.yaml 内联的
    # [本质] 源清单要经常改（站点反爬、换源），不该混在带密钥的主配置里
    # [后果] 两个文件各自单一职责，改源不会误碰密钥
    sources = _read_yaml(SOURCES_FILE, required=False).get("sources")
    if sources is None:
        sources = data.get("news_sources") or []
    enabled = [s for s in sources if s.get("enabled", True)]
    if not enabled:
        raise ConfigError("没有任何启用的新闻源（检查 config/sources.yaml）")
    data["news_sources"] = sources
    data["enabled_sources"] = enabled

    storage = data.setdefault("storage", {})
    storage["db_path"] = str(resolve_path(storage.get("db_path"), "data/news_ai_science.db"))

    logging_cfg = data.setdefault("logging", {})
    logging_cfg["dir"] = str(resolve_path(logging_cfg.get("dir"), "logs"))

    data["project_dir"] = str(PROJECT_ROOT)
    data["config_path"] = str(cfg_path)
    return data


def _read_yaml(path: Path, *, required: bool) -> dict:
    if not path.exists():
        if required:
            raise ConfigError(f"配置文件不存在: {path}")
        return {}
    with path.open("r", encoding="utf-8") as fp:
        loaded = yaml.safe_load(fp) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"配置文件格式不对，顶层必须是映射: {path}")
    return loaded


def _apply_env(data: dict) -> None:
    """按 ENV_OVERRIDES 把环境变量写回配置树；未设置的环境变量不动。"""
    for env_key, (section, field) in ENV_OVERRIDES.items():
        value = os.environ.get(env_key)
        if not value:
            continue
        if section is None:
            data[field] = value
        else:
            data.setdefault(section, {})[field] = value


def _load_dotenv() -> None:
    """
    # [表面] 项目根有 .env 就载入
    # [本质] 部署走真实环境变量，本地开发用 .env；override=False 让
    #        「环境里已有的值」优先，不会被文件覆盖。
    #        SKIP_DOTENV=1 供测试隔离：否则工作目录里真实的 .env 会渗进
    #        临时配置，让「缺 webhook 应报错」这类用例永远不触发
    # [后果] 同一份代码在本地与服务器行为一致，测试结果不随本机 .env 漂移
    """
    if os.environ.get("SKIP_DOTENV") == "1":
        return
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_file, override=False)
