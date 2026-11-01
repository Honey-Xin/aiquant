# 架构与数据流

## 1. 分层与信任边界

| 层 | 组件 | 能做什么 | 绝对不能做什么 |
| --- | --- | --- | --- |
| 观察层 | `okx-trade-mcp --read-only`、新闻源 | 读行情、资金费率、持仓量、盘口、新闻 | 下单、撤单、划转（自检拦截） |
| 标签层 | `label_service` + LLM | 生成 `Label` JSON 并写 Redis | 访问 OKX 交易凭证、调用 CLI、直接触发交易 |
| 裁决层 | `gate.decision.Gate` | 读标签、算裁决、写审计 | 发起网络交易请求、调 LLM |
| 执行层 | `freqtrade_strategy` + `okx-trade-cli` | 依据裁决下单/缩量 | 绕过闸门下单 |
| 验证层 | `validation/*` | 回放、WFA、A/B 报告 | 被实盘路径导入 |

边界由 `tests/test_architecture.py` 用独立子进程探针守护：导入 `aiquant.gate.decision`
时不允许出现 `aiquant.validation`、`aiquant.label_service.llm`、`httpx`。

## 2. 标签 Schema（FR-1.3）

```json
{
  "pair": "BTC/USDT",
  "generated_at": "2026-01-01T00:00:00Z",
  "ttl_sec": 300,
  "regime": "risk_off",
  "event_flags": ["funding_spike"],
  "action": "reduce_size",
  "size_multiplier": 0.5,
  "confidence": "medium",
  "evidence": [{"source": "okx_mcp", "summary": "资金费率 0.09% 偏高", "ref": "funding_rate"}]
}
```

* `action=allow` ⇒ `size_multiplier` 必须为 `1.0`；`reduce_size` ⇒ 必须落在 `(0, 1]`；
  `block_new_entries` ⇒ 乘数被忽略；
* `evidence` 0~10 条，禁止凭证类字段（schema 与 LLM prompt 双重约束）；
* 解析严格：markdown 代码围栏、前后缀文本一律视为结构错误 → 重试一次 → 兜底 `allow`。

## 3. 时序：一次开仓裁决

```
Freqtrade    Gate                     Redis                 Audit
   │ confirm_trade_entry(pair, amt)     │                     │
   ├──────────────▶│  GET ai_gate:BTC/USDT                     │
   │               ├────────────────────▶│                     │
   │               │◀──── label / None ──┤                     │
   │               │ 过期或缺失 ⇒ allow                          │
   │               │ block ⇒ False                             │
   │               │ reduce ⇒ amt*mult, 低于最小量 ⇒ False      │
   │               ├───────────────────────────────────────────▶│ gate_decision
   │◀── (bool, decision) ───────────────┤                     │
   │ 下单（custom_stake_amount 已按乘数缩放）                    │
```

耗时目标：Redis 读 P99 < 5ms，整次裁决 < 10ms（`aiquant_gate_decision_seconds` 直方图）。

## 4. Fail-closed 守护（FR-2.5）

```
Boot: Freqtrade 以 initial_state=paused 启动
Guarder（每 30s）:
  check_label_service() → GET {LABEL_SERVICE_URL}/health : status == ok
  check_redis()         → store.ping()
  check_halt()          → system:halt 不存在
  三项全过 ⇒ POST /api/v1/start（审计 resume）
  任一失败 ⇒ 保持暂停（审计 resume_refused）
外部手动 halt ⇒ POST /gate/pause?reason=xxx（写 system:halt）
```

注意"闸门 fail-open"与"宿主 fail-closed"是两个不同方向的保障：
标签异常时**单笔交易放行**，但标签服务整体不可用时**不许开新仓**（宿主保持暂停）。

## 5. 指标与告警

`aiquant_*` 指标由 `metrics.py` 自实现（零依赖），`/metrics` 暴露，`config/alerts.yml` 给出规则：

| 指标 | 用途 |
| --- | --- |
| `aiquant_gate_total{result}` | 裁决分布（missing 激增 ⇒ 标签链路故障） |
| `aiquant_gate_decision_seconds` | 裁决延迟 |
| `aiquant_label_heartbeat_age_seconds` | 标签服务存活 |
| `aiquant_cli_calls_total{result}` / `aiquant_cli_seconds` | 执行层成败与耗时 |
| `aiquant_freqtrade_paused` | 宿主是否被守护暂停 |
| `aiquant_system_halt` | 人工熔断状态 |
| `aiquant_readonly_check_ok` | 只读自检结果 |

## 6. 审计

* JSONL 分片 `logs/{stream}-YYYY-MM-DD.jsonl`，`fsync` 落盘；
* 事件流：`label_*`、`gate_*`、`cli_*`、`resume*`、`halt`、`readonly_check`；
* 脱敏：`redact()` 处理键值型密钥、Bearer、32~64 位十六进制/长 base64；
* 保留 180 天，`AuditLog.purge_old()` 清理过期分片，`daily_summary()` 生成日报。
