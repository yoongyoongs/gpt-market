# Phase 3 全市场特征实施记录

> 日期：2026-08-30（Asia/Shanghai）  
> 分支：`codex/phase3-full-market-features`  
> 状态：5,551 只全市场技术验收完成；生产 V3 仍关闭

## 1. 实施范围

本阶段按《架构设计实施稿》实现事实层能力，不修改 V1/V2 评分，不增加自动交易：

- `0003_full_market_features` Migration；
- 不可变 `feature_runs`、`security_features`、`market_regime_snapshots`；
- 绑定 Universe Snapshot、QFQ DAY Revision、Factor Revision 和输入 Hash 的可重放 Feature Run；
- 收益、位置、均线、斜率、ATR、波动率、区间、突破/回踩、成交额、量比、量能扩张、相对强度和质量字段；
- 白名单字段选择、过滤、排序和稳定 Cursor；
- 事实型 Market Regime 与显式 UNKNOWN；
- 独立 Feature Job 和 V3 READ API。

## 2. 计算口径

- 所有全市场特征只读取同一条截至 `as_of` 已发布的 QFQ DAY Revision，不混用其他周期或复权口径。
- `return_Nd` 需要至少 `N+1` 根完整日 K；数据不足返回 `null` 并写入 `missing_fields`。
- `position_Nd` 使用窗口内最高价/最低价；ATR 使用 14 日 True Range；20 日波动率按日收益标准差年化（250 交易日）。
- 突破使用当前收盘价与此前 20 根最高价比较；回踩要求 MA20 上行且收盘价位于 MA20 上下 3% 内。
- 行业或指数基准未绑定时，相对强度保持 `null`，不得编造。
- Regime 中缺少指数、板块、涨跌停或市值风格事实时保存 `UNKNOWN + reason`，不输出总分或买卖硬开关。

## 3. 发布与重放

Feature 先按批读取和计算，最后在单事务内写入 PUBLISHED Run、全部 Security Feature 和对应 Regime。失败不会留下可查询的半成品。Run 内容 Hash 由 Universe Snapshot、`as_of`、Feature Version、Revision Set Hash、覆盖和错误摘要决定；相同输入重复运行返回既有 Run。

每条 Security Feature 保存 `series_revision_id`、`factor_revision_id`、`input_hash`、`content_hash`、质量、缺失字段、stale 和来源错误。三张 Phase 3 表均由数据库触发器禁止 UPDATE/DELETE。

## 4. 查询接口

- `GET /api/v3/universe/features`；
- 兼容别名：`GET /api/v3/universe/query`；
- `GET /api/v3/market-regime`。

Universe Query 最大页长 200。字段、排序键和数值过滤只允许类型化白名单；Cursor 绑定排序字段，并使用“排序值 + security_id”保证稳定翻页。V3 Feature Flag 关闭时明确返回 HTTP 503。

## 5. 全市场验收

- 空 PostgreSQL 17 从 base 升级到 `0003_full_market_features (head)` 成功；
- 2 只证券、每只 260 根 QFQ 日 K 的真实数据库链路通过；
- 原子发布、重复输入幂等、数值降序 Cursor、Regime 读取和不可变触发器通过；
- V3 全部测试（启用真实 PostgreSQL）：`81 passed`；
- 全项目测试（启用真实 PostgreSQL）：`154 passed, 5 skipped`；
- 全新环境发现并补齐 SQLAlchemy async 所需的显式 `greenlet` 依赖。
- 首次全市场冷启动预验收发现并修复两项数据层缺陷：非整比复权因子派生 HFQ 时先规范化 6 位价格再计算 Hash；空库官方源失败时，超过 5,700 成员的 Secondary 污染集合会被明确拒绝。
- 服务器隔离 PostgreSQL 17 中完成 5,551 只输入回填：日/周 RAW、QFQ、HFQ 各 5,551，完整月线各 5,534；共 6,169,716 根 Bar、1,636,526 条 Factor，回填失败 0。
- 官方 SZSE 源在验收窗口持续断连，系统没有把东方财富 5,905 条污染集合发布为 PRIMARY；验收库使用上一版官方 5,551 集合重建为显式 stale LKG，来源事实未伪装为本次新鲜官方快照。
- 正式 Feature Run `4e8749ff-3652-4ef3-8266-01318f0621f6`：5,551/5,551、失败 0、覆盖 100%，运行 180.679 秒，峰值内存约 291 MiB；发布后 5,551 条 Security Feature 和 1 条 Regime 一次可见。
- 使用相同 Universe、`as_of`、版本和 Revision Set 全市场重跑后返回原 Feature Run；数据库仍为 1 个 Run、5,551 条 Feature，幂等发布无重复。
- 收益字段非空数：3/5/10/20/60/120/250 日依次为 5,550 / 5,549 / 5,543 / 5,534 / 5,511 / 5,469 / 5,397；低覆盖样本对应真实上市历史不足，最短样本只有 1 根 Bar。
- 特征覆盖率分母已从错误的 35 修正为实际 28 个字段，并增加回归断言。成熟样本覆盖率为 25/28：成交额、指数相对强度和行业相对强度均显式缺失。
- 查询 Benchmark 每场景 30 次、页长 100：代码排序 P50/P95 9.496/14.688 ms，20 日收益降序 8.843/9.019 ms，沪市 60 日位置降序 6.982/7.187 ms，20 日收益区间 6.378/6.511 ms，覆盖率升序 8.553/8.984 ms；均返回正确总数和稳定下一页 Cursor。
- 隔离验收库体积 1,437 MB；本地完整回归 `144 passed, 17 skipped`，Ruff 通过；生产 `market-mcp` 在验收期间保持 healthy，未挂载验收数据库或启用 V3。

## 6. 显式缺口与边界

- 本轮实际日 K 主源为腾讯 5,122 只、Sina fallback 429 只。两者不提供可靠历史成交额，系统将 `amount` 保持 `null`，Regime Turnover 保存 `observed=0`，禁止用 `volume * close` 估算冒充真实成交额。
- 指数基准与行业分类尚未绑定到本 Feature Run，因此两项相对强度及对应 Regime 维度保持 `null/UNKNOWN + reason`。这不会阻塞收益、位置、趋势和量能事实使用，但后续接入基准 Revision 时必须重跑并形成新版本。
- 当前结论只代表 Phase 3 技术验收完成，不代表 Phase 4 至 Phase 11、最终生产 Migration 或 V3 正式部署已经完成。
