"""用 AliMeeting 官方 Test_Ali_far 标注核对 DER 评分引擎。"""

from __future__ import annotations

import argparse
import itertools
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyannote.core import Annotation, Timeline
from pyannote.core import Segment as PyannoteSegment
from pyannote.metrics.diarization import DiarizationErrorRate

from app.importers.textgrid import parse_textgrid
from app.scoring.speaker import der as project_der


@dataclass(frozen=True)
class Sample:
    """一场会议的参考标注、扰动假设与完整音频时长。"""

    file_id: str
    duration_s: float
    reference: tuple[tuple[str, int, int], ...]
    hypothesis: tuple[tuple[str, int, int], ...]


def _load_samples(dataset_root: Path) -> list[Sample]:
    """读取 TextGrid，并从配对 WAV 头部取得 UEM 终点。"""
    textgrid_dir = dataset_root / "textgrid_dir"
    audio_dir = dataset_root / "audio_dir"
    grids = sorted(textgrid_dir.glob("*.TextGrid"))
    if not grids:
        raise ValueError(f"TextGrid 目录为空：{textgrid_dir}")

    samples: list[Sample] = []
    for grid in grids:
        audios = sorted(audio_dir.glob(f"{grid.stem}_MS*.wav"))
        if len(audios) != 1:
            raise ValueError(f"{grid.stem} 应配对一个远场 WAV，实际找到 {len(audios)} 个")
        with wave.open(str(audios[0]), "rb") as audio:
            duration_s = audio.getnframes() / audio.getframerate()

        segments = [
            (
                tier.name,
                round(interval.start_s * 1000),
                round(interval.end_s * 1000),
            )
            for tier in parse_textgrid(grid)
            for interval in tier.intervals
            if interval.text.strip() and interval.end_s > interval.start_s
        ]
        reference = tuple(sorted(segments, key=lambda item: (item[1], item[2], item[0])))
        if not reference:
            raise ValueError(f"{grid.name} 没有可评分的说话人区间")
        hypothesis = _make_probe_hypothesis(reference, round(duration_s * 1000))
        samples.append(Sample(grid.stem, duration_s, reference, hypothesis))

    return samples


def _make_probe_hypothesis(
    reference: tuple[tuple[str, int, int], ...], duration_ms: int
) -> tuple[tuple[str, int, int], ...]:
    """固定生成包含 miss、confusion、false alarm 的非零假设。"""
    speakers = sorted({segment[0] for segment in reference})
    if len(speakers) < 2:
        raise ValueError("DER 探测样例至少需要两个说话人")

    by_duration = sorted(
        range(len(reference)),
        key=lambda index: (reference[index][2] - reference[index][1], -index),
        reverse=True,
    )
    miss_index, confusion_index = by_duration[:2]
    miss_target = reference[miss_index]
    confusion_target = reference[confusion_index]
    hypothesis = [segment for index, segment in enumerate(reference) if index != miss_index]

    # 把另一段最长区间改成其他说话人，形成可测的簇混淆。
    confusion_position = next(
        index for index, segment in enumerate(hypothesis) if segment == confusion_target
    )
    speaker, start, end = hypothesis[confusion_position]
    wrong_speaker = speakers[(speakers.index(speaker) + 1) % len(speakers)]
    hypothesis[confusion_position] = (wrong_speaker, start, end)

    # 选一段之后至少有 800ms 静音的区间，将其延长 400ms 形成 false alarm。
    ordered = sorted(reference, key=lambda segment: (segment[1], segment[2], segment[0]))
    false_alarm_target: tuple[str, int, int] | None = None
    for index, segment in enumerate(ordered[:-1]):
        next_start = ordered[index + 1][1]
        if (
            segment not in (miss_target, confusion_target)
            and next_start - segment[2] >= 800
            and segment[2] + 400 <= duration_ms
        ):
            false_alarm_target = segment
            break
    if false_alarm_target is None:
        raise ValueError("未找到可构造 false alarm 的静音区间")
    for index, segment in enumerate(hypothesis):
        if segment == false_alarm_target:
            hypothesis[index] = (segment[0], segment[1], segment[2] + 400)
            break

    return tuple(sorted(hypothesis, key=lambda item: (item[1], item[2], item[0])))


def _as_pyannote(segments: tuple[tuple[str, int, int], ...], uri: str) -> Annotation:
    annotation = Annotation(uri=uri)
    for speaker, start_ms, end_ms in segments:
        annotation[PyannoteSegment(start_ms / 1000, end_ms / 1000)] = speaker
    return annotation


