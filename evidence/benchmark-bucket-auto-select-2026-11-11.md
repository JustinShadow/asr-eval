# 基准分桶维度自动选择（2026-11-11）

## 问题

新建基准时手动勾选分桶维度，预览里常出现 `unknown`：这些字段在数据集里没有值，
勾选不会产生有效分桶，只是增加人工试错。

## 实现

### 1. 覆盖率接口

```http
GET /api/v1/dataset-versions/{version_id}/bucket-coverage?subsets=all,near
```

返回每个注册维度：

```json
{
  "key": "dialect",
  "kind": "static",
  "known": 19723,
  "total": 19723,
  "coverage": 1.0,
  "distinct": 9
}
```

- `known`/`coverage`：该维度在所选子集里有值的样本数/比例；
- `distinct`：有效取值类别数；
- `coverage=0` 即“数据中无此维度”；
- 同时修正 `lang` 维度：兼容导入器写入的 `buckets.language`。

### 2. 前端自动选择

- 选定版本/子集后自动查询覆盖率；
- 新建基准默认勾选：
  - `duration_bin`；
  - 覆盖率 ≥95%、取值 2–20 的静态维度；
  - 最多 4 个，避免报告被高基数维度淹没；
- `coverage=0` 的维度置灰，未选中时禁止勾选；
- 提供“自动选择”按钮，随时按当前覆盖率重新推荐；
- 切换数据集/版本时清空手动维度选择并重新推荐；
- 每个维度卡片显示 `有值条数/总数 · 取值类别数`。

## 验证

- `make test`：397 passed（新增 bucket coverage 用例）；
- `make lint`：通过；
- `make openapi-check`：契约与基线一致；
- `web npm run build` / `npm run lint`：通过。

## 典型结果

| 数据集 | 自动推荐 |
|---|---|
| AISHELL-1 | `duration_bin`（`lang`/`split` 只有 1 类，`scene` 等缺失） |
| KeSpeech | `dialect` + `duration_bin` |
| AliMeeting | `scene` + `duration_bin`（`speaker` 高基数/覆盖不足，不自动选） |