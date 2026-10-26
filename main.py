# -*- coding: utf-8 -*-
"""
=====================================================================
[文件] main.py（项目根，薄壳）
[职责] 把 src 与项目根加进 sys.path，其余全部交给 news_ai_science.main。
[说明] 保留这一层是为了 `python main.py --once` 与定时任务命令保持不变，
       同时让 src 布局可以随时改成 `pip install -e .` 而不影响调用方。
=====================================================================
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from news_ai_science.main import main  # noqa: E402  (必须在 sys.path 之后导入)

if __name__ == "__main__":
    sys.exit(main())
