@echo off
setlocal EnableExtensions
rem SubFix 系统级安装（对应 macOS 的系统级 payload）
rem 用途：Windows 用户名含中文时，%APPDATA% 路径含中文，Lua 5.1 的 ANSI 文件 API 可能
rem 解析失败；本脚本把插件装到纯 ASCII 的 C:\ProgramData 路径规避该问题。需要管理员权限。
set "SRC=%~dp0"
set "DEST=C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility"

net session >nul 2>&1
if errorlevel 1 (
  echo 需要管理员权限，正在请求 UAC 授权...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo 安装 SubFix（系统级）到: %DEST%
if not exist "%SRC%SubFix\SubFix.lua" (
  echo [错误] 未找到 SubFix\SubFix.lua，请先完整解压 ZIP 再运行本脚本。
  pause
  exit /b 1
)

xcopy "%SRC%SubFix" "%DEST%\SubFix\" /E /I /Y >nul || goto :fail
xcopy "%SRC%.subfix_support" "%DEST%\.subfix_support\" /E /I /Y >nul || goto :fail

echo.
echo 系统级安装完成。请重新打开 DaVinci Resolve，在 Workspace - Scripts - Utility 下找到 SubFix。
echo 提示：用户级（%APPDATA%）与系统级安装同时存在时，建议只保留一份。
pause
exit /b 0

:fail
echo 安装失败，请检查是否有管理员权限与磁盘写入权限。
pause
exit /b 1
