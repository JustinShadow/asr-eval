"""OpenAPI 契约导出与结构门禁（M7 §8）。"""

import argparse
import json
import sys
from collections import Counter
from typing import Any, TextIO

from app.main import app


def build_contract() -> dict[str, Any]:
    """生成并校验 OpenAPI 3.1 契约，拒绝缺失或重复 operationId。"""
    schema = app.openapi()
    if schema.get("openapi") != "3.1.0":
        raise ValueError("OpenAPI 版本必须为 3.1.0")

    operations = [
        operation
        for path_item in schema.get("paths", {}).values()
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
    ]
    operation_ids = [operation.get("operationId") for operation in operations]
    if any(not operation_id for operation_id in operation_ids):
        raise ValueError("每个 API 操作都必须声明 operationId")
    duplicates = [
        operation_id for operation_id, count in Counter(operation_ids).items() if count > 1
    ]
    if duplicates:
        raise ValueError(f"operationId 重复：{', '.join(duplicates)}")
    return schema


def write_contract(stream: TextIO = sys.stdout) -> None:
    """以稳定缩进输出契约，便于重定向为 docs/openapi.json。"""
    json.dump(build_contract(), stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def main() -> None:
    """执行 `python -m app.openapi export`。"""
    parser = argparse.ArgumentParser(description="导出 ASR 评测平台 OpenAPI 契约")
    parser.add_argument("command", choices=["export"])
    parser.parse_args()
    write_contract()


if __name__ == "__main__":
    main()
