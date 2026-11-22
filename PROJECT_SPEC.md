# 公告镜像数据契约

## 版本与公告

- UTF-8 JSON 对象含 `institution`、`edition`、`parent`、`effective`、`changes`，并可含 `agreements`、`policy`。标识为大写 ASCII 字母、数字或连字符，须以字母或数字开头。首版为 `edition=1,parent=0`，后续要求父版已成为该院校最新版本，`effective` 必须严格递增，格式为 `YYYY-MM-DD`。
- 同版每个 `source_course` 最多有一条变更。`map` 记录 `target_course` 和整数 `hundredths`（百分之一学分）。`withdraw` 只能撤回当前有效的映射。不出现在变更列表里的映射沿用旧值。
- `edition` 保存规范化 JSON 及 SHA-256；`rule_change` 保存结构化变更和数组内 1 起算的来源序号。同院校同版同摘要为空操作，异摘要拒绝；批量变更原子提交。

## 转授协议与直认公告（`agreements`）

`agreements` 是一组互认段，发布方即**接收方**（受授方）。每段记录：

- `grantor`：授出该段结果的院校；
- `course`：该院校侧的课程码；
- `target_course`：接收方认可的课程码；
- `source_institution`：最初开课院校；整条链必须一致；
- `study_from`／`study_to`：适用的修课日期区间；
- `hundredths`：本段可保留的学分上限；
- `retransfer`：是否准许把结果继续转授。

`grantor == source_institution` 的段是**直认公告**；其余是**转授协议**。一段只有在 `retransfer=true` 时才能作为中间跳，且后段只能收窄前段：修课日期必须同时落入每一段区间，源校全程不变。协议的课程以 `(接收方, grantor, course, source_institution)` 四元组识别，因此同名课程在不同院校不是同一门。

`action=withdraw` 撤回一段协议，可带 `study_from`／`study_to` 限定受影响的修课日期；不带则整段撤回。只能撤回当前有效的协议。

## 接收方政策（`policy`）

同一批公告中，接收方可声明 `policy`：

- `open`（缺省）：直认与已授权转授一并开放；
- `direct_only`：只开放直认，覆盖较少但不依赖中转协议；
- `transfer_fallback`：仅在不存在适用直认时开放已授权转授，使原本无映射的课程可查；中转协议撤回后这些答复随之失效。

## 查询语义

- 查询取就读日当天或更早生效的最后一条相关公告，映射、撤回、从未映射分别返回 `mapped`、`withdrawn`、`unmapped`。基线（无协议）查询保持原口径与原有字段。
- 转授链的学分取各段 `hundredths` 的最小值，前段损失不得在后段恢复；任何回环不得用于恢复学分或延长有效期。
- 多条合法路径到达同一课程但认可不同学分（或不同目标课程）时返回 `divergent`，`divergence.results` 并列分歧，绝不相加。
- 撤回某中间段后：撤回日前的修课日期保留原合法答复；不受影响的直认与旧映射照常回答；失去依据的答复在 `invalid_paths` 中以 `withdrawn` 说明，`withdrawn_intervals` 给出影响区间，`dependencies` 列出原先所凭的公告，`baseless_transfers` 点出依赖它的后段公告。
- `lookup-span` 按修课日期区间压缩结果变化，便于定位争议影响区间；其他仍合法的映射不被一并挡住。

## 入口

- `apply`：应用一份公告（对象），或原子应用一个数组。
- `apply-many`：原子应用 JSON 数组中的多份公告；涉及多所接收方时任一条失败即整批回滚，未获转授权的条目不作为可生效来源。
- `lookup INSTITUTION COURSE ATTENDED_ON [--source SOURCE]`：按就读日查询单门课程。`--source` 在存在同名课程歧义时锁定最初开课院校。
- `lookup-span INSTITUTION COURSE START END [--source SOURCE]`：按修课日期区间汇总答复变化。

`articulation/edition.py` 解析公告与日期；`articulation/store.py` 管理 SQLite；`articulation/mirror.py` 串联事务写入、政策选择与按日链式查询。
