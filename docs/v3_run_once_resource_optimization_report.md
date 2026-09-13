# V3 run_once 资源隔离与 SSH 稳定性优化——实施报告

> 日期：2026-09-10
> 方案：`V3_run_once资源隔离与SSH稳定性优化_详细设计方案.md`
> 分支：`fix/v3-candidate-engine-round2-1`
>
> **PRODUCTION_SSH_TEST = PENDING**
>
> 本报告只覆盖本地代码改造与测试。生产部署与 SSH SLO 验收未做，
> 本地不能、也不会伪造生产验证结果。

---

## 1. Commit

| 项 | 值 |
|---|---|
| 修改前 commit | `7c191ae`（tag `pre-run-once-resource-opt`） |
| 修改后 commit | `26a3fa9` |
| push 状态 | 未 push（与补验报告一并推送） |

## 2. 修改文件

| 文件 | 改动 |
|---|---|
| `scripts/v3_phase2_market_job.py` | P0-01 并发默认 16→4；P0-06 新增 `--yield-every/--yield-seconds` 并传入 execute |
| `app/v3/application/scan_universe.py` | P0-02 `_DEFAULT_MINUTE60_CONCURRENCY` 12→4（env 可覆盖不变） |
| `scripts/v3_scheduler.py` | P0-03 candidate Deep `periods=("60m",)`；P1-02 candidate metrics 带 provider_health；P1-03 `SchedulerBundle` + run_once try/finally 收口 + index 腾讯 fallback 共享实例；P1-04 三处"并行"注释修正 |
| `app/v3/application/backfill_daily_bars.py` | P0-06 execute 增 `yield_every=100/yield_seconds=0.5`（含合法性校验），批间让步 |
| `app/v3/jobs/orchestrator.py` | P1-01 `_run_with_retry` 每 attempt 自动补 `wall_seconds` + `resource_before/after`（成功、失败、fallback 路径均覆盖） |
| `app/v3/operations/resource_snapshot.py`（新增） | P1-01 轻量资源快照（/proc + cgroup v1/v2），零依赖 |
| `app/v3/operations/__init__.py`（新增） | 包标记 |
| `docker-compose.yml` | P0-04 worker `cpus/mem_limit/pids_limit`；P0-05 worker env 覆盖 `V3_DATABASE_POOL_SIZE/MAX_OVERFLOW` |
| `.env.example` | 新增资源配置段（P0-01/02/04/05/06 全量默认值+注释） |
| `tests/v3/test_run_once_resource_governance.py`（新增） | 专项测试 13 项 |
| `tests/v3/test_scheduler.py` | 2 处 fake 改返回 `SchedulerBundle`（run_once 经 bundle 收口） |

## 3. 配置项（.env.example 同步）

```env
V3_PHASE2_CONCURRENCY=4            # P0-01（原 16）
V3_PHASE2_YIELD_EVERY=100          # P0-06
V3_PHASE2_YIELD_SECONDS=0.5        # P0-06
V3_SCAN_MINUTE60_CONCURRENCY=4     # P0-02（原 12）
V3_WORKER_DB_POOL_SIZE=3           # P0-05（worker 专属）
V3_WORKER_DB_MAX_OVERFLOW=1        # P0-05（worker 专属）
V3_WORKER_CPU_LIMIT=2.0            # P0-04（示例值，生产按规格覆盖）
V3_WORKER_MEMORY_LIMIT=3g          # P0-04（示例值）
V3_WORKER_PIDS_LIMIT=512           # P0-04（示例值）
```

## 4. 关键设计落点

### P1-03 SchedulerBundle（生命周期收口）
- `build_orchestrators()` 返回 `SchedulerBundle(main, maintenance, database, closeables, candidate_scan_deep_service)`；
- 保留 `__iter__/__getitem__` 兼容旧式三元组解构/索引（`test_rt10` 等既有测试零改动）；
- `close()` 幂等（`_closed` 门闩）、逐项关闭、单个失败吞异常不外泄；
- **修复原缺陷**：run_once 只在成功路径 `database.close()`，异常路径 Provider/DB 全泄漏——现 try/finally 全路径收口；
- 关闭顺序：candidate PM → index tencent → eastmoney → database；
- index-benchmarks 的腾讯 fallback 由 handler 内每次新建（从不 close）提为 build 级共享实例，与 candidate 腾讯实例分离（§19 不纠缠）。

### P1-01 资源可观测
- `resource_snapshot.snapshot()`：loadavg、VmRSS、cgroup v2/v1 memory.current/max、cpu.stat；任何失败降级 `null + reasons[]`，绝不抛错、绝不影响 Job 成败；
- 每 Job metrics 自动带 `wall_seconds / resource_before / resource_after`——下一次 SSH 失联可从 `orchestrator_job_runs.metrics` 直接读出事发时 RSS/load 曲线，不再猜。

