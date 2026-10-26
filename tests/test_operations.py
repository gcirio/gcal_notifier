import asyncio
import io
import logging
import runpy
import unittest
from datetime import datetime, timedelta, timezone
from inspect import signature
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, mock_open, patch

with (
    patch("desktop_notifier.DesktopNotifier"),
    patch("logging.basicConfig") as import_logging,
    patch(
        "logging.FileHandler._open",
        side_effect=AssertionError("Import must not open a log file"),
    ),
):
    import main as app

for configured_call in import_logging.call_args_list:
    for configured_handler in configured_call.kwargs.get("handlers", []):
        configured_handler.close()


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


class StopMain(BaseException):
    """Stop a controlled main loop without triggering operational recovery."""


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.log_file = self.directory / "operations.log"
        self.root = logging.getLogger()
        self.original_handlers = self.root.handlers[:]
        self.addCleanup(self.restore_handlers)

    def restore_handlers(self):
        for handler in self.root.handlers[:]:
            if handler not in self.original_handlers:
                self.root.removeHandler(handler)
                handler.close()
        self.root.handlers[:] = self.original_handlers

    def capture_handlers(self, basic_config):
        handlers = []
        for configured_call in basic_config.call_args_list:
            handlers.extend(configured_call.kwargs.get("handlers", []))
        for handler in handlers:
            self.addCleanup(handler.close)
        return handlers

    def configure(self):
        self.assertTrue(
            callable(getattr(app, "configure_logging", None)),
            "main.configure_logging() is required",
        )
        with (
            patch.object(app, "LOG_FILE", self.log_file),
            patch("logging.basicConfig") as basic_config,
        ):
            app.configure_logging()
        handlers = self.capture_handlers(basic_config)
        basic_config.assert_called_once()
        self.assertEqual(len(handlers), 1)
        self.assertEqual(self.root.handlers, self.original_handlers)
        return handlers[0], basic_config.call_args.kwargs

    def test_import_configures_delayed_logging_at_app_path_independent_of_cwd(self):
        expected_directory = Path(app.__file__).parent.resolve()
        with (
            patch("os.getcwd", return_value=str(self.directory)),
            patch("desktop_notifier.DesktopNotifier"),
            patch("logging.basicConfig") as basic_config,
            patch(
                "logging.FileHandler._open",
                side_effect=AssertionError("Import must not open a log file"),
            ),
        ):
            namespace = runpy.run_path(app.__file__, run_name="operations_import_test")
        handlers = self.capture_handlers(basic_config)

        self.assertEqual(namespace.get("APP_DIR"), expected_directory)
        self.assertEqual(
            namespace.get("LOG_FILE"), expected_directory / "gcal_notifier.log"
        )
        self.assertEqual(app.APP_DIR, expected_directory)
        self.assertEqual(app.LOG_FILE, expected_directory / "gcal_notifier.log")
        basic_config.assert_called_once()
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].baseFilename, str(app.LOG_FILE))
        self.assertTrue(handlers[0].delay)
        self.assertIsNone(handlers[0].stream)
        self.assertEqual(self.root.handlers, self.original_handlers)

    def test_configuration_uses_bounded_utf8_append_handler_and_existing_format(self):
        handler, options = self.configure()

        self.assertIsInstance(handler, RotatingFileHandler)
        self.assertEqual(handler.baseFilename, str(self.log_file))
        self.assertEqual(handler.maxBytes, 5 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)
        self.assertEqual(handler.encoding, "utf-8")
        self.assertEqual(handler.mode, "a")
        self.assertTrue(handler.delay)
        self.assertIsNone(handler.stream)
        self.assertFalse(self.log_file.exists())
        self.assertEqual(options["level"], logging.INFO)
        self.assertEqual(options["format"], "%(asctime)s %(levelname)s: %(message)s")
        self.assertNotIn("filename", options)

    def test_utf8_records_append_to_existing_log_across_reconfiguration(self):
        self.log_file.write_text("existing café\n", encoding="utf-8")
        for message in ("meeting 東京", "reminder ☕"):
            handler, _ = self.configure()
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.emit(logging.makeLogRecord({"msg": message}))
            handler.close()

        self.assertEqual(
            self.log_file.read_text(encoding="utf-8"),
            "existing café\nmeeting 東京\nreminder ☕\n",
        )

    def test_rotation_keeps_only_three_backups_and_the_newest_records(self):
        handler, _ = self.configure()
        handler.maxBytes = 80
        handler.setFormatter(logging.Formatter("%(message)s"))
        messages = [f"{index:02d} " + "x" * 50 for index in range(12)]
        for message in messages:
            handler.emit(logging.makeLogRecord({"msg": message}))
        handler.close()

        files = [self.log_file] + [
            self.directory / f"operations.log.{index}" for index in range(1, 4)
        ]
        self.assertEqual(set(self.directory.iterdir()), set(files))
        for index, path in enumerate(files):
            with self.subTest(path=path.name):
                self.assertEqual(
                    path.read_text(encoding="utf-8"), messages[-1 - index] + "\n"
                )
                self.assertLessEqual(path.stat().st_size, handler.maxBytes)


class CalendarIdsTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            callable(getattr(app, "read_calendar_ids", None)),
            "main.read_calendar_ids() must be available at module scope",
        )
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(app, "APP_DIR", self.directory))
        self.enterContext(patch.object(app, "logging", Mock()))
        self.calendar_file = self.directory / app.CALENDAR_IDS_FILE

    def test_strips_blank_lines_and_deduplicates_utf8_ids_in_input_order(self):
        self.calendar_file.write_bytes(
            " \r\n calendar-b \r\n\tcalendar-a\t\n\ncalendar-b\n"
            " calendrier-été \n 東京\n calendar-a\n".encode("utf-8")
        )

        self.assertEqual(
            app.read_calendar_ids(),
            ["calendar-b", "calendar-a", "calendrier-été", "東京"],
        )

    def test_reads_app_relative_file_with_explicit_utf8_encoding(self):
        open_signature = signature(io.open)
        opened = mock_open(read_data="calendar-for-test\n")
        with patch("builtins.open", opened), patch.object(io, "open", opened):
            self.assertEqual(app.read_calendar_ids(), ["calendar-for-test"])

        opened.assert_called_once()
        arguments = open_signature.bind(
            *opened.call_args.args, **opened.call_args.kwargs
        ).arguments
        self.assertEqual(Path(arguments["file"]), self.calendar_file)
        self.assertEqual(arguments.get("encoding"), "utf-8")

    def test_missing_file_is_a_configuration_error(self):
        with self.assertRaises(app.CalendarConfigurationError):
            app.read_calendar_ids()

    def test_unreadable_file_is_a_configuration_error(self):
        for error in (
            PermissionError("test permission failure"),
            OSError("test I/O failure"),
        ):
            with self.subTest(error=type(error).__name__):
                opened = mock_open()
                opened.side_effect = error
                with (
                    patch("builtins.open", opened),
                    patch.object(io, "open", opened),
                    self.assertRaises(app.CalendarConfigurationError),
                ):
                    app.read_calendar_ids()

    def test_invalid_utf8_is_a_configuration_error(self):
        self.calendar_file.write_bytes(b"calendar-for-test\n\xff\n")

        with self.assertRaises(app.CalendarConfigurationError):
            app.read_calendar_ids()

    def test_empty_and_whitespace_only_files_are_configuration_errors(self):
        for contents in ("", " \t\r\n\n\t \n"):
            with self.subTest(contents=contents):
                self.calendar_file.write_text(contents, encoding="utf-8")
                with self.assertRaises(app.CalendarConfigurationError):
                    app.read_calendar_ids()


class PruneSentNotificationsTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            callable(getattr(app, "prune_sent_notifications", None)),
            "main.prune_sent_notifications(shared_set, now) is required",
        )
        self.cutoff = NOW - timedelta(seconds=app.UPDATE_INTERVAL)

    def test_prunes_expired_and_exact_boundary_in_place_but_retains_eligible_and_future(
        self,
    ):
        expired = ("calendar-a", "event", self.cutoff - timedelta(microseconds=1))
        boundary = ("calendar-a", "event", self.cutoff)
        eligible = ("calendar-a", "event", self.cutoff + timedelta(microseconds=1))
        current = ("calendar-b", "event", NOW)
        future = ("calendar-a", "future-event", NOW + timedelta(days=1))
        shared = {expired, boundary, eligible, current, future}
        alias = shared

        app.prune_sent_notifications(shared, NOW)

        self.assertIs(shared, alias)
        self.assertEqual(alias, {eligible, current, future})

    def test_compares_aware_times_by_instant_across_utc_offsets(self):
        offset = timezone(timedelta(hours=5, minutes=30))
        boundary = ("calendar-a", "boundary", self.cutoff.astimezone(offset))
        eligible = (
            "calendar-a",
            "eligible",
            (self.cutoff + timedelta(microseconds=1)).astimezone(offset),
        )
        shared = {boundary, eligible}

        app.prune_sent_notifications(
            shared, NOW.astimezone(timezone(timedelta(hours=-4)))
        )

        self.assertEqual(shared, {eligible})

    def test_repeated_pruning_preserves_eligible_key_until_its_exact_expiry(self):
        key = ("calendar-a", "event", NOW)
        shared = {key}
        app.prune_sent_notifications(shared, NOW)
        app.prune_sent_notifications(
            shared,
            NOW + timedelta(seconds=app.UPDATE_INTERVAL) - timedelta(microseconds=1),
        )
        self.assertEqual(shared, {key})

        app.prune_sent_notifications(
            shared, NOW + timedelta(seconds=app.UPDATE_INTERVAL)
        )
        self.assertEqual(shared, set())
        app.prune_sent_notifications(shared, NOW + timedelta(days=1))
        self.assertEqual(shared, set())


class MainOperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue(
            callable(getattr(app, "read_calendar_accounts", None)),
            "main.read_calendar_accounts() must be available at module scope",
        )
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(app, "APP_DIR", self.directory))
        self.enterContext(
            patch.object(
                app, "open", mock_open(read_data="calendar-for-test\n"), create=True
            )
        )
        self.reader = self.enterContext(
            patch.object(
                app, "read_calendar_accounts", return_value={"calendar-for-test": None}
            )
        )
        self.now = NOW
        clock = Mock(wraps=datetime)
        clock.now.side_effect = lambda tz=None: self.now.astimezone(tz)
        self.client = SimpleNamespace(close=AsyncMock())
        self.client_factory = Mock(return_value=self.client)
        self.authenticate = AsyncMock(return_value=self.client)
        self.fetch = AsyncMock(return_value=[])

        async def send(**kwargs):
            kwargs["on_dispatched"]()
            return "test-notification"

        self.notifier = SimpleNamespace(
            get_capabilities=AsyncMock(
                return_value=frozenset({app.Capability.ON_DISPATCHED})
            ),
            send=AsyncMock(side_effect=send),
            clear=AsyncMock(),
        )
        for name, value in (
            ("datetime", clock),
            ("GoogleCalendarClient", self.client_factory),
            ("authenticate_google_calendar", self.authenticate),
            ("get_upcoming_events", self.fetch),
            ("notifier", self.notifier),
            ("logging", Mock()),
            ("wait_for_work", AsyncMock(side_effect=StopMain)),
        ):
            self.enterContext(patch.object(app, name, value))

    async def test_startup_configuration_failure_precedes_client_creation_and_authentication(
        self,
    ):
        error = app.CalendarConfigurationError(
            "Synthetic invalid calendar configuration"
        )
        self.reader.side_effect = error

        with self.assertRaises(app.CalendarConfigurationError) as raised:
            await asyncio.wait_for(app.main(), 0.5)

        self.assertIs(raised.exception, error)
        self.reader.assert_called_once_with()
        self.client_factory.assert_not_called()
        self.authenticate.assert_not_called()
        self.fetch.assert_not_called()
        self.notifier.send.assert_not_called()

    async def test_main_prunes_the_supplied_set_on_repeated_loop_iterations(self):
        self.assertTrue(callable(getattr(app, "prune_sent_notifications", None)))
        shared = {("calendar-for-test", "expired", NOW - timedelta(days=1))}
        checks = []
        with patch.object(
            app, "prune_sent_notifications", wraps=app.prune_sent_notifications
        ) as prune:

            async def wait(tasks, seconds):
                self.assertGreater(prune.call_count, checks[-1] if checks else 0)
                self.assertIs(prune.call_args.args[0], shared)
                self.assertEqual(prune.call_args.args[1], self.now)
                self.assertEqual(shared, set())
                checks.append(prune.call_count)
                if len(checks) == 3:
                    raise StopMain
                self.now += timedelta(seconds=1)
                await asyncio.sleep(0)

            with patch.object(app, "wait_for_work", wait), self.assertRaises(StopMain):
                await asyncio.wait_for(app.main(shared), 0.5)

        self.assertEqual(len(checks), 3)
        self.client.close.assert_awaited_once_with()

    async def test_pruning_preserves_successful_key_and_prevents_duplicate_on_later_main_run(
        self,
    ):
        self.assertTrue(callable(getattr(app, "prune_sent_notifications", None)))
        successful_key = ("calendar-for-test", "test-event", NOW)
        shared = {("calendar-for-test", "expired-event", NOW - timedelta(days=1))}
        alias = shared
        self.fetch.return_value = [
            {
                "id": "test-event",
                "summary": "Synthetic meeting",
                "start": {"dateTime": NOW.isoformat()},
            }
        ]
        target_fetches = 1
        waits = 0

        async def wait(tasks, seconds):
            nonlocal waits
            waits += 1
            if waits > 30:
                self.fail("Main did not finish a controlled fetch/evaluation cycle")
            if self.fetch.await_count >= target_fetches and not tasks:
                raise StopMain
            await asyncio.sleep(0)

        with patch.object(app, "wait_for_work", wait):
            for run in range(2):
                with self.subTest(run=run), self.assertRaises(StopMain):
                    await asyncio.wait_for(app.main(shared), 0.5)
                self.assertIs(shared, alias)
                self.assertEqual(alias, {successful_key})
                self.now += timedelta(seconds=app.RESTART_TIME)
                target_fetches += 1

        self.assertEqual(self.fetch.await_count, 2)
        self.notifier.send.assert_awaited_once()
        self.assertEqual(
            self.notifier.send.await_args.kwargs["title"], "Synthetic meeting"
        )
        self.assertEqual(self.client.close.await_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
