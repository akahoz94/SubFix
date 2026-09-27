@echo off
setlocal EnableExtensions
rem SubFix Windows 版卸载脚本（对应 macOS 的 卸载_SubFix.command）
rem 仅删除 SubFix 自己的文件；达芬奇项目、HooperAI_Backups 字幕备份与识别模型保留。
set "DEST=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"

echo 将删除以下 SubFix 插件文件及其运行环境：
echo   %DEST%\SubFix
echo   %DEST%\.subfix_support
echo 保留达芬奇项目、字幕备份，以及 %APPDATA%\SubFix 下的识别模型。
echo.
set "CONF="
set /p "CONF=请先关闭 SubFix 窗口。输入 UNINSTALL 确认卸载，其他输入均取消: "
if /I not "%CONF%"=="UNINSTALL" (
  echo 已取消，未删除文件。
  pause
  exit /b 0
)

if exist "%DEST%\SubFix" rmdir /S /Q "%DEST%\SubFix"
if exist "%DEST%\.subfix_support" rmdir /S /Q "%DEST%\.subfix_support"

rem 系统级安装（C:\ProgramData）如存在也一并清理
set "SYSTEM_DEST=C:\ProgramData\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility"
if exist "%SYSTEM_DEST%\SubFix" (
  net session >nul 2>&1
  if errorlevel 1 (
    echo 检测到系统级安装但当前无管理员权限，已跳过；请右键"以管理员身份运行"本脚本后重试。
  ) else (
    rmdir /S /Q "%SYSTEM_DEST%\SubFix"
    rmdir /S /Q "%SYSTEM_DEST%\.subfix_support"
    echo 系统级安装已删除。
  )
)

if exist "%APPDATA%\SubFix\envs" rmdir /S /Q "%APPDATA%\SubFix\envs"
if exist "%APPDATA%\SubFix\.subfix-qwen-local-ready.json" del /Q "%APPDATA%\SubFix\.subfix-qwen-local-ready.json"

echo SubFix 卸载完成，请重新打开达芬奇以刷新脚本菜单。
echo 识别模型保留在 %APPDATA%\SubFix\models，如不再需要可手动删除整个 %APPDATA%\SubFix。
pause
exit /b 0
