import asyncio
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, mock_open, patch

from desktop_notifier import Capability

from google_calendar import AuthorizationRequired, CalendarConfigurationError

with patch("desktop_notifier.DesktopNotifier"), patch("logging.basicConfig"):
    import main as app


class AuthenticationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prompts = []
        self.notifier = SimpleNamespace(
            get_capabilities=AsyncMock(
                return_value=frozenset(
                    {
                        Capability.ON_DISPATCHED,
                        Capability.ON_CLICKED,
                        Capability.BUTTONS,
                    }
                )
            ),
            send=AsyncMock(side_effect=self.send),
            clear=AsyncMock(),
        )
        self.logging = Mock()
        self.enterContext(patch.object(app, "notifier", self.notifier))
        self.enterContext(patch.object(app, "logging", self.logging))

    async def send(self, **kwargs):
        self.prompts.append(kwargs)
        on_dispatched = kwargs.get("on_dispatched")
        if on_dispatched is not None:
            on_dispatched()
        return f"authentication-{len(self.prompts)}"

    def client(self, *outcomes):
        authenticate = AsyncMock(return_value=object())
        if outcomes:
            authenticate.side_effect = outcomes
        return SimpleNamespace(authenticate=authenticate, close=AsyncMock())

    def start(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.addAsyncCleanup(self.cancel_task, task)
        return task

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 0.5)

    async def until(self, predicate, task=None):
        async def poll():
            while not predicate():
                if task is not None and task.done():
                    task.result()
                    self.fail("Task finished before the expected event")
                await asyncio.sleep(0)

        await asyncio.wait_for(poll(), 0.5)

    def replace_sleep(self, sleep):
        controlled_asyncio = SimpleNamespace(**vars(asyncio))
        controlled_asyncio.sleep = sleep
        self.enterContext(patch.object(app, "asyncio", controlled_asyncio))

    def prompt_callback(self, index=0, *, button=False):
        prompt = self.prompts[index]
        self.assertEqual(prompt["title"], "Google Calendar sign-in required")
        self.assertIn("click", prompt["message"].lower())
        self.assertIn("authenticat", prompt["message"].lower())
        self.assertEqual(prompt["timeout"], 0)
        self.assertTrue(callable(prompt["on_dispatched"]))
        capabilities = self.notifier.get_capabilities.return_value
        buttons = prompt.get("buttons")
        on_clicked = prompt.get("on_clicked")
        if Capability.BUTTONS in capabilities:
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0].title, "Authenticate")
            self.assertTrue(callable(buttons[0].on_pressed))
        else:
            self.assertFalse(buttons)
        if Capability.ON_CLICKED in capabilities:
            self.assertTrue(callable(on_clicked))
        else:
            self.assertIsNone(on_clicked)
        if {Capability.BUTTONS, Capability.ON_CLICKED} <= capabilities:
            self.assertIs(buttons[0].on_pressed, on_clicked)
        return buttons[0].on_pressed if button else on_clicked


