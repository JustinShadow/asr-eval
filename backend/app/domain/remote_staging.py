"""把内置声纹录音暂存到设备侧可读取的目录。"""

import hashlib
import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.domain.models import Device

ETSSTT_VOICEPRINT_DIR = "/data/voiceprint"
GATEWAY_VOICEPRINT_DIR = "/mnt/voiceprint"
STAGING_COMMAND_TIMEOUT_S = 30


class StagingError(RuntimeError):
    """设备端暂存失败或校验不一致。"""


@dataclass(frozen=True)
class StagedAudio:
    """已验证的设备侧音频路径和传输信息。"""

    name: str
    remote_path: str
    sha256: str
    method: str
    host_dir: str | None
    container_dir: str | None


def stage_audio(
    device: Device,
    local_path: Path,
    *,
    preferred_method: str | None = None,
) -> StagedAudio:
    """上传一个探测 WAV，并要求设备侧 SHA-256 与本地一致。"""
    if not local_path.is_file():
        raise StagingError(f"本地探测音频不存在：{local_path.name}")
    sha256 = _file_sha256(local_path)
    name = f"{sha256[:16]}.wav"
    if device.kind == "etsstt":
        remote_path = f"{ETSSTT_VOICEPRINT_DIR}/{name}"
        _stage_etsstt(device, local_path, remote_path, sha256)
        return StagedAudio(
            name=name,
            remote_path=remote_path,
            sha256=sha256,
            method="sftp",
            host_dir=ETSSTT_VOICEPRINT_DIR,
            container_dir=ETSSTT_VOICEPRINT_DIR,
        )
    if device.kind == "gateway":
        remote_path = f"{GATEWAY_VOICEPRINT_DIR}/{name}"
        method = _stage_gateway(
            device, local_path, remote_path, sha256, preferred_method=preferred_method
        )
        return StagedAudio(
            name=name,
            remote_path=remote_path,
            sha256=sha256,
            method=method,
            host_dir=None,
            container_dir=GATEWAY_VOICEPRINT_DIR,
        )
    raise StagingError("云端设备不支持文件暂存和声纹自检")


def cleanup_audio(device: Device, staged: StagedAudio) -> None:
    """尽力删除自检临时文件，不覆盖主流程结果。"""
    try:
        if staged.method == "sftp":
            target, _ = _ssh_target(device)
            command = [
                "ssh",
                "-p",
                str(device.ssh_port),
                "-oBatchMode=yes",
                "-oConnectTimeout=10",
                target,
                f"rm -f -- '{staged.remote_path}'",
            ]
            _run(command, timeout=STAGING_COMMAND_TIMEOUT_S)
            return
        if device.kind == "gateway" and device.ssh_user:
            _gateway_ssh(
                device,
                (
                    "exec",
                    "-n",
                    "ns-meagt",
                    "deploy/etsllm",
                    "--",
                    "rm",
                    "-f",
                    "--",
                    staged.remote_path,
                ),
            )
            return
        kubectl = shutil.which("kubectl")
        if kubectl is not None:
            _run(
                [kubectl, "exec", "deploy/etsllm", "--", "rm", "-f", staged.remote_path],
                timeout=STAGING_COMMAND_TIMEOUT_S,
            )
    except (OSError, subprocess.SubprocessError, StagingError):
        return


def _stage_etsstt(device: Device, local_path: Path, remote_path: str, sha256: str) -> None:
    """通过设备 SSH key/agent 使用 SFTP 暂存并校验文件。"""
    if not device.ssh_user:
        raise StagingError("设备未配置 SSH 用户，无法暂存声纹音频")
    target, args = _ssh_target(device)
    local = _sftp_quote(str(local_path.resolve()))
    remote = _sftp_quote(remote_path)
    batch = f"-mkdir {ETSSTT_VOICEPRINT_DIR}\nput {local} {remote}\n"
    sftp = shutil.which("sftp")
    if sftp is None:
        raise StagingError("运行环境未安装 sftp")
    _run(
        [sftp, "-q", "-b", "-", *args, "-oBatchMode=yes", "-oConnectTimeout=10", target],
        input_text=batch,
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )
    actual = _remote_sha256_ssh(device, target, remote_path)
    if actual != sha256:
        raise StagingError("SFTP 上传后的 SHA-256 与本地文件不一致")


