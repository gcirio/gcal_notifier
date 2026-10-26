import io
import unittest
from inspect import signature
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import mock_open, patch
from urllib.parse import parse_qsl, urlsplit

with (
    patch("desktop_notifier.DesktopNotifier"),
    patch("logging.basicConfig") as import_logging,
    patch(
        "logging.FileHandler._open",
        side_effect=AssertionError("Import must not open a log file"),
    ),
):
    import main as app

for configured_call in import_logging.call_args_list:
    for configured_handler in configured_call.kwargs.get("handlers", []):
        configured_handler.close()


class MeetingAccountTestCase(unittest.TestCase):
    def setUp(self):
        self.logging = self.enterContext(patch.object(app, "logging"))
        client = self.enterContext(
            patch.object(
                app,
                "GoogleCalendarClient",
                side_effect=AssertionError("Account hints must not query Google"),
            )
        )
        browser = self.enterContext(
            patch(
                "webbrowser.open",
                side_effect=AssertionError("Account hints must not open a browser"),
            )
        )
        self.addCleanup(client.assert_not_called)
        self.addCleanup(browser.assert_not_called)


class AccountEmailTests(MeetingAccountTestCase):
    def test_accepts_syntactic_gmail_workspace_and_plus_addresses(self):
        for value in (
            "alice@gmail.com",
            "alice@googlemail.com",
            "alice@workspace.example",
            "first.last+calendar@gmail.com",
            "first_last+team@dept.workspace.example",
            "Alice+Meet@Workspace.Example",
            "not-a-real-account@unregistered.example",
        ):
            with self.subTest(value=value):
                self.assertIs(app.is_account_email(value), True)

    def test_rejects_missing_parts_multiple_at_signs_and_display_names(self):
        for value in (
            "",
            "primary",
            "alice",
            "@example.com",
            "alice@",
            "alice@localhost",
            "alice@@example.com",
            "alice@example@com",
            "<alice@example.com>",
            "Alice <alice@example.com>",
            "alice@example.com (Alice)",
        ):
            with self.subTest(value=value):
                self.assertIs(app.is_account_email(value), False)

    def test_rejects_whitespace_including_surrounding_whitespace(self):
        for value in (
            " alice@example.com",
            "alice@example.com ",
            "ali ce@example.com",
            "alice@ example.com",
            "alice@example .com",
            "alice\t@example.com",
            "alice@example.com\n",
            "alice\r@example.com",
            "alice\u00a0@example.com",
        ):
            with self.subTest(value=value):
                self.assertIs(app.is_account_email(value), False)

    def test_rejects_all_google_generated_calendar_domains_case_insensitively(self):
        for domain in (
            "calendar.google.com",
            "group.calendar.google.com",
            "import.calendar.google.com",
            "resource.calendar.google.com",
            "holiday.calendar.google.com",
            "arbitrary.deep.calendar.google.com",
            "CALENDAR.GOOGLE.COM",
            "GrOuP.CaLeNdAr.GoOgLe.CoM",
        ):
            with self.subTest(domain=domain):
                self.assertIs(app.is_account_email(f"calendar-id@{domain}"), False)

    def test_generated_domain_exclusion_respects_domain_boundaries(self):
        for value in (
            "alice@notcalendar.google.com",
            "alice@calendar.google.com.example.org",
            "calendar.google.com@example.org",
        ):
            with self.subTest(value=value):
                self.assertIs(app.is_account_email(value), True)