class AuthenticationTests(AuthenticationTestCase):
    async def test_valid_cached_credentials_return_client_without_prompt(self):
        client = self.client()

        result = await asyncio.wait_for(app.authenticate_google_calendar(client), 0.5)

        self.assertIs(result, client)
        client.authenticate.assert_awaited_once_with()
        self.notifier.send.assert_not_awaited()
        self.notifier.clear.assert_not_awaited()

    async def authenticate_by_click(self, *, button):
        client = self.client(AuthorizationRequired("Sign in"), object())
        loop = asyncio.get_running_loop()

        with patch.object(
            loop, "call_soon_threadsafe", wraps=loop.call_soon_threadsafe
        ) as schedule:
            task = self.start(app.authenticate_google_calendar(client))
            await self.until(lambda: len(self.prompts) == 1, task)
            await asyncio.sleep(0)
            client.authenticate.assert_awaited_once_with()
            self.assertFalse(task.done())
            callback = self.prompt_callback(button=button)
            schedule.assert_called_once()
            dispatch_set = schedule.call_args.args[0]
            self.assertIsInstance(dispatch_set.__self__, asyncio.Event)
            self.assertEqual(dispatch_set.__name__, "set")
            schedule.reset_mock()
            callback()
            schedule.assert_called_once()
            event_set = schedule.call_args.args[0]
            self.assertIsInstance(event_set.__self__, asyncio.Event)
            self.assertEqual(event_set.__name__, "set")
            await asyncio.sleep(0)

        self.assertIs(await asyncio.wait_for(task, 0.5), client)
        self.assertEqual(
            client.authenticate.await_args_list, [call(), call(interactive=True)]
        )
        self.notifier.send.assert_awaited_once()
        self.notifier.clear.assert_any_await("authentication-1")

    async def test_notification_button_is_required_before_interactive_authentication(
        self,
    ):
        await self.authenticate_by_click(button=True)

    async def test_notification_body_is_required_before_interactive_authentication(
        self,
    ):
        await self.authenticate_by_click(button=False)

    async def test_button_only_backend_can_authenticate(self):
        self.notifier.get_capabilities.return_value = frozenset(
            {Capability.ON_DISPATCHED, Capability.BUTTONS}
        )
        await self.authenticate_by_click(button=True)

    async def test_body_only_backend_can_authenticate(self):
        self.notifier.get_capabilities.return_value = frozenset(
            {Capability.ON_DISPATCHED, Capability.ON_CLICKED}
        )
        await self.authenticate_by_click(button=False)

    async def unsupported_capabilities_are_fatal(self, capabilities):
        self.notifier.get_capabilities.return_value = frozenset(capabilities)
        client = self.client(AuthorizationRequired("Sign in"))

        with self.assertRaises(app.AuthenticationNotificationError):
            await asyncio.wait_for(app.authenticate_google_calendar(client), 0.5)

        self.notifier.get_capabilities.assert_awaited_once_with()
        client.authenticate.assert_awaited_once_with()
        self.notifier.send.assert_not_awaited()
        self.notifier.clear.assert_not_awaited()

    async def test_no_interaction_capabilities_is_fatal_without_browser_launch(self):
        await self.unsupported_capabilities_are_fatal({Capability.ON_DISPATCHED})

    async def test_no_dispatch_capability_is_fatal_without_browser_launch(self):
        await self.unsupported_capabilities_are_fatal(
            {Capability.ON_CLICKED, Capability.BUTTONS}
        )

    async def test_repeated_clicks_do_not_overlap_or_queue_interactive_flows(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak_active = 0

        async def authenticate(interactive=False):
            nonlocal active, peak_active
            if not interactive:
                raise AuthorizationRequired("Sign in")
            active += 1
            peak_active = max(peak_active, active)
            entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

        client = self.client()
        client.authenticate.side_effect = authenticate
        task = self.start(app.authenticate_google_calendar(client))
        await self.until(lambda: len(self.prompts) == 1, task)
        callback = self.prompt_callback()
        callback()
        callback()
        await asyncio.wait_for(entered.wait(), 0.5)
        self.notifier.clear.assert_any_await("authentication-1")
        callback()
        callback()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(client.authenticate.await_count, 2)
        self.assertEqual(peak_active, 1)
        self.assertEqual(active, 1)
        self.assertFalse(task.done())
        self.assertEqual(len(self.prompts), 1)
        release.set()
        self.assertIs(await asyncio.wait_for(task, 0.5), client)
        await asyncio.sleep(0)
        self.assertEqual(client.authenticate.await_count, 2)
        self.assertEqual(len(self.prompts), 1)

    async def retry_requires_another_click(self, error):
        client = self.client(AuthorizationRequired("Sign in"), error, object())
        task = self.start(app.authenticate_google_calendar(client))
        await self.until(lambda: len(self.prompts) == 1, task)
        self.prompt_callback(button=True)()
        await self.until(lambda: len(self.prompts) == 2, task)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertFalse(task.done())
        self.assertEqual(
            client.authenticate.await_args_list, [call(), call(interactive=True)]
        )
        self.notifier.clear.assert_any_await("authentication-1")
        self.prompt_callback(1)()
        self.assertIs(await asyncio.wait_for(task, 0.5), client)
        self.assertEqual(
            client.authenticate.await_args_list,
            [call(), call(interactive=True), call(interactive=True)],
        )
        self.assertEqual(len(self.prompts), 2)
        self.notifier.clear.assert_any_await("authentication-2")

    async def test_interactive_failure_waits_for_fresh_notification_click(self):
        await self.retry_requires_another_click(RuntimeError("Browser sign-in failed"))

    async def test_interactive_authorization_required_waits_for_fresh_click(self):
        await self.retry_requires_another_click(AuthorizationRequired("Try again"))

    async def test_interactive_timeout_waits_for_fresh_notification_click(self):
        await self.retry_requires_another_click(TimeoutError("Sign-in timed out"))

    async def test_cancellation_while_waiting_clears_prompt_and_propagates(self):
        client = self.client(AuthorizationRequired("Sign in"))
        task = self.start(app.authenticate_google_calendar(client))
        await self.until(lambda: len(self.prompts) == 1, task)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)

        client.authenticate.assert_awaited_once_with()
        self.notifier.send.assert_awaited_once()
        self.notifier.clear.assert_any_await("authentication-1")

    async def test_cancellation_during_interactive_authentication_does_not_reprompt(
        self,
    ):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def authenticate(interactive=False):
            if not interactive:
                raise AuthorizationRequired("Sign in")
            entered.set()
            await release.wait()

        client = self.client()
        client.authenticate.side_effect = authenticate
        task = self.start(app.authenticate_google_calendar(client))
        await self.until(lambda: len(self.prompts) == 1, task)
        self.prompt_callback()()
        await asyncio.wait_for(entered.wait(), 0.5)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)

        self.assertEqual(client.authenticate.await_count, 2)
        self.notifier.send.assert_awaited_once()
        self.notifier.clear.assert_any_await("authentication-1")

    async def test_silent_configuration_error_propagates_without_prompt(self):
        error = CalendarConfigurationError("Invalid OAuth configuration")
        client = self.client(error)

        with self.assertRaises(CalendarConfigurationError) as raised:
            await asyncio.wait_for(app.authenticate_google_calendar(client), 0.5)

        self.assertIs(raised.exception, error)
        client.authenticate.assert_awaited_once_with()
        self.notifier.send.assert_not_awaited()

    async def test_interactive_configuration_error_propagates_without_reprompt(self):
        error = CalendarConfigurationError("Cannot persist credentials")
        client = self.client(AuthorizationRequired("Sign in"), error)
        task = self.start(app.authenticate_google_calendar(client))
        await self.until(lambda: len(self.prompts) == 1, task)
        self.prompt_callback()()

        with self.assertRaises(CalendarConfigurationError) as raised:
            await asyncio.wait_for(task, 0.5)

        self.assertIs(raised.exception, error)
        self.assertEqual(client.authenticate.await_count, 2)
        self.notifier.send.assert_awaited_once()
        self.notifier.clear.assert_any_await("authentication-1")

    async def test_notification_send_error_is_fatal_without_interactive_authentication(
        self,
    ):
        client = self.client(AuthorizationRequired("Sign in"))
        self.notifier.send.side_effect = OSError("Notification service unavailable")

        with self.assertRaises(app.AuthenticationNotificationError) as raised:
            await asyncio.wait_for(app.authenticate_google_calendar(client), 0.5)

        self.assertTrue(str(raised.exception))
        client.authenticate.assert_awaited_once_with()
        self.notifier.send.assert_awaited_once()
        self.notifier.clear.assert_not_awaited()

    async def test_empty_notification_identifier_is_fatal(self):
        for identifier in (None, ""):
            with self.subTest(identifier=identifier):
                client = self.client(AuthorizationRequired("Sign in"))
                self.notifier.send.reset_mock()
                self.notifier.send.side_effect = None
                self.notifier.send.return_value = identifier

                with self.assertRaises(app.AuthenticationNotificationError) as raised:
                    await asyncio.wait_for(
                        app.authenticate_google_calendar(client), 0.5
                    )

                self.assertTrue(str(raised.exception))
                client.authenticate.assert_awaited_once_with()
                self.notifier.send.assert_awaited_once()
                self.notifier.clear.assert_not_awaited()

    async def test_truthy_identifier_without_dispatch_acknowledgement_is_fatal(self):
        self.enterContext(patch.object(app, "NOTIFICATION_DISPATCH_TIMEOUT", 0.01))
        self.notifier.send.side_effect = None
        self.notifier.send.return_value = "undispatched-authentication"
        client = self.client(AuthorizationRequired("Sign in"))

        with self.assertRaises(app.AuthenticationNotificationError):
            await asyncio.wait_for(app.authenticate_google_calendar(client), 0.5)

        client.authenticate.assert_awaited_once_with()
        self.notifier.send.assert_awaited_once()
        self.assertTrue(callable(self.notifier.send.await_args.kwargs["on_dispatched"]))
        self.notifier.clear.assert_any_await("undispatched-authentication")

    async def test_cleanup_failure_does_not_undo_successful_authentication(self):
        client = self.client(AuthorizationRequired("Sign in"), object())
        self.notifier.clear.side_effect = OSError("Notification already gone")
        task = self.start(app.authenticate_google_calendar(client))
        await self.until(lambda: len(self.prompts) == 1, task)
        self.prompt_callback()()

        self.assertIs(await asyncio.wait_for(task, 0.5), client)
        self.notifier.clear.assert_any_await("authentication-1")
        self.notifier.send.assert_awaited_once()

    async def test_reauthenticate_skips_silent_valid_token_and_waits_for_click(self):
        client = self.client()
        task = self.start(app.authenticate_google_calendar(client, reauthenticate=True))
        await self.until(lambda: len(self.prompts) == 1, task)
        await asyncio.sleep(0)
        client.authenticate.assert_not_awaited()
        self.assertFalse(task.done())

        self.prompt_callback()()

        self.assertIs(await asyncio.wait_for(task, 0.5), client)
        client.authenticate.assert_awaited_once_with(interactive=True)
        self.notifier.send.assert_awaited_once()
        self.notifier.clear.assert_any_await("authentication-1")

    async def test_silent_transient_errors_back_off_without_prompting(self):
        sleeping = asyncio.Event()
        release = asyncio.Event()
        delays = []

        async def sleep(seconds):
            delays.append(seconds)
            sleeping.set()
            await release.wait()
            await asyncio.sleep(0)

        self.replace_sleep(AsyncMock(side_effect=sleep))
        client = self.client(
            ConnectionError("Offline"), TimeoutError("Slow refresh"), object()
        )
        task = self.start(app.authenticate_google_calendar(client))
        await asyncio.wait_for(sleeping.wait(), 0.5)
        client.authenticate.assert_awaited_once_with()
        self.notifier.send.assert_not_awaited()
        self.assertFalse(task.done())

        release.set()

        self.assertIs(await asyncio.wait_for(task, 0.5), client)
        self.assertEqual(client.authenticate.await_args_list, [call(), call(), call()])
        self.assertEqual(len(delays), 2)
        self.assertGreater(delays[0], 0)
        self.assertGreaterEqual(delays[1], delays[0])
        self.notifier.send.assert_not_awaited()


