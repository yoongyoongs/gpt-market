# V3 生产调度拆分 + run_once 优化 + 看板中文化与交互整改 实施报告

> 设计指导：《V3_生产调度拆分_run_once优化_看板中文化与交互整改_详细设计指导.md》
> 分支：`fix/v3-candidate-engine-round2-1`　报告日期：2026-09-11
> 本文对应设计 §105 的 13 项要求。生产验收（§107-§111）待 SSH 恢复后执行。

---

## 1. Before commit（实施前基线）

| 项 | 值 |
|---|---|
| 基线 commit | `782dcc0`（Commit A 文档交接）之前的生产逻辑为 `9b5debc` 前夜状态 |
| 调度 | 单一 `V3_SCHEDULE_AT=18:45`，run_once 一次跑完全部 13 个 Job（Data Prep 与扫描挤同一时点，2C/1.9G 机器上 SSH 握手被负载挤掉） |
| run_once 语义 | 手动触发 = 按 execution_order 全量重跑所有 Job |
| 看板 | 用户可见大量英文内部代码（Live Status / Pipeline / Attention / Universe / DEAD / Why Not / observed / mean_return_3d 等） |
| Final30 | 只显示代码+分数，无股票名称 |
| Why Not | 仅 `?whynot=` 参数、标题裸代码、列=阶段/状态/分数/名次/淘汰原因（原文英文） |
| 行情筛选 | 点"应用"整页 GET 重载，滚动位置丢失、其他区块重新加载 |
| 回归基线 | 919 passed / 106 skipped |

## 2. After commit（实施后状态）

| 项 | 值 |
|---|---|
| head | `cc1de9a`（全部已 push） |
| 调度 | 四时点拆分 15:35 / 18:20 / 18:45 / 20:30，`V3_SCHEDULE_AT` Deprecated 兜底 |
| run_once | 智能补跑入口：按组 catch-up（`(组内最后成功交易日, 今天]` 排他补齐） |
| 看板 | 展示层全中文（原始值留 HTML title / API raw 字段），未映射兜底"未配置中文名称"+日志 |
| Final30 | 代码+名称+分数+轨迹链接，名称批量 1 次查询 |
| 轨迹 | `trace=` 任意股票查询（`whynot=` 兼容保留），中文阶段/状态/说明 |
| 行情筛选 | fetch 局部刷新，只换表格+摘要，失败保留旧表 |
| 回归 | 931 passed / 106 skipped |

## 3. 四组 commit

| Commit | 内容 | hash |
|---|---|---|
| A | `scheduler: split daily pipeline into timed groups`——四时点 daily_slots、run_once 分组智能 catch-up、CLI `--group`、ensure_eod_prerequisites（§11）、eod_scan_completed 显式判断（§16）、V3_SCHEDULE_AT Deprecated fallback（§15）、resolve_next_slot 单主循环（§13）、SchedulerBundle 四组字段（§7）；附带 `e56f6f1` 修 phase6 契约测试文档路径 | `9b5debc` |
| B | `ops: cap worker load and candidate minute requests`——资源治理核对，零缺口：V3_PHASE2_CONCURRENCY=4、V3_SCAN_MINUTE60_CONCURRENCY=4、candidate 只抓 60m、yield throttle 100/0.5、docker `--cpus/--memory/--pids-limit`、worker DB pool 3+1 均已在 26a3fa9 等前序 commit 落地，本轮核对无新代码 | 无新 commit（核对结论） |
| C | `dashboard: add Chinese presentation labels and scan names`——presentation 层 zh_cn_labels、看板全量中文化、Final30 名称（禁 N+1）、trace= UX、scan API label 字段 | `e013b1f` |
| D | `dashboard: make feature filters update table in place`——features-fragment 局部刷新 | `cc1de9a` |

## 4. Scheduler 新时间表

| 时点 | 组 | Job（拓扑序） |
|---|---|---|
| 15:35 | data-prep | market-data → index-benchmarks |
| 18:20 | evidence | evidence-increment |
| 18:45 | eod-scan | features → full-recall → candidate-scan |
| 20:30 | maintenance | corporate-action-match / projection-verify / performance-mature / recall-observation-mature / shadow-observation / expected-run-registry / candidate-outcome-mature |

- env：`V3_DATA_PREP_AT` / `V3_EVIDENCE_AT` / `V3_EOD_SCAN_AT` / `V3_MAINTENANCE_AT`（默认 15:35/18:20/18:45/20:30）；`V3_EOD_SCAN_AT > V3_SCHEDULE_AT(Deprecated) > 默认` 三级 fallback，非法值抛 ValueError
- eod-scan 前置检查（§11）：当日 market-data 与 evidence-increment 未成功 → 跳过本日 EOD 并落 SKIPPED（prereq not ready），不裸跑
- 各组独立幂等锁；组间无锁冲突互杀

## 5. run_once 新语义

