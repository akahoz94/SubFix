@echo off
setlocal EnableExtensions
rem 接入本机已有模型，避免首次使用时重复下载（对应模型目录可按需修改后再次运行）
rem 当前指向 G:\AImodel，模型文件经校验齐全：
rem   Qwen3-ASR-1.7B          完整 safetensors（config/index/两个分片）
rem   Qwen3-ForcedAligner-0.6B config.json + model.safetensors
setx SUBFIX_QWEN3_ASR_MODEL "G:\AImodel\Qwen3-ASR-1.7B" >nul
setx SUBFIX_QWEN3_ALIGNER_MODEL "G:\AImodel\Qwen3-ForcedAligner-0.6B" >nul

rem 同时把模型 junction 到 SubFix 数据目录，"Qwen（本地）"安装器即可识别为已下载、零下载复用
set "DATA_ROOT=%APPDATA%\SubFix\models"
if not exist "%DATA_ROOT%" mkdir "%DATA_ROOT%"
if not exist "%DATA_ROOT%\qwen3-asr-1.7b" (
  mklink /J "%DATA_ROOT%\qwen3-asr-1.7b" "G:\AImodel\Qwen3-ASR-1.7B" >nul && echo 已创建模型 junction: %DATA_ROOT%\qwen3-asr-1.7b
) else (
  echo 模型 junction 已存在，跳过。
)
echo 已写入用户环境变量：
echo   SUBFIX_QWEN3_ASR_MODEL     = G:\AImodel\Qwen3-ASR-1.7B
echo   SUBFIX_QWEN3_ALIGNER_MODEL = G:\AImodel\Qwen3-ForcedAligner-0.6B
echo   数据目录 junction = %APPDATA%\SubFix\models\qwen3-asr-1.7b
echo.
echo 请完全退出并重新打开 DaVinci Resolve 使变量生效。
echo 如需改回联网下载模型，删除这两个环境变量即可（系统设置-高级-环境变量）。
pause
exit /b 0