class ClearNotificationTests(AuthenticationTestCase):
    async def test_ordinary_cleanup_errors_are_best_effort(self):
        for error in (
            KeyError("Gone"),
            OSError("Desktop unavailable"),
            RuntimeError("Gone"),
        ):
            with self.subTest(error=type(error).__name__):
                self.notifier.clear.reset_mock()
                self.notifier.clear.side_effect = error

                await asyncio.wait_for(app.clear_notification("authentication-1"), 0.5)

                self.notifier.clear.assert_awaited_once_with("authentication-1")

    async def test_cleanup_cancellation_propagates(self):
        self.notifier.clear.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(app.clear_notification("authentication-1"), 0.5)

        self.notifier.clear.assert_awaited_once_with("authentication-1")

    async def test_empty_identifier_does_not_call_desktop_cleanup(self):
        await asyncio.wait_for(app.clear_notification(""), 0.5)

        self.notifier.clear.assert_not_awaited()


class SupervisorTests(AuthenticationTestCase):
    def replace_main(self, *outcomes):
        main = AsyncMock(side_effect=outcomes)
        self.enterContext(patch.object(app, "main", main))
        return main

    async def test_cancellation_propagates_without_restart_notification(self):
        main = self.replace_main(asyncio.CancelledError(), None)
        sleep = AsyncMock(side_effect=asyncio.CancelledError())
        self.replace_sleep(sleep)

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(app.run_notifier(), 0.5)

        main.assert_awaited_once()
        self.notifier.send.assert_not_awaited()
        sleep.assert_not_awaited()

    async def fatal_error_returns_one(self, error):
        main = self.replace_main(error, None)
        sleep = AsyncMock(side_effect=lambda seconds: None)
        self.replace_sleep(sleep)

        self.assertEqual(await asyncio.wait_for(app.run_notifier(), 0.5), 1)

        main.assert_awaited_once()
        self.notifier.send.assert_not_awaited()
        sleep.assert_not_awaited()
        self.assertTrue(
            self.logging.error.called
            or self.logging.exception.called
            or self.logging.critical.called
        )

    async def test_configuration_error_is_logged_and_returns_one_without_restart(self):
        await self.fatal_error_returns_one(
            CalendarConfigurationError("Bad configuration")
        )

    async def test_authentication_notification_error_returns_one_without_restart(self):
        await self.fatal_error_returns_one(
            app.AuthenticationNotificationError("Cannot show sign-in prompt")
        )

    async def test_initial_authentication_notification_failure_returns_one_without_restart(
        self,
    ):
        client = self.client(AuthorizationRequired("Sign in"))
        factory = self.enterContext(
            patch.object(app, "GoogleCalendarClient", return_value=client)
        )
        self.enterContext(
            patch.object(
                app, "open", mock_open(read_data="calendar-for-test\n"), create=True
            )
        )
        self.notifier.send.side_effect = OSError("Notification service unavailable")

        self.assertEqual(await asyncio.wait_for(app.run_notifier(), 0.5), 1)

        factory.assert_called_once_with()
        client.authenticate.assert_awaited_once_with()
        client.close.assert_awaited_once_with()
        self.notifier.send.assert_awaited_once()
        self.assertEqual(
            self.notifier.send.await_args.kwargs["title"],
            "Google Calendar sign-in required",
        )
        self.logging.exception.assert_called()

    async def test_repeated_cancellation_during_main_cleanup_still_closes_client(self):
        started = asyncio.Event()
        draining = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def authenticate(client):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.set()
                await release_cleanup.wait()

        client = self.client()
        self.enterContext(
            patch.object(app, "GoogleCalendarClient", return_value=client)
        )
        self.enterContext(
            patch.object(
                app, "open", mock_open(read_data="calendar-for-test\n"), create=True
            )
        )
        authentication = self.enterContext(
            patch.object(
                app, "authenticate_google_calendar", AsyncMock(side_effect=authenticate)
            )
        )
        task = self.start(app.main())
        await asyncio.wait_for(started.wait(), 0.5)

        task.cancel()
        await asyncio.wait_for(draining.wait(), 0.5)
        self.assertFalse(task.done())
        client.close.assert_not_awaited()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)

        authentication.assert_awaited_once_with(client)
        client.close.assert_awaited_once_with()
        self.notifier.send.assert_not_awaited()

    async def test_recovery_notification_cleanup_failure_does_not_crash_supervisor(
        self,
    ):
        main = self.replace_main(RuntimeError("Recoverable main failure"), None)
        self.notifier.clear.side_effect = OSError("Notification service disappeared")
        sleep = AsyncMock(side_effect=lambda seconds: None)
        self.replace_sleep(sleep)

        await asyncio.wait_for(app.run_notifier(), 0.5)

        self.assertEqual(main.await_count, 2)
        self.assertEqual(self.notifier.send.await_count, 2)
        self.notifier.clear.assert_any_await("authentication-1")
        sleep.assert_awaited()

    async def test_script_entry_exits_with_supervisor_return_code(self):
        def run_without_executing(coroutine):
            coroutine.close()
            return 1

        with (
            patch("desktop_notifier.DesktopNotifier", return_value=self.notifier),
            patch("logging.basicConfig"),
            patch("asyncio.run", side_effect=run_without_executing) as run,
            patch("sys.exit") as exit_app,
        ):
            runpy.run_path(str(Path(app.__file__)), run_name="__main__")

        run.assert_called_once()
        exit_app.assert_called_once_with(1)
        self.notifier.send.assert_not_awaited()


