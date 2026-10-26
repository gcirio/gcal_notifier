"""Async Google Calendar access backed by one transport-owning worker."""

import asyncio
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httplib2
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp, Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)
HTTP_TIMEOUT_SECONDS = 30
CONSENT_TIMEOUT_SECONDS = 120


class AuthorizationRequired(Exception):
    """The user must explicitly authorize with authenticate(interactive=True)."""


class CalendarConfigurationError(Exception):
    """OAuth client configuration or credential persistence needs attention."""


class GoogleCalendarClient:
    def __init__(
        self,
        credentials_path=Path(__file__).parent / "credentials.json",
        token_path=Path(__file__).parent / "token.json",
    ):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="google-calendar"
        )
        self._service = None
        self._pending = set()
        self._closed = False
        self._close_future = None

    async def authenticate(self, interactive=False):
        return await self._run(self._authenticate, interactive)

    async def get_events(self, calendar_id):
        return await self._run(self._get_events, calendar_id)

    async def list_calendars(self):
        return await self._run(self._list_calendars)

    async def _run(self, operation, *args):
        if self._closed:
            raise RuntimeError("GoogleCalendarClient is closed.")
        future = self._executor.submit(self._invoke, operation, *args)
        self._pending.add(future)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        finally:
            self._pending.discard(future)

    def _invoke(self, operation, *args):
        try:
            return operation(*args)
        except RefreshError as error:
            if error.retryable:
                raise
            self._require_authorization()
        except HttpError as error:
            if error.resp.status != 401:
                raise
            self._require_authorization()

    def _require_authorization(self):
        # A missing client file cannot be fixed by repeatedly clicking Authorize.
        self._read_client_config()
        raise AuthorizationRequired(
            "Google Calendar authorization is required; "
            "call authenticate(interactive=True)."
        )

    def _read_client_config(self):
        try:
            with self.credentials_path.open(encoding="utf-8") as stream:
                config = json.load(stream)
            if not isinstance(config, dict):
                raise ValueError("Expected an OAuth client object.")
            client = config.get("web", config.get("installed"))
            if not isinstance(client, dict):
                raise ValueError("Expected an installed or web OAuth client.")
            for key in ("client_id", "client_secret", "auth_uri", "token_uri"):
                if not isinstance(client.get(key), str) or not client[key].strip():
                    raise ValueError(f"Missing OAuth client field: {key}.")
            for key in ("auth_uri", "token_uri"):
                uri = urlsplit(client[key])
                if uri.scheme != "https" or not uri.hostname:
                    raise ValueError(f"Invalid OAuth client endpoint: {key}.")
            return config
        except (OSError, ValueError, TypeError, RecursionError) as error:
            raise CalendarConfigurationError(
                f"Cannot load OAuth client configuration from {self.credentials_path}."
            ) from error

    def _load_credentials(self):
        try:
            with self.token_path.open(encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                return None
            for key in ("refresh_token", "client_id", "client_secret"):
                if not isinstance(data.get(key), str) or not data[key].strip():
                    return None
            if data.get("token") is not None and not isinstance(data["token"], str):
                return None
            scopes = data.get("scopes")
            if isinstance(scopes, str):
                scopes = scopes.split()
            if not isinstance(scopes, list) or not all(
                isinstance(scope, str) for scope in scopes
            ):
                return None
            credentials = Credentials.from_authorized_user_info(data)
            if not credentials.has_scopes(SCOPES):
                return None
            return credentials
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, AttributeError, RecursionError):
            return None
        except OSError as error:
            raise CalendarConfigurationError(
                f"Cannot read cached credentials from {self.token_path}."
            ) from error

    def _save_credentials(self, credentials):
        temporary_path = None
        try:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.token_path.parent,
                    prefix=f".{self.token_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary_path = Path(stream.name)
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(credentials.to_json())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.token_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError) as error:
            raise CalendarConfigurationError(
                f"Cannot persist credentials to {self.token_path}."
            ) from error

    def _authenticate(self, interactive):
        save = interactive
        if interactive:
            config = self._read_client_config()
            try:
                flow = InstalledAppFlow.from_client_config(config, SCOPES)
            except (ValueError, TypeError, KeyError) as error:
                raise CalendarConfigurationError(
                    "Cannot initialize the OAuth client."
                ) from error
            try:
                # run_local_server forwards extra kwargs to the authorization URL,
                # not the token exchange. Bind its HTTP timeout separately.
                flow.fetch_token = partial(
                    flow.fetch_token, timeout=HTTP_TIMEOUT_SECONDS
                )
                credentials = flow.run_local_server(
                    host="localhost",
                    port=0,
                    timeout_seconds=CONSENT_TIMEOUT_SECONDS,
                    authorization_prompt_message="",
                    prompt="consent",
                )
            finally:
                flow.oauth2session.close()
        else:
            credentials = self._load_credentials()
            if credentials is None:
                self._require_authorization()

        http = httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS)
        try:
            if not credentials.valid:
                credentials.refresh(Request(http))
                save = True
            granted = getattr(credentials, "granted_scopes", None)
            if not credentials.has_scopes(SCOPES) or (
                granted is not None and not set(SCOPES).issubset(granted)
            ):
                self._require_authorization()
            if not credentials.valid or not credentials.refresh_token:
                self._require_authorization()
            if save:
                self._save_credentials(credentials)
            authorized_http = AuthorizedHttp(credentials, http=http)
            self._close_service()
            self._service = build(
                "calendar", "v3", http=authorized_http, cache_discovery=False
            )
        except BaseException:
            http.close()
            raise
        return self

    def _service_required(self):
        if self._service is None:
            self._require_authorization()
        return self._service

    def _list_items(self, resource, **params):
        items = []
        while True:
            if self._closed:
                raise asyncio.CancelledError
            page = resource.list(**params).execute(num_retries=0)
            items.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return items
            params["pageToken"] = page_token

    def _get_events(self, calendar_id):
        service = self._service_required()
        start = datetime.now(timezone.utc)
        end = start + timedelta(hours=24)
        return self._list_items(
            service.events(),
            calendarId=calendar_id,
            timeMin=start.isoformat().replace("+00:00", "Z"),
            timeMax=end.isoformat().replace("+00:00", "Z"),
            singleEvents=True,
            orderBy="startTime",
        )

    def _list_calendars(self):
        return self._list_items(self._service_required().calendarList())

    def _close_service(self):
        service, self._service = self._service, None
        if service is not None:
            # Resource.close delegates to AuthorizedHttp.close and its owned Http.
            service.close()

    def _shutdown(self):
        try:
            self._close_service()
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def close(self):
        if self._close_future is None:
            self._closed = True
            for future in tuple(self._pending):
                future.cancel()
            self._close_future = self._executor.submit(self._shutdown)

        # No asyncio cleanup task can be cancelled by loop-wide task shutdown.
        # Shield the worker's cleanup and defer caller cancellation until it ends.
        completion = asyncio.wrap_future(self._close_future)
        cancelled = False
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                cancelled = True
        completion.result()
        if cancelled:
            raise asyncio.CancelledError