def _load_dscore(dscore_dir: Path) -> tuple[Any, Any, Any, Any]:
    if not (dscore_dir / "scorelib" / "md-eval-22.pl").is_file():
        raise ValueError(f"dscore 目录缺少 scorelib/md-eval-22.pl：{dscore_dir}")
    sys.path.insert(0, str(dscore_dir))
    from scorelib.metrics import der as dscore_der
    from scorelib.rttm import write_rttm
    from scorelib.turn import Turn
    from scorelib.uem import UEM, write_uem

    return dscore_der, write_rttm, Turn, (UEM, write_uem)


def _rttm_inputs(
    samples: list[Sample], turn_type: Any, uem_type: Any
) -> tuple[list[Any], list[Any], Any]:
    reference_turns: list[Any] = []
    hypothesis_turns: list[Any] = []
    uem = uem_type()
    for sample in samples:
        uem[sample.file_id] = [(0.0, sample.duration_s)]
        for target, segments in (
            (reference_turns, sample.reference),
            (hypothesis_turns, sample.hypothesis),
        ):
            target.extend(
                turn_type(
                    start_ms / 1000,
                    offset=end_ms / 1000,
                    speaker_id=speaker,
                    file_id=sample.file_id,
                )
                for speaker, start_ms, end_ms in segments
            )
    return reference_turns, hypothesis_turns, uem


