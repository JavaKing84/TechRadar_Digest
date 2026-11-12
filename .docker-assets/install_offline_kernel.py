#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线安装 Camoufox 浏览器内核（Windows / Linux / macOS 通用）。

[为什么需要] 目标环境访问不了 GitHub，而 `python -m camoufox fetch` 必须联网。
    本脚本把官方 zip 摆成 SDK 期望的多版本布局，让 Camoufox 认为"已经装好了"，
    从而完全不联网。

[调用] python install_offline_kernel.py /path/to/camoufox-<ver>-<os>.x86_64.zip
       例：camoufox-156.0.1-beta.36-win.x86_64.zip（本机 Windows）
           camoufox-156.0.1-beta.36-lin.x86_64.zip（Docker 镜像）
       文件名不规范时（如 camoufox-lin.zip）回退到 SDK 自带的
       browser-pin.json 取版本，因此两种命名都能用。

[还原出的布局，与 `camoufox fetch` 完全一致]
    <cache>/.0.5_FLAG                              兼容标记，缺失会被 SDK 清空缓存
    <cache>/config.json                            {"active_version": "browsers/official/<ver>"}
    <cache>/browsers/official/<ver>/...            浏览器文件
    <cache>/browsers/official/<ver>/version.json   version / build / interface 三字段

  注意 <cache> 由 SDK 自己算，不要写死：
    Linux   ~/.cache/camoufox
    Windows %LOCALAPPDATA%\\camoufox\\camoufox\\Cache
    macOS   ~/Library/Caches/camoufox
  用 `python -m camoufox path` 可以直接打印出来。

[跨平台要点] 各平台内核包里的启动文件不同（win: camoufox.exe / lin: camoufox-bin），
  老版本脚本写死了 camoufox-bin，所以 Windows 上必然报"内核包不完整"。
  这里改为读 SDK 的 LAUNCH_FILE 表，按当前平台校验。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = "official"  # 对应 browser-pin.json 里 repo_name("Official") 的小写形式


def pinned_version() -> tuple[str, str]:
    """读取 SDK 自带的 browser-pin.json，拿到这一版库钉住的内核版本。"""
    import camoufox

    pin = Path(camoufox.__file__).parent / "browser-pin.json"
    data = json.loads(pin.read_text(encoding="utf-8"))
    version = str(data.get("version") or "")
    build = str(data.get("build") or "")
    if not version:
        raise SystemExit(f"browser-pin.json 无 version 字段，无法确定内核版本：{pin}")
    return version, build


def parse_ver(zip_name: str) -> tuple[str, str]:
    """从文件名解析 version / build；解析不到就用 SDK 钉住的版本兜底。

    命名形如 camoufox-156.0.1-beta.36-win.x86_64.zip，
    平台段 win / lin / mac 任一都认。
    """
    m = re.match(r"camoufox-(.+?)-(?:lin|win|mac)[.\-]", zip_name)
    if m:
        full = m.group(1)
        version, _, build = full.partition("-")
        return version, build

    version, build = pinned_version()
    print(f"[warn] 文件名 '{zip_name}' 不含版本号，回退到 SDK 钉住的 {version}-{build}")
    return version, build


def restore_exec_bits(root: Path) -> int:
    """补回可执行权限（仅 POSIX 需要）。

    [为什么必须做] Python 的 zipfile.extractall 完全不保留 zip 里的 POSIX 权限位，
    解出来的 camoufox-bin 是 0644，Playwright 启动时报
        spawn .../camoufox-bin EACCES
    真实故障，实测复现。解决办法是扫一遍文件头：ELF 或带 #! 的都 chmod 755。
    Windows 的 .exe 不依赖权限位，直接跳过。
    """
    if sys.platform.startswith("win"):
        return 0

    fixed = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        try:
            with open(p, "rb") as f:
                magic = f.read(4)
        except OSError:
            continue
        if magic[:4] == b"\x7fELF" or magic[:2] == b"#!":
            p.chmod(0o755)
            fixed += 1
    return fixed


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    zpath = Path(sys.argv[1]).resolve()
    if not zpath.is_file():
        raise SystemExit(f"内核包不存在：{zpath}")

    # SDK 自己决定缓存根目录与平台，避免 Linux/Windows 路径差异
    from camoufox.pkgman import INSTALL_DIR, LAUNCH_FILE, OS_NAME

    launch_entry = LAUNCH_FILE[OS_NAME]

    version, build = parse_ver(zpath.name)
    full = f"{version}-{build}" if build else version
    target = INSTALL_DIR / "browsers" / REPO / full

    print(f"[info] 平台 {OS_NAME}，内核 {full}，目标目录 {target}")

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        entries = list(Path(tmp).iterdir())
        src = entries[0] if len(entries) == 1 and entries[0].is_dir() else Path(tmp)
        for item in src.iterdir():
            shutil.move(str(item), str(target / item.name))

    n = restore_exec_bits(target)
    if n:
        print(f"[OK] 已补回 {n} 个可执行文件的权限位")

    # zip 里不带 version.json，必须自己写，否则 SDK 判定为"未安装"
    (target / "version.json").write_text(
        json.dumps(
            {"version": version, "build": build, "interface": 1, "prerelease": bool(build)},
            indent=2,
        ),
        encoding="utf-8",
    )
    (INSTALL_DIR / "config.json").write_text(
        json.dumps({"active_version": f"browsers/{REPO}/{full}"}, indent=2),
        encoding="utf-8",
    )
    (INSTALL_DIR / ".0.5_FLAG").touch()

    # 双保险：当前平台的启动文件必须存在且可执行
    exe = target / launch_entry
    if not exe.exists():
        raise SystemExit(
            f"[FAIL] 内核包不完整，缺少 {launch_entry}：{target}\n"
            f"       很可能把 Linux 包（*-lin*.zip）装到了 Windows / macOS 上。"
        )
    if not sys.platform.startswith("win"):
        exe.chmod(0o755)

    print(f"[OK] 内核已离线安装：{target}")
    print(f"[OK] active_version = browsers/{REPO}/{full}")
    print(f"[OK] 启动文件 = {launch_entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
