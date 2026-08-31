from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class TradeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    ADJUST_SL = "ADJUST_SL"


class PositionDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class Candle(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    complete: bool = True


class MarketPrice(BaseModel):
    instrument: str
    time: str
    bid: float
    ask: float
    spread_pips: float
    mid: float


class IndicatorValues(BaseModel):
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_50: Optional[float] = None
    ema_200: Optional[float] = None
    rsi_14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    atr_14: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    stoch_k: Optional[float] = None
    stoch_d: Optional[float] = None
    vwap: Optional[float] = None
    vwap_upper: Optional[float] = None
    vwap_lower: Optional[float] = None
    rvol: Optional[float] = None
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    trend_bias: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    volatility_regime: str = "NORMAL"  # LOW, NORMAL, HIGH, EXPANDING


class MarketStructure(BaseModel):
    swing_high: Optional[float] = None
    swing_low: Optional[float] = None
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    fvg_bullish: bool = False
    fvg_bearish: bool = False
    orb_bias: Optional[str] = None  # ABOVE_ORB_HIGH, BELOW_ORB_LOW, INSIDE_RANGE
    active_session: str = "GLOBAL"  # LONDON, NEW_YORK, US_RTH, US_PRE_MARKET, etc.


class GeminiTradeDecision(BaseModel):
    action: TradeAction = TradeAction.HOLD
    instrument: str
    confidence: float = Field(default=0.0, ge=0.0, le=100.0)
    reasoning: str = ""
    thesis_summary: str = ""
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    risk_reward_ratio: Optional[float] = None
    invalidation_level: Optional[float] = None
    suggested_risk_pct: float = 1.0
    setup_type: str = "NONE"  # TREND_CONTINUATION, PULLBACK, RANGE_FADE, BREAKOUT, S/R_BOUNCE
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Position(BaseModel):
    id: str
    instrument: str
    direction: PositionDirection
    units: int
    entry_price: float
    current_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    open_time: str
    close_time: Optional[str] = None
    status: PositionStatus = PositionStatus.OPEN
    close_reason: Optional[str] = None  # TP_HIT, SL_HIT, MANUAL, AGENT_SIGNAL, CIRCUIT_BREAKER
    r_multiple: Optional[float] = None


class OrderRequest(BaseModel):
    instrument: str
    direction: PositionDirection
    units: int
    order_type: str = "MARKET"  # MARKET, LIMIT
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_distance_pips: Optional[float] = None


class AccountSummary(BaseModel):
    account_id: str
    currency: str = "EUR"
    balance: float = 10000.0
    unrealized_pl: float = 0.0
    realized_pl: float = 0.0
    equity: float = 10000.0
    margin_used: float = 0.0
    margin_available: float = 10000.0
    open_positions_count: int = 0
    daily_drawdown_pct: float = 0.0
    daily_start_equity: float = 10000.0


class TradePerformanceStats(BaseModel):
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0


class AgentTelemetry(BaseModel):
    timestamp: str
    is_running: bool
    mode: str  # simulator, practice, live
    current_instrument: str
    market_price: Optional[MarketPrice] = None
    indicators: Optional[IndicatorValues] = None
    market_structure: Optional[MarketStructure] = None
    last_decision: Optional[GeminiTradeDecision] = None
    open_positions: List[Position] = []
    account: AccountSummary
    stats: TradePerformanceStats
    recent_logs: List[Dict[str, Any]] = []
