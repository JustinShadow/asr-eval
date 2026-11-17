# CER 黄金样例

数据源：`evidence/p0/spotcheck-20.csv`（P0 人工抽查 20 条，含 19 条一致与 1 条真实错误）。

门禁内容：

- 逐条复算 CER 必须与 CSV 的 `cer` 一致；
- micro CER = Σ(S+D+I) / Σ参考字符 = 2 / 250 = 0.008；
- macro CER = 样本平均 = 0.007692307692307693；
- 中文标点与空格默认不参与编辑距离。

运行：

```bash
make golden
# 或
cd backend && uv run pytest tests/golden -q
```

更新原则：只有口径变更且经人工复核后才能更新 CSV 或期望值；不要为了让失败变绿而放宽断言。