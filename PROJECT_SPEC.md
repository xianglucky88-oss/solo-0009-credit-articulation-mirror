# 公告镜像数据契约

- UTF-8 JSON 对象含 `institution`、`edition`、`parent`、`effective`、`changes`。标识为大写 ASCII 字母、数字或连字符，须以字母或数字开头。首版为 `edition=1,parent=0`，后续要求父版已成为该院校最新版本，`effective` 必须严格递增，格式为 `YYYY-MM-DD`。
- 同版每个 `source_course` 最多有一条变更。`map` 记录 `target_course` 和整数 `hundredths`（百分之一学分）。`withdraw` 只能撤回当前有效的映射。不出现在变更列表里的映射沿用旧值。
- `edition` 保存规范化 JSON 及 SHA-256；`rule_change` 保存结构化变更和数组内 1 起算的来源序号。同院校同版同摘要为空操作，异摘要拒绝；批量变更原子提交。
- 查询取就读日当天或更早生效的最后一条课程变更，映射、撤回、从未映射分别返回 `mapped`、`withdrawn`、`unmapped`。基线不支持插入历史公告或覆写已有版本。

`articulation/edition.py` 解析公告与日期；`articulation/store.py` 管理 SQLite；`articulation/mirror.py` 串联事务写入和按日联表查询。

## 毕业要求公告

- 对象含 `family`、`version`、`parent`、`effective`、`programs`，可选 `share`。规则与公告版次一致：首版 `version=1,parent=0`，父版必须是该 family 的最新版，`effective` 严格递增。
- 每个 `programs[]` 条目有 `id`、可选 `required_hundredths` 与 `items`。每个 `items[]` 含 `id`、`kind`（`mandatory`/`elective`，默认 `mandatory`）、`hundredths`（组合学分目标）、`courses`（该条目恰好容纳的修课数，默认 1）、`accepts`（可接受的映射目标课程）。
- 一个条目要么不参与裁定，要么被恰好 `courses` 次归属满足且学分合计达到 `hundredths`；部分填充不算达成，也不写归属。
- `share` 规定同一修课跨项目共用的范围：`{"all": bool, "pairs": [[A,B],...], "max_hundredths": N}`。未声明时视为允许任意项目对；`all=false` 时只允许 `pairs` 列出的项目对；`max_hundredths` 进一步限定可共用修课的学分数。同一项目内一次修课不得重复归属。
- `requirement_edition` 与 `requirement_item` 分别保存公告与条目，SHA-256 标识内容；同版同摘要为空操作，异摘要拒绝。

## 批次受理与钉住视图

- 批次对象含 `batch`、`received_on`、`requirements`（`family`/`version`）、`visible`（每校当时已收到的公告版次上限，0 表示未收到）、`attempts`（每次修课的 `attempt`、`institution`、`course`、`attended_on`、`sitting`）与可选 `share`。
- `accept_batch` 把每门课在**该批次可见版次上限内**、就读日当天或更早的最后一个变更解析为 `mapped`/`withdrawn`/`unmapped`，连同公告行号、生效日与摘要一并写入 `eligibility`。之后 `inspect` 始终按这份钉住视图复核，因此柜台能按原受理批次核同一门课；新批次按自己的 `visible` 复核。
- 同一院校可见版次必须是目录中已应用的版本。批次号、每次修课标识唯一；同一 `sitting` 的不同修课视为同一补考组，裁定最多使用其中一次。
- `narrow()` 将公告声明的 `share` 与批次可选 `share` 取交集：任一方都可收窄允许项目对或降低学分上限。

## 裁定与签转

- 目标为字典序：先最大化达成的 `mandatory` 条目数，再最大化认可学分；一次修课被两个项目共用时学分只计一次。
- 可行裁定要求每次归属指向接受其映射目标课程的条目，条目按上文语义恰好填满，补考组至多一次，跨项目共用受 `share` 限制。
- `plan` 返回最优目标、每条可行归属、所有剩余最优裁定共有的归属（`common`）、尚未签下的可用归属（`remaining`）以及一条见证裁定。
- `sign --mode common` 只签 `common` 中的归属，保留改选机会，不核发回执。`sign --mode choose` 签下明示选定的一套归属：当所选集合本身构成一套完整最优裁定时立即核发回执；只选了一部分时签下其中可行的归属但不发回执，批次继续保留其余改选机会。两种模式都会列出因本次选择而**不能再签的同等最优归属**（`foreclosed`）。
- 每次归属在写事务内以 `(batch, attempt, program)` 加锁；第二名审核员提交争用同一修课的选择时得到 `commitment` 冲突而不双授，不争用的条目仍可办。

## 回执与毕业核算

- 回执快照保存已签归属、学分数、必修条目数、所依据的要求版本与可见公告版次。`receipt` 原样返回该快照。
- `reckon` 只从回执读取已签学分，不从后来的公告或要求重算。它对照钉住版与最新（或用 `--against` 指定的）要求分别复核原签事实，把阻挡分为 `catalog`（公告资格，含在钉住阈值下无法查证）、`program`（项目归属，含条目不复存在或不再接受目标课程）、`commitment`（已签承诺，含学分合计不足、修课数超出或补考组重复）。
- 若 family 已发布无法容纳原签事实的更正版，`sign` 在冲突处返回 `halted` 并列出冲突类别，不再在陈旧钉住上继续签发。`rebase` 显式采用更正版与/或更宽的可见视图；仅当更正版仍能容纳全部已签事实时才 `rebased`，否则 `halted` 并保持原钉住。
