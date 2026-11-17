#!/usr/bin/env python3
"""真机探测 etsSTT/gateway 的转写、分离与声纹匹配协议。"""

import argparse
import asyncio
import json
import math
import sys
import wave
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.adapters.etsstt import TranscribeOutcome, extract_voiceprint, transcribe_file  # noqa: E402
from app.domain.models import Device  # noqa: E402
from app.domain.remote_staging import StagedAudio, cleanup_audio, stage_audio  # noqa: E402

DEFAULT_AUDIO = REPOSITORY_ROOT / "assets" / "probe" / "probe_two_spk_10s.wav"
DEFAULT_SPEAKER_A = REPOSITORY_ROOT / "assets" / "probe" / "probe_spk_A.wav"
DEFAULT_SPEAKER_B = REPOSITORY_ROOT / "assets" / "probe" / "probe_spk_B.wav"
TRANSCRIPTION_PATH = "/v1/audio/transcription/realtime"


def _parse_endpoint(value: str) -> tuple[str, int]:
    """校验实时转写 WebSocket 地址并返回设备主机与端口。"""
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--url 格式无效") from exc
    if parsed.scheme != "ws" or parsed.hostname is None:
        raise argparse.ArgumentTypeError("--url 必须是 ws:// 主机地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("--url 不允许用户名、密码、query 或 fragment")
    if parsed.path not in {"", TRANSCRIPTION_PATH}:
        raise argparse.ArgumentTypeError(f"--url 路径必须为 {TRANSCRIPTION_PATH}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--url 端口无效") from exc
    port = 80 if port is None else port
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--url 端口必须在 1 到 65535 之间")
    return parsed.hostname, port


def _duration_ms(path: Path) -> int:
    """读取探测 WAV 时长并检查设备协议要求的采样规格。"""
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() != 16000 or audio.getnchannels() != 1:
            raise ValueError("探测 WAV 必须为 16kHz、单声道")
        if audio.getsampwidth() != 2:
            raise ValueError("探测 WAV 必须为 16-bit PCM")
        return round(audio.getnframes() * 1000 / audio.getframerate())


def _safe_matches(speaker_match: dict[str, Any]) -> list[dict[str, Any]]:
    """只保留匹配 ID 与置信度，禁止探测结果输出声纹向量。"""
    matches: list[dict[str, Any]] = []
    for cluster_id, speaker in speaker_match.items():
        if not isinstance(speaker, dict):
            continue
        match_info = speaker.get("match_info")
        if not isinstance(match_info, dict):
            continue
        matched_id = match_info.get("matched_id")
        confidence = match_info.get("confidence")
        if not isinstance(matched_id, str):
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        if not math.isfinite(float(confidence)):
            continue
        matches.append(
            {
                "cluster_id": str(cluster_id),
                "matched_id": matched_id,
                "confidence": float(confidence),
            }
        )
    return matches


def _transcription_summary(
    outcome: TranscribeOutcome,
    *,
    audio_path: Path,
    duration_ms: int,
) -> dict[str, Any]:
    """构造不含声纹向量的转写诊断结果。"""
    usage = outcome.usage
    inference_ms = usage.get("inference_ms")
    rtf_dev = (
        float(inference_ms) / duration_ms
        if isinstance(inference_ms, (int, float))
        and not isinstance(inference_ms, bool)
        and math.isfinite(float(inference_ms))
        and inference_ms >= 0
        and duration_ms > 0
        else None
    )
    return {
        "ok": outcome.ok,
        "audio": str(audio_path),
        "audio_duration_ms": duration_ms,
        "text": outcome.text,
        "final_text": outcome.final_text,
        "segment_count": outcome.segment_count,
        "timed_segments": outcome.segments,
        "message_counts": outcome.message_counts,
        "global_spk_list": outcome.global_spk_list,
        "observed": outcome.observed,
        "usage": usage,
        "rtf_dev": rtf_dev,
        "wall_ms": outcome.wall_ms,
        "ttft_ms": outcome.ttft_ms,
        "finalize_ms": outcome.finalize_ms,
        "speaker_matches": _safe_matches(outcome.speaker_match),
        "error": outcome.error,
    }


async def _run_probe(args: argparse.Namespace, host: str, port: int) -> tuple[int, dict[str, Any]]:
    """运行设备探测，声纹文件在退出路径中清理。"""
    audio_path = args.audio.resolve()
    duration_ms = _duration_ms(audio_path)
    voiceprint_groups: list[dict[str, Any]] = []
    extraction_results: list[dict[str, Any]] = []
    staged_files: list[StagedAudio] = []
    device: Device | None = None

    try:
        if args.voiceprints:
            speaker_paths = (("A", args.speaker_a.resolve()), ("B", args.speaker_b.resolve()))
            device = Device(
                name="probe-cli",
                platform=args.platform,
                kind=args.kind,
                host=args.ssh_host or host,
                port=port,
                ssh_user=args.ssh_user,
                ssh_port=args.ssh_port,
            )
            for speaker_id, speaker_path in speaker_paths:
                speaker_duration_ms = _duration_ms(speaker_path)
                staged = stage_audio(device, speaker_path)
                staged_files.append(staged)
                extracted = await extract_voiceprint(
                    host,
                    port,
                    args.model,
                    staged.remote_path,
                    speaker_id,
                    speaker_duration_ms,
                    timeout_s=args.timeout,
                )
                extraction_results.append(
                    {
                        "id": speaker_id,
                        "ok": extracted.ok,
                        "dimensions": len(extracted.embedding),
                        "sample_duration_ms": extracted.sample_duration_ms,
                        "error": extracted.error,
                    }
                )
                if not extracted.ok or len(extracted.embedding) != 192:
                    summary = {
                        "ok": False,
                        "audio_duration_ms": duration_ms,
                        "voiceprint_extractions": extraction_results,
                        "error": extracted.error or f"声纹 {speaker_id} 不是 192 维",
                    }
                    return 1, summary
                voiceprint_groups.append(
                    {
                        "id": speaker_id,
                        "voiceprints": [
                            {
                                "embedding": extracted.embedding,
                                "weight": extracted.sample_duration_ms or speaker_duration_ms,
                            }
                        ],
                    }
                )

        outcome = await transcribe_file(
            host,
            port,
            args.model,
            str(audio_path),
            timeout_s=args.timeout,
            diarization=args.diarize or args.voiceprints,
            voiceprints=voiceprint_groups or None,
            speaker_match_threshold=args.match_threshold,
            max_num_spks=args.max_spks,
            mode=args.mode,
            chunk_ms=args.chunk_ms,
            input_rate=args.input_rate,
        )
        summary = _transcription_summary(outcome, audio_path=audio_path, duration_ms=duration_ms)
        summary["voiceprint_extractions"] = extraction_results
        voiceprint_check_ok = True
        if args.voiceprints:
            speaker_matches = cast(list[dict[str, Any]], summary["speaker_matches"])
            confident_matches = [
                match
                for match in speaker_matches
                if match["matched_id"] in {"A", "B"} and match["confidence"] > 0.5
            ]
            voiceprint_check_ok = bool(confident_matches)
            summary["voiceprint_check"] = {
                "ok": voiceprint_check_ok,
                "confidence_threshold": 0.5,
                "confident_matches": confident_matches,
            }
        success = outcome.ok and voiceprint_check_ok
        summary["ok"] = success
        return (0 if success else 1), summary
    finally:
        if device is not None:
            for staged in reversed(staged_files):
                cleanup_audio(device, staged)


def _parser() -> argparse.ArgumentParser:
    """创建真机探测命令行参数。"""
    parser = argparse.ArgumentParser(
        description="探测 etsSTT/gateway 转写、分离和声纹匹配协议；结果不含声纹向量。"
    )
    parser.add_argument(
        "--url", required=True, help="ws://host:port/v1/audio/transcription/realtime"
    )
    parser.add_argument("--model", required=True, help="设备 /v1/models 返回的 model key")
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO, help="转写 WAV")
    parser.add_argument("--diarize", action="store_true", help="启用说话人分离")
    parser.add_argument("--max-spks", type=int, default=4)
    parser.add_argument("--mode", choices=("stream", "burst"), default="stream")
    parser.add_argument("--chunk-ms", type=int, default=30)
    parser.add_argument("--input-rate", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--voiceprints", action="store_true", help="提取 A/B 声纹并验证 matched_id")
    parser.add_argument("--speaker-a", type=Path, default=DEFAULT_SPEAKER_A)
    parser.add_argument("--speaker-b", type=Path, default=DEFAULT_SPEAKER_B)
    parser.add_argument("--kind", choices=("etsstt", "gateway"), default="etsstt")
    parser.add_argument("--platform", default=None, help="设备平台号，默认按 kind 推断")
    parser.add_argument("--ssh-host", help="SSH/kubectl staging 主机；默认使用 --url 的主机")
    parser.add_argument("--ssh-user", help="设备 SSH 用户；etsstt 声纹暂存必填")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--match-threshold", type=float, default=0.6)
    parser.add_argument("--save", type=Path, help="将不含向量的 JSON 摘要保存到指定路径")
    return parser


def main() -> int:
    """校验输入、执行探测并输出可复核摘要。"""
    parser = _parser()
    args = parser.parse_args()
    try:
        host, port = _parse_endpoint(args.url)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.max_spks < 1 or args.chunk_ms < 1 or args.input_rate <= 0 or args.timeout <= 0:
        parser.error("--max-spks、--chunk-ms、--input-rate 与 --timeout 必须大于 0")
    if not 0 <= args.match_threshold <= 1:
        parser.error("--match-threshold 必须在 0 到 1 之间")
    if args.voiceprints and args.kind == "etsstt" and not args.ssh_user:
        parser.error("etsstt 声纹探测需要 --ssh-user 以 SFTP 暂存素材")
    if args.platform is None:
        args.platform = "502" if args.kind == "gateway" else "205"

    try:
        exit_code, summary = asyncio.run(_run_probe(args, host, port))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    if args.save is not None:
        args.save.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