class CalendarAccountsTests(MeetingAccountTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(app, "APP_DIR", self.directory))
        self.enterContext(patch.object(app, "CALENDAR_IDS_FILE", "test-calendars.txt"))
        self.calendar_file = self.directory / app.CALENDAR_IDS_FILE

    def write_config(self, contents):
        self.calendar_file.write_text(contents, encoding="utf-8")

    def assert_error_line(self, contents, line_number):
        self.write_config(contents)
        with self.assertRaises(app.CalendarConfigurationError) as raised:
            app.read_calendar_accounts()
        self.assertRegex(str(raised.exception), rf"(?i)\bline\s+{line_number}\b")

    def test_strips_blank_lines_and_fields_and_preserves_utf8_and_input_order(self):
        self.write_config(
            " \r\n alice@gmail.com \r\n"
            "\t東京 | owner+meet@workspace.example \t\n"
            "\n calendrier-été\n"
            " shared@group.calendar.google.com | other@gmail.com \n"
        )

        accounts = app.read_calendar_accounts()

        self.assertIsInstance(accounts, dict)
        self.assertEqual(
            list(accounts.items()),
            [
                ("alice@gmail.com", "alice@gmail.com"),
                ("東京", "owner+meet@workspace.example"),
                ("calendrier-été", None),
                ("shared@group.calendar.google.com", "other@gmail.com"),
            ],
        )
        self.assertEqual(self.logging.warning.call_count, 1)

    def test_infers_ordinary_accounts_but_allows_explicit_overrides(self):
        self.write_config(
            "alice@gmail.com\n"
            "first.last+calendar@workspace.example\n"
            "another@gmail.com | selected+meet@workspace.example\n"
            "primary | primary-owner@gmail.com\n"
        )

        self.assertEqual(
            app.read_calendar_accounts(),
            {
                "alice@gmail.com": "alice@gmail.com",
                "first.last+calendar@workspace.example": (
                    "first.last+calendar@workspace.example"
                ),
                "another@gmail.com": "selected+meet@workspace.example",
                "primary": "primary-owner@gmail.com",
            },
        )
        self.logging.warning.assert_not_called()

    def test_generated_ids_and_other_bare_ids_have_no_account_hint(self):
        calendar_ids = [
            "primary",
            "opaque-calendar-id",
            "Alice <alice@example.com>",
            "id@calendar.google.com",
            "id@group.calendar.google.com",
            "id@import.calendar.google.com",
            "id@arbitrary.deep.calendar.google.com",
            "id@GrOuP.CaLeNdAr.GoOgLe.CoM",
        ]
        self.write_config("\n".join(calendar_ids))

        self.assertEqual(
            list(app.read_calendar_accounts().items()),
            [(calendar_id, None) for calendar_id in calendar_ids],
        )
        self.assertEqual(self.logging.warning.call_count, len(calendar_ids))

    def test_warns_once_per_unique_unmapped_calendar_not_per_duplicate_line(self):
        self.write_config(
            "primary\nprimary\n"
            "id@group.calendar.google.com\n"
            "alice@gmail.com\n"
            "id@group.calendar.google.com\n"
            "mapped@group.calendar.google.com | owner@gmail.com\n"
            "opaque-calendar-id\nopaque-calendar-id\n"
        )

        accounts = app.read_calendar_accounts()

        self.assertEqual(
            list(accounts),
            [
                "primary",
                "id@group.calendar.google.com",
                "alice@gmail.com",
                "mapped@group.calendar.google.com",
                "opaque-calendar-id",
            ],
        )
        self.assertEqual(self.logging.warning.call_count, 3)
        messages = []
        for warning_call in self.logging.warning.call_args_list:
            message, *arguments = warning_call.args
            messages.append(
                str(message) % tuple(arguments) if arguments else str(message)
            )
        for calendar_id in (
            "primary",
            "id@group.calendar.google.com",
            "opaque-calendar-id",
        ):
            with self.subTest(calendar_id=calendar_id):
                self.assertEqual(sum(calendar_id in message for message in messages), 1)

    def test_deduplicates_matching_resolved_accounts_without_reordering(self):
        self.write_config(
            "alice@gmail.com\n"
            "shared | owner@workspace.example\n"
            "alice@gmail.com | alice@gmail.com\n"
            "shared | owner@workspace.example\n"
            "another | owner@workspace.example\n"
            "alice@gmail.com\n"
        )

        self.assertEqual(
            list(app.read_calendar_accounts().items()),
            [
                ("alice@gmail.com", "alice@gmail.com"),
                ("shared", "owner@workspace.example"),
                ("another", "owner@workspace.example"),
            ],
        )
        self.logging.warning.assert_not_called()

    def test_read_uses_app_relative_path_and_explicit_utf8_encoding(self):
        opened = mock_open(read_data="alice@gmail.com\n")
        with patch("builtins.open", opened), patch.object(io, "open", opened):
            self.assertEqual(
                app.read_calendar_accounts(), {"alice@gmail.com": "alice@gmail.com"}
            )

        opened.assert_called_once()
        arguments = (
            signature(io.open)
            .bind(*opened.call_args.args, **opened.call_args.kwargs)
            .arguments
        )
        self.assertEqual(Path(arguments["file"]), self.calendar_file)
        self.assertEqual(arguments.get("encoding"), "utf-8")

    def test_conflicting_explicit_accounts_are_fatal_and_report_physical_line(self):
        self.assert_error_line(
            "\nshared | first@gmail.com\n\nshared | second@gmail.com\n", 4
        )

    def test_conflicting_inferred_and_explicit_accounts_are_fatal_in_either_order(self):
        for contents in (
            "alice@gmail.com\nalice@gmail.com | other@gmail.com\n",
            "alice@gmail.com | other@gmail.com\nalice@gmail.com\n",
        ):
            with self.subTest(contents=contents):
                self.assert_error_line(contents, 2)

    def test_unmapped_and_explicit_accounts_conflict_in_either_order(self):
        for lines in (
            (
                "shared@group.calendar.google.com",
                "shared@group.calendar.google.com | owner@gmail.com",
            ),
            (
                "shared@group.calendar.google.com | owner@gmail.com",
                "shared@group.calendar.google.com",
            ),
        ):
            with self.subTest(lines=lines):
                self.assert_error_line("\n".join(lines), 2)

    def test_malformed_mapping_lines_are_fatal_and_report_physical_line(self):
        for invalid_line in (
            "shared | owner@gmail.com | extra@gmail.com",
            "shared || owner@gmail.com",
            "| owner@gmail.com",
            " \t | owner@gmail.com",
            "shared |",
            "shared | \t ",
            "|",
        ):
            with self.subTest(invalid_line=invalid_line):
                self.assert_error_line(f"alice@gmail.com\n\n{invalid_line}\n", 3)

    def test_explicit_accounts_must_be_ordinary_email_addresses(self):
        for account in (
            "not-an-email",
            "owner@localhost",
            "owner@@example.com",
            "@example.com",
            "owner@",
            "Owner <owner@example.com>",
            "own er@example.com",
            "owner\t@example.com",
            "id@calendar.google.com",
            "id@group.calendar.google.com",
            "id@import.calendar.google.com",
            "id@arbitrary.deep.calendar.google.com",
            "id@GrOuP.CaLeNdAr.GoOgLe.CoM",
        ):
            with self.subTest(account=account):
                self.assert_error_line(f"\nshared | {account}\n", 2)

    def test_empty_or_whitespace_only_configurations_are_fatal(self):
        for contents in ("", " \t\r\n\n\t \n"):
            with self.subTest(contents=contents):
                self.write_config(contents)
                with self.assertRaises(app.CalendarConfigurationError):
                    app.read_calendar_accounts()

    def test_missing_configuration_is_fatal(self):
        with self.assertRaises(app.CalendarConfigurationError):
            app.read_calendar_accounts()

    def test_unreadable_configuration_is_fatal(self):
        for error in (
            PermissionError("test permission failure"),
            OSError("test I/O failure"),
        ):
            with self.subTest(error=type(error).__name__):
                opened = mock_open()
                opened.side_effect = error
                with (
                    patch("builtins.open", opened),
                    patch.object(io, "open", opened),
                    self.assertRaises(app.CalendarConfigurationError),
                ):
                    app.read_calendar_accounts()

    def test_invalid_utf8_configuration_is_fatal(self):
        self.calendar_file.write_bytes(b"alice@gmail.com\n\xff\n")

        with self.assertRaises(app.CalendarConfigurationError):
            app.read_calendar_accounts()

    def test_read_calendar_ids_delegates_to_accounts_and_returns_ordered_keys(self):
        accounts = {"calendar-b": "owner@gmail.com", "calendar-a": None}
        with patch.object(
            app, "read_calendar_accounts", return_value=accounts
        ) as reader:
            self.assertEqual(app.read_calendar_ids(), ["calendar-b", "calendar-a"])
        reader.assert_called_once_with()

    def test_read_calendar_ids_parses_mappings_and_deduplicates_for_legacy_callers(
        self,
    ):
        self.write_config(
            "shared | owner@gmail.com\nalice@gmail.com\n"
            "shared | owner@gmail.com\nprimary\nprimary\n"
        )

        self.assertEqual(
            app.read_calendar_ids(), ["shared", "alice@gmail.com", "primary"]
        )


