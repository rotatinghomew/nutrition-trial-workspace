# 持续营养说明试验的分流、推断与投放接管

本项目资料使用本地合成记录，不含真实账号或个人资料。现有工具读取业务资料并检查交接格式，不对资料作业务判决。

运行环境为 Python 3.11。`requirements.txt` 固定直接依赖版本，`requirements.lock.txt` 固定完整依赖。建立虚拟环境后使用 `python3 -m pip install -r requirements.lock.txt` 安装；离线运行时使用已准备的依赖环境。

在项目根目录执行 `python3 -m materials` 查看资料清单，执行 `python3 -m unittest discover -s tests -v` 校验字段类型、编号和跨表引用。`python3 -m compileall -q materials tests` 检查现有 Python 入口。上述检查只证明资料可读取，不代表业务应用的结果。

数据字典见 [资料协议](docs/data-contract.md)，机器可读定义为 `data/schema.json`。每张表对应同名 UTF-8 JSONL 文件，每行一条原始记录。时点使用含时区的 ISO 8601，日期采用 YYYY-MM-DD。结束边界不包含边界时点；空值表示协议说明的未知或不适用，不作默认数值。

资料读取入口位于 `materials`，交接格式检查位于 `tests`。本地运行状态可以写入 `local-state`；原始资料保持可追踪。现有资料不包含模型参数、业务算法或预先计算的分析结论。

## 持续试验平台

`trial_platform` 在原始资料之上承担试验登记、实际分配、持续分析与投放接管，
提供本地 HTTP 服务给工作人员操作：

```bash
python3 -m trial_platform serve --port 8000
python3 -m trial_platform summary --as-of 2026-02-06T00:00:00+08:00
python3 -m trial_platform simulate --trials 100
python3 -m unittest discover -s tests -v
```

设计、估计量、判定门槛与操作范围见 [平台设计说明](docs/platform.md)。要点：

- 合资格访问先分配、后展示；重复请求与设备切换返回同一随机单位，家庭不能同时
  进入同一互斥范围的两个试验；
- 每次分配记录当时政策、实际采用概率和入组原因；概率缺失会把相关范围降为不可
  判定，不删除家庭；
- 回执可重复、可长期不明；停止新增分配后未兑现许可有明确撤销状态，已发生展示
  仍兑现；
- 主要比较按原分配，窗口内无答卷不剔除，另报显示偏离与答卷缺失敏感性；
- 持续查看使用置信序列控制错误推广机会，推广需效果与风险条件在同一证据范围同时
  成立并由获批负责人确认；
- 接管只覆盖已回执范围，未接管范围继续试验，迟到答卷归原试验；
- 随时可查某天当时的决定、结论为何改变、哪些许可未收回、推广在哪些渠道生效。

运行时状态写入 `local-state/events.jsonl`（只追加，可重放）；`POST /admin/reset`
清除运行时状态并回到资料原貌。原始资料表保持不变。

