"""字符级对齐，供 Badcase 工作台展示。

返回 op 列表：eq / sub / del / ins；sub 同时带 ref 与 hyp 字符。
性能：用 rapidfuzz 的 C 实现 opcodes，而不是纯 Python DP——
30 分钟会议转写上万字，O(n*m) 的 Python 表会直接把请求拖死。
超长文本按 M8 §9.7.1 截断（长音频应看分段/泳道，而不是整篇 diff）。
"""

from rapidfuzz.distance import Levenshtein

# 整篇 diff 上限；超出时前端应使用分段视图（§9.7.1）
MAX_DIFF_CHARS = 20_000


def char_diff(ref: str, hyp: str) -> list[dict[str, str]]:
    """字符对齐；相邻 del+ins 合并为 sub（与前端渲染契约一致）。"""
    if len(ref) > MAX_DIFF_CHARS:
        ref = ref[:MAX_DIFF_CHARS]
    if len(hyp) > MAX_DIFF_CHARS:
        hyp = hyp[:MAX_DIFF_CHARS]

    ops: list[dict[str, str]] = []
    for code in Levenshtein.opcodes(ref, hyp):
        tag = str(code.tag)
        i1, i2 = int(code.src_start), int(code.src_end)
        j1, j2 = int(code.dest_start), int(code.dest_end)
        if tag == "equal":
            for offset in range(i2 - i1):
                ops.append({"type": "eq", "ref": ref[i1 + offset], "hyp": hyp[j1 + offset]})
        elif tag == "replace":
            pairs = min(i2 - i1, j2 - j1)
            for offset in range(pairs):
                ops.append(
                    {
                        "type": "sub",
                        "ref": ref[i1 + offset],
                        "hyp": hyp[j1 + offset],
                    }
                )
            for offset in range(i1 + pairs, i2):
                ops.append({"type": "del", "ref": ref[offset]})
            for offset in range(j1 + pairs, j2):
                ops.append({"type": "ins", "hyp": hyp[offset]})
        elif tag == "delete":
            for offset in range(i1, i2):
                ops.append({"type": "del", "ref": ref[offset]})
        else:  # insert
            for offset in range(j1, j2):
                ops.append({"type": "ins", "hyp": hyp[offset]})

    # 相邻 del+ins 合并为 sub（保持原契约：替换成对显示）
    merged: list[dict[str, str]] = []
    index = 0
    while index < len(ops):
        if ops[index]["type"] == "del":
            dels: list[dict[str, str]] = []
            while index < len(ops) and ops[index]["type"] == "del":
                dels.append(ops[index])
                index += 1
            inss: list[dict[str, str]] = []
            while index < len(ops) and ops[index]["type"] == "ins":
                inss.append(ops[index])
                index += 1
            pairs = min(len(dels), len(inss))
            for pair in range(pairs):
                merged.append(
                    {
                        "type": "sub",
                        "ref": dels[pair].get("ref", ""),
                        "hyp": inss[pair].get("hyp", ""),
                    }
                )
            merged.extend(dels[pairs:])
            merged.extend(inss[pairs:])
        else:
            merged.append(ops[index])
            index += 1
    return merged
