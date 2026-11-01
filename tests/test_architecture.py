"""架构约束测试（FR-2.1 / FR-3.3：实盘路径不得引入验证模块与 LLM 调用）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def import_probe(code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["AIQUANT_STORE"] = "memory"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


class TestLivePathIsolation:
    def test_gate_does_not_import_validation(self):
        code = (
            "import sys\n"
            "import aiquant.gate.decision as d\n"
            "bad = [m for m in sys.modules if m.startswith('aiquant.validation')]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        proc = import_probe(code)
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_gate_does_not_import_llm_or_http(self):
        code = (
            "import sys\n"
            "import aiquant.gate.decision as d\n"
            "bad = [m for m in sys.modules if m.startswith(('aiquant.label_service.llm', 'httpx'))]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        proc = import_probe(code)
        assert proc.returncode == 0, proc.stderr

    def test_execution_does_not_import_llm(self):
        code = (
            "import sys\n"
            "import aiquant.execution.okx_cli as x\n"
            "bad = [m for m in sys.modules if m.startswith('aiquant.label_service.llm')]\n"
            "assert not bad, bad\n"
            "print('ok')\n"
        )
        proc = import_probe(code)
        assert proc.returncode == 0, proc.stderr

    def test_strategy_module_imports_without_freqtrade(self):
        code = (
            "import aiquant.execution.freqtrade_strategy as s\n"
            "assert hasattr(s, 'AiQuantGateStrategy')\n"
            "print('ok')\n"
        )
        proc = import_probe(code)
        assert proc.returncode == 0, proc.stderr

    def test_validation_is_separately_importable(self):
        code = (
            "import aiquant.validation.replay as r\n"
            "import aiquant.validation.report as p\n"
            "import aiquant.validation.wfa as w\n"
            "print('ok')\n"
        )
        proc = import_probe(code)
        assert proc.returncode == 0, proc.stderr


class TestRepoHygiene:
    def test_no_hardcoded_credentials_in_source(self):
        needles = ("sk-live", "AKIA", "-----BEGIN RSA PRIVATE KEY-----")
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.name}:{needle}")
        assert offenders == []

    def test_label_service_requires_readonly_cmd(self):
        from aiquant.settings import Settings

        assert "--read-only" in Settings.model_fields["okx_mcp_readonly_cmd"].default

    @pytest.mark.parametrize(
        "module",
        [
            "aiquant.gate.service",
            "aiquant.gate.guarder",
            "aiquant.label_service.main",
            "aiquant.execution.okx_cli",
            "aiquant.validation.cli",
        ],
    )
    def test_modules_import_cleanly(self, module):
        proc = import_probe(f"import {module}\nprint('ok')\n")
        assert proc.returncode == 0, proc.stderr
