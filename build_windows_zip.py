"""SubFix Windows 打包脚本：生成 SubFix-v<版本>-Windows.zip。

用 python zipfile 而非 Compress-Archive：中文文件名会带上 UTF-8(EFS) 标志，
资源管理器 / 7-Zip / WinRAR 解压都不会乱码。

用法：python build_windows_zip.py [--version 3.3.0]
"""
import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PLUGIN_LUA = ["SubFix.lua", "生成选区字幕.lua"]
# 安装布局里所有 python 助手都进 .subfix_support/；其中 4 个在开发树位于仓库根目录
ROOT_PY = [
    "subfix_asr_transcribe.py",
    "subfix_generate_v4.py",
    "subfix_generate_v5.py",
    "subfix_generate_textnorm.py",
]
SUPPORT_FILES = [
    "subfix_generate_selection_core.lua",
    "subfix_qwen_local_manager.py",
    "subfix_process_group.py",
    "subfix_update.py",
    "setup_asr_env.cmd",
    "segmentation_profile.json",
    "segmentation_profile_v3.json",
    "segmentation_profile_v4.json",
    "doubao_credentials.json.example",
]
TOP_FILES = ["安装_SubFix.bat", "安装_SubFix_系统级.bat", "卸载_SubFix.bat", "接入本地模型.bat", "README-win.md"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="3.3.0")
    args = parser.parse_args()

    stage = Path(tempfile.mkdtemp(prefix="subfix-win-stage-"))
    try:
        plugin_dir = stage / "SubFix"
        plugin_dir.mkdir()
        support_dir = stage / ".subfix_support"
        support_dir.mkdir()

        for name in PLUGIN_LUA:
            shutil.copy2(ROOT / name, plugin_dir / name)
        for name in ROOT_PY:
            shutil.copy2(ROOT / name, support_dir / name)
        for name in SUPPORT_FILES:
            src = ROOT / ".subfix_support" / name
            if src.exists():
                shutil.copy2(src, support_dir / name)
        for name in TOP_FILES:
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, stage / name)

        missing = [n for n in ROOT_PY if not (support_dir / n).exists()]
        if missing:
            raise SystemExit(f"打包失败：核心文件缺失 {missing}")

        zip_path = ROOT / f"SubFix-v{args.version}-Windows.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    arcname = path.relative_to(stage).as_posix()
                    zf.write(path, arcname)
        print(f"已生成 {zip_path}")
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
