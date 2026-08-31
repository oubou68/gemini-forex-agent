import asyncio
import logging
import math
from datetime import datetime
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
    TradePerformanceStats, AgentTelemetry
)
from config.settings import settings, yaml_config

logger = logging.getLogger(__name__)


class StockTradingAgentOrchestrator:
    """
    Zentraler Orchestrator des autonomen Gemini Intraday-Aktien-Trading-Agenten (Alpaca).
    Koordiniert Marktscan für US-Aktien/ETFs (AAPL, NVDA, TSLA, SPY, etc.),
    VWAP & ORB Indikatorberechnung, Gemini KI-Entscheidungen,
    Position Sizing in Shares, Orderausführung und Web-Telemetrie.
    """

    def __init__(self):
        self.is_running: bool = False
        self.mode: str = settings.ALPACA_ENVIRONMENT.lower()  # "simulator", "paper", "live"
        self.current_symbol: str = settings.DEFAULT_STOCK_SYMBOL
        self.monitored_symbols: List[str] = yaml_config.get("stock_trading", {}).get("symbols", [
            "AAPL", "NVDA", "TSLA", "SPY", "QQQ", "MSFT", "AMD"
        ])
        self.scan_interval: int = settings.STOCK_SCAN_INTERVAL_SECONDS

        # Subkomponenten initialisieren
        self.broker: BaseBroker = self._create_broker()
        self.indicator_engine = StockTechnicalIndicatorEngine()
        self.structure_analyzer = MarketStructureAnalyzer()
        self.session_analyzer = StockSessionAnalyzer()
        self.ai_analyst = GeminiStockAnalyst(
            api_key=settings.GEMINI_API_KEY,
            model_name=settings.GEMINI_MODEL
        )
        self.memory = AgentMemory()

        risk_cfg = yaml_config.get("risk", {})
        self.risk_manager = RiskManager(
            risk_per_trade_pct=risk_cfg.get("risk_per_trade_pct", settings.STOCK_RISK_PERCENT_PER_TRADE),
            max_open_positions=risk_cfg.get("max_open_positions", settings.STOCK_MAX_OPEN_POSITIONS),
            max_daily_drawdown_pct=risk_cfg.get("max_daily_drawdown_pct", settings.STOCK_MAX_DAILY_DRAWDOWN_PERCENT),
            max_spread_pips=risk_cfg.get("max_spread_dollars", 0.25),
            min_risk_reward_ratio=risk_cfg.get("min_risk_reward_ratio", 1.5),
            confidence_threshold=yaml_config.get("gemini", {}).get("confidence_threshold", 65.0),
            breakeven_trigger_r=risk_cfg.get("breakeven_trigger_r", 1.0)
        )

        # Telemetrie & State Cache
        self.last_price: Optional[MarketPrice] = None
        self.last_indicators: Optional[IndicatorValues] = None
        self.last_structure: Optional[MarketStructure] = None
        self.last_decision: Optional[GeminiTradeDecision] = None
        self.recent_logs: List[Dict[str, Any]] = []
        self._telemetry_subscribers: List[Callable[[Dict[str, Any]], Any]] = []
        self._loop_task: Optional[asyncio.Task] = None

    def _create_broker(self) -> BaseBroker:
        if self.mode in ("paper", "live") and settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY:
            logger.info(f"Initialisiere Alpaca Client ({self.mode})...")
            return AlpacaClient(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                environment=self.mode
            )
        else:
            logger.info("Initialisiere Stock Simulator Broker (USD)...")
            return StockSimulatorBroker(initial_balance=100000.0, currency="USD")

    def log(self, message: str, level: str = "INFO", category: str = "SYSTEM"):
        timestamp = datetime.utcnow().strftime("%H:%M:%S")
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
        self.log("Stock Intraday Agent Orchestrator erfolgreich initialisiert.", "INFO", "SYSTEM")

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.log("Autonomer Aktien-Trading-Loop GESTARTET.", "INFO", "SYSTEM")
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

    async def _orchestrator_loop(self):
        while self.is_running:
            try:
                # Scanne das primäre Aktien-Symbol
                await self.run_scan_cycle(self.current_symbol)

                # Checke offene Positionen auf Trailing Stop / BE
                await self._manage_open_positions()

                # Broadcast Live State
                await self.broadcast_telemetry()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"Fehler im Stock-Orchestrator-Loop: {e}", "ERROR", "SYSTEM")

            await asyncio.sleep(self.scan_interval)

    def calculate_shares_size(self, equity: float, entry_price: float, stop_loss_price: float, risk_pct: float = 1.0, max_capital_pct: float = 0.50) -> int:
        """
        Berechnet die exakte Anzahl der Aktien (Shares) basierend auf dem maximalen $-Risiko pro Trade.
        """
        if entry_price <= 0 or stop_loss_price <= 0 or entry_price == stop_loss_price:
            return 1

        risk_amount = equity * (risk_pct / 100.0)
        risk_per_share = abs(entry_price - stop_loss_price)
        shares = math.floor(risk_amount / max(0.05, risk_per_share))
        
        # Max Buying-Power Schutz (maximal max_capital_pct des Gesamtkapitals in eine einzelne Position)
        max_shares_capital = math.floor((equity * max_capital_pct) / entry_price)
        final_shares = max(1, min(shares, max_shares_capital if max_shares_capital > 0 else shares))
        return final_shares

    async def run_scan_cycle(self, symbol: str):
        """Führt einen kompletten Analyse- und Entscheidungszyklus für US-Aktien durch."""
        try:
            # 1. Preis- & Kerzendaten abrufen
            price = await self.broker.get_current_price(symbol)
            candles = await self.broker.get_candles(symbol, granularity="M5", count=100)
            self.last_price = price

            # 2. Indikatoren berechnen (VWAP, ORB, RVOL, EMAs, RSI, MACD)
            indicators = self.indicator_engine.calculate_indicators(candles, orb_bars=3)
            self.last_indicators = indicators

            # 3. Session & Marktstruktur
            session_name = self.session_analyzer.get_current_session()
            structure = self.structure_analyzer.analyze(candles, current_session=session_name)
            
            # ORB Bias anreichern
            if indicators.orb_high and price.mid > indicators.orb_high:
                structure.orb_bias = "ABOVE_ORB_HIGH"
            elif indicators.orb_low and price.mid < indicators.orb_low:
                structure.orb_bias = "BELOW_ORB_LOW"
            else:
                structure.orb_bias = "INSIDE_ORB_RANGE"

            self.last_structure = structure

            # 4. Account & offene Positionen
            account = await self.broker.get_account_summary()
            open_positions = await self.broker.get_open_positions()
            active_for_symbol = len([p for p in open_positions if p.instrument == symbol])

            # 5. Gemini KI Entscheidungsfindung für Aktien
            recent_context = self.memory.get_recent_history_context(limit=3)
            decision = await self.ai_analyst.analyze_market(
                symbol=symbol,
                price=price,
                indicators=indicators,
                structure=structure,
                session_name=session_name,
                open_positions_count=active_for_symbol,
                daily_drawdown_pct=account.daily_drawdown_pct,
                recent_history_context=recent_context
            )
            self.last_decision = decision

            # 6. Signal-Ausführung & Risikoprüfung
            if decision.action in (TradeAction.BUY, TradeAction.SELL):
                self.log(
                    f"Stock KI Signal generiert: {decision.action.value} {symbol} (Konfidenz: {decision.confidence}%) | Setup: {decision.setup_type}",
                    "INFO",
                    "AI_SIGNAL"
                )

                # Risikoprüfung
                is_valid, reason = self.risk_manager.validate_trade_decision(
                    decision=decision,
                    account=account,
                    spread_pips=price.spread_pips,
                    open_positions=open_positions
                )

                if is_valid:
                    entry = decision.entry_price or price.mid
                    sl = decision.stop_loss or (entry * 0.98 if decision.action == TradeAction.BUY else entry * 1.02)
                    shares = self.calculate_shares_size(
                        equity=account.equity,
                        entry_price=entry,
                        stop_loss_price=sl,
                        risk_pct=decision.suggested_risk_pct
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
                        f"Stock Order ausgeführt: {direction.value} {shares} Shares {symbol} @ ${pos.entry_price:.2f} (SL: ${pos.stop_loss}, TP: ${pos.take_profit})",
                        "INFO",
                        "EXECUTION"
                    )
                else:
                    self.log(f"Stock Trade abgelehnt durch Risikomanager: {reason}", "WARNING", "RISK")

            elif decision.action == TradeAction.CLOSE:
                for p in open_positions:
                    if p.instrument == symbol:
                        closed_pos = await self.broker.close_position(p.id, reason="AGENT_SIGNAL")
                        self.memory.record_closed_trade(closed_pos, thesis=decision.thesis_summary)
                        self.log(f"Position {p.id} ({symbol}) geschlossen. PnL=${closed_pos.realized_pnl:,.2f} USD", "INFO", "EXECUTION")

        except Exception as e:
            self.log(f"Fehler im Stock Scan-Zyklus für {symbol}: {e}", "ERROR", "ANALYSIS")

    async def _manage_open_positions(self):
        """Überwacht Break-Even und Trailing Stop für aktive Aktien-Trades."""
        try:
            open_positions = await self.broker.get_open_positions()
            for pos in open_positions:
                price_info = await self.broker.get_current_price(pos.instrument)
                curr_price = price_info.bid if pos.direction == PositionDirection.BUY else price_info.ask

                # Check Break-Even Anpassung
                new_sl = self.risk_manager.evaluate_breakeven_adjustment(pos, curr_price)
                if new_sl and (pos.stop_loss is None or abs(new_sl - pos.stop_loss) > 0.05):
                    await self.broker.update_stop_loss(pos.id, new_sl)
                    self.log(f"Stop-Loss für Aktie {pos.instrument} ({pos.id}) auf Break-Even nachgezogen (${new_sl:.2f})", "INFO", "RISK")
        except Exception as e:
            logger.debug(f"Fehler bei Stock Positionsüberwachung: {e}")

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
        atr = self.last_indicators.atr_14 if self.last_indicators and self.last_indicators.atr_14 else max(0.50, round(entry * 0.01, 2))

        sl = round(entry - (1.5 * atr) if direction.upper() == "BUY" else entry + (1.5 * atr), 2)
        tp = round(entry + (2.5 * atr) if direction.upper() == "BUY" else entry - (2.5 * atr), 2)

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
        stats = self.memory.get_performance_stats()

        return AgentTelemetry(
            timestamp=datetime.utcnow().isoformat() + "Z",
            is_running=self.is_running,
            mode=self.mode,
            current_instrument=self.current_symbol,
            market_price=self.last_price,
            indicators=self.last_indicators,
            market_structure=self.last_structure,
            last_decision=self.last_decision,
            open_positions=open_positions,
            account=account,
            stats=stats,
            recent_logs=self.recent_logs[-30:]
        )
