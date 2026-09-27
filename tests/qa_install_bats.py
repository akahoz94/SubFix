"""安装/卸载 bat 编码与执行 QA。

检查四层：
1. 静态字节审计：bat 必须是纯 GBK、CRLF、无 BOM、无 chcp 残留，中文渲染正确
2. zip 审计：bat 条目与仓库文件逐字节一致，中文文件名可正确解出
3. 解压审计：Expand-Archive（资源管理器同源实现）解出的文件哈希一致
4. 执行审计：从 zip 全新解压开始走 安装→覆盖安装→取消卸载→确认卸载→空卸载，
   全程输出按 GBK 解码，断言无 mojibake 标志、无"不是内部或外部命令"

运行：python tests/qa_install_bats.py
"""
import hashlib
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "dist" / "SubFix-v3.3.0-Windows.zip"
BAT_INSTALL = "安装_SubFix.bat"
BAT_UNINSTALL = "卸载_SubFix.bat"
DEST = Path.home() / "AppData/Roaming/Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility"

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def audit_batch(path: Path, expect_chcp_absent=True):
    data = path.read_bytes()
    check(f"{path.name}: 无 UTF-8 BOM", not data.startswith(b"\xef\xbb\xbf"))
    check(f"{path.name}: 严格 GBK 可解码", _try_gbk(data))
    check(f"{path.name}: 无裸 LF（全部 CRLF）", b"\r\n" in data and data.replace(b"\r\n", b"").find(b"\n") == -1)
    text = data.decode("gbk")
    check(f"{path.name}: 无 chcp（避免换码页偏移错乱）", "chcp" not in text.lower() or not expect_chcp_absent)
    check(f"{path.name}: 中文关键句渲染正确", _chinese_ok(text, path.name))
    check(f"{path.name}: 无 UTF-8 乱码特征(鈥/锛/锟)", not any(m in text for m in ("鈥", "锛", "锟", "æ", "ç")))
    return text


def _try_gbk(data):
    try:
        data.decode("gbk")
        return True
    except UnicodeDecodeError:
        return False


def _chinese_ok(text, name):
    if name == BAT_INSTALL:
        keys = ["安装 SubFix 到", "安装完成", "未找到", "首次使用本地识别前"]
    elif name == BAT_UNINSTALL:
        keys = ["将删除以下", "输入 UNINSTALL 确认卸载", "已取消，未删除文件", "卸载完成"]
    else:
        keys = ["使用 Python", "ASR 环境已安装", "未找到 Python"]
    return all(k in text for k in keys)


# ---------- 1. 仓库内静态审计 ----------
print("== 1. 仓库内 bat/cmd 静态字节审计 ==")
audit_batch(ROOT / "installer" / BAT_INSTALL)
audit_batch(ROOT / "installer" / BAT_UNINSTALL)
audit_batch(ROOT / ".subfix_support" / "setup_asr_env.cmd")

# ---------- 2. zip 审计 ----------
print("== 2. zip 中文名与字节一致性 ==")
check("zip 存在", ZIP.exists())
zf = zipfile.ZipFile(ZIP)
names = zf.namelist()
# zip 条目 → 仓库源文件 映射（目录规整后安装器源码在 installer/，cmd 在 .subfix_support/）
BAT_PAIRS = [
    (BAT_INSTALL, ROOT / "installer" / BAT_INSTALL),
    (BAT_UNINSTALL, ROOT / "installer" / BAT_UNINSTALL),
    (".subfix_support/setup_asr_env.cmd", ROOT / ".subfix_support" / "setup_asr_env.cmd"),
]
for entry in (BAT_INSTALL, BAT_UNINSTALL, "README.md", "SubFix/SubFix.lua", ".subfix_support/setup_asr_env.cmd"):
    check(f"zip 条目名正确: {entry}", entry in names)
