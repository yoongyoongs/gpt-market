# V3 r11 生产部署交接与验收清单（2026-09-10 晚）

> 交接对象：下一台电脑/下一会话继续 §8 补验。
> 本文档只记录状态、位置、命令，不含任何密钥。
> 部署与资源治理改造报告见 `v3_run_once_resource_optimization_report.md`。

---

## 2026-09-11 增补：调度四时点拆分（Commit A 已完成并 push）

**设计指导**：《V3_生产调度拆分_run_once优化_看板中文化与交互整改_详细设计指导.md》
**本轮 A/B/C/D 四组 commit + 报告已全部完成并 push（head `cc1de9a`）**，
实施报告见 `docs/v3_scheduler_dashboard_usability_refactor_report.md`（§105 13 项）。

| 项 | 状态 |
|---|---|
| Commit A 代码 | **完成并 push**（`9b5debc` scheduler: split daily pipeline into timed groups） |
| Commit A 测试 | 全量回归 **919 passed / 106 skipped**；§96-100 全覆盖（daily_slots env 链、resolve_next_slot、CLI --group、非交易日、catch-up 追平、§99 candidate 失败不掩盖、§11 EOD 前置检查、LOCKED→PARTIAL） |
| Commit B | 核对完成，零缺口（V3_PHASE2_CONCURRENCY=4、minute60 并发 4、candidate 只抓 60m、yield throttle、docker 资源限制、worker DB pool 3+1 均已在 26a3fa9 等落地），无新代码 |
| Commit C | **完成并 push**（`e013b1f` dashboard: add Chinese presentation labels and scan names）——zh_cn_labels 映射层、看板全量中文化、Final30 名称（批量 1 次查询）、trace= 筛选轨迹 UX、scan API label 字段；回归 926 passed |
| Commit D | **完成并 push**（`cc1de9a` dashboard: make feature filters update table in place）——features-fragment 局部刷新 + vanilla JS 渐进增强；回归 **931 passed / 106 skipped** |
| 报告 | `docs/v3_scheduler_dashboard_usability_refactor_report.md`（§105 13 项全） |
| 部署注意 | 生产 r11 镜像**未更新**——四时点调度与看板新功能要生效需重建 worker 镜像 + `.env` 增四 env（见 .env.example）；旧 V3_SCHEDULE_AT 兜底不坏 |

**接手电脑待办（生产验证，按报告 §13 顺序）**：
1. SSH 恢复 → 重建 r11 worker 镜像 + `.env` 增 `V3_DATA_PREP_AT/V3_EVIDENCE_AT/V3_EOD_SCAN_AT/V3_MAINTENANCE_AT`。
2. 至少一个交易日四时点观察（§107 期望 JobRun 时点表）。
3. 15:35 Data Prep 时段 SSH 探针（§108，0 失败目标；仍失败降 V3_PHASE2_CONCURRENCY，不合回 18:45）。
4. Final 页面验收（§109 名称+轨迹）、中文验收（§110）、应用按钮局部刷新验收（§111）。
5. `--once` / `--group eod-scan` 补跑演练 + 18:45 run_once 结果验收（§3）。

---

## 0. 工作总账（整个 V3 候选引擎需求）

### 已完成（本分支 10 commits，全部已 push，head=08eddf9）

| 块 | 内容 | 证明 |
|---|---|---|
| Round2.1 代码 | R2.1-P0-01~06 + P1-01~06 全落地（60m 读取路径、Outcome bars_from、三态语义、Deep #61~120 trace、结构化 RR、candidate-scan/outcome-mature 接入 Scheduler 等） | commits 41ad6c8~2707d22；回归 823 passed / 101 skipped |
| coverage 哈希 bug | FeatureRun Numeric(8,7) 精度对齐（7c191ae）；存量 3 坏哈希只能新 run 顶替（DB immutable 触发器禁 UPDATE） | 同日已确认 |
| run_once 资源治理 | P0-01~06 + P1-01~04（26a3fa9）+ 报告（cc27295）；专项 13 测试 | tests/v3/test_run_once_resource_governance.py |
| 生产部署 | r10（alembic 0019）→ **r11 现役**：资源笼子 + 治理 env + healthcheck 修正 | 交接文档 §1 |
| PG 隔离测试 | gpt_market_test 库 6/6 通过（§H） | 用户授权建库 |
| 生产验收进行中 | 手动 run_once 16:49 启动，SSH 探针 ok=39 fail=0 | 本文档 §3 做完后勾 |

### 未完成（今晚另一台电脑接手，按序）

1. **run_once 结果验收**（§3 命令现成）——回填资源优化报告 §50 Production 项。
2. **§C/§G** 扫描解锁：新 run 顶坏哈希后 run_full_scan 真实扫描 + 同日 already_scanned 幂等。
3. **§E** Deep trace 抽查 2~3 只。
4. **§D** Outcome 三态查询（A/NONE 等 10 月中旬，如实记录）。
5. **§F** orchestrator_job_runs 链核对（candidate-scan V2 Gate 排除=正确语义，如实记录）。
6. **报告回填 + commit + push**（补验报告 C~H + 资源优化报告 Production 项）。
7. **资源调参**：按 run_once 实测 rss/load 决定并发 3→4→6。
8. **明天 18:45 常驻轮**：验证 catchup 追平 + 正常调度。

### 明确不做

- P0-07 保留 checkpoint、P0-08 禁止减股票；§23-25 日 K 增量化=二期独立立项。

---

## 1. 当前状态快照（2026-09-10 17:30 CST 写入）

