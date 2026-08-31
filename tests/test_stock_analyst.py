import pytest
from agent.ai.stock_analyst import GeminiStockAnalyst
from agent.core.models import (
    MarketPrice, IndicatorValues, MarketStructure, TradeAction
)


@pytest.mark.asyncio
async def test_stock_analyst_heuristic_bullish():
    analyst = GeminiStockAnalyst(api_key="")  # Heuristic fallback
    
    price = MarketPrice(
        instrument="AAPL",
        time="2026-08-29T14:45:00Z",
        bid=225.48,
        ask=225.52,
        spread_pips=0.04,
        mid=225.50
    )

    indicators = IndicatorValues(
        ema_9=225.80,
        ema_21=224.90,
        rsi_14=56.0,
        macd_hist=0.45,
        atr_14=1.80,
        vwap=224.20,
        vwap_upper=226.50,
        vwap_lower=221.90,
        orb_high=224.80,
        orb_low=222.50,
        rvol=1.45,
        trend_bias="BULLISH"
    )

    structure = MarketStructure(
        swing_high=227.0,
        swing_low=221.0,
        nearest_support=224.0,
        nearest_resistance=228.0,
        active_session="US_OPENING_DRIVE (RTH OPEN)"
    )

    decision = await analyst.analyze_market(
        symbol="AAPL",
        price=price,
        indicators=indicators,
        structure=structure,
        session_name="US_OPENING_DRIVE (RTH OPEN)",
        open_positions_count=0,
        daily_drawdown_pct=0.0
    )

    assert decision.instrument == "AAPL"
    assert decision.action == TradeAction.BUY
    assert decision.confidence >= 70.0
    assert decision.stop_loss is not None
    assert decision.take_profit_1 is not None
    assert decision.stop_loss < decision.entry_price < decision.take_profit_1
    assert decision.setup_type in ["OPENING_RANGE_BREAKOUT", "VWAP_PULLBACK"]
