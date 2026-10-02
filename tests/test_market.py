from datetime import UTC, date, datetime

import pytest
from exchange_calendars.exchange_calendar_xnys import XNYSExchangeCalendar

from stock_digest.market import choose_session

CAL = XNYSExchangeCalendar(start=date(2026, 8, 1), end=date(2026, 10, 30))


@pytest.mark.parametrize("now, session, mode, noted", [
    (datetime(2026, 9, 28, 13, 40, tzinfo=UTC), date(2026, 9, 25), "completed", True),   # 9:40 ET: just opened
    (datetime(2026, 9, 28, 15, 0, tzinfo=UTC), date(2026, 9, 28), "intraday", False),
    (datetime(2026, 9, 28, 20, 5, tzinfo=UTC), date(2026, 9, 28), "intraday", False),    # 16:05 ET: just closed
    (datetime(2026, 9, 28, 20, 20, tzinfo=UTC), date(2026, 9, 28), "completed", False),
    (datetime(2026, 9, 27, 16, 0, tzinfo=UTC), date(2026, 9, 25), "completed", False),   # Sunday
])
def test_choose_session_has_no_dead_zones(now, session, mode, noted):  # B14
    chosen, chosen_mode, note = choose_session(now, CAL)
    assert chosen.date() == session
    assert chosen_mode == mode
    assert bool(note) == noted
