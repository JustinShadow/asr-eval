#!/usr/bin/env python3
"""P0 验收自检：健康、登录、设备、数据集、最近一次 Run 的 CER。

用法：
    uv run python ../tools/p0_check.py --base-url http://127.0.0.1:8080 \
        --username admin --password admin12345
"""

import argparse
import sys
from typing import Any

import httpx


def _fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--min-items", type=int, default=1000)
    parser.add_argument("--run-id", default=None, help="指定 Run；默认取最近 completed")
    parser.add_argument("--min-run-n", type=int, default=1000, help="Run 的评分样本量下限")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        ready = client.get("/readyz")
        if ready.status_code != 200:
            _fail(f"/readyz {ready.status_code}")
        checks = ready.json().get("checks", {})
        if checks.get("database") != "ok":
            _fail(f"database={checks.get('database')}")
        print(f"[OK] readyz {checks}")

        login = client.post(
            "/api/v1/auth/login",
            json={"username": args.username, "password": args.password},
        )
        if login.status_code != 200:
            _fail(f"login {login.status_code}: {login.text}")
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"[OK] login role={login.json()['user']['role']}")

        devices = client.get("/api/v1/devices", headers=headers).json()["items"]
        if not devices:
            _fail("没有登记设备")
        online = [d for d in devices if d["status"] == "online"]
        if not online:
            _fail("没有在线设备")
        print(f"[OK] devices={len(devices)} online={len(online)}")

        datasets = client.get("/api/v1/datasets", headers=headers).json()["items"]
        if not datasets:
            _fail("没有数据集")
        dataset = datasets[0]
        versions = client.get(f"/api/v1/datasets/{dataset['id']}/versions", headers=headers).json()[
            "items"
        ]
        ready_versions = [v for v in versions if v["status"] == "ready"]
        if not ready_versions:
            _fail("没有 ready 的数据集版本")
        version = ready_versions[0]
        if version["item_count"] < args.min_items:
            _fail(f"版本条目 {version['item_count']} < {args.min_items}")
        print(
            f"[OK] dataset={dataset['name']} version={version['version']} "
            f"items={version['item_count']}"
        )

        if args.run_id:
            run_id = args.run_id
        else:
            runs = client.get("/api/v1/runs", headers=headers).json()["items"]
            completed = [r for r in runs if r["status"] == "completed"]
            if not completed:
                _fail("没有 completed 的 Run")
            run_id = completed[0]["id"]
        detail: dict[str, Any] = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        metrics = detail.get("metrics") or []
        cer = next((m for m in metrics if m["metric"] == "cer"), None)
        if cer is None or cer["value"] is None:
            _fail("该 Run 没有 CER")
        if cer["n"] < args.min_run_n:
            _fail(f"Run 样本量 {cer['n']} < {args.min_run_n}")
        print(
            f"[OK] run={detail['run']['name']} status={detail['run']['status']} "
            f"CER={cer['value']:.4%} n={cer['n']}"
        )

    print("\nP0 自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
