# 公告镜像数据契约

- UTF-8 JSON 对象含 `institution`、`edition`、`parent`、`effective`、`changes`。标识为大写 ASCII 字母、数字或连字符，须以字母或数字开头。首版为 `edition=1,parent=0`，后续要求父版已成为该院校最新版本，`effective` 必须严格递增，格式为 `YYYY-MM-DD`。
- 同版每个 `source_course` 最多有一条变更。`map` 记录 `target_course` 和整数 `hundredths`（百分之一学分）。`withdraw` 只能撤回当前有效的映射。不出现在变更列表里的映射沿用旧值。
- `edition` 保存规范化 JSON 及 SHA-256；`rule_change` 保存结构化变更和数组内 1 起算的来源序号。同院校同版同摘要为空操作，异摘要拒绝；批量变更原子提交。
- 查询取就读日当天或更早生效的最后一条课程变更，映射、撤回、从未映射分别返回 `mapped`、`withdrawn`、`unmapped`。基线不支持插入历史公告或覆写已有版本。

`articulation/edition.py` 解析公告与日期；`articulation/store.py` 管理 SQLite；`articulation/mirror.py` 串联事务写入和按日联表查询。
