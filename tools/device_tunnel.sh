#!/usr/bin/env bash
# 为只监听设备回环地址的 ASR 服务建立本机 SSH 端口转发（M2/M4）。
#
# 205 的 etsstt 只在设备 127.0.0.1:8701 监听；平台侧不要改设备绑定地址，
# 用 SSH 本地转发即可，不暴露到局域网。
#
# 用法：
#   tools/device_tunnel.sh [SSH_TARGET] [LOCAL_PORT] [REMOTE_HOST] [REMOTE_PORT]
#   tools/device_tunnel.sh etsme@192.168.65.181 18705 127.0.0.1 8701
# 注意：18701 被 `make e2e` 的假设备占用，日常真机矩阵默认用 18705。
#
# 已核验设备指纹时可提供一次性 known_hosts：
#   ASR_TUNNEL_KNOWN_HOSTS=/tmp/205_known_hosts tools/device_tunnel.sh ...
set -Eeuo pipefail

SSH_TARGET="${1:-etsme@192.168.65.181}"
LOCAL_PORT="${2:-18705}"
REMOTE_HOST="${3:-127.0.0.1}"
REMOTE_PORT="${4:-8701}"

if ! command -v ssh >/dev/null 2>&1; then
  echo "找不到 ssh 客户端" >&2
  exit 2
fi
if [[ ! "$LOCAL_PORT" =~ ^[0-9]+$ || ! "$REMOTE_PORT" =~ ^[0-9]+$ ]]; then
  echo "端口必须是数字" >&2
  exit 2
fi

SSH_ARGS=(
  -N
  -o ConnectTimeout=10
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -L "127.0.0.1:${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}"
)

if [[ -n "${ASR_TUNNEL_KNOWN_HOSTS:-}" ]]; then
  if [[ ! -r "$ASR_TUNNEL_KNOWN_HOSTS" ]]; then
    echo "ASR_TUNNEL_KNOWN_HOSTS 不可读：$ASR_TUNNEL_KNOWN_HOSTS" >&2
    exit 2
  fi
  SSH_ARGS+=(
    -o "UserKnownHostsFile=${ASR_TUNNEL_KNOWN_HOSTS}"
    -o StrictHostKeyChecking=yes
  )
fi

echo "本地 127.0.0.1:${LOCAL_PORT} -> ${SSH_TARGET} ${REMOTE_HOST}:${REMOTE_PORT}" >&2
exec ssh "${SSH_ARGS[@]}" "$SSH_TARGET"