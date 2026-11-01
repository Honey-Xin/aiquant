# AI 量化交易守门员（aiquant）

> AI 只写标签，Freqtrade 做裁决，`okx-trade-cli` 负责下单。
> LLM 永不直接触发交易；实盘路径上没有任何同步的 LLM 调用。

本仓库按 `docs/requirements.md`（原始需求文档）实现，覆盖 P0（守门闭环）、
P1（守护与运维）、P2（回放 / Walk-Forward / A/B 验证）。

---

## 1. 架构一图流

```
                 ┌──────────────────── 标签域（AI，只读） ───────────────────┐
okx-trade-mcp ──▶│ 行情快照 ─┐                                              │
 (--read-only)   │           ├─▶ LLM(JSON) ─▶ Schema 校验 ─▶ 标签 ai_gate:X  │
 新闻/情绪源 ────▶│ 情绪快照 ─┘        (失败⇒兜底 allow)      (Redis, TTL 300s)│
                 └──────────────────────────────────┬───────────────────────────┘
                                                    │ 只写不读
                 ┌────────────────── 执行域（确定性） ▼ ──────────────────────┐
                 │ Freqtrade ── confirm_trade_entry ──▶ Gate.decision（纯函数）│
                 │      │                                    │                 │
                 │      │ custom_stake_amount ◀── reduce_size 乘数             │
                 │      ▼                                    │                 │
                 │  okx-trade-cli（唯一执行者）               └─ 审计 JSONL     │
                 └────────────────────────────────────────────────────────────┘
```

* 标签域**不持有交易凭证**，只能读行情 + 写 Redis；
* 执行域**不调用 LLM**，只做确定性裁决（`tests/test_architecture.py` 用子进程探针
  保证这两条边界不被悄悄打破）。

## 2. 快速开始

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env      # 填 LLM_API_KEY / OKX_* / GATE_API_TOKEN

# 1) 测试 + 覆盖率（门槛 90%）
python -m pytest --cov=aiquant --cov-report=term-missing

# 2) 本地不依赖 Redis 跑标签服务（AIQUANT_STORE=memory）
$env:AIQUANT_STORE="memory"; $env:READONLY_CHECK_REQUIRED="false"
python -m aiquant.label_service.main

# 3) 守门服务
python -m aiquant.gate.service
```

容器方式（Redis + 两个服务 + Freqtrade）：

```powershell
docker compose --profile p0 up -d      # P0：守门闭环
docker compose --profile p1 up -d      # P1：加 Freqtrade 守护与自动恢复
```

## 3. 裁决语义（FR-2.2，顺序不可调换）

| 步骤 | 条件 | 结果 | 审计 `result` |
| --- | --- | --- | --- |
| 1 | `GATE_ENABLED=false` | 放行 | `disabled` |
| 2 | 标签缺失 / JSON 非法 / `generated_at+ttl_sec < now` | 放行 | `missing` |
| 3 | `action=block_new_entries` | 拒绝 | `block` |
| 4 | `action=reduce_size` | 按 `size_multiplier` 缩放，缩放后低于最小下单量则拒绝 | `reduce` |
| 5 | `action=allow` 或未知值 | 放行 | `allow` |
| 6 | 任意异常（Redis 超时等） | 放行（`GATE_FAIL_OPEN` 强制 true） | `error` |

* 读 Redis 的 `socket_timeout` 默认 **50ms**：任何抖动都在毫秒级失败，不挂住下单路径；
* 平仓（`confirm_trade_exit`）永远放行——守门只拦"开新仓"。

## 4. 目录

```
src/aiquant/
  settings.py            # 全部配置来自环境变量，无硬编码密钥
  metrics.py             # 零依赖 Prometheus 文本格式实现
  audit/logger.py        # JSONL 审计 + 脱敏 + 分片 + 保留期清理
  label_service/         # 标签域：schema、LLM、只读 MCP、新闻、服务
  gate/                  # 执行域：decision、freqtrade_client、guarder、health、service
  execution/             # freqtrade_strategy（宿主策略）、okx_cli（唯一执行者）
  validation/            # replay / wfa / report / cli（禁止被实盘路径导入）
config/                  # freqtrade + labels 配置、Prometheus 告警规则
docker/                  # 两个服务的 Dockerfile
docs/                    # 需求副本、架构、运维手册、验收对照
scripts/                 # 自动提交推送、演示数据等
tests/                   # 299 个用例，覆盖率 94%
```

## 5. 关键约束（勿改）

1. `GATE_FAIL_OPEN` 必须为 `true`，否则 `Settings` 直接启动失败；
2. 标签服务启动时执行只读自检：命令里必须有 `--read-only`，且 `tools/list`
   不得暴露任何下单/撤单/划转类工具，否则拒绝启动；
3. Freqtrade `initial_state=paused`，只有守门服务的守护线程确认
   （标签服务 `/health` ok + Redis ping ok + 无 `system:halt`）后才调 `/api/v1/start`；
4. 标签里不得出现凭证；审计与日志双层脱敏；
5. `aiquant.validation` 与 `aiquant.label_service.llm` 不得出现在实盘路径的导入链上。

## 6. 验证与验收

```powershell
# 回放 + A/B 报告：退出码 0=approve，2=reject，3=无交易
python -m aiquant.validation.cli --labels data/labels.jsonl --bars data/bars.csv --out reports/
```

验收用例 G-01 ~ G-12 的落点见 [docs/acceptance.md](docs/acceptance.md)，
运维处置流程见 [docs/runbook.md](docs/runbook.md)。

## 7. 免责声明

本项目为量化交易工程示例：默认 `OKX_DEMO_TRADING=true` 与 `dry_run=true`，
切换实盘前请自行完成回测、小额验证与风控评审，交易盈亏由使用者自负。
