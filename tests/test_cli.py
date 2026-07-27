from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import (
    DEFAULT_METADATA_PLIST_TEMPLATE,
    EXTERNAL_ATTACHMENT_CONTENT,
    create_synthetic_mail_root,
)
from mailgrep.cli import main
from mailgrep.locator import CACHE_DIRECTORY_ENVIRONMENT_VARIABLE


class CommandLineCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.mail_root = create_synthetic_mail_root(base)
        self.previous_cache_directory = os.environ.get(CACHE_DIRECTORY_ENVIRONMENT_VARIABLE)
        os.environ[CACHE_DIRECTORY_ENVIRONMENT_VARIABLE] = str(base / "cache")

    def tearDown(self):
        if self.previous_cache_directory is None:
            os.environ.pop(CACHE_DIRECTORY_ENVIRONMENT_VARIABLE, None)
        else:
            os.environ[CACHE_DIRECTORY_ENVIRONMENT_VARIABLE] = self.previous_cache_directory
        self.temporary_directory.cleanup()

    def run_command(self, *arguments: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["--mail-root", str(self.mail_root), *arguments])
        return exit_code, buffer.getvalue()

    def run_json_command(self, *arguments: str) -> tuple[int, object]:
        exit_code, output = self.run_command("--json", *arguments)
        return exit_code, json.loads(output)

    def message_path(self, message_id: int) -> Path:
        matches = list(self.mail_root.rglob(f"{message_id}.*emlx"))
        self.assertEqual(len(matches), 1)
        return matches[0]


class DoctorTests(CommandLineCase):
    def test_reports_complete_coverage_on_intact_store(self):
        exit_code, report = self.run_json_command("doctor")
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["access_state"], "readable")
        self.assertTrue(report["schema_ok"])
        self.assertTrue(report["coverage_complete"])
        self.assertEqual(report["messages_indexed"], 5)
        self.assertEqual(report["messages_on_disk"], 5)
        self.assertEqual(report["readable_message_count"], 5)

    def test_text_output_names_the_version_directory_and_accounts(self):
        exit_code, output = self.run_command("doctor")
        self.assertEqual(exit_code, 0)
        self.assertIn("V10", output)
        self.assertIn("coverage:               complete", output)
        self.assertIn("imap://petr@example.com@imap.example.com", output)

    def test_detects_indexed_message_with_no_file_on_disk(self):
        self.message_path(1002).unlink()
        exit_code, report = self.run_json_command("doctor", "--reindex")
        self.assertEqual(exit_code, 4)
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(report["indexed_without_file_count"], 1)
        self.assertEqual(report["indexed_without_file_sample"], [1002])
        self.assertEqual(report["readable_message_count"], 4)

    def test_detects_file_on_disk_missing_from_index(self):
        messages_directory = self.message_path(1001).parent
        (messages_directory / "1009.emlx").write_bytes(b"5\nhello")
        exit_code, report = self.run_json_command("doctor", "--reindex")
        self.assertEqual(exit_code, 4)
        self.assertEqual(report["file_without_index_count"], 1)
        self.assertEqual(report["file_without_index_sample"], [1009])

    def test_missing_mail_root_exits_with_access_code(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(["--mail-root", str(self.mail_root / "absent"), "doctor"])
        self.assertEqual(exit_code, 2)
        self.assertIn("mail_root_missing", buffer.getvalue())


class SearchTests(CommandLineCase):
    def test_lists_every_message_by_default(self):
        exit_code, payload = self.run_json_command("search")
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["match_count"], 5)
        self.assertEqual(payload["examined_count"], 5)
        self.assertFalse(payload["truncated_by_limit"])

    def test_filters_by_sender(self):
        _, payload = self.run_json_command("search", "--from", "alec@example.com")
        identifiers = [match["message_id"] for match in payload["matches"]]
        self.assertEqual(sorted(identifiers), [1001, 1003, 1004])

    def test_filters_by_subject_and_date(self):
        _, payload = self.run_json_command("search", "--subject", "contract", "--since", "2026-06-18")
        self.assertEqual([match["message_id"] for match in payload["matches"]], [1004])

    def test_body_search_reads_message_files(self):
        _, payload = self.run_json_command("search", "--body", "rollout timeline")
        self.assertEqual([match["message_id"] for match in payload["matches"]], [1001])
        self.assertEqual(payload["examined_count"], 5)

    def test_body_search_matches_html_only_messages(self):
        _, payload = self.run_json_command("search", "--body", "Second paragraph")
        self.assertEqual([match["message_id"] for match in payload["matches"]], [1002])

    def test_body_search_is_case_insensitive(self):
        _, payload = self.run_json_command("search", "--body", "ROLLOUT TIMELINE")
        self.assertEqual([match["message_id"] for match in payload["matches"]], [1001])

    def test_limit_is_reported_as_truncation(self):
        exit_code, output = self.run_command("search", "--limit", "2")
        self.assertEqual(exit_code, 0)
        self.assertIn("stopped at --limit 2", output)
        _, payload = self.run_json_command("search", "--limit", "2")
        self.assertTrue(payload["truncated_by_limit"])
        self.assertEqual(payload["match_count"], 2)

    def test_unreadable_messages_are_reported_not_hidden(self):
        self.message_path(1001).unlink()
        exit_code, output = self.run_command("search", "--body", "anything")
        self.assertEqual(exit_code, 0)
        self.assertIn("could not be read", output)
        _, payload = self.run_json_command("search", "--body", "anything")
        self.assertEqual(payload["unreadable_count"], 1)
        self.assertEqual(payload["unreadable_sample"], [1001])

    def test_no_matches_still_reports_examined_count(self):
        _, payload = self.run_json_command("search", "--subject", "nothing matches this")
        self.assertEqual(payload["match_count"], 0)
        self.assertEqual(payload["examined_count"], 0)


