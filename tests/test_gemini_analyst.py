import pytest
from agent.ai.gemini_analyst import GeminiForexAnalyst
from agent.core.models import (
    MarketPrice, IndicatorValues, MarketStructure, TradeAction
)


@pytest.mark.asyncio
async def test_gemini_analyst_heuristic_fallback():
    analyst = GeminiForexAnalyst(api_key="")  # No key -> heuristic
    
    price = MarketPrice(
        instrument="EUR_USD",
        time="2026-08-29T12:00:00Z",
        bid=1.0850,
        ask=1.0851,
        spread_pips=1.0,
        mid=1.08505
    )

    indicators = IndicatorValues(
        ema_9=1.0855,
        ema_21=1.0845,
        rsi_14=52.0,
        macd_hist=0.00015,
        atr_14=0.0015,
        trend_bias="BULLISH"
    )

    structure = MarketStructure(
        swing_high=1.0870,
        swing_low=1.0830,
        nearest_support=1.0845,
        nearest_resistance=1.0875,
        fvg_bullish=True,
        active_session="LONDON"
    )

    decision = await analyst.analyze_market(
        instrument="EUR_USD",
        price=price,
        indicators=indicators,
        structure=structure,
        session_name="LONDON",
        open_positions_count=0,
        daily_drawdown_pct=0.0
    )

    assert decision.instrument == "EUR_USD"
    assert decision.action == TradeAction.BUY
    assert decision.confidence >= 70.0
    assert decision.stop_loss is not None
    assert decision.take_profit_1 is not None
    assert decision.stop_loss < decision.entry_price < decision.take_profit_1


@pytest.mark.asyncio
async def test_gemini_analyst_null_json_parsing():
    analyst = GeminiForexAnalyst(api_key="")
    price = MarketPrice(instrument="EUR_USD", time="2026-08-29T12:00:00Z", bid=1.0850, ask=1.0851, spread_pips=1.0, mid=1.08505)

    null_json = """
    ```json
    {
      "action": "HOLD",
      "confidence": 75,
      "thesis_summary": "Neutrales Marktumfeld, kein klares Signal.",
      "reasoning": "RSI neutral, keine Trendbestätigung",
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

    # Mock the client generate
    class MockModel:
        def generate_content(self, model, contents):
            class Resp:
                text = null_json
            return Resp()

    class MockClient:
        models = MockModel()

    analyst.client = MockClient()
    indicators = IndicatorValues(ema_9=1.0855, ema_21=1.0845, rsi_14=50.0, macd_hist=0.0, atr_14=0.0015)
    decision = await analyst._call_gemini_api("TEST PROMPT", "EUR_USD", price, indicators)
    
    assert decision is not None
    assert decision.action == TradeAction.HOLD
    assert decision.confidence == 75.0
    assert decision.entry_price == price.mid
    assert decision.stop_loss is None
    assert decision.risk_reward_ratio == 1.8
    assert decision.setup_type == "NONE"


