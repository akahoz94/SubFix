"""文稿直出字幕引擎（Script Match 移植）。

改编自 heiba-wk/DaVinci-ASR（Apache-2.0）runtime/subtitles/script_match.py，
按 SubFix 的行结构与对齐产物重写。纯函数、零模型、零 I/O。

核心思想：文稿每行 = 一条字幕。对齐器把整段文稿对到音频上得到带时间的
token，再按归一化字符序列（中日韩逐字、拉丁按词组）映射回文稿行，最后过
质量门：六项检查（对齐有效性/文本覆盖/映射覆盖/唯一区间占比/对齐跨度比/
局部塌缩）+ 区间卫生（越界/非单调/重叠/无效）。任何一项不过 = 不产出，
由调用方回退「贴稿转录 + 校对」路径。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable

# 音频时长上限：对齐器单次前向的 timestamp 槽位有限，超长音频会被压缩
DIRECT_MAX_AUDIO_SECONDS = 300.0
# 中文口播一般 4-6 字/秒，超过这个比例说明音频比稿子短得多
DIRECT_MAX_PLAUSIBLE_CHARS_PER_SECOND = 9.0
# 粘贴的整段文稿常常一行的到底；超过这个长度就按标点自动分句
DIRECT_AUTO_SPLIT_CHARS = 24
DIRECT_SENTENCE_HARD_BREAK_CHARS = 40
DIRECT_MIN_ALIGNMENT_COVERAGE = 0.98
DIRECT_MIN_MAPPING_COVERAGE = 0.98
DIRECT_MIN_LINE_COVERAGE = 0.90
DIRECT_MIN_UNIQUE_INTERVAL_RATIO = 0.80
DIRECT_MIN_ALIGNED_SPAN_RATIO = 0.35
DIRECT_LOCAL_COLLAPSE_MAX_SECONDS = 0.50
DIRECT_LOCAL_COLLAPSE_MIN_DISPLAY_UNITS_PER_SECOND = 24.0


@dataclass(frozen=True)
class ScriptLine:
    index: int
    text: str
    normalized_text: str


@dataclass(frozen=True)
class SubtitleBlock:
    start: float
    end: float
    text: str
    line_index: int = -1


@dataclass
class ScriptMapping:
    blocks: list[SubtitleBlock] = field(default_factory=list)
    mapping_coverage: float = 0.0
    line_coverages: list[float] = field(default_factory=list)
    unmapped_line_indices: list[int] = field(default_factory=list)
    reference_character_count: int = 0
    matched_character_count: int = 0


@dataclass(frozen=True)
class IntervalDiagnostics:
    invalid_count: int
    non_monotonic_count: int
    overlap_count: int
    out_of_bounds_count: int


@dataclass(frozen=True)
class DirectQuality:
    passed: bool
    reasons: tuple[str, ...]
    unique_interval_ratio: float
    aligned_span_ratio: float
    intervals: IntervalDiagnostics
    locally_collapsed_line_indices: tuple[int, ...]


STRONG_SENTENCE_ENDS = "。！？!?…"
WEAK_BREAKS = "，,、；;：:"
DIRECT_MAX_SENTENCE_CHARS = 40


def split_line_into_sentences(raw_line: str) -> list[str]:
    """粘贴进来的经常是一整段没有换行的稿：先按句末标点自动分句。

    一行 = 一条候选字幕，所以不分句就会出现「一条字幕 = 一整段」，
    既过不了对齐，也谈不上长度上限。
    """
    text = str(raw_line or "").strip()
    if not text:
        return []
    sentences: list[str] = []
    buffer = ""
    for character in text:
        buffer += character
        if character in STRONG_SENTENCE_ENDS:
            sentences.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        sentences.append(buffer.strip())

    output: list[str] = []
    for sentence in sentences:
        if default_text_length(sentence) <= DIRECT_MAX_SENTENCE_CHARS:
            output.append(sentence)
            continue
        # 仍然过长（整段没有句末标点）时，退到读断类标点切
        piece = ""
        for character in sentence:
            piece += character
            if character in WEAK_BREAKS and default_text_length(piece) >= DIRECT_MAX_SENTENCE_CHARS // 2:
                output.append(piece.strip())
                piece = ""
        if piece.strip():
            if output and default_text_length(output[-1]) + default_text_length(piece) <= DIRECT_MAX_SENTENCE_CHARS:
                output[-1] = output[-1] + piece
            else:
                output.append(piece.strip())
    return [item for item in output if item]


def parse_reference_lines(reference_text: str) -> list[ScriptLine]:
    """非空句 = 一条候选字幕（整段粘贴会先自动分句）；全空抛错。"""
    lines: list[ScriptLine] = []
    for raw_line in str(reference_text or "").splitlines():
        for sentence in split_line_into_sentences(raw_line):
            lines.append(
                ScriptLine(
                    index=len(lines),
                    text=sentence,
                    normalized_text=normalize_alignment_text(sentence),
                )
            )
    if not lines:
        raise ValueError("SCRIPT_MATCH_REFERENCE_EMPTY")
    return lines


def prune_out_of_bounds_tokens(tokens: list[dict], duration: float, tolerance: float = 0.05) -> list[dict]:
    """丢掉超出音频长度的 token。

    贴整篇稿而选区只念了其中几句时，对齐器会把没念的句子挤到音频末尾变成
    越界时间戳 —— 它们正是 SCRIPT_INTERVAL_OUT_OF_BOUNDS 的来源，
    留着还会拉低唯一区间占比（COLLAPSED_ALIGNMENT_INTERVALS）。
    """
    kept: list[dict] = []
    for token in tokens:
        start = float(token.get("start") or 0.0)
        end = float(token.get("end") or 0.0)
        if start < -tolerance or end > duration + tolerance:
            continue
        kept.append(token)
    return kept


def _longest_contiguous_run(indices: list[int]) -> list[int]:
    best: list[int] = []
    current: list[int] = []
    for value in indices:
        if current and value != current[-1] + 1:
            current = []
        current.append(value)
        if len(current) > len(best):
            best = list(current)
    return best


def _block_chars_per_second(block: SubtitleBlock) -> float:
    seconds = block.end - block.start
    if seconds <= 1e-6:
        return float("inf")
    return default_text_length(block.text) / seconds


def align_script_to_spoken_window(
    tokens: list[dict],
    lines: list[ScriptLine],
    *,
    language: str,
    duration: float,
) -> tuple[ScriptMapping, list[ScriptLine], str]:
    """全篇映射；只对上一部分句子时，取出真正念到的连续句段重新映射。

    返回 (映射, 参与映射的文稿行, 说明)。贴整篇稿配短选区是正常用法，不能因为
    "有句子没念"就整段放弃直出。没念到的句子会被对齐器硬塞成极窄区间（实测 13 字
    挤进 0.56 秒 = 23 字/秒），所以除了"有没有映射上"还要按语速再筛一遍。
    """
    full = map_aligned_tokens_to_lines(tokens, lines, language=language)
    if not full.blocks:
        return full, lines, ""

    limit = DIRECT_MAX_PLAUSIBLE_CHARS_PER_SECOND
    unmapped = set(full.unmapped_line_indices)
    implausible = {block.line_index for block in full.blocks if _block_chars_per_second(block) > limit}
    usable = [index for index in range(len(lines)) if index not in unmapped and index not in implausible]
    if len(usable) == len(lines):
        return full, lines, ""
    run = _longest_contiguous_run(usable)
    if not run:
        return full, lines, ""

    window_lines = parse_reference_lines("\n".join(lines[index].text for index in run))
    window = map_aligned_tokens_to_lines(tokens, window_lines, language=language)
    kept_blocks = [block for block in window.blocks if _block_chars_per_second(block) <= limit]
    if not kept_blocks:
        return full, lines, ""
    kept_indices = {block.line_index for block in kept_blocks}
    window.blocks = kept_blocks
    survived = [line for line in window_lines if line.index in kept_indices]
    note = "文稿 %d 句中取第 %d-%d 句（其余 %d 句不在本选区音频里）" % (
        len(lines), run[0] + 1, run[-1] + 1, len(lines) - len(run)
    )
    return window, survived, note


def normalize_alignment_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character).startswith(("L", "N"))
    )


def display_units(text: str) -> int:
    """终端风格视觉宽度（中日韩全角记 2，其余记 1），供塌缩密度判断。"""
    width = 0
    for character in str(text):
        width += 2 if _is_cjk(character) or ord(character) > 0x2E80 else 1
    return width


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _word_mapping_groups(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    groups: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            groups.append("".join(current))
            current.clear()

    for index, character in enumerate(normalized):
        unit = normalize_alignment_text(character)
        if unit:
            if _is_cjk(character):
                flush()
                groups.append(unit)
            else:
                current.append(unit)
            continue
        previous_alnum = index > 0 and normalize_alignment_text(normalized[index - 1])
        following_alnum = index + 1 < len(normalized) and normalize_alignment_text(
            normalized[index + 1]
        )
        if character in {"'", "’", ".", "-"} and previous_alnum and following_alnum:
            continue
        flush()
    flush()
    return groups


def _mapping_groups(value: str, language: str) -> list[str]:
    normalized = normalize_alignment_text(value)
    if language in {"Chinese", "Cantonese"}:
        return list(normalized)
    return _word_mapping_groups(value)


@dataclass(frozen=True)
class _ReferenceUnit:
    text: str
    line_index: int
    group_index: int


@dataclass(frozen=True)
class _TimedUnit:
    text: str
    start: float
    end: float


def _reference_units(lines: list[ScriptLine], language: str) -> list[_ReferenceUnit]:
    units: list[_ReferenceUnit] = []
    group_index = 0
    for line in lines:
        for group in _mapping_groups(line.text, language):
            units.extend(
                _ReferenceUnit(character, line.index, group_index)
                for character in group
            )
            group_index += 1
    return units


def _group_coverage(
    reference_units: list[_ReferenceUnit],
    matched_reference_indices: set[int],
    *,
    line_count: int,
) -> tuple[float, list[float]]:
    group_totals: dict[int, int] = {}
    group_matches: dict[int, int] = {}
    group_lines: dict[int, int] = {}
    for index, unit in enumerate(reference_units):
        group_totals[unit.group_index] = group_totals.get(unit.group_index, 0) + 1
        group_lines[unit.group_index] = unit.line_index
        if index in matched_reference_indices:
            group_matches[unit.group_index] = group_matches.get(unit.group_index, 0) + 1
    complete = {
        group_index
        for group_index, total in group_totals.items()
        if group_matches.get(group_index, 0) == total
    }
    totals_by_line = [0 for _index in range(line_count)]
    matches_by_line = [0 for _index in range(line_count)]
    for group_index, line_index in group_lines.items():
        totals_by_line[line_index] += 1
        if group_index in complete:
            matches_by_line[line_index] += 1
    line_coverages = [
        matches / total if total else 0.0
        for matches, total in zip(matches_by_line, totals_by_line)
    ]
    return (
        len(complete) / len(group_totals) if group_totals else 0.0,
        line_coverages,
    )


def _token_units(tokens: list[dict[str, Any]]) -> list[_TimedUnit]:
    units: list[_TimedUnit] = []
    for token in tokens:
        normalized = normalize_alignment_text(str(token.get("text") or ""))
        if not normalized:
            continue
        start = float(token.get("start") or 0.0)
        end = float(token.get("end") or 0.0)
        duration = max(0.0, end - start)
        count = len(normalized)
        for index, character in enumerate(normalized):
            units.append(
                _TimedUnit(
                    character,
                    start + duration * index / count,
                    start + duration * (index + 1) / count,
                )
            )
    return units


def _matching_pairs(expected: str, actual: str) -> list[tuple[int, int]]:
    if expected == actual:
        return [(index, index) for index in range(len(expected))]
    matcher = SequenceMatcher(None, expected, actual, autojunk=False)
    pairs: list[tuple[int, int]] = []
    for match in matcher.get_matching_blocks():
        pairs.extend(
            (match.a + offset, match.b + offset) for offset in range(match.size)
        )
    return pairs


def _stabilize_blocks(blocks: list[SubtitleBlock]) -> list[SubtitleBlock]:
    output = [SubtitleBlock(block.start, block.end, block.text, block.line_index) for block in blocks]
    for index in range(1, len(output)):
        previous = output[index - 1]
        current = output[index]
        if current.start + 1e-9 >= previous.end:
            continue
        lower = previous.start + 1e-6
        upper = current.end - 1e-6
        if lower >= upper:
            continue
        boundary = min(max((previous.end + current.start) / 2.0, lower), upper)
        # SubtitleBlock 是 frozen dataclass，只能整块替换；原地赋值会抛 FrozenInstanceError
        output[index - 1] = SubtitleBlock(previous.start, boundary, previous.text, previous.line_index)
        output[index] = SubtitleBlock(boundary, current.end, current.text, current.line_index)
    return output


def map_aligned_tokens_to_lines(
    tokens: list[dict],
    lines: list[ScriptLine],
    *,
    language: str,
    minimum_line_coverage: float = DIRECT_MIN_LINE_COVERAGE,
) -> ScriptMapping:
    """对齐 token（{text,start,end}）映射回文稿行；覆盖不足的行不产出。"""
    if not 0.0 <= minimum_line_coverage <= 1.0:
        raise ValueError("minimum_line_coverage must be between 0 and 1")
    reference_units = _reference_units(lines, language)
    timed_units = _token_units(tokens)
    expected = "".join(unit.text for unit in reference_units)
    actual = "".join(unit.text for unit in timed_units)
    pairs = _matching_pairs(expected, actual)
    matched_by_line: list[list[_TimedUnit]] = [[] for _line in lines]
    matched_reference_indices: set[int] = set()
    for expected_index, actual_index in pairs:
        if expected_index >= len(reference_units) or actual_index >= len(timed_units):
            continue
        line_index = reference_units[expected_index].line_index
        matched_by_line[line_index].append(timed_units[actual_index])
        matched_reference_indices.add(expected_index)

    mapping_coverage, line_coverages = _group_coverage(
        reference_units,
        matched_reference_indices,
        line_count=len(lines),
    )
    unmapped: list[int] = []
    blocks: list[SubtitleBlock] = []
    for line in lines:
        coverage = line_coverages[line.index]
        matches = matched_by_line[line.index]
        if coverage + 1e-9 < minimum_line_coverage or not matches:
            unmapped.append(line.index)
            continue
        start = min(unit.start for unit in matches)
        end = max(unit.end for unit in matches)
        if end <= start:
            unmapped.append(line.index)
            continue
        blocks.append(SubtitleBlock(start, end, line.text, line_index=line.index))

    return ScriptMapping(
        blocks=_stabilize_blocks(blocks),
        mapping_coverage=mapping_coverage,
        line_coverages=line_coverages,
        unmapped_line_indices=unmapped,
        reference_character_count=len(reference_units),
        matched_character_count=len(pairs),
    )


def interval_diagnostics(
    blocks: list[SubtitleBlock], duration: float
) -> IntervalDiagnostics:
    invalid = 0
    non_monotonic = 0
    overlaps = 0
    out_of_bounds = 0
    previous: SubtitleBlock | None = None
    for block in blocks:
        if block.start < 0.0 or block.end <= block.start:
            invalid += 1
        if block.start < -1e-6 or block.end > duration + 1e-6:
            out_of_bounds += 1
        if previous is not None:
            if block.start + 1e-6 < previous.start or block.end + 1e-6 < previous.end:
                non_monotonic += 1
            if block.start + 1e-6 < previous.end:
                overlaps += 1
        previous = block
    return IntervalDiagnostics(invalid, non_monotonic, overlaps, out_of_bounds)


def evaluate_direct_quality(
    tokens: list[dict],
    mapping: ScriptMapping,
    *,
    line_count: int,
    duration: float,
) -> DirectQuality:
    """质量门：任一检查不过即拒绝直出（reasons 为机器可读代码）。"""
    reasons: list[str] = []
    if not mapping.blocks:
        reasons.append("ALIGNMENT_EMPTY")
    if mapping.matched_character_count / max(1, mapping.reference_character_count) + 1e-9 < DIRECT_MIN_ALIGNMENT_COVERAGE:
        reasons.append("LOW_ALIGNMENT_COVERAGE")
    if mapping.mapping_coverage + 1e-9 < DIRECT_MIN_MAPPING_COVERAGE:
        reasons.append("LOW_SCRIPT_MAPPING_COVERAGE")
    if mapping.unmapped_line_indices or len(mapping.blocks) != line_count:
        reasons.append("UNMAPPED_SCRIPT_LINES")

    interval_count = len(
        {(round(token.get("start", 0.0), 6), round(token.get("end", 0.0), 6)) for token in tokens}
    )
    unique_ratio = interval_count / len(tokens) if tokens else 0.0
    if len(tokens) >= 10 and unique_ratio + 1e-9 < DIRECT_MIN_UNIQUE_INTERVAL_RATIO:
        reasons.append("COLLAPSED_ALIGNMENT_INTERVALS")

    if tokens and duration > 0.0:
        span = max(float(token.get("end") or 0.0) for token in tokens) - min(
            float(token.get("start") or 0.0) for token in tokens
        )
        span_ratio = max(0.0, span) / duration
    else:
        span_ratio = 0.0
    if (
        duration >= 10.0
        and mapping.reference_character_count >= 10
        and span_ratio + 1e-9 < DIRECT_MIN_ALIGNED_SPAN_RATIO
    ):
        reasons.append("COMPRESSED_ALIGNMENT_SPAN")

    locally_collapsed = tuple(
        index
        for index, block in enumerate(mapping.blocks)
        if (
            block.end - block.start < DIRECT_LOCAL_COLLAPSE_MAX_SECONDS
            and display_units(block.text) / (block.end - block.start)
            > DIRECT_LOCAL_COLLAPSE_MIN_DISPLAY_UNITS_PER_SECOND
        )
    )
    if locally_collapsed:
        # 全局覆盖与区间占比可能依然健康，但一行塌成几十毫秒的字幕不可读：
        # 只要有一行塌缩就走回退，Script Match 的下限由最差的一行决定。
        reasons.append("LOCALLY_COLLAPSED_SCRIPT_INTERVALS")

    intervals = interval_diagnostics(mapping.blocks, duration)
    if intervals.invalid_count:
        reasons.append("INVALID_SCRIPT_INTERVALS")
    if intervals.non_monotonic_count:
        reasons.append("NON_MONOTONIC_SCRIPT_INTERVALS")
    if intervals.overlap_count:
        reasons.append("OVERLAPPING_SCRIPT_INTERVALS")
    if intervals.out_of_bounds_count:
        reasons.append("SCRIPT_INTERVAL_OUT_OF_BOUNDS")
    return DirectQuality(
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        unique_interval_ratio=unique_ratio,
        aligned_span_ratio=span_ratio,
        intervals=intervals,
        locally_collapsed_line_indices=locally_collapsed,
    )


HARD_BREAK_CHARS = "。！？!?；;…"
SOFT_BREAK_CHARS = "，,、：: \t）)】」》"


def default_text_length(text: str) -> int:
    """与主断句路径同一把尺：数字符、忽略空白。"""
    return len(re.sub(r"\s+", "", str(text or "")))


def _break_after(text: str, breaks: str, start: int, upper: int) -> int:
    for position in range(upper, start, -1):
        if text[position - 1] in breaks:
            return position
    return -1


def _inside_latin_word(text: str, position: int) -> bool:
    if position <= 0 or position >= len(text):
        return False
    previous, current = text[position - 1], text[position]
    return previous.isascii() and current.isascii() and previous.isalnum() and current.isalnum()


def _latin_word_start(text: str, start: int, position: int) -> int:
    cursor = position
    while cursor > start and text[cursor - 1].isascii() and text[cursor - 1].isalnum():
        cursor -= 1
    return cursor


def split_text_to_max_chars(
    text: str,
    max_chars: int | None,
    measure: Callable[[str], int] = default_text_length,
) -> list[str]:
    """超长文本按标点切细；片段首尾相接，拼回原文一字不差。

    优先切在句末标点后，其次读断类标点/括号/空白，都没有才不切断英文单词。
    单个英文词本身超限就整词放行（与主路径 enforce_hard_char_limit 同一取舍）。
    """
    value = str(text or "")
    if not value:
        return [value]
    if not max_chars or int(max_chars) <= 0 or measure(value) <= max_chars:
        return [value]

    limit = int(max_chars)
    length = len(value)
    segments: list[str] = []
    start = 0
    while start < length:
        end = start
        while end < length and measure(value[start:end + 1]) <= limit:
            end += 1
        if end >= length:
            cut = length
        else:
            cut = _break_after(value, HARD_BREAK_CHARS, start, end)
            if cut <= start:
                cut = _break_after(value, SOFT_BREAK_CHARS, start, end)
            if cut <= start:
                cut = end
            if _inside_latin_word(value, cut):
                backed = _latin_word_start(value, start, cut)
                if backed > start and measure(value[start:backed]) <= limit:
                    cut = backed
                elif backed <= start:
                    # 整个窗口都在一个英文词里：整词放行，宁可超上限也不把词劈开
                    word_end = cut
                    while word_end < length and value[word_end].isascii() and value[word_end].isalnum():
                        word_end += 1
                    if word_end > start:
                        cut = word_end
        if cut <= start:
            cut = start + 1
        segments.append(value[start:cut])
        start = cut
    return segments


def build_direct_rows(
    mapping: ScriptMapping,
    *,
    fps: float,
    timeline_start_frame: int = 0,
    speaker_track_index: int = 1,
    max_chars: int | None = None,
    text_length: Callable[[str], int] | None = None,
) -> list[dict]:
    """映射块 → v5 管线行格式（文本即文稿原文，零改动）。

    block.start/end 是相对**已截出的选区音频**的秒数，回填要落到时间线绝对帧，
    所以必须加 timeline_start_frame；漏了加偏移的行会全部落在选区之前，
    被核心层的 scope 裁剪成 end<=start 而整批丢弃。

    ``max_chars`` 对应用户选的「字幕长度（标准 ≤25 / 短视频 ≤10）」。文稿一行可能
    就是一整段（200 字），不拆就会出一条 200 字的字幕——直接无视用户设置。
    拆分只改切点不改文字：各片按标点取整，帧号按字符数比例分配且首尾相接。
    """
    measure = text_length or default_text_length
    rows: list[dict] = []
    base_frame = int(timeline_start_frame or 0)
    rate = max(1.0, float(fps or 30.0))
    index = 1
    for block in mapping.blocks:
        block_start = base_frame + int(round(block.start * rate))
        block_end = max(block_start + 1, base_frame + int(round(block.end * rate)))
        segments = split_text_to_max_chars(block.text, max_chars, measure)
        weights = [max(1, measure(segment)) for segment in segments]
        total_weight = sum(weights)
        cursor = 0
        for position, segment in enumerate(segments):
            span = block_end - block_start
            start_frame = block_start if position == 0 else block_start + int(span * cursor / total_weight)
            cursor += weights[position]
            end_frame = (
                block_end
                if position == len(segments) - 1
                else block_start + int(span * cursor / total_weight)
            )
            end_frame = max(end_frame, start_frame + 1)
            rows.append(
                {
                    "index": index,
                    "text": segment,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "speaker_track_index": speaker_track_index,
                    "timing_decision": "script_direct",
                }
            )
            index += 1
    return rows
