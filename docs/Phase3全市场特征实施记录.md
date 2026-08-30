# Phase 3 全市场特征实施记录

> 日期：2026-08-30（Asia/Shanghai）  
> 分支：`codex/phase3-full-market-features`  
> 状态：核心实现与 PostgreSQL 集成验收完成；5,551 只全市场性能验收待执行

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

## 5. 当前验收

- 空 PostgreSQL 17 从 base 升级到 `0003_full_market_features (head)` 成功；
- 2 只证券、每只 260 根 QFQ 日 K 的真实数据库链路通过；
- 原子发布、重复输入幂等、数值降序 Cursor、Regime 读取和不可变触发器通过；
- V3 全部测试（启用真实 PostgreSQL）：`81 passed`；
- 全项目测试（启用真实 PostgreSQL）：`154 passed, 5 skipped`；
- 全新环境发现并补齐 SQLAlchemy async 所需的显式 `greenlet` 依赖。

## 6. 待完成验收

核心实现不等于全市场性能验收。Phase 3 标记最终完成前仍需：

1. 在完整 5,551 Universe、完整 QFQ/HFQ 输入库执行正式 Feature Run；
2. 核对 3/5/10/20/60/120/250 日字段覆盖与上市不足样本的缺失原因；
3. 验证 5,551 行查询的过滤、字段选择、正反排序、连续 Cursor 和 Explain；
4. 记录总耗时、峰值内存、数据库体积、查询 P50/P95 和最终覆盖率；
5. 完成后更新本记录、需求追踪和工作状态，再建立 Phase 3 标签。
