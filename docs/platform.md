# 持续试验平台设计说明

本说明解释 `trial_platform` 如何把题目中的业务约束落到代码。原始资料协议见
[资料协议](data-contract.md)；这里只说明实现取舍和操作方式。

## 1. 组成

| 模块 | 职责 |
| --- | --- |
| `trial_platform.model` | 读取 JSONL，解析时区，做跨表一致性检查，记录可降级原因 |
| `trial_platform.inference` | 按原分配的比例估计、持续查看区间、推广/风险判定 |
| `trial_platform.engine` | 登记、政策、访问分配、回执、答卷、快照、许可与接管 |
| `trial_platform.service` | FastAPI 本地服务，供工作人员操作 |
| `trial_platform.simulation` | 可重复随机合成校验与端到端演练 |
| `materials` | 原有资料读取与格式检查（保持不变） |

状态 = 原始资料表（只读）+ 只追加的 `local-state/events.jsonl`。服务重启后重放
事件恢复。`POST /admin/reset` 删除本地事件流，回到资料原貌。

## 2. 随机单位与互斥

- **随机单位是家庭**。同一 `request_key` 重复到达返回原分配，不重新随机；同一家庭
  换设备或换请求键再次访问，仍返回第一次的分配（`duplicate_household`），
  避免设备切换产生新的随机单位。
- 分配由 `SHA-256(experiment_id, household_id, request_key)` 决定，给定政策权重即
  确定；重放与并发都不改变结果。
- 同一家庭不能同时进入同一 `exclusive_scope` 的两个试验；冲突访问被拒绝并在
  审计日志留证。
- 分配在展示之前形成，未形成的机会不会被补记成某个版本的曝光。

## 3. 分配概率与可追溯

- 每次分配记录：`policy_id`、该次实际采用概率 `probability`、入组原因
  `enrollment_reason`、当时政策权重与生效时间。
- 政策可随获批版本更替；分析结果带 `assignment_probability_model.policy_versions`。
- 资料中 `probability` 缺失**保留原貌**，并把该范围结论降为 `undecidable`，不会
  用默认数值填补，也不会悄悄删掉该家庭。

## 4. 展示许可、回执与停止

- 分配即一张展示许可。回执 `displayed` 表示已兑现；`unknown` 表示结果长期不明，
  不算已显示，也不当作未显示。
- 相同回执重复提交按 `receipt_key` 或（分配、时间、渠道、状态）去重。
- 回执晚于答卷被项目知道时，仍按各自的 `known_at` 决定当时能看见什么。
- 风险停用在 `at`（事实生效时间）起只影响**指定范围的新增分配**：未兑现许可在该
  时点转为 `revoked`，已发生的展示保持 `redeemed`；停用后仍出现的展示标为
  `post_stop_display`，供安全负责人追查。回溯快照只计入当时已知道的决定。
- 渠道撤销回执（`permit_revoke_ack`）把对应许可标为 `revoked_acknowledged`；没有
  回执就不能假装渠道已经切换成功。
- 渠道未知的家庭：渠道范围停用时许可标 `scope_uncertain`，不臆断已撤销或未撤销。

## 5. 答卷窗与主要估计量

观察窗从**机会形成时间**起算，长度是登记的 `followup_hours`。每个已成熟家庭落入
一种状态：

| 状态 | 含义 |
| --- | --- |
| `answered` | 窗口内作答，取最高 `revision` |
| `outside_window` | 只有窗口外答卷（逾期） |
| `missing` | 已成熟但没有任何答卷 |
| `waiting` | 尚在窗口内，未成熟 |

主要比较（事前登记，按原分配）：

- 正确理解比例 = 窗口内正确人数 / **全部已成熟家庭**
- 错误确信比例 = 窗口内错误确信人数 / **全部已成熟家庭**

窗口内无答卷按“未正确理解、未错误确信”计入，不删除家庭。另报告三种敏感性：
只算已答家庭（complete case）、把缺失都当作错误确信（worst case）、以及实际显示
偏离（未收到回执 / 回执不明 / 停用后展示）的分布。敏感性只用于解释，**不用于
挑选有利算法**。

## 6. 持续查看与错误推广控制

固定样本的普通显著性检验会在反复查看后抬高错误推广概率。这里使用二项比例的
**混合鞅置信序列**（Beta 混合备择、复合零假设 `p = μ`）：

- 任意时点查看都有效，不需要预先固定样本量；
- 判定门槛：`log M_n(μ) < log(1/α)` 的 μ 构成当前区间；
- 效果差 `候选 − 参照` 由两个比例的区间取并集界；
- 推广还需 `效果下界 ≥ minimum_gain` 且 `风险上界 ≤ maximum_false_certainty`，
  两项条件必须在**同一份有效证据**上同时成立；
