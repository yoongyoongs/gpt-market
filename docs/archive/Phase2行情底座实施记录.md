# V3 Phase 2 行情底座实施记录

> 日期：2026-08-30（Asia/Shanghai）  
> 状态：Phase 2 技术验收完成；生产 V3 仍保持关闭
> 分支：`codex/phase2-market-data-foundation`

## 1. 本轮实现

- 建立日 K `RAW/QFQ` 成对抓取和 Provider 顺序 fallback；
- 同一 Provider 能取得 RAW 与 QFQ 时，按交易日通过 `QFQ close / RAW close` 生成不可变 Factor Revision；
- 只有 QFQ 时不伪造 RAW/Factor，Revision 明确标记 `LIMITED` 及原因；
- 非空但陈旧的 K 线通过 `minimum_last_bar_date` 门禁拒绝，不把旧数据当成功；
- 当前未收盘日 Bar 单独标记 `partial`，不进入 `PUBLISHED` Revision；
- 周/月 K 由同口径日 K 本地聚合，周期完整性由注入的交易日历判断；
- Factor、RAW/QFQ、周/月 Revision 和 Bars 通过同一 Unit of Work 发布；
- V3 `market_bars.amount` 改为可空。腾讯和新浪未提供历史成交额时保存 `null/UNKNOWN`，不再写入伪造的 `0`。
- 历史 Backfill 固定绑定不可变 Universe Snapshot，并按稳定目标序号持久化每只证券结果；
- 中断后只重试失败或尚未处理的证券，已成功证券通过运行游标和已发布覆盖检查双重跳过；
- 每个受控并发批次完成后 checkpoint，运行计数由连续高水位和失败项重新计算，避免重试造成重复累计；
- `row_version` 乐观锁拒绝同一运行的并发写入，网络请求保持在数据库事务之外。
- Backfill 支持 `1..32` 的显式受控并发，Job 默认 16；按并发窗口抓取/发布，由单协调器串行 checkpoint；
- 游标只保存 `next_index` 连续高水位和当前失败项，成功目标不反复写入 JSONB；成功全量时游标为常量大小，避免 O(全市场) JSON 重写和 WAL 放大；
- 东方财富历史 K 连续失败 3 次后全局熔断 300 秒，冷却后真实探测恢复；熔断期间立即进入腾讯/Sina，不为每只证券重复制造无效请求；
- 严格交易日历采用 `exchange_calendars:XSHG`，运行时记录包版本、日历代码及覆盖起止日期；
- 日历覆盖外日期显式抛出 `TradingCalendarOutOfRange`，不以普通工作日规则静默替代交易日。

## 2. Universe Provider 实测

可复用探针：`scripts/v3_phase2_universe_probe.py`。服务器真实请求上交所、深交所、北交所官方接口：

| 市场 | 实际证券数 |
|---|---:|
| 上海 | 2,315 |
| 深圳 | 2,897 |
| 北京 | 339 |
| 合计 | 5,551 |

上游声明 5,551，解析 5,551，唯一代码 5,551，覆盖率 100%，墙钟 5.309 秒；北交所 339 只全部为当前 `920xxx` 代码。任一交易所分页不完整都会拒绝整份 Secondary 快照，不以沪深数据冒充全 A 股。

集合对账进一步发现东方财富列表 5,905 是官方 5,551 的超集，多出 354 条：BJ 12、SH 151、SZ 191；样本包含定向可转债、转板旧代码及大量退市证券，官方集合没有 `official_only` 缺口。因此 Job 调整为三交易所官方源 PRIMARY、东方财富 SECONDARY，并增加相对 LKG 最大 5% 异常扩张门禁；数量更多不再自动等于覆盖更好。

最终生产组合 Provider 只使用三交易所决定成员资格，再以东方财富补充停牌、ST 和退市风险状态；东方财富额外 354 条不会进入 Universe。最终快照为 5,551 只，其中 4 只明确标记停牌。

## 3. Bar Provider 实测

探针：`scripts/v3_phase2_bar_probe.py`，300 个交易日，4 并发，最后 Bar 不得早于当前日期前 10 个自然日。

| 指标 | 结果 |
|---|---:|
| 样本数 | 20 |
| 成功数 / 成功率 | 20 / 100% |
| 东方财富主源 | 0 |
| 腾讯接管 | 14 |
| 新浪接管 | 6 |
| RAW 可用 | 20 |
| Factor 可用 | 20 |
| FULL / LIMITED | 20 / 0 |
| 每只平均耗时 | 1.057 秒 |
| 4 并发墙钟耗时 | 5.872 秒 |
| 每只有效 Bar | 300 |
| 最后 Bar 日期 | 全部为 2026-08-28 |

