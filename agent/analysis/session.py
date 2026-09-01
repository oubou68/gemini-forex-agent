from datetime import datetime, timezone
from typing import Optional


class SessionAnalyzer:
    """
    Ermittelt die aktuell aktive Forex-Handelssitzung basierend auf UTC-Zeit
    und prüft, ob der Interbanken-Devisenmarkt geöffnet ist.
    """

    @staticmethod
    def is_market_open(dt: Optional[datetime] = None) -> bool:
        """
        Prüft, ob der globale Forex-Markt geöffnet ist.
        Der Forex-Markt öffnet Sonntag um 21:00 UTC (Sydney/Tokyo Open)
        und schließt Freitag um 22:00 UTC (New York Close).
        Samstag ganztägig sowie Sonntag vor 21:00 UTC ist der Markt geschlossen.
        """
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        weekday = dt.weekday()  # 0 = Montag, 4 = Freitag, 5 = Samstag, 6 = Sonntag
        hour = dt.hour

        if weekday == 5:  # Samstag
            return False
        if weekday == 6 and hour < 21:  # Sonntag vor 21:00 UTC
            return False
        if weekday == 4 and hour >= 22:  # Freitag nach 22:00 UTC
            return False

        return True

    @staticmethod
    def get_current_session(dt: Optional[datetime] = None) -> str:
        """
        Ermittelt die Haupt-Liquiditätssitzung.
        """
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # Prüfe zuerst Wochenend-Schließung
        if not SessionAnalyzer.is_market_open(dt):
            return "FOREX_MARKET_CLOSED (WEEKEND)"

        hour = dt.hour

        # London & NY Overlap (Höchste Liquidität und Volatilität)
        if 13 <= hour < 17:
            return "OVERLAP_LONDON_NY"
        # London Session (08:00 - 17:00 UTC)
        elif 8 <= hour < 17:
            return "LONDON"
        # New York Session (13:00 - 22:00 UTC)
        elif 13 <= hour < 22:
            return "NEW_YORK"
        # Tokyo / Asian Session (00:00 - 09:00 UTC)
        elif 0 <= hour < 9:
            return "TOKYO_ASIAN"
        # Sydney / Pacific Session (21:00 - 06:00 UTC)
        elif hour >= 21 or hour < 6:
            return "SYDNEY"
        else:
            return "GLOBAL_INTERBANK"
