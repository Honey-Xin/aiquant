"""存储与审计日志测试（G-11：密钥不得落盘）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from aiquant.audit.logger import (
    AuditLog,
    daily_summary,
    looks_like_credential,
    redact,
    redact_obj,
)
from aiquant.label_service.store.base import StoreError
from aiquant.label_service.store.memory_store import MemoryStore


class TestMemoryStore:
    def test_set_get_delete(self):
        s = MemoryStore()
        s.setex("k", 60, "v")
        assert s.get("k") == "v"
        s.delete("k")
        assert s.get("k") is None

    def test_ttl_expiry(self):
        s = MemoryStore()
        s.setex("k", 1, "v")
        assert s.ttl("k") is not None and s.ttl("k") <= 1.0
        # ttl<=0 表示不过期
        s.setex("forever", 0, "v")
        assert s.ttl("forever") is None
        assert s.get("forever") == "v"

    def test_ping_and_availability(self):
        s = MemoryStore()
        assert s.ping() is True
        s.set_available(False)
        assert s.ping() is False

    def test_keys_and_clear(self):
        s = MemoryStore()
        s.setex("a", 10, "1")
        s.setex("b", 10, "2")
        assert sorted(s.keys()) == ["a", "b"]
        s.clear()
        assert s.keys() == []

    def test_store_error_is_runtime_error(self):
        assert issubclass(StoreError, RuntimeError)


class TestRedaction:
    def test_api_key_assignment_masked(self):
        out = redact("api_key=abcdef1234567890")
        assert "abcdef1234567890" not in out
        assert "***REDACTED***" in out

    def test_passphrase_masked(self):
        assert "hunter2pass" not in redact("passphrase: hunter2pass")

    def test_bearer_masked(self):
        out = redact("authorization: Bearer abcdefghijklmnop")
        assert "abcdefghijklmnop" not in out

    def test_extra_secret_masked(self):
        assert "supersecretvalue" not in redact("value=supersecretvalue", ["supersecretvalue"])

    def test_long_hex_masked(self):
        token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        assert token not in redact(f"raw {token}")

    def test_redact_obj_nested(self):
        payload = {"k": "api_secret=abcd1234efgh", "n": [{"t": "Bearer zzzzzzzzzzzzzz"}]}
        out = redact_obj(payload)
        dumped = json.dumps(out)
        assert "abcd1234efgh" not in dumped
        assert "zzzzzzzzzzzzzz" not in dumped

    def test_looks_like_credential(self):
        assert looks_like_credential("api_key=abcdef123456")
        assert looks_like_credential("Bearer abcdefghijkl")
        assert not looks_like_credential("BTC/USDT funding 0.01%")
        assert not looks_like_credential("")
        assert not looks_like_credential(None)


class TestAuditLog:
    def test_write_creates_daily_shard(self, tmp_path):
        audit = AuditLog(tmp_path, 180, ["topsecretvalue"])
        path = audit.write("gate", "gate_decision", pair="BTC/USDT", result="allow")
        assert path.exists()
        assert path.name.startswith("gate-")
        record = json.loads(path.read_text(encoding="utf-8").strip())
        assert record["event"] == "gate_decision"
        assert record["pair"] == "BTC/USDT"
        assert record["ts"].endswith("Z")

    def test_write_redacts_secret(self, tmp_path):
        audit = AuditLog(tmp_path, 180, ["topsecretvalue"])
        path = audit.write("execution", "cli_call", detail="topsecretvalue")
        text = path.read_text(encoding="utf-8")
        assert "topsecretvalue" not in text
        assert "***REDACTED***" in text

    def test_iter_and_filter(self, tmp_path):
        audit = AuditLog(tmp_path, 180, ())
        audit.write("gate", "gate_decision", result="allow")
        audit.write("label", "label_written", action="allow")
        assert len(list(audit.iter_records(stream="gate"))) == 1
        assert len(list(audit.iter_records())) == 2

        future = datetime.now(timezone.utc) + timedelta(days=1)
        assert list(audit.iter_records(end=future)) != []
        assert list(audit.iter_records(start="2000-01-01T00:00:00Z")) != []
        assert list(audit.iter_records(start=future)) == []

    def test_export(self, tmp_path):
        audit = AuditLog(tmp_path, 180, ())
        audit.write("gate", "gate_decision", result="block")
        out = audit.export(tmp_path / "export.jsonl", stream="gate")
        lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["result"] == "block"

    def test_purge_old(self, tmp_path):
        old = tmp_path / "gate-2020-01-01.jsonl"
        old.write_text('{"ts":"2020-01-01T00:00:00Z"}\n', encoding="utf-8")
        audit = AuditLog(tmp_path, 180, ())
        removed = audit.purge_old()
        assert removed == 1
        assert not old.exists()

    def test_purge_ignores_unparsable_names(self, tmp_path):
        keep = tmp_path / "notes.jsonl"
        keep.write_text("{}\n", encoding="utf-8")
        audit = AuditLog(tmp_path, 1, ())
        assert audit.purge_old() == 0
        assert keep.exists()


class TestDailySummary:
    def test_summary_counts(self, tmp_path):
        audit = AuditLog(tmp_path, 180, ())
        audit.write("label", "label_written", pair="BTC/USDT", action="block_new_entries")
        audit.write("label", "label_written", pair="ETH/USDT", action="allow")
        audit.write("gate", "gate_decision", result="block")
        audit.write("execution", "order_submitted")
        audit.write("execution", "cli_failed")
        audit.write("error", "exception")
        audit.write("readonly", "readonly_check", status="ok")
        audit.write("gate", "halt_detected")

        summary = daily_summary(tmp_path)
        assert summary.label_count == 2
        assert summary.label_actions == {"block_new_entries": 1, "allow": 1}
        assert summary.gate_results == {"block": 1}
        assert summary.order_count == 1
        assert summary.cli_failures == 1
        assert summary.error_count == 1
        assert summary.readonly_check == "ok"
        assert summary.halt_events == 1
        assert "审计摘要" in summary.render_markdown()

    def test_summary_empty_day(self, tmp_path):
        summary = daily_summary(tmp_path, "2000-01-01")
        assert summary.label_count == 0
        assert json.loads(json.dumps(summary.to_dict()))["day"] == "2000-01-01"
