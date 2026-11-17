#!/usr/bin/env python3
"""AliMeeting near 句段标点 F1 基准（205 etsstt，100 条）。

参考文本来自 TextGrid 的 IntervalTier，音频按区间从原 WAV 临时裁切为 16k 单声道；
源音频只读。当前只评 CER 与标点 F1/句末准确率；命名需要逐说话人声纹注册，
未提供注册音频时返回 `na(no_voiceprint_enrollment)`，不伪造准确率。

用法：
    cd backend && uv run python ../tools/bench_punctuation.py \
      --near-root "/mnt/f/.../alimeeting/Test_Ali/Test_Ali_near" \
      --host 127.0.0.1 --port 18705 --limit 100 --save /tmp/punct.json
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.adapters.etsstt import transcribe_file  # noqa: E402
from app.importers.textgrid import parse_textgrid  # noqa: E402
from app.scoring.cer import cer  # noqa: E402
from app.scoring.punct import score_punctuation  # noqa: E402


@dataclass(frozen=True)
class PunctCase:
    """一条可评分句段：会议、参考文本、源 WAV 与时间范围。"""

    meeting: str
    reference: str
    audio: Path
    start_s: float
    end_s: float


def _load_cases(near_root: Path, limit: int) -> list[PunctCase]:
    """按会议与时间顺序取前 N 条带标点句段。"""
    audio_dir = near_root / "audio_dir"
    textgrid_dir = near_root / "textgrid_dir"
    if not audio_dir.is_dir() or not textgrid_dir.is_dir():
        raise FileNotFoundError(f"AliMeeting near 目录不完整：{near_root}")
    cases: list[PunctCase] = []
    for textgrid in sorted(textgrid_dir.glob("*.TextGrid")):
        audio = audio_dir / f"{textgrid.stem}.wav"
        if not audio.is_file():
            continue
        for tier in parse_textgrid(textgrid):
            for interval in tier.intervals:
                text = interval.text.strip()
                if not text or not any(char in text for char in "，。！？、；："):
                    continue
                if interval.end_s <= interval.start_s:
                    continue
                cases.append(
                    PunctCase(
                        meeting=textgrid.stem,
                        reference=text,
                        audio=audio,
                        start_s=interval.start_s,
                        end_s=interval.end_s,
                    )
                )
                if len(cases) == limit:
                    return cases
    if len(cases) != limit:
        raise ValueError(f"只找到 {len(cases)} 条带标点句段，少于要求的 {limit} 条")
    return cases


def _cut_segment(case: PunctCase, target: Path) -> None:
    """把句段裁成临时 16k/16bit/mono WAV，源文件只读。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("裁切 AliMeeting 音频需要 ffmpeg")
    duration = max(0.01, case.end_s - case.start_s)
    subprocess.run(  # noqa: S603 - 固定 ffmpeg 参数，仅处理只读源音频。
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{case.start_s:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(case.audio),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-sample_fmt",
            "s16",
            str(target),
        ],
        check=True,
        capture_output=True,
    )


async def _run_case(
    case: PunctCase,
    target: Path,
    *,
    host: str,
    port: int,
    model: str,
    timeout_s: float,
    chunk_ms: int,
) -> dict[str, Any]:
    """裁切并识别一条句段，返回安全摘要。"""
    _cut_segment(case, target)
    started = time.perf_counter()
    try:
        outcome = await transcribe_file(
            host,
            port,
            model,
            str(target),
            timeout_s=timeout_s,
            mode="burst",
            chunk_ms=chunk_ms,
            input_rate=1.0,
        )
    except Exception as exc:
        return {
            "meeting": case.meeting,
            "start_s": case.start_s,
            "end_s": case.end_s,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:240],
        }
    wall_ms = round((time.perf_counter() - started) * 1000)
    if not outcome.ok:
        return {
            "meeting": case.meeting,
            "start_s": case.start_s,
            "end_s": case.end_s,
            "ok": False,
            "wall_ms": wall_ms,
            "error": (outcome.error or "设备转写失败")[:240],
        }
    cer_result = cer(case.reference, outcome.text)
    punct = score_punctuation(case.reference, outcome.text)
    return {
        "meeting": case.meeting,
        "start_s": case.start_s,
        "end_s": case.end_s,
        "ok": True,
        "wall_ms": wall_ms,
        "cer": cer_result.value,
        "edits": cer_result.edits,
        "reference_chars": cer_result.n,
        "punct": {
            "tp": punct.tp,
            "fp": punct.fp,
            "fn": punct.fn,
            "ref_punct": punct.ref_punct,
            "hyp_punct": punct.hyp_punct,
            "end_total": punct.end_total,
            "end_correct": punct.end_correct,
        },
    }


def _summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总 micro CER、标点 P/R/F1 与句末准确率。"""
    successful = [case for case in cases if case.get("ok")]
    edits = sum(int(case["edits"]) for case in successful)
    reference_chars = sum(int(case["reference_chars"]) for case in successful)
    tp = sum(int(case["punct"]["tp"]) for case in successful)
    fp = sum(int(case["punct"]["fp"]) for case in successful)
    fn = sum(int(case["punct"]["fn"]) for case in successful)
    end_total = sum(int(case["punct"]["end_total"]) for case in successful)
    end_correct = sum(int(case["punct"]["end_correct"]) for case in successful)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "samples": len(cases),
        "successes": len(successful),
        "success_rate": len(successful) / len(cases) if cases else 0.0,
        "cer": edits / reference_chars if reference_chars else None,
        "edits": edits,
        "reference_chars": reference_chars,
        "punct_f1": f1,
        "punct_precision": precision,
        "punct_recall": recall,
        "punct_tp": tp,
        "punct_fp": fp,
        "punct_fn": fn,
        "sentence_end_acc": end_correct / end_total if end_total else None,
        "sentence_end_total": end_total,
        "naming": {
            "status": "na",
            "reason": "no_voiceprint_enrollment",
            "note": "命名需要逐说话人声纹注册；本工具只评标点/CER",
        },
    }


def _parser() -> argparse.ArgumentParser:
    """创建 205 标点基准参数。"""
    parser = argparse.ArgumentParser(description="AliMeeting near 句段标点 F1 基准（205）")
    parser.add_argument("--near-root", required=True, type=Path, help="Test_Ali_near 目录")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model", default="etsstt")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--chunk-ms", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--save", type=Path)
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    """逐条裁切/识别并汇总。"""
    cases = _load_cases(args.near_root, args.limit)
    print(f"标点基准：{len(cases)} 条句段", file=sys.stderr)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="asr-punct-") as work_dir:
        for index, case in enumerate(cases, start=1):
            results.append(
                await _run_case(
                    case,
                    Path(work_dir) / f"{index:05d}.wav",
                    host=args.host,
                    port=args.port,
                    model=args.model,
                    timeout_s=args.timeout,
                    chunk_ms=args.chunk_ms,
                )
            )
            if index % args.progress_every == 0 or index == len(cases):
                print(f"{index}/{len(cases)}", file=sys.stderr, flush=True)
    summary = _summarize(results)
    summary["cases"] = results
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.save is not None:
        args.save.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if summary["successes"] == summary["samples"] else 1


def main() -> int:
    """命令行入口。"""
    args = _parser().parse_args()
    if args.limit < 1 or args.progress_every < 1 or args.chunk_ms < 1:
        raise SystemExit("--limit/--progress-every/--chunk-ms 必须大于 0")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
