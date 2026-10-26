import asyncio
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlsplit

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp, Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google_auth_oauthlib.helpers import session_from_client_config
from googleapiclient.errors import HttpError

import google_calendar as calendar


class GoogleCalendarClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.credentials_path = self.directory / "credentials.json"
        self.token_path = self.directory / "token.json"
        self.config = {
            "installed": {
                "client_id": "fixture-client",
                "client_secret": "fixture-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        self.credentials_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.write_token()
        self.operations = []
        self.transports = []
        self.services = []
        self.http_factory = self.enterContext(
            patch.object(calendar.httplib2, "Http", side_effect=self.make_http)
        )
        self.build = self.enterContext(
            patch.object(calendar, "build", side_effect=self.make_service)
        )
        self.flow_factory = self.enterContext(
            patch.object(
                calendar.InstalledAppFlow,
                "from_client_config",
                side_effect=AssertionError("OAuth must be explicitly interactive"),
            )
        )
        self.client = calendar.GoogleCalendarClient(
            self.credentials_path, self.token_path
        )
        self.addAsyncCleanup(self.client.close)

    def record(self, operation):
        self.operations.append((operation, threading.get_ident()))

    def make_http(self, **kwargs):
        self.record("http")
        transport = Mock()
        transport.close.side_effect = lambda: self.record("transport.close")
        self.transports.append(transport)
        return transport

    def make_service(self, *args, **kwargs):
        self.record("build")
        service = Mock()

        def close():
            self.record("service.close")
            kwargs["http"].close()

        service.close.side_effect = close
        self.services.append(service)
        return service

    def credentials(self, token="cached-token"):
        return Credentials(
            token=token,
            refresh_token="fixture-refresh",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="fixture-client",
            client_secret="fixture-secret",
            scopes=calendar.SCOPES,
            expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        )

    def write_token(self, **overrides):
        data = json.loads(self.credentials().to_json())
        data.update(overrides)
        self.token_path.write_text(json.dumps(data), encoding="utf-8")

    def refresh(self, credentials, request):
        self.record("refresh")
        self.assertIsInstance(request, Request)
        self.assertIs(request.http, self.transports[-1])
        credentials.token = "refreshed-token"
        credentials.expiry = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) + timedelta(hours=1)

    def interactive_flow(self):
        flow = Mock()
        flow.run_local_server.side_effect = lambda **kwargs: (
            self.record("consent") or self.credentials("interactive-token")
        )
        flow.oauth2session.close.side_effect = lambda: self.record("oauth.close")
        self.flow_factory.side_effect = None
        self.flow_factory.return_value = flow
        return flow

    async def wait_started(self, event):
        async with asyncio.timeout(3):
            while not event.is_set():
                await asyncio.sleep(0.001)

    def assert_single_worker(self):
        identities = {identity for _, identity in self.operations}
        self.assertEqual(len(identities), 1, self.operations)
        self.assertNotIn(threading.get_ident(), identities)

    async def test_constructor_does_not_read_files_or_create_transport(self):
        with patch.object(Path, "open", side_effect=AssertionError("file read")):
            client = calendar.GoogleCalendarClient(
                self.credentials_path, self.token_path
            )
        await client.close()
        self.http_factory.assert_not_called()
        self.build.assert_not_called()
        self.flow_factory.assert_not_called()

    async def test_valid_cache_authenticates_without_browser_or_client_file(self):
        self.credentials_path.unlink()
        before = self.token_path.read_bytes()
        self.assertIs(await self.client.authenticate(), self.client)
        self.flow_factory.assert_not_called()
        self.assertEqual(self.token_path.read_bytes(), before)
        self.http_factory.assert_called_once_with(timeout=30)
        args, kwargs = self.build.call_args
        self.assertEqual(args, ("calendar", "v3"))
        self.assertFalse(kwargs["cache_discovery"])
        self.assertIsInstance(kwargs["http"], AuthorizedHttp)
        self.assertIs(kwargs["http"].http, self.transports[0])
        self.assertIs(kwargs["http"]._request.http, self.transports[0])
        await self.client.close()
        self.transports[0].close.assert_called_once_with()
        self.assert_single_worker()

    async def test_missing_cache_requires_auth_and_ignores_pickle(self):
        self.token_path.unlink()
        legacy = self.directory / "token.pickle"
        legacy.write_bytes(b"not a pickle; never deserialize")
        original_open = Path.open
        opened = []

        def open_fixture(path, *args, **kwargs):
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_fixture):
            with self.assertRaises(calendar.AuthorizationRequired):
                await self.client.authenticate()
        self.assertNotIn(legacy, opened)
        self.assertEqual(legacy.read_bytes(), b"not a pickle; never deserialize")
        self.flow_factory.assert_not_called()
        self.http_factory.assert_not_called()

    async def test_corrupt_or_unusable_cache_requires_auth_without_browser(self):
        cases = [
            "{broken",
            "[]",
            "null",
            "{}",
            json.dumps({"refresh_token": None}),
            json.dumps({**json.loads(self.credentials().to_json()), "scopes": []}),
            json.dumps({**json.loads(self.credentials().to_json()), "scopes": 5}),
            json.dumps(
                {
                    **json.loads(self.credentials().to_json()),
                    "scopes": {calendar.SCOPES[0]: True},
                }
            ),
            json.dumps(
                {
                    **json.loads(self.credentials().to_json()),
                    "scopes": [calendar.SCOPES[0], 5],
                }
            ),
            "[" * 1500 + "]" * 1500,
            json.dumps({**json.loads(self.credentials().to_json()), "token": 5}),
            json.dumps({**json.loads(self.credentials().to_json()), "expiry": 5}),
            json.dumps(
                {**json.loads(self.credentials().to_json()), "refresh_token": ""}
            ),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.token_path.write_text(data, encoding="utf-8")
                with self.assertRaises(calendar.AuthorizationRequired):
                    await self.client.authenticate()
                self.assertEqual(self.token_path.read_text(encoding="utf-8"), data)
        self.flow_factory.assert_not_called()
        self.http_factory.assert_not_called()

    async def test_invalid_utf8_cache_requires_authorization(self):
        original = b"\xff\xfe invalid UTF-8"
        self.token_path.write_bytes(original)
        with self.assertRaises(calendar.AuthorizationRequired):
            await self.client.authenticate()
        self.assertEqual(self.token_path.read_bytes(), original)
        self.flow_factory.assert_not_called()
        self.http_factory.assert_not_called()

    async def test_space_separated_cached_scopes_remain_supported(self):
        self.write_token(scopes=" ".join(calendar.SCOPES))
        self.assertIs(await self.client.authenticate(), self.client)
        self.flow_factory.assert_not_called()

    async def test_configuration_errors_take_precedence_when_auth_needed(self):
        self.token_path.unlink()
        cases = [
            None,
            "broken json",
            "[]",
            "[" * 1500 + "]" * 1500,
            "{}",
            '{"installed": null}',
            '{"installed": {"client_id": "only-id"}}',
        ]
        for key in ("client_id", "client_secret", "auth_uri", "token_uri"):
            config = json.loads(json.dumps(self.config))
            del config["installed"][key]
            cases.append(json.dumps(config))
        bad_uri = json.loads(json.dumps(self.config))
        bad_uri["installed"]["token_uri"] = "not-an-https-endpoint"
        cases.append(json.dumps(bad_uri))
        for data in cases:
            with self.subTest(data=data):
                if data is None:
                    self.credentials_path.unlink(missing_ok=True)
                else:
                    self.credentials_path.write_text(data, encoding="utf-8")
                with self.assertRaises(calendar.CalendarConfigurationError):
                    await self.client.authenticate()
                with self.assertRaises(calendar.CalendarConfigurationError):
                    await self.client.authenticate(interactive=True)
        self.flow_factory.assert_not_called()
        self.http_factory.assert_not_called()

    async def test_unreadable_token_is_configuration_error(self):
        original_open = Path.open

        def open_fixture(path, *args, **kwargs):
            if path == self.token_path:
                raise PermissionError("fixture denied")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_fixture):
            with self.assertRaises(calendar.CalendarConfigurationError):
                await self.client.authenticate()
        self.flow_factory.assert_not_called()

    async def test_expired_cache_refreshes_silently_and_persists(self):
        self.write_token(expiry="2000-01-01T00:00:00Z")
        self.credentials_path.unlink()
        with patch.object(
            Credentials, "refresh", autospec=True, side_effect=self.refresh
        ):
            await self.client.authenticate()
        data = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(data["token"], "refreshed-token")
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        self.flow_factory.assert_not_called()
        self.assert_single_worker()

    async def test_missing_access_token_can_refresh_silently(self):
        self.write_token(token=None)
        with patch.object(
            Credentials, "refresh", autospec=True, side_effect=self.refresh
        ):
            await self.client.authenticate()
        self.flow_factory.assert_not_called()
        self.assertEqual(
            json.loads(self.token_path.read_text(encoding="utf-8"))["token"],
            "refreshed-token",
        )

    async def test_revoked_refresh_requires_auth_and_preserves_cache(self):
        self.write_token(expiry="2000-01-01T00:00:00Z")
        original = self.token_path.read_bytes()
        error = RefreshError("invalid_grant", retryable=False)
        with patch.object(Credentials, "refresh", side_effect=error):
            with self.assertRaises(calendar.AuthorizationRequired):
                await self.client.authenticate()
        self.assertEqual(self.token_path.read_bytes(), original)
        self.transports[-1].close.assert_called_once_with()
        self.flow_factory.assert_not_called()
        self.build.assert_not_called()
        self.credentials_path.unlink()
        with patch.object(Credentials, "refresh", side_effect=error):
            with self.assertRaises(calendar.CalendarConfigurationError):
                await self.client.authenticate()

    async def test_transient_refresh_and_transport_errors_propagate_unchanged(self):
        self.write_token(expiry="2000-01-01T00:00:00Z")
        self.credentials_path.unlink()
        original = self.token_path.read_bytes()
        for error in (
            RefreshError("temporarily unavailable", retryable=True),
            TransportError("connection unavailable"),
            TimeoutError("request timed out"),
        ):
            with self.subTest(error=error):
                with patch.object(Credentials, "refresh", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        await self.client.authenticate()
                self.assertEqual(raised.exception.args, error.args)
                if not isinstance(error, TimeoutError):
                    self.assertIs(raised.exception, error)
                self.assertEqual(self.token_path.read_bytes(), original)
                self.transports[-1].close.assert_called_once_with()
        self.flow_factory.assert_not_called()
        self.build.assert_not_called()

    async def test_refresh_missing_granted_scope_requires_authorization(self):
        self.write_token(expiry="2000-01-01T00:00:00Z")
        original = self.token_path.read_bytes()

        def refresh(credentials, request):
            self.refresh(credentials, request)
            credentials._granted_scopes = ["unrelated-scope"]

        with patch.object(Credentials, "refresh", refresh):
            with self.assertRaises(calendar.AuthorizationRequired):
                await self.client.authenticate()
        self.assertEqual(self.token_path.read_bytes(), original)
        self.build.assert_not_called()

    async def test_interactive_auth_skips_cache_and_persists_atomically(self):
        flow = self.interactive_flow()
        self.token_path.chmod(0o644)
        original_replace = os.replace
        original_fsync = os.fsync
        stages = []

        def fsync(descriptor):
            self.record("fsync")
            stages.append("fsync")
            return original_fsync(descriptor)

        def replace(source, destination):
            self.record("replace")
            self.assertEqual(stages, ["fsync"])
            source = Path(source)
            self.assertEqual(source.parent, self.token_path.parent)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(source.read_text(encoding="utf-8"))["token"],
                "interactive-token",
            )
            self.assertEqual(
                json.loads(self.token_path.read_text(encoding="utf-8"))["token"],
                "cached-token",
            )
            stages.append("replace")
            original_replace(source, destination)

        with (
            patch.object(
                self.client,
                "_load_credentials",
                side_effect=AssertionError("cache read"),
            ),
            patch.object(calendar.os, "fsync", side_effect=fsync),
            patch.object(calendar.os, "replace", side_effect=replace),
        ):
            await self.client.authenticate(interactive=True)
        self.assertEqual(stages, ["fsync", "replace"])
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        self.assertEqual(
            set(self.directory.iterdir()), {self.credentials_path, self.token_path}
        )
        self.flow_factory.assert_called_once_with(self.config, calendar.SCOPES)
        flow.run_local_server.assert_called_once_with(
            host="localhost",
            port=0,
            timeout_seconds=120,
            authorization_prompt_message="",
            prompt="consent",
        )
        flow.oauth2session.close.assert_called_once_with()
        self.assert_single_worker()

    async def test_actual_flow_routes_http_timeout_to_token_not_authorization_url(self):
        # Exercise the installed flow's forwarding behavior without HTTP or a browser.
        self.flow_factory.side_effect = None
        # Construct directly because the shared class factory is mocked.
        session, config = session_from_client_config(self.config, calendar.SCOPES)
        flow = InstalledAppFlow(session, "installed", config)
        self.flow_factory.return_value = flow
        server = Mock(server_port=12345)
        browser = Mock()

        def make_server(host, port, app, **kwargs):
            def callback():
                self.record("callback")
                app.last_request_uri = (
                    "http://localhost:12345/?code=fixture-code&state="
                    + flow.oauth2session._state
                )

            server.handle_request.side_effect = callback
            return server

        def exchange(token_uri, **kwargs):
            self.record("token.exchange")
            self.assertEqual(kwargs["timeout"], 30)
            self.assertIn("authorization_response", kwargs)
            flow.oauth2session.token = {
                "access_token": "interactive-token",
                "refresh_token": "fixture-refresh",
                "expires_at": time.time() + 3600,
                "scope": list(calendar.SCOPES),
                "token_type": "Bearer",
            }
            return flow.oauth2session.token

        with (
            patch(
                "google_auth_oauthlib.flow.wsgiref.simple_server.make_server",
                side_effect=make_server,
            ),
            patch("google_auth_oauthlib.flow.webbrowser.get", return_value=browser),
            patch.object(flow.oauth2session, "fetch_token", side_effect=exchange),
            patch.object(
                flow.oauth2session, "close", wraps=flow.oauth2session.close
            ) as close,
        ):
            await self.client.authenticate(interactive=True)
        query = parse_qs(urlsplit(browser.open.call_args.args[0]).query)
        self.assertNotIn("timeout", query)
        self.assertNotIn("timeout_seconds", query)
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(server.timeout, 120)
        server.server_close.assert_called_once_with()
        close.assert_called_once_with()
        self.assert_single_worker()

    async def test_failed_interactive_flow_closes_session_preserves_old_token(self):
        flow = self.interactive_flow()
        error = TimeoutError("consent timed out")
        flow.run_local_server.side_effect = error
        original = self.token_path.read_bytes()
        with self.assertRaises(TimeoutError) as raised:
            await self.client.authenticate(interactive=True)
        self.assertEqual(raised.exception.args, error.args)
        flow.oauth2session.close.assert_called_once_with()
        self.assertEqual(self.token_path.read_bytes(), original)
        self.http_factory.assert_not_called()

    async def test_persistence_failures_preserve_old_token_and_remove_temporary_file(
        self,
    ):
        self.interactive_flow()
        original = self.token_path.read_bytes()
        self.token_path.chmod(0o644)
        for operation in ("replace", "fsync", "fchmod"):
            with self.subTest(operation=operation):
                with patch.object(
                    calendar.os, operation, side_effect=OSError("fixture write failure")
                ):
                    with self.assertRaises(calendar.CalendarConfigurationError):
                        await self.client.authenticate(interactive=True)
                self.assertEqual(self.token_path.read_bytes(), original)
                self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o644)
                self.assertEqual(
                    set(self.directory.iterdir()),
                    {self.credentials_path, self.token_path},
                )
                self.transports[-1].close.assert_called_once_with()
        self.build.assert_not_called()

    async def test_temporary_file_creation_failure_is_configuration_error(self):
        self.interactive_flow()
        original = self.token_path.read_bytes()
        with patch.object(
            calendar.tempfile,
            "NamedTemporaryFile",
            side_effect=PermissionError("fixture directory denied"),
        ):
            with self.assertRaises(calendar.CalendarConfigurationError):
                await self.client.authenticate(interactive=True)
        self.assertEqual(self.token_path.read_bytes(), original)
        self.assertEqual(
            set(self.directory.iterdir()), {self.credentials_path, self.token_path}
        )
        self.transports[-1].close.assert_called_once_with()
        self.build.assert_not_called()

    async def test_build_failure_closes_transport_and_propagates(self):
        error = TransportError("discovery unavailable")
        self.build.side_effect = error
        with self.assertRaises(TransportError) as raised:
            await self.client.authenticate()
        self.assertIs(raised.exception, error)
        self.transports[-1].close.assert_called_once_with()

    async def test_events_use_one_utc_window_across_all_pages_including_empty(self):
        await self.client.authenticate()
        resource = self.services[-1].events.return_value
        resource.list.return_value.execute.side_effect = [
            {
                "items": [{"id": "first", "summary": "Full event"}],
                "nextPageToken": "p2",
            },
            {"items": [], "nextPageToken": "p3"},
            {"items": [{"id": "last", "start": {"date": "2026-10-26"}}]},
        ]
        start = datetime(2026, 10, 26, 23, 30, 45, tzinfo=timezone.utc)
        with patch.object(calendar, "datetime") as clock:
            clock.now.return_value = start
            events = await self.client.get_events("fixture-calendar")
        clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual([event["id"] for event in events], ["first", "last"])
        self.assertEqual(events[0]["summary"], "Full event")
        params = {
            "calendarId": "fixture-calendar",
            "timeMin": "2026-10-26T23:30:45Z",
            "timeMax": "2026-10-27T23:30:45Z",
            "singleEvents": True,
            "orderBy": "startTime",
        }
        self.assertEqual(
            resource.list.call_args_list,
            [
                call(**params),
                call(**params, pageToken="p2"),
                call(**params, pageToken="p3"),
            ],
        )
        self.assertEqual(
            resource.list.return_value.execute.call_args_list,
            [call(num_retries=0)] * 3,
        )
        self.assert_single_worker()

    async def test_window_rolls_forward_on_each_fetch(self):
        await self.client.authenticate()
        resource = self.services[-1].events.return_value
        resource.list.return_value.execute.return_value = {}
        first = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        with patch.object(calendar, "datetime") as clock:
            clock.now.side_effect = [first, first + timedelta(minutes=10)]
            self.assertEqual(await self.client.get_events("fixture"), [])
            self.assertEqual(await self.client.get_events("fixture"), [])
        windows = [entry.kwargs for entry in resource.list.call_args_list]
        self.assertNotEqual(windows[0]["timeMin"], windows[1]["timeMin"])
        for window in windows:
            self.assertEqual(
                datetime.fromisoformat(window["timeMax"])
                - datetime.fromisoformat(window["timeMin"]),
                timedelta(hours=24),
            )

    async def test_calendar_list_paginates_through_empty_page(self):
        await self.client.authenticate()
        resource = self.services[-1].calendarList.return_value
        items = [{"id": "a", "primary": True}, {"id": "b", "summary": "Other"}]
        resource.list.return_value.execute.side_effect = [
            {"items": [items[0]], "nextPageToken": "p2"},
            {"nextPageToken": "p3"},
            {"items": [items[1]]},
        ]
        self.assertEqual(await self.client.list_calendars(), items)
        self.assertEqual(
            resource.list.call_args_list,
            [call(), call(pageToken="p2"), call(pageToken="p3")],
        )

    async def test_pagination_failure_never_returns_partial_results(self):
        await self.client.authenticate()
        for method, resource in (
            (lambda: self.client.get_events("fixture"), self.services[-1].events()),
            (self.client.list_calendars, self.services[-1].calendarList()),
        ):
            error = TransportError("second page unavailable")
            resource.list.return_value.execute.side_effect = [
                {"items": [{"id": "partial"}], "nextPageToken": "p2"},
                error,
            ]
            with self.assertRaises(TransportError) as raised:
                await method()
            self.assertIs(raised.exception, error)

    async def test_api_auth_failures_translate_but_transient_failures_do_not(self):
        await self.client.authenticate()
        resource = self.services[-1].events.return_value
        errors = [
            (HttpError(httplib2.Response({"status": "401"}), b"{}"), True),
            (RefreshError("invalid_grant", retryable=False), True),
            (RefreshError("unavailable", retryable=True), False),
            (HttpError(httplib2.Response({"status": "403"}), b"{}"), False),
            (HttpError(httplib2.Response({"status": "503"}), b"{}"), False),
            (TransportError("connection lost"), False),
        ]
        original = self.token_path.read_bytes()
        for error, requires_auth in errors:
            with self.subTest(error=error):
                resource.list.return_value.execute.side_effect = error
                expected = (
                    calendar.AuthorizationRequired if requires_auth else type(error)
                )
                with self.assertRaises(expected) as raised:
                    await self.client.get_events("fixture")
                if not requires_auth:
                    self.assertIs(raised.exception, error)
                self.assertEqual(self.token_path.read_bytes(), original)
        self.flow_factory.assert_not_called()

    async def test_calendar_list_also_translates_unauthorized(self):
        await self.client.authenticate()
        resource = self.services[-1].calendarList.return_value
        resource.list.return_value.execute.side_effect = HttpError(
            httplib2.Response({"status": "401"}), b"{}"
        )
        with self.assertRaises(calendar.AuthorizationRequired):
            await self.client.list_calendars()

    async def test_fetch_before_auth_requires_authorization_without_browser(self):
        with self.assertRaises(calendar.AuthorizationRequired):
            await self.client.get_events("fixture")
        with self.assertRaises(calendar.AuthorizationRequired):
            await self.client.list_calendars()
        self.flow_factory.assert_not_called()
        self.http_factory.assert_not_called()

    async def test_concurrent_fetches_and_reauthentication_use_one_worker(self):
        await self.client.authenticate()
        active = 0
        maximum = 0

        def execute(**kwargs):
            nonlocal active, maximum
            self.record("execute")
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.005)
            active -= 1
            return {"items": []}

        self.services[
            -1
        ].events.return_value.list.return_value.execute.side_effect = execute
        self.services[
            -1
        ].calendarList.return_value.list.return_value.execute.side_effect = execute
        results = await asyncio.gather(
            self.client.get_events("a"),
            self.client.list_calendars(),
            self.client.get_events("b"),
        )
        self.assertEqual(results, [[], [], []])
        self.assertEqual(maximum, 1)
        flow = self.interactive_flow()
        await self.client.authenticate(interactive=True)
        self.services[0].close.assert_called_once_with()
        self.transports[0].close.assert_called_once_with()
        flow.oauth2session.close.assert_called_once_with()
        await self.client.close()
        self.services[1].close.assert_called_once_with()
        self.transports[1].close.assert_called_once_with()
        self.assert_single_worker()

    async def test_cancelled_queued_operation_never_runs(self):
        await self.client.authenticate()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def execute(**kwargs):
            self.record("execute")
            started.set()
            if not release.wait(3):
                raise TimeoutError("fixture worker blocked")
            return {"items": []}

        service = self.services[-1]
        service.events.return_value.list.return_value.execute.side_effect = execute
        running = asyncio.create_task(self.client.get_events("running"))
        await self.wait_started(started)
        queued = asyncio.create_task(self.client.list_calendars())
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        release.set()
        self.assertEqual(await running, [])
        await self.client.close()
        service.calendarList.assert_not_called()
        self.assert_single_worker()

    async def test_close_cancels_queue_and_survives_repeated_cancellation(self):
        await self.client.authenticate()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def execute(**kwargs):
            self.record("execute")
            started.set()
            if not release.wait(3):
                raise TimeoutError("fixture worker blocked")
            return {"items": []}

        service = self.services[-1]
        service.events.return_value.list.return_value.execute.side_effect = execute
        running = asyncio.create_task(self.client.get_events("running"))
        await self.wait_started(started)
        queued = asyncio.create_task(self.client.list_calendars())
        await asyncio.sleep(0)
        closing = asyncio.create_task(self.client.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        service.close.assert_not_called()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        release.set()
        self.assertEqual(await running, [])
        with self.assertRaises(asyncio.CancelledError):
            await closing
        service.calendarList.assert_not_called()
        service.close.assert_called_once_with()
        self.transports[-1].close.assert_called_once_with()
        await self.client.close()
        service.close.assert_called_once_with()
        for operation in (
            self.client.authenticate,
            self.client.list_calendars,
            lambda: self.client.get_events("fixture"),
        ):
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await operation()
        self.assert_single_worker()

    async def test_close_during_first_page_does_not_issue_second_page(self):
        await self.client.authenticate()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def first_page(**kwargs):
            self.record("execute")
            started.set()
            if not release.wait(3):
                raise TimeoutError("fixture worker blocked")
            return {"items": [{"id": "partial"}], "nextPageToken": "p2"}

        service = self.services[-1]
        resource = service.events.return_value
        resource.list.return_value.execute.side_effect = first_page
        running = asyncio.create_task(self.client.get_events("fixture"))
        await self.wait_started(started)
        closing = asyncio.create_task(self.client.close())
        try:
            await asyncio.sleep(0)
            self.assertTrue(self.client._closed)
            self.assertFalse(closing.done())
            service.close.assert_not_called()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await closing
        resource.list.assert_called_once()
        resource.list.return_value.execute.assert_called_once_with(num_retries=0)
        service.close.assert_called_once_with()
        self.transports[-1].close.assert_called_once_with()
        self.assert_single_worker()

    async def test_loop_wide_task_cancellation_cannot_cancel_worker_shutdown(self):
        await self.client.authenticate()
        existing_tasks = asyncio.all_tasks()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def execute(**kwargs):
            self.record("execute")
            started.set()
            if not release.wait(3):
                raise TimeoutError("fixture worker blocked")
            return {"items": []}

        service = self.services[-1]
        service.events.return_value.list.return_value.execute.side_effect = execute
        running = asyncio.create_task(self.client.get_events("fixture"))
        await self.wait_started(started)
        closing = asyncio.create_task(self.client.close())
        also_closing = asyncio.create_task(self.client.close())
        try:
            await asyncio.sleep(0)
            tasks = asyncio.all_tasks() - existing_tasks
            # There is no internal cleanup Task for loop shutdown to cancel.
            self.assertEqual(tasks, {running, closing, also_closing})
            for task in tasks:
                task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertFalse(also_closing.done())
            service.close.assert_not_called()
        finally:
            release.set()
        results = await asyncio.wait_for(
            asyncio.gather(running, closing, also_closing, return_exceptions=True),
            timeout=3,
        )
        for result in results:
            self.assertIsInstance(result, asyncio.CancelledError)
        await self.client.close()
        service.close.assert_called_once_with()
        self.transports[-1].close.assert_called_once_with()
        self.assert_single_worker()

    async def test_cancelling_running_fetch_still_allows_worker_cleanup(self):
        await self.client.authenticate()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def execute(**kwargs):
            self.record("execute")
            started.set()
            if not release.wait(3):
                raise TimeoutError("fixture worker blocked")
            return {"items": []}

        service = self.services[-1]
        service.events.return_value.list.return_value.execute.side_effect = execute
        running = asyncio.create_task(self.client.get_events("fixture"))
        await self.wait_started(started)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        closing = asyncio.create_task(self.client.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        service.close.assert_not_called()
        release.set()
        await closing
        service.close.assert_called_once_with()
        self.assert_single_worker()


if __name__ == "__main__":
    unittest.main()
