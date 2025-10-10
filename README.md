# Google Calendar Notifier

A Python application that retrieves events from all your Google Calendars and displays notifications for upcoming events and reminders.

## Features

- Retrieves events from all calendars listed in a file.
- Fetches events for the next 24 hours.
- Displays notifications at event start and for reminders set up in google calendar
- Clickable notifications open Google Meet/Hangout links in your browser.
- Sleeps most of the time, only wakes for notifications and event updates and goes back to sleep

## Setup

### 1. Clone the Repository

```bash
git clone <your-repo-url>
cd gcal_notifier
```

### 2. Install Dependencies

This project uses [uv](https://github.com/astral-sh/uv) for package management. Dependencies will be installed on first run.

### 3. Set Up Google API Credentials

#### a. Create a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project.

#### b. Enable Google Calendar API

1. In your project, go to **APIs & Services > Library**.
2. Search for "Google Calendar API" and enable it.

#### c. Configure (and eventually Publish) OAuth Consent Screen

1. Go to **APIs & Services > OAuth consent screen**.

Then, either:

2. Set up the consent screen (for personal use, set as "External" and add your email as a test user).

**OR**

2. Publish the app to remove the 7-day token revocation limit.
   - In the OAuth consent screen, click "Publish App".
   - This makes the app available for authentication, but only users with your credentials and calendar IDs can access your calendar.
   - Publishing is necessary to prevent tokens from being revoked after 7 days.

#### d. Create OAuth Credentials

1. Go to **APIs & Services > Credentials**.
2. Click **Create Credentials > OAuth client ID**.
3. Choose "Desktop app" and create.
4. Download the `credentials.json` file and place it in the `gcal_notifier` directory.

#### e. First Run Authentication

- On first run, the app will prompt you to authenticate in your browser.
- A `token.pickle` file will be created for future runs.
- **Note:** If the app is not published, your authentication token will be revoked after 7 days. Publishing the app as described above is required for persistent access.

### 4. List Your Calendar IDs

Create a file named `calendar_ids.txt` in the `gcal_notifier` directory.
Add one calendar ID per line.
You can find your calendar IDs in the Google Calendar web UI under "Settings and sharing" for each calendar.

**Example:**
```
your.account@gmail.com
another_calendar_id@group.calendar.google.com
```

## Usage

Run the application from the project root:

```bash
uv run main.py
```

- The app will run in the background, periodically updating events and sending notifications.
- Logs are written to `gcal_notifier.log`.

## Troubleshooting

- **Authentication Issues:** Make sure your Google account is listed as a test user in the OAuth consent screen.
- **API Errors:** Ensure your credentials are correct and the Calendar API is enabled.
- **No Notifications:** Check that your calendar IDs are correct and events exist in the next 24 hours.

## Customization

- **Notification Timeout:** Change the `NOTIFICATION_TIMEOUT` variable in `main.py`.
- **Update Interval:** Change the `UPDATE_INTERVAL` variable in `main.py`.
- **Calendar List:** Edit `calendar_ids.txt`.
- **Notification Icon:** Change `notification.png` with whatever you want to use.

## License

MIT License
