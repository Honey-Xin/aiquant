"""审计运维命令行（保留期清理 / 导出 / 日报）。

用法：

    python -m aiquant.audit.cli purge                 # 清理超过保留期的分片
    python -m aiquant.audit.cli export --out dump.jsonl
    python -m aiquant.audit.cli summary --day 2026-01-01

所有子命令只读写 ``AUDIT_LOG_DIR``，不接触交易接口。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from aiquant.settings import Settings, get_settings

from .logger import AuditLog, daily_summary, redact

__all__ = ["build_parser", "main", "run"]

_SHARD_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiquant.audit.cli", description="审计日志运维命令")
    parser.add_argument("--log-dir", default=None, help="覆盖 AUDIT_LOG_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    purge = sub.add_parser("purge", help="删除超过保留期的分片")
    purge.add_argument("--retention-days", type=int, default=None)
    purge.add_argument("--dry-run", action="store_true", help="只列出将被删除的文件")

    export = sub.add_parser("export", help="导出合并后的 JSONL")
    export.add_argument("--out", required=True)
    export.add_argument("--stream", default=None, help="只导出某一事件流（label/gate/cli/audit…）")
    export.add_argument("--start", default=None, help="YYYY-MM-DD")
    export.add_argument("--end", default=None, help="YYYY-MM-DD")

    summary = sub.add_parser("summary", help="打印某日日报（Markdown）")
    summary.add_argument("--day", default=None, help="YYYY-MM-DD，默认今天（UTC）")
    return parser


def _settings_and_dir(args: argparse.Namespace) -> tuple[Settings, Path]:
    settings = get_settings()
    log_dir = Path(args.log_dir) if args.log_dir else Path(settings.audit_log_dir)
    return settings, log_dir


def _expired_shards(log_dir: Path, retention_days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    expired: list[Path] = []
    if not log_dir.exists():
        return expired
    for path in sorted(log_dir.glob("*.jsonl")):
        m = _SHARD_DATE_RE.search(path.stem)
        if not m:
            continue
        try:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if day < cutoff:
            expired.append(path)
    return expired


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings, log_dir = _settings_and_dir(args)

    if args.command == "purge":
        retention = args.retention_days or settings.audit_retention_days
        if args.dry_run:
            candidates = _expired_shards(log_dir, retention)
            print(f"dry-run：保留 {retention} 天，目录 {log_dir}，待删 {len(candidates)} 个分片")
            for path in candidates:
                print(f"  candidate {path.name}")
            return 0
        log = AuditLog(log_dir, retention_days=retention, secrets=settings.secrets())
        removed = log.purge_old()
        print(f"purge 完成：删除 {removed} 个分片（保留 {retention} 天）")
        return 0

    if args.command == "export":
        log = AuditLog(log_dir, retention_days=settings.audit_retention_days, secrets=settings.secrets())
        out = log.export(Path(args.out), stream=args.stream, start=args.start, end=args.end)
        lines = sum(1 for _ in out.open("r", encoding="utf-8"))
        print(f"已导出 {lines} 条记录到 {out}")
        return 0

    day = args.day or datetime.now(timezone.utc).date().isoformat()
    report = daily_summary(log_dir, day)
    print(redact(report.render_markdown(), settings.secrets()))
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - 入口
    try:
        return run(argv)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