for entry, disk in BAT_PAIRS:
    in_zip = zf.read(entry)
    check(f"zip 内 {entry} 与仓库逐字节一致", in_zip == disk.read_bytes(),
          f"zip={hashlib.sha256(in_zip).hexdigest()[:12]} disk={hashlib.sha256(disk.read_bytes()).hexdigest()[:12]}")

# ---------- 3. Expand-Archive 解压审计（与资源管理器同源实现） ----------
print("== 3. Expand-Archive 解压（模拟资源管理器） ==")
stage = Path(tempfile.mkdtemp(prefix="subfix_qa_extract_"))
stage = stage / "extract"
stage.mkdir(parents=True)
r = subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Expand-Archive -LiteralPath '{ZIP}' -DestinationPath '{stage}' -Force"],
                   capture_output=True)
check("Expand-Archive 解压成功", r.returncode == 0, r.stderr.decode("gbk", "replace")[:200])
for entry, disk in BAT_PAIRS:
    extracted = stage / entry
    if extracted.exists():
        check(f"解压后 {entry} 哈希一致", hashlib.sha256(extracted.read_bytes()).hexdigest()
              == hashlib.sha256(disk.read_bytes()).hexdigest())
    else:
        check(f"解压后存在 {entry}", False, str(list(stage.iterdir())))

# ---------- 4. 执行审计（全新解压目录里走完整流程） ----------
print("== 4. 端到端执行（安装→覆盖→取消卸载→确认卸载→空卸载） ==")
cp = subprocess.run(["cmd", "/c", "chcp"], capture_output=True).stdout.decode("gbk", "replace").strip()
print(f"  当前控制台默认代码页: {cp}")


def run_bat(name, stdin_text=""):
    r = subprocess.run(["cmd", "/c", name], cwd=str(stage), input=stdin_text.encode("gbk"),
                       capture_output=True, timeout=120)
    out = (r.stdout + r.stderr).decode("gbk", "replace")
    return r.returncode, out


def assert_clean_output(tag, out):
    # mojibake 标志：GBK 解码后出现的典型乱码字符与 cmd 报错
    garbage = [m for m in ("锛", "鈥", "锟", "鏄", "鐨") if m in out]
    check(f"{tag}: 无乱码字符", not garbage, str(garbage))
    check(f"{tag}: 无命令解析错误", "不是内部或外部命令" not in out and "不是内部或外部" not in out)


rc, out = run_bat(BAT_INSTALL)
assert_clean_output("安装", out)
check("安装: 报告完成", "安装完成" in out)
check("安装: 目标路径正确显示", str(DEST)[:30] in out, out[:200])
check("安装: SubFix.lua 已落盘", (DEST / "SubFix/SubFix.lua").exists())
check("安装: setup_asr_env.cmd 已落盘", (DEST / ".subfix_support/setup_asr_env.cmd").exists())
check("安装: 落盘文件与仓库一致",
      (DEST / "SubFix/SubFix.lua").read_bytes() == (ROOT / "SubFix.lua").read_bytes())

rc, out = run_bat(BAT_INSTALL)  # 覆盖安装
assert_clean_output("覆盖安装", out)
check("覆盖安装: 仍成功", "安装完成" in out)

rc, out = run_bat(BAT_UNINSTALL, "no")
assert_clean_output("取消卸载", out)
check("取消卸载: 提示已取消", "已取消，未删除文件" in out)
check("取消卸载: 文件仍在", (DEST / "SubFix/SubFix.lua").exists())

rc, out = run_bat(BAT_UNINSTALL, "UNINSTALL\n")
assert_clean_output("确认卸载", out)
check("确认卸载: 提示完成", "卸载完成" in out)
check("确认卸载: SubFix 目录已删", not (DEST / "SubFix").exists())
check("确认卸载: .subfix_support 已删", not (DEST / ".subfix_support").exists())

rc, out = run_bat(BAT_UNINSTALL, "UNINSTALL\n")  # 空卸载（已无文件）
assert_clean_output("空卸载", out)
check("空卸载: 不报错完成", "卸载完成" in out)

print()
if failures:
    print(f"共 {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("QA 全部通过")
