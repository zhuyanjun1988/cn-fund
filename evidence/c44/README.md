# C44 公开研究证据

本目录发布 C44 的脱敏、机器可读研究摘要：

- [`public_research_summary.json`](public_research_summary.json)：精确权重、形成期指标、对照、压力、结构和产品边界；
- [`../../config/strategy-research-c44-v1/001_C44-Standard-1_public_contract.json`](../../config/strategy-research-c44-v1/001_C44-Standard-1_public_contract.json)：决策、执行、成本和晋级门；
- [`../../scripts/verify_c44_public_release.py`](../../scripts/verify_c44_public_release.py)：使用 Python 标准库校验公开摘要的权重、差值和权限边界。

本次没有上传第三方原始净值、基金网页响应、PDF 副本、账户数据或本机路径。
公开摘要保留原始合同、代码、数值结果、当前结构/产品审计和独立复算结果的
SHA-256，用于对照内部不可改写工件。

全部 2015—2026 结果仍属于 `contaminated formation`，不是 clean OOS、模拟盘或
实盘绩效。C44 只在研究策略和新增资金目标层面替代 C11；已有 C11 持仓的分批迁移
必须先核对买入日、赎回费、当日限购和家庭组合的黄金总敞口。

人类可读说明见 [C44 策略与 C11 替换建议](../../docs/15_C44成熟三因子多经理固收加杠铃策略.md)。