def _stage_gateway(
    device: Device,
    local_path: Path,
    remote_path: str,
    sha256: str,
    *,
    preferred_method: str | None,
) -> str:
    """SSH 配置时在网关宿主 exec-cat；否则用服务端 kubectl 并保留 cp 回退（M2 §5.4）。"""
    if device.ssh_user:
        try:
            _gateway_ssh_exec_cat(device, local_path, remote_path)
            actual = _remote_sha256_gateway_ssh(device, remote_path)
            if actual != sha256:
                raise StagingError("网关容器内文件 SHA-256 与本地文件不一致")
            return "exec_cat"
        except (OSError, subprocess.SubprocessError, StagingError) as exc:
            raise StagingError(f"网关 SSH/kubectl exec-cat 暂存失败：{str(exc)[:300]}") from exc

    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise StagingError("运行环境未安装 kubectl，无法向网关容器暂存音频")
    methods = ["exec_cat", "kubectl_cp"]
    if preferred_method in methods:
        methods.remove(preferred_method)
        methods.insert(0, preferred_method)
    errors: list[str] = []
    for method in methods:
        try:
            if method == "exec_cat":
                _gateway_exec_cat(kubectl, local_path, remote_path)
            else:
                _gateway_kubectl_cp(kubectl, local_path, remote_path)
            actual = _remote_sha256_kubectl(kubectl, remote_path)
            if actual != sha256:
                raise StagingError("网关容器内文件 SHA-256 与本地文件不一致")
            return method
        except (OSError, subprocess.SubprocessError, StagingError) as exc:
            errors.append(f"{method}: {str(exc)[:300]}")
    raise StagingError("网关暂存方式均失败；" + "；".join(errors))


def _gateway_ssh_exec_cat(device: Device, local_path: Path, remote_path: str) -> None:
    """经网关 SSH 宿主转发 stdin 到 kubectl，开发机不需要本地集群上下文。"""
    shell_command = (
        f"mkdir -p {shlex.quote(GATEWAY_VOICEPRINT_DIR)} && cat > {shlex.quote(remote_path)}"
    )
    _gateway_ssh(
        device,
        (
            "exec",
            "-i",
            "-n",
            "ns-meagt",
            "deploy/etsllm",
            "--",
            "sh",
            "-c",
            shell_command,
        ),
        input_bytes=local_path.read_bytes(),
    )


def _remote_sha256_gateway_ssh(device: Device, remote_path: str) -> str:
    """通过网关宿主 kubectl 读取容器内文件的 SHA-256。"""
    result = _gateway_ssh(
        device,
        ("exec", "-n", "ns-meagt", "deploy/etsllm", "--", "sha256sum", "--", remote_path),
        text_output=True,
    )
    return _parse_sha256(result)


def _gateway_ssh(
    device: Device,
    kubectl_args: tuple[str, ...],
    *,
    input_bytes: bytes | None = None,
    text_output: bool = False,
) -> subprocess.CompletedProcess[str | bytes]:
    """在配置设备宿主的 SSH 主机上执行固定参数化 kubectl 子命令。"""
    target, _ = _ssh_target(device)
    ssh = shutil.which("ssh")
    if ssh is None:
        raise StagingError("运行环境未安装 ssh，无法连接网关宿主")
    remote_command = shlex.join(("sudo", "-n", "kubectl", *kubectl_args))
    return _run(
        [
            ssh,
            "-p",
            str(device.ssh_port),
            "-oBatchMode=yes",
            "-oConnectTimeout=10",
            target,
            remote_command,
        ],
        input_bytes=input_bytes,
        text_output=text_output,
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )


def _gateway_exec_cat(kubectl: str, local_path: Path, remote_path: str) -> None:
    """通过 kubectl stdin 写入容器，不依赖容器内 tar。"""
    shell_command = f"mkdir -p {GATEWAY_VOICEPRINT_DIR} && cat > {remote_path}"
    _run(
        [kubectl, "exec", "-i", "deploy/etsllm", "--", "sh", "-c", shell_command],
        input_bytes=local_path.read_bytes(),
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )


