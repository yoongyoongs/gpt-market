# V3 模块局部规则

本目录同时受根 `AGENTS.md` 和 `docs/ARCHITECTURE_GUARDRAILS.md` 约束。

- V3 通过独立 Router、Schema、Migration、Worker 和 Feature Flag 与 Legacy 隔离；不要为 V3 任务侵入 `app/services`、V1/V2 Scanner、Provider 或评分逻辑。
- 依赖方向保持 Interface → Application → Domain/Protocol → Infrastructure；Domain 不依赖 FastAPI、SQLAlchemy 或具体 Provider/模型 SDK。
- Application Service 使用显式 DTO 和 Protocol；网络 I/O、外部计算在事务外完成，事务只负责短时原子发布。
- PostgreSQL 事实优先 append-only；Correction、Revision、Replacement 或 supersedes 链替代覆盖历史。
- 所有时间必须带时区；`known_at` 表示系统最早可知时间，Replay 只能读取 `known_at <= replay_as_of` 的记录。
- 缺失值保持 `null/UNKNOWN` 并携带 coverage/stale/conflict/error；禁止用 0 或推测值补齐。
- 新 API 只返回机器可读 JSON；HTML 不是 V3 ChatGPT 主接口。
- 当前 Phase 的允许修改范围、冻结 Contract 和测试门槛以 `docs/phases/<phase>/` 为准。
