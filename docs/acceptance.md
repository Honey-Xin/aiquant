# 验收对照表（需求文档 §7 G-01 ~ G-12）

运行方式：

```powershell
python -m pytest -q                              # 299 个用例
python -m pytest --cov=aiquant --cov-report=term-missing   # 覆盖率 94.14%（门槛 90%）
```

| 编号 | 验收要求 | 实现落点 | 对应测试（`tests/` 下） |
| --- | --- | --- | --- |
| G-01 | 标签缺失 ⇒ 放行，审计 `result=missing` | `src/aiquant/gate/decision.py::Gate.evaluate` 分支 2 | `test_gate.py::test_g01_missing_label_allows` |
| G-02 | `generated_at+ttl_sec < now` ⇒ 视为缺失 | `label_service/schema.py::Label.is_expired` | `test_gate.py::test_g02_expired_label_allows`、`test_schema.py::test_expiry_property` |
| G-03 | `block_new_entries` ⇒ 拒绝，审计 `block` | `Gate.evaluate` 分支 3 | `test_gate.py::test_g03_block_denies`、`test_block_ignores_multiplier` |
| G-04 | `reduce_size` ⇒ 乘数缩放；低于最小下单量 ⇒ 拒绝 | `Gate.evaluate` 分支 4 + `Settings.min_amounts` | `test_gate.py::test_g04_reduce_scales_amount`、`test_g04_reduce_below_min_denies`、`test_min_amount_lookup` |
| G-05 | 非法 JSON / Schema 不符 ⇒ 放行并留痕 | `label_service/schema.py::parse_label` 严格模式 | `test_gate.py::test_g05_invalid_json_fails_open`、`test_g05_schema_violation_fails_open`、`test_schema.py::test_markdown_fence_rejected` |
| G-06 | 只读自检：命令无 `--read-only`、或暴露下单/撤单/划转工具 ⇒ 拒绝启动 | `label_service/readonly_check.py` | `test_readonly_check.py::test_g06_rejects_command_without_flag`、`test_g06_rejects_exposed_mutating_tool`、`test_label_service.py::test_startup_rejected_when_readonly_fails` |
| G-07 | fail-closed：任一检查失败 ⇒ Freqtrade 保持暂停 | `gate/guarder.py::Guarder.resume_if_safe` | `test_guarder.py::test_g07_keeps_paused_when_label_service_down`、`..._when_halt`、`..._when_redis_down`、`test_g07_resume_when_all_checks_pass` |
| G-08 | 回放禁未来函数（只取 `generated_at <= bar.ts`） | `validation/replay.py::LabelHistory.as_of` | `test_validation.py::test_g08_no_future_leak`、`test_as_of_picks_latest_visible` |
| G-09 | CLI 超时不静默重复下单：重试有上限、失败必审计 | `execution/okx_cli.py::OkxCliExecutor.execute` | `test_execution.py::test_g09_timeout_returns_error_without_raising`、`test_failure_not_retried`、`test_timeout_then_success` |
| G-10 | `GATE_ENABLED=false` ⇒ 全量放行；开关热生效并审计 | `gate/decision.py::GateToggle` | `test_gate.py::test_g10_disabled_gate_passes_through`、`test_toggle_reads_env`、`test_gate_service.py::test_toggle_hot_applies` |
| G-11 | 密钥不落日志/审计（双层脱敏） | `audit/logger.py::redact` + `logging_setup.RedactingFilter` | `test_audit_store.py::test_write_redacts_secret`、`test_api_key_assignment_masked`、`test_adapters.py::test_redacting_filter_masks_secret_in_message_and_args`、`test_metrics_settings.py::test_secrets_collected` |
| G-12 | Schema 非法重试一次，仍失败兜底 `allow` 并审计 | `schema.parse_with_retry` + `label_service/main.py::LabelService.generate_label` | `test_schema.py::test_retry_then_valid`、`test_all_invalid_falls_back_to_allow`、`test_label_service.py::test_g12_retry_on_invalid_json`、`test_g12_all_invalid_falls_back_to_allow` |

## 补充不变量（架构级）

| 不变量 | 检查方式 |
| --- | --- |
| 实盘路径不引入 LLM / 验证模块 / httpx | `test_architecture.py::test_gate_does_not_import_validation`、`test_gate_does_not_import_llm_or_http`、`test_execution_does_not_import_llm` |
| 仓库内无硬编码凭证 | `test_architecture.py::test_no_hardcoded_credentials_in_source` |
| 只读命令默认带 `--read-only` | `test_architecture.py::test_label_service_requires_readonly_cmd` |
| `GATE_FAIL_OPEN` 必须 true | `test_metrics_settings.py::test_gate_fail_open_must_be_true` |
| 无 freqtrade 时策略仍可导入 | `test_architecture.py::test_strategy_module_imports_without_freqtrade` |

## 手工验收清单（上线前）

1. `docker compose --profile p1 up -d`，确认 Freqtrade 日志出现心跳；
2. 停掉 `label-service`，30s 内 Freqtrade 被 `/api/v1/stop` 暂停（`GET /gate/status` 可见 `safe_to_resume=false`）；
3. 恢复 `label-service`，30s 内自动 `/api/v1/start`，审计出现 `event=resume`；
4. `POST /gate/pause?reason=drill`，确认写入 `system:halt=1` 且不再自动恢复；
5. `POST /gate/enabled?enabled=false`，观察 `aiquant_gate_total{result="disabled"}` 增长；
6. 抽检 `logs/audit-YYYY-MM-DD.jsonl`，确认无 `OKX_API_SECRET`、无 `Bearer ` 明文；
7. `python -m aiquant.validation.cli --labels ... --bars ... --out reports/` 的 A/B 结论与人工回测一致。
