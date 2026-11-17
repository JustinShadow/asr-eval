# MagicData 方言标签统一（2026-11-11）

## 原行为

MagicData 导入器直接写：

```python
buckets = {"dialect": root.name, "language": _language_of(root.name)}
```

因此分桶值是完整目录名：

| 数据集 | dialect | language |
|---|---|---|
| MagicData-沪语 | `MagicData-沪语（吴语）` | `wuu` |
| MagicData-湘语 | `MagicData-湘语` | `xiang` |
| MagicData-赣语 | `MagicData-赣语` | `gan` |

## 统一规则

新增 `infer_dialect(name)`：

```python
沪/吴 → ("wuu", "沪语")
湘     → ("xiang", "湘语")
赣     → ("gan", "赣语")
其它   → ("zh", name)
```

导入器写入统一的 `language/dialect`；未知目录保留原名，避免丢来源。

## 回填

```text
MagicData-沪语 v1: 10 条 → language=wuu, dialect=沪语
MagicData-湘语 v1: 2 条 → language=xiang, dialect=湘语
MagicData-湘语 v2: 12 条 → language=xiang, dialect=湘语
MagicData-赣语 v1: 12 条 → language=gan, dialect=赣语
共更新 36 条 item
```

同时更新：

- `item.lang`；
- `item.buckets.language/dialect`；
- `dataset_version.capabilities.langs` 与 `bucket_counts`；
- 各 subset 能力快照。

## 影响

- 分桶「方言」从 `MagicData-沪语（吴语）` 变为 `沪语`，与 APY 的 `闽南语`、KeSpeech 的 `Beijing` 风格一致；
- `language` 保持 `wuu/xiang/gan`；
- 不改变 `version_hash`，历史 Run 仍可读；
- 未改任何音频文件。

## 复现

```bash
cd backend
uv run python ../tools/backfill_magicdata_dialects.py          # dry-run
uv run python ../tools/backfill_magicdata_dialects.py --apply  # 写库
```