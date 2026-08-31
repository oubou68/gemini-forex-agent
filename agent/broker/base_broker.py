from abc import ABC, abstractmethod
from typing import List, Optional
from agent.core.models import Candle, MarketPrice, OrderRequest, Position, AccountSummary


class BaseBroker(ABC):
    """
    Abstrakte Basisklasse für alle Broker-Implementierungen (OANDA Live, Practice, Simulator).
    """

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialisiert die Verbindung und validiert Zugangsdaten."""
        pass

    @abstractmethod
    async def get_account_summary(self) -> AccountSummary:
        """Ruft die Kontozusammenfassung (Balance, Equity, Margin, PnL) ab."""
        pass

    @abstractmethod
    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        """Ruft historische Kerzendaten (OHLCV) für das angegebene Instrument ab."""
        pass

    @abstractmethod
    async def get_current_price(self, instrument: str) -> MarketPrice:
        """Ruft den aktuellen Bid/Ask-Spread und Mittelkurs ab."""
        pass

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> Position:
        """Platziert eine neue Order (Market / Limit) inkl. Stop-Loss & Take-Profit."""
        pass

    @abstractmethod
    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        """Schließt eine offene Position."""
        pass

    @abstractmethod
    async def update_stop_loss(self, position_id: str, new_sl: float) -> Position:
        """Passt das Stop-Loss-Level einer offenen Position an (z.B. Trailing Stop / BE)."""
        pass

    @abstractmethod
    async def get_open_positions(self) -> List[Position]:
        """Gibt alle aktuell aktiven Positionen zurück."""
        pass

    @abstractmethod
    async def get_closed_positions(self) -> List[Position]:
        """Gibt die Historie geschlossener Positionen zurück."""
        pass
