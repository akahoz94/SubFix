# SubFix v3.3.0 Windows 版

DaVinci Resolve 字幕插件的 Windows 移植版，基于上游 macOS v3.3.0（HooperH/SubFix）。
口播、现场、单段和批量字幕生成统一使用 v5 引擎，功能与 macOS 版一致。

## 发行形态

| 包 | 内容 | 适用 |
|---|---|---|
| `SubFix-v*-Windows.zip` | 插件本体，依赖系统 Python/ffmpeg | 已装 Python 的机器 |
| `SubFix-v*-Windows-Full.zip` | 内置 Python 3.11 运行时与 ffmpeg（含 DLL） | 免装依赖 |
| `SubFix-v*-Windows-Full-Max.zip` | Full + 自编译 qwen3-asr-cli 加速对齐（GGUF 模型） | 全离线 |
| `SubFix-v*-Windows-Setup.exe` | Inno Setup 安装器（简体中文向导，可静默 `/VERYSILENT`） | 常规安装 |

构建命令（`.build_cache` 缓存下载物，产物统一输出到 `dist/`）：

```
python build_windows_zip.py                       # 轻量
python build_windows_zip.py --bundle-runtime      # Full
python build_windows_zip.py --bundle-runtime --qwen-cpp <组装片段目录>   # Max
python build_windows_zip.py --installer           # Setup.exe
```

各包的详细说明与选择指南见 `docs/发行说明.md`（发行版目录随包附带）。

## 仓库目录结构

```
SubFix-win/
├── README.md / CHANGELOG.md     文档（根，GitHub 惯例）
├── docs/                        其余文档（QA 报告等）
├── installer/                   安装器脚本源（打包进 zip 顶层）
│   ├── 安装_SubFix.bat / 安装_SubFix_系统级.bat
│   ├── 卸载_SubFix.bat / 接入本地模型.bat
├── dist/                        构建产物（zip/Setup.exe，不入版本库）
├── SubFix.lua、生成选区字幕.lua、subfix_*.py、.subfix_support/   插件源码
├── scripts/windows/             Windows 构建与打包脚本
├── tests/                       pytest 套件 + 真机 QA 脚本
└── build_windows_zip.py         构建入口
```

qwen3-asr.cpp Windows 构建流程见 `scripts/windows/`（MinGW-w64 + CMake+Ninja；
对齐模型用 `scripts/convert_hf_to_gguf.py` 从本地 transformers 模型转换，无需联网）。

## 系统要求

- Windows 10/11 x64
- DaVinci Resolve（脚本菜单需可用）
- Python 3.10-3.13（安装时勾选 Add python.exe to PATH；仅首次安装 ASR 环境和 Qwen 依赖时需要）
- FFmpeg（加入 PATH 即可；或把 ffmpeg.exe 放到 `.subfix_support\bin\ffmpeg.exe`）

Windows 轻量包不内置 Python 运行时与 FFmpeg（macOS 完整包内置），这两项由系统提供。
缺失时插件会给出明确报错，按提示安装后重试。

## 安装

1. 完整解压 ZIP（不要只拖单个文件）。
2. 双击 `安装_SubFix.bat`，脚本把 `SubFix\` 与 `.subfix_support\` 复制到
   `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility`。
3. 重新打开 DaVinci Resolve，在 Workspace - Scripts - Utility 下找到 SubFix。
4. 首次使用本地 Qwen 识别前，双击
   `...\Scripts\Utility\.subfix_support\setup_asr_env.cmd` 安装识别依赖
   （走清华 PyPI 镜像，失败自动回退官方源）。

## 与 macOS 版的差异

- 字幕轨 UI 自动切换（activate_subtitle_target_track_via_ui）在上游 macOS 版也只有定义、没有调用，
  属于未接线的辅助代码；实际目标轨由插件 UI 的轨道下拉框经 Resolve API（mediaPool:AppendToTimeline 等）
  控制，Windows 上无功能损失，无需替代实现。
- 在线更新已停用（macOS 更新器只会下载 mac 资产）；请到 GitHub Releases 手动更新。
- 强制对齐加速：上游 qwen3-asr.cpp 无官方 Windows 构建，Windows 版用 MinGW-w64 自编译
  （`dist` 的 Max 包已内置 qwen3-asr-cli 与对齐 GGUF）；轻量/Full 包识别走 Python 环境（qwen-asr），
  结果一致、速度略慢。
- 后台任务通过 cmd 批处理 + PowerShell 记录 PID，取消按钮用 taskkill 终止进程树。
- 安装/卸载为 bat 脚本与 Inno Setup 安装器，对应 macOS 的 pkg 与卸载 command。

## 复用本机已有模型

首次使用本地识别默认从魔搭/HuggingFace 下载模型。若本机已有模型，双击 `接入本地模型.bat`
写入两个用户环境变量（默认指向 G:\AImodel，可按需修改 bat 后重新运行）：

- `SUBFIX_QWEN3_ASR_MODEL` = G:\AImodel\Qwen3-ASR-1.7B
- `SUBFIX_QWEN3_ALIGNER_MODEL` = G:\AImodel\Qwen3-ForcedAligner-0.6B

重启 DaVinci Resolve 后生效；删除环境变量即可恢复联网下载。
模型格式要求：transformers 本地目录（config.json + safetensors），
与魔搭 Qwen/Qwen3-ASR-1.7B、Qwen/Qwen3-ForcedAligner-0.6B 的目录结构一致。

## 卸载

双击 `卸载_SubFix.bat` 并输入 UNINSTALL 确认。达芬奇项目、`Desktop\HooperAI_Backups`
字幕备份与 `%APPDATA%\SubFix` 下的识别模型保留；envs 运行环境会被删除。

## 已知边界

- 安装/卸载 bat 与 setup_asr_env.cmd 以 GBK 编码保存（中文 Windows 控制台默认代码页 936 原生显示，
  无 chcp 切页，避免 cmd 中途换码页导致的解析错乱）。若系统开启了"Beta: 使用 Unicode UTF-8 提供全球语言支持"，
  bat 内中文提示会乱码，但安装/卸载功能不受影响（关键命令均为 ASCII）。

## 源码与构建

`build_windows_zip.py` 生成发行 ZIP（zipfile 写入，中文名带 UTF-8 标志，各解压工具不乱码）：

```
python build_windows_zip.py --version 3.3.0
```

改动相对上游的清单：SubFix.lua 与 subfix_generate_selection_core.lua 顶部各有一段
"Windows 兼容层"（shell 引用、临时目录、进程树终止、后台批处理），所有 os.execute/io.popen
调用点已按平台分支；Python 侧改动集中在 qwen 本地管理器（fcntl 锁、venv 布局、数据目录）。
