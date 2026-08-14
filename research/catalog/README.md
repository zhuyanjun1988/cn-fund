# 策略研究目录

本目录是公开仓库的规范研究索引。它解决三个问题：

1. 同名策略到底是哪一版；
2. 哪些候选通过形成期门、哪些已经否决；
3. 历史结果是否拥有样本外、模拟或实盘权限。

文件：

- [`strategy_registry.json`](strategy_registry.json)：策略身份、数据窗口、指标和当前权限；
- [`research_rounds.json`](research_rounds.json)：每一轮假设、唯一改动、分支数和裁决；
- [`lineage.json`](lineage.json)：父子、衍生和否决邻居关系。

状态词含义：

- `REJECTED_*`：候选失败，保留负面证据，不再在同一历史中修补；
- `FROZEN_CONTAMINATED_FORMATION_*`：通过了形成期事前门并冻结，只能收集未来证据；
- `PASS_CONTAMINATED_RESEARCH_ONLY`：可作为研究候选，不代表可交易；
- `simulation_status=NOT_APPROVED`、`live_status=NOT_APPROVED`：没有模拟或实盘授权。

目录不会把 C23/C26 的短窗口结果改写为 clean OOS，也不会把指数发布前回溯改写成
基金实盘历史。完整公开摘要见
[`evidence/c21-c26/public_research_summary.json`](../../evidence/c21-c26/public_research_summary.json)。
