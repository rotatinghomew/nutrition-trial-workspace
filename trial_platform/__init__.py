"""持续营养说明试验：试验登记、分配、持续分析与投放接管。

模块划分：

- ``trial_platform.model``     把资料表读成内存状态，并做跨表一致性检查
- ``trial_platform.inference`` 按原分配的持续查看、区间与不可判定判定
- ``trial_platform.engine``    分配与回执、答卷成熟度、决定时点快照、风险停用与接管
- ``trial_platform.service``   FastAPI 本地服务入口
- ``trial_platform.simulation`` 可重复随机合成校验
"""

__all__ = ["model", "inference", "engine", "service", "simulation"]
