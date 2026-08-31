from datetime import datetime


class SessionAnalyzer:
    """
    Ermittelt die aktuell aktive Forex-Handelssitzung basierend auf UTC-Zeit.
    """

    @staticmethod
    def get_current_session(dt: datetime = None) -> str:
        if dt is None:
            dt = datetime.utcnow()

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
