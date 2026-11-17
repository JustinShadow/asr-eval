# APY 闽南语方言标签修复（2026-11-11）

## 问题

APY-闽南语数据集的 `language` 全部是 `zh`，没有对应的方言类别。

## 原因

`ApyImporter` 从 metadata 的 `ACT` 字段推断语言：

```python
if "cantonese" in act: return "yue"
if "sichuan" in act: return "sichuan"
if "minnan" in act or "min" in act: return "minnan"
return "zh"
```

但 APY 不同包的 `ACT` 含义不一致：

- 粤语包：`ACT Cantonese` → yue；
- 四川包：`ACT sichuan` → sichuan；
- 闽语包：`ACT O1_漳州龙文区,O2_漳州芗城区` —— 是地点，不含 “minnan”，
  因而落回 `zh`。

同时 APY 只写 `buckets.language` 与 `accent`，没有写 `buckets.dialect`，
所以分桶里的“方言”也是 unknown。

## 修复

1. `_language()` 增加 `闽 / 漳州 / 泉州 / 厦门 / hokkien` 识别；
2. 新增 `infer_dialect(path)`：优先从包路径推断（`APY210930001-闽语` →
   `minnan/闽南语`），路径不明确时再回退 ACT；
3. APY 导入同时写入：
   ```json
   {"language": "minnan", "dialect": "闽南语", "accent": "O1_漳州龙文区,..."}
   ```
4. 新增 `tools/backfill_apy_dialects.py`，回填历史 item 与 subset/version 能力快照。

## 回填结果

```text
APY-四川话 v1: 3 条 → language=sichuan, dialect=四川话
APY-粤语 v1: 19 条 → language=yue, dialect=粤语
APY-粤语 v2: 20 条 → language=yue, dialect=粤语
APY-闽南语 v1: 5 条 → language=minnan, dialect=闽南语
共更新 47 条 item
```

回填后：

- `item.lang = minnan`；
- `item.buckets.language = minnan`；
- `item.buckets.dialect = 闽南语`；
- `dataset_version.capabilities.langs = ["minnan"]`；
- subset 能力快照同步。

## 影响

- 分桶「语言」现在会显示 `minnan`，不再落到 `zh`；
- 分桶「方言」可用，值为 `闽南语`；
- 不改变 `version_hash`（hash 只看 content_uid、sampling、profile、bucket_rules、params），
  已有基准/历史 Run 仍可读；
- 已检查 APY 相关基准均为 `mode=all`，回填不会改变样本选择。

## 复现

```bash
cd backend
uv run python ../tools/backfill_apy_dialects.py          # dry-run
uv run python ../tools/backfill_apy_dialects.py --apply  # 写库
```

新导入的 APY 包会直接走修复后的 `infer_dialect`。