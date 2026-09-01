from datetime import datetime, timezone
from typing import Optional


class StockSessionAnalyzer:
    """
    Analysiert die aktuellen Handelsphasen des US-Aktienmarkts (NYSE / NASDAQ)
    in US Eastern Time (ET) und UTC.
    """

    @staticmethod
    def get_current_session(dt: Optional[datetime] = None) -> str:
        """
        Gibt die aktuelle US-Handelssitzung zurück:
        - US_OPENING_DRIVE (09:30 - 10:30 EST): Höchste Volatilität & ORB-Formierung
        - US_MID_DAY_RTH (10:30 - 15:00 EST): Trendfortsetzung / VWAP-Mean-Reversion
        - US_POWER_HOUR (15:00 - 16:00 EST): Institutionelles Closing & Momentum
        - US_PRE_MARKET (04:00 - 09:30 EST): Vorbörslicher Handel & News-Gaps
        - US_AFTER_HOURS (16:00 - 20:00 EST): Nachbörslicher Handel (Earnings)
        - US_MARKET_CLOSED: Wochenende oder Nachts
        """
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # Wochentag prüfen: Montag = 0, Sonntag = 6
        weekday = dt.weekday()
        if weekday >= 5:  # Samstag oder Sonntag
            return "US_MARKET_CLOSED (WEEKEND)"

        # US Eastern Time Berechnung (ca. UTC - 4 / - 5)
        # UTC 13:30 = 09:30 EST (Sommerzeit / EDT)
        minute_of_day_utc = dt.hour * 60 + dt.minute

        # 08:00 UTC (04:00 EST) bis 13:30 UTC (09:30 EST)
        if 480 <= minute_of_day_utc < 810:
            return "US_PRE_MARKET"
        # 13:30 UTC (09:30 EST) bis 14:30 UTC (10:30 EST)
        elif 810 <= minute_of_day_utc < 870:
            return "US_OPENING_DRIVE (RTH OPEN)"
        # 14:30 UTC (10:30 EST) bis 19:00 UTC (15:00 EST)
        elif 870 <= minute_of_day_utc < 1140:
            return "US_MID_DAY_RTH"
        # 19:00 UTC (15:00 EST) bis 20:00 UTC (16:00 EST)
        elif 1140 <= minute_of_day_utc < 1200:
            return "US_POWER_HOUR"
        # 20:00 UTC (16:00 EST) bis 00:00 UTC (20:00 EST)
        elif 1200 <= minute_of_day_utc < 1440:
            return "US_AFTER_HOURS"
        else:
            return "US_MARKET_CLOSED (OVERNIGHT)"

    @staticmethod
    def is_regular_trading_hours(dt: Optional[datetime] = None) -> bool:
        """Gibt True zurück, wenn die regulären US-Handelszeiten (09:30 - 16:00 EST) aktiv sind."""
        sess = StockSessionAnalyzer.get_current_session(dt)
        return any(s in sess for s in ["OPENING_DRIVE", "MID_DAY", "POWER_HOUR"])

    @staticmethod
    def is_market_open(dt: Optional[datetime] = None, regular_hours_only: bool = False) -> bool:
        """
        Prüft, ob der US-Aktienmarkt geöffnet ist.
        - regular_hours_only = True: Nur reguläre Haupthandelszeiten (09:30 - 16:00 EST)
        - regular_hours_only = False: Inklusive Pre-Market & After-Hours (04:00 - 20:00 EST)
        """
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        weekday = dt.weekday()
        if weekday >= 5:  # Samstag oder Sonntag
            return False

        if regular_hours_only:
            return StockSessionAnalyzer.is_regular_trading_hours(dt)

        sess = StockSessionAnalyzer.get_current_session(dt)
        return not ("CLOSED" in sess)
