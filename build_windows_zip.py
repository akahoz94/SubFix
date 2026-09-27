"""SubFix Windows 打包脚本：生成轻量 zip / 内置运行时 zip(Full) / Inno Setup 安装器。

- 默认:SubFix-v<版本>-Windows.zip（轻量包,依赖系统 Python/ffmpeg）
- --bundle-runtime:追加内置 Python 3.11 运行时(nuget)与静态 ffmpeg(gyan),
  产物 SubFix-v<版本>-Windows-Full.zip;Lua/manager 的 runtime 路径自动识别
- --installer:调用 Inno Setup(ISCC) 生成 SubFix-v<版本>-Windows-Setup.exe

zip 用 python zipfile 写入:中文文件名带 UTF-8(EFS) 标志,各解压工具不乱码。
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import urllib.request
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

# 内置运行时下载源（构建机缓存目录 .build_cache 可重用已下载文件）
PYTHON_NUGET_URL = "https://www.nuget.org/api/v2/package/python/3.11.9"
PYTHON_NUGET_VERSION = "3.11.9"
# ffmpeg 主源 gyan.dev，备用源 BtbN GitHub Actions 构建（国内网络更稳）
FFMPEG_URLS = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip",
)


def build_stage(version: str, bundle_runtime: bool, qwen_cpp: Path | None = None) -> tuple[Path, list[str]]:
    stage = Path(tempfile.mkdtemp(prefix="subfix-win-stage-"))
    notes = []

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
    missing = [n for n in ROOT_PY if not (support_dir / n).exists()]
    if missing:
        shutil.rmtree(stage, ignore_errors=True)
        raise SystemExit(f"打包失败：核心文件缺失 {missing}")
    for name in TOP_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, stage / name)

    if bundle_runtime:
        cache = ROOT / ".build_cache"
        cache.mkdir(exist_ok=True)        # 1) 内置 Python（nuget 包 = zip，tools/ 是完整便携 Python，支持 venv/pip）
        nupkg = cache / f"python.{PYTHON_NUGET_VERSION}.nupkg"
        if not nupkg.exists():
            print(f"下载内置 Python {PYTHON_NUGET_VERSION} ...")
            urllib.request.urlretrieve(PYTHON_NUGET_URL, nupkg)
        runtime_dir = stage / ".subfix_support" / "runtime" / "python"
        with zipfile.ZipFile(nupkg) as zf:
            for member in zf.namelist():
                if member.startswith("tools/") and not member.endswith("/"):
                    target = runtime_dir / Path(member).relative_to("tools")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src_fh, open(target, "wb") as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh)
        if not (runtime_dir / "python.exe").exists():
            shutil.rmtree(stage, ignore_errors=True)
            raise SystemExit("内置 Python 解压失败：runtime/python/python.exe 不存在")
        notes.append(f"内置 Python {PYTHON_NUGET_VERSION}")

        # 2) 内置静态 ffmpeg（gyan essentials）
        ff_zip = cache / "ffmpeg-bundled.zip"
        if not ff_zip.exists():
            last_err = None
            for url in FFMPEG_URLS:
                print(f"下载内置 ffmpeg: {url} ...")
                try:
                    urllib.request.urlretrieve(url, ff_zip)
                    last_err = None
                    break
                except Exception as exc:
                    last_err = exc
            if last_err is not None:
                shutil.rmtree(stage, ignore_errors=True)
                raise SystemExit(f"ffmpeg 下载失败（可手动下载 {FFMPEG_URLS[0]} 存为 {cache/'ffmpeg-bundled.zip'}）：{last_err}")
        bin_dir = stage / ".subfix_support" / "bin"
        bin_dir.mkdir()
        with zipfile.ZipFile(ff_zip) as zf:
            for member in zf.namelist():
                base = Path(member).name
                if member.endswith("/") or not base:
                    continue
                # shared 构建的 exe 与依赖 DLL 同在 bin/，全部拷走
                if Path(member).parent.name == "bin" and (base.endswith(".exe") or base.endswith(".dll")):
                    with zf.open(member) as src_fh, open(bin_dir / base, "wb") as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh)
        if not (bin_dir / "ffmpeg.exe").exists():
            shutil.rmtree(stage, ignore_errors=True)
            raise SystemExit("内置 ffmpeg 解压失败：bin/ffmpeg.exe 不存在")
        dll_count = len(list(bin_dir.glob("*.dll")))
        notes.append(f"内置 ffmpeg（{dll_count} 个 DLL）")

    if qwen_cpp is not None:
        # qwen_cpp 指向已用 scripts/windows/stage_qwen3_cpp.py 组装好的 .subfix_support 片段
        src_bin = qwen_cpp / "bin"
        cli = src_bin / "qwen3-asr-cli.exe"
        if not cli.exists():
            shutil.rmtree(stage, ignore_errors=True)
            raise SystemExit(f"--qwen-cpp 缺少 {cli}；先运行 scripts/windows/stage_qwen3_cpp.py")
        support_out = stage / ".subfix_support"
        shutil.copytree(src_bin, support_out / "bin", dirs_exist_ok=True)
        (support_out / "models").mkdir(parents=True, exist_ok=True)
        dll_count = len(list((support_out / "bin").glob("*.dll")))
        ggufs = sorted((qwen_cpp / "models").glob("*.gguf")) if (qwen_cpp / "models").exists() else []
        for gguf in ggufs:
            shutil.copy2(gguf, support_out / "models" / gguf.name)
        notes.append(f"qwen3-asr-cli（{dll_count} DLL）" + (f" + 对齐模型 {ggufs[0].name}" if ggufs else ""))
    return stage, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="3.3.0")
    parser.add_argument("--bundle-runtime", action="store_true",
                        help="内置 Python 运行时与 ffmpeg（Full 包）")
    parser.add_argument("--installer", action="store_true",
                        help="用 Inno Setup 生成 Setup.exe（需要 ISCC.exe）")
    parser.add_argument("--qwen-cpp", type=Path, default=None,
                        help="qwen3-asr.cpp 组装片段目录（bin/+models/），打入包内")
    args = parser.parse_args()

    stage, notes = build_stage(args.version, args.bundle_runtime, args.qwen_cpp)
    try:
        suffix = "-Full" if args.bundle_runtime else ""
        if args.qwen_cpp:
            suffix += "-Max"
        if args.installer:
            iss = ROOT / "scripts" / "windows" / "SubFix-setup.iss"
            cmd = ["ISCC.exe", f"/DVersion={args.version}", f"/DStageDir={stage}", str(iss)]
            for candidate in (
                r"C:\Program Files (x86)\Inno Setup 6",
                str(Path.home() / "AppData/Local/Programs/Inno Setup 6"),
            ):
                if (Path(candidate) / "ISCC.exe").exists():
                    cmd[0] = str(Path(candidate) / "ISCC.exe")
                    break
            r = subprocess.run(cmd, capture_output=True)
            out = (r.stdout + r.stderr).decode("gbk", "replace")
            if r.returncode != 0:
                print(out[-1500:])
                return 1
            # ISCC 以 iss 的 OutputDir/OutputBaseFilename 落盘，重命名为正式名
            staged_exe = stage / "SubFixSetup-stage.exe"
            exe = ROOT / f"SubFix-v{args.version}-Windows-Setup.exe"
            shutil.move(str(staged_exe), exe)
            print(f"已生成 {exe}")
        else:
            zip_path = ROOT / f"SubFix-v{args.version}-Windows{suffix}.zip"
            if zip_path.exists():
                zip_path.unlink()
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(stage.rglob("*")):
                    if path.is_file():
                        arcname = path.relative_to(stage).as_posix()
                        zf.write(path, arcname)
            tag = f"（{'、'.join(notes)}）" if notes else ""
            print(f"已生成 {zip_path} {tag}")
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