class WaitForWorkTests(AuthenticationTestCase):
    def controlled_timer(self):
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def timer(seconds):
            self.addAsyncCleanup(self.cancel_task, asyncio.current_task())
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        sleep = AsyncMock(side_effect=timer)
        self.replace_sleep(sleep)
        return sleep, started, release, cancelled

    async def test_no_work_sleeps_for_requested_delay(self):
        sleep, started, release, _ = self.controlled_timer()
        waiting = self.start(app.wait_for_work([], 17))
        await asyncio.wait_for(started.wait(), 0.5)
        self.assertFalse(waiting.done())
        sleep.assert_awaited_once_with(17)

        release.set()

        await asyncio.wait_for(waiting, 0.5)

    async def test_first_completed_work_cancels_only_timer(self):
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        first = self.start(first_release.wait())
        second = self.start(second_release.wait())
        sleep, started, _, cancelled = self.controlled_timer()
        waiting = self.start(app.wait_for_work([first, second], 17))
        await asyncio.wait_for(started.wait(), 0.5)

        first_release.set()
        await asyncio.wait_for(waiting, 0.5)
        await asyncio.wait_for(cancelled.wait(), 0.5)

        sleep.assert_awaited_once_with(17)
        self.assertTrue(first.done())
        self.assertFalse(first.cancelled())
        self.assertFalse(second.done())
        second_release.set()
        self.assertTrue(await asyncio.wait_for(second, 0.5))

    async def test_timer_completion_leaves_work_running(self):
        work_release = asyncio.Event()
        work = self.start(work_release.wait())
        _, started, timer_release, _ = self.controlled_timer()
        waiting = self.start(app.wait_for_work([work], 17))
        await asyncio.wait_for(started.wait(), 0.5)

        timer_release.set()
        await asyncio.wait_for(waiting, 0.5)

        self.assertFalse(work.done())
        work_release.set()
        self.assertTrue(await asyncio.wait_for(work, 0.5))

    async def test_helper_cancellation_cancels_timer_but_not_work(self):
        work_release = asyncio.Event()
        work = self.start(work_release.wait())
        _, started, _, cancelled = self.controlled_timer()
        waiting = self.start(app.wait_for_work([work], 17))
        await asyncio.wait_for(started.wait(), 0.5)

        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, 0.5)
        await asyncio.wait_for(cancelled.wait(), 0.5)

        self.assertFalse(work.done())
        work_release.set()
        self.assertTrue(await asyncio.wait_for(work, 0.5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
