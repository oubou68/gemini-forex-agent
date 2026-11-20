import math
from typing import List, Optional
import pandas as pd
import numpy as np

from agent.core.models import Candle, IndicatorValues
from agent.analysis.indicators import _compute_wilder_adx, _compute_htf_trend_bias


def _crypto_precision(price: float) -> int:
    """Adaptive Nachkommastellen-Präzision (von BTC $64,500 bis SHIB/PEPE $0.000008)."""
    if price >= 100.0:
        return 2
    elif price >= 1.0:
        return 4
    elif price >= 0.01:
        return 5
    else:
        return 8


class CryptoTechnicalIndicatorEngine:
    """
    Spezialisierte 24/7 Krypto-Indikatoren-Engine für Alpaca Spot Trading.
    Berechnet:
    - Adaptive Präzision (unterstützt BTC bis Mikro-Preis-Coins wie SHIB/PEPE)
    - Rolling VWAP & Standardabweichungsbänder
    - Wilder's RSI(14), ATR(14) & ADX(14)
    - M15 Higher-Timeframe Trend-Alignment
    - Bollinger Bandwidth % (Volatility Squeeze & Expansion Erkennung)
    - Relative Volume (RVOL) & Relative Stärke vs. BTC/USD
    """

    @staticmethod
    def calculate_indicators(
        candles: List[Candle],
        btc_return_pct: Optional[float] = None,
        btc_regime: Optional[str] = None
    ) -> IndicatorValues:
        if not candles or len(candles) < 15:
            return IndicatorValues(btc_regime=btc_regime)

        df = pd.DataFrame([{
            "time": c.time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": max(1.0, float(c.volume))
        } for c in candles])

        closes = df["close"]
        highs = df["high"]
        lows = df["low"]
        volumes = df["volume"]
        curr_price = float(closes.iloc[-1])
        prec = _crypto_precision(curr_price)

        # 1. EMAs (9, 21, 50, 200)
        ema_9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
        ema_21 = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
        ema_50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1]) if len(df) >= 50 else None
        ema_200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1]) if len(df) >= 200 else None

        # 2. Rolling 24/7 VWAP (letzte 60 M5-Kerzen = 5h institutioneller Anker)
        vwap_df = df.iloc[-min(len(df), 60):]
        tp = (vwap_df["high"] + vwap_df["low"] + vwap_df["close"]) / 3.0
        cum_v = vwap_df["volume"].cumsum()
        vwap_series = (tp * vwap_df["volume"]).cumsum() / (cum_v + 1e-12)
        vwap_val = float(vwap_series.iloc[-1])
        vwap_var = (((tp - vwap_series) ** 2) * vwap_df["volume"]).cumsum() / (cum_v + 1e-12)
        vwap_std = float(np.sqrt(np.maximum(0.0, vwap_var.iloc[-1])))
        vwap_upper = vwap_val + 1.5 * vwap_std
        vwap_lower = max(0.0, vwap_val - 1.5 * vwap_std)

        # 3. Breakout-Range (4h High/Low als Krypto-Range-Anker)
        range_df = df.iloc[-min(len(df), 48):-1] if len(df) > 5 else df
        orb_high = float(range_df["high"].max())
        orb_low = float(range_df["low"].min())

        # 4. RVOL (Relative Volume vs 20-Bar Mean)
        avg_vol_20 = float(volumes.rolling(window=20, min_periods=5).mean().iloc[-1])
        curr_vol = float(volumes.iloc[-1])
        rvol = round(curr_vol / (avg_vol_20 + 1e-9), 2) if avg_vol_20 > 0 else 1.0

        # 5. Wilder's RSI (14)
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1.0 / 14.0, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0 / 14.0, adjust=False).mean()
        rs = gain / (loss + 1e-12)
        rsi_series = 100.0 - (100.0 / (1.0 + rs))
        rsi_14 = float(rsi_series.iloc[-1]) if not math.isnan(rsi_series.iloc[-1]) else 50.0

        # 6. MACD (12, 26, 9)
        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal

        # 7. Wilder's ATR (14) & ADX (14)
        tr1 = highs - lows
        tr2 = (highs - closes.shift()).abs()
        tr3 = (lows - closes.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_series = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        atr_14 = float(atr_series.iloc[-1]) if not math.isnan(atr_series.iloc[-1]) else float(tr.mean())
        adx_14 = _compute_wilder_adx(highs, lows, closes, period=14)

        # 8. Bollinger Bands (20, 2) & Bandwidth % (Squeeze/Expansion Detector)
        bb_mid_s = closes.rolling(window=20, min_periods=5).mean()
        bb_std_s = closes.rolling(window=20, min_periods=5).std()
        bb_middle = float(bb_mid_s.iloc[-1]) if not math.isnan(bb_mid_s.iloc[-1]) else curr_price
        bb_std = float(bb_std_s.iloc[-1]) if not math.isnan(bb_std_s.iloc[-1]) else atr_14
        bb_upper = bb_middle + 2.0 * bb_std
        bb_lower = max(0.0, bb_middle - 2.0 * bb_std)
        bb_width_pct = ((bb_upper - bb_lower) / max(bb_middle, 1e-12)) * 100.0

        # 9. Stochastic RSI
        min_rsi = rsi_series.rolling(window=14, min_periods=5).min()
        max_rsi = rsi_series.rolling(window=14, min_periods=5).max()
        stoch_rsi = (rsi_series - min_rsi) / (max_rsi - min_rsi + 1e-9) * 100.0
        stoch_k = float(stoch_rsi.rolling(window=3, min_periods=1).mean().iloc[-1])
        stoch_d = float(stoch_rsi.rolling(window=3, min_periods=1).mean().rolling(window=3, min_periods=1).mean().iloc[-1])
        if math.isnan(stoch_k):
            stoch_k = 50.0
        if math.isnan(stoch_d):
            stoch_d = 50.0

        # 10. Trend Bias, M15 HTF Bias & Relative Strength vs BTC
        trend_bias = "NEUTRAL"
        if curr_price > vwap_val and ema_9 > ema_21:
            trend_bias = "BULLISH"
        elif curr_price < vwap_val and ema_9 < ema_21:
            trend_bias = "BEARISH"

        htf_trend_bias = _compute_htf_trend_bias(closes, group_size=3)

        # Coin return over lookback window vs BTC return
        lookback = min(len(closes) - 1, 24)
        base_close = float(closes.iloc[-1 - lookback])
        coin_ret_pct = ((curr_price - base_close) / max(base_close, 1e-12)) * 100.0
        rs_vs_btc = round(coin_ret_pct - btc_return_pct, 2) if btc_return_pct is not None else 0.0

        # 11. Volatility Regime
        avg_atr = tr.rolling(window=50, min_periods=10).mean().iloc[-1] if len(df) >= 20 else atr_14
        vol_regime = "NORMAL"
        if not math.isnan(avg_atr) and avg_atr > 0:
            ratio = atr_14 / avg_atr
            if ratio > 1.45:
                vol_regime = "EXPANDING"
            elif ratio > 1.15:
                vol_regime = "HIGH"
            elif ratio < 0.72:
                vol_regime = "LOW"

        return IndicatorValues(
            ema_9=round(ema_9, prec),
            ema_21=round(ema_21, prec),
            ema_50=round(ema_50, prec) if ema_50 is not None else None,
            ema_200=round(ema_200, prec) if ema_200 is not None else None,
            rsi_14=round(rsi_14, 2),
            macd=round(float(macd_line.iloc[-1]), min(prec + 2, 8)),
            macd_signal=round(float(macd_signal.iloc[-1]), min(prec + 2, 8)),
            macd_hist=round(float(macd_hist.iloc[-1]), min(prec + 2, 8)),
            atr_14=round(atr_14, prec),
            bb_upper=round(bb_upper, prec),
            bb_middle=round(bb_middle, prec),
            bb_lower=round(bb_lower, prec),
            stoch_k=round(stoch_k, 2),
            stoch_d=round(stoch_d, 2),
            vwap=round(vwap_val, prec),
            vwap_upper=round(vwap_upper, prec),
            vwap_lower=round(vwap_lower, prec),
            rvol=round(rvol, 2),
            orb_high=round(orb_high, prec),
            orb_low=round(orb_low, prec),
            adx_14=round(adx_14, 2),
            htf_trend_bias=htf_trend_bias,
            btc_regime=btc_regime,
            rs_vs_btc=rs_vs_btc,
            bb_width_pct=round(bb_width_pct, 2),
            trend_bias=trend_bias,
            volatility_regime=vol_regime
        )
