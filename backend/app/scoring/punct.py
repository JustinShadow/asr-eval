"""标点 F1 与句末标点准确率（M5 §5.3）。

口径：
1. 在**去标点**序列上对齐 ref/hyp（复用 CER 的 opcodes）；
2. 把 ref 标点位置映射到对齐后的索引（eq 直接映射，del 折叠到同一位置，insert 推移）；
3. 位置与类型都对 → TP；参考有、hyp 无（或类型不同）→ FN；hyp 有、参考无 → FP；
4. 句末标点单独统计 sentence_end_acc。

门控：dataset 必须 punct_supported=true；设备声明不支持标点时返回 na(device_no_punctuation)。
"""

from dataclasses import dataclass
from typing import TypedDict

from rapidfuzz.distance import Levenshtein

from app.scoring.cer import normalize_script

# 参与评分的标点（中英文常见句读与句末）
PUNCT_CHARS = set("，。！？、；：,.!?;:")
SENTENCE_END = set("。！？.!?")


@dataclass
class PunctResult:
    """单条样本的标点统计。"""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    end_total: int = 0
    end_correct: int = 0
    ref_punct: int = 0
    hyp_punct: int = 0


class PunctSummary(TypedDict):
    """标点 micro 汇总的稳定返回结构。"""

    f1: float | None
    precision: float | None
    recall: float | None
    tp: int
    fp: int
    fn: int
    sentence_end_acc: float | None
    sentence_end_total: int
    n: int


def _split(
    text: str,
    *,
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    lowercase: bool = True,
    traditional: str = "keep",
) -> tuple[str, list[tuple[int, str]]]:
    """返回 (去标点文本, [(标点所在 clean 边界索引, 字符)])。

    边界索引 = 该标点前已出现的非标点字符数；句末标点索引等于 clean 长度。
    """
    text = normalize_script(text, traditional=traditional)
    for tag in (*drop_tags, *drop_fillers):
        text = text.replace(tag, "")
    if lowercase:
        text = text.lower()
    clean: list[str] = []
    puncts: list[tuple[int, str]] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        if char in PUNCT_CHARS:
            # 数字内部的小数点/千分位逗号属于数字格式，不计作标点指标。
            if (
                char in {".", ","}
                and index > 0
                and index + 1 < len(text)
                and text[index - 1].isdigit()
                and text[index + 1].isdigit()
            ):
                continue
            puncts.append((len(clean), char))
        else:
            clean.append(char)
    return "".join(clean), puncts


def _alignment_map(ref: str, hyp: str) -> list[int]:
    """ref clean 索引（含末尾边界）→ hyp clean 索引。"""
    mapping = [0] * (len(ref) + 1)
    hyp_index = 0
    for code in Levenshtein.opcodes(ref, hyp):
        tag = str(code.tag)
        r1, r2 = int(code.src_start), int(code.src_end)
        h1, h2 = int(code.dest_start), int(code.dest_end)
        if tag == "equal":
            for offset in range(r2 - r1):
                mapping[r1 + offset] = h1 + offset
            hyp_index = h2
        elif tag == "delete":
            for offset in range(r1, r2):
                mapping[offset] = h1
            hyp_index = h2
        elif tag == "insert":
            hyp_index = h2
        else:  # replace
            pairs = min(r2 - r1, h2 - h1)
            for offset in range(pairs):
                mapping[r1 + offset] = h1 + offset
            for offset in range(r1 + pairs, r2):
                mapping[offset] = h1 + pairs
            hyp_index = h2
    mapping[len(ref)] = hyp_index
    return mapping


def score_punctuation(
    ref: str,
    hyp: str,
    *,
    drop_tags: tuple[str, ...] = (),
    drop_fillers: tuple[str, ...] = (),
    lowercase: bool = True,
    traditional: str = "keep",
) -> PunctResult:
    """单条标点 P/R/F1 统计（不聚合）。"""
    ref_clean, ref_puncts = _split(
        ref,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        lowercase=lowercase,
        traditional=traditional,
    )
    hyp_clean, hyp_puncts = _split(
        hyp,
        drop_tags=drop_tags,
        drop_fillers=drop_fillers,
        lowercase=lowercase,
        traditional=traditional,
    )
    if not ref_puncts:
        return PunctResult(fp=len(hyp_puncts), ref_punct=0, hyp_punct=len(hyp_puncts))

    mapping = _alignment_map(ref_clean, hyp_clean)
    hyp_by_pos: dict[int, list[str]] = {}
    for position, char in hyp_puncts:
        hyp_by_pos.setdefault(position, []).append(char)

    result = PunctResult(ref_punct=len(ref_puncts), hyp_punct=len(hyp_puncts))
    used: set[tuple[int, int]] = set()
    for position, char in ref_puncts:
        mapped = mapping[position] if position < len(mapping) else len(hyp_clean)
        candidates = hyp_by_pos.get(mapped, [])
        matched_index = None
        for index, _candidate in enumerate(candidates):
            if (mapped, index) in used:
                continue
            matched_index = index
            break
        if matched_index is None:
            result.fn += 1
        else:
            used.add((mapped, matched_index))
            if candidates[matched_index] == char:
                result.tp += 1
            else:
                # 位置对、类型不同：参考这侧记 FN，假设那侧记 FP
                result.fn += 1
                result.fp += 1
        if char in SENTENCE_END:
            result.end_total += 1
            if matched_index is not None and candidates[matched_index] == char:
                result.end_correct += 1

    for position, candidates in hyp_by_pos.items():
        for index, _char in enumerate(candidates):
            if (position, index) not in used:
                result.fp += 1
    return result


def aggregate(rows: list[PunctResult]) -> PunctSummary:
    """micro 聚合：F1 = 2TP / (2TP + FP + FN)。"""
    tp = sum(row.tp for row in rows)
    fp = sum(row.fp for row in rows)
    fn = sum(row.fn for row in rows)
    end_total = sum(row.end_total for row in rows)
    end_correct = sum(row.end_correct for row in rows)
    denominator = 2 * tp + fp + fn
    return {
        "f1": (2 * tp / denominator) if denominator else None,
        "precision": (tp / (tp + fp)) if (tp + fp) else None,
        "recall": (tp / (tp + fn)) if (tp + fn) else None,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "sentence_end_acc": (end_correct / end_total) if end_total else None,
        "sentence_end_total": end_total,
        "n": len(rows),
    }
