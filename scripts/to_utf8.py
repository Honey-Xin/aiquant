"""一次性工具：把仓库内被 GBK/CP936 写入的文本文件统一转换成 UTF-8。

背景：部分源码文件在 Windows PowerShell 环境下以 CP936 落盘，Python 以 UTF-8
读取时会出现乱码（日志/日报中的中文尤其明显）。本脚本只做编码归一，不改内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt", ".ps1", ".cfg", ".ini"}
NAMES = {".env.example", ".gitignore"}

ROOT = Path(__file__).resolve().parents[1]


def candidates() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "__pycache__", ".pytest_cache", "htmlcov", "logs"} for part in path.parts):
            continue
        if path.suffix in SUFFIXES or path.name in NAMES:
            out.append(path)
    return out


def convert() -> list[tuple[str, str]]:
    report: list[tuple[str, str]] = []
    for path in candidates():
        raw = path.read_bytes()
        if not raw:
            continue
        try:
            raw.decode("utf-8")
            continue  # 已经是 UTF-8（或纯 ASCII）
        except UnicodeDecodeError:
            pass
        for encoding in ("gbk", "cp936", "latin-1"):
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            path.write_text(text, encoding="utf-8", newline="\n")
            report.append((str(path.relative_to(ROOT)), encoding))
            break
    return report


if __name__ == "__main__":
    changes = convert()
    for name, enc in changes:
        print(f"{name}  {enc} -> utf-8")
    print(f"转换完成：{len(changes)} 个文件")
    sys.exit(0)
