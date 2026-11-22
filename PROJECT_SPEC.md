# 公告镜像与夜间交割数据契约

## 1. 基础公告（白天柜台镜像）

- UTF-8 JSON 对象含 `institution`、`edition`、`parent`、`effective`、`changes`。标识为大写 ASCII 字母、数字或连字符，须以字母或数字开头。首版为 `edition=1,parent=0`，后续要求父版已成为该院校最新版本，`effective` 必须严格递增，格式为 `YYYY-MM-DD`。
- 同版每个 `source_course` 最多有一条变更。`map` 记录 `target_course` 和整数 `hundredths`（百分之一学分）。`withdraw` 只能撤回当前有效的映射。不出现在变更列表里的映射沿用旧值。
- `edition` 保存规范化 JSON 及 SHA-256；`rule_change` 保存结构化变更和数组内 1 起算的来源序号。同院校同版同摘要为空操作（`replay`），异摘要拒绝（`replay_conflict`）；批量变更原子提交。
- 日常查询取就读日当天或更早生效的最后一条课程变更，映射、撤回、从未映射分别返回 `mapped`、`withdrawn`、`unmapped`。基础镜像本身仍只追加：回溯能力由下面的补发层提供，不插入或覆写 `edition`/`rule_change`。

## 2. 双时间轴

系统区分两条时间轴，互不混用：

- **生效日（effective day）**：课程答案适用的日历日，即柜台的就读日。
- **系统时刻（system instant）**：UTC ISO-8601（`...Z`），补发经夜间交割在某院校激活的时刻。

柜台可用两种视角查询：当前视角（只含已激活补发）和系统时刻回看（`lookup-as-of`，只含该时刻之前激活的补发）。已受理但未交割激活的补发不会进入任何柜台答案。

## 3. 中央补发（retroactive reissue）

`reissue` 文档含 `reissue` 标识与 `amendments` 数组；每个 amendment 指明 `institution`、被修正的历史 `edition`、更正后的 `effective` 及 `rows`。一份补发可同时覆盖多所院校，各校确认的版本与课程范围独立。

- **改原公告行**：`(院校, edition, source_course)` 与原 `rule_change` 同键时原位替换该行；`origin_row` 记录被替换的旧公告行号（1 起算）。
- **填补历史空段**：同键无原行时作为新增历史行入库，`origin_row` 为空。
- **不得误盖后来独立发布的变更**：补发只替换它点名的键；后续 edition 的行原样保留，在各自生效日照常胜出。
- **生效日严格递增**：替换生效日后，同院校 edition 间仍须严格递增，否则整份补发拒绝（`effective_order_violation`）。
- **撤回前提保护**：若修正使某条后来的 `withdraw` 不再紧跟一条有效映射，则后续撤回失去前提，**整份补发不得成为当前目录**，受理阶段即以 `withdrawal_premise_lost` 整体拒绝；激活前对完整链条再校验一次。
- 补发同标识同摘要重放为 `replay`，同标识异摘要拒绝（`reissue_conflict`）。

## 4. 授权范围

`authorize` 登记每所院校获准的课程集合。交割包内出现未授权课程，该包在收件核验时拒绝（`scope_unauthorized`），并在系列状态里保留可解释的阻塞原因。

## 5. 夜间交割系列与包

一个交割系列（`delivery_series`）属于一所院校，模式为 `delta` 或 `full`：

- **delta**：清单至少含一个 `base` 包，外加若干 `correction` 小包；基底齐全后可只交更正包（引用已激活的 `base_series`），为其他课程节省本批额度。小包缺失时不能跳过基底：无基底且无已激活基底系列的系列在开设时拒绝（`invalid_manifest`/`base_not_active`/`missing_base_series`）。
- **full**：清单恰为一个 `full` 包，重交完整授权范围，占用更多本批额度。
- **额度按材料记录数计算**：系列开设时声明每包 `records` 与总 `quota`，清单超额即 `quota_exceeded`；收件时按已核验包累计，超额同样拒绝。
- 包状态：`pending → verified | rejected`。系列状态：`open → complete → active → superseded`。
- **收件、核验、柜台切换相互独立**：包只在 `activate` 成功后才进入柜台目录；只收到半批时柜台继续答旧目录，另一校完成后即可答新目录。
- **整份重交保护**：`full` 系列激活时若院校在交割期间又确认了新公告版（head edition 前移），拒绝激活（`school_update_conflict`），不得覆盖校方更新过的目录；delta 模式允许 head 前移，后续独立变更保留。
- **幂等与迟到**：同内容重复收件返回 `replay`；系列激活或被取代后迟到的同包仍是 `replay`，不产生第二次激活。被拒包槽位允许更正后重传。
- **未生效原因可解释**：`status` 的 `blockers` 区分缺哪批（`missing_batches`）、包被拒及原因（`rejected_package`，内含 `scope_unauthorized`、`material_mismatch`、`reissue_not_effective` 等）；激活失败另有 `school_update_conflict` 与 `withdrawal_premise_lost`。

## 6. 查询与溯源

- `lookup`（当前视角）与 `lookup-as-of`（系统时刻视角）返回结论、生效日、公告版、来源行号与摘要；答案来自补发时附带 `reissue` 与 `origin_row`。
- `lineage` 给出一门课的完整新旧公告行链：补发行通过 `replaces` 关联被替换的原公告行（空段填补时为 `null`）。**来源改变、结论未变**的记录同样能同时核到新旧两条公告行及各自 SHA-256。
- 中央对某院校已交割状态的查询（`status`）与柜台日常查询在迟到回执、重复收包及随后一次新更正后保持一致：答案中的 `reissue` 必出现在该院校的激活记录里。

## 7. 模块

`articulation/edition.py` 解析公告与日期；`articulation/store.py` 管理 SQLite 全部表；`articulation/mirror.py` 串联基础事务写入并把日常查询委托给补发感知层；`articulation/settlement.py` 实现授权、补发受理、夜间交割状态机与双时间轴查询；`articulation/__main__.py` 提供命令行入口。
