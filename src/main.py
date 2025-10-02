import asyncio
import os
import pickle
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from plyer import notification

# Constants
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.pickle"
UPDATE_INTERVAL = 10 * 60  # 10 minutes in seconds


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


async def get_upcoming_events(service):
    """Retrieve upcoming events from Google Calendar."""
    now = datetime.now(timezone.utc).isoformat()  # ISO 8601 string with UTC timezone
    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=10,
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


async def main():
    """Main function to run the notifier."""
    service = await authenticate_google_calendar()
    events = await get_upcoming_events(service)
    event_notifications = []

    while True:
        now = datetime.now(timezone.utc)
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            event_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if now < event_time and (event_time - now).total_seconds() <= 60:
                if event["id"] not in event_notifications:
                    await notify_event(event)
                    event_notifications.append(event["id"])

        # Update events list every 10 minutes
        await asyncio.sleep(UPDATE_INTERVAL)
        events = await get_upcoming_events(service)


if __name__ == "__main__":
    asyncio.run(main())