样本覆盖沪市主板、科创板、深市主板、创业板和北交所。北交所使用 2025 年全面切换后的 `920xxx` 当前证券代码；旧 `4/8` 代码只返回切换前历史，已由陈旧门禁拒绝。

东方财富三个历史域名在服务器上的实际行为为：`push2his` 与 `push2` 主动断开，`push2delay` 返回 `rc=0` 但 `klines=[]`。系统完整保留该错误并继续走腾讯/Sina，不把 HTTP 成功或 `rc=0` 当成数据成功。

## 4. 数据正确性

- 腾讯日 K 可取得 RAW/QFQ，但历史响应无成交额，V3 保存 `amount=null`；
- 腾讯对部分科创板和北交所只返回 RAW 或空 QFQ，系统继续 fallback；
- 新浪适配器读取日 K 与独立 QFQ Factor，按生效日选择因子并推导 QFQ；
- RAW/QFQ 日期未完全对齐时，QFQ Revision 降为 `LIMITED`，不得绑定不完整 Factor；
- Provider 返回错误代码、错误周期、错误复权类型、空序列、重复/乱序或陈旧序列时均拒绝。

## 5. 持久化验收

服务器隔离 PostgreSQL 17 从空库执行 `0001 -> 0002` 后通过：

- `amount=NULL` 的正式 Market Bar 可写入；
- Factor Revision 先于绑定它的 QFQ Revision 生效；
- 当前 Bundle 发布写入 1 个 Factor Revision、RAW/QFQ/HFQ 3 个 Series Revision 和 9 条 Market Bar；
- 原 Bundle 再发布时 Factor/Series 均为零新增；
- Series 任一写入失败时 Factor 与 Bars 整组回滚；
- 已发布 Bar 仍受不可变触发器保护。
- 真实 Universe Snapshot 中两只证券完成 Backfill 后，运行记录为 `COMPLETED / 2 / 2 / 0`；
- 两只证券的 RAW/QFQ/HFQ 日、周、月共生成 18 个 Series Revision；同一 `run_id` 再执行时直接返回，序列数保持 18；
- 单元故障注入覆盖“首轮部分失败后只重试失败目标”和“发布成功但 checkpoint 前崩溃后无重复恢复”；
- PostgreSQL 端到端测试：`test_backfill_run_reads_universe_checkpoints_and_replays_without_duplicates`，服务器隔离 PostgreSQL 17 实测通过。
- 6 目标、并发上限 3 的故障注入测试实测最大并发为 3，并产生初始、两个批次和终态共 4 次 checkpoint；
- 真实 PostgreSQL 17 以默认并发重跑 2 目标发布与幂等重放通过。
- Universe 成员在 Domain 按 `(market, code)` 规范排序后再哈希，Provider 返回顺序不再影响集合事实；
- Canonical Hash 将 aware datetime 统一转换为 UTC；Universe coverage、Factor、OHLC、成交额分别规范到 DDL 的 5/12/6/4 位小数，保证 PostgreSQL 往返可复算；
- `scripts/v3_phase2_universe_hash_audit.py` 在 5,551 成员快照上验证存储 Hash 与数据库重建 Hash 完全一致。
- Factor、RAW/QFQ、周/月增量 Revision 自动指向同证券同周期上一版；同一 Bundle 重放不会形成自指或重复链，真实 PostgreSQL 已验证 `supersedes_revision_id`。

## 6. 交易日历验收

- 依赖基线：`exchange-calendars>=4.13.2,<5`，Calendar Code 为 `XSHG`；
- 当前 4.13.2 数据覆盖 `2006-08-30` 至 `2026-12-31`，超过边界必须先升级并重新验收；
- 对照上交所《2026 年春节休市安排》，验证 2026-02-23 休市、2026-02-24 开市；
- 2026-02-24 至 2026-02-27 节后短周只含 4 个 Session，周五 15:10 后可发布完整周 Bar，不要求伪造第 5 根；
- 本地严格日历及聚合测试 8 项通过，服务器 Python 3.12 镜像复测 6 项通过。

## 7. 全市场 Job 验收

入口：`scripts/v3_phase2_market_job.py`。

