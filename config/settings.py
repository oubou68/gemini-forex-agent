import logging
import os
import secrets
from pathlib import Path
from typing import List, Dict, Any, Optional
import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator

BASE_DIR = Path(__file__).resolve().parent.parent
_settings_logger = logging.getLogger(__name__)
_WEAK_SECRET_PLACEHOLDERS = {
    "",
    "generate_a_random_secret_string_here",
    "aura_quant_sec_token_98f4e2b8109d7c6a5e4c3b2a10",
    "changeme",
    "secret",
}

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Gemini AI
    GEMINI_API_KEY: str = Field(default="", description="Google Gemini API Key")
    GEMINI_MODEL: str = Field(default="gemini-2.5-flash", description="Gemini Model Identifier")
    GEMINI_RESTRICT_TO_TRADING_HOURS: bool = Field(default=True, description="Restrict Gemini LLM queries to active market hours")
    GEMINI_STOCK_REGULAR_HOURS_ONLY: bool = Field(default=False, description="Restrict stock Gemini queries strictly to RTH (09:30-16:00 EST)")
    
    # OANDA v20 Broker (Forex)
    OANDA_API_KEY: str = Field(default="", description="OANDA v20 API Key")
    OANDA_ACCOUNT_ID: str = Field(default="", description="OANDA v20 Account ID")
    OANDA_ENVIRONMENT: str = Field(default="simulator", description="practice, live, or simulator")
    
    # Alpaca Broker (Stocks - Authentication per https://docs.alpaca.markets/us/docs/authentication)
    ALPACA_API_KEY: str = Field(default="", description="Alpaca API Key ID (APCA-API-KEY-ID)")
    ALPACA_SECRET_KEY: str = Field(default="", description="Alpaca Secret Key (APCA-API-SECRET-KEY)")
    ALPACA_ENVIRONMENT: str = Field(default="simulator", description="paper, live, or simulator")
    ALPACA_OAUTH_TOKEN: str = Field(default="", description="Optional Alpaca OAuth Bearer Access Token")
    ALPACA_PAPER_URL: str = Field(default="https://paper-api.alpaca.markets", description="Alpaca Paper Trading Endpoint")
    ALPACA_LIVE_URL: str = Field(default="https://api.alpaca.markets", description="Alpaca Live Trading Endpoint")
    ALPACA_DATA_URL: str = Field(default="https://data.alpaca.markets", description="Alpaca Market Data Endpoint")
    ALPACA_DATA_FEED: str = Field(default="iex", description="Market data feed: iex (free) or sip (paid)")
    
    # Alpaca Standard Aliases
    APCA_API_KEY_ID: Optional[str] = Field(default=None, description="Standard Alpaca SDK Alias for Key ID")
    APCA_API_SECRET_KEY: Optional[str] = Field(default=None, description="Standard Alpaca SDK Alias for Secret Key")
    APCA_API_BASE_URL: Optional[str] = Field(default=None, description="Standard Alpaca SDK Alias for Base URL")

    # Trading Defaults (Forex)
    DEFAULT_INSTRUMENT: str = Field(default="EUR_USD")
    SCAN_INTERVAL_SECONDS: int = Field(default=10)
    RISK_PERCENT_PER_TRADE: float = Field(default=1.0)
    MAX_DAILY_DRAWDOWN_PERCENT: float = Field(default=3.0)
    MAX_OPEN_POSITIONS: int = Field(default=3)
    SPREAD_LIMIT_PIPS: float = Field(default=2.5)
    
    # Trading Defaults (Stocks)
    DEFAULT_STOCK_SYMBOL: str = Field(default="AAPL")
    STOCK_SCAN_INTERVAL_SECONDS: int = Field(default=10)
    STOCK_RISK_PERCENT_PER_TRADE: float = Field(default=1.0)
    STOCK_MAX_DAILY_DRAWDOWN_PERCENT: float = Field(default=3.0)
    STOCK_MAX_OPEN_POSITIONS: int = Field(default=20)

    # Trading Defaults (Crypto - Alpaca 24/7 Spot)
    DEFAULT_CRYPTO_SYMBOL: str = Field(default="BTC/USD")
    CRYPTO_SCAN_INTERVAL_SECONDS: int = Field(default=15)
    CRYPTO_RISK_PERCENT_PER_TRADE: float = Field(default=1.25, description="Medium-risk volatility-targeted risk % per crypto trade")
    CRYPTO_MAX_DAILY_DRAWDOWN_PERCENT: float = Field(default=4.0, description="Crypto daily drawdown circuit breaker %")
    CRYPTO_MAX_OPEN_POSITIONS: int = Field(default=6, description="Max concurrent crypto spot positions")
    CRYPTO_MAX_PORTFOLIO_EXPOSURE_PCT: float = Field(default=65.0, description="Max % of equity deployed across all crypto positions (keeps >=35% USD cash buffer)")
    ALPACA_CRYPTO_LOC: str = Field(default="us", description="Alpaca Crypto data location (us, us-1, eu-1)")
    
    # Advanced Risk & Guardrail Configuration
    ALLOW_AI_CLOSE_SIGNALS: bool = Field(default=True, description="Allow Gemini to send premature CLOSE orders before SL/TP")
    AUTO_LIQUIDATE_ON_DRAWDOWN: bool = Field(default=False, description="Auto-close all active trades when daily drawdown limit is hit")
    DEFAULT_ATR_MULTIPLIER_SL: float = Field(default=1.5, description="Stop-Loss distance ATR multiplier")
    DEFAULT_ATR_MULTIPLIER_TP: float = Field(default=2.5, description="Take-Profit target ATR multiplier")
    MIN_RISK_REWARD_RATIO: float = Field(default=1.5, description="Minimum RRR required for trade execution")
    BREAKEVEN_TRIGGER_R: float = Field(default=1.0, description="R profit multiple to move Stop-Loss to Breakeven")
    TRAILING_STOP_ENABLED: bool = Field(default=True, description="Enable Breakeven and Trailing Stop updates")
    DYNAMIC_RISK_SCALING: bool = Field(default=True, description="Scale trade risk by confidence, drawdown, and recent streak")
    LOSS_COOLDOWN_SECONDS: int = Field(default=300, description="Cooldown per instrument after a stop-loss hit to prevent whipsaw churn")
    MIN_ADX_TREND_STRENGTH: float = Field(default=18.0, description="Minimum ADX(14) trend strength for trend-following entries")
    MULTI_INSTRUMENT_SCAN_ENABLED: bool = Field(default=True, description="Autonomously scan all monitored instruments / top screener setups")
    MAX_GEMINI_CALLS_PER_CYCLE: int = Field(default=2, description="Cap Gemini LLM API calls per autonomous scan cycle")
    
    # Server
    SERVER_HOST: str = Field(default="0.0.0.0")
    SERVER_PORT: int = Field(default=8000)
    CORS_ALLOWED_ORIGINS: str = Field(
        default="http://localhost:8000,http://127.0.0.1:8000",
        description="Comma-separated list of allowed CORS origins"
    )
    ENABLE_API_DOCS: bool = Field(default=False, description="Expose Swagger/OpenAPI docs publicly")

    # Security & Authentication
    AUTH_ENABLED: bool = Field(default=True, description="Enable authentication for Web and API")
    AUTH_USERNAME: str = Field(default="admin", description="Admin username for local login")
    AUTH_PASSWORD: str = Field(default="AuraQuant_2026!TradeSafe", description="Admin password or PBKDF2 hash (override in .env!)")
    AUTH_SECRET_KEY: str = Field(default_factory=lambda: secrets.token_hex(32), description="Secret key for signing session tokens")
    AUTH_SESSION_EXPIRE_HOURS: int = Field(default=72, description="Session expiration in hours")
    AUTH_COOKIE_SECURE: Optional[bool] = Field(default=None, description="Force Secure cookie flag (None = auto-detect HTTPS)")
    AUTH_RATE_LIMIT_MAX_ATTEMPTS: int = Field(default=10, description="Max failed login attempts per IP within rate limit window")
    AUTH_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=300, description="Login rate limit window in seconds")
    GOOGLE_ALLOWED_EMAIL: str = Field(default="oubou68@gmail.com", description="Restricted email address for Google Ident")
    GOOGLE_CLIENT_ID: str = Field(default="", description="Optional Google OAuth2 Client ID for Google Identity Services")

    @model_validator(mode="after")
    def resolve_alpaca_auth_aliases(self) -> "Settings":
        # Resolve Key ID
        if not self.ALPACA_API_KEY and self.APCA_API_KEY_ID:
            self.ALPACA_API_KEY = self.APCA_API_KEY_ID
        elif not self.APCA_API_KEY_ID and self.ALPACA_API_KEY:
            self.APCA_API_KEY_ID = self.ALPACA_API_KEY
            
        # Resolve Secret Key
        if not self.ALPACA_SECRET_KEY and self.APCA_API_SECRET_KEY:
            self.ALPACA_SECRET_KEY = self.APCA_API_SECRET_KEY
        elif not self.APCA_API_SECRET_KEY and self.ALPACA_SECRET_KEY:
            self.APCA_API_SECRET_KEY = self.ALPACA_SECRET_KEY

        # Replace weak/placeholder signing secret with a strong cryptographic key
        if (self.AUTH_SECRET_KEY or "").strip() in _WEAK_SECRET_PLACEHOLDERS or len(self.AUTH_SECRET_KEY.strip()) < 24:
            self.AUTH_SECRET_KEY = secrets.token_hex(32)
            _settings_logger.warning(
                "SECURITY: Weak or placeholder AUTH_SECRET_KEY detected — generated ephemeral 256-bit key. "
                "Set a strong random AUTH_SECRET_KEY in .env for persistent sessions across restarts."
            )

        return self

    def get_cors_origins(self) -> List[str]:
        origins = [o.strip() for o in (self.CORS_ALLOWED_ORIGINS or "").split(",") if o.strip()]
        return origins or ["http://localhost:8000", "http://127.0.0.1:8000"]

    def get_alpaca_key(self) -> str:
        return self.ALPACA_API_KEY or self.APCA_API_KEY_ID or ""

    def get_alpaca_secret(self) -> str:
        return self.ALPACA_SECRET_KEY or self.APCA_API_SECRET_KEY or ""

    def get_alpaca_base_url(self, environment: Optional[str] = None) -> str:
        if self.APCA_API_BASE_URL:
            return self.APCA_API_BASE_URL
        env = (environment or self.ALPACA_ENVIRONMENT).lower()
        if env == "live":
            return self.ALPACA_LIVE_URL
        return self.ALPACA_PAPER_URL


def load_yaml_config() -> Dict[str, Any]:
    config_path = BASE_DIR / "config" / "default_config.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


settings = Settings()
yaml_config = load_yaml_config()