def _score_md_eval(
    reference_turns: list[Any],
    hypothesis_turns: list[Any],
    uem: Any,
    write_rttm: Any,
    write_uem: Any,
    md_eval_path: Path,
) -> tuple[float, dict[str, float]]:
    perl = shutil.which("perl")
    if perl is None:
        raise RuntimeError("未找到 Perl，无法运行 md-eval-22.pl")
    with tempfile.TemporaryDirectory(prefix="der-crosscheck-") as temp_dir:
        directory = Path(temp_dir)
        reference_path = directory / "reference.rttm"
        hypothesis_path = directory / "hypothesis.rttm"
        uem_path = directory / "evaluation.uem"
        write_rttm(str(reference_path), reference_turns)
        write_rttm(str(hypothesis_path), hypothesis_turns)
        write_uem(str(uem_path), uem)
        output = subprocess.run(  # noqa: S603 - 固定运行本地 md-eval Perl 脚本
            [
                perl,
                str(md_eval_path),
                "-af",
                "-r",
                str(reference_path),
                "-s",
                str(hypothesis_path),
                "-u",
                str(uem_path),
                "-c",
                "0.25",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    return _parse_md_eval_results(output)


def _parse_md_eval_results(output: str) -> tuple[float, dict[str, float]]:
    """解析 md-eval 的逐会话及 ALL 汇总 DER。"""
    blocks = re.findall(
        r"\*\*\* Performance analysis for Speaker Diarization for "
        r"(?:f=)?([^\s*]+) \*\*\*(.*?)(?="
        r"\*\*\* Performance analysis for Speaker Diarization for |\Z)",
        output,
        flags=re.DOTALL,
    )
    if not blocks:
        raise RuntimeError("无法从 md-eval 输出解析会话结果")

    results: dict[str, float] = {}
    global_value: float | None = None
    metrics = (
        ("scored", "SCORED SPEAKER TIME"),
        ("miss", "MISSED SPEAKER TIME"),
        ("fa", "FALARM SPEAKER TIME"),
        ("conf", "SPEAKER ERROR TIME"),
    )
    for file_id, block in blocks:
        totals = {
            name: re.search(rf"{pattern} =([0-9.]+) secs", block) for name, pattern in metrics
        }
        if any(value is None for value in totals.values()):
            raise RuntimeError(f"无法从 md-eval 输出解析 DER 分项：{file_id}")

        scored_match = totals["scored"]
        if scored_match is None:
            raise RuntimeError(f"md-eval 缺少计分时长：{file_id}")
        scored = float(scored_match.group(1))
        if scored <= 0:
            raise RuntimeError(f"md-eval 的参考说话人时长为 0：{file_id}")
        errors = sum(
            float(value.group(1))
            for name in ("miss", "fa", "conf")
            if (value := totals[name]) is not None
        )
        value = errors / scored
        if file_id == "ALL":
            global_value = value
        else:
            if file_id in results:
                raise RuntimeError(f"md-eval 重复输出会话：{file_id}")
            results[file_id] = value

    if global_value is None:
        raise RuntimeError("md-eval 输出缺少 ALL 汇总")
    if not results:
        raise RuntimeError("md-eval 输出缺少逐会话结果")
    return global_value, results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--dscore-dir", type=Path, required=True)
    args = parser.parse_args()

    samples = _load_samples(args.dataset_root)
    if len(samples) != 20:
        raise AssertionError(f"AliMeeting Test_Ali_far 应有 20 段，实际为 {len(samples)} 段")

    dscore_der, write_rttm, turn_type, writers = _load_dscore(args.dscore_dir)
    uem_type, write_uem = writers
    reference_turns, hypothesis_turns, uem = _rttm_inputs(samples, turn_type, uem_type)

    local_by_file: dict[str, float] = {}
    local_miss = local_fa = local_conf = local_ref = 0
    pyannote_metric = DiarizationErrorRate(collar=0.5, skip_overlap=False)
    pyannote_by_file: dict[str, float] = {}
    for sample in samples:
        result = project_der(
            sample.reference,
            sample.hypothesis,
            round(sample.duration_s * 1000),
            collar_ms=250,
        )
        if result["value"] is None:
            raise RuntimeError(f"项目 DER 无有效分母：{sample.file_id}")
        local_by_file[sample.file_id] = float(result["value"])
        local_miss += int(result["miss_frames"])
        local_fa += int(result["fa_frames"])
        local_conf += int(result["conf_frames"])
        local_ref += int(result["ref_frames"])

        pyannote_value = pyannote_metric(
            _as_pyannote(sample.reference, sample.file_id),
            _as_pyannote(sample.hypothesis, sample.file_id),
            uem=Timeline([PyannoteSegment(0, sample.duration_s)]),
        )
        pyannote_by_file[sample.file_id] = float(pyannote_value)

    local_global = (local_miss + local_fa + local_conf) / local_ref
    dscore_by_file, dscore_percent = dscore_der(
        reference_turns,
        hypothesis_turns,
        collar=0.25,
        ignore_overlaps=False,
        uem=uem,
    )
    dscore_global = dscore_percent / 100
    md_eval_global, md_eval_by_file = _score_md_eval(
        reference_turns,
        hypothesis_turns,
        uem,
        write_rttm,
        write_uem,
        args.dscore_dir / "scorelib" / "md-eval-22.pl",
    )
    if md_eval_by_file.keys() != local_by_file.keys():
        missing = sorted(local_by_file.keys() - md_eval_by_file.keys())
        extra = sorted(md_eval_by_file.keys() - local_by_file.keys())
        raise AssertionError(f"md-eval 会话集合与 TextGrid 不一致：缺失={missing}，多余={extra}")
    pyannote_global = float(abs(pyannote_metric))

    values = {
        "项目 DER": local_global,
        "dscore": dscore_global,
        "md-eval.pl": md_eval_global,
        "pyannote": pyannote_global,
    }
    spreads = [abs(left - right) for left, right in itertools.combinations(values.values(), 2)]
    max_spread = max(spreads)
    per_session_spread = max(
        max(
            abs(left - right)
            for left, right in itertools.combinations(
                (
                    local_by_file[file_id],
                    dscore_by_file[file_id] / 100,
                    md_eval_by_file[file_id],
                    pyannote_by_file[file_id],
                ),
                2,
            )
        )
        for file_id in local_by_file
    )

    print(f"AliMeeting Test_Ali_far 会话数：{len(samples)}")
    print("探测假设：每段包含 miss、confusion、false alarm；重叠语音计分")
    print(
        "项目 DER 分项（10ms 帧）："
        f"miss={local_miss}，FA={local_fa}，confusion={local_conf}，"
        f"参考={local_ref}"
    )
    for name, value in values.items():
        print(f"{name}：{value * 100:.6f}%")
    print(f"全量汇总最大差值：{max_spread * 100:.6f}pp")
    print(f"md-eval 逐会话结果：{len(md_eval_by_file)} 场")
    print(f"单会话四引擎最大差值：{per_session_spread * 100:.6f}pp")

    if local_global <= 0 or dscore_global <= 0 or pyannote_global <= 0:
        raise AssertionError("探测假设应产生非零 DER")
    if min(local_miss, local_fa, local_conf) <= 0:
        raise AssertionError("探测假设没有同时覆盖 miss、false alarm 与 confusion")
    if abs(dscore_global - md_eval_global) > 1e-8:
        raise AssertionError("dscore 封装结果与直接 md-eval 输出不一致")
    if max_spread >= 0.002 or per_session_spread >= 0.002:
        raise AssertionError("DER 引擎差值达到或超过设计门槛 0.2pp")


if __name__ == "__main__":
    main()
