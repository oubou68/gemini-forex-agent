import pytest
from agent.core.stock_orchestrator import StockTradingAgentOrchestrator
from agent.broker.stock_simulator import StockSimulatorBroker


@pytest.mark.asyncio
async def test_nasdaq_dow_universe_catalogs():
    orch = StockTradingAgentOrchestrator()
    await orch.set_mode("simulator")
    await orch.initialize()

    catalog = orch.get_universe_catalog()
    assert "dow_jones_30" in catalog
    assert "nasdaq_100" in catalog
    assert "index_etfs" in catalog
    assert len(catalog["dow_jones_30"]) >= 30
    assert len(catalog["nasdaq_100"]) >= 90
    assert "AAPL" in catalog["dow_jones_30"]
    assert "UNH" in catalog["dow_jones_30"]
    assert "MSFT" in catalog["nasdaq_100"]
    assert "NVDA" in catalog["nasdaq_100"]
    assert "QQQ" in catalog["index_etfs"]
    assert "DIA" in catalog["index_etfs"]


@pytest.mark.asyncio
async def test_market_universe_screener():
    orch = StockTradingAgentOrchestrator()
    await orch.set_mode("simulator")
    await orch.initialize()

    candidates = await orch.screen_market_universe(limit=6)
    assert len(candidates) > 0
    top = candidates[0]
    assert top.symbol is not None
    assert top.index in ("NASDAQ", "DOW", "ETF", "GROWTH")
    assert top.price > 0
    assert top.score >= 0


@pytest.mark.asyncio
async def test_spacex_anthropic_and_growth_symbols():
    orch = StockTradingAgentOrchestrator()
    await orch.set_mode("simulator")
    await orch.initialize()

    expected = ["SPCX", "ANT", "CRWV", "GEV", "VRT", "TSM", "RKLB", "ASTS", "HOOD", "RDDT", "SOFI", "IONQ"]
    catalog = orch.get_universe_catalog()
    assert "innovation_growth" in catalog
    assert "SPCX" in catalog["watchlist"]
    assert "ANT" in catalog["watchlist"]
    for sym in expected:
        assert sym in catalog["innovation_growth"]
        price = await orch.broker.get_current_price(sym)
        assert price.mid > 0
        candles = await orch.broker.get_candles(sym, count=20)
        assert len(candles) == 20


@pytest.mark.asyncio
async def test_simulator_nasdaq_dow_symbols():
    sim = StockSimulatorBroker()
    await sim.initialize()

    # Check Dow Jones symbol
    p_dow = await sim.get_current_price("UNH")
    assert p_dow.mid > 300.0
    candles_dow = await sim.get_candles("UNH", count=20)
    assert len(candles_dow) == 20

    # Check NASDAQ symbol
    p_nasdaq = await sim.get_current_price("AMZN")
    assert p_nasdaq.mid > 100.0
    candles_nasdaq = await sim.get_candles("AMZN", count=20)
    assert len(candles_nasdaq) == 20

    # Check Index ETF
    p_dia = await sim.get_current_price("DIA")
    assert p_dia.mid > 200.0