- `python -m scripts.v3_scheduler --once` = **检查缺失 + 按组顺序补齐**，不再是"全量重跑"
- 每组独立 catch-up：`catchup_trade_dates = (该组 last_succeeded_idempotency_key, today]` 排他区间，已成功的交易日自动跳过（幂等安全）
- `--group data-prep|evidence|eod-scan|maintenance|all` 可只补跑指定组
- 报告结构：`report["groups"][组] = {required_jobs, pending, runs}`；maintenance 组为原始 orchestrator 报告；非交易日数据组 `{status: SKIPPED, reason: NON_TRADING_DAY}`；状态聚合：空 runs=COMPLETED，LOCKED/SKIPPED(prereq)→PARTIAL
- candidate-scan 失败不被 full-recall 成功掩盖（§99，report 标 PARTIAL）

## 6. 所有新增 env

| env | 默认 | 说明 |
|---|---|---|
| `V3_DATA_PREP_AT` | `15:35` | Data Prep 组触发时点 |
| `V3_EVIDENCE_AT` | `18:20` | Evidence 组触发时点 |
| `V3_EOD_SCAN_AT` | `18:45` | EOD Scan 组触发时点 |
| `V3_MAINTENANCE_AT` | `20:30` | Maintenance 组触发时点 |
| `V3_SCHEDULE_AT` | — | **Deprecated**：仅当四新 env 未设时作 EOD 兜底，日志提示迁移 |

（B 组既有治理 env 不变：`V3_PHASE2_CONCURRENCY` / `V3_SCAN_MINUTE60_CONCURRENCY` / `V3_PHASE2_YIELD_EVERY` / `V3_PHASE2_YIELD_SECONDS` / `V3_WORKER_CPU_LIMIT` / `V3_WORKER_MEMORY_LIMIT` / `V3_WORKER_PIDS_LIMIT` / `V3_WORKER_DB_POOL_SIZE` / `V3_WORKER_DB_MAX_OVERFLOW`，已在 `.env.example` 文档化。）

## 7. 中文 Label 覆盖范围

`app/v3/presentation/zh_cn_labels.py` 十余张映射表，全部消费于展示层：

| 表 | 覆盖 |
|---|---|
| STAGE_LABELS | UNIVERSE/SAFETY/RECALL/PARETO/MACHINE/DEEP/FINAL → 全市场…最终候选（7） |
| STAGE_PASS_NOTES | 每层通过时的本层说明（7） |
| EXPERT_LABELS | LP/BT/RV/AC/PB/RS/FQ/CAT 8 专家，展示序=注册序（不按字母） |
| REGIME_SECTION_LABELS / REGIME_FACT_LABELS | breadth/turnover/risk_appetite 三节；observed 在宽度节=纳入统计股票数、成交节=有效成交股票数（分节消歧义） |
| JOB_LABELS | 13 个 orchestrator Job |
| DROP_REASON_LABELS | Safety 8 项 + Recall/Rank 3 项 + 60m 4 项 + 趋势 3 项 + no_chase_extension/trendline_break（§49-§53 全量） |
| SUPPORT_NOTE_LABEL | support_not_broken 是加分证据 → "关键支撑仍未跌破（多头证据）"正向说明（§52） |
| STATUS_LABELS | 含 ALREADY_SUCCEEDED/RUNNING/LOCKED/ACKED |
| SESSION_LABELS / LIVE_STATUS_FIELD_LABELS | 交易中/开盘前/午间休市/已收盘；判断时间/上海时间/交易日期/是否交易日/当前交易时段 |
| EVENT_TYPE_LABELS | AttentionEventType 13 项，未知 → "其他事件" |
| FIELD_LABELS | 27 个特征字段（missing_fields 与表头共用） |
| MARKET_LABELS | SH→沪市 / SZ→深市 / BJ→北交所（§60） |
| ERROR_KIND_LABELS | 运行超时/数据源异常/数据库异常/任务执行失败（§80 关键词归类） |

兜底规则（§36）：未映射内部代码不上屏 → "未配置中文名称" + `dashboard_unmapped_label` 日志；None/空 → "—"。DB/API 绝不写入中文（§85），原始值保留 HTML title 与 API raw 字段。

## 8. Final 名称实现方式

- `scan_repositories.security_names(security_ids)`：一次 `IN` 批量查 `SecurityModel`（name/market/code），返回 `{security_id: {...}}`
- 看板端点将 **Top30 + 轨迹行合并一次调用**（§38-§39 禁 N+1；测试断言 `names_calls == 1`）
- 名称缺失 → "名称暂缺"（§59；API 侧 name 可为 null，不造数据）
- scan top API 同步附 `name/market/stage_label`（§83）

## 9. Why Not / Trace 新 UX

