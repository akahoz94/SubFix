"""组装 qwen3-asr.cpp Windows 产物到 .subfix_support 布局。

用法:python scripts/windows/stage_qwen3_cpp.py --build <build-mingw> --model <aligner.gguf> [--dest <stage>]
产物:bin/qwen3-asr-cli.exe + 全部依赖 DLL;models/qwen3-forced-aligner-0.6b-f16.gguf
"""
import argparse
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", required=True, help="qwen3-asr.cpp 构建目录(build-mingw)")
    parser.add_argument("--model", required=True, help="qwen3-forced-aligner-0.6b-*.gguf 路径")
    parser.add_argument("--dest", required=True, help="目标 .subfix_support 目录")
    args = parser.parse_args()

    build = Path(args.build)
    cli = build / "qwen3-asr-cli.exe"
    if not cli.exists():
        raise SystemExit(f"未找到 {cli}")
    model = Path(args.model)
    if not model.exists():
        raise SystemExit(f"未找到 {model}")

    dest = Path(args.dest)
    bin_dir = dest / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (dest / "models").mkdir(parents=True, exist_ok=True)

    shutil.copy2(cli, bin_dir / "qwen3-asr-cli.exe")
    copied = ["qwen3-asr-cli.exe"]
    for dll in sorted(build.glob("*.dll")):
        shutil.copy2(dll, bin_dir / dll.name)
        copied.append(dll.name)
    target_model = dest / "models" / model.name
    if not target_model.exists() or target_model.stat().st_size != model.stat().st_size:
        shutil.copy2(model, target_model)
    copied.append(model.name)
    print("已组装:")
    for name in copied:
        print("  ", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
