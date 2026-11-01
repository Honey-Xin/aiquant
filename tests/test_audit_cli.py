"""审计运维命令行测试（purge / export / summary）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aiquant.audit import cli
from aiquant.audit.logger import AuditLog


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "logs", retention_days=180)


def _write_shard(log_dir, day: str, stream: str = "audit") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{stream}-{day}.jsonl"
    record = {
        "ts": f"{day}T00:00:00+00:00",
        "stream": stream,
        "event": "label_written",
        "action": "allow",
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_purge_dry_run_lists_candidates(tmp_path, capsys):
    log_dir = tmp_path / "logs"
    old = (datetime.now(timezone.utc).date() - timedelta(days=400)).isoformat()
    _write_shard(log_dir, old)
    code = cli.run(["--log-dir", str(log_dir), "purge", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "candidate" in out
    assert any(log_dir.iterdir())


def test_purge_removes_old_shards(tmp_path, capsys):
    log_dir = tmp_path / "logs"
    old = (datetime.now(timezone.utc).date() - timedelta(days=400)).isoformat()
    fresh = datetime.now(timezone.utc).date().isoformat()
    _write_shard(log_dir, old)
    _write_shard(log_dir, fresh, stream="gate")
    assert cli.run(["--log-dir", str(log_dir), "purge"]) == 0
    assert "删除 1 个分片" in capsys.readouterr().out
    assert [p.name for p in log_dir.iterdir()] == [f"gate-{fresh}.jsonl"]


def test_export_writes_jsonl(tmp_path, capsys):
    log_dir = tmp_path / "logs"
    today = datetime.now(timezone.utc).date().isoformat()
    _write_shard(log_dir, today, stream="gate")
    out = tmp_path / "dump.jsonl"
    assert cli.run(["--log-dir", str(log_dir), "export", "--out", str(out), "--stream", "gate"]) == 0
    assert "已导出 1 条记录" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["event"] == "label_written"


def test_summary_prints_markdown(tmp_path, capsys):
    log_dir = tmp_path / "logs"
    today = datetime.now(timezone.utc).date().isoformat()
    _write_shard(log_dir, today)
    assert cli.run(["--log-dir", str(log_dir), "summary", "--day", today]) == 0
    out = capsys.readouterr().out
    assert "审计摘要" in out and "标签数量" in out


def test_summary_defaults_to_today(tmp_path, capsys):
    assert cli.run(["--log-dir", str(tmp_path / "empty"), "summary"]) == 0
    assert "审计摘要" in capsys.readouterr().out


def test_main_reports_bad_arguments(capsys):
    assert cli.main(["--log-dir", ".", "summary", "--day", "not-a-date"]) == 2
    assert "参数错误" in capsys.readouterr().err


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])
