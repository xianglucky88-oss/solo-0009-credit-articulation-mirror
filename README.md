# 课程互认公告镜像

**数据工程 · 数仓与数据湖。** 将院校逐版发布的课程互认公告摄入 SQLite，按就读日还原当时适用的参考数据，并返回公告摘要和来源行。本项目不办理学生申请，也不处理个人信息。

在仓库根目录以 Python 3.9+ 运行，无第三方依赖：

```sh
python3 -B -m articulation --db demo.sqlite apply examples/opening.json
python3 -B -m articulation --db demo.sqlite lookup BAY-FIELD HIS-17 2025-12-31
python3 -B -m articulation --db demo.sqlite apply-many examples/transfer.json
python3 -B -m articulation --db demo.sqlite lookup ELMDALE CS-10 2026-01-15
python3 -B examples/smoke.py
python3 -B examples/scenarios.py
```

样例首次导入两条映射；查询返回 `HIST-120`、`300` 学分百分位。`examples/transfer.json` 演示同源课程经中转后在两所接收院校的直认与转授：`NORTHGATE` 授权再转授，`SOUTHPORT` 不授权；`ELMDALE` 只能经 `NORTHGATE` 查到该课。`examples/scenarios.py` 覆盖分歧并列、撤回区间、接收方政策、回环与批量原子性。离线演练进一步导入撤回公告，验证新旧日期的不同结果、跳版拒绝和冲突重放。字段和持久化语义见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
