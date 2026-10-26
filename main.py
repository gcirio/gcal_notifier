import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from math import ceil
from pathlib import Path

from desktop_notifier import Button, Capability, DesktopNotifier, Icon, Urgency
from googleapiclient.errors import HttpError

from google_calendar import (
    AuthorizationRequired,
    CalendarConfigurationError,
    GoogleCalendarClient,
)

# Constants

CALENDAR_IDS_FILE = "calendar_ids.txt"
NOTIFICATION_ICON_FILE = "notification.png"
UPDATE_INTERVAL = 10 * 60  # 10 minutes in seconds
NOTIFICATION_TIMEOUT = 1000
NOTIFICATION_DISPATCH_TIMEOUT = 5
RESTART_TIME = 5

notifier = DesktopNotifier(
    app_name="Google Calendar Notifier",
    app_icon=Icon(path=Path(__file__).parent.resolve() / NOTIFICATION_ICON_FILE),
)

# Setup logging
logging.basicConfig(
    filename="gcal_notifier.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)


# utility:
class AuthenticationNotificationError(Exception):
    """Authorization cannot proceed without a visible, clickable notification."""


async def wait_for_work(tasks, seconds):
    """Wait for background work or a deadline without cancelling the work."""
    timer = asyncio.create_task(asyncio.sleep(max(0, seconds)))
    try:
        await asyncio.wait([*tasks, timer], return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)


async def print_all_calendar_ids(service):
    """Print all calendar IDs for the authenticated user."""
    calendars = await service.list_calendars()
    print(json.dumps([calendar["id"] for calendar in calendars], indent=2))


async def authenticate_google_calendar(client, *, reauthenticate=False):
    """Use cached credentials, or wait for an explicit sign-in notification click."""
    if not reauthenticate:
        retry_delay = RESTART_TIME
        while True:
            try:
                await client.authenticate()
                return client
            except AuthorizationRequired:
                break
            except CalendarConfigurationError:
                raise
            except Exception:
                logging.exception("Unable to connect to Google Calendar; retrying.")
                await wait_for_work([], retry_delay)
                retry_delay = min(retry_delay * 2, UPDATE_INTERVAL)

    while True:
        requested = asyncio.Event()
        dispatched = asyncio.Event()
        loop = asyncio.get_running_loop()
        request_authentication = partial(loop.call_soon_threadsafe, requested.set)
        acknowledge_dispatch = partial(loop.call_soon_threadsafe, dispatched.set)
        notification_id = None
        try:
            try:
                async with asyncio.timeout(NOTIFICATION_DISPATCH_TIMEOUT):
                    capabilities = await notifier.get_capabilities()
                    if Capability.ON_DISPATCHED not in capabilities or not (
                        Capability.ON_CLICKED in capabilities
                        or Capability.BUTTONS in capabilities
                    ):
                        raise AuthenticationNotificationError(
                            "The notification service does not support clickable sign-in notifications."
                        )
                    notification_id = await notifier.send(
                        title="Google Calendar sign-in required",
                        message="Click to authenticate and resume calendar updates.",
                        on_clicked=(
                            request_authentication
                            if Capability.ON_CLICKED in capabilities
                            else None
                        ),
                        buttons=(
                            [
                                Button(
                                    title="Authenticate",
                                    on_pressed=request_authentication,
                                )
                            ]
                            if Capability.BUTTONS in capabilities
                            else []
                        ),
                        on_dispatched=acknowledge_dispatch,
                        timeout=0,
                    )
                    if not notification_id:
                        raise AuthenticationNotificationError(
                            "The authentication notification could not be displayed."
                        )
                    await dispatched.wait()
            except Exception as e:
                raise AuthenticationNotificationError(
                    "Cannot display the sign-in notification; check the desktop "
                    "notification service and restart the app."
                ) from e
            await requested.wait()
        finally:
            await clear_notification(notification_id)

        try:
            await client.authenticate(interactive=True)
            return client
        except CalendarConfigurationError:
            raise
        except Exception:
            logging.exception(
                "Sign-in did not complete. Click the notification to retry."
            )


async def get_upcoming_events(service, calendar_id):
    """Retrieve every page of events in the next rolling 24 hours."""
    logging.info(f"Updating events for calendar: {calendar_id}")
    return await service.get_events(calendar_id)


def parse_event_time(start):
    """Parse event start time to always return an offset-aware datetime."""
    if "dateTime" in start:
        dt_str = start["dateTime"]
        if dt_str.endswith("Z"):
            dt_str = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(dt_str)
    elif "date" in start:
        # All-day event, treat as UTC midnight
        return datetime.fromisoformat(start["date"]).replace(tzinfo=timezone.utc)
    else:
        return None


async def main(sent_notifications=None):
    """Run notification scheduling independently of Google requests and sign-in."""
    if sent_notifications is None:
        sent_notifications = set()
    notification_retries = {}
    events_by_calendar = {}
    calendar_fetch_times = {}
    calendar_retry_delays = {}

    # Read calendar IDs from file at startup
    def read_calendar_ids():
        try:
            with open(Path(__file__).parent.resolve() / CALENDAR_IDS_FILE, "r") as f:
                return [line.strip() for line in f if line.strip()]
        except Exception as e:
            logging.error(f"Failed to read calendar IDs: {e}")
            return []

    calendar_ids = read_calendar_ids()
    calendar_update_times: dict[str, datetime | None] = {
        calendar_id: datetime.now(timezone.utc) for calendar_id in calendar_ids
    }
    client = GoogleCalendarClient()
    authentication_task = None
    fetch_task = None
    fetch_calendar = None
    refresh_started = None
    try:
        authentication_task = asyncio.create_task(authenticate_google_calendar(client))
        while True:
            if authentication_task is not None and authentication_task.done():
                authentication_task.result()
                authentication_task = None
                calendar_update_times = {
                    calendar_id: datetime.now(timezone.utc)
                    for calendar_id in calendar_ids
                }
                calendar_retry_delays.clear()
                continue

            if fetch_task is not None and fetch_task.done():
                completed_fetch, fetch_task = fetch_task, None
                calendar_id = fetch_calendar
                assert calendar_id is not None and refresh_started is not None
                try:
                    events = completed_fetch.result()
                except CalendarConfigurationError:
                    raise
                except Exception as e:
                    if isinstance(e, AuthorizationRequired) or (
                        isinstance(e, HttpError) and e.resp.status == 401
                    ):
                        authentication_task = asyncio.create_task(
                            authenticate_google_calendar(client, reauthenticate=True)
                        )
                    else:
                        permanent_error = isinstance(e, HttpError) and (
                            e.resp.status in (400, 404)
                            or (
                                e.resp.status == 403
                                and any(
                                    isinstance(detail, dict)
                                    and detail.get("reason")
                                    in (
                                        "forbidden",
                                        "insufficientPermissions",
                                        "accessNotConfigured",
                                        "forbiddenForNonOrganizer",
                                        "notACalendarUser",
                                        "requiredAccessLevel",
                                    )
                                    for detail in e.error_details
                                )
                            )
                        )
                        if permanent_error:
                            calendar_update_times[calendar_id] = None
                            events_by_calendar.pop(calendar_id, None)
                            logging.error(
                                f"Calendar {calendar_id} is unavailable: {e}. "
                                "Skipping it until the app restarts."
                            )
                        else:
                            retry_delay = calendar_retry_delays.get(
                                calendar_id, RESTART_TIME
                            )
                            calendar_update_times[calendar_id] = datetime.now(
                                timezone.utc
                            ) + timedelta(seconds=retry_delay)
                            calendar_retry_delays[calendar_id] = min(
                                retry_delay * 2, UPDATE_INTERVAL
                            )
                            logging.error(
                                f"Failed to update calendar {calendar_id}: {e}. "
                                f"Retrying in {retry_delay} seconds."
                            )
                else:
                    events_by_calendar[calendar_id] = events
                    calendar_fetch_times[calendar_id] = refresh_started
                    calendar_retry_delays.pop(calendar_id, None)
                    calendar_update_times[calendar_id] = datetime.now(
                        timezone.utc
                    ) + timedelta(seconds=UPDATE_INTERVAL)
                continue

            wake_times = []
            notification_events = {}
            current_event_ids = set()

            # Deliver cached deadlines even while a request or sign-in is pending.
            for calendar_id, events in events_by_calendar.items():
                for event in events:
                    current_event_ids.add((calendar_id, event["id"]))
                    start_time = parse_event_time(event["start"])
                    if not start_time:
                        continue
                    notification_times = [start_time]
                    reminders = event.get("reminders", {})
                    overrides = (
                        reminders.get("overrides", [])
                        if not reminders.get("useDefault", True)
                        else []
                    )
                    for override in overrides:
                        if override.get("method") == "popup":
                            notification_times.append(
                                start_time
                                - timedelta(minutes=override.get("minutes", 0))
                            )
                    for notify_time in notification_times:
                        notification_events[(calendar_id, event["id"], notify_time)] = (
                            event
                        )

            for key, (_, _, event) in list(notification_retries.items()):
                if key in notification_events:
                    continue
                end_time = parse_event_time(event.get("end", {}))
                # Ended events can leave the query while their delivery is pending.
                if (
                    calendar_update_times[key[0]] is not None
                    and key[:2] not in current_event_ids
                    and end_time is not None
                    and end_time <= calendar_fetch_times[key[0]]
                ):
                    notification_events[key] = event
                else:
                    notification_retries.pop(key)

            for key, event in notification_events.items():
                if fetch_task is not None and fetch_task.done():
                    break
                if key in sent_notifications:
                    continue
                notify_time = key[2]
                now = datetime.now(timezone.utc)
                # Catch up for one update interval, but do not replay older alerts.
                expires_at = notify_time + timedelta(seconds=UPDATE_INTERVAL)
                if now >= expires_at:
                    notification_retries.pop(key, None)
                    continue
                retry_time, retry_delay, _ = notification_retries.get(
                    key, (notify_time, RESTART_TIME, event)
                )
                next_attempt = max(notify_time, retry_time)
                if now < next_attempt:
                    wake_times.append(min(next_attempt, expires_at))
                    continue

                import webbrowser

                start_time = parse_event_time(event["start"])
                if not start_time:
                    continue
                start = event["start"].get("dateTime", event["start"].get("date"))
                summary = event.get("summary", "No Title")
                if now >= start_time:
                    minutes = int((now - start_time).total_seconds() // 60)
                    message = (
                        f"Started {minutes} minutes ago" if minutes else "Starting now"
                    )
                else:
                    minutes = ceil((start_time - now).total_seconds() / 60)
                    message = f"Starting in {minutes} minutes"
                hangout_link = event.get("hangoutLink")
                on_clicked = None
                if hangout_link:
                    hangout_link_with_user = f"{hangout_link}?pli=1&authuser=1"
                    message += "\nClick to join the meeting..."
                    on_clicked = lambda url=hangout_link_with_user: webbrowser.open(url)
                try:
                    await notifier.send(
                        title=summary,
                        message=message,
                        on_clicked=on_clicked,
                        timeout=NOTIFICATION_TIMEOUT,
                    )
                except Exception as e:
                    logging.error(f"Failed to send notification for {summary}: {e}")
                    next_retry = datetime.now(timezone.utc) + timedelta(
                        seconds=retry_delay
                    )
                    notification_retries[key] = (
                        next_retry,
                        min(retry_delay * 2, UPDATE_INTERVAL),
                        event,
                    )
                    wake_times.append(min(next_retry, expires_at))
                else:
                    sent_notifications.add(key)
                    notification_retries.pop(key, None)
                    logging.info(f"Notification shown: {summary} at {start}")

            if fetch_task is not None and fetch_task.done():
                continue
            if authentication_task is None and fetch_task is None:
                for calendar_id, update_time in sorted(
                    (
                        (calendar_id, update_time)
                        for calendar_id, update_time in calendar_update_times.items()
                        if update_time is not None
                    ),
                    key=lambda item: item[1],
                ):
                    if datetime.now(timezone.utc) < update_time:
                        break
                    fetch_calendar = calendar_id
                    refresh_started = datetime.now(timezone.utc)
                    fetch_task = asyncio.create_task(
                        get_upcoming_events(client, calendar_id)
                    )
                    break

            if authentication_task is None and fetch_task is None:
                wake_times.extend(
                    update_time
                    for update_time in calendar_update_times.values()
                    if update_time is not None
                )
            now = datetime.now(timezone.utc)
            wake_times.append(now + timedelta(seconds=UPDATE_INTERVAL))
            work = [
                task for task in (authentication_task, fetch_task) if task is not None
            ]
            await wait_for_work(work, max(0, (min(wake_times) - now).total_seconds()))
    finally:
        try:
            tasks = [
                task for task in (authentication_task, fetch_task) if task is not None
            ]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await client.close()


async def clear_notification(notification_id):
    try:
        if notification_id:
            await notifier.clear(notification_id)
    except Exception:
        logging.warning("Unable to clear a notification.", exc_info=True)


async def run_notifier():
    notification_id = None
    sent_notifications = set()
    running_task = asyncio.current_task()
    assert running_task is not None
    try:
        while True:
            try:
                await main(sent_notifications)
                return
            except asyncio.CancelledError:
                raise
            except (CalendarConfigurationError, AuthenticationNotificationError):
                logging.exception(
                    "Cannot run Google Calendar Notifier; setup needs attention."
                )
                return 1
            except SystemExit:
                logging.info("SystemExit received, terminating app.")
                return
            except Exception as e:
                logging.exception("Fatal notifier error; restarting.")
                await clear_notification(notification_id)
                notification_id = None
                try:
                    notification_id = await notifier.send(
                        title=f"gcal_notifier error!\nWill restart in {RESTART_TIME} seconds...",
                        message=f"Fatal error: {e}",
                        timeout=NOTIFICATION_TIMEOUT,
                        urgency=Urgency.Critical,
                    )
                except Exception:
                    logging.warning(
                        "Unable to display the restart warning.", exc_info=True
                    )

                await wait_for_work([], RESTART_TIME)
                await clear_notification(notification_id)
                notification_id = None
                try:
                    notification_id = await notifier.send(
                        title="gcal_notifier",
                        message="Restarting...",
                        timeout=NOTIFICATION_TIMEOUT,
                        buttons=[
                            Button(title="Stop app", on_pressed=running_task.cancel)
                        ],
                    )
                except Exception:
                    logging.warning(
                        "Unable to display the restart notification.", exc_info=True
                    )
                await wait_for_work([], 1)
    finally:
        await clear_notification(notification_id)


if __name__ == "__main__":
    sys.exit(asyncio.run(run_notifier()))
