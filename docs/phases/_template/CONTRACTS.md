# Phase X Contracts

> Derived from V3 Architecture Baseline; cannot override it. Conflicts must be reported.

## Frozen Inputs

列出 DTO、Domain Object、Protocol、Repository、API、Migration 和版本要求。

## Outputs

列出本 Phase 新增或扩展的正式 Contract。

## Invariants

- 时点、Hash、Coverage、UNKNOWN、append-only 和事务不变量。

## Error Semantics

- `UNKNOWN`、`UNAVAILABLE`、`STALE`、`CONFLICT`、404/409/422 等语义。

## Versioning

- Schema、Builder、Feature、Prompt、Strategy 或 Migration 版本。

## Frozen Boundary

任何变更必须先报告 `DESIGN_CHANGE_REQUIRED` 的跨模块 Contract。
