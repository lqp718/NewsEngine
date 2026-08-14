"""NewsEngine configuration management using Pydantic Settings.

Loads configuration from .env file with validation and type safety.
Provides a global singleton for accessing configuration throughout the application.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Define project root directory - used for loading .env file
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Application settings loaded from .env file and environment variables."""
    
    # === 阿里百炼 API (LLM + Embedding) ===
    bailian_api_key: str = Field(
        ...,
        description="阿里百炼 API Key - 必填字段，无默认值",
    )

    # === Graphiti LLM Provider ===
    graphiti_llm_provider: str = Field(
        "openai",
        description="Graphiti LLM provider: 'gemini' or 'openai' (百炼)",
    )

    # === Google Gemini API ===
    gemini_api_key: str = Field(
        "",
        description="Google Gemini API Key (Google AI Studio)",
    )
    gemini_model: str = Field(
        "gemini-2.5-flash",
        description="Gemini LLM model name (default: gemini-2.5-flash)",
    )
    openai_base_url: str = Field(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="百炼 OpenAI 兼容 Base URL",
    )
    embedding_model: str = Field(
        "text-embedding-v4",
        description="百炼 Embedding 模型名",
    )
    llm_model: str = Field(
        "qwen3.7-plus",
        description="百炼 LLM 模型名",
    )

    # === DeepSeek API (LLM) ===
    deepseek_api_key: str = Field(
        ...,
        description="DeepSeek API Key，用于 LLM 客户端",
    )
    deepseek_base_url: str = Field(
        "https://api.deepseek.com",
        description="DeepSeek API Base URL",
    )
    deepseek_model: str = Field(
        "deepseek-v4-flash",
        description="DeepSeek 模型名",
    )
    
    # === Neo4j 连接 ===
    neo4j_uri: str = Field(
        "bolt://localhost:7687",
        description="Neo4j Bolt URI",
    )
    neo4j_user: str = Field(
        "neo4j",
        description="Neo4j 用户名",
    )
    neo4j_password: str = Field(
        "newsengine2026",
        description="Neo4j 密码",
    )
    
    # === FastAPI 服务 ===
    api_host: str = Field(
        "0.0.0.0",
        description="FastAPI 监听地址",
    )
    api_port: int = Field(
        8100,
        description="FastAPI 监听端口",
    )
    
    # === SynapseEngine 连接 ===
    synapse_base_url: str = Field(
        "http://localhost:8000",
        description="SynapseEngine REST API 地址",
    )
    ticker_whitelist_file: str = Field(
        "data/ticker_whitelist.json",
        description="Ticker 白名单本地缓存文件路径",
    )
    
    # === 日志 ===
    log_level: str = Field(
        "INFO",
        description="日志级别",
    )
    log_file: str = Field(
        "logs/news_engine.log",
        description="日志文件路径",
    )
    
    # === 数据摄取 ===
    ingestion_interval_sec: int = Field(
        900,
        description="数据源轮询间隔（秒）",
    )
    
    # === GDELT ===
    gdelt_lastupdate_url: str = Field(
        "http://data.gdeltproject.org/gdeltv2/lastupdate.txt",
        description="GDELT V2 lastupdate.txt URL",
    )
    gdelt_max_retries: int = Field(
        3,
        description="GDELT 下载重试次数",
    )
    gdelt_timeout_sec: int = Field(
        60,
        description="GDELT HTTP 超时（秒）",
    )
    
    # === RSS ===
    rss_timeout_sec: int = Field(
        30,
        description="RSS HTTP 超时（秒）",
    )
    
    # === AkShare ===
    akshare_request_interval_sec: float = Field(
        0.5,
        description="AkShare 查询间隔（秒）",
    )

    # === EastMoney ===
    eastmoney_page_size: int = Field(
        20,
        description="EastMoney 个股新闻每页条数（默认 20 条）",
    )
    
    # === Risk Summary 缓存 ===
    risk_summary_cache_ttl_sec: int = Field(
        300,
        description="Risk Summary 缓存 TTL（秒）",
    )

    # === Episode TTL 淘汰 (V2.2 新增) ===
    episode_ttl_macro_days: int = Field(
        14,
        description="MACRO scope Episode 保留天数（宏观事件，V2.2）",
    )
    episode_ttl_sector_days: int = Field(
        7,
        description="SECTOR scope Episode 保留天数（行业事件，V2.2）",
    )
    episode_ttl_symbol_days: int = Field(
        3,
        description="SYMBOL scope Episode 保留天数（个股事件，V2.2）",
    )
    ttl_cleanup_interval_hours: int = Field(
        24,
        description="TTL 清理间隔（小时），V2.2",
    )

    # === GDELT 宏观主题 (V2.2 新增) ===
    gdelt_macro_themes_file: str = Field(
        "src/adapters/macro_themes.py",
        description="GDELT 宏观主题白名单常量文件路径，V2.2",
    )
    
    # === News Age Filter ===
    news_max_age_days: int = Field(
        14,
        description="Maximum age of news articles in days (older articles are discarded)",
    )

    # === 宏观数据源认证 (Phase 1, add-phase1-macro-adapters) ===
    # FRED / EIA 需免费注册 API key；ACLED 使用账号密码（OAuth2 password-grant）。
    # 未配置时对应 adapter 优雅降级（fetch 返回空列表 + warning）。
    # OFAC SDN / OpenSanctions / BLS 无需 API key。
    fred_api_key: str = Field(
        "",
        description="FRED API Key（免费注册，https://fred.stlouisfed.org/docs/api/api_key.html）",
    )
    acled_username: str = Field(
        "",
        description="ACLED 用户名（邮箱）",
    )
    acled_password: str = Field(
        "",
        description="ACLED 密码",
    )
    eia_api_key: str = Field(
        "",
        description="EIA API Key（免费注册，https://www.eia.gov/opendata/register.php）",
    )

    # === 宏观数据源超时 (Phase 1, add-phase1-macro-adapters) ===
    fred_timeout_sec: int = Field(
        30,
        description="FRED HTTP 超时（秒）",
    )
    acled_timeout_sec: int = Field(
        30,
        description="ACLED HTTP 超时（秒）",
    )
    eia_timeout_sec: int = Field(
        30,
        description="EIA HTTP 超时（秒）",
    )
    bls_timeout_sec: int = Field(
        30,
        description="BLS HTTP 超时（秒）",
    )
    open_sanctions_timeout_sec: int = Field(
        30,
        description="OpenSanctions/OFAC HTTP 超时（秒）",
    )
    open_sanctions_api_key: str | None = Field(
        None,
        description="OpenSanctions API key（付费）。未配置时跳过 OpenSanctions，直接使用 OFAC SDN。",
    )

    # === Scheduler Cycle Guard (V2.3) ===
    min_cycle_gap_sec: int = Field(
        60,
        description="相邻 ingestion cycle 之间的最小冷却间隔（秒）。默认 60 秒，设为 0 可禁用。",
    )

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore undefined fields in .env for forward compatibility
    )
    
    @field_validator("min_cycle_gap_sec")
    @classmethod
    def validate_min_cycle_gap(cls, v: int) -> int:
        """Validate that min_cycle_gap_sec is not negative."""
        if v < 0:
            raise ValueError("min_cycle_gap_sec 必须 >= 0")
        return v

    @field_validator("bailian_api_key")
    @classmethod
    def validate_bailian_api_key(cls, v: str) -> str:
        """Validate that bailian_api_key is not a placeholder value."""
        if not v:
            raise ValueError("BAILIAN_API_KEY 必须设置为真实的百炼 API Key，不可为空")
        
        # Check for common placeholder values
        placeholder_values = ["***", "sk-***", "your-api-key", "YOUR_API_KEY", "your_api_key", "sk-your-api-key"]
        if v.strip() in placeholder_values:
            raise ValueError("BAILIAN_API_KEY 必须设置为真实的百炼 API Key，不可使用占位符")
        
        return v
    
    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate that log_level is a valid Python logging level (case-insensitive)."""
        valid_levels = {"debug", "info", "warning", "error", "critical"}
        if v.lower() not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}, got '{v}'")
        
        # Return uppercase version for consistency
        return v.upper()


# Global settings instance - initialized lazily
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get the global settings instance (lazy initialization)."""
    global _settings
    if _settings is None:
        _settings = Settings()
        logging.getLogger(__name__).info("Settings loaded successfully")
    return _settings


def reload_settings() -> Settings:
    """Reload the global settings instance (useful for testing or runtime updates)."""
    global _settings
    _settings = Settings()
    logging.getLogger(__name__).info("Settings reloaded successfully")
    return _settings


__all__ = ["Settings", "get_settings", "reload_settings", "PROJECT_ROOT"]