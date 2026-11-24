# 业务资料协议

说明编号是原始渠道字典，不是固定实验算法。设备键只在所属家庭内有效。所有时点采用含偏移的ISO 8601。声明类型total表示自主关注总糖，ingredient表示只关心添加成分，taste表示口味。答卷两个布尔编码来自不同问题，允许彼此不一致。回执未报或unknown不证明未显示。

所有数字、身份和业务来件均为合成。记录的稳定编号只在对应表内唯一，跨表关联以明确字段为准。布尔值为原来源明确回报，空字符串引用表示没有已知对应。金额如有均用整数分，比例为0至1，其他单位在字段中注明。

## experiments

业务方登记的说明试用计划。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| exclusive_scope | string | 不能同时试用的家庭范围标识 |
| start_at | string | 开始允许入组时点 |
| end_at | string | 最长收集结束时点 |
| followup_hours | number | 从机会形成起的答卷观察小时 |
| minimum_gain | number | 正确理解比例的最低实际增益 |
| maximum_false_certainty | number | 错误确信比例上限 |
| look_policy | string | 事前批准的查看方式 |
| arms | array | 候选说明编号 |

## households

家庭及设备的合资格登记。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| segment | string | 自主登记的需求人群 |
| region | string | 可用渠道地区 |
| devices | array | 属于同一合资格家庭的设备键 |
| eligible_from | string | 允许参与的最早时间 |

## assignments

渠道历史形成的分配事实；概率缺失保留原貌。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| household_id | string | 家庭编号 |
| experiment_id | string | 试用计划编号 |
| opportunity_at | string | 展示前形成机会的时间 |
| known_at | string | 记录首次可用的时间 |
| arm | string | 原分配说明编号 |
| probability | nullable_number | 该次实际分配到该说明的概率 |
| policy_id | string | 分流政策版本 |
| request_key | string | 渠道请求重复识别键 |
| device | string | 当次设备键 |

引用关系：household_id 对应 households.id；experiment_id 对应 experiments.id。

## displays

实际显示的渠道回执。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| assignment_id | string | 对应分配 |
| occurred_at | string | 渠道动作时间 |
| known_at | string | 回执可知时间 |
| channel | string | 实际渠道 |
| status | string | displayed表示已显示；unknown表示结果不明 |

引用关系：assignment_id 对应 assignments.id。

## answers

延迟返回的理解问答。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| assignment_id | string | 问答对应的原分配 |
| answered_at | string | 作答时点 |
| known_at | string | 答卷被收到的时点 |
| correct | boolean | 是否正确区分两个概念 |
| false_certainty | boolean | 错误且确信的原始答卷编码 |
| revision | number | 同一答卷的修订序号 |

引用关系：assignment_id 对应 assignments.id。

## decisions

人工登记的范围控制和渠道接管回执。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| experiment_id | string | 试用计划 |
| scope | string | 人群及渠道适用范围 |
| kind | string | risk_stop、promote_request、takeover_ack或permit_revoke_ack |
| at | string | 事实生效时间 |
| known_at | string | 被项目知道的时间 |
| reference | string | 被回复的业务决定编号；空串表示原始决定 |

引用关系：experiment_id 对应 experiments.id。

## access_requests

渠道随后提交的访问请求；未附分配答案。

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| id | string | 记录的稳定编号 |
| household_id | string | 家庭 |
| experiment_id | string | 申请试用计划 |
| request_key | string | 原请求重复识别键 |
| at | string | 请求到达时间 |
| channel | string | 渠道 |

引用关系：household_id 对应 households.id；experiment_id 对应 experiments.id。
