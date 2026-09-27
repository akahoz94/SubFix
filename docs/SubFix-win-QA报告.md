# SubFix v3.3.0 Windows 版 QA 报告

- 报告日期：2026-09-28
- 仓库：`G:\Agent\.zcode\.zcode\workspace\default\SubFix-win`
- 基线 revision：`bb40f3d`（09-28 02:07 mac 清理后）；本报告含后续测试侧修复（未提交，见 §5）
- 测试环境：Python 3.10 临时 venv（lupa 2.8 / pytest 9.1.1），Windows 10/11 x64，控制台代码页 65001

---

## 1. 结论摘要

| 项 | 结论 |
|---|---|
| 产品代码功能 | **全部通过**，QA 未发现产品代码缺陷 |
| 自动化测试 | 修复后 **38 passed / 1 skipped**（原 33 passed / 5 failed / 1 error） |
| 失败归因 | 6 项失败均为**测试侧问题**（macOS 移植遗留），非产品代码缺陷 |
| 发行一致性 | **存在缺口**：Full/Max 包未随 mac 清理重建，仍含已删除的 README-win.md |
| 复核 verdict | **pass**（修复 diff 无新引入缺陷） |

---

## 2. QA 执行结果

### 2.1 自动化测试套件

| 套件 | 覆盖 | 结果 |
|---|---|---|
| pytest：test_qwen_download_sources.py | 国内下载源、镜像回退、legacy 复用 | 修复后全部通过 |
| pytest：test_qwen_environment_location.py | 路径布局、安装锁、脚本树隔离 | 修复后全部通过 |
| pytest：test_v5_boundary_protection.py / test_v5_onset_protection.py | v5 时间边界保护（fps 24-60） | 修复后全部通过 |
| pytest：test_background_launch.py | POSIX 后台启动模板 | 修复后正确跳过（Windows 由 test_win_compat 覆盖） |
| pytest：test_ai_script_conversion.py | AI 任务路由（macOS Resolve LuaJIT） | 1 passed + 19 skipped（正确自跳过） |
| test_win_compat.py | Lua 语法、兼容层、批处理端到端 | 17/17 通过 |
| qa_python_entries.py | Python 入口、锁互斥、更新器、ffmpeg | 13/13 通过 |
| qa_lua_harness.py | 发行 ZIP 安装布局全量加载、kill_tree | 全部通过 |
| qa_ffi_paths.py | 中文路径 FFI UTF-16 根治 | 全部通过 |
| qa_install_bats.py | 真实安装→覆盖→取消→确认→空卸载 | 功能全部通过；10 项中文渲染检查因代码页 65001 失败（见 §3.2） |

### 2.2 静态与一致性检查

- Setup.exe：合法 PE（MZ 头）+ Inno Setup 标记
- 轻量 ZIP：20 条目，无 mac 残留，README.md 与仓库一致；bat/cmd 逐字节一致（GBK、CRLF、无 BOM、无 chcp）
- 三个 Lua 文件 LuaJIT 5.1 语法编译通过
- 测试后 DaVinci Resolve Scripts/Utility 目录完全恢复原状，无 SubFix 残留

---

## 3. 失败归因（修复前）

### 3.1 测试侧缺陷（6 项，均已在 §5 修复）

| # | 失败项 | 根因（触发→机制→症状） | 类型 |
|---|---|---|---|
| 1 | test_lua_manager_launches_suppress_runtime_bytecode_writes | Windows 移植改用 `-B` 标志抑制字节码写入（与 PYTHONDONTWRITEBYTECODE=1 等价），测试仍要求旧 env 变量写法（grep 证实全文件无该变量） | 断言过时 |
| 2 | test_real_local_pip_install_does_not_modify_script_tree | Windows 上 `base_python` 回退 `sys.executable`（已存在）→ `mkdir` 抛 FileExistsError；symlink 还需特权 | macOS 布局假设 |
| 3 | test_completed_legacy_environment_still_works_without_mutation | 测试硬编码 `bin/python`（macOS 布局），管理器按 `os.name` 用 `Scripts/python.exe` → legacy 永不识别为 ready | macOS 布局假设 |
| 4 | test_installed_legacy_environment_is_not_migrated_or_redownloaded | 同上 | macOS 布局假设 |
| 5 | test_pipeline_extends_display_only_after_refinement_and_keeps_long_pauses[True] | exec 代码片段含 `refined_rows: list[dict[str, Any]]`，命名空间未注入 `Any`（仅 v5_mode=True 分支触发 annotation 求值） | 测试夹具缺陷 |
| 6 | test_background_launch.py | macOS 专属 `/bin/sh` 模板，Windows 无跳过保护 → FileNotFoundError | 平台不适用未隔离 |

与修复前 `.pytest_cache` lastfailed（cpython-314 历史运行）一致，证明为持续存在的测试侧问题而非环境偶发。

### 3.2 环境相关（非缺陷，已记录边界）

qa_install_bats.py 的 10 项"无乱码字符 / 中文提示"检查失败：本机控制台代码页为 **65001**（系统开启"Beta: 使用 Unicode UTF-8 提供全球语言支持"），bat 为 GBK 编码，中文提示在 cmd 中显示为乱码。**安装/卸载功能全部正常**（落盘文件与仓库逐字节一致、目录删除正确、无命令解析错误），与 README「已知边界」描述一致。注意：本机用户实际双击 bat 时中文提示会乱码，但功能不受影响。

