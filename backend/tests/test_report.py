"""报告构建的单元测试（M6 §5.7/§5.8、M7 D-M7-12）。"""

import io
import zipfile
from xml.etree import ElementTree

from app.reports.builder import (
    TEMPLATE_VERSION,
    payload_hash,
    render_html,
    render_xlsx,
)


def _payload() -> dict:
    return {
        "template_version": TEMPLATE_VERSION,
        "level": "summary",
        "generated_at": "2026-11-05T12:00:00+00:00",
        "evaluation": {
            "id": "0f9c2f7e-1111-2222-3333-444455556666",
            "status": "completed",
            "profile_hash": "abc123",
            "scorer_version": "cer-2",
            "coverage": {"scored": 1000, "planned": 1000},
            "metrics": {"cer": 0.0175},
        },
        "run": {"id": "run-1", "name": "<script>alert(1)</script>", "status": "completed"},
        "benchmark": {"name": "v1-default", "version_hash": "deadbeef", "dataset_version": "v1"},
        "metrics": [
            {
                "metric": "cer",
                "subset": "all",
                "value": 0.0175,
                "n": 1000,
                "ci_low": 0.0149,
                "ci_high": 0.0204,
                "status": "ok",
                "reason": None,
            },
            {
                "metric": "der",
                "subset": "all",
                "value": None,
                "n": 0,
                "ci_low": None,
                "ci_high": None,
                "status": "na",
                "reason": "no_diarization_output",
            },
        ],
        "buckets": [
            {
                "bucket": {"duration_bucket": "lt_5s"},
                "metric": "cer",
                "value": 0.018,
                "n": 600,
                "status": "ok",
                "reason": None,
            }
        ],
        "cases": {"ok": 1000, "failed": 0},
    }


def test_html_escapes_user_content_and_is_self_contained() -> None:
    html = render_html(_payload())
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # 自包含：内联 style，无外链 script
    assert "<style>" in html
    assert "<script" not in html
    # na 带原因（而不是显示 0）
    assert "无识别分段" in html


def test_html_contains_metrics_and_buckets() -> None:
    html = render_html(_payload())
    assert "1.75%" in html
    assert "1.49%" in html and "2.04%" in html
    assert "lt_5s" in html
    assert "n=1000" in html or ">1000<" in html


def test_numeric_cer_is_labeled_and_formatted_as_percentage() -> None:
    payload = _payload()
    payload["metrics"].append(
        {
            "metric": "numeric_cer",
            "subset": "all",
            "value": 0.125,
            "n": 8,
            "ci_low": 0.1,
            "ci_high": 0.15,
            "status": "ok",
            "reason": None,
        }
    )

    html = render_html(payload)
    assert "数字相关 CER（近似）" in html
    assert "12.50%" in html
    assert "10.00% – 15.00%" in html

    with zipfile.ZipFile(io.BytesIO(render_xlsx(payload))) as archive:
        summary = archive.read("xl/worksheets/sheet1.xml").decode()
        styles = archive.read("xl/styles.xml").decode()
    assert "数字相关 CER（近似）" in summary
    # XLSX 保留 0–1 比例原值供分析，单元格格式负责显示为百分比。
    assert '<c r="E17" s="1"><v>0.125</v></c>' in summary
    assert '<c r="G17" s="1"><v>0.1</v></c>' in summary
    assert 'formatCode="0.00%"' in styles


def test_composite_numeric_cer_uses_same_label_and_percentage_format() -> None:
    source = _payload()
    evaluation_id = source["evaluation"]["id"]
    payload = {
        "kind": "matrix",
        "generated_at": source["generated_at"],
        "template_version": TEMPLATE_VERSION,
        "level": "summary",
        "query": {},
        "evaluations": [source],
        "comparison": {
            "evaluations": [
                {
                    "id": evaluation_id,
                    "run_name": "run-1",
                    "dataset_version_id": "dataset-1",
                    "profile_hash": "abc123",
                    "scorer_version": "cer-4",
                    "benchmark_version_hash": "deadbeef",
                }
            ],
            "rows": [
                {
                    "target": "device-1",
                    "subset": "all",
                    "metric": "numeric_cer",
                    "scope": "micro",
                    "bucket": {},
                    "values": [
                        {
                            "value": 0.125,
                            "n": 8,
                            "ci_low": 0.1,
                            "ci_high": 0.15,
                            "status": "ok",
                            "reason": None,
                        }
                    ],
                }
            ],
            "warnings": [],
        },
        "significance": None,
    }

    html = render_html(payload)
    assert "数字相关 CER（近似）" in html
    assert "12.50%" in html

    with zipfile.ZipFile(io.BytesIO(render_xlsx(payload))) as archive:
        summary = archive.read("xl/worksheets/sheet1.xml").decode()
    assert "数字相关 CER（近似）" in summary
    assert '<c r="F10" s="1"><v>0.125</v></c>' in summary


def test_xlsx_is_valid_zip_with_three_sheets() -> None:
    data = render_xlsx(_payload())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert "[Content_Types].xml" in names
        assert "xl/workbook.xml" in names
        assert {
            "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml",
            "xl/worksheets/sheet3.xml",
            "xl/styles.xml",
        } <= names
        for name in names:
            if name.endswith((".xml", ".rels")):
                # 测试仅解析本模块刚生成的 XLSX XML，不读取外部 XML。
                ElementTree.fromstring(archive.read(name))  # noqa: S314
        workbook = archive.read("xl/workbook.xml").decode()
        assert 'name="Summary"' in workbook
        assert 'name="Buckets"' in workbook
        assert 'name="Cases"' in workbook
        summary = archive.read("xl/worksheets/sheet1.xml").decode()
        # XML 转义：脚本名不会以原样标签出现
        assert "<script>" not in summary
        assert "&lt;script&gt;" in summary


def test_payload_hash_ignores_generated_at() -> None:
    first = _payload()
    second = _payload()
    second["generated_at"] = "2026-11-05T13:00:00+00:00"
    assert payload_hash(first) == payload_hash(second)
    second["metrics"][0]["value"] = 0.2
    assert payload_hash(first) != payload_hash(second)
