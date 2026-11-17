"""设备运维适配器的本地安全校验。"""

from types import SimpleNamespace

import pytest

from app.domain.device_ops import DeviceOpsError, _restart_script, _ssh_target


def test_restart_script_is_valid_python_and_has_no_config_writes() -> None:
    """重启脚本只重启服务，不得再修改 SSFConfig 等设备文件。"""
    script = _restart_script()
    compile(script, "remote_restart.py", "exec")
    assert "SSFConfig" not in script
    assert "shutil.copy2" not in script
    assert "restart" in script


def test_ssh_target_rejects_option_and_shell_injection() -> None:
    assert _ssh_target(SimpleNamespace(ssh_user="tester", host="192.0.2.4")) == "tester@192.0.2.4"
    with pytest.raises(DeviceOpsError):
        _ssh_target(SimpleNamespace(ssh_user="tester", host="-oProxyCommand=sh"))
    with pytest.raises(DeviceOpsError):
        _ssh_target(SimpleNamespace(ssh_user="tester;id", host="192.0.2.4"))
