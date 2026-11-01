# 运维手册（Runbook）

## 0. 关键端口与凭据

| 组件 | 端口 | 鉴权 |
| --- | --- | --- |
| 标签服务 | `LABEL_PORT`（默认 8081） | 内网，无 token；`/labels/*` 只读/触发 |
| 守门服务 | `GATE_PORT`（默认 8080） | `X-Gate-Token: $GATE_API_TOKEN` |
| Freqtrade | 8080（容器内） | `FREQTRADE_API_USER` / `FREQTRADE_API_PASSWORD` |
| Redis | 6379 | 内网，建议开启 requirepass 并写入 `REDIS_URL` |

> 所有密钥只放 `.env`（已在 `.gitignore`）。任何需要贴到工单/聊天的日志都先确认已被脱敏。

## 1. 日常值班（每班 1 次）

```powershell
curl -s $env:GATE_API_URL/health | ConvertFrom-Json | Format-List
curl -s -H "X-Gate-Token: $env:GATE_API_TOKEN" $env:GATE_API_URL/gate/status | ConvertFrom-Json
curl -s $env:LABEL_SERVICE_URL/health | ConvertFrom-Json
```

判读要点：

* `gate/status.freqtrade.state` 应为 `running`；为 `paused` 说明守护正在拦截（见 §3）；
* `checks` 里三项（label_service / redis / halt）必须全 `ok`；
* `label/health.heartbeat_age_sec < 60`，否则标签服务可能卡死；
* 看板：`aiquant_gate_total{result="missing"}` 斜率、`aiquant_cli_calls_total{result="fail"}`。

## 2. 熔断与恢复

```powershell
# 人工熔断（停止开新仓，写入 system:halt=1）
curl -X POST -H "X-Gate-Token: $env:GATE_API_TOKEN" "$env:GATE_API_URL/gate/pause?reason=manual_drill"

# 解除熔断（先删 halt，再让守护自动恢复；顺序不能反）
redis-cli DEL system:halt
curl -X POST -H "X-Gate-Token: $env:GATE_API_TOKEN" "$env:GATE_API_URL/gate/resume"

# 强制恢复（跳过检查，仅在确认底层已修复时使用，会写审计）
curl -X POST -H "X-Gate-Token: $env:GATE_API_TOKEN" "$env:GATE_API_URL/gate/resume?force=true"
```

`resume?force=true` 会记 `event=resume force=true`；事后必须在复盘文档里说明。

## 3. 故障处置矩阵

| 现象 | 判据 | 处置 |
| --- | --- | --- |
| Freqtrade 被暂停且不再恢复 | `gate/status.safe_to_resume=false` | 按 `checks` 里失败项修复：标签服务→重启容器；Redis→`redis-cli ping`；halt→确认是否为人为熔断 |
| 全是 `missing` | `aiquant_gate_total{result="missing"}` 飙升 | 标签服务 `run_cycle` 未写入：查 `/health.last_cycle_error`、LLM 配额、`label_heartbeat` |
| 全是 `block` | `result="block"` 持续 | 若不符合行情判断：查标签 `evidence`，必要时人工 `redis-cli DEL ai_gate:BTC/USDT` 让单笔回落到 fail-open |
| 单笔下单失败 | `aiquant_cli_calls_total{result="fail"}` | 查审计 `cli_error`：区分"交易所拒绝（参数/余额）"与"网络超时"；超时已自动重试 1 次，仍失败需人工确认是否已成交，避免重复下单 |
| 裁决变慢 | `aiquant_gate_decision_seconds` P99 > 50ms | 多为 Redis 抖动；确认 `socket_timeout=0.05` 生效、Redis 无 swap、连接数未打满 |
| 只读自检失败 | `/health.components.readonly_check != ok`、容器起不来 | `okx-trade-mcp --read-only` 是否在 PATH；`tools/list` 是否暴露下单工具（升级 MCP 后必查） |
| 审计盘满 | `logs/` 增长异常 | `AuditLog.purge_old()` 由定时任务每日 03:10 执行；手工：`python -m aiquant.audit.cli purge` |

## 4. 手动触发一次标签生成

```powershell
curl -X POST $env:LABEL_SERVICE_URL/labels/run-once
curl "$env:LABEL_SERVICE_URL/labels/BTC%2FUSDT"
```

返回的 `Label` 即为写入 Redis 的内容；若 `evidence[0].ref == "fallback"`，说明本次是兜底 allow（LLM 或 Schema 失败），需要查日志。

## 5. 回放 / A/B 验证（改参数前后必须做）

```powershell
python -m aiquant.validation.cli `
  --labels data/labels.jsonl --bars data/bars.csv `
  --periods-per-year 105120 --stake 100 --hold-bars 12 `
  --out reports/ab-2026-01-01.json
```

* 退出码 `0` approve：可上线；`2` reject：禁止上线；`3` 无交易：数据或阈值配置有问题；
* approve 规则（`validation/report.py`）：夏普提升 ≥ 0.1 **或** 最大回撤下降 ≥ 10%，
  且交易数下降 ≤ 30%，且任一窗口亏损 ≤ 基线 MDD 的 1.5 倍。

## 6. 密钥轮换

1. 新 OKX API Key 只授予交易权限（禁止提币），IP 白名单绑定出口；
2. 改 `.env` → `docker compose up -d --force-recreate gate-service`；
3. 确认 `/health` ok 后，到 OKX 后台吊销旧 Key；
4. 轮换 `GATE_API_TOKEN` / Freqtrade 密码时，同步更新调用方脚本；
5. 轮换 LLM Key 后跑一次 `/labels/run-once` 验证。

## 7. 变更与回滚

* 代码变更：`python -m pytest -q` + `--cov` 必须全绿（覆盖率 ≥ 90%）；
* 参数变更（TTL、乘数、最小下单量）：改 `.env` 后重启守门服务，审计会记录 `gate_toggle_changed`；
* 回滚：`git revert <sha>` → 重新构建镜像 → `docker compose up -d`；
* 回滚后 15 分钟内观察 `gate_total` 分布与 `cli_calls` 失败率为 0。

## 8. 每日自动动作

| 时间 | 动作 | 实现 |
| --- | --- | --- |
| 每 30s | 守护检查（标签健康 / Redis / halt），必要时暂停或恢复 Freqtrade | `Guarder.run_forever` |
| 每 30s | 写 `system:label_service_heartbeat`（TTL 60） | `LabelService.heartbeat` |
| 每 60s | 为每个交易对生成并发布标签 | `LabelService.run_cycle` |
| 每天 03:10 | 清理超期审计分片 | `AuditLog.purge_old` |
| 每 10 分钟 | 提交并推送代码快照到远端 | 计划任务 `aiquant-autopush` → `scripts/auto_push.ps1` |

注册/刷新计划任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
schtasks /Query /TN aiquant-autopush /FO LIST      # 查看下次执行时间
schtasks /Run   /TN aiquant-autopush               # 立即演练一次
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove   # 卸载
```

推送日志在 `logs/auto_push.log`；无变更时脚本直接跳过，不会产生空提交。
