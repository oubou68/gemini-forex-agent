import pytest
from datetime import datetime, timedelta
from agent.core.models import Candle
from agent.analysis.indicators import TechnicalIndicatorEngine
from agent.analysis.market_structure import MarketStructureAnalyzer


def generate_mock_candles(count: int = 60, trend: str = "up") -> list[Candle]:
    candles = []
    base_price = 1.0800
    now = datetime.utcnow()
    for i in range(count):
        step = 0.0002 if trend == "up" else -0.0002
        c_open = base_price + (i * step)
        c_close = c_open + (step * 0.8)
        c_high = max(c_open, c_close) + 0.0001
        c_low = min(c_open, c_close) - 0.0001
        t_str = (now - timedelta(minutes=(count - i) * 5)).isoformat()
        candles.append(Candle(
            time=t_str,
            open=round(c_open, 5),
            high=round(c_high, 5),
            low=round(c_low, 5),
            close=round(c_close, 5),
            volume=500
        ))
    return candles


def test_indicator_calculation():
    candles = generate_mock_candles(count=60, trend="up")
    ind = TechnicalIndicatorEngine.calculate_indicators(candles)

    assert ind.ema_9 is not None
    assert ind.ema_21 is not None
    assert ind.ema_50 is not None
    assert ind.rsi_14 is not None
    assert 0 <= ind.rsi_14 <= 100
    assert ind.macd is not None
    assert ind.atr_14 is not None and ind.atr_14 > 0
    assert ind.bb_upper is not None and ind.bb_lower is not None
    assert ind.bb_upper > ind.bb_lower
    assert ind.trend_bias == "BULLISH"


def test_market_structure_analyzer():
    candles = generate_mock_candles(count=50, trend="up")
    struct = MarketStructureAnalyzer.analyze(candles, current_session="LONDON")

    assert struct.active_session == "LONDON"
    assert struct.swing_high is not None
    assert struct.swing_low is not None
    assert struct.nearest_support is not None
