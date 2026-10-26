import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from functools import partial
from logging.handlers import RotatingFileHandler
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from desktop_notifier import Button, Capability, DesktopNotifier, Icon, Urgency
from googleapiclient.errors import HttpError

from google_calendar import (
    AuthorizationRequired,
    CalendarConfigurationError,
    GoogleCalendarClient,
)

# Constants

APP_DIR = Path(__file__).parent.resolve()
LOG_FILE = APP_DIR / "gcal_notifier.log"
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


def configure_logging():
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        delay=True,
    )
    logging.basicConfig(
        handlers=[handler],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )


configure_logging()


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


def read_calendar_ids():
    path = APP_DIR / CALENDAR_IDS_FILE
    try:
        with open(path, "r", encoding="utf-8") as stream:
            calendar_ids = list(
                dict.fromkeys(line.strip() for line in stream if line.strip())
            )
    except (OSError, UnicodeError) as e:
        raise CalendarConfigurationError(
            f"Cannot read calendar IDs from {path}."
        ) from e
    if not calendar_ids:
        raise CalendarConfigurationError(f"No calendar IDs configured in {path}.")
    return calendar_ids


def prune_sent_notifications(sent_notifications, now):
    cutoff = now - timedelta(seconds=UPDATE_INTERVAL)
    sent_notifications.difference_update(
        {key for key in sent_notifications if key[2] <= cutoff}
    )


async def send_event_notification(**kwargs):
    """A returned identifier alone does not prove the backend dispatched an alert."""
    dispatched = asyncio.Event()
    acknowledge_dispatch = partial(
        asyncio.get_running_loop().call_soon_threadsafe, dispatched.set
    )
    async with asyncio.timeout(NOTIFICATION_DISPATCH_TIMEOUT):
        capabilities = await notifier.get_capabilities()
        if Capability.ON_DISPATCHED not in capabilities:
            raise RuntimeError("The notification service cannot acknowledge dispatch.")
        notification_id = await notifier.send(
            on_dispatched=acknowledge_dispatch, **kwargs
        )
        if not notification_id:
            raise RuntimeError("The event notification could not be dispatched.")
        await dispatched.wait()
    return notification_id


def parse_event_time(start):
    """Parse an aware instant without guessing a timezone for timed events."""
    if not isinstance(start, dict):
        raise ValueError("Event time must be an object.")
    if "dateTime" in start:
        dt_str = start["dateTime"]
        if not isinstance(dt_str, str) or not dt_str:
            raise ValueError("Event dateTime must be a nonempty string.")
        if not any(separator in dt_str for separator in ("T", "t", " ")):
            raise ValueError("Event dateTime must include both a date and a time.")
        if dt_str.endswith("Z"):
            dt_str = dt_str.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(dt_str)
        if parsed.utcoffset() is None:
            zone_name = start.get("timeZone")
            if not isinstance(zone_name, str) or not zone_name:
                raise ValueError("Timed events require an offset or explicit timezone.")
            try:
                zone = ZoneInfo(zone_name)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError("Invalid event timezone.") from e
            parsed = parsed.replace(tzinfo=zone)
            if parsed.utcoffset() != parsed.replace(fold=1).utcoffset():
                raise ValueError(
                    "Ambiguous or nonexistent event time requires an explicit offset."
                )
        return parsed
    elif "date" in start:
        date_str = start["date"]
        if not isinstance(date_str, str):
            raise ValueError("All-day event date must be a string.")
        day = date.fromisoformat(date_str)
        # Preserve the existing UTC-midnight policy for all-day events.
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    else:
        return None


def event_notification_times(event, calendar_id):
    if not isinstance(event, dict):
        raise ValueError("Event must be an object.")
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("Event must have a nonempty string ID.")
    start_time = parse_event_time(event.get("start"))
    if start_time is None:
        raise ValueError("Event start time is missing.")
    # Scheduling arithmetic must measure elapsed time, not DST wall-clock time.
    try:
        start_instant = start_time.astimezone(timezone.utc)
    except OverflowError as e:
        raise ValueError("Event time is outside the supported UTC range.") from e
    notification_times = [start_instant]
    reminders = event.get("reminders", {})
    if not isinstance(reminders, dict) or not isinstance(
        reminders.get("useDefault", True), bool
    ):
        logging.warning(
            "Ignoring invalid reminders for event %s in calendar %s.",
            event_id,
            calendar_id,
        )
        return start_time, notification_times
    if reminders.get("useDefault", True):
        return start_time, notification_times
    overrides = reminders.get("overrides", [])
    if not isinstance(overrides, list):
        logging.warning(
            "Ignoring invalid reminder overrides for event %s in calendar %s.",
            event_id,
            calendar_id,
        )
        return start_time, notification_times
    for override in overrides:
        if not isinstance(override, dict) or override.get("method") not in (
            "popup",
            "email",
        ):
            logging.warning(
                "Ignoring invalid reminder for event %s in calendar %s.",
                event_id,
                calendar_id,
            )
            continue
        if override["method"] != "popup":
            continue
        minutes = override.get("minutes")
        if type(minutes) is not int or not 0 <= minutes <= 40320:
            logging.warning(
                "Ignoring invalid popup reminder for event %s in calendar %s.",
                event_id,
                calendar_id,
            )
            continue
        try:
            notification_times.append(start_instant - timedelta(minutes=minutes))
        except OverflowError:
            logging.warning(
                "Ignoring out-of-range reminder for event %s in calendar %s.",
                event_id,
                calendar_id,
            )
    return start_time, notification_times


async def main(sent_notifications=None):
    """Run notification scheduling independently of Google requests and sign-in."""
    if sent_notifications is None:
        sent_notifications = set()
    notification_retries = {}
    events_by_calendar = {}
    calendar_fetch_times = {}
    calendar_retry_delays = {}

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
            prune_sent_notifications(sent_notifications, datetime.now(timezone.utc))
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
                    event_id = event.get("id") if isinstance(event, dict) else None
                    if isinstance(event_id, str) and event_id:
                        current_event_ids.add((calendar_id, event_id))
                    if isinstance(event, dict) and event.get("status") == "cancelled":
                        continue
                    try:
                        _, notification_times = event_notification_times(
                            event, calendar_id
                        )
                    except ValueError as e:
                        logging.warning(
                            "Skipping invalid event %s in calendar %s: %s",
                            event_id if isinstance(event_id, str) else "<unknown>",
                            calendar_id,
                            e,
                        )
                        continue
                    for notify_time in notification_times:
                        notification_events[(calendar_id, event_id, notify_time)] = (
                            event
                        )

            for key, (_, _, event) in list(notification_retries.items()):
                if key in notification_events:
                    continue
                try:
                    end_time = parse_event_time(event.get("end", {}))
                except ValueError as e:
                    logging.warning(
                        "Dropping pending notification for event %s in calendar %s: %s",
                        key[1],
                        key[0],
                        e,
                    )
                    notification_retries.pop(key)
                    continue
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
                expires_at = notify_time.astimezone(timezone.utc) + timedelta(
                    seconds=UPDATE_INTERVAL
                )
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
                summary = event.get("summary")
                if not isinstance(summary, str) or not summary:
                    summary = "No Title"
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
                    await send_event_notification(
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
                    logging.info(f"Notification dispatched: {summary} at {start}")

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
