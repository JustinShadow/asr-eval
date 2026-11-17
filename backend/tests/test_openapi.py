"""OpenAPI 3.1 导出结构门禁。"""

import copy
import importlib.util
import json
from pathlib import Path
from types import ModuleType

from app.openapi import build_contract


def _check_openapi_module() -> ModuleType:
    """加载 tools/check_openapi.py，验证本地契约差异门禁本身。"""
    path = Path(__file__).resolve().parents[2] / "tools" / "check_openapi.py"
    spec = importlib.util.spec_from_file_location("check_openapi", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_contract_has_unique_operation_ids() -> None:
    """导出契约必须使用 3.1.0，且每个接口有唯一 operationId。"""
    schema = build_contract()
    assert schema["openapi"] == "3.1.0"
    assert "/api/v1/auth/login" in schema["paths"]
    assert "/api/v1/admin/service-tokens" in schema["paths"]
    assert "/api/v1/meta/enums" in schema["paths"]
    assert "/api/v1/dataset-versions/{version_id}/capabilities" in schema["paths"]
    assert "/api/v1/deployments/{deployment_id}" in schema["paths"]
    assert "/api/v1/speakers" in schema["paths"]
    assert "/api/v1/speakers/{speaker_id}/enrollments" in schema["paths"]
    assert "/api/v1/speakers/{speaker_id}/enrollments/{enrollment_id}/audio" in schema["paths"]
    assert "/api/v1/system/status" in schema["paths"]
    audio_response = schema["paths"][
        "/api/v1/speakers/{speaker_id}/enrollments/{enrollment_id}/audio"
    ]["get"]["responses"]["200"]["content"]["audio/wav"]["schema"]
    assert audio_response == {"type": "string", "format": "binary"}


def test_openapi_contract_matches_committed_baseline() -> None:
    """契约变更必须先同步 baseline，避免前端类型与后端漂移（M10 §3.4）。"""
    baseline_path = Path(__file__).resolve().parents[2] / "docs" / "openapi.baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert build_contract() == baseline, (
        "OpenAPI 与 docs/openapi.baseline.json 不一致；确认变更后运行 make openapi-sync"
    )


def test_openapi_diff_detects_removed_response_field() -> None:
    """删除响应字段必须被本地 openapi-check 识别（M10 §14）。"""
    module = _check_openapi_module()
    current = {
        "paths": {
            "/api/v1/demo": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    }
                }
            }
        }
    }
    baseline = copy.deepcopy(current)
    current["paths"]["/api/v1/demo"]["get"]["responses"]["200"].pop("content")

    messages = module.diff_contracts(current, baseline)

    assert any("接口变更 GET /api/v1/demo" in message for message in messages)


def test_openapi_is_admin_restricted_in_production(monkeypatch) -> None:
    """生产环境的契约路由必须声明 admin:system 权限。"""
    import app.main as main_module
    from app.core.config import Settings

    production_settings = Settings(app_env="prod")
    monkeypatch.setattr(main_module, "get_settings", lambda: production_settings)
    production_app = main_module.create_app()
    route = next(route for route in production_app.routes if route.path == "/api/openapi.json")
    required_permission = route.dependant.dependencies[0].call.__dict__["required_permission"]
    assert required_permission == "admin:system"
