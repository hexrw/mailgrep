from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import EXTERNAL_ATTACHMENT_CONTENT, create_synthetic_mail_root
from mailgrep.attachments import (
    AttachmentSource,
    AttachmentUnavailableError,
    attachment_bytes,
    extract_attachment,
    is_contained_within,
    list_attachments,
    sanitize_output_filename,
)
from mailgrep.emlx import read_emlx
from mailgrep.locator import build_locator
from mailgrep.store import open_mail_store


class AttachmentFixtureCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.mail_root = create_synthetic_mail_root(base)
        self.cache_directory = base / "cache"
        self.mail_store = open_mail_store(self.mail_root)
        self.locator = build_locator(self.mail_store, cache_directory=self.cache_directory)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def load(self, message_id: int):
        path = self.locator.path_for(message_id)
        self.assertIsNotNone(path, f"no path located for message {message_id}")
        return read_emlx(path)


class InlineAttachmentTests(AttachmentFixtureCase):
    def test_inline_attachment_is_savable_with_correct_bytes(self):
        parsed = self.load(1003)
        attachments = list_attachments(parsed, 1003)
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment.filename, "totals.csv")
        self.assertIs(attachment.source, AttachmentSource.INLINE)
        self.assertTrue(attachment.savable)
        self.assertEqual(attachment_bytes(parsed, attachment), b"pid,total\n123,456\n")

    def test_text_part_is_not_reported_as_attachment(self):
        parsed = self.load(1003)
        filenames = [attachment.filename for attachment in list_attachments(parsed, 1003)]
        self.assertEqual(filenames, ["totals.csv"])


class ExternalAttachmentTests(AttachmentFixtureCase):
    def test_stripped_attachment_resolves_to_sibling_file(self):
        parsed = self.load(1004)
        attachments = list_attachments(parsed, 1004)
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment.filename, "contract.pdf")
        self.assertIs(attachment.source, AttachmentSource.EXTERNAL)
        self.assertTrue(attachment.savable)
        self.assertEqual(attachment.declared_size, len(EXTERNAL_ATTACHMENT_CONTENT))

    def test_extraction_writes_full_bytes_never_zero_length(self):
        parsed = self.load(1004)
        attachment = list_attachments(parsed, 1004)[0]
        with tempfile.TemporaryDirectory() as destination:
            written = extract_attachment(parsed, attachment, Path(destination))
            self.assertTrue(written.exists())
            self.assertGreater(written.stat().st_size, 0)
            self.assertEqual(written.read_bytes(), EXTERNAL_ATTACHMENT_CONTENT)

    def test_repeated_extraction_does_not_overwrite(self):
        parsed = self.load(1004)
        attachment = list_attachments(parsed, 1004)[0]
        with tempfile.TemporaryDirectory() as destination:
            first = extract_attachment(parsed, attachment, Path(destination))
            second = extract_attachment(parsed, attachment, Path(destination))
            self.assertNotEqual(first, second)
            self.assertEqual(second.read_bytes(), EXTERNAL_ATTACHMENT_CONTENT)


class NotDownloadedAttachmentTests(AttachmentFixtureCase):
    def test_placeholder_is_reported_as_not_downloaded(self):
        parsed = self.load(1005)
        attachments = list_attachments(parsed, 1005)
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment.filename, "deck.ppt")
        self.assertIs(attachment.source, AttachmentSource.NOT_DOWNLOADED)
        self.assertFalse(attachment.savable)
        self.assertEqual(attachment.declared_size, 1835008)
        self.assertIn("not downloaded", attachment.reason)

    def test_extraction_refuses_rather_than_writing_empty_file(self):
        parsed = self.load(1005)
        attachment = list_attachments(parsed, 1005)[0]
        with tempfile.TemporaryDirectory() as destination:
            with self.assertRaises(AttachmentUnavailableError):
                extract_attachment(parsed, attachment, Path(destination))
            self.assertEqual(list(Path(destination).iterdir()), [])

    def test_missing_external_file_without_placeholder_is_not_extractable(self):
        parsed = self.load(1004)
        attachments_root = parsed.path.parent.parent / "Attachments" / "1004" / "2"
        for entry in attachments_root.iterdir():
            entry.unlink()
        attachment = list_attachments(parsed, 1004)[0]
        self.assertIs(attachment.source, AttachmentSource.NOT_EXTRACTABLE)
        self.assertFalse(attachment.savable)


class PathSafetyTests(unittest.TestCase):
    def test_containment_check_rejects_escaping_paths(self):
        with tempfile.TemporaryDirectory() as base:
            container = Path(base) / "attachments"
            container.mkdir()
            self.assertTrue(is_contained_within(container / "2" / "file.pdf", container))
            self.assertFalse(is_contained_within(container / ".." / "escaped.pdf", container))

    def test_output_filename_is_flattened(self):
        self.assertEqual(sanitize_output_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_output_filename("a/b/c.pdf"), "c.pdf")
        self.assertEqual(sanitize_output_filename(""), "attachment")


if __name__ == "__main__":
    unittest.main()
