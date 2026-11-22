# 课程互认公告镜像

**数据工程 · 数仓与数据湖。** 将院校逐版发布的课程互认公告摄入 SQLite，按就读日还原当时适用的参考数据，并返回公告摘要和来源行。在此基础上提供联合办学的分批毕业审核：按受理时可见的公告逐门核资格，按字典序求最优学分裁定，分批签转并核发回执，且让回执学分不被后来的公告或要求改写。本项目不办理学生申请，也不处理个人信息。

在仓库根目录以 Python 3.9+ 运行，无第三方依赖：

```sh
python3 -B -m articulation --db demo.sqlite apply examples/opening.json
python3 -B -m articulation --db demo.sqlite lookup BAY-FIELD HIS-17 2025-12-31
python3 -B examples/smoke.py
```

样例首次导入两条映射；查询返回 `HIST-120`、`300` 学分百分位。离线演练进一步导入撤回公告，验证新旧日期的不同结果、跳版拒绝和冲突重放。字段和持久化语义见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。

## 分批签转演练

```sh
python3 -B examples/demo.py                       # 完整走查，输出各步 JSON
python3 -B -m unittest discover -s tests -t .     # 41 项行为与命令行测试
```

`demo.py` 在临时库里重放一条完整链路：导入公告与毕业要求，受理 `BATCH-A`（含一次补考组与一次主修/辅修共用修课），先只签所有剩余最优裁定共有的条目，再明示选择一套最优归属并核发回执，最后做柜台复核与毕业核算。命令行各子命令：

| 命令 | 作用 |
| --- | --- |
| `apply-requirements FILE` | 应用版本化毕业要求公告 |
| `requirements FAMILY [--version N] [--on YYYY-MM-DD]` | 读取要求公告 |
| `accept FILE` | 受理批次并钉住逐门可见公告视图 |
| `inspect BATCH [--course C]` | 柜台按原受理批次复核同一门课 |
| `plan BATCH` | 字典序最优裁定、共有与剩余归属 |
| `sign BATCH --mode common\|choose [--select FILE] [--reviewer R]` | 分批签转，`choose` 核发回执并列出被排除的同等最优归属 |
| `rebase BATCH [--requirement N] [--visible FILE]` | 显式采用更正后的要求或更宽可见视图 |
| `reckon BATCH [--receipt ID] [--against N]` | 从回执读取已签学分并分类冲突 |
| `receipt BATCH [--receipt ID]` | 读取冻结的回执快照 |