- 总错误水平 5% 先按候选说明数与事前登记范围数拆分，再在效果/风险两项条件间
  对半拆分。因此“每天看一次”仍把每个说明的错误推广机会控制在登记水平内。

`trial_platform.simulation.run_suite` 用固定种子重复演练：无改善、少量样本、短观察
期三种情况下错误推广率应不超过 α；有真实改善时应能在观察期内识别；错误确信超限
时应触发风险警示。少量样本碰巧好看不会变成推广。

## 7. 不可判定范围

以下情况只把**相关范围**降为 `undecidable`，其他范围照常分析，且不删除家庭：

- 某次分配概率缺失或越界；
- 同一家庭同时进入互斥范围的两个试验；
- 分配使用未登记的说明、设备不属于家庭、机会早于合资格时间；
- 参照说明或候选在该范围没有任何分配（失去共同支持）；
- 答卷早于机会、回执早于机会、答卷可知时间早于作答时间。

资料范围之外的问题不会牵连无关试验。

## 8. 三类操作的范围不同

| 操作 | 影响范围 | 不影响 |
| --- | --- | --- |
| 推广 | 已确认的效果范围 | 其他人群/渠道继续试验；未成熟范围不能推广 |
| 继续收集 | 尚未成熟或尚在观察的范围 | 已停止范围不恢复新增分配 |
| 风险停用 | 明确人群+渠道的新增展示 | 其他范围继续；已发生展示仍兑现 |

结论按范围给出 `promotable / risk_alert / risk_watch / collecting / not_supported /
undecidable`。风险信号只用事前登记的主要指标（按原分配，缺失不当作阳性）：

- `risk_alert`：区间下界已越过上限，是确定的越界证据；
- `risk_watch`：区间上界仍高于上限，尚不能排除风险，既不能推广，也不能当作
  “继续收集”的理由；安全负责人可以据此立即停止该范围的新展示。

complete-case 与 worst-case 结果只随分析报告，不能单独用来停用某个范围。否则
就等于按“看过并答了题的人”挑选人群。

- 推广确认要求：结论不是 `undecidable`、效果与风险条件同时成立、操作人是登记的
  获批负责人（`approved_leads`）。推广申请本身不改变分流。
- 接管回执必须引用**已确认**的推广，且范围不能超出推广范围。资料中 `promote-1`
  只有申请、没有确认，`take-1` 因此被判为无效接管，不会假装渠道已切换。
- 部分接管：已接管范围的后续访问由新政策服务，记为 `handover`，**不进入试验**，
  也不计入试验效果；未接管范围继续按原分配进入同一试验。迟到答卷按原分配归入原
  试验。

## 9. 快照与解释

- `GET /experiments/{id}/snapshot?at=...` 只使用 `known_at <= at` 的记录，重放
  “那天当时能作的决定”：结论、各项比较、当时的决定、无效接管、未收回许可。
- `GET /experiments/{id}/explain-change?scope=...&from_at=...&to_at=...` 列出两次
  时点之间新成熟的分配、新收到的答卷、新记录的决定，以及结论为何变化。
- `GET /experiments/{id}/permits` 列出每张许可的 `outstanding / redeemed / revoked /
  revoked_acknowledged / expired / post_stop_display`。
- `GET /experiments/{id}/handovers` 列出已接管范围的访问，供核对推广实际生效的
  渠道与人群。
- 快照里的 `promotion_coverage` 按人群×渠道列出每项已确认推广是 `taken_over`
  （已有接管回执）还是 `still_trial`（尚未回执、继续按原分配进入试验）。

## 10. 配置不是硬编码

说明编号、人群、渠道、候选数量全部来自登记与政策，没有把演练资料里的
`plain/explain/compare` 之类写进判定规则。新增试验和说明通过
`POST /experiments`、`POST /experiments/{id}/policies` 完成。

## 11. 运行与校验

```bash
python3 -m unittest discover -s tests -v          # 资料与平台测试
python3 -m trial_platform simulate --trials 100   # 随机合成校验 + 端到端演练
python3 -m trial_platform serve --port 8000       # 本地服务
python3 -m trial_platform summary --as-of 2026-02-06T00:00:00+08:00
```

依赖：`requirements.lock.txt`（含 FastAPI/uvicorn/numpy/pandas/scipy）。
`httpx` 只在运行 HTTP 层测试时需要；缺失时该测试自动跳过。

## 12. 已知取舍

- 置信序列比固定样本区间宽，识别小幅改善需要更多成熟家庭；这是持续查看的代价，
  换来的是不因反复查看而虚高推广。
- 渠道归属依赖回执或访问请求；完全没有渠道信息的家庭不能进入渠道层结论，只能在
  人群层解释，且会明确标出。
- 运行时只追加事件，不压缩；事件量很大时 `reset` 或归档 `local-state` 即可。
