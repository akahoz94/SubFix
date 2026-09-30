"""发行 ZIP 定位：版本以 SubFix.lua 的 SUBFIX_VERSION 为准，dist 里没有就退到最新一包。

为什么需要：4 个 QA 脚本各自写死过版本号（3.4.0 / 3.7.0），产品升级后它们仍在解压旧包——
绿灯其实是在给旧代码背书。写死版本号还漏不出「dist 里根本没有当前版本包」这件事。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def current_version() -> str:
    text = (ROOT / "SubFix.lua").read_text(encoding="utf-8", errors="replace")
    match = re.search(r'SUBFIX_VERSION\s*=\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError("SubFix.lua 里找不到 SUBFIX_VERSION")
    return match.group(1)


def _version_key(path: Path):
    match = re.search(r"SubFix-v(\d+)\.(\d+)\.(\d+)", path.name)
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


def resolve_zip(suffix: str = "") -> Path:
    """suffix: ""（轻量）/ "-Full" / "-Full-Max" / "-Setup.exe" 由调用方决定包型。"""
    exact = DIST / f"SubFix-v{current_version()}-Windows{suffix}.zip"
    if exact.is_file():
        return exact
    candidates = sorted(DIST.glob(f"SubFix-v*-Windows{suffix}.zip"), key=_version_key)
    if not candidates:
        raise FileNotFoundError(f"dist/ 下没有 SubFix-v*-Windows{suffix}.zip，先跑 build_windows_zip.py")
    newest = candidates[-1]
    print(f"[qa_dist] 当前版本 {current_version()} 的包未构建，回退到最新包: {newest.name}")
    return newest
