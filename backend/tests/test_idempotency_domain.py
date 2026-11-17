"""幂等请求摘要与参数边界测试。"""

from app.domain.idempotency import request_hash


def test_idempotency_request_hash_is_order_independent() -> None:
    """请求对象键顺序不同仍得到相同规范化摘要。"""
    first = request_hash({"name": "回归", "targets": [{"id": "d1", "subset": "near"}]})
    second = request_hash({"targets": [{"subset": "near", "id": "d1"}], "name": "回归"})

    assert first == second
    assert first != request_hash({"name": "另一个请求", "targets": [{"id": "d1"}]})
