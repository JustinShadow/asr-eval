# 报告去重与设备硬件资源采集（2026-11-12）

## 1. 报告指标重复

### 问题

单 subset Run 的报告同时出现 `far` 与 `all`，且同一指标重复多行：

- `metric_value` 同时包含目标级行（`run_target_id=target`，subset=far）
  和全局行（`run_target_id=NULL`，subset=all）；
- 报告主指标表还把 `bucket` 行当普通指标行输出；
- 两者数据完全相同，导致 CER 出现 4 行、标点 3 行。

### 修复

`backend/app/reports/builder.py::build_payload`：

- 主指标表过滤 `bucket={}`，分桶数据仍单独走「分桶指标」；
- 若 Run 只有一个目标 subset，删除重复的全局 `all` 行，只保留目标行
  （例如 `0162 / far`）；
- 多 subset Run 仍保留 `all` + 各 subset 的聚合；
- HTML 主表增加「范围」列（micro/macro），避免 `both` 口径出现同名行。

### 报告补充设备/版本信息

报告头部现在包含：

```text
数据集 AliMeeting · 版本 v1 · 基准 ... · 子集 far · 基准哈希 ...
设备 0162（205/etsstt，subset far，etsstt，17b78ca25b695d8a）
```

XLSX Summary 同步增加数据集、版本、基准、设备行。

## 2. 设备硬件资源采集

### 采集方式

新增 `backend/app/domain/device_resources.py`：

- 通过设备 SSH 执行**固定只读脚本**：
  - `/proc/stat`（两次采样计算 CPU 使用率）
  - `/proc/meminfo`（MemTotal/MemAvailable）
  - `/proc/loadavg`
  - `/sys/kernel/debug/rknpu/load` 或 NPU devfreq
  - GPU devfreq
  - `/sys/class/thermal/thermal_zone*/temp`
- 不写设备文件，不保存凭据；
- 设备未配置 SSH 或命令失败时静默跳过，不影响 Run。

`resource_sampler.py` 在 Run target 执行期间每 15 秒采样一次，写入
`run_resource_sample`（migration `0038`）。

### 展示

- `GET /api/v1/runs/{id}/resources` 返回逐目标采样与汇总；
- Run 详情新增「资源」页签：
  - CPU / 内存 / NPU 平均与峰值、最高温度、采样数；
  - CPU / 内存 / NPU / 温度随时间曲线（SVG）。
- 报告新增「硬件资源」表：CPU/内存/NPU 平均与峰值、最高温度、样本数；
  XLSX Summary 同步输出。
- 历史 Run 没有采样时显示“该 Run 未采集硬件资源”，不会伪造 0。

## 3. 验证

```text
make test          全量通过
make lint          All checks passed
make openapi-check 契约与基线一致
cd web && npm run build / lint   通过
```

0162 直连采样实测：

```json
{"cpu_pct":2.01,"mem_used_pct":15.78,"npu_pct":0.0,
 "temp_c_max":51.8,"load1":0.17,"mem_total_mb":15934}
```