ORPHAN_MESSAGE = """From: Draft Author <drafts@example.com>
To: Petr <petr@example.com>
Subject: Unindexed draft about pricing
Date: Sat, 20 Jun 2026 10:00:00 +0000
Message-ID: <orphan-777@example.com>
Content-Type: text/plain; charset="utf-8"

This draft never made it into the envelope index.
"""


class UnindexedCoverageTests(CommandLineCase):
    def write_orphan(self, message_id: int = 7777) -> None:
        from fixtures import build_emlx_bytes

        messages_directory = self.message_path(1001).parent
        (messages_directory / f"{message_id}.emlx").write_bytes(build_emlx_bytes(ORPHAN_MESSAGE))

    def test_default_search_includes_messages_absent_from_index(self):
        self.write_orphan()
        _, payload = self.run_json_command("search")
        identifiers = [match["message_id"] for match in payload["matches"]]
        self.assertIn(7777, identifiers)
        self.assertEqual(payload["unindexed_match_count"], 1)

    def test_unindexed_matches_are_labelled_in_text_output(self):
        self.write_orphan()
        _, output = self.run_command("search", "--subject", "pricing")
        self.assertIn("(unindexed)", output)
        self.assertIn("read directly from disk", output)

    def test_unindexed_messages_respect_sender_filter(self):
        self.write_orphan()
        _, payload = self.run_json_command("search", "--from", "drafts@example.com")
        self.assertEqual([match["message_id"] for match in payload["matches"]], [7777])
        self.assertEqual(payload["matches"][0]["source"], "disk")

    def test_unindexed_messages_respect_subject_and_date_filters(self):
        self.write_orphan()
        _, payload = self.run_json_command("search", "--subject", "pricing", "--since", "2026-06-20")
        self.assertEqual([match["message_id"] for match in payload["matches"]], [7777])
        _, payload = self.run_json_command("search", "--subject", "pricing", "--since", "2026-06-21")
        self.assertEqual(payload["matches"], [])

    def test_unindexed_messages_are_body_searchable(self):
        self.write_orphan()
        _, payload = self.run_json_command("search", "--body", "never made it into the envelope")
        self.assertEqual([match["message_id"] for match in payload["matches"]], [7777])

    def test_unindexed_messages_carry_mailbox_path(self):
        self.write_orphan()
        _, payload = self.run_json_command("search", "--from", "drafts@example.com")
        self.assertEqual(payload["matches"][0]["mailbox_url"], "INBOX")

    def test_indexed_only_excludes_them_and_says_so(self):
        self.write_orphan()
        exit_code, output = self.run_command("search", "--indexed-only")
        self.assertEqual(exit_code, 0)
        self.assertNotIn("(unindexed)", output)
        self.assertIn("--indexed-only was set", output)

    def test_read_state_comes_from_the_emlx_flags_trailer(self):
        self.write_orphan()
        _, payload = self.run_json_command("search", "--unread")
        self.assertEqual(payload["skipped_unindexed_count"], 0)
        self.assertNotIn(7777, [match["message_id"] for match in payload["matches"]])

    def test_unread_orphan_is_found_when_flags_say_unread(self):
        from fixtures import build_emlx_bytes

        unread_plist = DEFAULT_METADATA_PLIST_TEMPLATE.format(flags=8623488)
        messages_directory = self.message_path(1001).parent
        (messages_directory / "7779.emlx").write_bytes(build_emlx_bytes(ORPHAN_MESSAGE, unread_plist))
        _, payload = self.run_json_command("search", "--unread")
        self.assertIn(7779, [match["message_id"] for match in payload["matches"]])

    def test_orphan_without_flags_trailer_is_counted_not_silently_dropped(self):
        message_bytes = ORPHAN_MESSAGE.encode("utf-8")
        messages_directory = self.message_path(1001).parent
        (messages_directory / "7780.emlx").write_bytes(
            f"{len(message_bytes)}\n".encode("ascii") + message_bytes
        )
        _, payload = self.run_json_command("search", "--unread")
        self.assertEqual(payload["skipped_unindexed_count"], 1)
        _, output = self.run_command("search", "--unread")
        self.assertIn("cannot be evaluated", output)

    def test_unparsable_orphan_is_reported_not_silently_dropped(self):
        messages_directory = self.message_path(1001).parent
        (messages_directory / "7778.emlx").write_bytes(b"not-a-length-prefix\nrubbish")
        _, payload = self.run_json_command("search")
        self.assertIn(7778, payload["unreadable_sample"])


