# 205 回环 ASR 的 SSH 本地转发验证（2026-11-10）

## 背景

205/600 的 ASR 只监听设备 `127.0.0.1:8701`。旧方案通过修改设备 `SSFConfig.yaml`
把 8701 暴露到 `0.0.0.0`；当前项目**不再修改目标设备文件**，改为平台侧 SSH 本地转发。

## 实现

`backend/app/adapters/ssh_tunnel.py`：

- 仅 `kind=etsstt` 且配置 `ssh_user` 时启用；
- 先探测 `device.host:device.port` 是否直连可达；可达则直连；
- 不可达时启动 `ssh -N -L 127.0.0.1:<临时端口>:127.0.0.1:<ASR端口>`；
- 转发只绑定平台回环，不改设备绑定、不开放局域网；
- SSH 使用平台用户的 key/agent 与 known_hosts；可用 `ASR_DEVICE_SSH_KNOWN_HOSTS` 指定已核验文件；
- 隧道进程退出后下次调用自动重建，应用退出时清理。

`probe_lite`、`probe_identity`、Run 转写、声纹提取/自检统一通过 `device_endpoint()` 获取 ASR 地址；
SSH 版本命令与 `/v1/system/info` 仍使用设备真实地址。

## 真机验证

205 `192.168.65.181:8701` 直连不可达，SSH 22 可达。平台自动建立转发后：

```text
endpoint 127.0.0.1 45261
{
  "ok": true,
  "identity": {
    "platform": "205",
    "kind": "etsstt",
    "bigversion": "3.2.671",
    "etsllm_version": "1.4.52",
    "accel": {"type": "rknn"},
    "model_key": "etsstt",
    "model_version": "1.0.2"
  },
  "sn": "E110020049125A000162",
  "soc": "rk3576",
  "warnings": [],
  "request_error": null
}
```

`/v1/models` 通过转发成功返回，identity 无 warning；隧道在验证后关闭。

### 0162 “检测设备” 的 `/v1/system/info` 404 修复

首次合并检测后，`full` 路径把 `asr_host`（隧道地址 `127.0.0.1`）误传给了
`/v1/system/info`，请求变成 `http://127.0.0.1:8080/v1/system/info` 并返回 404。

修复：`_run_full_probe` 显式传 `system_info_host=device.host`；ASR 的
`/v1/models` 与 WebSocket 仍走隧道，系统信息走设备真实地址 `192.168.65.181:8080`。
新增回归测试 `test_probe_identity_uses_device_host_for_system_info_when_asr_is_tunneled`。

验证系统信息地址：

```text
$ curl -sS -o /dev/null -w '%{http_code}' http://192.168.65.181:8080/v1/system/info
200
$ timeout 3 bash -c '</dev/tcp/192.168.65.181/8701'
（连接失败，8701 直连关闭，符合预期）
```

修复后对开发库 0162 执行一次 `mode=auto`（实际 `full`）：

```json
{
  "probe_status": "ok",
  "mode": "full",
  "duration_ms": 12531,
  "error": null,
  "deployment_uid": "17b78ca25b695d8a",
  "bigversion": "3.2.671",
  "model_version": "1.0.2",
  "warnings": [],
  "observation_error": null,
  "observed": {
    "partials": true,
    "segments": true,
    "usage_field": true,
    "global_spk_list": true,
    "punctuation_output": true,
    "finalizes_in_stream": true,
    "speakers_in_sentence": true
  }
}
```

设备状态由 `degraded` 变为 `online`，`last_error` 为空。

快速探测（`mode=lite`）同样修复为走 `device_endpoint`，0162 实测：

```json
{"status": "ok", "mode": "lite", "duration_ms": 287, "error": null, "model_key": "etsstt"}
```

## 同时移除旧方案

- `OpsName` 只保留 `restart`；`prepare` 接口、脚本、SSFConfig 备份/回滚与前端按钮已删除；
- 历史 `prepare` 记录仍可在运维历史中只读显示为“准备设备（历史）”；
- 文档 `M2 §5.3.2`、`asr-eval-platform-design` 更新为 SSH 转发方案。

## 门禁

```text
make test          381 passed
make lint          All checks passed
make openapi-check 契约与基线一致
make e2e           10 passed, 1 skipped
```