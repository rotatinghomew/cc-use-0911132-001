# 业务演练数据口径

orders 是订单篮，lines.quantity 为原始件数；revisions.quantity 为本次退回或撤销件数，不是剩余数量。availability 为左闭右开状态段，重叠时 start 较晚的段优先；没有状态表示未知可售机会，非缺货。proposal_end、validation_end 是按 placed_at 划分的半开时段终点。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
