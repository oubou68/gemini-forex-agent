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


class OandaClient(BaseBroker):
    """
    Vollständige Implementierung der OANDA v20 REST API.
    Unterstützt Demo (practice) und Realgeld (live) Konten.
    """

    def __init__(self, api_key: str, account_id: str, environment: str = "practice"):
        self.api_key = api_key
        self.account_id = account_id
        self.environment = environment.lower()
        
        if self.environment == "live":
            self.base_url = "https://api-fxtrade.oanda.com/v3"
            self.stream_url = "https://stream-fxtrade.oanda.com/v3"
        else:
            self.base_url = "https://api-fxpractice.oanda.com/v3"
            self.stream_url = "https://stream-fxpractice.oanda.com/v3"

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339"
        }
        self.open_positions_cache: List[Position] = []
        self.closed_positions_cache: List[Position] = []
        self._initial_equity: float = 10000.0

    def _get_pip_multiplier(self, instrument: str) -> float:
        """Gibt die Pip-Größe zurück (0.01 für JPY-Paare, 0.0001 für andere)."""
        return 0.01 if "JPY" in instrument.upper() else 0.0001

    async def initialize(self) -> bool:
        if not self.api_key or not self.account_id:
            logger.warning("OANDA API Key oder Account ID fehlt. OandaClient kann nicht im Live/Practice-Modus initialisiert werden.")
            return False
        
        try:
            summary = await self.get_account_summary()
            self._initial_equity = summary.balance
            logger.info(f"OANDA Client erfolgreich verbunden. Konto: {summary.account_id}, Balance: {summary.balance} {summary.currency}")
            return True
        except Exception as e:
            logger.error(f"Fehler bei OANDA Initialisierung: {e}")
            return False

    async def get_account_summary(self) -> AccountSummary:
        url = f"{self.base_url}/accounts/{self.account_id}/summary"
        
        def _request():
            return requests.get(url, headers=self.headers, timeout=10)
        
        response = await asyncio.to_thread(_request)
        if response.status_code != 200:
            raise Exception(f"OANDA API Fehler ({response.status_code}): {response.text}")
        
        data = response.json().get("account", {})
        balance = float(data.get("balance", 0.0))
        unrealized_pl = float(data.get("unrealizedPL", 0.0))
        equity = float(data.get("NAV", balance + unrealized_pl))
        margin_used = float(data.get("marginUsed", 0.0))
        margin_available = float(data.get("marginAvailable", equity - margin_used))
        currency = data.get("currency", "EUR")
        open_count = int(data.get("openTradeCount", 0))

        if self._initial_equity == 0:
            self._initial_equity = balance

        daily_dd_pct = max(0.0, ((self._initial_equity - equity) / self._initial_equity) * 100) if self._initial_equity > 0 else 0.0

        return AccountSummary(
            account_id=self.account_id,
            currency=currency,
            balance=balance,
            unrealized_pl=unrealized_pl,
            realized_pl=float(data.get("pl", 0.0)),
            equity=equity,
            margin_used=margin_used,
            margin_available=margin_available,
            open_positions_count=open_count,
            daily_drawdown_pct=round(daily_dd_pct, 2),
            daily_start_equity=self._initial_equity
        )

    async def get_candles(self, instrument: str, granularity: str = "M5", count: int = 100) -> List[Candle]:
        # Formatiere Instrument z.B. EUR_USD
        formatted_instrument = instrument.replace("/", "_").upper()
        url = f"{self.base_url}/instruments/{formatted_instrument}/candles"
        params = {
            "granularity": granularity,
            "count": count,
            "price": "M"  # Midpoint candles
        }

        def _request():
            return requests.get(url, headers=self.headers, params=params, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code != 200:
            raise Exception(f"OANDA Kerzenabruf fehlgeschlagen: {response.text}")

        candles_data = response.json().get("candles", [])
        result = []
        for c in candles_data:
            mid = c.get("mid", {})
            result.append(Candle(
                time=c.get("time", ""),
                open=float(mid.get("o", 0.0)),
                high=float(mid.get("h", 0.0)),
                low=float(mid.get("l", 0.0)),
                close=float(mid.get("c", 0.0)),
                volume=int(c.get("volume", 0)),
                complete=bool(c.get("complete", True))
            ))
        return result

    async def get_current_price(self, instrument: str) -> MarketPrice:
        formatted_instrument = instrument.replace("/", "_").upper()
        url = f"{self.base_url}/accounts/{self.account_id}/pricing"
        params = {"instruments": formatted_instrument}

        def _request():
            return requests.get(url, headers=self.headers, params=params, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code != 200:
            raise Exception(f"OANDA Preisenabruf fehlgeschlagen: {response.text}")

        prices = response.json().get("prices", [])
        if not prices:
            raise Exception(f"Keine Preisdaten für {instrument} erhalten.")

        price_info = prices[0]
        bid = float(price_info.get("bids", [{}])[0].get("price", 0.0))
        ask = float(price_info.get("asks", [{}])[0].get("price", 0.0))
        pip_mult = self._get_pip_multiplier(formatted_instrument)
        spread_pips = round((ask - bid) / pip_mult, 2)
        mid = (bid + ask) / 2.0

        return MarketPrice(
            instrument=formatted_instrument,
            time=price_info.get("time", datetime.utcnow().isoformat()),
            bid=bid,
            ask=ask,
            spread_pips=spread_pips,
            mid=mid
        )

    async def place_order(self, order: OrderRequest) -> Position:
        formatted_instrument = order.instrument.replace("/", "_").upper()
        url = f"{self.base_url}/accounts/{self.account_id}/orders"

        # Units sind positiv für BUY, negativ für SELL
        units = order.units if order.direction == PositionDirection.BUY else -abs(order.units)

        order_data: Dict[str, Any] = {
            "order": {
                "instrument": formatted_instrument,
                "units": str(units),
                "type": "MARKET",
                "positionFill": "DEFAULT",
                "timeInForce": "FOK"
            }
        }

        # Stop Loss
        if order.stop_loss is not None:
            order_data["order"]["stopLossOnFill"] = {
                "price": f"{order.stop_loss:.5f}" if "JPY" not in formatted_instrument else f"{order.stop_loss:.3f}",
                "timeInForce": "GTC"
            }

        # Take Profit
        if order.take_profit is not None:
            order_data["order"]["takeProfitOnFill"] = {
                "price": f"{order.take_profit:.5f}" if "JPY" not in formatted_instrument else f"{order.take_profit:.3f}",
                "timeInForce": "GTC"
            }

        def _request():
            return requests.post(url, headers=self.headers, json=order_data, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code not in (200, 201):
            raise Exception(f"OANDA Orderplatzierung fehlgeschlagen: {response.text}")

        res_json = response.json()
        fill_tx = res_json.get("orderFillTransaction", {})
        trade_opened = fill_tx.get("tradeOpened", {})
        trade_id = trade_opened.get("tradeID", str(fill_tx.get("id", datetime.utcnow().timestamp())))
        fill_price = float(fill_tx.get("price", order.price or 0.0))

        pos = Position(
            id=trade_id,
            instrument=formatted_instrument,
            direction=order.direction,
            units=abs(order.units),
            entry_price=fill_price,
            current_price=fill_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            unrealized_pnl=0.0,
            open_time=datetime.utcnow().isoformat(),
            status=PositionStatus.OPEN
        )
        self.open_positions_cache.append(pos)
        logger.info(f"OANDA Order ausgeführt: {order.direction.value} {order.units} {formatted_instrument} @ {fill_price}")
        return pos

    async def close_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        url = f"{self.base_url}/accounts/{self.account_id}/trades/{position_id}/close"

        def _request():
            return requests.put(url, headers=self.headers, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code not in (200, 201):
            raise Exception(f"OANDA Position schließen fehlgeschlagen: {response.text}")

        res_json = response.json()
        close_tx = res_json.get("orderFillTransaction", {})
        close_price = float(close_tx.get("price", 0.0))
        realized_pl = float(close_tx.get("pl", 0.0))

        target_pos = None
        for p in self.open_positions_cache:
            if p.id == position_id:
                target_pos = p
                break

        if target_pos:
            self.open_positions_cache.remove(target_pos)
            target_pos.status = PositionStatus.CLOSED
            target_pos.current_price = close_price
            target_pos.realized_pnl = realized_pl
            target_pos.close_time = datetime.utcnow().isoformat()
            target_pos.close_reason = reason
            self.closed_positions_cache.append(target_pos)
            return target_pos

        # Fallback Position object
        pos = Position(
            id=position_id,
            instrument="UNKNOWN",
            direction=PositionDirection.BUY,
            units=0,
            entry_price=0.0,
            current_price=close_price,
            realized_pnl=realized_pl,
            open_time=datetime.utcnow().isoformat(),
            close_time=datetime.utcnow().isoformat(),
            status=PositionStatus.CLOSED,
            close_reason=reason
        )
        self.closed_positions_cache.append(pos)
        return pos

    async def update_stop_loss(self, position_id: str, new_sl: float) -> Position:
        url = f"{self.base_url}/accounts/{self.account_id}/trades/{position_id}/orders"
        
        target_pos = next((p for p in self.open_positions_cache if p.id == position_id), None)
        formatted_instrument = target_pos.instrument if target_pos else "EUR_USD"
        sl_formatted = f"{new_sl:.5f}" if "JPY" not in formatted_instrument else f"{new_sl:.3f}"

        data = {
            "stopLoss": {
                "price": sl_formatted,
                "timeInForce": "GTC"
            }
        }

        def _request():
            return requests.put(url, headers=self.headers, json=data, timeout=10)

        response = await asyncio.to_thread(_request)
        if response.status_code not in (200, 201):
            logger.warning(f"Stop-Loss Update fehlgeschlagen: {response.text}")
        
        if target_pos:
            target_pos.stop_loss = new_sl
            return target_pos
        raise Exception(f"Position {position_id} nicht gefunden.")

    async def get_open_positions(self) -> List[Position]:
        # Sync open trades from OANDA
        url = f"{self.base_url}/accounts/{self.account_id}/openTrades"

        def _request():
            return requests.get(url, headers=self.headers, timeout=10)

        try:
            response = await asyncio.to_thread(_request)
            if response.status_code == 200:
                trades = response.json().get("trades", [])
                updated_positions = []
                for t in trades:
                    units_val = int(float(t.get("currentUnits", 0)))
                    direction = PositionDirection.BUY if units_val > 0 else PositionDirection.SELL
                    sl_obj = t.get("stopLossOrder", {})
                    tp_obj = t.get("takeProfitOrder", {})
                    pos = Position(
                        id=str(t.get("id")),
                        instrument=t.get("instrument", ""),
                        direction=direction,
                        units=abs(units_val),
                        entry_price=float(t.get("price", 0.0)),
                        current_price=float(t.get("price", 0.0)),
                        stop_loss=float(sl_obj.get("price")) if sl_obj.get("price") else None,
                        take_profit=float(tp_obj.get("price")) if tp_obj.get("price") else None,
                        unrealized_pnl=float(t.get("unrealizedPL", 0.0)),
                        open_time=t.get("openTime", datetime.utcnow().isoformat()),
                        status=PositionStatus.OPEN
                    )
                    updated_positions.append(pos)
                self.open_positions_cache = updated_positions
        except Exception as e:
            logger.warning(f"Konnte offene OANDA Trades nicht abrufen: {e}")

        return self.open_positions_cache

    async def get_closed_positions(self) -> List[Position]:
        return self.closed_positions_cache
