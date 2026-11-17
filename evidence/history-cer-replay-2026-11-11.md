# 历史 CER 测试集复现（2026-11-11）

## 目标

复现两段历史 CER 测试集：

1. KeSpeech：按方言子目录，每个目录前 250 条；
2. AISHELL-1：说话人 `S0765`、`S0766` 的全部条目。

要求样本与历史一致，不能使用随机抽样近似替代。

## 实现

新增采样能力（`backend/app/domain/benchmarks.py::sampled_item_ids`）：

- `sampling.order = "item_key"`：组内按 `item_key` 升序取前 N 条；
  默认仍为 `seed_hash`，不影响既有基准。
- `sampling.filter.speakers`：按 item.speakers 交集过滤；
  另支持 `filter.item_key_prefix`。
- 两者都进入 `Benchmark.sampling` 与 `version_hash`。
- 移除 `min_per_stratum`（结果与 `per_stratum` 上限等价，属于冗余配置）。

## 复现配置

### KeSpeech

```json
{
  "mode": "stratified",
  "dim": "dialect",
  "per_stratum": 250,
  "order": "item_key"
}
```

- 数据集：KeSpeech v2（19,723 条）；
- 方言目录：Beijing / Ji-Lu / Jiang-Huai / Jiao-Liao / Lan-Yin /
  Mandarin / Northeastern / Southwestern / Zhongyuan；
- 选中：2,250 条，音频约 3.60 小时；
- 已核对每个目录排序后的首尾 item_key 与源目录 `sorted(*.txt)[:250]` 一致。

### AISHELL-1

```json
{
  "mode": "all",
  "filter": { "speakers": ["S0765", "S0766"] }
}
```

- 数据集：AISHELL-1 v1；
- 选中：701 条（S0765 361 + S0766 340），音频约 0.87 小时。

## 已创建的基准（开发库）

| 名称 | ID | hash | 条数 |
|---|---|---|---:|
| 历史CER-KeSpeech-每方言前250 | `10fd5d61-faf5-455c-a747-acff6d699f32` | `b0d35093a43d84c6` | 2250 |
| 历史CER-AISHELL-S0765S0766 | `fa9a0095-a1a8-49d8-b233-12352469a442` | `010ab6b08c2b3e8d` | 701 |

两者都引用 `default-zh` v1 口径。

## 使用方式

1. 打开「基准与口径」，确认上述两个基准的预览条数为 2250 / 701；
2. 新建评测时多选这两个 Benchmark，选择设备 → 自动创建 RunGroup；
3. 组页查看每个数据集的 CER 与按 ref_len 加权的组级 CER；
4. 若历史结果是分开统计的，直接看两条 Run 的 CER；若历史是合并的，
   用组级 CER 对比（平台按 Σ(S+D+I)/Σref_len 计算）。

## 复现脚本

```bash
cd backend
uv run python ../tools/replay_history_benchmarks.py --dry-run   # 只统计
uv run python ../tools/replay_history_benchmarks.py --create    # 幂等创建基准
```

## 注意事项

- **排序假设**：`item_key` 是导入时的文件名 stem；KeSpeech 导入器按文件名排序。
  如果历史脚本用的是 `os.listdir()` 而非排序，选出的 250 条可能不同；
  需要拿历史结果里的一个边界 item_key 对照。
- **评分口径**：样本一致不等于 CER 可比。平台 `default-zh` 会去标点、去空格、
  小写化；若历史 CER 计入标点，需要新建自定义口径并关闭 `text.strip_punct`
  （必要时关闭 `strip_space`），再复制基准。
- 历史 CER 的聚合方式也需确认：micro（总编辑距离/总参考长度）还是 macro
  （逐句平均）。平台两条 Run 与组级汇总都保留对应读数。