"""M2 声纹远端暂存策略的隔离单元测试。"""

import hashlib
import subprocess
from pathlib import Path

import pytest

from app.domain.models import Device
from app.domain.remote_staging import StagingError, cleanup_audio, stage_audio


def _device(kind: str = "etsstt", *, ssh_user: str | None = "tester") -> Device:
    """创建不落库的设备测试对象。"""
    return Device(
        name="自检设备",
        platform="205" if kind == "etsstt" else "502",
        kind=kind,
        host="192.0.2.10",
        port=8701,
        ssh_user=ssh_user,
        ssh_port=2222,
        tags={},
    )


def test_etsstt_staging_uses_sftp_and_checks_remote_sha256(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """普通设备用 SFTP 写入 /data/voiceprint 并校验远端摘要。"""
    local = tmp_path / "probe.wav"
    local.write_bytes("合成测试音频".encode())
    expected = hashlib.sha256(local.read_bytes()).hexdigest()
    commands: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append((command, kwargs))
        output = (
            f"{expected}  /data/voiceprint/{expected[:16]}.wav\n" if command[0] == "ssh" else ""
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("app.domain.remote_staging.shutil.which", lambda _name: "/usr/bin/sftp")
    monkeypatch.setattr("app.domain.remote_staging.subprocess.run", fake_run)

    staged = stage_audio(_device(), local)

    assert staged.method == "sftp"
    assert staged.remote_path == f"/data/voiceprint/{expected[:16]}.wav"
    assert staged.sha256 == expected
    sftp_command, sftp_kwargs = commands[0]
    assert sftp_command[:6] == ["/usr/bin/sftp", "-q", "-b", "-", "-P", "2222"]
    assert "-mkdir /data/voiceprint" in str(sftp_kwargs["input"])
    assert str(local) in str(sftp_kwargs["input"])
    assert commands[1][0][0] == "ssh"


def test_gateway_staging_uses_exec_cat_and_verifies_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无 SSH 用户的网关继续使用服务端 kubectl 上下文。"""
    local = tmp_path / "probe.wav"
    local.write_bytes(b"gateway probe")
    expected = hashlib.sha256(local.read_bytes()).hexdigest()
    commands: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes | str]:
        commands.append((command, kwargs))
        output: bytes | str = (
            f"{expected}  /mnt/voiceprint/{expected[:16]}.wav\n" if "sha256sum" in command else b""
        )
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr("app.domain.remote_staging.shutil.which", lambda _name: "/usr/bin/kubectl")
    monkeypatch.setattr("app.domain.remote_staging.subprocess.run", fake_run)

    staged = stage_audio(_device("gateway", ssh_user=None), local)

    assert staged.method == "exec_cat"
    assert staged.container_dir == "/mnt/voiceprint"
    write_command, write_kwargs = commands[0]
    assert write_command[:5] == [
        "/usr/bin/kubectl",
        "exec",
        "-i",
        "deploy/etsllm",
        "--",
    ]
    assert "cat > /mnt/voiceprint/" in write_command[-1]
    assert write_kwargs["input"] == local.read_bytes()
    assert commands[1][0][-3:] == ["sha256sum", "--", staged.remote_path]


def test_gateway_falls_back_to_kubectl_cp_and_cleanup_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """exec-cat 失败后尝试 kubectl cp，清理复用实际暂存方式。"""
    local = tmp_path / "probe.wav"
    local.write_bytes(b"fallback probe")
    expected = hashlib.sha256(local.read_bytes()).hexdigest()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes | str]:
        commands.append(command)
        if command[1] == "exec" and "-i" in command:
            return subprocess.CompletedProcess(command, 1, b"", "exec-cat 不可用".encode())
        if command[1:3] == ["get", "deployment"]:
            return subprocess.CompletedProcess(
                command,
                0,
                '{"spec":{"selector":{"matchLabels":{"app":"etsllm"}}}}',
                "",
            )
        if command[1:3] == ["get", "pods"]:
            return subprocess.CompletedProcess(
                command,
                0,
                '{"items":[{"metadata":{"name":"etsllm-abc123"}}]}',
                "",
            )
        if command[1:3] == ["exec", "etsllm-abc123"]:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        output: bytes | str = (
            f"{expected}  /mnt/voiceprint/{expected[:16]}.wav\n" if "sha256sum" in command else b""
        )
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr("app.domain.remote_staging.shutil.which", lambda _name: "/usr/bin/kubectl")
    monkeypatch.setattr("app.domain.remote_staging.subprocess.run", fake_run)

    device = _device("gateway", ssh_user=None)
    staged = stage_audio(device, local)
    cleanup_audio(device, staged)

    assert staged.method == "kubectl_cp"
    assert commands[3][1:3] == ["exec", "etsllm-abc123"]
    assert commands[4][1:3] == ["cp", str(local)]
    assert commands[4][3] == f"etsllm-abc123:{staged.remote_path}"
    assert commands[-1][-3:] == ["rm", "-f", staged.remote_path]


def test_gateway_ssh_staging_streams_bytes_checks_hash_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """配置 SSH 的网关在宿主 kubectl 上下文暂存并校验后清理音频。"""
    local = tmp_path / "probe.wav"
    local.write_bytes(b"remote gateway probe")
    expected = hashlib.sha256(local.read_bytes()).hexdigest()
    commands: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes | str]:
        commands.append((command, kwargs))
        output: bytes | str = (
            f"{expected}  /mnt/voiceprint/{expected[:16]}.wav\n"
            if "sha256sum" in command[-1]
            else b""
        )
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(
        "app.domain.remote_staging.shutil.which",
        lambda name: "/usr/bin/ssh" if name == "ssh" else None,
    )
    monkeypatch.setattr("app.domain.remote_staging.subprocess.run", fake_run)

    device = _device("gateway")
    staged = stage_audio(device, local)
    cleanup_audio(device, staged)

    assert staged.method == "exec_cat"
    assert staged.container_dir == "/mnt/voiceprint"
    write_command, write_kwargs = commands[0]
    expected_remote_command = (
        "sudo -n kubectl exec -i -n ns-meagt deploy/etsllm -- sh -c "
        f"'mkdir -p /mnt/voiceprint && cat > {staged.remote_path}'"
    )
    assert write_command == [
        "/usr/bin/ssh",
        "-p",
        "2222",
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "tester@192.0.2.10",
        expected_remote_command,
    ]
    assert write_kwargs["input"] == local.read_bytes()
    assert commands[1][0][-1] == (
        f"sudo -n kubectl exec -n ns-meagt deploy/etsllm -- sha256sum -- {staged.remote_path}"
    )
    assert commands[2][0][-1] == (
        f"sudo -n kubectl exec -n ns-meagt deploy/etsllm -- rm -f -- {staged.remote_path}"
    )


def test_etsstt_staging_rejects_shell_metacharacters_in_ssh_user(tmp_path: Path) -> None:
    """设备 SSH 用户不能借 SFTP 目标注入命令行参数。"""
    local = tmp_path / "probe.wav"
    local.write_bytes(b"sample")

    with pytest.raises(StagingError, match="SSH 用户格式无效"):
        stage_audio(_device(ssh_user="tester;false"), local)
