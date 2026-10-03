"""SPEC section 15 retention: 7 daily, 4 weekly, 6 monthly. Pure policy, no files."""
from datetime import datetime, timedelta, timezone

from ankiweb.application.retention import parse_stamp, select_keep

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _daily(n: int, hour: int = 3) -> list[datetime]:
    base = NOW.replace(hour=hour, minute=0)
    return [base - timedelta(days=i) for i in range(n)]


def test_parse_stamp_reads_backup_filenames():
    assert parse_stamp("anki-lan-web-20261003T170500Z.tar.gz") == datetime(
        2026, 10, 3, 17, 5, 0, tzinfo=timezone.utc)
    assert parse_stamp("anki-lan-web-20261003T170500Z.tar.gz.sha256") is None
    assert parse_stamp("notes.txt") is None


def test_empty_input_keeps_nothing():
    assert select_keep([], NOW) == set()


def test_newest_backup_is_always_kept():
    only = [NOW - timedelta(days=400)]
    assert select_keep(only, NOW) == set(only)


def test_keeps_newest_per_day_for_seven_days():
    stamps = _daily(7) + [NOW.replace(hour=1)]      # two backups today; 03:00 is newer than 01:00
    keep = select_keep(stamps, NOW)
    assert NOW.replace(hour=3) in keep and NOW.replace(hour=1) not in keep


def test_thirty_daily_backups_collapse_to_expected_window():
    keep = select_keep(_daily(30), NOW)
    assert all(d in keep for d in _daily(7))            # the last 7 days all survive
    assert len(keep) <= 7 + 4 + 6                       # but never more than the three windows
    assert _daily(30)[-1] not in keep                   # a month-old, mid-month backup is pruned


def test_weekly_and_monthly_buckets_reach_back():
    stamps = [NOW - timedelta(weeks=w) for w in range(0, 20)]
    keep = select_keep(stamps, NOW)
    assert NOW - timedelta(weeks=3) in keep             # inside the 4 weekly buckets
    assert NOW - timedelta(weeks=19) not in keep        # older than 6 months
    first_of = {(d.year, d.month) for d in keep}
    assert len(first_of) >= 5                           # monthly coverage


def test_result_is_subset_of_input():
    stamps = _daily(40)
    assert select_keep(stamps, NOW) <= set(stamps)
