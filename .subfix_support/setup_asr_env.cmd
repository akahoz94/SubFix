@echo off
setlocal EnableExtensions
rem SubFix ASR 环境安装（Windows 版，对应 macOS 的 setup_asr_env.sh）
rem 用法：双击运行，或在终端执行。可用环境变量：
rem   SUBFIX_ASR_VENV_DIR      指定 venv 目标目录
rem   SUBFIX_INSTALL_QWEN_ASR  设为 0 跳过 qwen-asr 安装
set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.subfix_asr_env"
if defined SUBFIX_ASR_VENV_DIR set "VENV_DIR=%SUBFIX_ASR_VENV_DIR%"

set "PYTHON="
if exist "%SCRIPT_DIR%runtime\python\python.exe" set "PYTHON=%SCRIPT_DIR%runtime\python\python.exe"
if not defined PYTHON ( where py >nul 2>nul && set "PYTHON=py -3" )
if not defined PYTHON ( where python >nul 2>nul && set "PYTHON=python" )
if not defined PYTHON (
  echo [错误] 未找到 Python。请先安装 Python 3.10-3.13，安装时勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)

echo 使用 Python: %PYTHON%
echo ASR venv:   %VENV_DIR%

if not exist "%VENV_DIR%\Scripts\python.exe" (
  %PYTHON% -m venv "%VENV_DIR%" || goto :fail
)

set "PIP_INDEX=-i https://pypi.tuna.tsinghua.edu.cn/simple"
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip %PIP_INDEX% >nul 2>nul || "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip || goto :fail

if "%SUBFIX_INSTALL_QWEN_ASR%"=="0" (
  echo 已按 SUBFIX_INSTALL_QWEN_ASR=0 跳过 Qwen3-ASR。v4 生成字幕将不可用。
) else (
  "%VENV_DIR%\Scripts\python.exe" -m pip install -U qwen-asr %PIP_INDEX% || "%VENV_DIR%\Scripts\python.exe" -m pip install -U qwen-asr || goto :fail
  echo Qwen3-ASR 已安装。首次生成会下载 Qwen/Qwen3-ASR-1.7B。
)

echo ASR 环境已安装。
pause
exit /b 0

:fail
echo ASR 环境安装失败，请检查网络后重试。
pause
exit /b 1
