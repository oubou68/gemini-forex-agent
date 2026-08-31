import logging
import asyncio
from datetime import datetime
from typing import List, Optional, Dict, Any
import requests

from agent.broker.base_broker import BaseBroker
from agent.core.models import (
    Candle, MarketPrice, OrderRequest, Position,
    PositionDirection, PositionStatus, AccountSummary
)

logger = logging.getLogger(__name__)


class AlpacaClient(BaseBroker):
    """
    Vollständige Implementierung der Alpaca Markets v2 REST API für US-Aktien.
    Unterstützt Paper-Trading (paper-api.alpaca.markets) und Live-Trading (api.alpaca.markets).
    """

    def __init__(self, api_key: str, secret_key: str, environment: str = "paper"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.environment = environment.lower()

        if self.environment == "live":
            self.base_url = "https://api.alpaca.markets/v2"
        else:
            self.base_url = "https://paper-api.alpaca.markets/v2"
        
        self.data_url = "https://data.alpaca.markets/v2"

        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        self.open_positions_cache: List[Position] = []
        self.closed_positions_cache: List[Position] = []
        self._initial_equity: float = 100000.0
        self._daily_start_equity: float = 100000.0

    async def initialize(self) -> bool:
        """Initialisiert die Verbindung zu Alpaca und validiert die API Keys."""
        if not self.api_key or not self.secret_key:
            logger.warning("Alpaca API Key oder Secret Key fehlt. AlpacaClient kann nicht im Live/Paper-Modus initialisiert werden.")
            return False

        try:
            summary = await self.get_account_summary()
            self._initial_equity = summary.balance
            self._daily_start_equity = summary.balance
            logger.info(f"Alpaca Client erfolgreich verbunden ({self.environment.upper()}). Konto: {summary.account_id}, Equity: ${summary.equity:,.2f} {summary.currency}")
            return True
        except Exception as e:
            logger.error(f"Fehler bei Alpaca Initialisierung: {e}")
            return False

    async def get_account_summary(self) -> AccountSummary:
        """Ruft Kontodaten von Alpaca /v2/account ab."""
        url = f"{self.base_url}/account"

        def _request():
            return requests.get(url, headers=self.headers, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code != 200:
            raise Exception(f"Alpaca Account API Fehler ({response.status_code}): {response.text}")

        data = response.json()
        account_id = data.get("id", "alpaca-acc")
        currency = data.get("currency", "USD")
        equity = float(data.get("equity", data.get("portfolio_value", 100000.0)))
        balance = float(data.get("cash", equity))
        buying_power = float(data.get("buying_power", equity * 2))
        
        # Berechne Realized / Unrealized Pl
        positions = await self.get_open_positions()
        unrealized_pl = sum(p.unrealized_pnl for p in positions)
        realized_pl = sum(p.realized_pnl for p in self.closed_positions_cache)
        margin_used = max(0.0, equity - float(data.get("cash", equity)))

        if self._daily_start_equity <= 0:
            self._daily_start_equity = equity

        # Daily Drawdown
        daily_dd_pct = 0.0
        if self._daily_start_equity > 0:
            dd = (self._daily_start_equity - equity) / self._daily_start_equity * 100.0
            daily_dd_pct = max(0.0, round(dd, 2))

        return AccountSummary(
            account_id=account_id,
            currency=currency,
            balance=balance,
            unrealized_pl=round(unrealized_pl, 2),
            realized_pl=round(realized_pl, 2),
            equity=round(equity, 2),
            margin_used=round(margin_used, 2),
            margin_available=round(buying_power, 2),
            open_positions_count=len(positions),
            daily_drawdown_pct=daily_dd_pct,
            daily_start_equity=self._daily_start_equity
        )

    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        """
        Ruft historische Aktien-Kerzendaten (Bars) über Alpaca Market Data v2 ab.
        Mapping von M5 -> 5Min, M15 -> 15Min, H1 -> 1Hour, D -> 1Day.
        """
        symbol = instrument.replace("/", "").replace("_", "").upper()
        
        tf_map = {
            "M1": "1Min",
            "M5": "5Min",
            "M15": "15Min",
            "M30": "30Min",
            "H1": "1Hour",
            "D": "1Day"
        }
        timeframe = tf_map.get(granularity, "5Min")
        url = f"{self.data_url}/stocks/{symbol}/bars"
        params = {
            "timeframe": timeframe,
            "limit": min(count, 1000),
            "adjustment": "all",
            "feed": "iex"  # standard free feed or sip for paid
        }

        def _request():
            return requests.get(url, headers=self.headers, params=params, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code != 200:
            # Fallback auf /v2/stocks/bars?symbols=
            url_alt = f"{self.data_url}/stocks/bars"
            params_alt = {
                "symbols": symbol,
                "timeframe": timeframe,
                "limit": min(count, 1000),
                "feed": "iex"
            }
            def _request_alt():
                return requests.get(url_alt, headers=self.headers, params=params_alt, timeout=10)
            
            response = await asyncio.to_thread(_request_alt)
            if response.status_code != 200:
                raise Exception(f"Alpaca Bars Fehler ({response.status_code}): {response.text}")

        data = response.json()
        raw_bars = data.get("bars", [])
        if isinstance(raw_bars, dict):
            raw_bars = raw_bars.get(symbol, [])

        candles = []
        for b in raw_bars:
            t = b.get("t", datetime.utcnow().isoformat())
            candles.append(Candle(
                time=t if t.endswith("Z") else t + "Z",
                open=round(float(b.get("o", 0.0)), 2),
                high=round(float(b.get("h", 0.0)), 2),
                low=round(float(b.get("l", 0.0)), 2),
                close=round(float(b.get("c", 0.0)), 2),
                volume=int(b.get("v", 0)),
                complete=True
            ))

        return candles

    async def get_current_price(self, instrument: str) -> MarketPrice:
        """Ruft den aktuellen Bid/Ask-Spread und Mittelkurs für eine Aktie ab."""
        symbol = instrument.replace("/", "").replace("_", "").upper()
        url = f"{self.data_url}/stocks/{symbol}/quotes/latest"
        params = {"feed": "iex"}

        def _request():
            return requests.get(url, headers=self.headers, params=params, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code == 200:
            data = response.json()
            quote = data.get("quote", {})
            bid = float(quote.get("bp", 0.0))
            ask = float(quote.get("ap", 0.0))
            t = quote.get("t", datetime.utcnow().isoformat())
            
            if bid > 0 and ask > 0:
                mid = round((bid + ask) / 2.0, 2)
                spread_cents = round(ask - bid, 2)
                return MarketPrice(
                    instrument=symbol,
                    time=t if t.endswith("Z") else t + "Z",
                    bid=round(bid, 2),
                    ask=round(ask, 2),
                    spread_pips=spread_cents,  # Spread in Cents/USD
                    mid=mid
                )

        # Fallback: Latest Trade
        trade_url = f"{self.data_url}/stocks/{symbol}/trades/latest"
        def _trade_request():
            return requests.get(trade_url, headers=self.headers, params=params, timeout=10)
        
        t_res = await asyncio.to_thread(_trade_request)
        if t_res.status_code == 200:
            t_data = t_res.json().get("trade", {})
            p = float(t_data.get("p", 100.0))
            spread_est = 0.02
            return MarketPrice(
                instrument=symbol,
                time=datetime.utcnow().isoformat() + "Z",
                bid=round(p - spread_est / 2, 2),
                ask=round(p + spread_est / 2, 2),
                spread_pips=spread_est,
                mid=round(p, 2)
            )

        raise Exception(f"Konnte aktuellen Preis für Aktie {symbol} nicht abrufen: {response.text}")

    async def place_order(self, order: OrderRequest) -> Position:
        """
        Platziert eine neue Aktien-Order bei Alpaca (Market oder Bracket mit Stop-Loss & Take-Profit).
        """
        symbol = order.instrument.replace("/", "").replace("_", "").upper()
        side = "buy" if order.direction == PositionDirection.BUY else "sell"
        qty = max(1, order.units)

        payload: Dict[str, Any] = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order.order_type.lower(),
            "time_in_force": "day"
        }

        # Bracket Order wenn SL oder TP definiert
        if order.stop_loss and order.take_profit:
            payload["order_class"] = "bracket"
            payload["stop_loss"] = {"stop_price": str(round(order.stop_loss, 2))}
            payload["take_profit"] = {"limit_price": str(round(order.take_profit, 2))}
        elif order.stop_loss:
            payload["order_class"] = "oto"
            payload["stop_loss"] = {"stop_price": str(round(order.stop_loss, 2))}
        elif order.take_profit:
            payload["order_class"] = "oto"
            payload["take_profit"] = {"limit_price": str(round(order.take_profit, 2))}

        url = f"{self.base_url}/orders"
        def _request():
            return requests.post(url, headers=self.headers, json=payload, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code not in (200, 201):
            raise Exception(f"Alpaca Order Fehler ({response.status_code}): {response.text}")

        data = response.json()
        order_id = data.get("id", f"alpaca_{datetime.utcnow().timestamp()}")
        filled_price = float(data.get("filled_avg_price") or order.price or (await self.get_current_price(symbol)).mid)

        pos = Position(
            id=order_id,
            instrument=symbol,
            direction=order.direction,
            units=qty,
            entry_price=round(filled_price, 2),
            current_price=round(filled_price, 2),
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            open_time=datetime.utcnow().isoformat() + "Z",
            status=PositionStatus.OPEN
        )
        self.open_positions_cache.append(pos)
        return pos

    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        """Schließt eine offene Aktienposition bei Alpaca (DELETE /v2/positions/{symbol})."""
        # Suche Position
        pos_to_close = None
        for p in self.open_positions_cache:
            if p.id == position_id or p.instrument == position_id:
                pos_to_close = p
                break

        symbol = (pos_to_close.instrument if pos_to_close else position_id).replace("/", "").replace("_", "").upper()
        url = f"{self.base_url}/positions/{symbol}"

        def _request():
            return requests.delete(url, headers=self.headers, timeout=10)

        response = await asyncio.to_thread(_request)
        # Wenn Status 200 oder Position nicht existent (schon geschlossen)
        if response.status_code not in (200, 204, 404):
            logger.warning(f"Alpaca Position Close Warnung: {response.text}")

        curr_price_info = await self.get_current_price(symbol)
        curr_price = curr_price_info.mid

        if pos_to_close:
            self.open_positions_cache.remove(pos_to_close)
            if pos_to_close.direction == PositionDirection.BUY:
                pnl = (curr_price - pos_to_close.entry_price) * pos_to_close.units
            else:
                pnl = (pos_to_close.entry_price - curr_price) * pos_to_close.units

            closed_pos = Position(
                id=pos_to_close.id,
                instrument=symbol,
                direction=pos_to_close.direction,
                units=pos_to_close.units,
                entry_price=pos_to_close.entry_price,
                current_price=curr_price,
                stop_loss=pos_to_close.stop_loss,
                take_profit=pos_to_close.take_profit,
                unrealized_pnl=0.0,
                realized_pnl=round(pnl, 2),
                open_time=pos_to_close.open_time,
                close_time=datetime.utcnow().isoformat() + "Z",
                status=PositionStatus.CLOSED,
                close_reason=reason
            )
            self.closed_positions_cache.append(closed_pos)
            return closed_pos

        return Position(
            id=position_id,
            instrument=symbol,
            direction=PositionDirection.BUY,
            units=1,
            entry_price=curr_price,
            current_price=curr_price,
            status=PositionStatus.CLOSED,
            close_reason=reason,
            open_time=datetime.utcnow().isoformat() + "Z",
            close_time=datetime.utcnow().isoformat() + "Z"
        )

    async def update_stop_loss(self, position_id: str, new_sl: float) -> Position:
        """Passt das Stop-Loss-Level einer bestehenden Aktienposition an."""
        for pos in self.open_positions_cache:
            if pos.id == position_id:
                pos.stop_loss = round(new_sl, 2)
                return pos
        raise Exception(f"Position {position_id} nicht im lokalen Cache gefunden.")

    async def get_open_positions(self) -> List[Position]:
        """Ruft alle aktiven Aktienpositionen via Alpaca GET /v2/positions ab."""
        url = f"{self.base_url}/positions"
        def _request():
            return requests.get(url, headers=self.headers, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code != 200:
            return self.open_positions_cache

        data = response.json()
        live_positions: List[Position] = []
        for item in data:
            sym = item.get("symbol", "")
            side_str = item.get("side", "long").lower()
            direction = PositionDirection.BUY if side_str == "long" else PositionDirection.SELL
            qty = abs(int(float(item.get("qty", 1))))
            entry = float(item.get("avg_entry_price", 0.0))
            curr = float(item.get("current_price", entry))
            unrealized = float(item.get("unrealized_pl", 0.0))

            # Finde gemerkte SL/TP aus Cache
            matched_cached = next((p for p in self.open_positions_cache if p.instrument == sym), None)
            sl = matched_cached.stop_loss if matched_cached else None
            tp = matched_cached.take_profit if matched_cached else None

            pos = Position(
                id=item.get("asset_id", f"alpaca_{sym}"),
                instrument=sym,
                direction=direction,
                units=qty,
                entry_price=round(entry, 2),
                current_price=round(curr, 2),
                stop_loss=sl,
                take_profit=tp,
                unrealized_pnl=round(unrealized, 2),
                realized_pnl=0.0,
                open_time=matched_cached.open_time if matched_cached else datetime.utcnow().isoformat() + "Z",
                status=PositionStatus.OPEN
            )
            live_positions.append(pos)

        self.open_positions_cache = live_positions
        return self.open_positions_cache

    async def get_closed_positions(self) -> List[Position]:
        """Gibt die Liste der geschlossenen Trades zurück."""
        return self.closed_positions_cache
