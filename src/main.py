import asyncio
import json
import os
import pickle
from datetime import datetime, timedelta, timezone

from desktop_notifier import DesktopNotifier
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Constants
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.pickle"
UPDATE_INTERVAL = 10 * 60  # 10 minutes in seconds
CALENDAR_IDS = ["gabriel.cirio@gmail.com", "gabriel.cirio@seddi.com"]

notifier = DesktopNotifier()


# utility:
async def print_all_calendar_ids(service):
    """Print all calendar IDs for the authenticated user."""
    calendar_list = service.calendarList().list().execute()
    calendar_ids = [calendar["id"] for calendar in calendar_list.get("items", [])]
    print(json.dumps(calendar_ids, indent=2))


async def authenticate_google_calendar():
    """Authenticate and return the Google Calendar service."""
    creds = None
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
    service = build("calendar", "v3", credentials=creds)
    return service


async def get_upcoming_events(service, calendar_id):
    """Retrieve upcoming events from a specific Google Calendar for the next 24 hours."""
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(hours=24)).isoformat()
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


async def notify_event(event):
    """Display a desktop notification for an event."""
    start = event["start"].get("dateTime", event["start"].get("date"))
    summary = event.get("summary", "No Title")
    notification.notify(
        title="Upcoming Event", message=f"{summary} at {start}", timeout=10
    )


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

    while True:
        events.clear()

        for calendar_id in CALENDAR_IDS:
            events += await get_upcoming_events(service, calendar_id)

        # print(json.dumps(events, indent=2))

        now = datetime.now(timezone.utc)
        notifications_to_send = []
        next_notification_time = None

        for event in events:
            event_id = event["id"]
            start_time = parse_event_time(event["start"])

            # 1. Notification at event start
            if start_time and (event_id, start_time) not in sent_notifications:
                if now >= start_time and (now - start_time).total_seconds() < 60:
                    notifications_to_send.append((event, start_time, "Event starting"))
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
                                    f"Reminder: {minutes} min before",
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
            start = event["start"].get("dateTime", event["start"].get("date"))
            summary = event.get("summary", "No Title")
            await notifier.send(
                title=f"{note}", message=f"{summary} at {start}", timeout=10
            )

        # Update events list every 10 minutes
        # Sleep until next notification or update interval
        sleep_seconds = UPDATE_INTERVAL
        if next_notification_time:
            sleep_until_next = (
                next_notification_time - datetime.now(timezone.utc)
            ).total_seconds()
            if sleep_until_next > 0:
                sleep_seconds = min(sleep_seconds, sleep_until_next)
        await asyncio.sleep(sleep_seconds)


if __name__ == "__main__":
    asyncio.run(main())
