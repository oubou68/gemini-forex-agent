import asyncio
import logging
import math
import random
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable

from agent.broker.base_broker import BaseBroker
from agent.broker.stock_simulator import StockSimulatorBroker
from agent.broker.alpaca_client import AlpacaClient
from agent.analysis.stock_indicators import StockTechnicalIndicatorEngine
from agent.analysis.market_structure import MarketStructureAnalyzer
from agent.analysis.stock_session import StockSessionAnalyzer
from agent.ai.stock_analyst import GeminiStockAnalyst
from agent.ai.memory import AgentMemory
from agent.risk.risk_manager import RiskManager
from agent.core.models import (
    Candle, MarketPrice, IndicatorValues, MarketStructure,
    GeminiTradeDecision, TradeAction, Position, OrderRequest,
    PositionDirection, PositionStatus, AccountSummary,
    TradePerformanceStats, AgentTelemetry, StockScreenerCandidate
)
from config.settings import settings, yaml_config

logger = logging.getLogger(__name__)


class StockTradingAgentOrchestrator:
    """
    Zentraler Orchestrator des autonomen Gemini Intraday-Aktien-Trading-Agenten.
    Unterstützt das gesamte NASDAQ-100 Universum, den Dow Jones Industrial Average (DJIA 30)
    sowie führende US-Index-ETFs mit parallelem Full-Universe Screener, automatischer
    Ausführung auf den stärksten Screener-Kandidaten, tagesverankertem VWAP/ORB,
    dynamischem Risk-Scaling und mehrstufigem ATR-Trailing-Stop.
    """

    def __init__(self):
        self.is_running: bool = False
        self.mode: str = settings.ALPACA_ENVIRONMENT.lower()  # "simulator", "paper", "live"
        self.current_symbol: str = settings.DEFAULT_STOCK_SYMBOL

        stock_cfg = yaml_config.get("stock_trading", {})
        self.watchlist: List[str] = stock_cfg.get("symbols", [
            "AAPL", "NVDA", "SPCX", "ANT", "TSLA", "MSFT", "AMZN", "GOOGL", "META", "AMD", "SPY", "QQQ", "DIA"
        ])
        self.innovation_growth: List[str] = stock_cfg.get("innovation_growth", [
            "SPCX", "ANT", "CRWV", "GEV", "VRT", "TSM", "RKLB", "ASTS", "HOOD", "RDDT", "SOFI", "IONQ"
        ])
        self.dow_jones_30: List[str] = stock_cfg.get("dow_jones_30", [
            "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
            "DOW", "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD",
            "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG", "TRV", "UNH", "V", "VZ", "WMT"
        ])
        self.nasdaq_100: List[str] = stock_cfg.get("nasdaq_100", [
            "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AMAT", "AMD", "AMGN",
            "AMZN", "ANSS", "ARM", "ASML", "AVGO", "AZN", "BIIB", "BKNG", "BKR", "CCEP",
            "CDNS", "CDW", "CEG", "CHTR", "CPRT", "CRWD", "CSCO", "CSGP", "CSX", "CTAS",
            "CTSH", "DASH", "DDOG", "DLTR", "DXCM", "EA", "EXC", "FANG", "FAST", "FTNT",
            "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX", "ILMN", "INTC", "INTU",
            "ISRG", "KDP", "KHC", "KLAC", "LIN", "LRCX", "LULU", "MAR", "MCHP", "MDB",
            "MDLZ", "MELI", "META", "MNST", "MRNA", "MRVL", "MSFT", "MU", "NFLX", "NVDA",
            "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR",
            "PYPL", "QCOM", "REGN", "ROP", "ROST", "SBUX", "SIRI", "SMCI", "SNPS", "TEAM",
            "TMUS", "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY", "XEL", "ZS"
        ])
        self.index_etfs: List[str] = stock_cfg.get("index_etfs", [
            "SPY", "QQQ", "DIA", "IWM", "SMH", "XLK", "XLF", "XLE"
        ])

        self.scan_interval: int = settings.STOCK_SCAN_INTERVAL_SECONDS
        self.screener_candidates: List[StockScreenerCandidate] = []
        self._universe_scan_counter: int = 0

        # Subkomponenten initialisieren
        self.broker: BaseBroker = self._create_broker()
        self.indicator_engine = StockTechnicalIndicatorEngine()
        self.structure_analyzer = MarketStructureAnalyzer()
        self.session_analyzer = StockSessionAnalyzer()
        gemini_cfg = yaml_config.get("gemini", {})
        risk_cfg = yaml_config.get("risk", {})

        self.risk_manager = RiskManager(
            risk_per_trade_pct=risk_cfg.get("risk_per_trade_pct", settings.STOCK_RISK_PERCENT_PER_TRADE),
            max_open_positions=risk_cfg.get("stock_max_open_positions", settings.STOCK_MAX_OPEN_POSITIONS),
            max_daily_drawdown_pct=risk_cfg.get("max_daily_drawdown_pct", settings.STOCK_MAX_DAILY_DRAWDOWN_PERCENT),
            max_spread_pips=risk_cfg.get("max_spread_dollars", 0.35),
            min_risk_reward_ratio=risk_cfg.get("min_risk_reward_ratio", getattr(settings, "MIN_RISK_REWARD_RATIO", 1.5)),
            confidence_threshold=gemini_cfg.get("confidence_threshold", 65.0),
            breakeven_trigger_r=risk_cfg.get("breakeven_trigger_r", getattr(settings, "BREAKEVEN_TRIGGER_R", 1.0)),
            default_atr_multiplier_sl=risk_cfg.get("default_atr_multiplier_sl", getattr(settings, "DEFAULT_ATR_MULTIPLIER_SL", 1.5)),
            default_atr_multiplier_tp=risk_cfg.get("default_atr_multiplier_tp", getattr(settings, "DEFAULT_ATR_MULTIPLIER_TP", 2.5)),
            trailing_stop_enabled=risk_cfg.get("trailing_stop_enabled", getattr(settings, "TRAILING_STOP_ENABLED", True)),
            allow_ai_close_signals=risk_cfg.get("allow_ai_close_signals", getattr(settings, "ALLOW_AI_CLOSE_SIGNALS", True)),
            auto_liquidate_on_drawdown=risk_cfg.get("auto_liquidate_on_drawdown", getattr(settings, "AUTO_LIQUIDATE_ON_DRAWDOWN", False)),
            dynamic_risk_scaling=getattr(settings, "DYNAMIC_RISK_SCALING", True)
        )

        self.ai_analyst = GeminiStockAnalyst(
            api_key=settings.GEMINI_API_KEY,
            model_name=settings.GEMINI_MODEL,
            restrict_to_trading_hours=gemini_cfg.get("restrict_to_trading_hours", settings.GEMINI_RESTRICT_TO_TRADING_HOURS),
            regular_hours_only=gemini_cfg.get("stock_regular_hours_only", settings.GEMINI_STOCK_REGULAR_HOURS_ONLY),
            temperature=float(gemini_cfg.get("temperature", 0.2)),
            atr_sl_mult=self.risk_manager.default_atr_multiplier_sl,
            atr_tp_mult=self.risk_manager.default_atr_multiplier_tp
        )
        # Isolierter Memory-Namespace für US-Aktien
        self.memory = AgentMemory(namespace="stock")

        # Telemetrie & State Cache
        self.last_price: Optional[MarketPrice] = None
        self.last_indicators: Optional[IndicatorValues] = None
        self.last_structure: Optional[MarketStructure] = None
        self.last_decision: Optional[GeminiTradeDecision] = None
        self._symbol_atr_cache: Dict[str, float] = {}
        self.recent_logs: List[Dict[str, Any]] = []
        self._telemetry_subscribers: List[Callable[[Dict[str, Any]], Any]] = []
        self._loop_task: Optional[asyncio.Task] = None

    def _create_broker(self) -> BaseBroker:
        if self.mode in ("paper", "live") and (settings.ALPACA_OAUTH_TOKEN or (settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY)):
            logger.info(f"Initialisiere Alpaca Client ({self.mode})...")
            return AlpacaClient(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                environment=self.mode
            )
        else:
            logger.info("Initialisiere Stock Simulator Broker (USD)...")
            return StockSimulatorBroker(initial_balance=100000.0, currency="USD")

    def get_universe_catalog(self) -> Dict[str, Any]:
        """Gibt die vollständigen Index-Universen für UI und API zurück."""
        return {
            "watchlist": self.watchlist,
            "innovation_growth": self.innovation_growth,
            "dow_jones_30": self.dow_jones_30,
            "nasdaq_100": self.nasdaq_100,
            "index_etfs": self.index_etfs,
            "total_symbols_count": len(set(self.watchlist + self.innovation_growth + self.dow_jones_30 + self.nasdaq_100 + self.index_etfs))
        }

    def log(self, message: str, level: str = "INFO", category: str = "SYSTEM"):
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = {
            "timestamp": timestamp,
            "level": level,
            "category": category,
            "message": message
        }
        self.recent_logs.append(entry)
        if len(self.recent_logs) > 100:
            self.recent_logs.pop(0)
        logger.log(getattr(logging, level, logging.INFO), f"[STOCK_AGENT][{category}] {message}")

    def subscribe_telemetry(self, callback: Callable[[Dict[str, Any]], Any]):
        self._telemetry_subscribers.append(callback)

    async def broadcast_telemetry(self):
        telemetry = await self.get_telemetry()
        data = telemetry.model_dump()
        for sub in self._telemetry_subscribers:
            try:
                res = sub(data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug(f"Stock Telemetry Broadcast Fehler: {e}")

    async def set_mode(self, mode: str):
        self.mode = mode.lower()
        self.broker = self._create_broker()
        await self.broker.initialize()
        self.log(f"Alpaca Broker-Modus gewechselt zu: {self.mode}", "INFO", "CONFIG")
        await self.broadcast_telemetry()

    async def set_symbol(self, symbol: str):
        self.current_symbol = symbol.replace("/", "").replace("_", "").upper()
        self.log(f"Aktive US-Aktie gewechselt zu: {self.current_symbol}", "INFO", "CONFIG")
        await self.run_scan_cycle(self.current_symbol)
        await self.broadcast_telemetry()

    async def initialize(self):
        await self.broker.initialize()
        await self.screen_market_universe(limit=8)
        self.log("Stock Intraday Agent Orchestrator (NASDAQ & Dow Jones) erfolgreich initialisiert.", "INFO", "SYSTEM")

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.log("Autonomer Aktien-Trading-Loop (NASDAQ & Dow Jones) GESTARTET.", "INFO", "SYSTEM")
        self._loop_task = asyncio.create_task(self._orchestrator_loop())
        await self.broadcast_telemetry()

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
        self.log("Autonomer Aktien-Trading-Loop PAUSIERT.", "WARNING", "SYSTEM")
        await self.broadcast_telemetry()

    async def _trade_top_screener_candidates(self):
        """
        Führt autonome KI-Analyse & Ausführung auf den Top-Kandidaten des NASDAQ/Dow Screeners aus,
        damit hochprofitables Momentum über das gesamte Universum statt nur einem Symbol gehandelt wird.
        """
        if not self.screener_candidates:
            return

        open_positions = await self.broker.get_open_positions()
        if len(open_positions) >= self.risk_manager.max_open_positions:
            return

        held_symbols = {p.instrument for p in open_positions}
        max_extra = max(1, settings.MAX_GEMINI_CALLS_PER_CYCLE)
        evaluated = 0

        for cand in self.screener_candidates:
            if evaluated >= max_extra:
                break
            if cand.symbol == self.current_symbol or cand.symbol in held_symbols:
                continue
            if cand.signal not in ("BUY", "SELL") or cand.score < 70.0:
                continue
            if self.memory.get_instrument_loss_cooldown_remaining(cand.symbol, settings.LOSS_COOLDOWN_SECONDS) > 0:
                continue

            await self.run_scan_cycle(cand.symbol, update_primary_telemetry=False)
            evaluated += 1

    async def _orchestrator_loop(self):
        while self.is_running:
            try:
                # 1. Scanne das primäre Aktien-Symbol
                await self.run_scan_cycle(self.current_symbol, update_primary_telemetry=True)

                # 2. Multi-Symbol Full-Universe Screener (NASDAQ-100 + Dow Jones 30 + ETFs)
                self._universe_scan_counter += 1
                if self._universe_scan_counter % 4 == 0:
                    await self.screen_market_universe(limit=8)
                    if settings.MULTI_INSTRUMENT_SCAN_ENABLED:
                        await self._trade_top_screener_candidates()

                # 3. Checke offene Positionen auf Auto-Liquidation, Break-Even & ATR Trailing Stop
                await self._manage_open_positions()

                # 4. Broadcast Live State
                await self.broadcast_telemetry()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"Fehler im Stock-Orchestrator-Loop: {e}", "ERROR", "SYSTEM")

            await asyncio.sleep(self.scan_interval)

    async def screen_market_universe(self, limit: int = 8) -> List[StockScreenerCandidate]:
        """
        Paralleler Intraday Screener über das gesamte NASDAQ-100, Dow Jones 30 & ETF Universum.
        Filtert nach Relative Volume (RVOL), ORB Breakouts, VWAP-Trend, ADX, M15-Trend und RSI.
        """
        # Vollständiges Universum einbeziehen (inkl. Innovation & Growth: SpaceX, Anthropic, AI/Space/Power/Fintech)
        all_symbols = list(set(self.watchlist + self.innovation_growth + self.dow_jones_30 + self.nasdaq_100 + self.index_etfs))
        # Priorisiere Watchlist + Innovation-Kern + zufällige Rotation aus dem Gesamtuniversum
        priority_core = list(set(self.watchlist[:6] + self.innovation_growth[:4] + [self.current_symbol]))
        remaining_pool = [s for s in all_symbols if s not in priority_core]
        extra_sample = random.sample(remaining_pool, min(len(remaining_pool), 14))
        sampled_symbols = priority_core + extra_sample

        batch_prices: Dict[str, MarketPrice] = {}
        batch_candles: Dict[str, List[Candle]] = {}
        if hasattr(self.broker, "get_multi_prices") and hasattr(self.broker, "get_multi_candles"):
            batch_prices, batch_candles = await asyncio.gather(
                self.broker.get_multi_prices(sampled_symbols),
                self.broker.get_multi_candles(sampled_symbols, granularity="M5", count=60),
            )

        semaphore = asyncio.Semaphore(8)

        async def _eval_symbol(sym: str) -> Optional[StockScreenerCandidate]:
            async with semaphore:
                try:
                    price = batch_prices.get(sym) or await self.broker.get_current_price(sym)
                    candles = batch_candles.get(sym) or await self.broker.get_candles(sym, granularity="M5", count=60)
                    if not candles or len(candles) < 20:
                        return None

                    ind = self.indicator_engine.calculate_indicators(candles, orb_bars=3)
                    if ind.atr_14:
                        self._symbol_atr_cache[sym] = ind.atr_14

                    if sym in self.innovation_growth:
                        idx = "GROWTH"
                    elif sym in self.nasdaq_100:
                        idx = "NASDAQ"
                    elif sym in self.dow_jones_30:
                        idx = "DOW"
                    else:
                        idx = "ETF"

                    first_open = candles[0].open
                    change_pct = round(((price.mid - first_open) / max(first_open, 1e-6)) * 100.0, 2)

                    vwap_pos = "AT_VWAP"
                    if ind.vwap:
                        if price.mid > ind.vwap * 1.002:
                            vwap_pos = "ABOVE_VWAP"
                        elif price.mid < ind.vwap * 0.998:
                            vwap_pos = "BELOW_VWAP"

                    orb_stat = "INSIDE"
                    if ind.orb_high and price.mid > ind.orb_high:
                        orb_stat = "BREAKOUT_HIGH"
                    elif ind.orb_low and price.mid < ind.orb_low:
                        orb_stat = "BREAKDOWN_LOW"

                    score = 50.0
                    signal = "HOLD"
                    rvol_val = ind.rvol or 1.0

                    if rvol_val >= 1.5:
                        score += 15
                    elif rvol_val >= 1.2:
                        score += 8

                    if orb_stat == "BREAKOUT_HIGH" and vwap_pos == "ABOVE_VWAP":
                        score += 22
                        signal = "BUY"
                    elif orb_stat == "BREAKDOWN_LOW" and vwap_pos == "BELOW_VWAP":
                        score += 22
                        signal = "SELL"
                    elif ind.trend_bias == "BULLISH" and (ind.rsi_14 or 50) < 65:
                        score += 10
                        signal = "BUY"
                    elif ind.trend_bias == "BEARISH" and (ind.rsi_14 or 50) > 35:
                        score += 10
                        signal = "SELL"

                    # M15 Higher-Timeframe & ADX Bonus
                    if signal == "BUY" and ind.htf_trend_bias == "BULLISH":
                        score += 8
                    elif signal == "SELL" and ind.htf_trend_bias == "BEARISH":
                        score += 8
                    if ind.adx_14 is not None and ind.adx_14 >= 22.0 and signal != "HOLD":
                        score += 7

                    return StockScreenerCandidate(
                        symbol=sym,
                        index=idx,
                        price=price.mid,
                        change_pct=change_pct,
                        rvol=round(rvol_val, 2),
                        vwap_position=vwap_pos,
                        orb_status=orb_stat,
                        rsi=round(ind.rsi_14 or 50.0, 1),
                        score=round(min(99.0, max(10.0, score)), 1),
                        signal=signal
                    )
                except Exception:
                    return None

        results = await asyncio.gather(*[_eval_symbol(s) for s in sampled_symbols])
        candidates = [c for c in results if c is not None]
        candidates.sort(key=lambda x: x.score, reverse=True)
        self.screener_candidates = candidates[:limit]
        return self.screener_candidates

    def calculate_shares_size(
        self,
        equity: float,
        entry_price: float,
        stop_loss_price: float,
        risk_pct: float = 1.0,
        max_capital_pct: float = 0.50
    ) -> int:
        """
        Berechnet die exakte Anzahl der Aktien (Shares) basierend auf dem maximalen $-Risiko pro Trade.
        """
        if equity <= 0 or entry_price <= 0 or stop_loss_price <= 0 or entry_price == stop_loss_price:
            return 1

        clamped_risk = max(0.1, min(float(risk_pct), max(self.risk_manager.risk_per_trade_pct * 1.5, 5.0)))
        risk_amount = equity * (clamped_risk / 100.0)
        risk_per_share = abs(entry_price - stop_loss_price)
        shares = math.floor(risk_amount / max(0.05, risk_per_share))

        # Max Buying-Power Schutz
        max_shares_capital = math.floor((equity * max_capital_pct) / entry_price)
        final_shares = max(1, min(shares, max_shares_capital if max_shares_capital > 0 else shares))
        return final_shares

    async def run_scan_cycle(self, symbol: str, update_primary_telemetry: bool = True):
        """Führt einen kompletten Analyse- und Entscheidungszyklus für US-Aktien durch."""
        try:
            # 1. Preis- & Kerzendaten abrufen
            price = await self.broker.get_current_price(symbol)
            candles = await self.broker.get_candles(symbol, granularity="M5", count=100)

            # 2. Indikatoren berechnen (VWAP, ORB, RVOL, ADX, M15 HTF, EMAs, RSI, MACD)
            indicators = self.indicator_engine.calculate_indicators(candles, orb_bars=3)
            if indicators.atr_14:
                self._symbol_atr_cache[symbol] = indicators.atr_14

            # 3. Session & Marktstruktur
            session_name = self.session_analyzer.get_current_session()
            structure = self.structure_analyzer.analyze(candles, current_session=session_name)

            if indicators.orb_high and price.mid > indicators.orb_high:
                structure.orb_bias = "ABOVE_ORB_HIGH"
            elif indicators.orb_low and price.mid < indicators.orb_low:
                structure.orb_bias = "BELOW_ORB_LOW"
            else:
                structure.orb_bias = "INSIDE_ORB_RANGE"

            if update_primary_telemetry or symbol == self.current_symbol:
                self.last_price = price
                self.last_indicators = indicators
                self.last_structure = structure

            # 4. Account & offene Positionen
            account = await self.broker.get_account_summary()
            open_positions = await self.broker.get_open_positions()
            active_for_symbol = len([p for p in open_positions if p.instrument == symbol])

            pre_score = self.ai_analyst.score_setup_confluence(price, indicators)
            should_call_llm = (
                active_for_symbol > 0
                or pre_score["max_score"] >= 50
                or update_primary_telemetry
            )

            # 5. Gemini KI Entscheidungsfindung für Aktien
            recent_context = self.memory.get_recent_history_context(limit=5)
            decision = await self.ai_analyst.analyze_market(
                symbol=symbol,
                price=price,
                indicators=indicators,
                structure=structure,
                session_name=session_name,
                open_positions_count=active_for_symbol,
                daily_drawdown_pct=account.daily_drawdown_pct,
                recent_history_context=recent_context,
                use_llm=should_call_llm,
                atr_sl_mult=self.risk_manager.default_atr_multiplier_sl,
                atr_tp_mult=self.risk_manager.default_atr_multiplier_tp
            )

            if update_primary_telemetry or symbol == self.current_symbol:
                self.last_decision = decision

            # 6. Signal-Ausführung & Risikoprüfung
            if decision.action in (TradeAction.BUY, TradeAction.SELL):
                cooldown_rem = self.memory.get_instrument_loss_cooldown_remaining(
                    symbol, settings.LOSS_COOLDOWN_SECONDS
                )
                if cooldown_rem > 0:
                    self.log(
                        f"Loss-Cooldown aktiv für Aktie {symbol} ({cooldown_rem:.0f}s verbleibend). Einstieg verschoben.",
                        "INFO",
                        "RISK"
                    )
                    return

                self.log(
                    f"Stock KI Signal generiert: {decision.action.value} {symbol} (Konfidenz: {decision.confidence}%) | Setup: {decision.setup_type}",
                    "INFO",
                    "AI_SIGNAL"
                )

                is_valid, reason = self.risk_manager.validate_trade_decision(
                    decision=decision,
                    account=account,
                    spread_pips=price.spread_pips,
                    open_positions=open_positions
                )

                if is_valid:
                    dyn_risk_pct = self.risk_manager.compute_dynamic_risk_pct(
                        base_risk_pct=decision.suggested_risk_pct,
                        confidence=decision.confidence,
                        daily_drawdown_pct=account.daily_drawdown_pct,
                        consecutive_losses=self.memory.get_consecutive_losses()
                    )

                    entry = decision.entry_price or price.mid
                    sl = decision.stop_loss or (entry * 0.98 if decision.action == TradeAction.BUY else entry * 1.02)
                    shares = self.calculate_shares_size(
                        equity=account.equity,
                        entry_price=entry,
                        stop_loss_price=sl,
                        risk_pct=dyn_risk_pct
                    )

                    direction = PositionDirection.BUY if decision.action == TradeAction.BUY else PositionDirection.SELL
                    order = OrderRequest(
                        instrument=symbol,
                        direction=direction,
                        units=shares,
                        order_type="MARKET",
                        price=decision.entry_price,
                        stop_loss=decision.stop_loss,
                        take_profit=decision.take_profit_1
                    )

                    pos = await self.broker.place_order(order)
                    self.log(
                        f"Stock Order ausgeführt: {direction.value} {shares} Shares {symbol} @ ${pos.entry_price:.2f} (SL: ${pos.stop_loss}, TP: ${pos.take_profit}, Risk: {dyn_risk_pct}%)",
                        "INFO",
                        "EXECUTION"
                    )
                else:
                    self.log(f"Stock Trade abgelehnt durch Risikomanager: {reason}", "WARNING", "RISK")

            elif decision.action == TradeAction.CLOSE:
                if self.risk_manager.allow_ai_close_signals:
                    for p in open_positions:
                        if p.instrument == symbol:
                            closed_pos = await self.broker.close_position(p.id, reason="AGENT_SIGNAL")
                            self.memory.record_closed_trade(closed_pos, thesis=decision.thesis_summary)
                            self.log(f"Position {p.id} ({symbol}) geschlossen. PnL=${closed_pos.realized_pnl:,.2f} USD", "INFO", "EXECUTION")
                else:
                    logger.debug(f"Gemini CLOSE Signal für {symbol} ignoriert (Vorzeitige KI-Exits deaktiviert).")

        except Exception as e:
            self.log(f"Fehler im Stock Scan-Zyklus für {symbol}: {e}", "ERROR", "ANALYSIS")

    async def _manage_open_positions(self):
        """Überwacht Auto-Liquidation, Break-Even und ATR-Trailing-Stop für aktive Aktien-Trades."""
        try:
            open_positions = await self.broker.get_open_positions()
            if not open_positions:
                return

            if self.risk_manager.auto_liquidate_on_drawdown:
                account = await self.broker.get_account_summary()
                if account.daily_drawdown_pct >= self.risk_manager.max_daily_drawdown_pct:
                    self.log(
                        f"STOCK AUTO-LIQUIDATION ausgelöst: Tages-Drawdown ({account.daily_drawdown_pct}%) >= Limit ({self.risk_manager.max_daily_drawdown_pct}%).",
                        "WARNING",
                        "EMERGENCY"
                    )
                    for pos in list(open_positions):
                        try:
                            closed = await self.broker.close_position(pos.id, reason="CIRCUIT_BREAKER")
                            self.memory.record_closed_trade(closed, thesis="Auto-Liquidation durch Drawdown Circuit Breaker.")
                        except Exception as ex:
                            self.log(f"Fehler bei Stock Auto-Liquidation von {pos.id}: {ex}", "ERROR", "EMERGENCY")
                    return

            for pos in open_positions:
                price_info = await self.broker.get_current_price(pos.instrument)
                curr_price = price_info.bid if pos.direction == PositionDirection.BUY else price_info.ask
                atr = self._symbol_atr_cache.get(pos.instrument)

                new_sl = self.risk_manager.evaluate_breakeven_adjustment(pos, curr_price, atr=atr)
                if new_sl is not None and (pos.stop_loss is None or abs(new_sl - pos.stop_loss) >= 0.02):
                    await self.broker.update_stop_loss(pos.id, new_sl)
                    self.log(f"Trailing/BE Stop-Loss für Aktie {pos.instrument} ({pos.id}) nachgezogen auf ${new_sl:.2f}", "INFO", "RISK")
        except Exception as e:
            logger.debug(f"Fehler bei Stock Positionsüberwachung: {e}")

    async def close_single_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        """Schließt gezielt eine einzelne Aktienposition."""
        closed = await self.broker.close_position(position_id, reason=reason)
        self.memory.record_closed_trade(closed, thesis="Einzelne Aktienposition manuell über Dashboard geschlossen.")
        self.log(f"Aktien-Position {closed.id} ({closed.instrument}) geschlossen. PnL=${closed.realized_pnl:,.2f} USD", "INFO", "MANUAL")
        await self.broadcast_telemetry()
        return closed

    async def emergency_close_all(self) -> int:
        """Schließt unverzüglich alle offenen Aktienpositionen."""
        open_positions = await self.broker.get_open_positions()
        count = 0
        for p in list(open_positions):
            try:
                closed = await self.broker.close_position(p.id, reason="EMERGENCY_CLOSE")
                self.memory.record_closed_trade(closed, thesis="Manueller Notstopp für Aktien ausgelöst.")
                count += 1
            except Exception as e:
                self.log(f"Fehler beim Schließen von Position {p.id}: {e}", "ERROR", "EMERGENCY")

        self.log(f"NOTFALL-STOPP: {count} Aktien-Position(en) geschlossen.", "WARNING", "EMERGENCY")
        await self.broadcast_telemetry()
        return count

    async def execute_manual_trade(self, symbol: str, direction: str, risk_pct: float = 1.0) -> Position:
        """Führt eine manuelle Aktien-Order über das Web-Dashboard aus."""
        price = await self.broker.get_current_price(symbol)
        account = await self.broker.get_account_summary()

        entry = price.ask if direction.upper() == "BUY" else price.bid
        atr = (
            self._symbol_atr_cache.get(symbol)
            or (self.last_indicators.atr_14 if self.last_indicators and self.last_indicators.atr_14 else None)
            or max(0.50, round(entry * 0.01, 2))
        )

        sl_mult = self.risk_manager.default_atr_multiplier_sl
        tp_mult = self.risk_manager.default_atr_multiplier_tp
        sl = round(entry - (sl_mult * atr) if direction.upper() == "BUY" else entry + (sl_mult * atr), 2)
        tp = round(entry + (tp_mult * atr) if direction.upper() == "BUY" else entry - (tp_mult * atr), 2)

        shares = self.calculate_shares_size(
            equity=account.equity,
            entry_price=entry,
            stop_loss_price=sl,
            risk_pct=risk_pct
        )

        pos_dir = PositionDirection.BUY if direction.upper() == "BUY" else PositionDirection.SELL
        order = OrderRequest(
            instrument=symbol,
            direction=pos_dir,
            units=shares,
            order_type="MARKET",
            price=entry,
            stop_loss=sl,
            take_profit=tp
        )
        pos = await self.broker.place_order(order)
        self.log(f"Manuelle Aktien-Order ausgeführt: {direction} {shares} Shares {symbol} @ ${entry:.2f}", "INFO", "MANUAL")
        await self.broadcast_telemetry()
        return pos

    async def get_telemetry(self) -> AgentTelemetry:
        account = await self.broker.get_account_summary()
        open_positions = await self.broker.get_open_positions()

        try:
            broker_closed = await self.broker.get_closed_positions()
            for cp in broker_closed:
                if not any(t.get("id") == cp.id for t in self.memory.trade_logs):
                    self.memory.record_closed_trade(cp, thesis="Order durch Broker / SL / TP geschlossen")
        except Exception as e:
            logger.debug(f"Fehler bei Synchronisation geschlossener Aktien-Trades: {e}")

        stats = self.memory.get_performance_stats(reference_capital=account.daily_start_equity or 100000.0)
        closed_list = list(reversed(self.memory.trade_logs[-50:]))

        return AgentTelemetry(
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            is_running=self.is_running,
            mode=self.mode,
            current_instrument=self.current_symbol,
            market_price=self.last_price,
            indicators=self.last_indicators,
            market_structure=self.last_structure,
            last_decision=self.last_decision,
            open_positions=open_positions,
            closed_positions=closed_list,
            account=account,
            stats=stats,
            recent_logs=self.recent_logs[-30:],
            screener_candidates=self.screener_candidates
        )
