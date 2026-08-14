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
基金实盘历史。C44 虽然通过了冻结的 C11 替换门，但仍属于已污染形成期和当前幸存产品选择；
它只是大多数人的研究默认后继。C45 保持 60% 权益并通过了高进攻目标下的 C11 替换门，
只对明确要求更高权益的用户成为研究后继。C46 在不提高名义权益的前提下，将 C45 的
5% 现金改为 3% 多经理固收+、1% 短国开债和 1% 现金；它只对有独立应急金且接受更多
产品维护的用户成为优化进取后继。C46 不替代 C44 的大众默认地位，也不普遍替代更简单的
C45。三者都不拥有模拟、实盘或立即清仓迁移权限。

公开摘要：

- [`evidence/c21-c26/public_research_summary.json`](../../evidence/c21-c26/public_research_summary.json)
- [`evidence/c44/public_research_summary.json`](../../evidence/c44/public_research_summary.json)
- [`evidence/c45/public_research_summary.json`](../../evidence/c45/public_research_summary.json)
- [`evidence/c46/public_research_summary.json`](../../evidence/c46/public_research_summary.json)
