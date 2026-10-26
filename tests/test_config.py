# -*- coding: utf-8 -*-
"""core.config：新闻源从 sources.yaml 读取并过滤 enabled。"""
from __future__ import annotations

import pytest

from news_ai_science.core.config import SOURCES_FILE, load_config
from news_ai_science.core.errors import ConfigError

# [说明] 真实 config/config.yaml 不含任何密钥（webhook 与 llm.* 全部留空，
#        真实值来自项目根的 .env）。读真实配置的用例必须显式注入占位值，
#        不能依赖本机 .env 存在——否则 CI / 新机器上会集体失败。
FAKE_ENV = {
    "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test-only",
    "LLM_BASE_URL": "http://127.0.0.1:8000/v1",
    "LLM_API_KEY": "test-key",
    "LLM_MODEL": "test-model",
}


@pytest.fixture
def real_cfg(monkeypatch):
    """加载真实 config/ 目录（含 sources.yaml），密钥用占位值。"""
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)
    return load_config()


def test_sources_yaml_exists_and_is_used(real_cfg):
    assert SOURCES_FILE.exists(), "config/sources.yaml 必须存在"
    assert real_cfg["enabled_sources"], "至少要有一个启用的新闻源"
    assert all(s.get("enabled", True) for s in real_cfg["enabled_sources"])
    assert len(real_cfg["news_sources"]) >= len(real_cfg["enabled_sources"])


def test_disabled_sources_are_filtered(real_cfg):
    ids = {s["id"] for s in real_cfg["enabled_sources"]}
    for disabled in ("arstechnica", "theverge"):
        assert disabled not in ids, f"{disabled} 当前应是停用状态"


def test_relative_paths_resolve_to_project_root(real_cfg):
    from pathlib import Path

    from news_ai_science.core.paths import PROJECT_ROOT

    db_path = Path(real_cfg["storage"]["db_path"])
    assert db_path.is_absolute()
    assert db_path.parent == PROJECT_ROOT / "data"


def test_missing_webhook_raises(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('llm:\n  base_url: "x"\n  model: "m"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)
