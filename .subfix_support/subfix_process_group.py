#!/usr/bin/env python3
"""Run one shell command in a dedicated process group."""

from __future__ import annotations

import os
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: subfix_process_group.py COMMAND", file=sys.stderr)
        return 2
    if os.name == "nt":
        # Windows 没有 setsid/进程组；Windows 端由 taskkill /T 按进程树终止，
        # 这里同步执行并透传退出码即可。
        return subprocess.call(arguments[0], shell=True)
    os.setsid()
    os.execl("/bin/sh", "sh", "-c", arguments[0])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
