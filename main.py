import asyncio
import json
import logging
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from desktop_notifier import Button, DesktopNotifier, Icon, Urgency
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

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


async def main():
    """Main function to run the notifier."""
    service = await authenticate_google_calendar()
    event_notifications = []
    events = []

    # Initial fetch of events from all calendars

    # Track notifications sent: (event_id, notification_time)
    sent_notifications = set()

    # Read calendar IDs from file at startup
    def read_calendar_ids():
        try:
            with open(Path(__file__).parent.resolve() / CALENDAR_IDS_FILE, "r") as f:
                return [line.strip() for line in f if line.strip()]
        except Exception as e:
            logging.error(f"Failed to read calendar IDs: {e}")
            return []

    calendar_ids = read_calendar_ids()

    # Track last event update time
    last_update_time = datetime.now(timezone.utc)
    events.clear()
    for calendar_id in calendar_ids:
        events += await get_upcoming_events(service, calendar_id)

    while True:
        now = datetime.now(timezone.utc)
        notifications_to_send = []
        next_notification_time = None

        for event in events:
            event_id = event["id"]
            start_time = parse_event_time(event["start"])

            # 1. Notification at event start
            if start_time and (event_id, start_time) not in sent_notifications:
                if now >= start_time and (now - start_time).total_seconds() < 60:
                    notifications_to_send.append((event, start_time, "Starting now"))
                    sent_notifications.add((event_id, start_time))
                else:
                    if (
                        not next_notification_time
                        or start_time < next_notification_time
                    ):
                        if start_time > now:
                            next_notification_time = start_time

            # 2. Notifications for popup reminders
            reminders = event.get("reminders", {})
            overrides = (
                reminders.get("overrides", [])
                if not reminders.get("useDefault", True)
                else []
            )
            for override in overrides:
                if override.get("method") == "popup":
                    minutes = override.get("minutes", 0)
                    reminder_time = start_time - timedelta(minutes=minutes)
                    if (event_id, reminder_time) not in sent_notifications:
                        if (
                            now >= reminder_time
                            and (now - reminder_time).total_seconds() < 60
                        ):
                            notifications_to_send.append(
                                (
                                    event,
                                    reminder_time,
                                    f"Starting in {minutes} minutes",
                                )
                            )
                            sent_notifications.add((event_id, reminder_time))
                        else:
                            if (
                                not next_notification_time
                                or reminder_time < next_notification_time
                            ):
                                if reminder_time > now:
                                    next_notification_time = reminder_time

        # Send notifications
        for event, notify_time, note in notifications_to_send:
            import webbrowser

            start = event["start"].get("dateTime", event["start"].get("date"))
            summary = event.get("summary", "No Title")
            hangout_link = event.get("hangoutLink")
            message = f"{note}"
            if hangout_link:
                hangout_link_with_user = f"{hangout_link}?pli=1&authuser=1"
                message += f"\nClick to join the meeting..."
                logging.info(
                    f"Notification shown: {summary} at {start} (with hangout link)"
                )
                _ = await notifier.send(
                    title=f"{summary}",
                    message=message,
                    on_clicked=lambda: webbrowser.open(hangout_link_with_user),
                    timeout=NOTIFICATION_TIMEOUT,
                )
            else:
                logging.info(f"Notification shown: {summary} at {start}")
                _ = await notifier.send(
                    title=f"{summary}",
                    message=message,
                    timeout=NOTIFICATION_TIMEOUT,
                )

        # Sleep until next notification or next scheduled update
        now = datetime.now(timezone.utc)
        next_update_time = last_update_time + timedelta(seconds=UPDATE_INTERVAL)
        sleep_until_update = (next_update_time - now).total_seconds()
        sleep_seconds = UPDATE_INTERVAL
        if next_notification_time:
            sleep_until_next = (next_notification_time - now).total_seconds()
            if sleep_until_next > 0:
                sleep_seconds = min(sleep_until_update, sleep_until_next)
            else:
                sleep_seconds = sleep_until_update
        else:
            sleep_seconds = sleep_until_update
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)

        # Only update events if UPDATE_INTERVAL has passed since last update
        now = datetime.now(timezone.utc)
        if now >= next_update_time:
            events.clear()
            for calendar_id in calendar_ids:
                events += await get_upcoming_events(service, calendar_id)
            last_update_time = now


async def clear_notification(notification_id: str):
    try:
        if notification_id:
            await notifier.clear(notification_id)
    except KeyError:
        notification_id = ""


async def run_notifier():
    notification_id = ""
    while True:
        try:
            await main()
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