- 支持 `universe`、`backfill`、`corporate-actions`、`all` 四种模式；数据库地址必填且错误输出会脱敏；
- 支持 `run_id` 恢复、`stop_after` 分段、300 日限制、1..32 并发和 JSON/原子文件报告；
- 最近完整交易日由严格日历计算，2026-08-30 运行时门禁日期为 2026-08-28；
- 服务器新建 `gpt_market_phase2_job3`，从空库 Migration `0001 -> 0002` 后执行 `all --stop-after 20`；
- Universe PRIMARY 为官方 5,551（BJ 339、SH 2,315、SZ 2,897），前 20 只行情 `20/20`，失败 0，墙钟 24.772 秒；
- 使用同一 `run_id` 恢复后从 `next_index=20` 推进到 40，新增 20 只 `20/20`，失败 0，墙钟 9.708 秒；
- 运行终态若为 `PARTIAL/FAILED`，CLI 返回非零，供 Scheduler/容器健康检查报警。
- 最终增强快照运行 `7222f265-1ad2-4637-830e-ba5eddc5e648` 完成 `5,551/5,551`，失败 0，墙钟 1,362.295 秒；4 只停牌证券允许最后交易日早于 2026-08-28，但仍要求真实非空历史。
- 最新 QFQ 日线 Provider 分布为腾讯 5,122、Sina 429；RAW、Factor、`FULL` 精度均为 5,551/5,551。
- 最新 Revision Set 含日线 5,551、周线 5,551；月线 5,534，剩余 17 只当月新股没有完整自然月，系统不发布伪完整月 Bar。
- 最新日线中 5,375 只保留 300 根；其余为真实上市历史不足 300 根的新股，全部非停牌证券最后交易日为 2026-08-28。
- 验收中发现 PostgreSQL 返回上海零点后按 UTC `.date()` 会落到前一天，导致已覆盖证券被误判未覆盖。修复为 `astimezone(Asia/Shanghai).date()` 并增加真实 PostgreSQL 边界测试后，新建全市场任务 `5,551/5,551` 全部覆盖短路，墙钟 12.966 秒且没有再次调用行情 Provider。

## 8. Corporate Action 验收

- `EastmoneyCorporateActionProvider` 按除权日范围分页读取 `RPT_SHAREBONUS_DET`，标准化公告日、登记日、除权日、现金、送股、转增、方案进度和原始引用；
- 市场事实与账户调整严格分离，不直接改变持仓；非“实施分配”方案标记 `effective_date_status=PLANNED`，不得冒充已实施；
- 2025-01-01 起真实抓取 8,793 个逻辑事件：现金 8,033、现金加送转 700、送转 60；8,792 条已实施，1 条为已公告计划；
- 首次规范化发布 8,793 条；同一事实再次运行 8,793 条全部 `unchanged`、新增 0，墙钟 7.877 秒；
- 事实内容变化时追加新行并绑定 `supersedes_action_id`，语义内容 Hash 排除抓取时间等运行元数据，真实 PostgreSQL 已验证幂等和更正链。

## 9. Worker 与调度

- `scripts/v3_phase2_scheduler.py` 按 `Asia/Shanghai` 每日定时执行完整 Job，支持 `--once` 验收和原子报告文件；
- PostgreSQL advisory lock 防止 Scheduler、人工任务或容器重启重叠执行；真实双连接竞争与释放后重获已通过；
- Compose Worker 位于独立 `v3-worker` Profile，启动前执行 `alembic upgrade head`，不会随现有 V1/V2 API 或仅启用 `v3` PostgreSQL Profile 自动启动；
- 并发、历史长度、执行时刻、锁 Key 和报告路径均通过 `V3_PHASE2_*` 环境变量配置；
- 服务器 `--once` 端到端执行 Universe → Backfill → Corporate Action：官方增强 Universe 5,551，Backfill 5,551/5,551，公司行动窗口 5,113 条全部 unchanged，整体墙钟 23.627 秒；原子报告文件通过 JSON 解析。持久化挂载目录须预先授予 UID 10001 写权限；
- 当前日历只覆盖至 2026-12-31；2027 年前必须升级依赖并按交易所公告复验，越界时任务会明确失败。

## 10. 验收结论

Phase 2 验收目标“全市场覆盖率不低于 90%、断点续跑、Universe LKG、优先 raw/factor、前复权-only 限制”已满足。实际 Universe 和日/周覆盖率为 100%，月线缺失仅来自尚无完整月的新股，并有明确原因。Phase 2 可以进入 Phase 3；生产 V3 仍须保持 Feature Flag 关闭，待后续 Phase 和最终部署验收完成后统一启用。
