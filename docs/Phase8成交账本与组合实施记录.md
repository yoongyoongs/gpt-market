# Phase 8 成交账本与组合实施记录

> 状态：开发完成，待测试验收

## 已实现

- Account、Manual Trade Draft、人工确认和幂等成交；
- 图片导入保存 Hash、OCR 字段置信度与区域，只生成 Trade/Position Draft；
- Position Draft 人工确认后建立 Opening Position，不反推历史 BUY；
- 不可变 Trade Ledger 及追加式 Reverse/Correction；
- 账户级 Portfolio Adjustment，与市场 Corporate Action 事实分离；
- Reconciliation、Portfolio Snapshot 和版本化软偏好；
- Position Projection 从 Opening + 已确认 Adjustment + Ledger + Correction 重建；
- SELL 确认前锁定 Projection 并拒绝超卖；
- Execution Deviation 永久绑定成交当时的 EntryPlan ID/Version；无计划成交标记 `MANUAL_TRADE_WITHOUT_AI_ENTRY`；
- Phase 8 写入和查询 API。

## 核心边界

- OCR 不创建 Trade 或 Holding；
- 只有人工确认的 Trade/Opening 才影响 Position；
- 市场 Corporate Action 不直接修改账户，只有已确认 Portfolio Adjustment 参与重放；
- Ledger、Correction、Opening、Adjustment、Reconciliation、Snapshot、Preference Version 均追加且不可变；Projection 是可重建投影，不替代事实。

## 验收状态

按当前要求未执行单元、集成、Migration、OCR、并发超卖、重放一致性或性能测试，未部署生产。以上状态为开发完成，不代表通过 Phase 8 Architecture Gate。