- 新参数 `?trace=<code>` 查任意股票（漏斗区搜索表单"查询为什么没入选"，§44）；`whynot=` 保留兼容，旧链接不失效（§43）
- 标题 `筛选轨迹 · <股票名称>（<代码>）`（§46）；最终结果行："已进入最终候选 Top30" / "未进入最终候选 Top30"
- 列改为 阶段 | 状态 | 本层分数 | 本层排名 | 说明（§54）：通过行→STAGE_PASS_NOTES、淘汰行→DROP_REASON_LABELS、support_not_broken→正向说明；状态徽章 通过/已淘汰/已入选（DEAD 弃用）
- 扫描无记录 → 可读解释（未进扫描范围或代码有误），非空白
- 机器评分口径文案（§56）："分数用于候选之间排序，不代表预测涨幅，也不等于买入建议。"
- 链接文案："Why Not / 轨迹" → "查看筛选轨迹"

## 10. Ajax 表格局部刷新实现

- 端点 `GET /v3/dashboard/features-fragment`：只返回 `#feature-table-summary` + `#feature-table-result` 两个节点（无 doctype/html），与整页同一校验（非法 422）；无数据返回可读空态
- 共享查询 `_load_feature_table_data`（§66）+ 共享渲染 `_feature_table_nodes`（§67）：整页与 fragment 同一份数据口径与 DOM
- vanilla JS（无前端框架，§77 渐进增强）：
  - fetch + `X-Requested-With: fetch` + `cache: no-store`
  - `AbortController` 防重复提交（§73）
  - 按钮 disabled + "加载中…"（§71）
  - 成功：DOMParser 整节点替换 summary/result，form 不替换；`history.replaceState` 更新 URL（可分享可回退，§70）；恢复滚动位置
  - 失败：显示"加载失败，请稍后重试"，**保留原表格**（§72）
  - JS 禁用：原生 GET 提交 `/v3/dashboard` 兜底（SSR 完整可用）

## 11. 测试结果

| 套件 | 结果 |
|---|---|
| tests/v3/test_scheduler.py（A 组专项） | 53 passed（harness 重写 + §96-§100 全覆盖：daily_slots env 链、resolve_next_slot、CLI --group、非交易日、catch-up 追平、candidate 失败不掩盖、EOD 前置检查、LOCKED→PARTIAL） |
| tests/v3/test_v3_dashboard.py（C/D 组专项） | 19 passed（§86-§94：三区块中文、Regime 分节+observed 双译名+万亿格式化、Final 名称+names lookup=1 次 N+1 守卫、trace= 中文化+兼容 whynot=、无记录提示、漏斗表单隐藏域、fragment 无 doctype、参数透传、422、空态、渐进增强锚点） |
| **全量回归（每 commit 后）** | A 后 **919 passed / 106 skipped**；C 后 **926 passed / 106 skipped**；D 后 **931 passed / 106 skipped**（0 failed） |
| docker compose config | 本机无 docker daemon（生产 2C 机），配置项已在 B 组核对过 compose 文件本身；留生产验证 |

## 12. 算法回归结果

- 全量回归三轮全绿（919→926→931，只增不改），无任何既有用例失败
- Scheduler 改动只动 orchestration/时点/catch-up，不触碰 experts/Pareto/Machine/Deep/Final30 任何算法、阈值、权重（§102）
- 看板/Scan API 改动只加展示层 label 字段与 HTML，raw 字段与 DB 写入路径不变
- **Final30 语义零变化**

## 13. 生产未验证项（接手电脑按序）

1. **部署**：生产 r11 镜像未更新——重建 worker 镜像 + `.env` 增四个时点 env（`V3_SCHEDULE_AT` 可留可删，兜底不坏）
2. **四时点观察（§107）**：至少一个交易日，期望 15:35 market-data/index-benchmarks、18:20 evidence-increment、18:45 features/full-recall/candidate-scan、20:30 maintenance 各组 JobRun 落库
3. **SSH 探针（§108）**：15:35 Data Prep 时段每 10 秒探 22 端口，目标 0 失败；仍失败 → 降 `V3_PHASE2_CONCURRENCY`（不要合回 18:45）
4. **Final 页面（§109）**：Final30 全部有名称；点"查看筛选轨迹"看到中文阶段/通过淘汰/排名/原因
5. **中文验收（§110）**：普通用户页面不应再看到 observed/DEAD/Why Not/Universe 等英文内部代码作为主展示文字
6. **应用按钮（§111）**：筛选后只刷表格、不回顶部、Final30 不重载、URL 更新、失败保留旧表
7. **run_once 演练**：`--once` 与 `--group eod-scan` 各跑一次，核对报告结构与 catch-up 幂等

---

## 附：交接状态

- 详见 `docs/v3_r11_production_handoff_20260910.md` 顶部"2026-09-11 增补"章节
- 本机工作已全部完成并 push（head `cc1de9a`）；设计 §115 第 11 条"本地完成后 STOP"已遵守，无顺手扩展
