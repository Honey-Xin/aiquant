"""pytest 公共夹具。

关键点：所有测试都用 ``MemoryStore`` + 注入的假传输层，不依赖真实 Redis / OKX /
LLM。审计目录指向 tmp_path，避免污染仓库。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from aiquant.label_service.store.memory_store import MemoryStore  # noqa: E402
from aiquant.settings import Settings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AIQUANT_STORE", "memory")
    monkeypatch.delenv("GATE_ENABLED", raising=False)
    monkeypatch.delenv("LABEL_SERVICE_URL", raising=False)
    yield


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def settings_factory(tmp_path):
    def _make(**overrides) -> Settings:
        base: dict[str, object] = {
            "audit_log_dir": str(tmp_path / "logs"),
            "gate_api_token": "test-token",
            "label_pairs": "BTC/USDT,ETH/USDT",
            "label_ttl_sec": 300,
            "label_cycle_sec": 60,
            "gate_min_amount_json": '{"BTC/USDT": 0.001, "ETH/USDT": 0.002}',
            "label_service_url": "http://label.test:8081",
            "freqtrade_api_url": "http://ft.test:8082",
            "freqtrade_api_user": "freqtrader",
            "freqtrade_api_password": "ft-pass",
            "redis_url": "redis://127.0.0.1:6379/15",
        }
        base.update(overrides)
        return Settings(**base)

    return _make
