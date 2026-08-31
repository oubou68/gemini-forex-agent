import pytest
from datetime import datetime, timedelta
from agent.analysis.stock_indicators import StockTechnicalIndicatorEngine
from agent.analysis.stock_session import StockSessionAnalyzer
from agent.core.models import Candle


def test_stock_indicators_calculation():
    now = datetime.utcnow()
    candles = []
    base = 220.0
    for i in range(60, 0, -1):
        t = (now - timedelta(minutes=i * 5)).isoformat() + "Z"
        o = base + (i * 0.1)
        c = o + 0.3
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        v = 25000 + (i * 100)
        candles.append(Candle(time=t, open=o, high=h, low=l, close=c, volume=v, complete=True))

    ind = StockTechnicalIndicatorEngine.calculate_indicators(candles, orb_bars=3)

    assert ind.ema_9 is not None
    assert ind.ema_21 is not None
    assert ind.vwap is not None
    assert ind.vwap > 0
    assert ind.vwap_upper is not None
    assert ind.vwap_lower is not None
    assert ind.vwap_lower <= ind.vwap <= ind.vwap_upper
    assert ind.orb_high is not None
    assert ind.orb_low is not None
    assert ind.orb_low <= ind.orb_high
    assert ind.rvol is not None
    assert ind.rsi_14 is not None
    assert 0 <= ind.rsi_14 <= 100
    assert ind.trend_bias in ["BULLISH", "BEARISH", "NEUTRAL"]


def test_stock_session_analyzer():
    # Test session string generation
    sess = StockSessionAnalyzer.get_current_session()
    assert isinstance(sess, str)
    assert len(sess) > 0
