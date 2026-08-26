"""Tests for local provider support and configuration externalization.

Covers:
- `GRAPHITI_LLM_PROVIDER=local` settings (uses OPENAI_* config)
- Episode-level semaphore `_LLM_SEMAPHORE` from settings
- Circuit/429 backoff params from settings
- `_check_local_llm_health` existence in scheduler
- `_seed_semaphore_limit_env` / `_ensure_graphiti_indices` wiring
"""
import pytest
from unittest.mock import patch, MagicMock

from src.core.config import get_settings


def test_local_provider_config():
    """Verify local provider uses OPENAI_* config."""
    from src.core.config import reload_settings
    try:
        with patch.dict('os.environ', {
            'GRAPHITI_LLM_PROVIDER': 'local',
            'OPENAI_API_KEY': 'sk-local-test-key',  # 真实格式（*** 会被占位符校验拒绝）
            'OPENAI_BASE_URL': 'http://127.0.0.1:8080/v1',
            'LLM_MODEL': 'gemma4-12b',
        }, clear=False):
            # get_settings() 全局缓存单例：reload 强制从 patch 后的 env 重读，
            # 避免其他测试先导入 episode_writer 时已缓存默认值。
            reload_settings()
            settings = get_settings()
            assert settings.graphiti_llm_provider == "local"
            assert settings.openai_base_url == "http://127.0.0.1:8080/v1"
    finally:
        # 恢复：env 还原后再 reload 回 .env 值，避免污染后续测试
        reload_settings()


def test_episode_semaphore_from_settings():
    """Verify _LLM_SEMAPHORE uses settings value."""
    from src.graphiti.episode_writer import _LLM_SEMAPHORE
    # Default should be 3
    assert _LLM_SEMAPHORE._value == 3


def test_circuit_params_from_settings():
    """Verify circuit params use settings values."""
    from src.graphiti.episode_writer import (
        _CIRCUIT_MAX_CONSECUTIVE_429,
        _CIRCUIT_COOLDOWN_SEC,
        _MIN_429_BACKOFF_SEC,
    )
    assert _CIRCUIT_MAX_CONSECUTIVE_429 == 3
    assert _CIRCUIT_COOLDOWN_SEC == 60.0
    assert _MIN_429_BACKOFF_SEC == 37.0


def test_health_check_function_exists():
    """Verify _check_local_llm_health function exists."""
    from src.ingestion.scheduler import _check_local_llm_health
    assert callable(_check_local_llm_health)


def test_health_check_rejects_unreachable_server():
    """Verify health check raises when llama-server is unreachable."""
    from src.ingestion.scheduler import _check_local_llm_health
    with patch("httpx.get", side_effect=ConnectionError("refused")):
        with pytest.raises(RuntimeError, match="Local LLM not ready"):
            _check_local_llm_health("http://127.0.0.1:8080/v1")


def test_semaphore_limit_env_seeding():
    """Verify SEMAPHORE_LIMIT is seeded to process env from settings."""
    import os
    from src.ingestion.scheduler import _seed_semaphore_limit_env
    with patch.dict('os.environ', {}, clear=False):
        os.environ.pop("SEMAPHORE_LIMIT", None)
        _seed_semaphore_limit_env()
        assert os.environ.get("SEMAPHORE_LIMIT") == "3"