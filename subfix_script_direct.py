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

import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

# 音频时长上限：对齐器单次前向的 timestamp 槽位有限，超长音频会被压缩
DIRECT_MAX_AUDIO_SECONDS = 300.0
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


def parse_reference_lines(reference_text: str) -> list[ScriptLine]:
    """非空行 = 一条候选字幕；全空抛错（调用方应先判空）。"""
    lines: list[ScriptLine] = []
    for raw_line in str(reference_text or "").splitlines():
        if not raw_line.strip():
            continue
        lines.append(
            ScriptLine(
                index=len(lines),
                text=raw_line,
                normalized_text=normalize_alignment_text(raw_line),
            )
        )
    if not lines:
        raise ValueError("SCRIPT_MATCH_REFERENCE_EMPTY")
    return lines


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
    output = [SubtitleBlock(block.start, block.end, block.text) for block in blocks]
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
        previous.end = boundary
        current.start = boundary
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
        blocks.append(SubtitleBlock(start, end, line.text))

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


def build_direct_rows(
    mapping: ScriptMapping,
    *,
    fps: float,
    speaker_track_index: int = 1,
) -> list[dict]:
    """映射块 → v5 管线行格式（秒转帧；文本即文稿原文，零改动）。"""
    rows: list[dict] = []
    for block in mapping.blocks:
        start_frame = int(round(block.start * fps))
        end_frame = max(start_frame + 1, int(round(block.end * fps)))
        rows.append(
            {
                "text": block.text,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "speaker_track_index": speaker_track_index,
                "timing_decision": "script_direct",
            }
        )
    return rows
