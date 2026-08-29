# V3 Phase 2 行情底座实施记录

> 日期：2026-08-30（Asia/Shanghai）  
> 状态：Phase 2 中间实施记录，不代表 Phase 2 整体验收完成  
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
- 每只证券完成后立即 checkpoint，运行计数由唯一目标结果重新计算，避免重试造成重复累计；
- `row_version` 乐观锁拒绝同一运行的并发写入，网络请求保持在数据库事务之外。

## 2. Provider 实测

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

## 3. 数据正确性

- 腾讯日 K 可取得 RAW/QFQ，但历史响应无成交额，V3 保存 `amount=null`；
- 腾讯对部分科创板和北交所只返回 RAW 或空 QFQ，系统继续 fallback；
- 新浪适配器读取日 K 与独立 QFQ Factor，按生效日选择因子并推导 QFQ；
- RAW/QFQ 日期未完全对齐时，QFQ Revision 降为 `LIMITED`，不得绑定不完整 Factor；
- Provider 返回错误代码、错误周期、错误复权类型、空序列、重复/乱序或陈旧序列时均拒绝。

## 4. 持久化验收

服务器隔离 PostgreSQL 17 从空库执行 `0001 -> 0002` 后通过：

- `amount=NULL` 的正式 Market Bar 可写入；
- Factor Revision 先于绑定它的 QFQ Revision 生效；
- 首次 Bundle 发布写入 1 个 Factor Revision、2 个 Series Revision 和 6 条 Market Bar；
- 原 Bundle 再发布时 Factor/Series 均为零新增；
- Series 任一写入失败时 Factor 与 Bars 整组回滚；
- 已发布 Bar 仍受不可变触发器保护。
- 真实 Universe Snapshot 中两只证券完成 Backfill 后，运行记录为 `COMPLETED / 2 / 2 / 0`；
- RAW/QFQ 的日、周、月共生成 8 个 Series Revision；同一 `run_id` 再执行时直接返回，序列数保持 8；
- 单元故障注入覆盖“首轮部分失败后只重试失败目标”和“发布成功但 checkpoint 前崩溃后无重复恢复”；
- PostgreSQL 端到端测试：`test_backfill_run_reads_universe_checkpoints_and_replays_without_duplicates`，服务器隔离 PostgreSQL 17 实测通过。

## 5. 当前未完成

- 全市场 5,000+ 证券正式跑批及覆盖率/耗时验收；
- 严格交易日历 Provider 及日常增量调度；
- 全市场覆盖率不低于 90% 的正式持久化验收；
- Corporate Action 原始事实 Provider；
- 完整 Revision supersedes 链和批次运行观测；
- Phase 2 Job/Container/部署开关。

因此本记录只证明 Bar 数据层单股链路和 20 只真实样本通过，不把 Phase 2 标记为已完成。
