#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线安装 Camoufox 的 fpgen 指纹模型。

[为什么需要] Camoufox 在**每次首次启动浏览器**时都会调 ensure_fpgen_model()，
    从 GitHub 下载一个 1.5 MB 的模型包（model-2/2026）。实测容器里第一次
    `Camoufox(...)` 就会打印 "Downloading fpgen model model-2/2026: https://github.com/..."
    。学校服务器出不了 GitHub 时这一步会抛 FpgenModelError，浏览器根本起不来。
    模型本身只有 1.5 MB，直接随仓库一起带过去最省事。

[调用] python install_fpgen_model.py /path/to/fpgen-model-release.zip

[行为] 完全复用 camoufox 自己钉住的 sha256 做校验（外层 + 三个成员），
    布局与 camoufox.fpgen_model._install 一致：
        <site-packages>/fpgen/data/{values.dat.zst, values.json.zst,
                                   fingerprint-network.json.zst, .pinned-model}
    mtime 置为 PINNED_MTIME，fpgen 才认为模型是"新的"、不会再去联网刷新。
"""
from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    zpath = Path(sys.argv[1]).resolve()
    if not zpath.is_file():
        raise SystemExit(f"模型包不存在：{zpath}")

    from camoufox.fpgen_model import PIN, PINNED_MTIME, STAMP, fpgen_data_dir
    from camoufox.pkgman import write_atomic

    blob = zpath.read_bytes()
    got = hashlib.sha256(blob).hexdigest()
    if got != PIN["sha256"]:
        raise SystemExit(
            f"[FAIL] 模型包 sha256 不匹配\n  实际 {got}\n  期望 {PIN['sha256']}"
        )

    data_dir = fpgen_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    with ZipFile(io.BytesIO(blob)) as zf:
        names = sorted(zf.namelist())
        if names != sorted(PIN["file_sha256"]):
            raise SystemExit(f"[FAIL] 包内文件不符：{names}")
        for name, expected in PIN["file_sha256"].items():
            buf = zf.read(name)
            if hashlib.sha256(buf).hexdigest() != expected:
                raise SystemExit(f"[FAIL] 成员 {name} sha256 不符")
            write_atomic(data_dir / name, buf, mtime=PINNED_MTIME)

    # 印章最后写：只有全部落盘成功，fpgen 才认为装完了
    (data_dir / STAMP).write_text(PIN["sha256"] + "\n", encoding="utf-8")

    print(f"[OK] fpgen 模型已离线安装：{data_dir}")
    print(f"[OK] 版本 {PIN['tag']}（{len(PIN['file_sha256'])} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
