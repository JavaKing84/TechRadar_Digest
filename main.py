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

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# [跨平台] 无头 Linux 防呆：Windows 下绝不允许碰 Display（会直接抛异常）
# [为什么用 try/except] 服务器可能没装 Xvfb；config.yaml 里 headless=true 时
#   本来就不需要 X，此时启动失败也不能把整个程序带崩。
# ---------------------------------------------------------------------------
virtual_display = None
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    try:
        from pyvirtualdisplay import Display  # noqa: E402

        virtual_display = Display(visible=0, size=(1920, 1080))
        virtual_display.start()
    except Exception:  # noqa: BLE001 - 起不来就退回纯无头，不影响主流程
        virtual_display = None

from news_ai_science.main import main  # noqa: E402  (必须在 sys.path 之后导入)

if __name__ == "__main__":
    sys.exit(main())