### P0-03 只抓 60m
- Candidate Engine 只消费 `periods["60m"]`；scan 专用 DeepMarketDataService 显式 `periods=("60m",)`；
- 盘中 Deep（Action/Watchlist/Portfolio）保持默认 `("5m","15m","60m")` 不受影响；
- 选股结果不变：原本就只读 60m。

## 5. 测试结果

### 专项测试（新增 13 项，全过）
| 方案章节 | 测试 | 断言 |
|---|---|---|
| §33 Test1 | `test_backfill_twenty_targets_concurrency_never_exceeds_limit` | 20 股 concurrency=4，active 恒 ≤4 |
| §33 Test2 | `test_backfill_yields_after_every_n_completed` | yield_every=8/20 股 → sleep 恰 2 次；=0 关闭；负参 ValueError |
| §33 Test3 | `test_candidate_deep_service_requests_only_60m` + `test_scheduler_bundle_candidate_deep_periods_is_60m_only` | 只请求 60m，绝无 5m/15m；scheduler 装配 `_periods==("60m",)` |
| §33 Test4 | `test_minute60_fetch_concurrency_never_exceeds_limit` | 12 股 limit=2，active 恒 ≤2 |
| §35 | snapshot 降级/解析/真实调用 3 项 | 缺文件 → available=false+reason；fake 值正确解析；现场调用不抛错 |
| §36 | bundle close 3 项 + run_once 异常路径 1 项 | close_count==1（幂等）；吞单点 close 异常；maintenance 抛错 Provider 仍被收口 |
| 配置固化 | parser 默认值 2 项 | concurrency=4 / yield 100/0.5 |

### 全量回归
```text
tests/v3: 823 passed, 101 skipped（基线 810 passed + 13 专项）
算法 fixture 结果：零变化（本报告修改集不含任何 expert/pareto/machine/
deep/outcome 逻辑文件，全量绿即回归证明）
```

### 算法零改动证明
修改文件清单中不含 `app/v3/candidate_engine/**`、`app/v3/domain/features.py`、
任何专家/Pareto/Machine/Deep 评分模块；`scan_universe.py` 仅改默认并发常量
（运行时语义 = 信号量上限数值，不改变任何选股输入输出）。

## 6. Docker 配置验证

本地无 Docker daemon —— 按方案 §34 降级为静态验证：
- YAML 解析通过，`v3-market-worker` 具备 `cpus/mem_limit/pids_limit` 与
  DB pool 覆盖键，变量默认值渲染正确；
- **integration requirement**：部署机需执行 `docker compose config` 复核
  （或直接 docker run 参数注入，见 §7）。

## 7. 生产部署注意（未执行，待用户确认）

1. 生产 worker 为 `docker run` 直建（非 compose）。资源笼子对应参数：
   ```bash
   --cpus 1.0 --memory 1.2g --pids-limit 512
   # 2C/2G 宿主机建议（方案 §9 2C/4G 档收紧）：
   #   Worker CPU 0.8~1.0 / RAM 1.2~1.5G
   #   V3_PHASE2_CONCURRENCY=3、V3_SCAN_MINUTE60_CONCURRENCY=3
   #   V3_WORKER_DB_POOL_SIZE=3 / OVERFLOW=1
   ```
2. 部署前先实测：`nproc && free -h && docker stats --no-stream`。
3. **PRODUCTION_SSH_TEST = PENDING**：run_once 全程 10 秒 SSH 探针
   （目标 0 失败）+ `/health` 15 秒探针 + `dmesg -T | grep -i oom`。
4. 第二阶段（日 K 增量化）独立立项，不与本轮混做。

## 8. 方案 §50 Checklist（本地部分）

**Correctness**：✅ 不改 Universe/Safety/8 Experts/RRF/Pareto/Machine/Deep
Score/Final30；✅ 不降低全市场覆盖（5560 目标数不变，仅降并发）。

**Resource**：✅ P0-01/02 默认 4；✅ candidate 只请求 60m；✅ Worker CPU/
Memory Limit 可配置；✅ Worker DB pool 单独限制；✅ soft throttle 可配置；
✅ Provider 显式关闭（SchedulerBundle）。

**Observability**：✅ 每 Job wall_seconds；✅ resource_before/after；
✅ Candidate provider health；✅ 资源采集失败不阻断 Job。

**Test**：✅ concurrency/throttle/only-60m/provider close 测试；✅ candidate
算法回归（全量零失败）；✅ docker compose 静态验证（真 config 待部署机）。

**Production**：❌ SSH 探针 / ❌ /health / ❌ 无 OOM / ❌ 覆盖不下降 /
❌ run_once 完整结束 —— **全部 PENDING，待部署后验收**。
