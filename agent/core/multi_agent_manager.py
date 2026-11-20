import asyncio
import logging
from typing import Dict, Any, Optional, Callable, List

from agent.core.orchestrator import TradingAgentOrchestrator
from agent.core.stock_orchestrator import StockTradingAgentOrchestrator
from agent.core.crypto_orchestrator import CryptoTradingAgentOrchestrator

logger = logging.getLogger(__name__)


class MultiAgentManager:
    """
    Zentraler Manager für Multi-Bot Trading.
    Verwaltet und koordiniert:
    - Bot 1: Forex Agent (OANDA v20 / Simulator)
    - Bot 2: Stock Intraday Agent (Alpaca Equities / Simulator)
    - Bot 3: Crypto 24/7 Spot Agent (Alpaca Crypto Top 20 / Simulator)
    """

    def __init__(self):
        self.forex_agent = TradingAgentOrchestrator()
        self.stock_agent = StockTradingAgentOrchestrator()
        self.crypto_agent = CryptoTradingAgentOrchestrator()
        self._telemetry_subscribers: List[Callable[[str, Dict[str, Any]], Any]] = []

    async def initialize(self):
        """Initialisiert alle drei Trading-Agenten."""
        logger.info("Initialisiere Multi-Agent Manager (Forex, Stocks & Crypto 24/7)...")
        await self.forex_agent.initialize()
        await self.stock_agent.initialize()
        await self.crypto_agent.initialize()

        self.forex_agent.subscribe_telemetry(self._on_forex_telemetry)
        self.stock_agent.subscribe_telemetry(self._on_stock_telemetry)
        self.crypto_agent.subscribe_telemetry(self._on_crypto_telemetry)

    def subscribe_telemetry(self, callback: Callable[[str, Dict[str, Any]], Any]):
        """Callback erhält (bot_type: str, telemetry_dict: dict)."""
        self._telemetry_subscribers.append(callback)

    async def _dispatch_telemetry(self, bot_type: str, data: Dict[str, Any]):
        for sub in self._telemetry_subscribers:
            try:
                res = sub(bot_type, data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug(f"{bot_type} Telemetry Dispatch Fehler: {e}")

    async def _on_forex_telemetry(self, data: Dict[str, Any]):
        await self._dispatch_telemetry("forex", data)

    async def _on_stock_telemetry(self, data: Dict[str, Any]):
        await self._dispatch_telemetry("stock", data)

    async def _on_crypto_telemetry(self, data: Dict[str, Any]):
        await self._dispatch_telemetry("crypto", data)

    async def start_all(self):
        """Startet alle drei autonomen Loops."""
        await self.forex_agent.start()
        await self.stock_agent.start()
        await self.crypto_agent.start()

    async def stop_all(self):
        """Stoppt alle drei Loops."""
        await self.forex_agent.stop()
        await self.stock_agent.stop()
        await self.crypto_agent.stop()

    async def start_bot(self, bot_type: str):
        bt = bot_type.lower()
        if bt == "forex":
            await self.forex_agent.start()
        elif bt == "stock":
            await self.stock_agent.start()
        elif bt == "crypto":
            await self.crypto_agent.start()
        else:
            await self.start_all()

    async def stop_bot(self, bot_type: str):
        bt = bot_type.lower()
        if bt == "forex":
            await self.forex_agent.stop()
        elif bt == "stock":
            await self.stock_agent.stop()
        elif bt == "crypto":
            await self.crypto_agent.stop()
        else:
            await self.stop_all()

    async def emergency_close_all(self, bot_type: Optional[str] = None) -> Dict[str, int]:
        results = {}
        bt = bot_type.lower() if bot_type else None
        if not bt or bt == "forex":
            results["forex"] = await self.forex_agent.emergency_close_all()
        if not bt or bt == "stock":
            results["stock"] = await self.stock_agent.emergency_close_all()
        if not bt or bt == "crypto":
            results["crypto"] = await self.crypto_agent.emergency_close_all()
        return results

    async def get_all_telemetry(self) -> Dict[str, Any]:
        forex_tel = await self.forex_agent.get_telemetry()
        stock_tel = await self.stock_agent.get_telemetry()
        crypto_tel = await self.crypto_agent.get_telemetry()
        return {
            "forex": forex_tel.model_dump(),
            "stock": stock_tel.model_dump(),
            "crypto": crypto_tel.model_dump()
        }
