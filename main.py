import asyncio
import json
import logging
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

from desktop_notifier import Button, DesktopNotifier, Icon, Urgency
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Constants
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.pickle"
CALENDAR_IDS_FILE = "calendar_ids.txt"
NOTIFICATION_ICON_FILE = "notification.png"
UPDATE_INTERVAL = 10 * 60  # 10 minutes in seconds
NOTIFICATION_TIMEOUT = 1000
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
async def print_all_calendar_ids(service):
    """Print all calendar IDs for the authenticated user."""
    calendar_list = service.calendarList().list().execute()
    calendar_ids = [calendar["id"] for calendar in calendar_list.get("items", [])]
    print(json.dumps(calendar_ids, indent=2))


async def authenticate_google_calendar():
    """Authenticate and return the Google Calendar service."""
    creds = None
    logging.info("Authenticating with Google Calendar API")
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(creds, token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return service


async def get_upcoming_events(service, calendar_id):
    """Retrieve upcoming events from a specific Google Calendar for the next 24 hours."""
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(hours=24)).isoformat()
    logging.info(f"Updating events for calendar: {calendar_id}")
    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    events = events_result.get("items", [])
    return events


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
    """Main function to run the notifier."""
    service = await authenticate_google_calendar()
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

    while True:
        wake_times = []
        notification_events = {}
        current_event_ids = set()

        # Deliver cached events before a refresh can remove events missed during sleep.
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
                            start_time - timedelta(minutes=override.get("minutes", 0))
                        )
                for notify_time in notification_times:
                    notification_events[(calendar_id, event["id"], notify_time)] = event

        for key, (_, _, event) in list(notification_retries.items()):
            if key in notification_events:
                continue
            end_time = parse_event_time(event.get("end", {}))
            # Ended events fall outside the API query, but their delivery may be pending.
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
                next_retry = datetime.now(timezone.utc) + timedelta(seconds=retry_delay)
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

        refresh_attempted = False
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
            refresh_attempted = True
            refresh_started = datetime.now(timezone.utc)
            try:
                events_by_calendar[calendar_id] = await get_upcoming_events(
                    service, calendar_id
                )
            except Exception as e:
                permanent_error = isinstance(e, HttpError) and (
                    e.resp.status in (400, 401, 404)
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
                    retry_delay = calendar_retry_delays.get(calendar_id, RESTART_TIME)
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
                calendar_fetch_times[calendar_id] = refresh_started
                calendar_retry_delays.pop(calendar_id, None)
                calendar_update_times[calendar_id] = datetime.now(
                    timezone.utc
                ) + timedelta(seconds=UPDATE_INTERVAL)
            break

        # Reevaluate between fetches, before another calendar's cache is replaced.
        if refresh_attempted:
            continue
        wake_times.extend(
            update_time
            for update_time in calendar_update_times.values()
            if update_time is not None
        )
        now = datetime.now(timezone.utc)
        wake_times.append(now + timedelta(seconds=UPDATE_INTERVAL))
        sleep_seconds = (min(wake_times) - now).total_seconds()
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)


async def clear_notification(notification_id: str):
    try:
        if notification_id:
            await notifier.clear(notification_id)
    except KeyError:
        notification_id = ""


async def run_notifier():
    notification_id = ""
    sent_notifications = set()
    while True:
        try:
            await main(sent_notifications)
            break  # Exit if main() finishes normally
        except SystemExit:
            logging.info("SystemExit received, terminating app.")
            break
        except BaseException as e:
            logging.error(f"Fatal error: {e}", exc_info=True)

            # clear last warning notification
            await clear_notification(notification_id)

            # send error notification
            try:
                notification_id = await notifier.send(
                    title=f"gcal_notifier error!\nWill restart in {RESTART_TIME} seconds...",
                    message=f"Fatal error: {e}",
                    timeout=NOTIFICATION_TIMEOUT,
                    urgency=Urgency.Critical,
                )
            except Exception:
                pass

            # wait before restarting
            await asyncio.sleep(RESTART_TIME)

            # clear last warning notification
            await clear_notification(notification_id)

            # send restarting notification with "kill app" button
            try:
                kill_button = Button(
                    title="Click here to kill app!", on_pressed=lambda: sys.exit(0)
                )
                notification_id = await notifier.send(
                    title="gcal_notifier",
                    message="Restarting...",
                    timeout=NOTIFICATION_TIMEOUT,
                    buttons=[kill_button],
                )
            except Exception:
                pass

            # leave at least 1 second for the notification
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run_notifier())