class MeetingUrlTests(MeetingAccountTestCase):
    def test_no_mapping_returns_the_exact_original_url(self):
        for url in (
            "https://meet.google.com/abc-defg-hij",
            "https://meet.google.com/abc?authuser=old%40gmail.com&pli=1#room",
            "https://meet.google.com/abc?authuser=0&authuser=&x=a%20b&x=a+b&blank=#a%2Fb",
            "https://meet.google.com/abc?AuthUser=2&encoded=%2f%2F&bare&pli=0",
            "https://meet.google.com/abc?",
            "https://meet.google.com/abc#",
        ):
            with self.subTest(url=url):
                self.assertEqual(app.build_meeting_url(url, None), url)

    def test_adds_one_account_hint_without_adding_pli(self):
        for url in (
            "https://meet.google.com/abc-defg-hij",
            "https://meet.google.com/abc-defg-hij?",
        ):
            with self.subTest(url=url):
                result = urlsplit(app.build_meeting_url(url, "owner@gmail.com"))
                self.assertEqual(result.scheme, "https")
                self.assertEqual(result.netloc, "meet.google.com")
                self.assertEqual(result.path, "/abc-defg-hij")
                self.assertEqual(
                    parse_qsl(result.query, keep_blank_values=True),
                    [("authuser", "owner@gmail.com")],
                )
                self.assertEqual(result.fragment, "")

    def test_replaces_all_lowercase_authuser_parameters_and_keeps_other_pairs(self):
        url = (
            "https://meet.google.com/abc?authuser=0&x=first&authuser=old%40gmail.com"
            "&x=second&authuser=&blank=&AuthUser=keep&pli=0#room"
        )

        result = urlsplit(app.build_meeting_url(url, "owner@workspace.example"))
        pairs = parse_qsl(result.query, keep_blank_values=True)

        self.assertEqual(
            [value for key, value in pairs if key == "authuser"],
            ["owner@workspace.example"],
        )
        self.assertEqual(
            [(key, value) for key, value in pairs if key != "authuser"],
            [
                ("x", "first"),
                ("x", "second"),
                ("blank", ""),
                ("AuthUser", "keep"),
                ("pli", "0"),
            ],
        )
        self.assertEqual(result.fragment, "room")

    def test_plus_address_is_percent_encoded_and_round_trips_as_one_parameter(self):
        account = "owner+meet@workspace.example"
        result = urlsplit(app.build_meeting_url("https://meet.google.com/abc", account))

        self.assertEqual(
            parse_qsl(result.query, keep_blank_values=True), [("authuser", account)]
        )
        self.assertIn("authuser=owner%2Bmeet%40workspace.example", result.query)

    def test_preserves_duplicate_blank_and_encoded_query_values_and_fragment(self):
        url = (
            "https://meet.google.com/abc?state=a%2Bb%26c%3Dd&label=a+b"
            "&label=a%20b&empty=&bare&redirect=https%3A%2F%2Fexample.org%2F%3Fx%3D1"
            "#room%2Fdetails?authuser=fragment"
        )

        result = urlsplit(app.build_meeting_url(url, "owner@gmail.com"))
        original = urlsplit(url)
        pairs = parse_qsl(result.query, keep_blank_values=True)

        self.assertEqual(result[:3], original[:3])
        self.assertEqual(result.fragment, original.fragment)
        self.assertEqual(
            [(key, value) for key, value in pairs if key != "authuser"],
            parse_qsl(original.query, keep_blank_values=True),
        )
        self.assertEqual(
            [value for key, value in pairs if key == "authuser"], ["owner@gmail.com"]
        )

    def test_adds_query_before_fragment_when_original_has_no_query(self):
        result = urlsplit(
            app.build_meeting_url(
                "https://meet.google.com/abc#room?authuser=fragment", "owner@gmail.com"
            )
        )

        self.assertEqual(
            parse_qsl(result.query, keep_blank_values=True),
            [("authuser", "owner@gmail.com")],
        )
        self.assertEqual(result.fragment, "room?authuser=fragment")

    def test_preserves_existing_pli_values_without_adding_or_replacing_them(self):
        for query in ("pli=1", "pli=0", "pli=", "pli=1&pli=0&pli="):
            with self.subTest(query=query):
                result = urlsplit(
                    app.build_meeting_url(
                        f"https://meet.google.com/abc?{query}", "owner@gmail.com"
                    )
                )
                self.assertEqual(
                    [
                        (key, value)
                        for key, value in parse_qsl(
                            result.query, keep_blank_values=True
                        )
                        if key != "authuser"
                    ],
                    parse_qsl(query, keep_blank_values=True),
                )


if __name__ == "__main__":
    unittest.main()
