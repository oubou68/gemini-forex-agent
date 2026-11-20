from datetime import datetime, timezone
from typing import Optional


class CryptoSessionAnalyzer:
    """
    24/7 Liquiditäts- & Sitzungsanalyse für den globalen Kryptomarkt (Alpaca Spot).
    Krypto handelt rund um die Uhr, weist aber klare Liquiditätsregime auf:
    - CRYPTO_US_OVERLAP (13:00 - 21:00 UTC): Höchste ETF-/Institutionelle Liquidität
    - CRYPTO_LONDON_OPEN (07:00 - 13:00 UTC): Europäische Preisfindung & Breakouts
    - CRYPTO_ASIA_SESSION (00:00 - 07:00 UTC): Asiatisches Momentum & Funding-Resets
    - CRYPTO_LATE_US (21:00 - 00:00 UTC): Übergangsphase / Daily Close (00:00 UTC)
    - CRYPTO_WEEKEND_LIQUIDITY: Geringere Orderbuch-Tiefe -> strengere Konfluenz-Filter
    """

    @staticmethod
    def is_market_open(dt: Optional[datetime] = None) -> bool:
        """Kryptomärkte sind 24/7/365 geöffnet."""
        return True

    @staticmethod
    def is_weekend(dt: Optional[datetime] = None) -> bool:
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.weekday() >= 5

    @staticmethod
    def get_current_session(dt: Optional[datetime] = None) -> str:
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if dt.weekday() >= 5:
            return "CRYPTO_WEEKEND_24_7"

        hour = dt.hour
        if 13 <= hour < 21:
            return "CRYPTO_US_OVERLAP"
        elif 7 <= hour < 13:
            return "CRYPTO_LONDON_SESSION"
        elif 0 <= hour < 7:
            return "CRYPTO_ASIA_SESSION"
        else:
            return "CRYPTO_DAILY_CLOSE_TRANSITION"