def _gateway_kubectl_cp(kubectl: str, local_path: Path, remote_path: str) -> None:
    """尝试 kubectl cp 作为网关 stdin 暂存失败后的降级。"""
    pod = _gateway_running_pod(kubectl)
    _run(
        [kubectl, "exec", pod, "--", "mkdir", "-p", GATEWAY_VOICEPRINT_DIR],
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )
    _run(
        [kubectl, "cp", str(local_path), f"{pod}:{remote_path}"],
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )


def _gateway_running_pod(kubectl: str) -> str:
    """按 Deployment selector 找到可供 kubectl cp 使用的运行中 Pod。"""
    deployment_result = _run(
        [kubectl, "get", "deployment", "etsllm", "-o", "json"],
        text_output=True,
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )
    try:
        deployment = json.loads(str(deployment_result.stdout))
        labels = deployment["spec"]["selector"]["matchLabels"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise StagingError("无法读取 etsllm Deployment 的 Pod selector") from exc
    if not isinstance(labels, dict) or not labels:
        raise StagingError("etsllm Deployment 未配置 matchLabels")
    selector_parts: list[str] = []
    for key, value in labels.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise StagingError("etsllm Pod selector 格式无效")
        if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", key) or not re.fullmatch(
            r"[A-Za-z0-9_.-]*", value
        ):
            raise StagingError("etsllm Pod selector 含无效标签")
        selector_parts.append(f"{key}={value}")
    pods_result = _run(
        [
            kubectl,
            "get",
            "pods",
            "-l",
            ",".join(selector_parts),
            "--field-selector=status.phase=Running",
            "-o",
            "json",
        ],
        text_output=True,
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )
    try:
        pods = json.loads(str(pods_result.stdout))["items"]
        pod = next(
            item["metadata"]["name"]
            for item in pods
            if isinstance(item, dict)
            and isinstance(item.get("metadata"), dict)
            and isinstance(item["metadata"].get("name"), str)
        )
    except (KeyError, TypeError, json.JSONDecodeError, StopIteration) as exc:
        raise StagingError("etsllm Deployment 没有可用的运行中 Pod") from exc
    if not isinstance(pod, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", pod):
        raise StagingError("etsllm Pod 名称格式无效")
    return pod


def _remote_sha256_ssh(device: Device, target: str, remote_path: str) -> str:
    command = [
        "ssh",
        "-p",
        str(device.ssh_port),
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        target,
        f"sha256sum -- '{remote_path}'",
    ]
    result = _run(command, text_output=True, timeout=STAGING_COMMAND_TIMEOUT_S)
    return _parse_sha256(result)


def _remote_sha256_kubectl(kubectl: str, remote_path: str) -> str:
    result = _run(
        [kubectl, "exec", "deploy/etsllm", "--", "sha256sum", "--", remote_path],
        text_output=True,
        timeout=STAGING_COMMAND_TIMEOUT_S,
    )
    return _parse_sha256(result)


def _parse_sha256(result: subprocess.CompletedProcess[str | bytes]) -> str:
    """读取 sha256sum 的首字段并验证其格式。"""
    output = result.stdout
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    digest = output.strip().split(maxsplit=1)[0] if output.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise StagingError("设备未返回有效 SHA-256")
    return digest


def _ssh_target(device: Device) -> tuple[str, list[str]]:
    """校验 SSH 目标，避免设备字段成为命令选项或远端参数。"""
    user = device.ssh_user or ""
    host = device.host
    if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
        raise StagingError("设备 SSH 用户格式无效")
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        raise StagingError("设备 SSH 地址格式无效")
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{user}@{host_part}", ["-P", str(device.ssh_port)]


def _sftp_quote(value: str) -> str:
    """用 SFTP batch 的双引号格式转义路径。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _file_sha256(path: Path) -> str:
    """流式计算本地文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    input_bytes: bytes | None = None,
    text_output: bool = False,
    timeout: int,
) -> subprocess.CompletedProcess[str | bytes]:
    """执行外部命令时使用参数数组并限制运行时间。"""
    result = subprocess.run(  # noqa: S603 - 参数逐项传递，不经过 shell。
        command,
        input=input_text if input_text is not None else input_bytes,
        text=input_text is not None or text_output,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        detail = stderr.strip()[:500] if stderr else "命令返回非零状态"
        raise StagingError(detail)
    return result