| 项 | 值 |
|---|---|
| 代码 | 分支 `fix/v3-candidate-engine-round2-1`，head `cc27295`，**已全部 push**（9 commits） |
| 生产 worker | **r11 现役**，资源笼子 `--cpus 1.0 --memory 1.2g --pids-limit 512`，`--restart unless-stopped` |
| r11 治理 env | `V3_PHASE2_CONCURRENCY=3`、`V3_SCAN_MINUTE60_CONCURRENCY=3`、`V3_PHASE2_YIELD_EVERY=100`、`V3_PHASE2_YIELD_SECONDS=0.5`、`V3_DATABASE_POOL_SIZE=3`、`V3_DATABASE_MAX_OVERFLOW=1` |
| healthcheck | r11 已修正为 /proc 探测 scheduler 进程（r10 误配 market-mcp /health 导致永远 unhealthy） |
| 手动 run_once | **进行中**：16:49 CST 启动，`docker exec -d`（PID 随会话变，进程=容器内 `python -m scripts.v3_scheduler --once`），预计 18:50~20:20 完成 |
| run_once 输出 | 宿主机 `/opt/gpt-market/data/run_once_manual_0910.log`（容器内 `/data/run_once_manual_0910.log`），中途 0 字节属正常，结束时输出 JSON |
| SSH 探针 | 本次会话 Monitor 至 17:04 止 ok=39 fail=0；会话结束 Monitor 消失，属会话本地资源 |
| 18:45 常驻轮次 | 今天大概率锁冲突跳过/失败——**预期行为**，明天 catchup 追平（幂等安全） |

## 2. 入口地址

| 用途 | 地址 |
|---|---|
| V3 看板（隧道） | `https://them-fcc-argued-kai.trycloudflare.com/v3/dashboard`（quick tunnel **临时**，重启换址） |
| V3 看板（公网 IP） | `http://106.13.171.166/v3/dashboard` |
| GPT 行情入口 | 隧道 + `/gpt/<GPT_WEB_SECRET>/market`；secret 在 worker 容器 env `GPT_WEB_SECRET`（`docker exec gpt-market-v3-worker sh -c 'echo $GPT_WEB_SECRET'`），nginx 对 `/gpt/` `access_log off` |
| 隧道换址后取新 URL | `journalctl -u gpt-market-tunnel --no-pager \| grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \| tail -1` |

链路：cloudflared quick tunnel → nginx :80 → market-mcp 127.0.0.1:8000。

## 3. run_once 完成后立即验收（§50 Production 项）

```bash
# ① 结果 JSON（各 Job 成败）
ssh root@106.13.171.166 "tail -c 3000 /opt/gpt-market/data/run_once_manual_0910.log"

# ② JobRun 落库 + 首次真实 resource metrics（P1-01 验收核心）
docker exec gpt-market-postgres psql -U gpt_market -d gpt_market -c "
SELECT job_id, status, attempt, started_at, completed_at,
       metrics->>'wall_seconds' AS wall_s,
       metrics->'resource_after'->>'rss_mb' AS rss_after_mb,
       metrics->'resource_after'->>'load1' AS load1_after
FROM v3.orchestrator_job_runs
WHERE known_at > '2026-09-10 08:45+00'
ORDER BY known_at;"
# 注意：本次手动 run 是 08:45+00（16:45 CST）之后；18:45 常驻轮若也落行，用
# idempotency_key / started_at 区分两轮。

# ③ OOM 零容忍
ssh root@106.13.171.166 "dmesg -T | grep -i oom | tail -5"   # 期望无新记录

# ④ 覆盖不下降：backfill 股数
docker exec gpt-market-postgres psql -U gpt_market -d gpt_market -t -c "
SELECT count(DISTINCT security_id) FROM v3.bar_series_revisions
WHERE known_at >= '2026-09-10 08:45+00';"   # 期望 ≈5560 目标量级
```

## 4. §8 补验清单（run_once 通过后按序做）

1. **§C/§G 扫描解锁**：新 run 顶掉 3 个坏哈希 latest（7c191ae coverage 精度 bug 的存量）后 `run_full_scan` 真实全市场扫描应可执行；同日再跑一次验证 already_scanned 幂等（第二次必须跳过，不重复产出）。
2. **§E Deep trace 抽查**：抽 2~3 只 Machine Top120，核对 60m structure/rank/score 落库与 trace 完整。
3. **§D Outcome 三态查询**：如实记录当前分布（PENDING 为主；A/NONE 实例要等 10 月中旬 scan 满 20 交易日，写明即可）。
4. **§F JobRun 链**：`orchestrator_job_runs` 应有 candidate-outcome-mature 等 JobRun；**candidate-scan 在 release mode=V2 下被策略链 Gate 排除=正确语义，如实记录**（用户已确认）。
5. **报告回填 + push**：`v3_run_once_resource_optimization_report.md` §7/§8 PENDING 项改实测值；补验报告 C~H 回填；commit + push。
6. **资源调参**：按 §3② 的 rss_after/load1 峰值决定并发 3→4→6（2G 机，内存是硬约束；load 持续 >1.5 不升 CPU 并发）。

## 5. 后续观察点（非阻塞）

- **明天 18:45 常驻轮**：验证 catchup 追平今天锁冲突跳过的主链 + 新一天正常调度（幂等安全，JobRun 行区分日期）。
- 服务器待清理：`/tmp/build_r11.log`。
- `orchestrator_job_runs` 无 trade_date 列，按 `known_at`/`as_of` 过滤。

## 6. 安全红线（持续有效）

- SSH/PG/token/secret 一律不写入任何文档、commit、issue；命令行临时使用后清理，env-file 用完即删。
- 数据库只读查询；增删改必须用户确认（DB 有 immutable 触发器，V3 记录禁 UPDATE/DELETE）。
- 测试代码用完即删，不提交（`tests/v3` 正式测试除外）。