class ReadTests(CommandLineCase):
    def test_prints_headers_and_body(self):
        exit_code, output = self.run_command("read", "1001")
        self.assertEqual(exit_code, 0)
        self.assertIn("Subject: Resales sample data", output)
        self.assertIn("resales rollout timeline", output)

    def test_headers_only_omits_body(self):
        _, output = self.run_command("read", "1001", "--headers-only")
        self.assertIn("Subject: Resales sample data", output)
        self.assertNotIn("resales rollout timeline", output)

    def test_reports_attachment_state_inline(self):
        _, payload = self.run_json_command("read", "1005")
        self.assertEqual(payload["attachments"][0]["source"], "not_downloaded")
        self.assertFalse(payload["attachments"][0]["savable"])
        self.assertTrue(payload["is_partial"])

    def test_unknown_message_id_fails_cleanly(self):
        exit_code, _ = self.run_command("read", "999999")
        self.assertEqual(exit_code, 1)


class AttachmentCommandTests(CommandLineCase):
    def test_lists_attachment_sources(self):
        _, payload = self.run_json_command("attachments", "1004")
        self.assertEqual(payload["attachments"][0]["source"], "external")
        self.assertTrue(payload["attachments"][0]["savable"])

    def test_extracts_external_attachment_bytes(self):
        with tempfile.TemporaryDirectory() as destination:
            exit_code, payload = self.run_json_command("attachments", "1004", "--extract", destination)
            self.assertEqual(exit_code, 0)
            written = Path(payload["attachments"][0]["extracted_to"])
            self.assertEqual(written.read_bytes(), EXTERNAL_ATTACHMENT_CONTENT)

    def test_extracts_inline_attachment_bytes(self):
        with tempfile.TemporaryDirectory() as destination:
            exit_code, payload = self.run_json_command("attachments", "1003", "--extract", destination)
            self.assertEqual(exit_code, 0)
            written = Path(payload["attachments"][0]["extracted_to"])
            self.assertEqual(written.read_bytes(), b"pid,total\n123,456\n")

    def test_not_downloaded_attachment_exits_nonzero_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as destination:
            exit_code, payload = self.run_json_command("attachments", "1005", "--extract", destination)
            self.assertEqual(exit_code, 5)
            self.assertIsNone(payload["attachments"][0]["extracted_to"])
            self.assertEqual(list(Path(destination).iterdir()), [])

    def test_message_without_attachments_reports_none(self):
        exit_code, output = self.run_command("attachments", "1001")
        self.assertEqual(exit_code, 0)
        self.assertIn("no attachments", output)


class AccountCommandTests(CommandLineCase):
    def test_groups_mailboxes_into_accounts(self):
        _, payload = self.run_json_command("accounts")
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["username"], "petr@example.com")
        self.assertEqual(payload[0]["host"], "imap.example.com")
        self.assertEqual(payload[0]["message_count"], 5)

    def test_lists_mailboxes_with_counts(self):
        _, payload = self.run_json_command("mailboxes")
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["message_count"], 5)
        self.assertIn("INBOX", payload[0]["display_name"])


if __name__ == "__main__":
    unittest.main()
