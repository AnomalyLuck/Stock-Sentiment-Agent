from datetime import UTC, datetime

from stock_digest.dates import (availability_bound, date_in_passage, display_timestamp,
                                normalize_source_timestamp, time_in_passage)

REF = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)


def test_normalizes_common_formats():
    assert normalize_source_timestamp("September 28, 2026") == "2026-09-28"
    assert normalize_source_timestamp("Sept. 28, 2026") == "2026-09-28"
    assert normalize_source_timestamp("2026/09/28") == "2026-09-28"
    assert normalize_source_timestamp("28-Sep-2026") == "2026-09-28"
    assert normalize_source_timestamp("2026-09-28T14:15:00Z") == "2026-09-28T14:15:00+00:00"
    assert normalize_source_timestamp("2026-09-28T14:15:00") is None  # no invented timezone


def test_year_less_dateline_substantiates_with_reference():  # B5
    assert date_in_passage("2026-09-28", "Published Sep 28, 10:15 AM ET", REF)
    assert not date_in_passage("2026-09-28", "Published Sep 28, 10:15 AM ET")  # no reference, no inference
    assert not date_in_passage("2026-09-27", "Published Sep 28, 10:15 AM ET", REF)
    # A year-less future date resolves to last year, so it cannot match this year's claim.
    assert not date_in_passage("2026-09-30", "Sep 30", REF)


def test_relative_datelines():  # B5
    assert date_in_passage("2026-09-28", "Updated 3 hours ago", REF)
    assert date_in_passage("2026-09-27", "yesterday", REF)
    assert not date_in_passage("2026-09-20", "2 hours ago", REF)


def test_full_dates_still_match():
    assert date_in_passage("2026-09-28", "September 28, 2026 07:00 AM", REF)
    assert date_in_passage("2026-09-28", "9/28/2026")
    assert date_in_passage("2026-09-28", "28 Sept 2026")


def test_model_written_time_needs_quoted_time_and_zone():  # B9
    value = "2026-09-28T10:15:00-04:00"
    assert time_in_passage(value, "Sep 28, 2026 10:15 AM ET")
    assert time_in_passage(value, "2026-09-28 10:15 EDT")
    assert not time_in_passage(value, "Sep 28, 2026")          # date only
    assert not time_in_passage(value, "Sep 28, 2026 10:15 AM PT")  # wrong zone
    assert not time_in_passage(value, "Sep 28, 2026 10:15 PM ET")  # wrong meridiem
    assert not time_in_passage(value, "Sep 28, 2026 10:15 AM")    # no zone


def test_date_only_bound_is_capped_at_retrieval():
    assert availability_bound("2026-09-28", REF) == REF
    future = availability_bound("2026-10-05", REF)
    assert future > REF


def test_display_timestamp_never_raises():  # B4
    assert display_timestamp("September 28, 2026") == "2026-09-28"
    assert display_timestamp("2026-09-28T14:15:00+00:00") == "2026-09-28 10:15 EDT"
    assert display_timestamp("sometime last week") == "sometime last week"
