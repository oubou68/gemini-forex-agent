import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable

from agent.broker.base_broker import BaseBroker
from agent.broker.simulator import SimulatorBroker
from agent.broker.oanda_client import OandaClient
from agent.analysis.indicators import TechnicalIndicatorEngine
from agent.analysis.market_structure import MarketStructureAnalyzer
from agent.analysis.session import SessionAnalyzer
from agent.ai.gemini_analyst import GeminiForexAnalyst
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


class TradingAgentOrchestrator:
    """
    Zentraler Orchestrator des autonomen Gemini Forex Trading Agenten.
    Koordiniert Marktscan, Indikatorberechnung, Gemini KI-Entscheidungen,
    Risikoprüfung, Orderausführung und Web-Telemetrie.
    """

    def __init__(self):
        self.is_running: bool = False
        self.mode: str = settings.OANDA_ENVIRONMENT.lower()  # "simulator", "practice", "live"
        self.current_instrument: str = settings.DEFAULT_INSTRUMENT
        self.monitored_instruments: List[str] = yaml_config.get("trading", {}).get("instruments", [
            "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "EUR_JPY"
        ])
        self.scan_interval: int = settings.SCAN_INTERVAL_SECONDS
        
        # Subkomponenten initialisieren
        self.broker: BaseBroker = self._create_broker()
        self.indicator_engine = TechnicalIndicatorEngine()
        self.structure_analyzer = MarketStructureAnalyzer()
        self.session_analyzer = SessionAnalyzer()
        self.ai_analyst = GeminiForexAnalyst(
            api_key=settings.GEMINI_API_KEY,
            model_name=settings.GEMINI_MODEL
        )
        self.memory = AgentMemory()
        
        risk_cfg = yaml_config.get("risk", {})
        self.risk_manager = RiskManager(
            risk_per_trade_pct=risk_cfg.get("risk_per_trade_pct", settings.RISK_PERCENT_PER_TRADE),
            max_open_positions=risk_cfg.get("max_open_positions", settings.MAX_OPEN_POSITIONS),
            max_daily_drawdown_pct=risk_cfg.get("max_daily_drawdown_pct", settings.MAX_DAILY_DRAWDOWN_PERCENT),
            max_spread_pips=risk_cfg.get("max_spread_pips", settings.SPREAD_LIMIT_PIPS),
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
        if self.mode in ("practice", "live") and settings.OANDA_API_KEY and settings.OANDA_ACCOUNT_ID:
            logger.info(f"Initialisiere OANDA Client ({self.mode})...")
            return OandaClient(
                api_key=settings.OANDA_API_KEY,
                account_id=settings.OANDA_ACCOUNT_ID,
                environment=self.mode
            )
        else:
            logger.info("Initialisiere Simulator-Broker...")
            return SimulatorBroker(initial_balance=10000.0, currency="EUR")

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
        logger.log(getattr(logging, level, logging.INFO), f"[{category}] {message}")

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
                logger.debug(f"Telemetry Broadcast Callback Fehler: {e}")

    async def set_mode(self, mode: str):
        self.mode = mode.lower()
        self.broker = self._create_broker()
        await self.broker.initialize()
        self.log(f"Broker-Modus gewechselt zu: {self.mode}", "INFO", "CONFIG")
        await self.broadcast_telemetry()

    async def set_instrument(self, instrument: str):
        self.current_instrument = instrument.replace("/", "_").upper()
        self.log(f"Aktives Analyse-Instrument gewechselt zu: {self.current_instrument}", "INFO", "CONFIG")
        await self.run_scan_cycle(self.current_instrument)
        await self.broadcast_telemetry()

    async def initialize(self):
        await self.broker.initialize()
        self.log("Trading Agent Orchestrator erfolgreich initialisiert.", "INFO", "SYSTEM")

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.log("Autonomer Trading-Loop GESTARTET.", "INFO", "SYSTEM")
        self._loop_task = asyncio.create_task(self._orchestrator_loop())
        await self.broadcast_telemetry()

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
        self.log("Autonomer Trading-Loop PAUSIERT.", "WARNING", "SYSTEM")
        await self.broadcast_telemetry()

    async def _orchestrator_loop(self):
        while self.is_running:
            try:
                # Scanne das primäre Instrument
                await self.run_scan_cycle(self.current_instrument)
                
                # Checke offene Positionen auf Trailing Stop / BE
                await self._manage_open_positions()

                # Broadcast Live State
                await self.broadcast_telemetry()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"Fehler im Orchestrator-Loop: {e}", "ERROR", "SYSTEM")
            
            await asyncio.sleep(self.scan_interval)

    async def run_scan_cycle(self, instrument: str):
        """Führt einen kompletten Analyse- und Entscheidungszyklus durch."""
        try:
            # 1. Preis- & Kerzendaten abrufen
            price = await self.broker.get_current_price(instrument)
            candles = await self.broker.get_candles(instrument, granularity="M5", count=100)
            self.last_price = price

            # 2. Technische Indikatoren berechnen
            indicators = self.indicator_engine.calculate_indicators(candles)
            self.last_indicators = indicators

            # 3. Session & Marktstruktur
            session_name = self.session_analyzer.get_current_session()
            structure = self.structure_analyzer.analyze(candles, current_session=session_name)
            self.last_structure = structure

            # 4. Account & offene Positionen
            account = await self.broker.get_account_summary()
            open_positions = await self.broker.get_open_positions()
            active_for_pair = len([p for p in open_positions if p.instrument == instrument])

            # 5. Gemini KI Entscheidungsfindung
            recent_context = self.memory.get_recent_history_context(limit=3)
            decision = await self.ai_analyst.analyze_market(
                instrument=instrument,
                price=price,
                indicators=indicators,
                structure=structure,
                session_name=session_name,
                open_positions_count=active_for_pair,
                daily_drawdown_pct=account.daily_drawdown_pct,
                recent_history_context=recent_context
            )
            self.last_decision = decision

            # 6. Signal-Ausführung & Risikoprüfung
            if decision.action in (TradeAction.BUY, TradeAction.SELL):
                self.log(
                    f"KI Signal generiert: {decision.action.value} {instrument} (Konfidenz: {decision.confidence}%) | Setup: {decision.setup_type}",
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
                    # Berechne Units
                    units = self.risk_manager.calculate_position_size(
                        equity=account.equity,
                        entry_price=decision.entry_price or price.mid,
                        stop_loss_price=decision.stop_loss or (price.mid * 0.99),
                        instrument=instrument,
                        risk_pct=decision.suggested_risk_pct
                    )

                    direction = PositionDirection.BUY if decision.action == TradeAction.BUY else PositionDirection.SELL
                    order = OrderRequest(
                        instrument=instrument,
                        direction=direction,
                        units=units,
                        order_type="MARKET",
                        price=decision.entry_price,
                        stop_loss=decision.stop_loss,
                        take_profit=decision.take_profit_1
                    )

                    pos = await self.broker.place_order(order)
                    self.log(
                        f"Order erfolgreich platziert: {direction.value} {units} Units {instrument} @ {pos.entry_price} (SL: {pos.stop_loss}, TP: {pos.take_profit})",
                        "INFO",
                        "EXECUTION"
                    )
                else:
                    self.log(f"Trade abgelehnt durch Risikomanager: {reason}", "WARNING", "RISK")

            elif decision.action == TradeAction.CLOSE:
                # Schließe offene Positionen für dieses Instrument
                for p in open_positions:
                    if p.instrument == instrument:
                        closed_pos = await self.broker.close_position(p.id, reason="AGENT_SIGNAL")
                        self.memory.record_closed_trade(closed_pos, thesis=decision.thesis_summary)
                        self.log(f"Position {p.id} durch KI Signal geschlossen. PnL={closed_pos.realized_pnl} EUR", "INFO", "EXECUTION")

        except Exception as e:
            self.log(f"Fehler im Scan-Zyklus für {instrument}: {e}", "ERROR", "ANALYSIS")

    async def _manage_open_positions(self):
        """Überwacht Break-Even und Trailing Stop für aktive Trades."""
        try:
            open_positions = await self.broker.get_open_positions()
            for pos in open_positions:
                price_info = await self.broker.get_current_price(pos.instrument)
                curr_price = price_info.bid if pos.direction == PositionDirection.BUY else price_info.ask
                
                # Check BE Anpassung
                new_sl = self.risk_manager.evaluate_breakeven_adjustment(pos, curr_price)
                if new_sl and (pos.stop_loss is None or new_sl != pos.stop_loss):
                    await self.broker.update_stop_loss(pos.id, new_sl)
                    self.log(f"Stop-Loss für Position {pos.id} ({pos.instrument}) auf Break-Even nachgezogen ({new_sl})", "INFO", "RISK")
        except Exception as e:
            logger.debug(f"Fehler bei Positionsüberwachung: {e}")

    async def emergency_close_all(self) -> int:
        """Schließt unverzüglich alle offenen Positionen."""
        open_positions = await self.broker.get_open_positions()
        count = 0
        for p in list(open_positions):
            try:
                closed = await self.broker.close_position(p.id, reason="EMERGENCY_CLOSE")
                self.memory.record_closed_trade(closed, thesis="Manueller Notstopp ausgelöst.")
                count += 1
            except Exception as e:
                self.log(f"Fehler beim Schließen von Position {p.id}: {e}", "ERROR", "EMERGENCY")
        
        self.log(f"NOTFALL-STOPP: {count} Position(en) geschlossen.", "WARNING", "EMERGENCY")
        await self.broadcast_telemetry()
        return count

    async def execute_manual_trade(self, instrument: str, direction: str, risk_pct: float = 1.0) -> Position:
        """Führt eine manuelle Order über das Web-Dashboard aus."""
        price = await self.broker.get_current_price(instrument)
        account = await self.broker.get_account_summary()
        pip = 0.01 if "JPY" in instrument else 0.0001
        
        entry = price.ask if direction.upper() == "BUY" else price.bid
        atr = self.last_indicators.atr_14 if self.last_indicators and self.last_indicators.atr_14 else (20 * pip)
        
        sl = round(entry - (1.5 * atr) if direction.upper() == "BUY" else entry + (1.5 * atr), 5 if pip == 0.0001 else 3)
        tp = round(entry + (2.5 * atr) if direction.upper() == "BUY" else entry - (2.5 * atr), 5 if pip == 0.0001 else 3)

        units = self.risk_manager.calculate_position_size(
            equity=account.equity,
            entry_price=entry,
            stop_loss_price=sl,
            instrument=instrument,
            risk_pct=risk_pct
        )

        pos_dir = PositionDirection.BUY if direction.upper() == "BUY" else PositionDirection.SELL
        order = OrderRequest(
            instrument=instrument,
            direction=pos_dir,
            units=units,
            order_type="MARKET",
            price=entry,
            stop_loss=sl,
            take_profit=tp
        )
        pos = await self.broker.place_order(order)
        self.log(f"Manuelle Order ausgeführt: {direction} {units} {instrument} @ {entry}", "INFO", "MANUAL")
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
            current_instrument=self.current_instrument,
            market_price=self.last_price,
            indicators=self.last_indicators,
            market_structure=self.last_structure,
            last_decision=self.last_decision,
            open_positions=open_positions,
            account=account,
            stats=stats,
            recent_logs=self.recent_logs[-30:]
        )
