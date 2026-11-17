#!/usr/bin/env python3
"""比对 OpenAPI 当前导出与基线，输出可定位的接口差异（M10 §3.4）。

用法：
    python tools/check_openapi.py [当前契约] [基线契约]

默认读取 docs/openapi.json 与 docs/openapi.baseline.json。有差异时退出码 1，
用于本地门禁；同步基线请显式运行 `make openapi-sync`，避免契约变更被静默覆盖。
"""

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _operations(contract: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """把契约展开为 (METHOD, path) -> operation。"""
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for path, item in contract.get("paths", {}).items():
        if not isinstance(item, Mapping):
            continue
        for method, operation in item.items():
            if method.lower() in METHODS and isinstance(operation, Mapping):
                result[(method.upper(), path)] = operation
    return result


def _schemas(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    """返回 components.schemas，缺失时按空集合处理。"""
    components = contract.get("components", {})
    schemas = components.get("schemas", {}) if isinstance(components, Mapping) else {}
    return schemas if isinstance(schemas, Mapping) else {}


def _changed_keys(
    before: Mapping[str, Any], after: Mapping[str, Any], *, limit: int = 8
) -> list[str]:
    """列出对象键的变化，便于人工判断是加字段还是删字段。"""
    keys = sorted(set(before) | set(after))
    changed = [key for key in keys if before.get(key) != after.get(key)]
    return changed[:limit]


def diff_contracts(
    current: Mapping[str, Any], baseline: Mapping[str, Any]
) -> list[str]:
    """返回人类可读的差异行；空列表表示两份契约一致。"""
    messages: list[str] = []
    current_ops = _operations(current)
    baseline_ops = _operations(baseline)

    for method, path in sorted(set(current_ops) - set(baseline_ops)):
        messages.append(f"+ 新增接口 {method} {path}")
    for method, path in sorted(set(baseline_ops) - set(current_ops)):
        messages.append(f"- 删除接口 {method} {path}")
    for key in sorted(set(current_ops) & set(baseline_ops)):
        if current_ops[key] != baseline_ops[key]:
            method, path = key
            changed = _changed_keys(baseline_ops[key], current_ops[key])
            messages.append(f"~ 接口变更 {method} {path}：{', '.join(changed)}")

    current_schemas = _schemas(current)
    baseline_schemas = _schemas(baseline)
    for name in sorted(set(current_schemas) - set(baseline_schemas)):
        messages.append(f"+ 新增 schema {name}")
    for name in sorted(set(baseline_schemas) - set(current_schemas)):
        messages.append(f"- 删除 schema {name}")
    for name in sorted(set(current_schemas) & set(baseline_schemas)):
        if current_schemas[name] != baseline_schemas[name]:
            changed = _changed_keys(baseline_schemas[name], current_schemas[name])
            messages.append(f"~ schema 变更 {name}：{', '.join(changed)}")
    return messages


def main() -> int:
    """读取两份契约并打印差异。"""
    parser = argparse.ArgumentParser(description="检查 OpenAPI 契约与基线是否一致")
    parser.add_argument("current", nargs="?", default="docs/openapi.json")
    parser.add_argument("baseline", nargs="?", default="docs/openapi.baseline.json")
    args = parser.parse_args()

    current_path = Path(args.current)
    baseline_path = Path(args.baseline)
    for path in (current_path, baseline_path):
        if not path.is_file():
            print(f"契约文件不存在：{path}")
            return 2

    current = json.loads(current_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    messages = diff_contracts(current, baseline)
    if not messages:
        print(f"OpenAPI 契约与基线一致：{baseline_path}")
        return 0

    print(f"OpenAPI 契约与基线不一致（当前 {current_path}，基线 {baseline_path}）：")
    for message in messages:
        print(f"  {message}")
    print("\n确认变更后运行 `make openapi-sync` 同步基线。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())