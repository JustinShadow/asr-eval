#!/usr/bin/env python3
"""多设备 × 多数据集只读矩阵执行器。

支持 AISHELL-1、AISHELL-3、KeSpeech 三类句级语料；计划 JSON 定义设备与数据集
（示例见 `tools/matrix_plan.example.json`）。执行约束与 M4/M10 一致：

- 音频只读，不复制源文件、不写数据库；
- 每台设备内部严格单路，设备之间可并行；
- 数据集之间串行，避免同一台设备被两个数据集同时占用；
- 只保留安全摘要（不含原文、音频或声纹）。

用法：
    cd backend && uv run python ../tools/bench_matrix.py \
      --plan ../tools/matrix_plan.json --save /tmp/matrix.json
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from bench_aishell_matrix import (  # noqa: E402
    Sample,
    Target,
    _load_samples,
    _run_target,
)

SUPPORTED_KINDS = {"aishell1", "aishell3", "kespeech"}


@dataclass(frozen=True)
class DatasetSpec:
    """一份只读语料的定位与取样子集。"""

    name: str
    kind: str
    root: Path
    subset: str
    limit: int


def _load_plan(path: Path) -> tuple[list[Target], list[DatasetSpec]]:
    """解析计划 JSON；字段错误时抛出带路径的 ValueError。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("计划 JSON 顶层必须是对象")
    devices_payload = payload.get("devices")
    datasets_payload = payload.get("datasets")
    if not isinstance(devices_payload, list) or not devices_payload:
        raise ValueError("计划必须包含非空 devices 数组")
    if not isinstance(datasets_payload, list) or not datasets_payload:
        raise ValueError("计划必须包含非空 datasets 数组")

    devices: list[Target] = []
    for index, item in enumerate(devices_payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"devices[{index}] 必须是对象")
        name = str(item.get("name", "")).strip()
        host = str(item.get("host", "")).strip()
        model = str(item.get("model", "")).strip()
        mode = str(item.get("mode", "stream")).strip()
        try:
            port = int(item["port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"devices[{index}].port 必须是整数") from exc
        if not name or not host or not model:
            raise ValueError(f"devices[{index}] 缺少 name/host/model")
        if mode not in {"burst", "stream"}:
            raise ValueError(f"devices[{index}].mode 只能是 burst 或 stream")
        devices.append(Target(name, host, port, model, mode))

    datasets: list[DatasetSpec] = []
    for index, item in enumerate(datasets_payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"datasets[{index}] 必须是对象")
        name = str(item.get("name", "")).strip()
        kind = str(item.get("kind", "")).strip()
        root_value = str(item.get("root", "")).strip()
        subset = str(item.get("subset", "test")).strip() or "test"
        try:
            limit = int(item.get("limit", 100))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"datasets[{index}].limit 必须是整数") from exc
        if not name or not root_value:
            raise ValueError(f"datasets[{index}] 缺少 name/root")
        if kind not in SUPPORTED_KINDS:
            raise ValueError(
                f"datasets[{index}].kind 不支持 {kind!r}；只能是 {sorted(SUPPORTED_KINDS)}"
            )
        if limit < 1:
            raise ValueError(f"datasets[{index}].limit 必须大于 0")
        datasets.append(DatasetSpec(name, kind, Path(root_value).expanduser(), subset, limit))

    if len({device.name for device in devices}) != len(devices):
        raise ValueError("设备 name 不能重复")
    if len({dataset.name for dataset in datasets}) != len(datasets):
        raise ValueError("数据集 name 不能重复")
    return devices, datasets


def _load_aishell3_samples(spec: DatasetSpec) -> list[Sample]:
    """AISHELL-3：content.txt 第二列是“汉字 拼音”交替序列，取偶数下标。"""
    split_root = spec.root / spec.subset
    content = split_root / "content.txt"
    if not content.is_file():
        raise FileNotFoundError(f"AISHELL-3 参考文本不存在：{content}")
    audio_root = split_root / "wav"
    if not audio_root.is_dir():
        raise FileNotFoundError(f"AISHELL-3 split 音频目录不存在：{audio_root}")

    samples: list[Sample] = []
    for line in content.read_text(encoding="utf-8").splitlines():
        if len(samples) == spec.limit:
            break
        if "\t" not in line:
            continue
        filename, transcript = line.split("\t", 1)
        text = "".join(transcript.split()[::2])
        speaker = filename.split("_")[0].split(".")[0][:7]
        audio = audio_root / speaker / filename
        if not text or not audio.is_file():
            continue
        samples.append(Sample(f"{spec.subset}-{filename}", audio, text))
    if len(samples) != spec.limit:
        raise ValueError(
            f"AISHELL-3 {spec.subset} 只找到 {len(samples)} 条带参考音频，"
            f"少于要求的 {spec.limit} 条"
        )
    return samples


def _load_kespeech_samples(spec: DatasetSpec) -> list[Sample]:
    """KeSpeech：方言目录下 wav/txt 同名配对，文本为单行纯文本。"""
    if not spec.root.is_dir():
        raise FileNotFoundError(f"KeSpeech 根目录不存在：{spec.root}")
    roots = (
        [spec.root]
        if _has_pairs(spec.root)
        else [
            child for child in sorted(spec.root.iterdir()) if child.is_dir() and _has_pairs(child)
        ]
    )
    samples: list[Sample] = []
    for dialect_dir in roots:
        for txt in sorted(dialect_dir.glob("*.txt")):
            if len(samples) == spec.limit:
                break
            audio = txt.with_suffix(".wav")
            text = txt.read_text(encoding="utf-8").strip()
            if not audio.is_file() or not text:
                continue
            samples.append(Sample(f"{dialect_dir.name}-{txt.stem}", audio, text))
        if len(samples) == spec.limit:
            break
    if len(samples) != spec.limit:
        raise ValueError(f"KeSpeech 只找到 {len(samples)} 条带参考音频，少于要求的 {spec.limit} 条")
    return samples


def _has_pairs(path: Path) -> bool:
    """目录下同时存在 .wav 与 .txt 才视为一个语料目录。"""
    return path.is_dir() and any(path.glob("*.wav")) and any(path.glob("*.txt"))


def _load_dataset(spec: DatasetSpec) -> list[Sample]:
    """按数据集类型加载只读样本。"""
    if spec.kind == "aishell1":
        return _load_samples(spec.root, spec.subset, spec.limit)
    if spec.kind == "aishell3":
        return _load_aishell3_samples(spec)
    if spec.kind == "kespeech":
        return _load_kespeech_samples(spec)
    raise ValueError(f"不支持的数据集类型：{spec.kind}")


def _is_device_audio(path: Path) -> bool:
    """设备适配器只接受 16kHz/16-bit/单声道 WAV。"""
    try:
        with wave.open(str(path), "rb") as audio:
            return (
                audio.getframerate() == 16000
                and audio.getnchannels() == 1
                and audio.getsampwidth() == 2
            )
    except (wave.Error, EOFError):
        return False


def _prepare_samples(samples: list[Sample], work_dir: Path) -> list[Sample]:
    """把非 16k 音频转为临时缓存；源文件只读，不修改、不覆盖。"""
    prepared: list[Sample] = []
    for index, sample in enumerate(samples):
        if _is_device_audio(sample.audio_path):
            prepared.append(sample)
            continue
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError(f"音频不是 16kHz 单声道，且未找到 ffmpeg：{sample.audio_path}")
        target = work_dir / f"{index:05d}.wav"
        subprocess.run(  # noqa: S603 - 固定 ffmpeg 参数，仅处理本次只读样本。
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(sample.audio_path),
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
        prepared.append(Sample(sample.item_key, target, sample.reference))
    return prepared


async def _run_matrix(
    devices: list[Target],
    datasets: list[DatasetSpec],
    *,
    chunk_ms: int,
    timeout_s: float,
    progress_every: int,
) -> dict[str, Any]:
    """数据集串行、设备并行执行，返回可持久化的安全摘要。"""
    dataset_results: list[dict[str, Any]] = []
    for spec in datasets:
        samples = _load_dataset(spec)
        print(f"[{spec.name}] {len(samples)} 条样本，{len(devices)} 台设备", file=sys.stderr)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="asr-matrix-") as work_dir:
            prepared = _prepare_samples(samples, Path(work_dir))
            targets = await asyncio.gather(
                *(
                    _run_target(
                        device,
                        prepared,
                        timeout_s=timeout_s,
                        chunk_ms=chunk_ms,
                        progress_every=progress_every,
                    )
                    for device in devices
                )
            )
        dataset_results.append(
            {
                "name": spec.name,
                "kind": spec.kind,
                "subset": spec.subset,
                "sample_count": len(samples),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "targets": targets,
            }
        )
    return {
        "datasets": dataset_results,
        "profile": {
            "input_rate": 1.0,
            "chunk_ms": chunk_ms,
            "timeout_s": timeout_s,
            "per_device_concurrency": 1,
            "dataset_concurrency": 1,
        },
    }


def _print_matrix(result: dict[str, Any]) -> None:
    """打印“数据集 × 设备”矩阵；N/A 表示该设备不产出该指标。"""
    print(
        "数据集 | 设备 | 模式 | 成功率 | micro CER | 字符数 | "
        "TTFT median/P95 ms | finalize median/P95 ms | RTF_dev median/P95"
    )
    print("--- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---:")
    for dataset in result["datasets"]:
        for target in dataset["targets"]:
            cer_value = target["cer"]
            ttft = target["ttft_ms_median"]
            ttft_p95 = target["ttft_ms_p95"]
            finalize = target["finalize_ms_median"]
            finalize_p95 = target["finalize_ms_p95"]
            rtf = target["rtf_dev_median"]
            rtf_p95 = target["rtf_dev_p95"]
            cer_display = f"{cer_value:.2%}" if cer_value is not None else "N/A"
            row = (
                f"{dataset['name']} | {target['target']['name']} | {target['target']['mode']} | "
                f"{target['successes']}/{target['samples']} | {cer_display} | "
                f"{target['reference_chars']}"
            )
            if all(
                value is not None
                for value in (ttft, ttft_p95, finalize, finalize_p95, rtf, rtf_p95)
            ):
                row += (
                    f" | {ttft:.0f}/{ttft_p95:.0f} | {finalize:.0f}/{finalize_p95:.0f} | "
                    f"{rtf:.3f}/{rtf_p95:.3f}"
                )
            else:
                row += " | N/A | N/A | N/A"
            print(row)


def _parser() -> argparse.ArgumentParser:
    """创建 3×3 矩阵参数。"""
    parser = argparse.ArgumentParser(
        description="按计划 JSON 在只读语料上执行多设备 × 多数据集 CER 矩阵。"
    )
    parser.add_argument("--plan", required=True, type=Path, help="设备与数据集计划 JSON")
    parser.add_argument("--chunk-ms", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--save", type=Path, help="可选 JSON 汇总路径；不含原文和音频")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="只校验计划与样本可读性，不连接设备",
    )
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    """校验计划并按需执行矩阵。"""
    devices, datasets = _load_plan(args.plan)
    if args.validate_only:
        for spec in datasets:
            samples = _load_dataset(spec)
            print(f"[OK] {spec.name} ({spec.kind}) {len(samples)} 条样本")
        print(f"[OK] {len(devices)} 台设备、{len(datasets)} 个数据集计划可执行")
        return 0

    result = await _run_matrix(
        devices,
        datasets,
        chunk_ms=args.chunk_ms,
        timeout_s=args.timeout,
        progress_every=args.progress_every,
    )
    _print_matrix(result)
    if args.save is not None:
        args.save.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"矩阵摘要已保存：{args.save}", file=sys.stderr)
    return 0


def main() -> int:
    """命令行入口。"""
    args = _parser().parse_args()
    if args.chunk_ms < 1 or args.progress_every < 1 or args.timeout <= 0:
        raise SystemExit("--chunk-ms/--progress-every 必须大于 0，--timeout 必须大于 0")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
