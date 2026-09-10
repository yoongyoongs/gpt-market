# V3 r11 生产部署交接与验收清单（2026-09-10 晚）

> 交接对象：下一台电脑/下一会话继续 §8 补验。
> 本文档只记录状态、位置、命令，不含任何密钥。
> 部署与资源治理改造报告见 `v3_run_once_resource_optimization_report.md`。

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
