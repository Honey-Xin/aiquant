以下为完整 Markdown 文档，可直接保存为 `docs/requirements.md`。

---

# AI 量化交易守门员系统 需求说明书

> 文档版本：v1.0  
> 文档状态：开发基线  
> 适用对象：AI 编码助手 / 开发工程师  
> 文档目的：把已确定的技术方案转成可执行、可验收、边界清晰的开发需求。  
> 核心纪律：**AI 只读观察并写标签；Freqtrade 读标签做最终裁决；OKX CLI 执行下单；AI 永不直接触发交易。**

---

## 目录

1. [项目概述](#1-项目概述)
2. [术语与角色](#2-术语与角色)
3. [总体架构](#3-总体架构)
4. [技术栈与依赖](#4-技术栈与依赖)
5. [功能需求](#5-功能需求)
6. [接口与配置](#6-接口与配置)
7. [非功能需求](#7-非功能需求)
8. [分阶段交付与验收](#8-分阶段交付与验收)
9. [建议目录结构](#9-建议目录结构)
10. [给 AI 开发者的硬性约束](#10-给-ai-开发者的硬性约束)
11. [验收测试用例](#11-验收测试用例)
12. [风险、回滚与运维](#12-风险回滚与运维)
13. [附录](#13-附录)

---

## 1. 项目概述

### 1.1 目标

构建一个面向 OKX 虚拟货币交易的**双层异构系统**：

- **AI 标签域**：通过 `okx-trade-mcp --read-only` 和外部信息源，由 LLM 生成结构化市场状态标签。
- **执行域**：Freqtrade 生成策略信号，在 `confirm_trade_entry` 中读取标签，做**放行 / 降仓 / 拦截**的最终裁决，通过 `okx-trade-cli` 下单。
- **验证域**：上线前用历史标签回放 + Purged Walk-Forward 做 A/B 对照，样本外无显著改善则不允许接入闸门。

### 1.2 非目标（明确不做）

- 不让 LLM 直接下单、直接调用交易工具。
- 不在实盘执行链路中同步调用 LLM。
- 不实现“AI 预测涨跌”作为触发信号；AI 只做**否决或仓位乘数**。
- 不承诺盈利；只承诺系统纪律、可审计、可回滚。

### 1.3 成功标准

- 不接 AI 闸门时，执行域可稳定 dry-run。
- 接入只读标签后，标签服务故障不阻塞交易。
- 验证模块能输出可复现的 approve/reject 报告。
- 实盘路径中没有任何 LLM 同步调用。
- 所有裁决可追溯到标签快照与策略信号。

---

## 2. 术语与角色

| 术语 | 含义 |
|---|---|
| 标签（Label） | AI 输出的强类型状态对象，写入 Redis/SQLite |
| 闸门（Gate） | `confirm_trade_entry` 中的确定性裁决逻辑 |
| 只读实例 | `okx-trade-mcp --read-only`，不注册下单工具 |
| 交易实例 | `okx-trade-cli`，仅由 Freqtrade 子进程调用 |
| 默认放行 | 标签缺失、超时、解析失败时返回 `True` |
| fail-closed | 启动时暂停交易，守门服务确认无 halt 后才放行 |
| 研究域 | AI 标签域，无交易权限 |
| 执行域 | 确定性代码域，不调用 LLM |

---

## 3. 总体架构

```mermaid
flowchart LR
    subgraph EXT[外部信息源]
        N1[新闻/公告/情绪]
        N2[链上异动/事件日历]
    end

    subgraph AIDOM[AI 标签域 无交易权限]
        M1[okx-trade-mcp --read-only]
        L1[LLM 分析]
        S1[(Redis/SQLite<br/>ai_gate:{pair})]
    end

    subgraph EXEC[执行域 确定性]
        F1[Freqtrade 策略信号]
        G1[confirm_trade_entry]
        P1[内置 protections]
        C1[okx-trade-cli]
    end

    OKX[(OKX 交易所)]

    N1 --> L1
    N2 --> L1
    M1 --> L1
    L1 --> S1
    F1 --> G1
    S1 -. 只读标签 .-> G1
    G1 --> P1 --> C1 --> OKX
    M1 --> OKX
```

### 3.1 信任域隔离

- **AI 标签域**：无下单权限，只能读 OKX 数据、写标签存储。
- **执行域**：不调用 LLM；只读标签、执行确定性规则、调用 CLI。
- **密钥**：API Key 仅存在本地配置文件/环境变量，不进入 LLM 上下文、不写日志。

### 3.2 数据流规则

- AI 侧只能单向写标签，不能写订单、不能改策略参数。
- 执行侧只能单向读标签，不能反向调用 LLM。
- 标签缺失或异常时，执行侧默认放行。
- 任何外部调用必须有超时与异常降级。

---

## 4. 技术栈与依赖

| 组件 | 选型 | 说明 |
|---|---|---|
| 执行框架 | Freqtrade（Python） | 使用 `confirm_trade_entry` 钩子 |
| OKX 接入 | OKX Agent Trade Kit | `okx-trade-mcp --read-only` + `okx-trade-cli` |
| 标签存储 | Redis（主）+ SQLite（审计/回放） | Redis 低延迟读取；SQLite 持久化 |
| LLM 接入 | 可配置 OpenAI/Anthropic/DeepSeek | 通过环境变量配置，不硬编码 |
| 验证 | Python + pandas/numpy | 自实现 Purged Walk-Forward 与报告 |
| 部署 | Docker Compose | 至少三个服务：label-service、freqtrade、redis |
| 测试 | pytest | 覆盖闸门逻辑、降级、schema 校验 |

### 4.1 版本锁定要求

- 必须提供 `requirements.txt` 或 `pyproject.toml`，锁定主要依赖版本。
- Docker 镜像必须指定明确 tag，不得使用 `latest`。
- LLM SDK 版本必须锁定，避免接口漂移。

---

## 5. 功能需求

### FR-1 AI 标签服务

#### FR-1.1 输入源

- 必须支持：OKX 只读 MCP 数据（行情、资金费率、持仓量、资讯模块）。
- 必须支持：至少一个外部新闻/情绪源（如 CryptoPanic），接口可配置。
- 可选：链上异动、事件日历；未配置时不得阻塞主流程。

#### FR-1.2 标签生成

- 调用 LLM，要求输出**严格 JSON**，不得包含自然语言前后缀。
- 必须做 JSON Schema 校验；校验失败重试 1 次，仍失败则写 `action=allow` 并记录错误。
- 必须为每个交易对生成标签，写入 Redis key：`ai_gate:{pair}`，TTL 可配置（默认 300 秒）。
- 标签生成周期可配置，默认每 60 秒一轮。

#### FR-1.3 标签 Schema（必须实现）

```json
{
  "pair": "BTC/USDT",
  "generated_at": "2026-11-01T12:00:00Z",
  "ttl_sec": 300,
  "regime": "risk_on | risk_off | event_driven | illiquid | unknown",
  "event_flags": ["macro_event", "token_unlock", "exchange_incident"],
  "action": "allow | reduce_size | block_new_entries",
  "size_multiplier": 1.0,
  "confidence": "low | medium | high",
  "evidence": [
    {"source": "okx_mcp", "summary": "funding rate extreme", "ref": "..."}
  ]
}
```

约束：

- `action=reduce_size` 时 `size_multiplier` 必须在 `(0,1]`。
- `action=block_new_entries` 时 `size_multiplier` 忽略。
- `confidence` 不参与自动裁决，仅用于审计与统计。
- `evidence` 至少 0 条，最多 10 条；不得包含密钥或敏感凭证。

#### FR-1.4 只读保证

- 启动命令必须为 `okx-trade-mcp --read-only`。
- 服务启动时自检：尝试列出可用工具，若存在下单类工具则**拒绝启动**并告警。
- 自检结果写入审计日志。

#### FR-1.5 心跳与健康

- 标签服务必须每 30 秒写入 `system:label_service_heartbeat`，TTL 60 秒。
- 必须暴露 `/health`，返回标签服务状态、Redis 连接状态、MCP 只读自检状态。
- 必须暴露 `/metrics`，Prometheus 格式。

---

### FR-2 执行域闸门

#### FR-2.1 策略信号

- 使用 Freqtrade 标准策略接口生成入场信号。
- 策略本身不感知 AI；AI 逻辑只在 `confirm_trade_entry` 中。
- 策略参数必须通过配置文件管理，不得硬编码。

#### FR-2.2 `confirm_trade_entry` 裁决规则（必须按顺序）

1. 读取 `ai_gate:{pair}`。
2. 若不存在、解析失败、`generated_at + ttl_sec < now`：**返回 `True`（默认放行）**，记录 `gate_missing` 指标。
3. 若 `action == "block_new_entries"`：返回 `False`，记录 `gate_block`。
4. 若 `action == "reduce_size"`：将 `amount` 乘以 `size_multiplier`，若结果小于交易所最小下单量则返回 `False`；否则返回 `True`，记录 `gate_reduce`。
5. 若 `action == "allow"` 或未知值：返回 `True`。
6. 任何异常：捕获并返回 `True`，记录 `gate_error`。

#### FR-2.3 内置 protections

- 必须启用 Freqtrade protections：`StoplossGuard`、`MaxDrawdown`、`CooldownPeriod`，参数可配置。
- protections 在闸门之后、下单之前生效。
- protections 触发时必须记录审计日志。

#### FR-2.4 OKX 执行

- 通过 `okx-trade-cli` 子进程执行下单；不得在代码中直接使用 OKX REST 私钥签名。
- CLI 调用必须设置超时（默认 10 秒），超时视为失败并记录。
- 所有订单请求与响应写入审计日志（脱敏）。
- CLI 返回非零退出码时，必须记录 stderr 摘要，不得导致进程崩溃。

#### FR-2.5 fail-closed 启动

- Freqtrade 启动时默认 `paused=true`。
- 独立守门服务检查标签服务健康、Redis 可用、无 halt 标志后，调用 Freqtrade API 恢复交易。
- 任一检查失败：保持暂停并告警。
- 恢复交易动作必须写入审计日志。

#### FR-2.6 闸门开关

- 必须提供配置开关 `GATE_ENABLED`。
- 关闭时，`confirm_trade_entry` 直接返回 `True`，行为等同纯策略。
- 开关变更必须记录审计日志，不需要重启进程。

---

### FR-3 验证模块（上线前必须）

#### FR-3.1 历史标签回放

- 输入：历史行情 + 历史新闻/事件（时间戳对齐）。
- 输出：按时间序列的标签文件（JSONL），每条含 `pair, ts, action, size_multiplier`。
- 必须保证**标签只使用该时间点之前可得的信息**；禁止未来函数。
- 回放模块与实盘路径隔离，不得被实盘代码导入。

#### FR-3.2 A/B 对照回测

- 基线：纯策略，不读标签。
- 实验组：策略 + 闸门（按历史标签执行相同裁决逻辑）。
- 必须使用 **Purged Walk-Forward**：滚动训练/测试窗口，训练与测试之间设置 embargo，剔除标签窗口重叠样本。
- 必须输出报告：年化收益、夏普、最大回撤、换手率、交易次数、胜率、盈亏比、`gate_block/reduce/missing` 计数。
- 报告必须包含每个窗口的明细与汇总。

#### FR-3.3 接入闸门判定

- 判定规则可配置，默认：
  - 样本外夏普提升 ≥ 0.1，或最大回撤降低 ≥ 10%；
  - 且交易次数下降不超过 30%；
  - 且无单窗口灾难性亏损（超过基线最大回撤 1.5 倍）。
- 未通过：生成 `reject` 报告，禁止进入 FR-2 实盘接入。
- 通过：生成 `approve` 报告，人工确认后才允许修改配置接入。
- 判定逻辑必须与执行逻辑分离，验证模块不得被实盘路径导入。

#### FR-3.4 回放未来函数检查

- 必须校验标签时间戳不晚于信号时间。
- 发现违规时，验证模块必须报错并拒绝生成报告。
- 必须提供人工抽查清单，至少抽查 10 条标签。

---

### FR-4 监控与审计

- 必须暴露 `/health` 与 `/metrics`（Prometheus 格式）。
- 指标至少包括：标签生成成功率、标签延迟、`gate_*` 计数、CLI 调用失败数、Freqtrade 暂停状态。
- 审计日志字段：时间、pair、信号、标签快照、裁决结果、订单请求/响应摘要、错误堆栈。
- 日志必须脱敏：不得出现 API Key、Secret、Passphrase。
- 审计日志必须可按时间范围导出，用于复盘。

---

## 6. 接口与配置

### 6.1 Redis Key 约定

| Key | 类型 | 说明 |
|---|---|---|
| `ai_gate:{pair}` | String(JSON) | 标签 JSON，TTL 由标签内 `ttl_sec` 决定 |
| `system:halt` | String | 存在且值为 `1` 时，守门服务拒绝恢复交易 |
| `system:label_service_heartbeat` | String | 标签服务心跳，TTL 60 秒 |

### 6.2 配置文件（`.env.example` 必须提供）

| 变量 | 说明 | 默认 |
|---|---|---|
| `OKX_API_KEY/SECRET/PASSPHRASE` | 仅执行域使用 | 无 |
| `OKX_MCP_READONLY_CMD` | 只读 MCP 启动命令 | `okx-trade-mcp --read-only` |
| `OKX_CLI_CMD` | CLI 命令 | `okx-trade-cli` |
| `REDIS_URL` | Redis 地址 | `redis://redis:6379/0` |
| `LABEL_TTL_SEC` | 标签默认 TTL | `300` |
| `LLM_PROVIDER/MODEL/API_KEY` | LLM 配置 | 无 |
| `GATE_ENABLED` | 闸门开关 | `true` |
| `GATE_FAIL_OPEN` | 标签缺失是否放行 | `true`（必须为 true） |
| `WFA_EMBARGO_BARS` | Purged WFA embargo | `24` |
| `APPROVE_SHARPE_DELTA` | 接入阈值 | `0.1` |
| `APPROVE_MDD_REDUCTION` | 接入阈值 | `0.10` |
| `CLI_TIMEOUT_SEC` | CLI 超时 | `10` |

### 6.3 外部接口

- 标签服务：`GET /health`、`GET /metrics`。
- 守门服务：`POST /gate/resume`、`POST /gate/pause`、`GET /gate/status`。
- Freqtrade：使用其原生 REST API 进行暂停/恢复。
- 所有接口必须鉴权，默认只监听内网。

---

## 7. 非功能需求

| 类别 | 要求 |
|---|---|
| 延迟 | 闸门读取 Redis 的 P99 < 5ms；不得在 `confirm_trade_entry` 中调用 LLM 或外部 HTTP |
| 可靠性 | 标签服务故障不得阻塞交易；默认放行并告警 |
| 安全 | 密钥不进入 LLM、不写日志；只读 MCP 自检失败拒绝启动 |
| 可审计 | 每次裁决可追溯到标签快照与策略信号 |
| 可回滚 | 闸门接入通过配置开关控制，关闭后行为等同纯策略 |
| 可测试 | 闸门逻辑单元测试覆盖率 ≥ 90% |
| 可观测 | 所有关键路径有指标与日志；告警规则可配置 |
| 可部署 | 一条 `docker compose up` 可启动 P0/P1 最小集 |

---

## 8. 分阶段交付与验收

### P0 执行骨架（无 AI）

**交付**：Freqtrade + `okx-trade-cli` 跑通 dry-run；protections 生效；审计日志。  
**验收**：连续 dry-run 72 小时无崩溃；订单语义与 OKX 一致；关闭闸门开关时行为与纯策略一致。

### P1 只读标签服务

**交付**：`okx-trade-mcp --read-only` 接入；LLM 输出 Schema 校验；Redis 写入；心跳与指标。  
**验收**：只读自检通过；标签 JSON 100% 通过 Schema；断网/LLM 失败时写 `allow` 且不阻塞；日志无密钥。

### P2 验证闭环

**交付**：历史标签回放、Purged WFA A/B、报告生成、approve/reject 判定。  
**验收**：报告包含第 5.3 节全部指标；回放无未来函数（人工抽查 10 条）；判定规则可配置；reject 时系统拒绝接入。

### P3 闸门接入 dry-run

**交付**：`confirm_trade_entry` 读取标签；fail-closed 启动；`gate_*` 指标。  
**验收**：单测覆盖缺失/超时/block/reduce/异常五种路径；标签缺失放行；block 拦截；reduce 仓位乘数正确；CLI 超时降级。

### P4 小资金实盘

**交付**：小资金实盘运行；实盘与回测统计包络比对。  
**验收**：运行 ≥ 1 个市场周期（建议 ≥ 3 个月）；偏离超阈值自动暂停；审计日志完整可回放。

---

## 9. 建议目录结构

```text
ai-quant-gate/
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── README.md
├── config/
│   ├── freqtrade/config.json
│   └── labels/config.yaml
├── src/
│   ├── label_service/
│   │   ├── main.py
│   │   ├── schema.py
│   │   ├── sources/okx_mcp.py
│   │   ├── sources/news.py
│   │   ├── llm/client.py
│   │   └── store/redis_store.py
│   ├── execution/
│   │   ├── strategy.py
│   │   └── okx_cli_adapter.py
│   ├── gate/
│   │   ├── service.py
│   │   └── health.py
│   └── validation/
│       ├── replay.py
│       ├── wfa.py
│       └── report.py
└── tests/
    ├── test_gate.py
    ├── test_schema.py
    ├── test_replay.py
    └── test_cli_adapter.py
```

---

## 10. 给 AI 开发者的硬性约束

1. **不得**让 LLM 输出直接触发下单；所有交易必须经过 `confirm_trade_entry`。
2. **不得**在 `confirm_trade_entry` 中发起网络请求或 LLM 调用。
3. **不得**把 API Key 写入代码、日志、标签、LLM prompt。
4. **必须**实现“默认放行”：标签任何异常都不阻塞交易。
5. **必须**实现只读自检：发现下单工具暴露则拒绝启动。
6. **必须**提供单元测试，覆盖闸门五种路径与 Schema 校验失败重试。
7. **必须**提供 `.env.example` 与 `docker-compose.yml`，一条命令可启动 P0/P1。
8. **必须**在 README 中写明：如何跑 P2 验证、如何解读 approve/reject 报告、如何回滚闸门。
9. **禁止**为了“让回测好看”而调整验证逻辑；验证代码与执行代码分离，验证模块不得被实盘路径导入。
10. 所有外部调用必须有超时与异常降级；任何未捕获异常不得导致下单。
11. **禁止**在实盘路径中引入验证模块依赖。
12. **必须**保留审计日志，默认保留 180 天，可配置。

---

## 11. 验收测试用例

| 用例 | 输入 | 期望 |
|---|---|---|
| G-01 标签缺失 | Redis 无 key | 放行，`gate_missing+1` |
| G-02 标签超时 | `generated_at+ttl < now` | 放行，`gate_missing+1` |
| G-03 block | `action=block_new_entries` | 返回 False，`gate_block+1` |
| G-04 reduce | `action=reduce_size, size_multiplier=0.5` | amount 减半；低于最小下单量则 False |
| G-05 异常 JSON | 非法 JSON | 放行，`gate_error+1` |
| G-06 只读自检 | MCP 暴露下单工具 | 标签服务拒绝启动 |
| G-07 fail-closed | `system:halt=1` | Freqtrade 保持暂停 |
| G-08 回放未来函数 | 标签时间戳晚于信号时间 | 验证模块报错并拒绝生成报告 |
| G-09 CLI 超时 | CLI 进程超过 10 秒 | 记录失败，不崩溃，不重试下单 |
| G-10 闸门关闭 | `GATE_ENABLED=false` | `confirm_trade_entry` 直接返回 True |
| G-11 密钥脱敏 | 审计日志与错误堆栈 | 不出现 API Key/Secret/Passphrase |
| G-12 Schema 重试 | 首次非法 JSON，第二次合法 | 写入合法标签，记录一次重试 |

---

## 12. 风险、回滚与运维

### 12.1 主要风险

- **标签质量风险**：LLM 输出不稳定或证据不足。缓解：Schema 校验、默认放行、只做否决。
- **过拟合风险**：验证阈值被调松。缓解：验证代码与执行代码分离，阈值变更需人工审批。
- **权限风险**：交易凭证进入 AI 域。缓解：只读 MCP 自检、密钥不写日志、权限物理隔离。
- **运维风险**：标签服务故障导致误判。缓解：心跳、默认放行、告警。
- **回放污染风险**：历史标签使用未来信息。缓解：时间戳校验、人工抽查、验证模块隔离。

### 12.2 回滚方案

- 关闭 `GATE_ENABLED`，执行域立即退化为纯策略。
- 停止标签服务，不影响交易。
- 回滚到上一版本 Docker 镜像 tag。
- 回滚后必须保留审计日志，便于复盘。

### 12.3 运维要求

- 必须提供告警规则：标签服务心跳丢失、`gate_error` 激增、CLI 失败率上升、Freqtrade 长时间暂停。
- 必须提供每日审计摘要：标签数量、`gate_*` 分布、订单数量、异常次数。
- 必须提供月度复盘模板：标签有效性、闸门拦截质量、是否需要调整阈值。

---

## 13. 附录

### 13.1 标签示例：放行

```json
{
  "pair": "BTC/USDT",
  "generated_at": "2026-11-01T12:00:00Z",
  "ttl_sec": 300,
  "regime": "risk_on",
  "event_flags": [],
  "action": "allow",
  "size_multiplier": 1.0,
  "confidence": "medium",
  "evidence": [
    {"source": "okx_mcp", "summary": "funding normal", "ref": "funding:BTC-USDT-SWAP"}
  ]
}
```

### 13.2 标签示例：降仓

```json
{
  "pair": "ETH/USDT",
  "generated_at": "2026-11-01T12:00:00Z",
  "ttl_sec": 300,
  "regime": "event_driven",
  "event_flags": ["macro_event"],
  "action": "reduce_size",
  "size_multiplier": 0.5,
  "confidence": "high",
  "evidence": [
    {"source": "news", "summary": "macro data release in 30m", "ref": "cryptopanic:123"}
  ]
}
```

### 13.3 标签示例：拦截

```json
{
  "pair": "SOL/USDT",
  "generated_at": "2026-11-01T12:00:00Z",
  "ttl_sec": 300,
  "regime": "illiquid",
  "event_flags": ["exchange_incident"],
  "action": "block_new_entries",
  "size_multiplier": 0,
  "confidence": "high",
  "evidence": [
    {"source": "okx_mcp", "summary": "order book depth anomaly", "ref": "depth:SOL-USDT"}
  ]
}
```

### 13.4 一句话交付标准

AI 开发者按本文档实现后，系统应能在**不接 AI 闸门**时稳定 dry-run；接入只读标签后**不阻塞交易**；验证模块能给出**可复现的 approve/reject 报告**；实盘路径中**没有任何 LLM 同步调用**。

---

**文档结束**