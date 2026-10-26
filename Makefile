# =====================================================================
# news_ai_science · 常用命令（Windows Git Bash / WSL 通用）
# 解释器固定用项目内 venv，绝不回落到系统 python
# =====================================================================
PY      := .venv/Scripts/python.exe
PY_LIN  := .venv/bin/python

ifeq ($(OS),Windows_NT)
    PYTHON := $(PY)
else
    PYTHON := $(PY_LIN)
endif

.PHONY: help install run once dry stats push-only test lint fmt clean

help:
	@echo "make install   # 安装依赖到项目 venv（不装则用已有 .venv）"
	@echo "make once      # 抓取 + 推送一轮（定时任务跑的就是这个）"
	@echo "make dry       # 只生成卡片 JSON，不发群"
	@echo "make stats     # 看队列"
	@echo "make test      # pytest"
	@echo "make lint      # ruff 静态检查"

install:
	$(PYTHON) -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple

once:
	$(PYTHON) main.py --once

run: once

dry:
	$(PYTHON) main.py --once --dry-run

stats:
	$(PYTHON) main.py --stats

push-only:
	$(PYTHON) main.py --push-only

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

fmt:
	$(PYTHON) -m ruff format .

clean:
	find . -path ./.venv -prune -o -name "__pycache__" -type d -print0 | xargs -0 -r rm -rf
