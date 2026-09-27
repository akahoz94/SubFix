@echo off
setlocal EnableExtensions
rem SubFix Windows 版安装脚本
rem 目标目录：%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
set "SRC=%~dp0"
set "DEST=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"

echo 安装 SubFix 到: %DEST%
if not exist "%SRC%SubFix\SubFix.lua" (
  echo [错误] 未找到 SubFix\SubFix.lua，请先完整解压 ZIP 再运行本脚本。
  pause
  exit /b 1
)

xcopy "%SRC%SubFix" "%DEST%\SubFix\" /E /I /Y >nul || goto :fail
xcopy "%SRC%.subfix_support" "%DEST%\.subfix_support\" /E /I /Y >nul || goto :fail

echo.
echo 安装完成。请重新打开 DaVinci Resolve，在 Workspace - Scripts - Utility 下找到 SubFix。
echo 首次使用本地识别前，请运行 %DEST%\.subfix_support\setup_asr_env.cmd 安装 ASR 环境。
pause
exit /b 0

:fail
echo 安装失败，请检查文件是否完整、是否有写入权限。
pause
exit /b 1
