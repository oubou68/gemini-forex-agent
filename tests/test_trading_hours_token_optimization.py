import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agent.analysis.session import SessionAnalyzer
from agent.analysis.stock_session import StockSessionAnalyzer
from agent.ai.gemini_analyst import GeminiForexAnalyst
from agent.ai.stock_analyst import GeminiStockAnalyst
from agent.core.models import (
    MarketPrice, IndicatorValues, MarketStructure, TradeAction
)


def test_forex_session_analyzer_market_open():
    # Wednesday 14:00 UTC -> Market Open
    wed = datetime(2026, 9, 2, 14, 0, 0, tzinfo=timezone.utc)
    assert SessionAnalyzer.is_market_open(wed) is True
    assert SessionAnalyzer.get_current_session(wed) == "OVERLAP_LONDON_NY"

    # Saturday 12:00 UTC -> Weekend Closed
    sat = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert SessionAnalyzer.is_market_open(sat) is False
    assert "CLOSED" in SessionAnalyzer.get_current_session(sat)

    # Sunday 18:00 UTC -> Weekend Closed before 21:00
    sun_early = datetime(2026, 9, 6, 18, 0, 0, tzinfo=timezone.utc)
    assert SessionAnalyzer.is_market_open(sun_early) is False

    # Sunday 22:00 UTC -> Market Open
    sun_late = datetime(2026, 9, 6, 22, 0, 0, tzinfo=timezone.utc)
    assert SessionAnalyzer.is_market_open(sun_late) is True


def test_stock_session_analyzer_market_open():
    # Wednesday 15:00 UTC (11:00 EST) -> RTH Open
    wed_rth = datetime(2026, 9, 2, 15, 0, 0, tzinfo=timezone.utc)
    assert StockSessionAnalyzer.is_market_open(wed_rth, regular_hours_only=False) is True
    assert StockSessionAnalyzer.is_market_open(wed_rth, regular_hours_only=True) is True
    assert StockSessionAnalyzer.is_regular_trading_hours(wed_rth) is True

    # Wednesday 10:00 UTC (06:00 EST) -> Pre-Market
    wed_pre = datetime(2026, 9, 2, 10, 0, 0, tzinfo=timezone.utc)
    assert StockSessionAnalyzer.is_market_open(wed_pre, regular_hours_only=False) is True
    assert StockSessionAnalyzer.is_market_open(wed_pre, regular_hours_only=True) is False

    # Saturday 15:00 UTC -> Weekend Closed
    sat = datetime(2026, 9, 5, 15, 0, 0, tzinfo=timezone.utc)
    assert StockSessionAnalyzer.is_market_open(sat) is False
    assert "CLOSED" in StockSessionAnalyzer.get_current_session(sat)

    # Wednesday 02:00 UTC -> Overnight Closed
    wed_night = datetime(2026, 9, 2, 2, 0, 0, tzinfo=timezone.utc)
    assert StockSessionAnalyzer.is_market_open(wed_night) is False


@pytest.mark.asyncio
async def test_forex_gemini_token_optimization_when_closed():
    analyst = GeminiForexAnalyst(api_key="dummy_key", restrict_to_trading_hours=True)

    # Mock client
    mock_generate = MagicMock()
    class MockClient:
        class models:
            generate_content = mock_generate
    analyst.client = MockClient()

    price = MarketPrice(instrument="EUR_USD", time="2026-09-05T12:00:00Z", bid=1.0850, ask=1.0851, spread_pips=1.0, mid=1.08505)
    indicators = IndicatorValues(ema_9=1.0855, ema_21=1.0845, rsi_14=50.0, macd_hist=0.0, atr_14=0.0015)
    structure = MarketStructure(active_session="WEEKEND")

    # Simulate Saturday (Weekend Closed)
    with patch("agent.analysis.session.SessionAnalyzer.is_market_open", return_value=False):
        decision = await analyst.analyze_market(
            instrument="EUR_USD",
            price=price,
            indicators=indicators,
            structure=structure,
            session_name="FOREX_MARKET_CLOSED (WEEKEND)",
            open_positions_count=0,
            daily_drawdown_pct=0.0
        )

        # Ensure Gemini API was NOT called, preserving tokens
        assert mock_generate.call_count == 0
        assert decision.action == TradeAction.HOLD
        assert "Token" in decision.thesis_summary or "geschlossen" in decision.thesis_summary


@pytest.mark.asyncio
async def test_stock_gemini_token_optimization_when_closed():
    analyst = GeminiStockAnalyst(api_key="dummy_key", restrict_to_trading_hours=True, regular_hours_only=True)

    # Mock client
    mock_generate = MagicMock()
    class MockClient:
        class models:
            generate_content = mock_generate
    analyst.client = MockClient()

    price = MarketPrice(instrument="AAPL", time="2026-09-05T15:00:00Z", bid=225.0, ask=225.1, spread_pips=0.1, mid=225.05)
    indicators = IndicatorValues(ema_9=225.5, ema_21=224.5, rsi_14=50.0, macd_hist=0.0, atr_14=1.5)
    structure = MarketStructure(active_session="WEEKEND")

    # Simulate Market Closed
    with patch("agent.analysis.stock_session.StockSessionAnalyzer.is_market_open", return_value=False):
        decision = await analyst.analyze_market(
            symbol="AAPL",
            price=price,
            indicators=indicators,
            structure=structure,
            session_name="US_MARKET_CLOSED (WEEKEND)",
            open_positions_count=0,
            daily_drawdown_pct=0.0
        )

        # Ensure Gemini API was NOT called, preserving tokens
        assert mock_generate.call_count == 0
        assert decision.action == TradeAction.HOLD
        assert "Token" in decision.thesis_summary or "geschlossen" in decision.thesis_summary
