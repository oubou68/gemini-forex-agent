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


@pytest.mark.asyncio
async def test_stock_analyst_null_json_parsing():
    analyst = GeminiStockAnalyst(api_key="")
    price = MarketPrice(instrument="AAPL", time="2026-08-29T14:45:00Z", bid=225.48, ask=225.52, spread_pips=0.04, mid=225.50)

    null_json = """
    ```json
    {
      "action": "HOLD",
      "confidence": 80,
      "thesis_summary": "Konsolidierung am VWAP, Warten auf Ausbruch.",
      "reasoning": "RVOL gering, kein Momentum",
      "entry_price": null,
      "stop_loss": null,
      "take_profit_1": null,
      "take_profit_2": null,
      "risk_reward_ratio": null,
      "invalidation_level": null,
      "suggested_risk_pct": null,
      "setup_type": null
    }
    ```
    """

    class MockModel:
        def generate_content(self, model, contents):
            class Resp:
                text = null_json
            return Resp()

    class MockClient:
        models = MockModel()

    analyst.client = MockClient()
    indicators = IndicatorValues(ema_9=225.80, ema_21=224.90, rsi_14=50.0, macd_hist=0.0, atr_14=1.80)
    decision = await analyst._call_gemini_api("TEST PROMPT", "AAPL", price, indicators)
    
    assert decision is not None
    assert decision.action == TradeAction.HOLD
    assert decision.confidence == 80.0
    assert decision.entry_price == price.mid
    assert decision.stop_loss is None
    assert decision.risk_reward_ratio == 2.0
    assert decision.setup_type == "NONE"


