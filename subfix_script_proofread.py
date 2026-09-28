"""文稿校对引擎：转录文本 vs 用户文稿的字符级对齐与同音纠错。

设计契约（与用户对齐的定稿）：
- 文稿单向只进不改：文稿内容永远不会被新增进字幕，只可能替换
  已被说出来的字。跳读、即兴发挥、结构差异一律保转录。
- 同音才替换：只有与转录逐字拼音序列相同（不含声调）的替换块才
  采用文稿写法——在/再、的/得/地、反映/反应这类 ASR 同音别字。
- 标点差异跟文稿走：块内去掉标点空格后完全相等才算。
- 幂等：行内保留 transcript_text（原始转录），重校永远拿原始转录
  与新文稿 diff，不在校对结果上二次叠加，换稿重跑不污染。
- 零模型：difflib + pypinyin（可选），毫秒级；pypinyin 缺失时降级
  为只做标点修正，同音纠错自动失效，绝不误改。

纯函数模块，无内部依赖——未来迁入 service 包时原样搬运。
"""

from __future__ import annotations

import difflib
import unicodedata
from typing import Any

PROOFREAD_SCHEMA = "subfix_script_proofread_v1"
# 安全阀：超出即放弃校对（SequenceMatcher 最坏 O(n²)，文稿有 Lua 侧
# 字数风控，这里再兜一层底）。
MAX_PROOFREAD_CHARS = 200_000

try:
    from pypinyin import lazy_pinyin
except Exception:  # pragma: no cover - 依赖缺失走保守降级
    lazy_pinyin = None


def _is_word_char(character: str) -> bool:
    return unicodedata.category(character)[0] in ("L", "N")


def _strip_ignorable(text: str) -> str:
    return "".join(ch for ch in text if _is_word_char(ch))


def _pinyin_sequence(text: str) -> list[str] | None:
    if lazy_pinyin is None:
        return None
    sequence: list[str] = []
    for character in text:
        syllables = lazy_pinyin(character)
        if not syllables:
            return None
        sequence.extend(syllables)
    return sequence


def _classify_replace(transcript_block: str, script_block: str) -> str:
    """replace 块分类：punctuation / homophone / structure。"""
    transcript_core = _strip_ignorable(transcript_block)
    script_core = _strip_ignorable(script_block)
    if transcript_core == script_core:
        # 去掉标点空格后完全相等 → 只差标点写法，跟文稿走
        return "punctuation"
    if len(transcript_block) == len(script_block):
        transcript_py = _pinyin_sequence(transcript_block)
        script_py = _pinyin_sequence(script_block)
        if transcript_py is not None and transcript_py == script_py:
            return "homophone"
    return "structure"


def _normalize_script(script_text: str) -> str:
    """行-strip 后无缝拼接：文稿换行是排版，转录侧没有换行。"""
    return "".join(line.strip() for line in script_text.replace("\r\n", "\n").splitlines())


def _empty_diagnostic(status: str) -> dict[str, Any]:
    return {
        "proofread_schema": PROOFREAD_SCHEMA,
        "proofread_status": status,
        "proofread_pinyin_available": lazy_pinyin is not None,
        "proofread_script_chars": 0,
        "proofread_block_count": 0,
        "proofread_homophone_replace_count": 0,
        "proofread_punctuation_replace_count": 0,
        "proofread_structure_kept_count": 0,
        "proofread_cross_row_kept_count": 0,
        "proofread_changed_row_count": 0,
    }


def proofread_rows(
    rows: list[dict[str, Any]], script_text: str | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按文稿校对字幕行文本，时间轴字段一律不动。

    行数据契约：输入行可带 transcript_text（此前的原始转录）；校对
    来源永远是它（缺省用当前 text），输出行保证带 transcript_text。
    """
    diagnostic = _empty_diagnostic("noop_empty_script")
    if not rows or not script_text or not script_text.strip():
        return [dict(row) for row in rows or []], diagnostic

    script = _normalize_script(script_text)
    diagnostic["proofread_script_chars"] = len(script)

    # 拼接转录全文并记录每行拥有的字符区间（校对 diff 的锚定坐标系）
    originals: list[str] = []
    spans: list[tuple[int, int]] = []
    position = 0
    for row in rows:
        original = str(row.get("transcript_text") if row.get("transcript_text") is not None else row.get("text") or "")
        originals.append(original)
        spans.append((position, position + len(original)))
        position += len(original)
    transcript = "".join(originals)

    if len(transcript) + len(script) > MAX_PROOFREAD_CHARS:
        diagnostic["proofread_status"] = "noop_too_long"
        return [dict(row) for row in rows], diagnostic

    matcher = difflib.SequenceMatcher(None, transcript, script, autojunk=False)
    replacements: list[tuple[int, int, str, str]] = []
    opcodes = matcher.get_opcodes()
    for tag, a_start, a_end, _b_start, _b_end in opcodes:
        if tag == "equal":
            continue
        transcript_block = transcript[a_start:a_end]
        script_block = script[_b_start:_b_end]
        if tag in ("delete", "insert"):
            # delete=转录多出（即兴/重录）保转录；insert=文稿多出，内容
            # 永不新增进字幕。两者都只计数。
            diagnostic["proofread_structure_kept_count"] += 1
            continue
        kind = _classify_replace(transcript_block, script_block)
        if kind == "structure":
            diagnostic["proofread_structure_kept_count"] += 1
            continue
        owner = next(
            (index for index, (start, end) in enumerate(spans) if a_start >= start and a_end <= end and end > start),
            None,
        )
        if owner is None:
            # 块跨行：无法归属单行，保守保转录
            diagnostic["proofread_cross_row_kept_count"] += 1
            continue
        replacements.append((a_start, a_end, script_block, kind))
        diagnostic[f"proofread_{kind}_replace_count"] += 1
    diagnostic["proofread_block_count"] = sum(1 for tag, *_rest in opcodes if tag != "equal")

    # 应用替换（倒序防偏移失效），仅改 text，时间轴字段不动
    changed = 0
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        new_row = dict(row)
        start, end = spans[index]
        row_replacements = [rep for rep in replacements if start <= rep[0] and rep[1] <= end]
        for rep_start, rep_end, script_block, _kind in reversed(row_replacements):
            offset = rep_start - start
            originals[index] = originals[index][:offset] + script_block + originals[index][offset + (rep_end - rep_start):]
        if row.get("transcript_text") is None:
            new_row["transcript_text"] = str(row.get("text") or "")
        if row_replacements:
            new_row["text"] = originals[index]
            new_row["proofread_status"] = "script_corrected"
            changed += 1
        else:
            new_row["proofread_status"] = "transcript_kept"
        output.append(new_row)

    diagnostic["proofread_changed_row_count"] = changed
    if changed:
        diagnostic["proofread_status"] = (
            "applied" if lazy_pinyin is not None else "applied_degraded"
        )
    else:
        diagnostic["proofread_status"] = "noop_no_match"
    return output, diagnostic
