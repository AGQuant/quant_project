"""
NSE trading-holiday calendar — Scorr
=====================================
Full-closure EQUITY/F&O trading holidays. Weekend-falling holidays are
omitted (the weekend check already covers them). Settlement-only holidays
do NOT close trading and are excluded.

Source: NSE official 2026 circular (verified against Zerodha holiday calendar).
Update yearly: add the new year's set and extend NSE_HOLIDAYS.

is_trading_day(d): False on weekends AND notified holidays — the single
gate the scheduler / market-hours logic should use.
"""

from datetime import date

# 16 full weekday closures for 2026 (NSE equity + F&O).
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 15),   # Municipal Corp Elections Maharashtra
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Eid
    date(2026, 6, 26),   # Moharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
}

# Aggregate set across all loaded years.
NSE_HOLIDAYS = set()
NSE_HOLIDAYS |= NSE_HOLIDAYS_2026


def is_nse_holiday(d: date) -> bool:
    """True if d is a notified NSE full-closure trading holiday."""
    return d in NSE_HOLIDAYS


def is_trading_day(d: date) -> bool:
    """True only if d is a weekday AND not an NSE holiday."""
    if d.weekday() >= 5:        # Sat/Sun
        return False
    if d in NSE_HOLIDAYS:
        return False
    return True
