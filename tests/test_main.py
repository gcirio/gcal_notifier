import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, mock_open, patch
from urllib.parse import parse_qsl, urlsplit

from desktop_notifier import Capability
from googleapiclient.errors import HttpError

from google_calendar import AuthorizationRequired, CalendarConfigurationError

with patch("desktop_notifier.DesktopNotifier"), patch("logging.basicConfig"):
    import main as app


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
CALENDAR = "calendar-a"


class StopMain(BaseException):
    """End a main-loop test without being caught as an operational failure."""


class LoopLimitExceeded(BaseException):
    """Fail a test instead of hanging if scheduling stops making progress."""


def event(event_id, start_seconds, *, end_seconds=None, **fields):
    if end_seconds is not None:
        fields["end"] = {
            "dateTime": (START + timedelta(seconds=end_seconds)).isoformat()
        }
    return {
        "id": event_id,
        "summary": event_id,
        "start": {"dateTime": (START + timedelta(seconds=start_seconds)).isoformat()},
        **fields,
    }


def http_error(status, reason, *, details=None):
    error = {
        "code": status,
        "message": "Test calendar failure",
        "errors": [{"reason": reason}],
    }
    if details is not None:
        error["details"] = details
    content = json.dumps({"error": error}).encode("utf-8")
    return HttpError(
        SimpleNamespace(status=status, reason="Test calendar failure"), content
    )


class MainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = START
        self.stop_at = None
        self.steps = 0
        self.sleep_adjustments = []
        self.sleeps = []
        self.fetches = []
        self.attempts = []
        self.deliveries = []
        self.trace = []
        self.events = {}
        self.fetch_effect = None
        self.send_effect = None
        self.dispatch_enabled = True
        self.sent_notifications = set()

        controlled_datetime = Mock(wraps=datetime)
        controlled_datetime.now.side_effect = self.datetime_now
        self.service = SimpleNamespace(close=AsyncMock())
        self.client_factory = Mock(return_value=self.service)
        self.authenticate = AsyncMock(return_value=self.service)
        self.get_events = AsyncMock(side_effect=self.fetch)
        self.logging = Mock()
        self.notifier = SimpleNamespace(
            get_capabilities=AsyncMock(return_value=frozenset(Capability)),
            send=AsyncMock(side_effect=self.send),
            clear=AsyncMock(),
        )
        for name, value in (
            ("datetime", controlled_datetime),
            ("GoogleCalendarClient", self.client_factory),
            ("authenticate_google_calendar", self.authenticate),
            ("get_upcoming_events", self.get_events),
            ("notifier", self.notifier),
            ("wait_for_work", self.wait_for_work),
            ("logging", self.logging),
        ):
            self.enterContext(patch.object(app, name, value))
        self.open_url = self.enterContext(patch("webbrowser.open"))
        self.set_calendars(CALENDAR)

    def set_calendars(self, *calendar_ids):
        self.calendar_file = mock_open(read_data="\n".join(calendar_ids) + "\n")
        self.enterContext(patch.object(app, "open", self.calendar_file, create=True))

    def step(self):
        self.steps += 1
        if self.steps > 2000:
            raise LoopLimitExceeded("Main loop exceeded 2000 fake clock/I/O operations")

    def elapsed(self):
        return (self.now - START).total_seconds()

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def datetime_now(self, tz=None):
        self.step()
        if tz is None:
            return self.now.replace(tzinfo=None)
        return self.now.astimezone(tz)

    async def sleep(self, seconds):
        self.step()
        if seconds < 0:
            raise LoopLimitExceeded(f"Main requested a negative sleep: {seconds}")
        self.sleeps.append((self.elapsed(), seconds))
        wake_at = self.now + timedelta(seconds=seconds)
        if seconds > 0 and self.sleep_adjustments:
            wake_at += timedelta(seconds=self.sleep_adjustments.pop(0))
        if self.stop_at is not None and (
            self.now >= self.stop_at or wake_at > self.stop_at
        ):
            self.now = max(self.now, self.stop_at)
            raise StopMain
        self.now = wake_at

    async def wait_for_work(self, tasks, seconds):
        tasks = tuple(tasks)
        for _ in range(20):
            await asyncio.sleep(0)
            if any(task.done() for task in tasks):
                return
        await self.sleep(seconds)

    async def fetch(self, service, calendar_id):
        self.step()
        self.assertIs(service, self.service)
        self.fetches.append((calendar_id, self.elapsed()))
        self.trace.append(("fetch", calendar_id, self.elapsed()))
        if self.fetch_effect is not None:
            return await self.fetch_effect(calendar_id)
        return self.events.get(calendar_id, [])

    async def send(self, **kwargs):
        self.step()
        self.attempts.append((kwargs["title"], self.elapsed()))
        self.trace.append(("send", kwargs["title"], self.elapsed()))
        if self.send_effect is not None:
            await self.send_effect(**kwargs)
        if self.dispatch_enabled:
            on_dispatched = kwargs.get("on_dispatched")
            if on_dispatched is not None:
                on_dispatched()
            self.deliveries.append((kwargs["title"], self.elapsed()))
        return f"notification-{len(self.attempts)}"

    async def run_main(self, seconds, *, supplied_set=True):
        self.stop_at = self.now + timedelta(seconds=seconds)
        with self.assertRaises(StopMain):
            if supplied_set:
                await app.main(self.sent_notifications)
            else:
                await app.main()

    def sent_key(self, event_id, scheduled_seconds, calendar_id=CALENDAR):
        return (calendar_id, event_id, START + timedelta(seconds=scheduled_seconds))

    def start(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.addAsyncCleanup(self.cancel_task, task)
        return task

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 0.5)

    async def until(self, predicate, task):
        async def poll():
            while not predicate():
                if task.done():
                    task.result()
                    self.fail("Task finished before the expected event")
                await asyncio.sleep(0)

        await asyncio.wait_for(poll(), 0.5)

    def test_event_dispatch_timeout_defaults_to_five_seconds(self):
        self.assertEqual(app.NOTIFICATION_DISPATCH_TIMEOUT, 5)

    async def test_event_dispatch_waits_for_threadsafe_acknowledgement(self):
        self.dispatch_enabled = False
        loop = asyncio.get_running_loop()
        with patch.object(
            loop, "call_soon_threadsafe", wraps=loop.call_soon_threadsafe
        ) as schedule:
            task = self.start(
                app.send_event_notification(title="dispatch", message="Starting now")
            )
            await self.until(lambda: self.notifier.send.await_count == 1, task)
            await asyncio.sleep(0)

            self.assertFalse(task.done())
            self.notifier.get_capabilities.assert_awaited_once_with()
            kwargs = self.notifier.send.await_args.kwargs
            self.assertEqual(kwargs["title"], "dispatch")
            self.assertEqual(kwargs["message"], "Starting now")
            kwargs["on_dispatched"]()
            schedule.assert_called_once()
            event_set = schedule.call_args.args[0]
            self.assertIsInstance(event_set.__self__, asyncio.Event)
            self.assertEqual(event_set.__name__, "set")

        await asyncio.wait_for(task, 0.5)

    async def test_event_dispatch_rejects_missing_or_empty_notification_id(self):
        for notification_id in (None, ""):
            with self.subTest(notification_id=notification_id):

                async def empty_id(notification_id=notification_id, **kwargs):
                    kwargs["on_dispatched"]()
                    return notification_id

                self.notifier.send.side_effect = empty_id
                with self.assertRaises(RuntimeError):
                    await asyncio.wait_for(
                        app.send_event_notification(title="empty", message="Test"),
                        0.5,
                    )

    async def test_event_dispatch_requires_dispatch_capability(self):
        self.notifier.get_capabilities.return_value = frozenset(Capability) - {
            Capability.ON_DISPATCHED
        }

        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(
                app.send_event_notification(title="unsupported", message="Test"), 0.5
            )

        self.notifier.get_capabilities.assert_awaited_once_with()
        self.notifier.send.assert_not_awaited()

    async def test_event_dispatch_without_acknowledgement_times_out(self):
        self.dispatch_enabled = False

        with (
            patch.object(app, "NOTIFICATION_DISPATCH_TIMEOUT", 0.01),
            self.assertRaises(TimeoutError),
        ):
            await app.send_event_notification(title="suppressed", message="Test")

        self.notifier.send.assert_awaited_once()
        self.assertEqual(self.deliveries, [])

    async def test_stalled_event_sender_times_out_and_is_cancelled(self):
        cancelled = asyncio.Event()

        async def stall(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.send_effect = stall
        with (
            patch.object(app, "NOTIFICATION_DISPATCH_TIMEOUT", 0.01),
            self.assertRaises(TimeoutError),
        ):
            await app.send_event_notification(title="stalled", message="Test")

        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.attempts, [("stalled", 0)])
        self.assertEqual(self.deliveries, [])

    async def test_stalled_dispatch_capability_lookup_times_out(self):
        async def stall():
            await asyncio.Event().wait()

        self.notifier.get_capabilities.side_effect = stall
        with (
            patch.object(app, "NOTIFICATION_DISPATCH_TIMEOUT", 0.01),
            self.assertRaises(TimeoutError),
        ):
            await app.send_event_notification(title="stalled", message="Test")

        self.notifier.send.assert_not_awaited()

    async def test_sent_key_is_added_only_after_dispatch_acknowledgement(self):
        self.events[CALENDAR] = [event("waiting-for-ack", 0)]
        self.dispatch_enabled = False
        task = self.start(app.main(self.sent_notifications))
        await self.until(lambda: self.notifier.send.await_count == 1, task)
        await asyncio.sleep(0)

        self.assertFalse(task.done())
        self.assertEqual(self.sent_notifications, set())
        self.notifier.send.await_args.kwargs["on_dispatched"]()
        await self.until(
            lambda: self.sent_key("waiting-for-ack", 0) in self.sent_notifications,
            task,
        )

        self.notifier.send.assert_awaited_once()
        await self.cancel_task(task)
        self.service.close.assert_awaited_once_with()

    async def test_cancellation_while_waiting_for_dispatch_propagates(self):
        self.events[CALENDAR] = [event("waiting-for-ack", 0)]
        self.dispatch_enabled = False
        task = self.start(app.main(self.sent_notifications))
        await self.until(lambda: self.notifier.send.await_count == 1, task)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)

        self.assertEqual(self.sent_notifications, set())
        self.assertEqual(self.deliveries, [])
        self.notifier.send.assert_awaited_once()
        self.service.close.assert_awaited_once_with()

    async def test_truthy_id_without_ack_retries_after_five_seconds_and_delivers_once(
        self,
    ):
        self.events[CALENDAR] = [event("suppressed", 0), event("healthy", 0)]

        async def suppress_once(**kwargs):
            self.dispatch_enabled = not (
                kwargs["title"] == "suppressed" and self.elapsed() == 0
            )
            if kwargs["title"] == "suppressed":
                self.assertNotIn(
                    self.sent_key("suppressed", 0), self.sent_notifications
                )

        self.send_effect = suppress_once
        with patch.object(app, "NOTIFICATION_DISPATCH_TIMEOUT", 0.01):
            await self.run_main(6)

        self.assertEqual(
            self.attempts, [("suppressed", 0), ("healthy", 0), ("suppressed", 5)]
        )
        self.assertEqual(self.deliveries, [("healthy", 0), ("suppressed", 5)])
        self.assertEqual(
            self.sent_notifications,
            {self.sent_key("suppressed", 0), self.sent_key("healthy", 0)},
        )

    async def test_sender_stall_does_not_block_healthy_event_and_retries(self):
        self.events[CALENDAR] = [event("stalled", 0), event("healthy", 0)]
        cancelled = asyncio.Event()

        async def stall_once(**kwargs):
            if kwargs["title"] == "stalled" and self.elapsed() == 0:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        self.send_effect = stall_once
        with patch.object(app, "NOTIFICATION_DISPATCH_TIMEOUT", 0.01):
            await asyncio.wait_for(self.run_main(6), 0.5)

        self.assertTrue(cancelled.is_set())
        self.assertEqual(
            self.attempts, [("stalled", 0), ("healthy", 0), ("stalled", 5)]
        )
        self.assertEqual(self.deliveries, [("healthy", 0), ("stalled", 5)])
        self.assertEqual(
            self.sent_notifications,
            {self.sent_key("stalled", 0), self.sent_key("healthy", 0)},
        )

    async def test_unsupported_dispatch_capability_is_retryable(self):
        self.events[CALENDAR] = [event("unsupported-once", 0)]
        self.notifier.get_capabilities.side_effect = [
            frozenset(Capability) - {Capability.ON_DISPATCHED},
            frozenset(Capability),
        ]

        await self.run_main(6)

        self.assertEqual(self.notifier.get_capabilities.await_count, 2)
        self.assertEqual(self.attempts, [("unsupported-once", 5)])
        self.assertEqual(self.deliveries, [("unsupported-once", 5)])
        self.assertEqual(
            self.sent_notifications, {self.sent_key("unsupported-once", 0)}
        )

    def test_prune_sent_notifications_mutates_shared_set_at_expiry_boundary(self):
        expired = self.sent_key("expired", -1)
        boundary = self.sent_key("boundary", 0, "calendar-b")
        recent = self.sent_key("recent", 0.000001)
        future = self.sent_key("future", 601)
        self.sent_notifications.update({expired, boundary, recent, future})
        shared_set = self.sent_notifications

        app.prune_sent_notifications(shared_set, START + timedelta(seconds=600))

        self.assertIs(shared_set, self.sent_notifications)
        self.assertEqual(shared_set, {recent, future})
        app.prune_sent_notifications(shared_set, START + timedelta(seconds=601))
        self.assertEqual(shared_set, {future})

    async def test_pruning_does_not_replay_delivered_or_expired_notifications(self):
        self.sent_notifications.update(
            {
                self.sent_key("expired", -601),
                self.sent_key("boundary", -600),
                self.sent_key("already-delivered", -599),
            }
        )
        self.events[CALENDAR] = [
            event("expired", -601),
            event("boundary", -600),
            event("already-delivered", -599),
            event("new", 1),
        ]
        shared_set = self.sent_notifications

        await self.run_main(2)

        self.assertIs(self.sent_notifications, shared_set)
        self.assertEqual(self.deliveries, [("new", 1)])
        self.notifier.send.assert_awaited_once()
        self.assertEqual(shared_set, {self.sent_key("new", 1)})

    async def test_sent_keys_are_pruned_even_without_cached_events(self):
        self.sent_notifications.add(self.sent_key("previously-delivered", 0))

        await self.run_main(601)

        self.assertEqual(self.sent_notifications, set())
        self.notifier.send.assert_not_awaited()

    def test_read_calendar_ids_strips_whitespace_and_ignores_blank_lines(self):
        self.calendar_file = mock_open(read_data="  calendar-a  \n\n\tcalendar-b\t\n ")
        with patch.object(app, "open", self.calendar_file):
            self.assertEqual(app.read_calendar_ids(), [CALENDAR, "calendar-b"])

    def test_read_calendar_ids_rejects_empty_or_whitespace_only_files(self):
        for contents in ("", " \n\t\n"):
            with self.subTest(contents=contents):
                with (
                    patch.object(app, "open", mock_open(read_data=contents)),
                    self.assertRaises(CalendarConfigurationError),
                ):
                    app.read_calendar_ids()

    def test_read_calendar_ids_reports_unreadable_files_as_configuration_errors(self):
        for error in (FileNotFoundError("missing"), PermissionError("unreadable")):
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(app, "open", side_effect=error),
                    self.assertRaises(CalendarConfigurationError),
                ):
                    app.read_calendar_ids()

    def test_read_calendar_accounts_preserves_order_and_infers_normal_email_ids(self):
        self.set_calendars(
            "  person@gmail.com  ",
            "",
            "\tmember@company.example\t",
            "generated@group.calendar.google.com",
            "room@resource.calendar.google.com",
            "holidays@group.v.calendar.google.com",
            CALENDAR,
        )

        accounts = app.read_calendar_accounts()

        self.assertIsInstance(accounts, dict)
        self.assertEqual(
            list(accounts.items()),
            [
                ("person@gmail.com", "person@gmail.com"),
                ("member@company.example", "member@company.example"),
                ("generated@group.calendar.google.com", None),
                ("room@resource.calendar.google.com", None),
                ("holidays@group.v.calendar.google.com", None),
                (CALENDAR, None),
            ],
        )

    def test_read_calendar_accounts_explicit_email_overrides_inference(self):
        generated = "generated@group.calendar.google.com"
        self.set_calendars(
            f"  {generated}  |  member@company.example  ",
            f"\t{CALENDAR}\t|\tperson@gmail.com\t",
            "person@gmail.com | other@company.example",
        )

        self.assertEqual(
            list(app.read_calendar_accounts().items()),
            [
                (generated, "member@company.example"),
                (CALENDAR, "person@gmail.com"),
                ("person@gmail.com", "other@company.example"),
            ],
        )

    def test_read_calendar_accounts_deduplicates_identical_account_mappings(self):
        self.set_calendars(
            CALENDAR,
            "person@gmail.com",
            "generated@group.calendar.google.com | member@company.example",
            f"  {CALENDAR}  ",
            "person@gmail.com | person@gmail.com",
            "generated@group.calendar.google.com | member@company.example",
        )

        self.assertEqual(
            list(app.read_calendar_accounts().items()),
            [
                (CALENDAR, None),
                ("person@gmail.com", "person@gmail.com"),
                ("generated@group.calendar.google.com", "member@company.example"),
            ],
        )

    def test_read_calendar_accounts_rejects_conflicting_duplicate_accounts(self):
        conflicting_lines = (
            ("calendar-a | person@gmail.com", "calendar-a | member@company.example"),
            ("person@gmail.com", "person@gmail.com | member@company.example"),
            ("calendar-a", "calendar-a | person@gmail.com"),
        )
        for first, second in conflicting_lines:
            for lines in ((first, second), (second, first)):
                with self.subTest(lines=lines):
                    self.set_calendars(*lines)
                    with self.assertRaises(CalendarConfigurationError):
                        app.read_calendar_accounts()

    def test_read_calendar_ids_wraps_account_reader_and_returns_only_calendar_ids(self):
        accounts = {
            "generated@group.calendar.google.com": "person@gmail.com",
            "member@company.example": "other@company.example",
            CALENDAR: None,
        }
        with patch.object(app, "read_calendar_accounts", return_value=accounts) as read:
            self.assertEqual(app.read_calendar_ids(), list(accounts))
        read.assert_called_once_with()

    def test_read_calendar_accounts_rejects_empty_or_whitespace_only_files(self):
        for contents in ("", " \n\t\n"):
            with self.subTest(contents=contents):
                with (
                    patch.object(app, "open", mock_open(read_data=contents)),
                    self.assertRaises(CalendarConfigurationError),
                ):
                    app.read_calendar_accounts()

    def test_read_calendar_accounts_reports_unreadable_files_as_configuration_errors(
        self,
    ):
        errors = (
            FileNotFoundError("missing"),
            PermissionError("unreadable"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(app, "open", side_effect=error),
                    self.assertRaises(CalendarConfigurationError),
                ):
                    app.read_calendar_accounts()

    async def test_conflicting_calendar_accounts_fail_before_authentication(self):
        self.set_calendars(
            f"{CALENDAR} | person@gmail.com",
            f"{CALENDAR} | member@company.example",
        )
        self.stop_at = START + timedelta(seconds=1)

        with self.assertRaises(CalendarConfigurationError):
            await app.main(self.sent_notifications)

        self.authenticate.assert_not_awaited()
        self.get_events.assert_not_awaited()
        self.notifier.send.assert_not_awaited()

    async def test_main_reads_calendar_accounts_once_and_fetches_only_calendar_ids(
        self,
    ):
        generated = "generated@group.calendar.google.com"
        self.set_calendars(
            f"  {generated} | person@gmail.com  ",
            "member@company.example",
            f"{CALENDAR} | other@company.example",
        )
        with patch.object(
            app, "read_calendar_accounts", wraps=app.read_calendar_accounts
        ) as read:
            await self.run_main(1201)

        read.assert_called_once_with()
        self.calendar_file.assert_called_once_with(
            app.APP_DIR / app.CALENDAR_IDS_FILE, "r", encoding="utf-8"
        )
        calendar_ids = [generated, "member@company.example", CALENDAR]
        self.assertEqual(
            self.fetches,
            [
                (calendar_id, at)
                for at in (0, 600, 1200)
                for calendar_id in calendar_ids
            ],
        )
        self.assertEqual(
            self.get_events.await_args_list,
            [call(self.service, calendar_id) for calendar_id in calendar_ids] * 3,
        )
        self.logging.warning.assert_not_called()

    async def test_unmapped_calendars_warn_at_startup_but_remain_fetchable(self):
        unmapped = ["generated@group.calendar.google.com", CALENDAR]
        self.set_calendars(*unmapped, "person@gmail.com", "member@company.example")
        startup_warnings = []

        async def authenticate(client):
            startup_warnings.extend(self.logging.warning.call_args_list)
            return client

        self.authenticate.side_effect = authenticate

        await self.run_main(601)

        self.assertTrue(startup_warnings)
        warning_text = "\n".join(map(str, startup_warnings))
        for calendar_id in unmapped:
            self.assertIn(calendar_id, warning_text)
        self.assertNotIn("person@gmail.com", warning_text)
        self.assertNotIn("member@company.example", warning_text)
        self.assertEqual(self.logging.warning.call_args_list, startup_warnings)
        self.assertEqual(
            self.fetches,
            [
                (calendar_id, at)
                for at in (0, 600)
                for calendar_id in (
                    *unmapped,
                    "person@gmail.com",
                    "member@company.example",
                )
            ],
        )

    def test_build_meeting_url_adds_email_without_adding_pli(self):
        account = "person+calendar@company.example"
        for url in (
            "https://meet.google.com/abc-defg-hij",
            "https://meet.google.com/abc-defg-hij#details",
            "https://meet.google.com/abc-defg-hij?empty=&token=hello%20world#details",
        ):
            with self.subTest(url=url):
                original = urlsplit(url)
                result = urlsplit(app.build_meeting_url(url, account))
                self.assertEqual(result._replace(query=""), original._replace(query=""))
                self.assertEqual(
                    parse_qsl(result.query, keep_blank_values=True),
                    [
                        *parse_qsl(original.query, keep_blank_values=True),
                        ("authuser", account),
                    ],
                )

    def test_build_meeting_url_replaces_all_authusers_and_preserves_other_parameters(
        self,
    ):
        url = (
            "https://meet.google.com/abc-defg-hij?authuser=0&empty=&x=one"
            "&authuser=&x=two&pli=0&authuser=old%40example.com&encoded=a%2Bb%26c#details"
        )
        account = "person+calendar@company.example"

        result = urlsplit(app.build_meeting_url(url, account))

        self.assertEqual(result._replace(query=""), urlsplit(url)._replace(query=""))
        self.assertEqual(
            parse_qsl(result.query, keep_blank_values=True),
            [
                ("empty", ""),
                ("x", "one"),
                ("x", "two"),
                ("pli", "0"),
                ("encoded", "a+b&c"),
                ("authuser", account),
            ],
        )

    def test_build_meeting_url_without_account_returns_exact_original_url(self):
        for url in (
            "https://meet.google.com/abc-defg-hij",
            "https://meet.google.com/abc-defg-hij?",
            "https://meet.google.com/abc-defg-hij?pli=1&authuser=1#",
            "https://meet.google.com/abc-defg-hij?authuser=0&authuser=&empty="
            "&flag&token=hello%20world&encoded=%2f#details",
        ):
            with self.subTest(url=url):
                self.assertEqual(app.build_meeting_url(url, None), url)

    async def test_empty_calendar_configuration_fails_before_authentication(self):
        self.set_calendars()
        self.stop_at = START + timedelta(seconds=1)

        with self.assertRaises(CalendarConfigurationError):
            await app.main(self.sent_notifications)

        self.authenticate.assert_not_awaited()
        self.get_events.assert_not_awaited()
        self.notifier.send.assert_not_awaited()

    def test_event_notification_times_returns_aware_start_and_popup_times(self):
        meeting = event(
            "meeting",
            300,
            reminders={
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 5},
                    {"method": "email", "minutes": 10},
                    {"method": "popup", "minutes": 1},
                ],
            },
        )

        start, notification_times = app.event_notification_times(meeting, CALENDAR)

        self.assertEqual(start, START + timedelta(seconds=300))
        self.assertIsNotNone(start.utcoffset())
        self.assertCountEqual(
            notification_times,
            [START + timedelta(seconds=300), START, START + timedelta(seconds=240)],
        )

    def test_default_reminders_do_not_use_popup_or_email_overrides(self):
        meeting = event(
            "default-reminders",
            300,
            reminders={
                "useDefault": True,
                "overrides": [
                    {"method": "popup", "minutes": 5},
                    {"method": "email", "minutes": 5},
                ],
            },
        )

        start, notification_times = app.event_notification_times(meeting, CALENDAR)

        self.assertEqual(start, START + timedelta(seconds=300))
        self.assertEqual(notification_times, [start])

    def test_invalid_popup_values_skip_only_the_bad_reminder(self):
        for minutes in (None, "2", True, False, -1, 0.5, float("inf"), 10**20):
            with self.subTest(minutes=minutes):
                meeting = event(
                    "mixed-reminders",
                    120,
                    reminders={
                        "useDefault": False,
                        "overrides": [
                            {"method": "popup", "minutes": minutes},
                            {"method": "popup", "minutes": 2},
                        ],
                    },
                )
                self.logging.warning.reset_mock()

                start, notification_times = app.event_notification_times(
                    meeting, CALENDAR
                )

                self.assertEqual(start, START + timedelta(seconds=120))
                self.assertCountEqual(notification_times, [start, START])
                self.logging.warning.assert_called()

    async def test_malformed_events_do_not_block_good_events_or_valid_reminders(self):
        missing_id = event("missing-id", 0)
        del missing_id["id"]
        invalid_events = [
            None,
            "not-an-event",
            [],
            42,
            missing_id,
            *(event(event_id, 0) for event_id in (None, "", 42, [], {})),
            {"id": "missing-start"},
            event("null-start", 0, start=None),
            event("non-dict-start", 0, start="2026-01-01"),
            event("empty-start", 0, start={}),
            event("bad-time", 0, start={"dateTime": "not-a-time"}),
            event("null-time", 0, start={"dateTime": None}),
            event("bad-date", 0, start={"date": "2026-02-30"}),
            event("no-offset", 0, start={"dateTime": "2026-01-01T00:00:00"}),
        ]
        self.events[CALENDAR] = [
            *invalid_events,
            event(
                "mixed-reminders",
                120,
                reminders={
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": None},
                        {"method": "popup", "minutes": "2"},
                        {"method": "popup", "minutes": True},
                        {"method": "popup", "minutes": -1},
                        {"method": "popup", "minutes": 0.5},
                        {"method": "popup", "minutes": 10**20},
                        {"method": "popup", "minutes": 2},
                        {"method": "email", "minutes": 1},
                    ],
                },
            ),
            event("healthy", 0),
        ]

        await self.run_main(121)

        self.assertEqual(
            self.deliveries,
            [("mixed-reminders", 0), ("healthy", 0), ("mixed-reminders", 120)],
        )
        self.assertEqual(
            self.sent_notifications,
            {
                self.sent_key("mixed-reminders", 0),
                self.sent_key("mixed-reminders", 120),
                self.sent_key("healthy", 0),
            },
        )
        self.assertGreaterEqual(self.logging.warning.call_count, len(invalid_events))

    async def test_naive_event_with_explicit_timezone_is_scheduled_as_aware(self):
        self.events[CALENDAR] = [
            event(
                "zoned",
                0,
                start={
                    "dateTime": "2025-12-31T19:00:00",
                    "timeZone": "America/New_York",
                },
            )
        ]

        await self.run_main(1)

        self.assertEqual(self.deliveries, [("zoned", 0)])
        self.assertEqual(self.sent_notifications, {self.sent_key("zoned", 0)})

    async def test_dst_popup_and_start_deliver_once_without_replay_after_pruning(self):
        sent_snapshots = []

        async def observe_sent_keys(tasks, seconds):
            sent_snapshots.append(set(self.sent_notifications))
            await self.wait_for_work(tasks, seconds)

        for month, day, local_start in (
            (3, 8, "2026-03-08T03:05:00"),
            (11, 1, "2026-11-01T02:05:00"),
        ):
            with self.subTest(local_start=local_start):
                popup_time = datetime(2026, month, day, 6, 55, tzinfo=timezone.utc)
                self.now = popup_time
                initial_elapsed = self.elapsed()
                self.attempts.clear()
                self.deliveries.clear()
                self.sent_notifications.clear()
                self.notifier.send.reset_mock()
                sent_snapshots.clear()
                self.events[CALENDAR] = [
                    {
                        "id": "dst-meeting",
                        "summary": "dst-meeting",
                        "start": {
                            "dateTime": local_start,
                            "timeZone": "America/New_York",
                        },
                        "reminders": {
                            "useDefault": False,
                            "overrides": [{"method": "popup", "minutes": 10}],
                        },
                    }
                ]

                with patch.object(app, "wait_for_work", observe_sent_keys):
                    await self.run_main(601)

                expected = [
                    ("dst-meeting", initial_elapsed),
                    ("dst-meeting", initial_elapsed + 600),
                ]
                self.assertEqual(self.attempts, expected)
                self.assertEqual(self.deliveries, expected)
                self.assertEqual(self.notifier.send.await_count, 2)
                self.assertIn({(CALENDAR, "dst-meeting", popup_time)}, sent_snapshots)
                self.assertEqual(
                    [
                        send_call.kwargs["message"]
                        for send_call in self.notifier.send.await_args_list
                    ],
                    ["Starting in 10 minutes", "Starting now"],
                )
                self.assertEqual(
                    self.sent_notifications,
                    {(CALENDAR, "dst-meeting", popup_time + timedelta(seconds=600))},
                )

    async def test_all_day_event_still_uses_utc_midnight(self):
        self.events[CALENDAR] = [event("all-day", 0, start={"date": "2026-01-01"})]
        parsed = app.parse_event_time({"date": "2026-01-01"})
        self.assertEqual(parsed, START)
        assert parsed is not None
        self.assertIs(parsed.tzinfo, timezone.utc)

        await self.run_main(1)

        self.assertEqual(self.deliveries, [("all-day", 0)])
        self.assertEqual(self.sent_notifications, {self.sent_key("all-day", 0)})
        self.assertEqual(
            self.notifier.send.await_args.kwargs["message"], "Starting now"
        )

    async def test_invalid_end_drops_omitted_pending_retry_without_crashing(self):
        invalid_ends = (
            {"dateTime": "not-a-time"},
            {"dateTime": "2026-01-01T00:10:00"},
            {"date": "2026-02-30"},
            None,
        )
        meetings = [
            event(f"bad-end-{index}", 300, end=end)
            for index, end in enumerate(invalid_ends)
        ]
        healthy = event("healthy", 615)

        async def omit_pending_events(calendar_id):
            return [*meetings, healthy] if self.elapsed() < 600 else [healthy]

        async def fail_pending_events(**kwargs):
            if kwargs["title"].startswith("bad-end-"):
                raise RuntimeError("Desktop temporarily unavailable")

        self.fetch_effect = omit_pending_events
        self.send_effect = fail_pending_events

        await self.run_main(616)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 600)])
        self.assertEqual(
            self.attempts,
            [
                (meeting["id"], second)
                for second in (300, 305, 315, 335, 375, 455)
                for meeting in meetings
            ]
            + [("healthy", 615)],
        )
        self.assertEqual(self.deliveries, [("healthy", 615)])
        self.assertEqual(self.sent_notifications, {self.sent_key("healthy", 615)})

    async def test_cancelled_skeletal_events_are_ignored(self):
        self.events[CALENDAR] = [
            {"id": "cancelled-without-start", "status": "cancelled"},
            {"status": "cancelled"},
            event("cancelled-with-start", 0, status="cancelled"),
            event("healthy", 0),
        ]

        await self.run_main(1)

        self.assertEqual(self.deliveries, [("healthy", 0)])
        self.notifier.send.assert_awaited_once()
        self.assertEqual(self.sent_notifications, {self.sent_key("healthy", 0)})

    async def test_main_accepts_omitted_sent_set(self):
        self.events[CALENDAR] = [event("due", 0)]

        await self.run_main(1, supplied_set=False)

        self.assertEqual(self.deliveries, [("due", 0)])
        self.client_factory.assert_called_once_with()
        self.authenticate.assert_awaited_once_with(self.service)
        self.service.close.assert_awaited_once_with()

    async def test_existing_sent_key_is_skipped_and_new_success_is_recorded(self):
        self.sent_notifications.add(self.sent_key("already-sent", 0))
        self.events[CALENDAR] = [event("already-sent", 0), event("new", 0)]

        await self.run_main(1)

        self.assertEqual(self.deliveries, [("new", 0)])
        self.assertEqual(
            self.sent_notifications,
            {self.sent_key("already-sent", 0), self.sent_key("new", 0)},
        )

    async def test_future_event_before_due_event_is_reevaluated_after_slow_send(self):
        self.events[CALENDAR] = [event("future-b", 1), event("due-a", 0)]

        async def slow_send(**kwargs):
            if kwargs["title"] == "due-a":
                self.advance(2)

        self.send_effect = slow_send

        await self.run_main(10)

        self.assertEqual(self.attempts, [("due-a", 0), ("future-b", 2)])
        self.assertEqual(
            self.sent_notifications,
            {self.sent_key("due-a", 0), self.sent_key("future-b", 1)},
        )

    async def test_slow_send_expires_subsequent_notifications_before_their_attempts(
        self,
    ):
        self.events[CALENDAR] = [
            event("slow", 0),
            event("boundary", -598),
            event("expired", -599),
        ]

        async def slow_send(**kwargs):
            if kwargs["title"] == "slow":
                self.advance(2)

        self.send_effect = slow_send

        await self.run_main(3)

        self.assertEqual(self.attempts, [("slow", 0)])
        self.assertEqual(self.deliveries, [("slow", 2)])
        self.assertEqual(self.sent_notifications, {self.sent_key("slow", 0)})

    async def test_ninety_second_late_wakeup_delivers_once(self):
        self.events[CALENDAR] = [event("late", 10)]
        self.sleep_adjustments = [90]

        await self.run_main(1201)

        self.assertEqual(self.deliveries, [("late", 100)])
        self.notifier.send.assert_awaited_once()
        self.assertEqual(self.sent_notifications, set())

    async def test_catchup_window_includes_due_and_excludes_age_600_or_more(self):
        self.events[CALENDAR] = [
            event("future", 1),
            event("now", 0),
            event("late", -90),
            event("inside", -599),
            event("boundary", -600),
            event("expired", -601),
        ]

        await self.run_main(0.5)

        self.assertCountEqual(self.deliveries, [("now", 0), ("late", 0), ("inside", 0)])
        self.assertEqual(
            self.sent_notifications,
            {
                self.sent_key("now", 0),
                self.sent_key("late", -90),
                self.sent_key("inside", -599),
            },
        )

    async def test_popup_override_catches_up_without_sending_email_or_future_start(
        self,
    ):
        self.events[CALENDAR] = [
            event(
                "meeting",
                210,
                reminders={
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 5},
                        {"method": "email", "minutes": 10},
                    ],
                },
            )
        ]

        await self.run_main(1)

        self.assertEqual(self.deliveries, [("meeting", 0)])
        self.assertEqual(self.sent_notifications, {self.sent_key("meeting", -90)})
        self.assertEqual(
            self.notifier.send.await_args.kwargs["message"], "Starting in 4 minutes"
        )

    async def test_duplicate_zero_minute_popups_share_event_start_key(self):
        self.events[CALENDAR] = [
            event(
                "meeting",
                0,
                reminders={
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 0},
                        {"method": "popup", "minutes": 0},
                    ],
                },
            )
        ]

        await self.run_main(1)

        self.notifier.send.assert_awaited_once()
        self.assertEqual(self.deliveries, [("meeting", 0)])
        self.assertEqual(self.sent_notifications, {self.sent_key("meeting", 0)})

    async def test_remaining_minutes_round_up_fractional_seconds(self):
        reminders = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 5}],
        }
        self.events[CALENDAR] = [
            event("subsecond", 0.25, reminders=reminders),
            event("over-minute", 60.25, reminders=reminders),
            event("exact-minute", 60, reminders=reminders),
        ]

        await self.run_main(0.1)

        self.assertEqual(
            {
                send_call.kwargs["title"]: send_call.kwargs["message"]
                for send_call in self.notifier.send.await_args_list
            },
            {
                "subsecond": "Starting in 1 minutes",
                "over-minute": "Starting in 2 minutes",
                "exact-minute": "Starting in 1 minutes",
            },
        )

    async def test_popup_overrides_at_or_outside_catchup_boundary_are_skipped(self):
        reminders = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 15}],
        }
        self.events[CALENDAR] = [
            event("boundary", 300, reminders=reminders),
            event("expired", 299, reminders=reminders),
        ]

        await self.run_main(1)

        self.notifier.send.assert_not_awaited()
        self.assertEqual(self.sent_notifications, set())

    async def test_invalid_meeting_links_do_not_block_event_notifications(self):
        calendar_id = "personal@example.com"
        self.set_calendars(calendar_id)
        self.events[calendar_id] = [
            event("invalid-url", 0, hangoutLink="https://[invalid"),
            event(
                "invalid-type", 0, hangoutLink={"url": "https://meet.google.com/bad"}
            ),
            event("valid-url", 0, hangoutLink="https://meet.google.com/valid"),
        ]

        await self.run_main(1)

        self.assertEqual(len(self.deliveries), 3)
        sent = {
            args.kwargs["title"]: args.kwargs
            for args in self.notifier.send.await_args_list
        }
        self.assertIsNone(sent["invalid-url"]["on_clicked"])
        self.assertIsNone(sent["invalid-type"]["on_clicked"])
        sent["valid-url"]["on_clicked"]()
        self.open_url.assert_called_once_with(
            "https://meet.google.com/valid?authuser=personal%40example.com"
        )
        self.logging.warning.assert_called()

    async def test_meeting_callbacks_keep_their_own_urls(self):
        self.events[CALENDAR] = [
            event("first", 0, hangoutLink="https://meet.google.com/first"),
            event("second", 0, hangoutLink="https://meet.google.com/second"),
        ]

        await self.run_main(1)

        self.assertEqual(len(self.notifier.send.await_args_list), 2)
        for send_call in self.notifier.send.await_args_list:
            send_call.kwargs["on_clicked"]()
        self.assertEqual(
            self.open_url.call_args_list,
            [
                call("https://meet.google.com/first"),
                call("https://meet.google.com/second"),
            ],
        )

    async def test_bare_personal_and_work_calendars_select_their_source_accounts(self):
        personal = "person@gmail.com"
        work = "member@company.example"
        shared_url = "https://meet.google.com/shared?empty=#details"
        self.set_calendars(personal, work)
        self.events = {
            personal: [
                event(
                    "shared-id",
                    0,
                    summary="personal-shared",
                    hangoutLink=shared_url,
                    organizer={"email": work},
                ),
                event(
                    "distinct-id",
                    0,
                    summary="personal-distinct",
                    hangoutLink="https://meet.google.com/personal",
                ),
            ],
            work: [
                event(
                    "shared-id",
                    0,
                    summary="work-shared",
                    hangoutLink=shared_url,
                    organizer={"email": personal},
                ),
                event(
                    "distinct-id",
                    0,
                    summary="work-distinct",
                    hangoutLink="https://meet.google.com/work",
                ),
            ],
        }

        await self.run_main(1)

        notifications = {
            send_call.kwargs["title"]: send_call.kwargs
            for send_call in self.notifier.send.await_args_list
        }
        expected_urls = {
            "personal-shared": "https://meet.google.com/shared?empty="
            "&authuser=person%40gmail.com#details",
            "personal-distinct": "https://meet.google.com/personal?authuser=person%40gmail.com",
            "work-shared": "https://meet.google.com/shared?empty="
            "&authuser=member%40company.example#details",
            "work-distinct": "https://meet.google.com/work?authuser=member%40company.example",
        }
        self.assertEqual(self.notifier.send.await_count, 4)
        self.assertEqual(notifications.keys(), expected_urls.keys())
        self.open_url.assert_not_called()
        for title in expected_urls:
            notifications[title]["on_clicked"]()
        self.assertEqual(
            self.open_url.call_args_list, [call(url) for url in expected_urls.values()]
        )
        self.assertEqual(self.fetches, [(personal, 0), (work, 0)])
        self.assertEqual(
            self.sent_notifications,
            {
                self.sent_key(event_id, 0, calendar_id)
                for calendar_id in (personal, work)
                for event_id in ("shared-id", "distinct-id")
            },
        )
        self.logging.warning.assert_not_called()

    async def test_explicit_accounts_override_generated_and_email_calendar_ids(self):
        generated = "generated@group.calendar.google.com"
        email_id = "member@company.example"
        self.set_calendars(
            f"  {generated} | {email_id}  ",
            f"{email_id} | person@gmail.com",
        )
        self.events = {
            generated: [
                event("generated", 0, hangoutLink="https://meet.google.com/team")
            ],
            email_id: [
                event("email", 0, hangoutLink="https://meet.google.com/private")
            ],
        }

        await self.run_main(1)

        self.assertEqual(self.fetches, [(generated, 0), (email_id, 0)])
        self.assertEqual(self.deliveries, [("generated", 0), ("email", 0)])
        for send_call in self.notifier.send.await_args_list:
            send_call.kwargs["on_clicked"]()
        self.assertEqual(
            self.open_url.call_args_list,
            [
                call("https://meet.google.com/team?authuser=member%40company.example"),
                call("https://meet.google.com/private?authuser=person%40gmail.com"),
            ],
        )
        self.assertEqual(
            self.sent_notifications,
            {
                self.sent_key("generated", 0, generated),
                self.sent_key("email", 0, email_id),
            },
        )
        self.logging.warning.assert_not_called()

    async def test_unmapped_generated_calendar_keeps_original_meeting_url(self):
        generated = "generated@group.calendar.google.com"
        url = (
            "https://meet.google.com/unmapped?authuser=0&authuser=&empty="
            "&flag&token=hello%20world&encoded=%2f#details"
        )
        self.set_calendars(generated)
        self.events[generated] = [
            event(
                "unmapped", 0, hangoutLink=url, organizer={"email": "person@gmail.com"}
            )
        ]

        await self.run_main(1)

        self.assertEqual(self.fetches, [(generated, 0)])
        self.notifier.send.assert_awaited_once()
        self.notifier.send.await_args.kwargs["on_clicked"]()
        self.open_url.assert_called_once_with(url)
        self.logging.warning.assert_called()

    async def test_late_clicks_keep_urls_after_more_events_from_both_calendars(self):
        personal = "person@gmail.com"
        work = "member@company.example"
        self.set_calendars(personal, work)
        self.events = {
            personal: [
                event(
                    "first",
                    0,
                    hangoutLink="https://meet.google.com/first?authuser=0&empty=#first",
                ),
                event("third", 20, hangoutLink="https://meet.google.com/third#third"),
            ],
            work: [
                event("second", 10, hangoutLink="https://meet.google.com/second"),
                event("no-link", 30),
            ],
        }

        await self.run_main(31)

        self.assertEqual(
            self.deliveries,
            [("first", 0), ("second", 10), ("third", 20), ("no-link", 30)],
        )
        callbacks = [
            send_call.kwargs["on_clicked"]
            for send_call in self.notifier.send.await_args_list
        ]
        self.assertIsNone(callbacks[3])
        self.open_url.assert_not_called()
        for index in (0, 2, 1, 0):
            callbacks[index]()
        first_url = (
            "https://meet.google.com/first?empty=&authuser=person%40gmail.com#first"
        )
        self.assertEqual(
            self.open_url.call_args_list,
            [
                call(first_url),
                call("https://meet.google.com/third?authuser=person%40gmail.com#third"),
                call(
                    "https://meet.google.com/second?authuser=member%40company.example"
                ),
                call(first_url),
            ],
        )

    async def test_failed_meeting_send_retries_with_its_source_account(self):
        personal = "person@gmail.com"
        work = "member@company.example"
        url = "https://meet.google.com/shared"
        self.set_calendars(personal, work)
        self.events = {
            personal: [event("shared-id", 0, summary="personal", hangoutLink=url)],
            work: [event("shared-id", 0, summary="work", hangoutLink=url)],
        }

        async def fail_once(**kwargs):
            if kwargs["title"] == "personal" and self.elapsed() == 0:
                raise RuntimeError("Desktop temporarily unavailable")

        self.send_effect = fail_once

        await self.run_main(6)

        self.assertEqual(self.attempts, [("personal", 0), ("work", 0), ("personal", 5)])
        self.assertEqual(self.deliveries, [("work", 0), ("personal", 5)])
        self.assertEqual(
            self.sent_notifications,
            {
                self.sent_key("shared-id", 0, personal),
                self.sent_key("shared-id", 0, work),
            },
        )
        for send_call in self.notifier.send.await_args_list:
            send_call.kwargs["on_clicked"]()
        self.assertEqual(
            self.open_url.call_args_list,
            [
                call("https://meet.google.com/shared?authuser=person%40gmail.com"),
                call(
                    "https://meet.google.com/shared?authuser=member%40company.example"
                ),
                call("https://meet.google.com/shared?authuser=person%40gmail.com"),
            ],
        )

    async def test_pending_ended_meeting_retry_keeps_source_account_after_refresh(self):
        work = "member@company.example"
        url = "https://meet.google.com/shared?authuser=1&empty=#notes"
        meeting = event("ended", 300, end_seconds=600, hangoutLink=url)
        healthy = event("work", 610, hangoutLink=url)
        self.set_calendars(f"{CALENDAR} | person@gmail.com", work)

        async def omit_after_end(calendar_id):
            if calendar_id == CALENDAR:
                return [meeting] if self.elapsed() < 600 else []
            return [healthy]

        async def fail_through_455(**kwargs):
            if kwargs["title"] == "ended" and self.elapsed() <= 455:
                raise RuntimeError("Desktop temporarily unavailable")

        self.fetch_effect = omit_after_end
        self.send_effect = fail_through_455

        await self.run_main(616)

        self.assertEqual(
            self.fetches, [(CALENDAR, 0), (work, 0), (CALENDAR, 600), (work, 600)]
        )
        self.assertEqual(
            self.attempts,
            [("ended", second) for second in (300, 305, 315, 335, 375, 455)]
            + [("work", 610), ("ended", 615)],
        )
        self.assertEqual(self.deliveries, [("work", 610), ("ended", 615)])
        self.assertEqual(
            self.sent_notifications,
            {self.sent_key("ended", 300), self.sent_key("work", 610, work)},
        )
        for send_call in self.notifier.send.await_args_list[-2:]:
            send_call.kwargs["on_clicked"]()
        self.assertEqual(
            self.open_url.call_args_list,
            [
                call(
                    "https://meet.google.com/shared?empty=&authuser=member%40company.example#notes"
                ),
                call(
                    "https://meet.google.com/shared?empty=&authuser=person%40gmail.com#notes"
                ),
            ],
        )

    async def test_failed_send_does_not_block_other_send_and_retries_after_five_seconds(
        self,
    ):
        self.events[CALENDAR] = [event("fails-once", 0), event("healthy", 0)]

        async def fail_once(**kwargs):
            if kwargs["title"] == "fails-once" and self.elapsed() == 0:
                raise RuntimeError("Desktop temporarily unavailable")

        self.send_effect = fail_once

        await self.run_main(6)

        self.assertEqual(
            self.attempts, [("fails-once", 0), ("healthy", 0), ("fails-once", 5)]
        )
        self.assertEqual(self.deliveries, [("healthy", 0), ("fails-once", 5)])
        self.assertEqual(
            self.sent_notifications,
            {self.sent_key("healthy", 0), self.sent_key("fails-once", 0)},
        )

    async def test_failed_send_is_not_marked_sent_or_retried_at_window_expiry(self):
        self.events[CALENDAR] = [event("expiring", -595)]

        async def fail_send(**kwargs):
            raise RuntimeError("Desktop temporarily unavailable")

        self.send_effect = fail_send

        await self.run_main(20)

        self.assertEqual(self.attempts, [("expiring", 0)])
        self.assertEqual(self.deliveries, [])
        self.assertEqual(self.sent_notifications, set())

    async def test_pending_retry_survives_refresh_omitting_an_ended_event(self):
        meeting = event("meeting", 300, end_seconds=600)

        async def omit_after_end(calendar_id):
            return [meeting] if self.elapsed() < 600 else []

        async def fail_through_455(**kwargs):
            if self.elapsed() <= 455:
                raise RuntimeError("Desktop temporarily unavailable")

        self.fetch_effect = omit_after_end
        self.send_effect = fail_through_455

        await self.run_main(1201)

        self.assertEqual(
            self.fetches, [(CALENDAR, 0), (CALENDAR, 600), (CALENDAR, 1200)]
        )
        self.assertEqual(
            self.attempts,
            [("meeting", second) for second in (300, 305, 315, 335, 375, 455, 615)],
        )
        self.assertEqual(self.deliveries, [("meeting", 615)])
        self.assertEqual(self.sent_notifications, set())

    async def test_event_ending_during_refresh_does_not_retain_pending_retry(self):
        meeting = event("meeting", 300, end_seconds=610)

        async def slow_removal(calendar_id):
            if self.elapsed() < 600:
                return [meeting]
            self.advance(20)
            return []

        async def fail_through_455(**kwargs):
            if self.elapsed() <= 455:
                raise RuntimeError("Desktop temporarily unavailable")

        self.fetch_effect = slow_removal
        self.send_effect = fail_through_455

        await self.run_main(901)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 600)])
        self.assertEqual(
            self.attempts,
            [("meeting", second) for second in (300, 305, 315, 335, 375, 455)],
        )
        self.assertEqual(self.deliveries, [])
        self.assertEqual(self.sent_notifications, set())

    async def test_refresh_completed_during_send_discards_removed_event_retry(self):
        release_refresh = asyncio.Event()
        refresh_finished = asyncio.Event()
        self.events[CALENDAR] = [
            event("release-refresh", 610),
            event("removed", 595, end_seconds=900),
        ]

        async def refresh(calendar_id):
            if len(self.fetches) == 1:
                return self.events[calendar_id]
            await release_refresh.wait()
            refresh_finished.set()
            return []

        async def send_and_release_refresh(**kwargs):
            if kwargs["title"] == "removed" and self.elapsed() <= 600:
                raise RuntimeError("Desktop temporarily unavailable")
            if kwargs["title"] == "release-refresh":
                self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 600)])
                release_refresh.set()
                await refresh_finished.wait()

        self.fetch_effect = refresh
        self.send_effect = send_and_release_refresh

        await self.run_main(611)

        self.assertTrue(refresh_finished.is_set())
        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 600)])
        self.assertEqual(
            self.attempts,
            [("removed", 595), ("removed", 600), ("release-refresh", 610)],
        )
        self.assertEqual(self.deliveries, [("release-refresh", 610)])
        self.assertEqual(
            self.sent_notifications, {self.sent_key("release-refresh", 610)}
        )

    async def test_removal_before_event_end_discards_pending_retry(self):
        meeting = event("meeting", 300, end_seconds=900)

        async def remove_before_end(calendar_id):
            return [meeting] if self.elapsed() < 600 else []

        async def fail_through_455(**kwargs):
            if self.elapsed() <= 455:
                raise RuntimeError("Desktop temporarily unavailable")

        self.fetch_effect = remove_before_end
        self.send_effect = fail_through_455

        await self.run_main(901)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 600)])
        self.assertEqual(
            self.attempts,
            [("meeting", second) for second in (300, 305, 315, 335, 375, 455)],
        )
        self.assertEqual(self.deliveries, [])
        self.assertEqual(self.sent_notifications, set())

    async def test_rescheduling_discards_old_pending_retry_but_sends_new_start(self):
        async def reschedule(calendar_id):
            if self.elapsed() < 600:
                return [event("meeting", 300, end_seconds=600)]
            return [event("meeting", 1200, end_seconds=1500)]

        async def fail_through_455(**kwargs):
            if self.elapsed() <= 455:
                raise RuntimeError("Desktop temporarily unavailable")

        self.fetch_effect = reschedule
        self.send_effect = fail_through_455

        await self.run_main(1201)

        self.assertEqual(
            self.attempts,
            [("meeting", second) for second in (300, 305, 315, 335, 375, 455, 1200)],
        )
        self.assertEqual(self.deliveries, [("meeting", 1200)])
        self.assertEqual(self.sent_notifications, {self.sent_key("meeting", 1200)})

    async def test_removing_popup_discards_pending_retry_but_sends_event_start(self):
        async def remove_popup(calendar_id):
            overrides = (
                [{"method": "popup", "minutes": 10}] if self.elapsed() < 600 else []
            )
            return [
                event(
                    "meeting",
                    900,
                    end_seconds=1200,
                    reminders={"useDefault": False, "overrides": overrides},
                )
            ]

        async def fail_through_455(**kwargs):
            if self.elapsed() <= 455:
                raise RuntimeError("Desktop temporarily unavailable")

        self.fetch_effect = remove_popup
        self.send_effect = fail_through_455

        await self.run_main(901)

        self.assertEqual(
            self.attempts,
            [("meeting", second) for second in (300, 305, 315, 335, 375, 455, 900)],
        )
        self.assertEqual(self.deliveries, [("meeting", 900)])
        self.assertEqual(self.sent_notifications, {self.sent_key("meeting", 900)})

    async def test_send_backoff_doubles_without_retrying_after_catchup_window(self):
        self.events[CALENDAR] = [event("unavailable", 0)]

        async def fail_send(**kwargs):
            raise RuntimeError("Desktop temporarily unavailable")

        self.send_effect = fail_send

        await self.run_main(1301)

        self.assertEqual(
            self.attempts,
            [("unavailable", second) for second in (0, 5, 15, 35, 75, 155, 315)],
        )
        self.assertEqual(self.sent_notifications, set())
        self.assertTrue(all(delay <= 600 for _, delay in self.sleeps))

    async def test_successful_send_resets_retry_delay_for_later_notification(self):
        self.events[CALENDAR] = [event("first", 0), event("second", 20)]
        failed = set()

        async def fail_each_once(**kwargs):
            title = kwargs["title"]
            if title not in failed:
                failed.add(title)
                raise RuntimeError("Desktop temporarily unavailable")

        self.send_effect = fail_each_once

        await self.run_main(30)

        self.assertEqual(
            self.attempts, [("first", 0), ("first", 5), ("second", 20), ("second", 25)]
        )
        self.assertEqual(self.deliveries, [("first", 5), ("second", 25)])

    async def test_matching_event_id_and_time_in_two_calendars_are_independent(self):
        self.set_calendars(CALENDAR, "calendar-b")
        self.events = {
            CALENDAR: [event("shared-id", 0, summary="from-a")],
            "calendar-b": [event("shared-id", 0, summary="from-b")],
        }

        await self.run_main(1)

        self.assertCountEqual(self.deliveries, [("from-a", 0), ("from-b", 0)])
        self.assertEqual(
            self.sent_notifications,
            {
                self.sent_key("shared-id", 0),
                self.sent_key("shared-id", 0, "calendar-b"),
            },
        )

    async def test_supplied_successful_set_prevents_resend_on_next_main_call(self):
        self.events[CALENDAR] = [event("due", 0)]

        await self.run_main(1)
        await self.run_main(1)

        self.assertEqual(self.deliveries, [("due", 0)])
        self.assertEqual(self.sent_notifications, {self.sent_key("due", 0)})
        self.assertEqual(self.authenticate.await_count, 2)

    async def test_cached_notification_is_sent_before_due_refresh_removes_event(self):
        async def fetch_then_remove(calendar_id):
            return [event("cached", 600)] if self.elapsed() == 0 else []

        self.fetch_effect = fetch_then_remove

        await self.run_main(601)

        self.assertEqual(self.deliveries, [("cached", 600)])
        self.assertEqual(
            self.trace,
            [("fetch", CALENDAR, 0), ("send", "cached", 600), ("fetch", CALENDAR, 600)],
        )

    async def test_slow_calendar_fetch_sends_other_cache_before_its_refresh(self):
        self.set_calendars(CALENDAR, "calendar-b")
        meeting = event("cached-b", 605, end_seconds=608)

        async def refresh(calendar_id):
            if calendar_id == CALENDAR:
                if self.elapsed() == 600:
                    self.advance(10)
                return []
            return [meeting] if self.elapsed() < 608 else []

        self.fetch_effect = refresh

        await self.run_main(611)

        self.assertEqual(self.deliveries, [("cached-b", 610)])
        self.assertEqual(
            self.sent_notifications, {self.sent_key("cached-b", 605, "calendar-b")}
        )
        self.assertEqual(
            self.fetches,
            [(CALENDAR, 0), ("calendar-b", 0), (CALENDAR, 600), ("calendar-b", 610)],
        )
        self.assertLess(
            self.trace.index(("send", "cached-b", 610)),
            self.trace.index(("fetch", "calendar-b", 610)),
        )

    async def test_cached_notification_is_delivered_while_another_fetch_is_pending(
        self,
    ):
        self.set_calendars(CALENDAR, "calendar-b")
        blocked = asyncio.Event()
        blocked_tasks = []
        fetch_cancelled = asyncio.Event()

        async def refresh(calendar_id):
            if calendar_id == CALENDAR:
                if self.elapsed() >= 600:
                    blocked_tasks.append(asyncio.current_task())
                    try:
                        await blocked.wait()
                    except asyncio.CancelledError:
                        await asyncio.sleep(0)
                        fetch_cancelled.set()
                        raise
                return []
            return [event("cached-b", 605)]

        async def send_while_fetching(**kwargs):
            self.assertEqual(kwargs["title"], "cached-b")
            self.assertEqual(len(blocked_tasks), 1)
            self.assertFalse(blocked_tasks[0].done())
            self.assertFalse(blocked.is_set())

        async def close_after_cancellation():
            self.assertTrue(fetch_cancelled.is_set())
            self.assertTrue(blocked_tasks[0].cancelled())

        self.fetch_effect = refresh
        self.send_effect = send_while_fetching
        self.service.close.side_effect = close_after_cancellation

        await self.run_main(606)

        self.assertEqual(self.deliveries, [("cached-b", 605)])
        self.assertEqual(
            self.sent_notifications, {self.sent_key("cached-b", 605, "calendar-b")}
        )
        self.assertEqual(len(blocked_tasks), 1)
        self.assertTrue(blocked_tasks[0].cancelled())
        self.assertTrue(fetch_cancelled.is_set())
        self.service.close.assert_awaited_once_with()

    async def test_successful_fetch_is_immediately_reevaluated_after_completion(self):
        async def slow_refresh(calendar_id):
            if len(self.fetches) == 1:
                return []
            self.advance(7)
            return [event("newly-fetched", 600)]

        self.fetch_effect = slow_refresh

        await self.run_main(608)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 600)])
        self.assertEqual(self.deliveries, [("newly-fetched", 607)])

    async def test_failed_fetch_is_immediately_reevaluated_for_cached_deadline(self):
        async def slow_failed_refresh(calendar_id):
            if len(self.fetches) == 1:
                return [event("cached", 605)]
            self.advance(10)
            raise OSError("Temporary network failure")

        self.fetch_effect = slow_failed_refresh

        await self.run_main(611)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 600)])
        self.assertEqual(self.deliveries, [("cached", 610)])

    async def test_transient_failure_preserves_cache_and_does_not_block_healthy_calendar(
        self,
    ):
        self.set_calendars(CALENDAR, "calendar-b")

        async def refresh(calendar_id):
            if calendar_id == CALENDAR:
                if self.elapsed() == 0:
                    return [event("cached-a", 602)]
                raise OSError("Calendar A temporarily unavailable")
            return [] if self.elapsed() == 0 else [event("fresh-b", 601)]

        self.fetch_effect = refresh

        await self.run_main(604)

        self.assertEqual(self.deliveries, [("fresh-b", 601), ("cached-a", 602)])
        self.assertCountEqual(
            self.fetches,
            [(CALENDAR, 0), ("calendar-b", 0), (CALENDAR, 600), ("calendar-b", 600)],
        )

    async def test_fetch_backoff_doubles_caps_at_600_and_resets_after_success(self):
        async def intermittent_fetch(calendar_id):
            if len(self.fetches) <= 8 or len(self.fetches) == 10:
                raise OSError("Temporary network failure")
            return []

        self.fetch_effect = intermittent_fetch

        await self.run_main(1841)

        self.assertEqual(
            self.fetches,
            [
                (CALENDAR, second)
                for second in (0, 5, 15, 35, 75, 155, 315, 635, 1235, 1835, 1840)
            ],
        )
        self.assertTrue(all(delay <= 600 for _, delay in self.sleeps))

    async def test_successful_refresh_interval_starts_at_fetch_completion(self):
        async def slow_fetch(calendar_id):
            if len(self.fetches) <= 2:
                self.advance(30)
            return []

        self.fetch_effect = slow_fetch

        await self.run_main(1261)

        self.assertEqual(
            self.fetches, [(CALENDAR, 0), (CALENDAR, 630), (CALENDAR, 1260)]
        )

    async def test_calendar_refresh_schedules_remain_independent(self):
        self.set_calendars(CALENDAR, "calendar-b")
        failed = False

        async def fail_a_once(calendar_id):
            nonlocal failed
            if calendar_id == CALENDAR and not failed:
                failed = True
                raise OSError("Calendar A temporarily unavailable")
            return []

        self.fetch_effect = fail_a_once

        await self.run_main(606)

        self.assertEqual(
            [at for calendar_id, at in self.fetches if calendar_id == CALENDAR],
            [0, 5, 605],
        )
        self.assertEqual(
            [at for calendar_id, at in self.fetches if calendar_id == "calendar-b"],
            [0, 600],
        )

    async def test_permanent_http_errors_disable_only_affected_calendars(self):
        errors = {
            "bad-400": http_error(400, "badRequest"),
            "bad-403": http_error(403, "forbidden"),
            "bad-404": http_error(404, "notFound"),
        }
        self.set_calendars(*errors, CALENDAR)

        async def fetch_with_permanent_failures(calendar_id):
            if calendar_id in errors:
                raise errors[calendar_id]
            return [event("healthy", 0)]

        self.fetch_effect = fetch_with_permanent_failures

        await self.run_main(1801)

        for calendar_id in errors:
            with self.subTest(calendar_id=calendar_id):
                self.assertEqual(
                    [at for name, at in self.fetches if name == calendar_id], [0]
                )
        self.assertEqual(
            [at for calendar_id, at in self.fetches if calendar_id == CALENDAR],
            [0, 600, 1200, 1800],
        )
        self.assertEqual(self.deliveries, [("healthy", 0)])

    async def assert_reauthentication_preserves_cached_alerts(self, failure):
        self.set_calendars(CALENDAR, "calendar-b")
        self.events = {
            CALENDAR: [event("cached-a", 605)],
            "calendar-b": [event("cached-b", 607), event("finish-auth", 610)],
        }
        authorized = asyncio.Event()
        auth_tasks = []
        revoked = False
        fetches_before_auth = [
            (CALENDAR, 0),
            ("calendar-b", 0),
            (CALENDAR, 600),
        ]

        async def authenticate(client, *, reauthenticate=False):
            self.assertIs(client, self.service)
            if reauthenticate:
                auth_tasks.append(asyncio.current_task())
                await authorized.wait()
            return self.service

        async def refresh(calendar_id):
            nonlocal revoked
            if self.elapsed() == 0:
                return self.events[calendar_id]
            if calendar_id == CALENDAR and not revoked:
                revoked = True
                raise failure
            self.assertTrue(authorized.is_set())
            self.assertTrue(auth_tasks[0].done())
            title = "fresh-a" if calendar_id == CALENDAR else "fresh-b"
            return [event(title, 610)]

        async def send_during_auth(**kwargs):
            if kwargs["title"] in ("cached-a", "cached-b", "finish-auth"):
                self.assertEqual(self.fetches, fetches_before_auth)
                self.assertEqual(len(auth_tasks), 1)
                self.assertFalse(auth_tasks[0].done())
                if kwargs["title"] == "finish-auth":
                    authorized.set()

        self.authenticate.side_effect = authenticate
        self.fetch_effect = refresh
        self.send_effect = send_during_auth

        await self.run_main(611)

        self.assertEqual(
            self.authenticate.await_args_list,
            [call(self.service), call(self.service, reauthenticate=True)],
        )
        self.assertEqual(
            self.fetches,
            fetches_before_auth + [(CALENDAR, 610), ("calendar-b", 610)],
        )
        self.assertEqual(
            self.deliveries[:3],
            [("cached-a", 605), ("cached-b", 607), ("finish-auth", 610)],
        )
        self.assertCountEqual(self.deliveries[3:], [("fresh-a", 610), ("fresh-b", 610)])
        self.assertEqual(len(auth_tasks), 1)
        self.assertTrue(auth_tasks[0].done())
        self.assertFalse(auth_tasks[0].cancelled())
        self.service.close.assert_awaited_once_with()

    async def test_authorization_required_pauses_fetches_but_preserves_cached_alerts(
        self,
    ):
        await self.assert_reauthentication_preserves_cached_alerts(
            AuthorizationRequired("Google Calendar access was revoked")
        )

    async def test_http_401_pauses_fetches_but_preserves_cached_alerts(self):
        await self.assert_reauthentication_preserves_cached_alerts(
            http_error(401, "authError")
        )

    async def test_confirmed_permission_403_errors_disable_only_affected_calendars(
        self,
    ):
        reasons = (
            "forbidden",
            "insufficientPermissions",
            "accessNotConfigured",
            "forbiddenForNonOrganizer",
            "notACalendarUser",
            "requiredAccessLevel",
        )
        self.set_calendars(*reasons, CALENDAR)

        async def deny_known_permissions(calendar_id):
            if calendar_id in reasons:
                raise http_error(403, calendar_id)
            return [event("healthy", 0)]

        self.fetch_effect = deny_known_permissions

        await self.run_main(601)

        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertEqual(
                    [at for calendar_id, at in self.fetches if calendar_id == reason],
                    [0],
                )
        self.assertEqual(
            [at for calendar_id, at in self.fetches if calendar_id == CALENDAR],
            [0, 600],
        )
        self.assertEqual(self.deliveries, [("healthy", 0)])

    async def test_permanently_disabled_calendar_is_retried_on_next_main_call(self):
        async def missing_calendar(calendar_id):
            raise http_error(404, "notFound")

        self.fetch_effect = missing_calendar

        await self.run_main(601)

        self.assertEqual(self.fetches, [(CALENDAR, 0)])
        self.fetch_effect = None
        self.events[CALENDAR] = [event("restored", 601)]

        await self.run_main(1)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 601)])
        self.assertEqual(self.deliveries, [("restored", 601)])

    async def test_quota_and_rate_limit_403_errors_are_retried(self):
        reasons = ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded")
        self.set_calendars(*reasons)
        failed = set()

        async def quota_then_success(calendar_id):
            if calendar_id not in failed:
                failed.add(calendar_id)
                raise http_error(403, calendar_id)
            return [event(calendar_id, 5)]

        self.fetch_effect = quota_then_success

        await self.run_main(6)

        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertEqual(
                    [at for calendar_id, at in self.fetches if calendar_id == reason],
                    [0, 5],
                )
        self.assertCountEqual(self.deliveries, [(reason, 5) for reason in reasons])

    async def test_403_with_legacy_and_structured_rate_limit_reasons_is_retried(self):
        failure = http_error(
            403,
            "rateLimitExceeded",
            details=[
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "RATE_LIMIT_EXCEEDED",
                    "domain": "googleapis.com",
                }
            ],
        )

        async def rate_limit_once(calendar_id):
            if len(self.fetches) == 1:
                raise failure
            return [event("recovered", 5)]

        self.fetch_effect = rate_limit_once

        await self.run_main(6)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 5)])
        self.assertEqual(self.deliveries, [("recovered", 5)])

    async def test_unknown_403_reason_retries_with_exponential_backoff(self):
        async def unknown_failure_twice(calendar_id):
            if len(self.fetches) <= 2:
                raise http_error(403, "unknownTransientReason")
            return [event("recovered", 15)]

        self.fetch_effect = unknown_failure_twice

        await self.run_main(16)

        self.assertEqual(self.fetches, [(CALENDAR, 0), (CALENDAR, 5), (CALENDAR, 15)])
        self.assertEqual(self.deliveries, [("recovered", 15)])

    async def test_http_429_and_503_fetch_errors_are_retried(self):
        errors = {
            "http-429": http_error(429, "rateLimitExceeded"),
            "http-503": http_error(503, "backendError"),
        }
        self.set_calendars(*errors)
        failed = set()

        async def fail_once(calendar_id):
            if calendar_id not in failed:
                failed.add(calendar_id)
                raise errors[calendar_id]
            return [event(calendar_id, 5)]

        self.fetch_effect = fail_once

        await self.run_main(6)

        for calendar_id in errors:
            with self.subTest(calendar_id=calendar_id):
                self.assertEqual(
                    [at for name, at in self.fetches if name == calendar_id], [0, 5]
                )
        self.assertCountEqual(self.deliveries, [(name, 5) for name in errors])

    async def test_supervisor_preserves_same_successful_set_across_internal_restart(
        self,
    ):
        seen_sets = []
        seen_contents = []
        successful_key = self.sent_key("successful-before-crash", 0)

        async def main_once(sent_notifications=None):
            seen_sets.append(sent_notifications)
            seen_contents.append(
                None if sent_notifications is None else set(sent_notifications)
            )
            if len(seen_sets) == 1:
                if sent_notifications is not None:
                    sent_notifications.add(successful_key)
                raise RuntimeError("Force an internal restart")

        replacement = AsyncMock(side_effect=main_once)
        with patch.object(app, "main", replacement):
            await app.run_notifier()

        self.assertEqual(replacement.await_count, 2)
        self.assertIsInstance(seen_sets[0], set)
        self.assertIs(seen_sets[0], seen_sets[1])
        self.assertEqual(seen_contents, [set(), {successful_key}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
