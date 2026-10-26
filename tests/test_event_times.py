import asyncio
import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, mock_open, patch
from zoneinfo import ZoneInfo

with patch("desktop_notifier.DesktopNotifier"), patch("logging.basicConfig"):
    import main as app


START = datetime(2026, 1, 15, 12, tzinfo=UTC)
CALENDAR = "test-calendar"


class ParseEventTimeTests(unittest.TestCase):
    def test_explicit_offsets_are_preserved_without_a_timezone_field(self):
        for text in (
            "2026-01-15T12:34:56Z",
            "2026-01-15T12:34:56+00:00",
            "2026-01-15T12:34:56.123456+05:45",
            "2026-01-15T12:34:56-07:00",
        ):
            with self.subTest(text=text):
                expected = datetime.fromisoformat(text)
                actual = app.parse_event_time({"dateTime": text})
                assert actual is not None
                self.assertEqual(actual, expected)
                self.assertEqual(actual.utcoffset(), expected.utcoffset())
                self.assertEqual(
                    actual.replace(tzinfo=None), expected.replace(tzinfo=None)
                )

    def test_naive_times_use_the_explicit_iana_timezone(self):
        cases = (
            ("2026-01-15T12:00:00", "America/New_York", -300),
            ("2026-07-15T12:00:00", "America/New_York", -240),
            ("2026-03-08T01:59:59", "America/New_York", -300),
            ("2026-03-08T03:00:00", "America/New_York", -240),
            ("2026-11-01T00:59:59", "America/New_York", -240),
            ("2026-11-01T02:00:00", "America/New_York", -300),
            ("2026-07-15T12:00:00", "Europe/Berlin", 120),
            ("2026-01-15T12:00:00", "Asia/Kathmandu", 345),
            ("2026-01-15T12:00:00", "UTC", 0),
        )
        for text, zone, offset_minutes in cases:
            with self.subTest(text=text, zone=zone):
                actual = app.parse_event_time({"dateTime": text, "timeZone": zone})
                assert actual is not None
                assert isinstance(actual.tzinfo, ZoneInfo)
                self.assertEqual(actual.tzinfo.key, zone)
                self.assertEqual(actual.utcoffset(), timedelta(minutes=offset_minutes))
                self.assertEqual(
                    actual.replace(tzinfo=None), datetime.fromisoformat(text)
                )

    def test_nonexistent_and_ambiguous_dst_wall_times_are_rejected(self):
        cases = (
            ("2026-03-08T02:00:00", "America/New_York"),
            ("2026-03-08T02:30:00", "America/New_York"),
            ("2026-03-08T02:59:59", "America/New_York"),
            ("2026-11-01T01:00:00", "America/New_York"),
            ("2026-11-01T01:30:00", "America/New_York"),
            ("2026-11-01T01:59:59", "America/New_York"),
            ("2026-03-29T02:30:00", "Europe/Berlin"),
            ("2026-10-25T02:30:00", "Europe/Berlin"),
        )
        for text, zone in cases:
            with self.subTest(text=text, zone=zone), self.assertRaises(ValueError):
                app.parse_event_time({"dateTime": text, "timeZone": zone})

    def test_explicit_offsets_remain_authoritative_around_dst_transitions(self):
        for text in (
            "2026-03-08T02:30:00-05:00",
            "2026-03-08T02:30:00-04:00",
            "2026-11-01T01:30:00-04:00",
            "2026-11-01T01:30:00-05:00",
            "2026-11-01T01:30:00Z",
        ):
            with self.subTest(text=text):
                expected = datetime.fromisoformat(text)
                actual = app.parse_event_time(
                    {"dateTime": text, "timeZone": "America/New_York"}
                )
                assert actual is not None
                self.assertEqual(actual, expected)
                self.assertEqual(actual.utcoffset(), expected.utcoffset())
                self.assertEqual(
                    actual.replace(tzinfo=None), expected.replace(tzinfo=None)
                )

    def test_naive_time_without_a_timezone_is_not_assumed_to_be_utc(self):
        with self.assertRaises(ValueError):
            app.parse_event_time({"dateTime": "2026-01-15T12:00:00"})

    def test_naive_time_requires_a_valid_iana_timezone_string(self):
        for zone in (None, "", "Not/A_Zone", "+05:30", "/UTC", 0, True, [], {}):
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                app.parse_event_time(
                    {"dateTime": "2026-01-15T12:00:00", "timeZone": zone}
                )

    def test_all_day_dates_use_date_fromisoformat_and_utc_midnight(self):
        for text in ("2026-01-15", "20260115", "2026-W03-4"):
            for zone in (None, "America/New_York", "Not/A_Zone"):
                with self.subTest(text=text, zone=zone):
                    start = {"date": text}
                    if zone is not None:
                        start["timeZone"] = zone
                    actual = app.parse_event_time(start)
                    assert actual is not None
                    expected = datetime.combine(
                        date.fromisoformat(text), datetime.min.time(), UTC
                    )
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual.utcoffset(), timedelta(0))
                    self.assertEqual(actual.hour, 0)

    def test_non_dict_start_is_rejected_with_value_error(self):
        for start in (None, [], (), "dateTime", 1, True, START):
            with self.subTest(start=start), self.assertRaises(ValueError):
                app.parse_event_time(start)

    def test_date_and_datetime_fields_must_be_strings(self):
        for field in ("date", "dateTime"):
            for value in (None, False, 0, 1.5, [], {}, date(2026, 1, 15), START):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    app.parse_event_time({field: value, "timeZone": "UTC"})

    def test_malformed_datetime_and_date_only_datetime_fields_are_rejected(self):
        for text in (
            "",
            "not-a-time",
            "2026-01-15",
            "2026-13-15T12:00:00Z",
            "2026-01-32T12:00:00Z",
            "2026-01-15T25:00:00Z",
            "2026-01-15T12:00:00+25:00",
            "2026-01-15T12:00:00Zjunk",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                app.parse_event_time({"dateTime": text, "timeZone": "UTC"})

    def test_malformed_dates_and_datetime_strings_in_date_fields_are_rejected(self):
        for text in (
            "",
            "not-a-date",
            "2026-02-29",
            "2026-13-01",
            "2026-01-00",
            "2026-01-15T00:00:00",
            "2026-01-15T00:00:00Z",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                app.parse_event_time({"date": text})

    def test_dict_without_date_or_datetime_returns_none(self):
        for start in ({}, {"timeZone": "UTC"}, {"summary": "No start"}):
            with self.subTest(start=start):
                self.assertIsNone(app.parse_event_time(start))


class EventNotificationTimesTests(unittest.TestCase):
    def setUp(self):
        self.logging = self.enterContext(patch.object(app, "logging", Mock()))

    def event(self, **fields):
        return {"id": "test-event", "start": {"dateTime": START.isoformat()}, **fields}

    def assert_times(self, event, expected_start, expected_times):
        result = app.event_notification_times(event, CALENDAR)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        start, times = result
        self.assertEqual(start, expected_start)
        self.assertEqual(start.utcoffset(), expected_start.utcoffset())
        self.assertIsNotNone(start.utcoffset())
        self.assertIsInstance(times, list)
        self.assertIn(start, times)
        self.assertEqual(set(times), set(expected_times))
        for notify_time in times:
            self.assertIsNotNone(notify_time.utcoffset())

    def test_start_is_included_without_custom_reminders(self):
        for reminders in (
            {},
            {"useDefault": False},
            {"useDefault": False, "overrides": []},
        ):
            with self.subTest(reminders=reminders):
                self.assert_times(self.event(reminders=reminders), START, [START])
        self.assert_times(self.event(), START, [START])
        self.logging.warning.assert_not_called()

    def test_unrepresentable_utc_instants_are_rejected_as_invalid_events(self):
        for text in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(start=text), self.assertRaises(ValueError):
                app.event_notification_times(
                    self.event(start={"dateTime": text}), CALENDAR
                )

    def test_default_reminders_are_not_interpreted_as_custom_popups(self):
        for fields in ({}, {"useDefault": True}):
            with self.subTest(fields=fields):
                reminders = {
                    "overrides": [{"method": "popup", "minutes": 10}],
                    **fields,
                }
                self.assert_times(self.event(reminders=reminders), START, [START])
        self.logging.warning.assert_not_called()

    def test_valid_popup_minutes_include_both_boundaries(self):
        minutes = (0, 1, 15, 40320)
        reminders = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": value} for value in minutes],
        }
        self.assert_times(
            self.event(reminders=reminders),
            START,
            [START, *(START - timedelta(minutes=value) for value in minutes)],
        )
        self.logging.warning.assert_not_called()

    def test_popup_reminders_use_elapsed_utc_time_across_dst_transitions(self):
        cases = (
            (
                "2026-11-01T02:05:00",
                datetime(2026, 11, 1, 7, 5, tzinfo=UTC),
                datetime(2026, 11, 1, 6, 55, tzinfo=UTC),
            ),
            (
                "2026-03-08T03:05:00",
                datetime(2026, 3, 8, 7, 5, tzinfo=UTC),
                datetime(2026, 3, 8, 6, 55, tzinfo=UTC),
            ),
        )
        for text, expected_start_utc, expected_popup_utc in cases:
            with self.subTest(start=text):
                event = self.event(
                    start={"dateTime": text, "timeZone": "America/New_York"},
                    reminders={
                        "useDefault": False,
                        "overrides": [{"method": "popup", "minutes": 10}],
                    },
                )
                start, times = app.event_notification_times(event, CALENDAR)
                self.assertEqual(start.tzinfo, ZoneInfo("America/New_York"))
                self.assertEqual(
                    start.replace(tzinfo=None), datetime.fromisoformat(text)
                )
                self.assertEqual(start.astimezone(UTC), expected_start_utc)
                for notify_time in times:
                    self.assertIs(notify_time.tzinfo, UTC)
                self.assertCountEqual(times, [expected_start_utc, expected_popup_utc])
                self.assertEqual(
                    (start.astimezone(UTC) - min(times)).total_seconds(), 600
                )
                self.logging.warning.assert_not_called()

    def test_email_reminders_are_ignored_even_with_invalid_minutes(self):
        reminders = {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 10},
                {"method": "email", "minutes": "invalid"},
                {"method": "email"},
                {"method": "popup", "minutes": 5},
            ],
        }
        self.assert_times(
            self.event(reminders=reminders),
            START,
            [START, START - timedelta(minutes=5)],
        )
        self.logging.warning.assert_not_called()

    def test_invalid_popups_warn_without_discarding_valid_reminders(self):
        invalid_overrides = [
            {"method": "popup"},
            *(
                {"method": "popup", "minutes": value}
                for value in (
                    None,
                    True,
                    False,
                    "5",
                    5.0,
                    float("inf"),
                    float("nan"),
                    [],
                    {},
                    -1,
                    40321,
                    10**1000,
                )
            ),
            None,
            [],
            "popup",
            1,
        ]
        for override in invalid_overrides:
            with self.subTest(override=override):
                self.logging.reset_mock()
                reminders = {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 10},
                        override,
                        {"method": "popup", "minutes": 20},
                    ],
                }
                self.assert_times(
                    self.event(reminders=reminders),
                    START,
                    [
                        START,
                        START - timedelta(minutes=10),
                        START - timedelta(minutes=20),
                    ],
                )
                self.logging.warning.assert_called()

    def test_reminder_datetime_underflow_warns_without_invalidating_the_event(self):
        start = datetime(1, 1, 1, 0, 5, tzinfo=UTC)
        reminders = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": value} for value in (5, 6, 40320, 1)
            ],
        }
        self.assert_times(
            self.event(start={"dateTime": start.isoformat()}, reminders=reminders),
            start,
            [start, start - timedelta(minutes=5), start - timedelta(minutes=1)],
        )
        self.logging.warning.assert_called()

    def test_bad_reminders_containers_warn_and_preserve_start(self):
        for reminders in (None, [], (), "reminders", 1, False):
            with self.subTest(reminders=reminders):
                self.logging.reset_mock()
                self.assert_times(self.event(reminders=reminders), START, [START])
                self.logging.warning.assert_called()

    def test_use_default_requires_a_boolean(self):
        for use_default in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(use_default=use_default):
                self.logging.reset_mock()
                reminders = {
                    "useDefault": use_default,
                    "overrides": [{"method": "popup", "minutes": 10}],
                }
                self.assert_times(self.event(reminders=reminders), START, [START])
                self.logging.warning.assert_called()

    def test_bad_overrides_containers_warn_and_preserve_start(self):
        for overrides in (None, {}, (), "overrides", 1, False):
            with self.subTest(overrides=overrides):
                self.logging.reset_mock()
                reminders = {"useDefault": False, "overrides": overrides}
                self.assert_times(self.event(reminders=reminders), START, [START])
                self.logging.warning.assert_called()

    def test_event_requires_a_nonempty_string_id(self):
        missing_id = self.event()
        del missing_id["id"]
        invalid_events = [
            missing_id,
            *(self.event(id=value) for value in (None, "", 0, False, [], {})),
        ]
        for event in invalid_events:
            with self.subTest(event=event), self.assertRaises(ValueError):
                app.event_notification_times(event, CALENDAR)

    def test_non_dict_events_are_rejected(self):
        for event in (None, [], (), "event", 1, True):
            with self.subTest(event=event), self.assertRaises(ValueError):
                app.event_notification_times(event, CALENDAR)

    def test_event_requires_a_valid_start(self):
        missing_start = self.event()
        del missing_start["start"]
        invalid_starts = (
            None,
            [],
            {},
            {"timeZone": "UTC"},
            {"dateTime": None},
            {"dateTime": "not-a-time"},
            {"date": "2026-02-29"},
            {"dateTime": "2026-01-15T12:00:00"},
            {"dateTime": "2026-01-15T12:00:00", "timeZone": "Not/A_Zone"},
            {"dateTime": "2026-03-08T02:30:00", "timeZone": "America/New_York"},
            {"dateTime": "2026-11-01T01:30:00", "timeZone": "America/New_York"},
        )
        for event in (
            missing_start,
            *(self.event(start=value) for value in invalid_starts),
        ):
            with self.subTest(event=event), self.assertRaises(ValueError):
                app.event_notification_times(event, CALENDAR)

    def test_timezone_and_all_day_starts_are_supported(self):
        for start, expected in (
            (
                {"dateTime": "2026-07-15T12:00:00", "timeZone": "America/New_York"},
                datetime.fromisoformat("2026-07-15T12:00:00-04:00"),
            ),
            (
                {"date": "2026-01-15", "timeZone": "America/New_York"},
                datetime(2026, 1, 15, tzinfo=UTC),
            ),
        ):
            with self.subTest(start=start):
                reminders = {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": 10}],
                }
                self.assert_times(
                    self.event(start=start, reminders=reminders),
                    expected,
                    [expected, expected - timedelta(minutes=10)],
                )


class CancelledEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_skeletal_events_do_not_block_a_valid_event(self):
        valid_event = {
            "id": "valid-event",
            "summary": "Valid event",
            "start": {"dateTime": START.isoformat()},
        }
        events = [
            {"status": "cancelled"},
            {"id": "cancelled-skeletal", "status": "cancelled"},
            {**valid_event, "id": "cancelled-full", "status": "cancelled"},
            valid_event,
        ]

        async def send(**kwargs):
            kwargs["on_dispatched"]()
            return "notification-id"

        service = SimpleNamespace(close=AsyncMock())
        notifier = SimpleNamespace(
            get_capabilities=AsyncMock(return_value={app.Capability.ON_DISPATCHED}),
            send=AsyncMock(side_effect=send),
        )
        clock = Mock(wraps=datetime)
        clock.now.return_value = START
        waits = 0

        async def wait_for_work(tasks, seconds):
            nonlocal waits
            waits += 1
            if notifier.send.await_count:
                raise asyncio.CancelledError
            if waits > 20:
                self.fail(
                    "Main did not deliver the valid event within 20 loop iterations"
                )
            for _ in range(5):
                await asyncio.sleep(0)

        for name, value in (
            ("open", mock_open(read_data=CALENDAR + "\n")),
            ("datetime", clock),
            ("GoogleCalendarClient", Mock(return_value=service)),
            ("authenticate_google_calendar", AsyncMock(return_value=service)),
            ("get_upcoming_events", AsyncMock(return_value=events)),
            ("notifier", notifier),
            ("logging", Mock()),
            ("wait_for_work", wait_for_work),
        ):
            self.enterContext(patch.object(app, name, value, create=name == "open"))

        sent = set()
        with self.assertRaises(asyncio.CancelledError):
            await app.main(sent)

        notifier.send.assert_awaited_once()
        self.assertEqual(notifier.send.await_args.kwargs["title"], "Valid event")
        self.assertEqual(sent, {(CALENDAR, "valid-event", START)})
        service.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
