# 可售机会约束下的采购篮关联推荐

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `b2b_reorder_network.dispatch.cli`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/reorder_sagas -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q b2b_reorder_network tests`。

业务调用：`python3 -m b2b_reorder_network.dispatch.cli fixtures/demo.json --database /tmp/b2b_reorder_network-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：stock_gap、late_return、buyer_concentration。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

分析入口接通后执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。尚未接通时该命令会报模块不存在，不会用静态样例冒充成功。分析 ID 为 `analysis_id`，其余输出包括 `rules, validation, exclusions, source_versions`。方法正确性还需覆盖业务说明中的反例，结构核验不能代替统计验证。

### 采购篮关联分析 `b2b_reorder_network.basket_insights`

入口（不改动任何实际下单流程，只读取本地明细并写入分析表）：

```
python3 -m b2b_reorder_network.basket_insights --input fixtures/analysis/input.json --database <order_store.sqlite3> --output <association.json>
```

口径与护栏（完整定义同时写入结果的 `methodology` 字段）：

- **篮子化**：一次订单一个篮子；同款多件只计一次共现。退货/撤销按 `available_at` 做时点门控，`as_of` 之后才可知的事实不采用，窗口终点后才可知的更正不倒灌该窗口；全退/全撤的行不再支持搭配，但篮子保留在分母里。
- **可售机会约束**：可售状态为左闭右开分段、重叠时晚开始者优先，无状态段表示未知而非缺货。两件商品在该篮下单时刻没有同时可售机会时，该篮不进入该商品对的分母，也不算一次未购买；状态未知的篮子单独披露。
- **关联度量**：support（支持度）、confidence（条件购买概率）、lift（相对基准购买概率的变化），并给出 Wilson 95% 区间及 lift 的保守区间。全部规则标记为 `association_only`：没有干预/对照组，不声明促销因果效果。
- **稳定性与防操纵护栏**：规则只在提案窗口（`proposal_end` 之前）筛选，在不参与筛选的验证窗口独立复算，区间重叠且有复现购买才记 `validated`。候选还须满足 `min_buyers` 个不同买家、最大买家份额不超过 0.5、支持篮跨至少两个 ISO 周，避免单个采购商或一个短促销周决定名单；候选截断到 `max_rules`，截断记录写入 `exclusions`。
- **零证据不包装**：零同时可售机会记为 `zero_simultaneous_opportunity`，有机会但零共购记为 `no_observed_co_purchase`，都不产出 lift=0 之类的伪结论。
- **可回查与账本关联**：每条规则带 `evidence_orders`（支持/排除/缺货/未知及验证窗口订单号），并持久化到 `basket_analysis_rules` 等表；`baseline_run_id` 关联同库 `runs` 表中的既有演示运行。
- **幂等与版本**：同一批明细重放复用同一 `analysis_id`，不产生第二份分析；输入变化（如退货新版本）生成新分析，并通过 `lineage.supersedes_analysis_id` 与旧版对照。

分析方法测试：`python3 -m unittest discover -s tests -p 'test_basket_insights.py' -v`。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
