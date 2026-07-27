from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import create_synthetic_mail_root
from mailgrep.envelope import (
    COCOA_EPOCH_OFFSET_SECONDS,
    EnvelopeIndexMissingError,
    EnvelopeSchemaError,
    MessageFilter,
    all_message_ids,
    count_messages,
    introspect_schema,
    list_mailbox_urls,
    open_envelope_index,
    query_messages,
)
from mailgrep.store import open_mail_store


class EnvelopeFixtureCase(unittest.TestCase):
    use_unix_timestamps = False

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.mail_root = create_synthetic_mail_root(
            Path(self.temporary_directory.name), use_unix_timestamps=self.use_unix_timestamps
        )
        self.mail_store = open_mail_store(self.mail_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def records_for(self, message_filter: MessageFilter):
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            schema = introspect_schema(connection)
            return list(query_messages(connection, schema, message_filter))


class SchemaIntrospectionTests(EnvelopeFixtureCase):
    def test_resolves_expected_columns(self):
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            schema = introspect_schema(connection)
        self.assertEqual(schema.message_columns["date_received"], "date_received")
        self.assertEqual(schema.message_columns["sender_reference"], "sender")
        self.assertEqual(schema.message_columns["subject_reference"], "subject")
        self.assertEqual(schema.message_columns["mailbox_reference"], "mailbox")
        self.assertEqual(schema.address_columns["display_name"], "comment")
        self.assertTrue(schema.has_recipients_table)

    def test_detects_cocoa_epoch(self):
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            schema = introspect_schema(connection)
        self.assertEqual(schema.timestamp_offset_seconds, COCOA_EPOCH_OFFSET_SECONDS)

    def test_raises_with_schema_dump_when_table_missing(self):
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            pass
        database_path = self.mail_store.envelope_index_path
        connection = sqlite3.connect(database_path)
        connection.execute("DROP TABLE subjects")
        connection.commit()
        connection.close()
        with open_envelope_index(database_path) as connection:
            with self.assertRaises(EnvelopeSchemaError) as caught:
                introspect_schema(connection)
        self.assertIn("subjects", str(caught.exception))
        self.assertIn("CREATE TABLE messages", str(caught.exception))

    def test_raises_when_column_layout_unrecognised(self):
        database_path = self.mail_store.envelope_index_path
        connection = sqlite3.connect(database_path)
        connection.execute("ALTER TABLE messages RENAME COLUMN sender TO originator")
        connection.commit()
        connection.close()
        with open_envelope_index(database_path) as connection:
            with self.assertRaises(EnvelopeSchemaError) as caught:
                introspect_schema(connection)
        self.assertIn("sender_reference", str(caught.exception))

    def test_missing_envelope_index_is_reported(self):
        self.mail_store.envelope_index_path.unlink()
        with self.assertRaises(EnvelopeIndexMissingError):
            with open_envelope_index(self.mail_store.envelope_index_path):
                pass


class UnixTimestampSchemaTests(EnvelopeFixtureCase):
    use_unix_timestamps = True

    def test_detects_unix_epoch_and_dates_still_resolve(self):
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            schema = introspect_schema(connection)
            records = list(query_messages(connection, schema, MessageFilter()))
        self.assertEqual(schema.timestamp_offset_seconds, 0)
        received_years = {record.date_received.year for record in records}
        self.assertEqual(received_years, {2026})


class QueryTests(EnvelopeFixtureCase):
    def test_returns_all_messages_newest_first(self):
        records = self.records_for(MessageFilter())
        self.assertEqual([record.message_id for record in records], [1005, 1004, 1003, 1002, 1001])

    def test_dates_are_converted_from_cocoa_epoch(self):
        records = self.records_for(MessageFilter(subject="Resales sample data"))
        self.assertEqual(records[0].date_received.year, 2026)
        self.assertEqual(records[0].date_received.month, 6)
        self.assertEqual(records[0].date_received.day, 15)

    def test_filters_by_sender_address(self):
        records = self.records_for(MessageFilter(sender="alec@example.com"))
        self.assertEqual({record.message_id for record in records}, {1001, 1003, 1004})

    def test_filters_by_sender_display_name(self):
        records = self.records_for(MessageFilter(sender="Katchur"))
        self.assertEqual({record.message_id for record in records}, {1001, 1003, 1004})

    def test_filters_by_subject_substring(self):
        records = self.records_for(MessageFilter(subject="contract"))
        self.assertEqual([record.message_id for record in records], [1004])

    def test_subject_includes_the_reply_prefix(self):
        records = self.records_for(MessageFilter(subject="Weekly digest"))
        self.assertEqual(records[0].subject, "Re: Weekly digest")

    def test_matches_subject_search_that_spans_the_prefix(self):
        records = self.records_for(MessageFilter(subject="Re: Weekly"))
        self.assertEqual([record.message_id for record in records], [1002])

    def test_still_matches_subject_without_the_prefix(self):
        records = self.records_for(MessageFilter(subject="digest"))
        self.assertEqual([record.message_id for record in records], [1002])

    def test_filters_by_recipient(self):
        records = self.records_for(MessageFilter(recipient="petr@example.com"))
        self.assertEqual(len(records), 5)

    def test_filters_by_mailbox_url(self):
        self.assertEqual(len(self.records_for(MessageFilter(mailbox="INBOX"))), 5)
        self.assertEqual(len(self.records_for(MessageFilter(mailbox="Archive"))), 0)

    def test_filters_by_date_range(self):
        since = datetime(2026, 6, 17, tzinfo=timezone.utc)
        records = self.records_for(MessageFilter(since=since))
        self.assertEqual({record.message_id for record in records}, {1003, 1004, 1005})
        until = datetime(2026, 6, 16, 23, 59, tzinfo=timezone.utc)
        records = self.records_for(MessageFilter(until=until))
        self.assertEqual({record.message_id for record in records}, {1001, 1002})

    def test_filters_unread_and_flagged(self):
        self.assertEqual([record.message_id for record in self.records_for(MessageFilter(unread_only=True))], [1005])
        self.assertEqual([record.message_id for record in self.records_for(MessageFilter(flagged_only=True))], [1001])

    def test_sender_display_name_is_populated(self):
        records = self.records_for(MessageFilter(subject="Resales sample data"))
        self.assertEqual(records[0].sender_name, "Alec Katchur-Marsh")
        self.assertEqual(records[0].sender_address, "alec@example.com")

    def test_counts_and_identifiers_match_fixture(self):
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            schema = introspect_schema(connection)
            self.assertEqual(count_messages(connection, schema), 5)
            self.assertEqual(all_message_ids(connection), {1001, 1002, 1003, 1004, 1005})
            mailboxes = list_mailbox_urls(connection, schema)
        self.assertEqual(len(mailboxes), 1)
        self.assertEqual(mailboxes[0][1], 5)


class SnapshotIsolationTests(EnvelopeFixtureCase):
    def test_reading_does_not_modify_the_original_store(self):
        original_bytes = self.mail_store.envelope_index_path.read_bytes()
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            introspect_schema(connection)
        self.assertEqual(self.mail_store.envelope_index_path.read_bytes(), original_bytes)

    def test_connection_is_read_only(self):
        with open_envelope_index(self.mail_store.envelope_index_path) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM messages")


if __name__ == "__main__":
    unittest.main()