---

## 4. 代码库梳理（架构心智模型）

### 4.1 模块责任

| 模块 | 责任 | 入口 |
|---|---|---|
| SubFix.lua | 插件 UI + Windows 兼容层（FFI UTF-16、后台批处理、进程树终止） | Resolve 脚本菜单 |
| 生成选区字幕.lua / subfix_generate_selection_core.lua | 字幕生成核心（v5 时间边界、AI 任务路由、qwen 命令构建） | 选区/整轨生成 |
| subfix_asr_transcribe.py | ASR 编排（豆包云端/本地 Qwen），v4→v5 时序管线 | 识别入口 |
| subfix_generate_v4.py / v5.py / textnorm.py | 时间边界细化、显示延长、文本规范 | 被 transcribe 调用 |
| subfix_qwen_local_manager.py | 本地 Qwen 环境/模型管理（路径布局、双源下载、跨平台锁） | CLI status/install |
| build_windows_zip.py + scripts/windows/ | 四种发行形态构建（轻量/Full/Max/Setup.exe） | 命令行 |
| tests/ | 4 个 pytest 文件 + 4 个真机 QA 脚本（Windows 适用） | pytest / 直接运行 |

### 4.2 核心链路（confirmed，真机 QA 验证）

1. **字幕生成**：Resolve UI → SubFix.lua → subfix_generate_selection_core.lua → subfix_asr_transcribe.py → v5 边界细化 → 回写时间线
2. **本地 Qwen 安装**：Lua `build_qwen_install_command`（`-B`）→ `install()` 首查 `inspect_install()`（新布局 data_root → legacy root/envs 回退，`os.name` 分支 Scripts/bin）→ venv + 清华镜像依赖 → 魔搭→HF 模型下载 → ready marker
3. **Windows 后台任务**：`subfix_write_bg_batch`（chcp 65001、清 PYTHONHOME/PATH、CIM ParentProcessId 记 PID）→ `subfix_launch_bg_batch`（CreateProcessW 直启 `cmd /c`，短路径兜底）→ 取消走 `taskkill /T /F`

### 4.3 责任闭合卡（跨进程主张）

ASR 后台任务 | 触发：Lua 生成流程 | 装配：`SUBFIX_IS_WINDOWS` 分支 | 执行：CreateProcessW + batch | 成功观察点：stdout/exit/done/pid 文件 + UI Timer 轮询 progress.json | 失败：worker_python 缺失返回 false+提示 | 取消：taskkill 树终止（qa_lua_harness 实测 cmd 及其子进程被杀） | **status: confirmed**

### 4.4 关键反例与未知

- legacy marker 不会认证不完整新环境（有测试覆盖）
- bat 中文在 65001 代码页乱码但功能正常（已记录边界）
- **发行一致性缺口：Full/Max 包未随 mac 清理重建，仍含已删除的 README-win.md**
- 未知：本地 Qwen 全流程实测（真实下载+推理）与 Resolve 内 UI 交互未在本机验证（README 亦声明）

---

## 5. 修复清单（诊断与修复）

产品代码**零改动**，6 处均为测试侧修复：

| # | 文件 | 修复 |
|---|---|---|
| 1 | tests/test_qwen_environment_location.py | 断言收窄为 `-B`（等价机制，保留契约语义） |
| 2 | tests/test_qwen_environment_location.py | mkdir/symlink 仅 posix 执行（Windows base_python 回退 sys.executable，无需软链） |
| 3 | tests/test_qwen_environment_location.py | legacy python 路径按 `os.name` 选 Scripts/python.exe |
| 4 | tests/test_qwen_download_sources.py | 同上（另补 `import os`） |
| 5 | tests/test_v5_boundary_protection.py | namespace 注入 `Any`（`from typing import Any`） |
| 6 | tests/test_background_launch.py | posix-only skip（docstring 说明 Windows 机制由 test_win_compat 覆盖；复核中修正 docstring 中 `start /b` 为 CreateProcessW 直启的准确描述） |

**验证**：重建环境全量回归 **38 passed / 1 skipped**；test_win_compat 无回归。

---

## 6. 变更复核

**范围**：HEAD → 工作树，4 文件 23 增 7 删，逐 hunk 闭合。

**Findings**：未发现由本次变更新引入且需要报告的问题。

- 各 hunk 行为变化均与生产语义对齐：legacy 路径分支与管理器 `os.name` 分支一致；`-B` 为等价机制；`Any` 注入只影响 annotation 求值；skip 后的 Windows 覆盖由 test_win_compat 补齐（已实测）
- 复核中自发现并修复 1 处 docstring 不精确
- **verdict: pass**

---

## 7. 遗留事项

1. **发行包重建（发布前必须）**：Full.zip / Full-Max.zip 为 mac 清理前构建（01:01 / 01:48），仍含已删除的 README-win.md。需执行：
   - `python build_windows_zip.py --bundle-runtime`（Full）
   - `python build_windows_zip.py --bundle-runtime --qwen-cpp <组装片段目录>`（Max）
2. 本机 65001 代码页下 bat 中文提示乱码（功能正常）；如需改善提示可评估 bat 内动态编码方案，属后续优化
3. 本地 Qwen 全流程（真实下载 1.7B 模型 + ASR 推理）未实测，建议发布前在目标机器跑通一次
