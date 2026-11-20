from datetime import datetime, timezone, timedelta
from typing import Optional


class StockSessionAnalyzer:
    """
    Analysiert die aktuellen Handelsphasen des US-Aktienmarkts (NYSE / NASDAQ)
    in echter US Eastern Time (ET, automatisch DST-korrigiert für EDT/EST).
    """

    @staticmethod
    def _to_us_eastern(dt: datetime) -> datetime:
        """
        Konvertiert UTC nach US Eastern Time (America/New_York) mit exakter
        US-Sommerzeit-Regel (2. Sonntag im März 07:00 UTC bis 1. Sonntag im November 06:00 UTC).
        Funktioniert plattformunabhängig ohne externe tzdata-Pakete.
        """
        if dt.tzinfo is None:
            dt_utc = dt.replace(tzinfo=timezone.utc)
        else:
            dt_utc = dt.astimezone(timezone.utc)

        year = dt_utc.year
        # 2. Sonntag im März (02:00 EST = 07:00 UTC)
        march_1 = datetime(year, 3, 1, tzinfo=timezone.utc)
        days_to_first_sun_mar = (6 - march_1.weekday()) % 7
        dst_start = march_1 + timedelta(days=days_to_first_sun_mar + 7, hours=7)

        # 1. Sonntag im November (02:00 EDT = 06:00 UTC)
        nov_1 = datetime(year, 11, 1, tzinfo=timezone.utc)
        days_to_first_sun_nov = (6 - nov_1.weekday()) % 7
        dst_end = nov_1 + timedelta(days=days_to_first_sun_nov, hours=6)

        is_edt = dst_start <= dt_utc < dst_end
        offset = timezone(timedelta(hours=-4 if is_edt else -5))
        return dt_utc.astimezone(offset)

    @staticmethod
    def get_current_session(dt: Optional[datetime] = None) -> str:
        """
        Gibt die aktuelle US-Handelssitzung zurück:
        - US_OPENING_DRIVE (09:30 - 10:30 ET): Höchste Volatilität & ORB-Formierung
        - US_MID_DAY_RTH (10:30 - 15:00 ET): Trendfortsetzung / VWAP-Mean-Reversion
        - US_POWER_HOUR (15:00 - 16:00 ET): Institutionelles Closing & Momentum
        - US_PRE_MARKET (04:00 - 09:30 ET): Vorbörslicher Handel & News-Gaps
        - US_AFTER_HOURS (16:00 - 20:00 ET): Nachbörslicher Handel (Earnings)
        - US_MARKET_CLOSED: Wochenende oder Nachts
        """
        if dt is None:
            dt = datetime.now(timezone.utc)

        et = StockSessionAnalyzer._to_us_eastern(dt)

        # Wochentag prüfen in US Eastern Time: Montag = 0, Sonntag = 6
        if et.weekday() >= 5:
            return "US_MARKET_CLOSED (WEEKEND)"

        minute_of_day_et = et.hour * 60 + et.minute

        # 04:00 ET bis 09:30 ET
        if 240 <= minute_of_day_et < 570:
            return "US_PRE_MARKET"
        # 09:30 ET bis 10:30 ET
        elif 570 <= minute_of_day_et < 630:
            return "US_OPENING_DRIVE (RTH OPEN)"
        # 10:30 ET bis 15:00 ET
        elif 630 <= minute_of_day_et < 900:
            return "US_MID_DAY_RTH"
        # 15:00 ET bis 16:00 ET
        elif 900 <= minute_of_day_et < 960:
            return "US_POWER_HOUR"
        # 16:00 ET bis 20:00 ET
        elif 960 <= minute_of_day_et < 1200:
            return "US_AFTER_HOURS"
        else:
            return "US_MARKET_CLOSED (OVERNIGHT)"

    @staticmethod
    def is_regular_trading_hours(dt: Optional[datetime] = None) -> bool:
        """Gibt True zurück, wenn die regulären US-Handelszeiten (09:30 - 16:00 ET) aktiv sind."""
        sess = StockSessionAnalyzer.get_current_session(dt)
        return any(s in sess for s in ["OPENING_DRIVE", "MID_DAY", "POWER_HOUR"])

    @staticmethod
    def is_market_open(dt: Optional[datetime] = None, regular_hours_only: bool = False) -> bool:
        """
        Prüft, ob der US-Aktienmarkt geöffnet ist.
        - regular_hours_only = True: Nur reguläre Haupthandelszeiten (09:30 - 16:00 ET)
        - regular_hours_only = False: Inklusive Pre-Market & After-Hours (04:00 - 20:00 ET)
        """
        if dt is None:
            dt = datetime.now(timezone.utc)

        et = StockSessionAnalyzer._to_us_eastern(dt)
        if et.weekday() >= 5:
            return False

        if regular_hours_only:
            return StockSessionAnalyzer.is_regular_trading_hours(dt)

        sess = StockSessionAnalyzer.get_current_session(dt)
        return "CLOSED" not in sess
