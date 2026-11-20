import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable

from agent.broker.base_broker import BaseBroker
from agent.broker.crypto_simulator import CryptoSimulatorBroker
from agent.broker.alpaca_crypto_client import AlpacaCryptoClient, normalize_crypto_symbol
from agent.analysis.crypto_indicators import CryptoTechnicalIndicatorEngine, _crypto_precision
from agent.analysis.market_structure import MarketStructureAnalyzer
from agent.analysis.crypto_session import CryptoSessionAnalyzer
from agent.ai.crypto_analyst import GeminiCryptoAnalyst
from agent.ai.memory import AgentMemory
from agent.risk.risk_manager import RiskManager
from agent.core.models import (
    MarketPrice, IndicatorValues, MarketStructure,
    GeminiTradeDecision, TradeAction, Position, OrderRequest,
    PositionDirection, AgentTelemetry, StockScreenerCandidate
)
from config.settings import settings, yaml_config

logger = logging.getLogger(__name__)


class CryptoTradingAgentOrchestrator:
    """
    Zentraler Orchestrator des autonomen 24/7 Alpaca Krypto-Trading-Agenten.
    Handelt die Top 20 Alpaca-Kryptowährungen mit der spezialisierten Strategie:
    'BTC-Regime-Adaptive Momentum & Volatility-Targeted Spot Rotation'
    (Ziel: Maximale Rendite bei mittlerem Risiko):
    - Live BTC/USD Macro-Regime Filter (RISK_ON / NEUTRAL / RISK_OFF)
    - Relative-Stärke-Rotation in Top-Outperformer gegen BTC
    - Tier-basierte Positions-Caps (Tier 1 <= 20%, Tier 2 <= 12%, Tier 3 <= 6% & max 2)
    - Max 65% Gesamt-Portfolio-Exposition (>= 35% USD-Cash-Puffer gegen Krypto-Crashs)
    - Synthetischer SL/TP + 1.2R Fee-aware Break-Even + 2.0R 50% Partial TP1 + Chandelier ATR Runner
    """

    DEFAULT_TIER_1 = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"]
    DEFAULT_TIER_2 = [
        "LINK/USD", "AVAX/USD", "DOT/USD", "UNI/USD", "AAVE/USD",
        "LTC/USD", "BCH/USD", "GRT/USD", "XTZ/USD", "CRV/USD", "MKR/USD"
    ]
    DEFAULT_TIER_3 = ["DOGE/USD", "SHIB/USD", "PEPE/USD", "SUSHI/USD", "BAT/USD"]

    TIER_MAX_ALLOC_PCT = {
        "TIER_1": 0.20,
        "TIER_2": 0.12,
        "TIER_3": 0.06,
    }

    def __init__(self):
        self.is_running: bool = False
        self.mode: str = settings.ALPACA_ENVIRONMENT.lower()  # "simulator", "paper", "live"
        crypto_cfg = yaml_config.get("crypto_trading", {})

        self.current_symbol: str = normalize_crypto_symbol(
            crypto_cfg.get("default_symbol", settings.DEFAULT_CRYPTO_SYMBOL)
        )
        self.tier_1_core: List[str] = list(crypto_cfg.get("tier_1_core", self.DEFAULT_TIER_1))
        self.tier_2_alt_defi: List[str] = list(crypto_cfg.get("tier_2_alt_defi", self.DEFAULT_TIER_2))
        self.tier_3_high_beta: List[str] = list(crypto_cfg.get("tier_3_high_beta", self.DEFAULT_TIER_3))
        self.symbols: List[str] = list(crypto_cfg.get(
            "symbols",
            self.tier_1_core + self.tier_2_alt_defi + self.tier_3_high_beta
        ))

        self.scan_interval: int = int(crypto_cfg.get("scan_interval_sec", settings.CRYPTO_SCAN_INTERVAL_SECONDS))
        self.max_portfolio_exposure_pct: float = float(
            crypto_cfg.get("max_portfolio_exposure_pct", settings.CRYPTO_MAX_PORTFOLIO_EXPOSURE_PCT)
        )
        self.partial_tp_trigger_r: float = float(crypto_cfg.get("partial_tp_trigger_r", 2.0))
        self.partial_tp_fraction: float = float(crypto_cfg.get("partial_tp_fraction", 0.50))

        self.screener_candidates: List[StockScreenerCandidate] = []
        self.btc_regime: str = "NEUTRAL"
        self._btc_return_pct: float = 0.0
        self._universe_scan_counter: int = 0

        # Subkomponenten
        self.broker: BaseBroker = self._create_broker()
        self.indicator_engine = CryptoTechnicalIndicatorEngine()
        self.structure_analyzer = MarketStructureAnalyzer()
        self.session_analyzer = CryptoSessionAnalyzer()

        gemini_cfg = yaml_config.get("gemini", {})
        self.risk_manager = RiskManager(
            risk_per_trade_pct=float(crypto_cfg.get("risk_per_trade_pct", settings.CRYPTO_RISK_PERCENT_PER_TRADE)),
            max_open_positions=int(crypto_cfg.get("max_open_positions", settings.CRYPTO_MAX_OPEN_POSITIONS)),
            max_daily_drawdown_pct=float(crypto_cfg.get("max_daily_drawdown_pct", settings.CRYPTO_MAX_DAILY_DRAWDOWN_PERCENT)),
            max_spread_pips=float(crypto_cfg.get("max_spread_pct", 0.35)),  # Spread in %
            min_risk_reward_ratio=float(crypto_cfg.get("min_risk_reward_ratio", 1.8)),
            confidence_threshold=float(gemini_cfg.get("confidence_threshold", 65.0)),
            breakeven_trigger_r=float(crypto_cfg.get("breakeven_trigger_r", 1.2)),
            default_atr_multiplier_sl=float(crypto_cfg.get("atr_multiplier_sl", 2.0)),
            default_atr_multiplier_tp=float(crypto_cfg.get("atr_multiplier_tp", 3.8)),
            trailing_stop_enabled=True,
            allow_ai_close_signals=True,
            auto_liquidate_on_drawdown=False,
            dynamic_risk_scaling=getattr(settings, "DYNAMIC_RISK_SCALING", True),
            max_correlated_exposure=0  # Krypto nutzt eigene BTC-Regime- & Tier-Caps
        )

        self.ai_analyst = GeminiCryptoAnalyst(
            api_key=settings.GEMINI_API_KEY,
            model_name=settings.GEMINI_MODEL,
            temperature=float(gemini_cfg.get("temperature", 0.2)),
            atr_sl_mult=self.risk_manager.default_atr_multiplier_sl,
            atr_tp_mult=self.risk_manager.default_atr_multiplier_tp
        )
        self.memory = AgentMemory(namespace="crypto")

        # Telemetrie & Cache
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
            logger.info(f"Initialisiere Alpaca Crypto Client ({self.mode})...")
            return AlpacaCryptoClient(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                environment=self.mode
            )
        else:
            logger.info("Initialisiere Crypto Simulator Broker (USD 24/7)...")
            return CryptoSimulatorBroker(initial_balance=50000.0, currency="USD")

    def get_coin_tier(self, symbol: str) -> str:
        sym = normalize_crypto_symbol(symbol)
        if sym in self.tier_1_core:
            return "TIER_1"
        if sym in self.tier_3_high_beta:
            return "TIER_3"
        return "TIER_2"

    def get_universe_catalog(self) -> Dict[str, Any]:
        return {
            "symbols": self.symbols,
            "tier_1_core": self.tier_1_core,
            "tier_2_alt_defi": self.tier_2_alt_defi,
            "tier_3_high_beta": self.tier_3_high_beta,
            "btc_regime": self.btc_regime,
            "strategy_mode": "btc_regime_momentum_vol_target",
            "total_symbols_count": len(set(self.symbols))
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
        logger.log(getattr(logging, level, logging.INFO), f"[CRYPTO_AGENT][{category}] {message}")

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
                logger.debug(f"Crypto Telemetry Broadcast Fehler: {e}")

    async def set_mode(self, mode: str):
        self.mode = mode.lower()
        self.broker = self._create_broker()
        await self.broker.initialize()
        self.log(f"Alpaca Crypto Modus gewechselt zu: {self.mode}", "INFO", "CONFIG")
        await self.broadcast_telemetry()

    async def set_symbol(self, symbol: str):
        self.current_symbol = normalize_crypto_symbol(symbol)
        self.log(f"Aktive Kryptowährung gewechselt zu: {self.current_symbol}", "INFO", "CONFIG")
        await self.run_scan_cycle(self.current_symbol)
        await self.broadcast_telemetry()

    async def initialize(self):
        await self.broker.initialize()
        await self.update_btc_macro_regime()
        await self.screen_crypto_universe(limit=10)
        self.log("Crypto 24/7 Agent Orchestrator (Top 20 Alpaca) erfolgreich initialisiert.", "INFO", "SYSTEM")

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.log("Autonomer 24/7 Krypto-Trading-Loop GESTARTET.", "INFO", "SYSTEM")
        self._loop_task = asyncio.create_task(self._orchestrator_loop())
        await self.broadcast_telemetry()

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        if self._loop_task:
            self._loop_task.cancel()
        self.log("Autonomer 24/7 Krypto-Trading-Loop PAUSIERT.", "WARNING", "SYSTEM")
        await self.broadcast_telemetry()

    async def update_btc_macro_regime(self) -> str:
        """
        Bestimmt das globale BTC/USD Macro-Regime (RISK_ON, NEUTRAL, RISK_OFF)
        und die 2h-BTC-Referenzrendite zur Relative-Stärke-Messung aller Top-20 Coins.
        """
        try:
            btc_candles = await self.broker.get_candles("BTC/USD", granularity="M5", count=100)
            if not btc_candles or len(btc_candles) < 20:
                return self.btc_regime

            ind = self.indicator_engine.calculate_indicators(btc_candles)
            lookback = min(len(btc_candles) - 1, 24)
            base_p = btc_candles[-1 - lookback].close
            curr_p = btc_candles[-1].close
            self._btc_return_pct = round(((curr_p - base_p) / max(base_p, 1e-9)) * 100.0, 2)

            rsi = ind.rsi_14 or 50.0
            if ind.trend_bias == "BULLISH" and ind.htf_trend_bias != "BEARISH" and rsi >= 48.0:
                self.btc_regime = "RISK_ON"
            elif ind.trend_bias == "BEARISH" and ind.htf_trend_bias == "BEARISH" and rsi <= 46.0:
                self.btc_regime = "RISK_OFF"
            else:
                self.btc_regime = "NEUTRAL"
        except Exception as e:
            logger.debug(f"BTC Macro Regime Update Fehler: {e}")

        return self.btc_regime

    async def screen_crypto_universe(self, limit: int = 10) -> List[StockScreenerCandidate]:
        """
        Paralleler 24/7 Top-20 Krypto-Screener mit BTC-Macro-Regime & Relative-Stärke-Ranking.
        Nutzt native Multi-Symbol Batch-Endpoints (2–3 HTTP-Calls für alle 20 Coins statt 40!)
        zur drastischen Reduktion der Alpaca API-Last.
        """
        session_name = self.session_analyzer.get_current_session()

        # Batch-Vorladung (falls Broker get_multi_prices / get_multi_candles unterstützt, z.B. AlpacaCryptoClient)
        batch_prices: Dict[str, MarketPrice] = {}
        batch_candles: Dict[str, List[Any]] = {}
        if hasattr(self.broker, "get_multi_prices") and hasattr(self.broker, "get_multi_candles"):
            batch_prices, batch_candles = await asyncio.gather(
                self.broker.get_multi_prices(self.symbols),
                self.broker.get_multi_candles(self.symbols, granularity="M5", count=60),
            )

        # BTC Macro Regime aus vorgeladenen BTC-Kerzen (0 Extra-Calls!)
        await self.update_btc_macro_regime()

        semaphore = asyncio.Semaphore(8)

        async def _eval_coin(sym: str) -> Optional[StockScreenerCandidate]:
            async with semaphore:
                try:
                    price = batch_prices.get(sym) or await self.broker.get_current_price(sym)
                    candles = batch_candles.get(sym) or await self.broker.get_candles(sym, granularity="M5", count=60)
                    if not candles or len(candles) < 20:
                        return None

                    ind = self.indicator_engine.calculate_indicators(
                        candles,
                        btc_return_pct=self._btc_return_pct,
                        btc_regime=self.btc_regime
                    )
                    if ind.atr_14:
                        self._symbol_atr_cache[sym] = ind.atr_14

                    tier = self.get_coin_tier(sym)
                    first_open = candles[0].open
                    change_pct = round(((price.mid - first_open) / max(first_open, 1e-12)) * 100.0, 2)

                    vwap_pos = "AT_VWAP"
                    if ind.vwap:
                        if price.mid > ind.vwap * 1.003:
                            vwap_pos = "ABOVE_VWAP"
                        elif price.mid < ind.vwap * 0.997:
                            vwap_pos = "BELOW_VWAP"

                    orb_stat = "INSIDE"
                    if ind.orb_high and price.mid > ind.orb_high:
                        orb_stat = "BREAKOUT_HIGH"
                    elif ind.orb_low and price.mid < ind.orb_low:
                        orb_stat = "BREAKDOWN_LOW"

                    score_info = self.ai_analyst.score_crypto_confluence(
                        symbol=sym,
                        price=price,
                        ind=ind,
                        tier=tier,
                        btc_regime=self.btc_regime,
                        session_name=session_name
                    )
                    score = float(score_info["long_score"])
                    min_buy = 75.0 if tier == "TIER_3" else 68.0
                    signal = "BUY" if score >= min_buy else "HOLD"

                    return StockScreenerCandidate(
                        symbol=sym,
                        index=tier,
                        price=price.mid,
                        change_pct=change_pct,
                        rvol=round(ind.rvol or 1.0, 2),
                        vwap_position=vwap_pos,
                        orb_status=orb_stat,
                        rsi=round(ind.rsi_14 or 50.0, 1),
                        score=round(min(99.0, max(5.0, score)), 1),
                        signal=signal
                    )
                except Exception:
                    return None

        results = await asyncio.gather(*[_eval_coin(s) for s in self.symbols])
        candidates = [c for c in results if c is not None]
        candidates.sort(key=lambda x: x.score, reverse=True)
        self.screener_candidates = candidates[:limit]
        return self.screener_candidates

    def calculate_crypto_quantity(
        self,
        symbol: str,
        equity: float,
        entry_price: float,
        stop_loss_price: float,
        risk_pct: float = 1.25,
        current_deployed_notional: float = 0.0
    ) -> float:
        """
        Volatility-Targeted Fractional Crypto Sizing mit Tier-Caps & 65% Gesamt-Expositionsgrenze:
        - Zielrisiko: risk_pct % der Account-Equity am Stop-Loss
        - Tier-Cap: Tier 1 <= 20%, Tier 2 <= 12%, Tier 3 <= 6% der Equity
        - Portfolio-Cap: Gesamte Krypto-Allokation <= 65% der Equity (mind. 35% USD Cash-Puffer)
        """
        if equity <= 0 or entry_price <= 0 or stop_loss_price <= 0 or entry_price <= stop_loss_price:
            return 0.0

        tier = self.get_coin_tier(symbol)
        clamped_risk = max(0.20, min(float(risk_pct), 2.5))
        # Reduziere Risk im NEUTRAL-Regime um 25%, im RISK_OFF um 50%
        regime_mult = 1.0 if self.btc_regime == "RISK_ON" else (0.75 if self.btc_regime == "NEUTRAL" else 0.50)

        risk_usd = equity * (clamped_risk / 100.0) * regime_mult
        risk_per_coin = abs(entry_price - stop_loss_price)
        raw_qty = risk_usd / max(risk_per_coin, 1e-12)

        # Tier Allocation Cap
        tier_cap_usd = equity * self.TIER_MAX_ALLOC_PCT.get(tier, 0.12)

        # Portfolio-Wide Exposure Cap (Medium Risk: >= 35% USD Dry Powder)
        max_total_usd = equity * (self.max_portfolio_exposure_pct / 100.0)
        remaining_portfolio_usd = max(0.0, max_total_usd - current_deployed_notional)

        max_allowed_usd = min(tier_cap_usd, remaining_portfolio_usd)
        if max_allowed_usd < 10.0:
            return 0.0

        final_notional = min(raw_qty * entry_price, max_allowed_usd)
        qty = final_notional / entry_price
        return round(qty, 8 if entry_price >= 1.0 else 4)

    async def _trade_top_crypto_candidates(self):
        """Handelt autonom die stärksten Relative-Stärke-Setups aus dem Top-20 Screener."""
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
            if cand.signal != "BUY":
                continue
            if self.memory.get_instrument_loss_cooldown_remaining(cand.symbol, settings.LOSS_COOLDOWN_SECONDS) > 0:
                continue

            await self.run_scan_cycle(cand.symbol, update_primary_telemetry=False)
            evaluated += 1

    async def _orchestrator_loop(self):
        while self.is_running:
            try:
                await self.update_btc_macro_regime()
                await self.run_scan_cycle(self.current_symbol, update_primary_telemetry=True)

                self._universe_scan_counter += 1
                if self._universe_scan_counter % 4 == 0:
                    await self.screen_crypto_universe(limit=10)
                    if settings.MULTI_INSTRUMENT_SCAN_ENABLED:
                        await self._trade_top_crypto_candidates()

                await self._manage_open_positions()
                await self.broadcast_telemetry()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"Fehler im Crypto-Orchestrator-Loop: {e}", "ERROR", "SYSTEM")

            await asyncio.sleep(self.scan_interval)

    async def run_scan_cycle(self, symbol: str, update_primary_telemetry: bool = True):
        """Kompletter 24/7 Analyse- & Ausführungszyklus für ein Krypto-Spot-Paar."""
        try:
            sym = normalize_crypto_symbol(symbol)
            price = await self.broker.get_current_price(sym)
            candles = await self.broker.get_candles(sym, granularity="M5", count=100)

            indicators = self.indicator_engine.calculate_indicators(
                candles,
                btc_return_pct=self._btc_return_pct,
                btc_regime=self.btc_regime
            )
            if indicators.atr_14:
                self._symbol_atr_cache[sym] = indicators.atr_14

            session_name = self.session_analyzer.get_current_session()
            structure = self.structure_analyzer.analyze(candles, current_session=session_name)

            if indicators.orb_high and price.mid > indicators.orb_high:
                structure.orb_bias = "ABOVE_ORB_HIGH"
            elif indicators.orb_low and price.mid < indicators.orb_low:
                structure.orb_bias = "BELOW_ORB_LOW"
            else:
                structure.orb_bias = "INSIDE_ORB_RANGE"

            if update_primary_telemetry or sym == self.current_symbol:
                self.last_price = price
                self.last_indicators = indicators
                self.last_structure = structure

            account = await self.broker.get_account_summary()
            open_positions = await self.broker.get_open_positions()
            active_for_symbol = len([p for p in open_positions if p.instrument == sym])
            deployed_notional = sum(p.current_price * p.units for p in open_positions)
            exposure_pct = (deployed_notional / max(account.equity, 1.0)) * 100.0
            tier = self.get_coin_tier(sym)

            # Token-Effizienz für 24/7 Betrieb: LLM nur bei echter Konfluenz oder aktiver Position abfragen
            pre = self.ai_analyst.score_crypto_confluence(sym, price, indicators, tier, self.btc_regime, session_name)
            should_call_llm = active_for_symbol > 0 or pre["long_score"] >= 58

            recent_context = self.memory.get_recent_history_context(limit=5)
            decision = await self.ai_analyst.analyze_market(
                symbol=sym,
                price=price,
                indicators=indicators,
                structure=structure,
                session_name=session_name,
                open_positions_count=active_for_symbol,
                daily_drawdown_pct=account.daily_drawdown_pct,
                tier=tier,
                btc_regime=self.btc_regime,
                portfolio_exposure_pct=exposure_pct,
                recent_history_context=recent_context,
                use_llm=should_call_llm,
                atr_sl_mult=self.risk_manager.default_atr_multiplier_sl,
                atr_tp_mult=self.risk_manager.default_atr_multiplier_tp
            )

            if update_primary_telemetry or sym == self.current_symbol:
                self.last_decision = decision

            if decision.action == TradeAction.BUY:
                # 1. Post-SL Loss Cooldown
                cooldown_rem = self.memory.get_instrument_loss_cooldown_remaining(sym, settings.LOSS_COOLDOWN_SECONDS)
                if cooldown_rem > 0:
                    self.log(f"Loss-Cooldown aktiv für {sym} ({cooldown_rem:.0f}s verbleibend).", "INFO", "RISK")
                    return

                # 2. Tier-3 Max Concurrent Guard (Medium Risk: max 2 spekulative Meme/High-Beta Positionen)
                if tier == "TIER_3":
                    tier3_open = [p for p in open_positions if self.get_coin_tier(p.instrument) == "TIER_3"]
                    if len(tier3_open) >= 2:
                        self.log(f"Tier-3 Risikolimit erreicht (max 2 High-Beta Coins). {sym} übersprungen.", "WARNING", "RISK")
                        return

                # 3. Portfolio Exposure Cap (<= 65% deployed, >= 35% USD Cash)
                if exposure_pct >= self.max_portfolio_exposure_pct:
                    self.log(f"Portfolio-Expositionslimit ({exposure_pct:.1f}% >= {self.max_portfolio_exposure_pct}%) erreicht.", "WARNING", "RISK")
                    return

                # 4. Risikomanager-Prüfung
                is_valid, reason = self.risk_manager.validate_trade_decision(
                    decision=decision,
                    account=account,
                    spread_pips=price.spread_pips,
                    open_positions=open_positions
                )

                if is_valid:
                    dyn_risk = self.risk_manager.compute_dynamic_risk_pct(
                        base_risk_pct=decision.suggested_risk_pct,
                        confidence=decision.confidence,
                        daily_drawdown_pct=account.daily_drawdown_pct,
                        consecutive_losses=self.memory.get_consecutive_losses()
                    )
                    entry = decision.entry_price or price.ask
                    sl = decision.stop_loss or (entry * 0.96)
                    qty = self.calculate_crypto_quantity(
                        symbol=sym,
                        equity=account.equity,
                        entry_price=entry,
                        stop_loss_price=sl,
                        risk_pct=dyn_risk,
                        current_deployed_notional=deployed_notional
                    )
                    if qty <= 0:
                        self.log(f"Keine verfügbare Allokation für {sym} unter Risiko-/Tier-Caps.", "WARNING", "RISK")
                        return

                    order = OrderRequest(
                        instrument=sym,
                        direction=PositionDirection.BUY,
                        units=qty,
                        order_type="MARKET",
                        price=entry,
                        stop_loss=sl,
                        take_profit=decision.take_profit_1
                    )
                    pos = await self.broker.place_order(order)
                    self.log(
                        f"Crypto Spot-Kauf: {pos.units} {sym} @ ${pos.entry_price} (SL: ${pos.stop_loss}, TP: ${pos.take_profit}, Tier: {tier}, BTC: {self.btc_regime})",
                        "INFO",
                        "EXECUTION"
                    )
                else:
                    self.log(f"Crypto Trade abgelehnt durch Risikomanager: {reason}", "WARNING", "RISK")

            elif decision.action == TradeAction.CLOSE and self.risk_manager.allow_ai_close_signals:
                for p in open_positions:
                    if p.instrument == sym:
                        closed = await self.broker.close_position(p.id, reason="AGENT_SIGNAL")
                        self.memory.record_closed_trade(closed, thesis=decision.thesis_summary)
                        self.log(f"Crypto Position {p.id} ({sym}) geschlossen. PnL=${closed.realized_pnl:,.2f} USD", "INFO", "EXECUTION")

        except Exception as e:
            self.log(f"Fehler im Crypto Scan-Zyklus für {symbol}: {e}", "ERROR", "ANALYSIS")

    async def _manage_open_positions(self):
        """
        Aktives 24/7 Positionsmanagement für Alpaca Crypto Spot:
        - Auto-Liquidation bei Drawdown-Limit
        - Synthetische Stop-Loss & Take-Profit Auslösung (da Alpaca Crypto keine Bracket-Orders hat)
        - Stage 1 (1.2R): Fee-kompensierter Break-Even (+0.15%)
        - Stage 2 (2.0R): 50% Partial Profit-Taking (TP1) + +1.0R Stop-Lock
        - Stage 3 (2.5R+): Chandelier ATR Trailing Stop (highest_price - 1.2 * ATR)
        """
        try:
            open_positions = await self.broker.get_open_positions()
            if not open_positions:
                return

            if self.risk_manager.auto_liquidate_on_drawdown:
                account = await self.broker.get_account_summary()
                if account.daily_drawdown_pct >= self.risk_manager.max_daily_drawdown_pct:
                    self.log(f"CRYPTO AUTO-LIQUIDATION ausgelöst: Drawdown {account.daily_drawdown_pct}%.", "WARNING", "EMERGENCY")
                    for pos in list(open_positions):
                        closed = await self.broker.close_position(pos.id, reason="CIRCUIT_BREAKER")
                        self.memory.record_closed_trade(closed, thesis="Crypto Drawdown Circuit Breaker.")
                    return

            for pos in list(open_positions):
                price_info = await self.broker.get_current_price(pos.instrument)
                curr_price = price_info.bid
                pos.highest_price = max(pos.highest_price or pos.entry_price, curr_price)
                prec = _crypto_precision(pos.entry_price)

                # 1. Synthetische SL / TP Auslösung
                if pos.stop_loss and curr_price <= pos.stop_loss:
                    closed = await self.broker.close_position(pos.id, reason="SL_HIT")
                    self.memory.record_closed_trade(closed, thesis="Synthetischer Krypto Stop-Loss ausgelöst.")
                    self.log(f"Crypto SL ausgelöst für {pos.instrument}: PnL=${closed.realized_pnl:,.2f}", "WARNING", "RISK")
                    continue

                if pos.take_profit and curr_price >= pos.take_profit:
                    closed = await self.broker.close_position(pos.id, reason="TP_HIT")
                    self.memory.record_closed_trade(closed, thesis="Krypto Take-Profit Ziel erreicht.")
                    self.log(f"Crypto TP erreicht für {pos.instrument}: PnL=+${closed.realized_pnl:,.2f}", "INFO", "EXECUTION")
                    continue

                # 2. Multi-Stage Trailing & Partial Profit-Taking
                if not self.risk_manager.trailing_stop_enabled or not pos.stop_loss:
                    continue

                ref_sl = pos.initial_stop_loss if pos.initial_stop_loss is not None else pos.stop_loss
                initial_risk = pos.entry_price - ref_sl
                if initial_risk <= 0:
                    continue

                profit = curr_price - pos.entry_price
                r_mult = profit / initial_risk
                atr = self._symbol_atr_cache.get(pos.instrument) or (initial_risk / 2.0)

                # Stage 2: Partial TP1 (50% Gewinne sichern bei 2.0R)
                if r_mult >= self.partial_tp_trigger_r and not pos.partial_tp_taken and hasattr(self.broker, "reduce_position"):
                    partial = await self.broker.reduce_position(pos.id, fraction=self.partial_tp_fraction, reason="PARTIAL_TP1")
                    if partial:
                        self.memory.record_closed_trade(partial, thesis=f"50% Partial Profit-Taking bei {r_mult:.1f}R")
                        self.log(f"Partial TP1 (50%) für {pos.instrument} realisiert: +${partial.realized_pnl:,.2f}", "INFO", "EXECUTION")

                # Ziel-Stop-Loss berechnen (nie rückwärts bewegen!)
                new_sl = None
                if r_mult >= 2.5:
                    chandelier = pos.highest_price - (1.2 * atr)
                    lock_sl = pos.entry_price + (1.0 * initial_risk)
                    new_sl = round(max(lock_sl, min(chandelier, curr_price * 0.995)), prec)
                elif r_mult >= self.partial_tp_trigger_r:
                    new_sl = round(pos.entry_price + (1.0 * initial_risk), prec)
                elif r_mult >= self.risk_manager.breakeven_trigger_r:
                    # Fee-kompensierter Break-Even (+0.15% deckt Alpaca Spot-Gebühr)
                    new_sl = round(pos.entry_price * 1.0015, prec)

                if new_sl and new_sl > pos.stop_loss * 1.0005:
                    await self.broker.update_stop_loss(pos.id, new_sl)
                    self.log(f"Crypto Trailing-SL für {pos.instrument} nachgezogen auf ${new_sl} ({r_mult:.1f}R)", "INFO", "RISK")

        except Exception as e:
            logger.debug(f"Fehler bei Crypto Positionsüberwachung: {e}")

    async def close_single_position(self, position_id: str, reason: str = "MANUAL") -> Position:
        closed = await self.broker.close_position(position_id, reason=reason)
        self.memory.record_closed_trade(closed, thesis="Krypto-Position manuell geschlossen.")
        self.log(f"Crypto Position {closed.id} ({closed.instrument}) geschlossen. PnL=${closed.realized_pnl:,.2f} USD", "INFO", "MANUAL")
        await self.broadcast_telemetry()
        return closed

    async def emergency_close_all(self) -> int:
        open_positions = await self.broker.get_open_positions()
        count = 0
        for p in list(open_positions):
            try:
                closed = await self.broker.close_position(p.id, reason="EMERGENCY_CLOSE")
                self.memory.record_closed_trade(closed, thesis="Krypto Notstopp ausgelöst.")
                count += 1
            except Exception as e:
                self.log(f"Fehler beim Schließen von Krypto-Position {p.id}: {e}", "ERROR", "EMERGENCY")
        self.log(f"CRYPTO NOTFALL-STOPP: {count} Position(en) geschlossen.", "WARNING", "EMERGENCY")
        await self.broadcast_telemetry()
        return count

    async def execute_manual_trade(self, symbol: str, direction: str, risk_pct: float = 1.25) -> Position:
        """Manuelle Krypto-Spot-Order (BUY kauft Spot; SELL schließt offene Position für dieses Symbol)."""
        sym = normalize_crypto_symbol(symbol)
        open_positions = await self.broker.get_open_positions()

        if direction.upper() == "SELL":
            existing = next((p for p in open_positions if p.instrument == sym), None)
            if existing:
                return await self.close_single_position(existing.id, reason="MANUAL_SELL")
            raise ValueError(f"Alpaca Crypto ist Spot-Long-Only: Keine offene {sym}-Position zum Verkaufen vorhanden.")

        price = await self.broker.get_current_price(sym)
        account = await self.broker.get_account_summary()
        prec = _crypto_precision(price.ask)
        entry = price.ask
        atr = self._symbol_atr_cache.get(sym) or (entry * 0.015)

        sl = round(max(entry * 0.85, entry - self.risk_manager.default_atr_multiplier_sl * atr), prec)
        tp = round(entry + self.risk_manager.default_atr_multiplier_tp * atr, prec)
        deployed = sum(p.current_price * p.units for p in open_positions)

        qty = self.calculate_crypto_quantity(
            symbol=sym,
            equity=account.equity,
            entry_price=entry,
            stop_loss_price=sl,
            risk_pct=risk_pct,
            current_deployed_notional=deployed
        )
        if qty <= 0:
            raise ValueError("Maximale Krypto-Portfolio-Exposition erreicht oder unzureichendes Cash.")

        order = OrderRequest(
            instrument=sym,
            direction=PositionDirection.BUY,
            units=qty,
            order_type="MARKET",
            price=entry,
            stop_loss=sl,
            take_profit=tp
        )
        pos = await self.broker.place_order(order)
        self.log(f"Manuelle Crypto-Order: BUY {pos.units} {sym} @ ${pos.entry_price}", "INFO", "MANUAL")
        await self.broadcast_telemetry()
        return pos

    async def get_telemetry(self) -> AgentTelemetry:
        account = await self.broker.get_account_summary()
        open_positions = await self.broker.get_open_positions()

        try:
            broker_closed = await self.broker.get_closed_positions()
            for cp in broker_closed:
                if not any(t.get("id") == cp.id for t in self.memory.trade_logs):
                    self.memory.record_closed_trade(cp, thesis="Crypto Order durch SL / TP / Scale-Out geschlossen")
        except Exception as e:
            logger.debug(f"Fehler bei Crypto Closed-Trade-Sync: {e}")

        stats = self.memory.get_performance_stats(reference_capital=account.daily_start_equity or 50000.0)
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
