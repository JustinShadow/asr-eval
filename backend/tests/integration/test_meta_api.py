"""元数据枚举接口契约与访问控制集成测试。"""

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_enum_labels_follow_m1_design_and_require_auth(client: TestClient) -> None:
    """枚举映射覆盖 M1 §7.2 标签，匿名请求不得读取。"""
    client.cookies.clear()
    anonymous = client.get("/api/v1/meta/enums")
    assert anonymous.status_code == 401

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    response = client.get(
        "/api/v1/meta/enums",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    enums = response.json()
    assert enums["languages"] == {
        "zh": "普通话",
        "yue": "粤语",
        "wuu": "吴语（沪语）",
        "xiang": "湘语",
        "gan": "赣语",
        "sichuan": "四川话",
        "minnan": "闽南语",
        "hak": "客家话",
        "en": "英语",
    }
    assert enums["dialects"]["Jiang-Huai"] == "江淮"
    assert enums["genders"] == {"M": "男", "F": "女", "unknown": "未知"}
    assert enums["age_groups"] == {
        "A": "14 岁以下",
        "B": "14~25 岁",
        "C": "26~40 岁",
        "D": "41 岁以上",
    }
    assert enums["splits"] == {
        "train": "训练集",
        "dev": "开发集",
        "test": "测试集",
        "all": "全部",
    }
    assert enums["metrics"]["cer"] == "字错误率（CER）"
    assert enums["metrics"]["speaker_eer"] == "说话人等错误率（EER）"
    assert enums["reasons"]["no_sentence_punctuation"] == "参考缺少句末标点"
    assert enums["reasons"]["low_coverage"] == "有效样本覆盖率不足"
