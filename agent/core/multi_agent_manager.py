import asyncio
import logging
from typing import Dict, Any, Optional, Callable, List

from agent.core.orchestrator import TradingAgentOrchestrator
from agent.core.stock_orchestrator import StockTradingAgentOrchestrator
from config.settings import settings

logger = logging.getLogger(__name__)


class MultiAgentManager:
    """
    Zentraler Manager für Multi-Bot Trading.
    Verwaltet und koordiniert:
    - Bot 1: Forex Agent (OANDA v20 / Simulator)
    - Bot 2: Stock Intraday Agent (Alpaca / Simulator)
    """

    def __init__(self):
        self.forex_agent = TradingAgentOrchestrator()
        self.stock_agent = StockTradingAgentOrchestrator()
        self._telemetry_subscribers: List[Callable[[str, Dict[str, Any]], Any]] = []

    async def initialize(self):
        """Initialisiert beide Trading-Agenten."""
        logger.info("Initialisiere Multi-Agent Manager...")
        await self.forex_agent.initialize()
        await self.stock_agent.initialize()

        # Telemetrie-Forwarding verknüpfen
        self.forex_agent.subscribe_telemetry(self._on_forex_telemetry)
        self.stock_agent.subscribe_telemetry(self._on_stock_telemetry)

    def subscribe_telemetry(self, callback: Callable[[str, Dict[str, Any]], Any]):
        """Callback erhält (bot_type: str, telemetry_dict: dict)."""
        self._telemetry_subscribers.append(callback)

    async def _on_forex_telemetry(self, data: Dict[str, Any]):
        for sub in self._telemetry_subscribers:
            try:
                res = sub("forex", data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug(f"Forex Telemetry Dispatch Fehler: {e}")

    async def _on_stock_telemetry(self, data: Dict[str, Any]):
        for sub in self._telemetry_subscribers:
            try:
                res = sub("stock", data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug(f"Stock Telemetry Dispatch Fehler: {e}")

    async def start_all(self):
        """Startet beide Loops."""
        await self.forex_agent.start()
        await self.stock_agent.start()

    async def stop_all(self):
        """Stoppt beide Loops."""
        await self.forex_agent.stop()
        await self.stock_agent.stop()

    async def start_bot(self, bot_type: str):
        if bot_type.lower() == "forex":
            await self.forex_agent.start()
        elif bot_type.lower() == "stock":
            await self.stock_agent.start()
        else:
            await self.start_all()

    async def stop_bot(self, bot_type: str):
        if bot_type.lower() == "forex":
            await self.forex_agent.stop()
        elif bot_type.lower() == "stock":
            await self.stock_agent.stop()
        else:
            await self.stop_all()

    async def emergency_close_all(self, bot_type: Optional[str] = None) -> Dict[str, int]:
        results = {}
        if not bot_type or bot_type.lower() == "forex":
            results["forex"] = await self.forex_agent.emergency_close_all()
        if not bot_type or bot_type.lower() == "stock":
            results["stock"] = await self.stock_agent.emergency_close_all()
        return results

    async def get_all_telemetry(self) -> Dict[str, Any]:
        forex_tel = await self.forex_agent.get_telemetry()
        stock_tel = await self.stock_agent.get_telemetry()
        return {
            "forex": forex_tel.model_dump(),
            "stock": stock_tel.model_dump()
        }
