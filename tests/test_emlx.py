from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import HTML_MESSAGE, PLAIN_MESSAGE, build_emlx_bytes
from mailgrep.emlx import (
    EmlxParseError,
    html_to_text,
    message_id_from_filename,
    parse_emlx_bytes,
    split_emlx,
)


class SplitEmlxTests(unittest.TestCase):
    def test_extracts_message_using_declared_length(self):
        raw = build_emlx_bytes(PLAIN_MESSAGE)
        message_bytes, metadata = split_emlx(raw)
        self.assertEqual(message_bytes.decode("utf-8"), PLAIN_MESSAGE)
        self.assertEqual(metadata.get("flags"), 8623489)

    def test_ignores_trailing_bytes_beyond_declared_length(self):
        raw = build_emlx_bytes(PLAIN_MESSAGE)
        message_bytes, _ = split_emlx(raw + b"garbage")
        self.assertEqual(message_bytes.decode("utf-8"), PLAIN_MESSAGE)

    def test_tolerates_missing_plist_trailer(self):
        message_bytes = PLAIN_MESSAGE.encode("utf-8")
        raw = f"{len(message_bytes)}\n".encode("ascii") + message_bytes
        extracted, metadata = split_emlx(raw)
        self.assertEqual(extracted, message_bytes)
        self.assertEqual(metadata, {})

    def test_tolerates_unparsable_plist_trailer(self):
        message_bytes = PLAIN_MESSAGE.encode("utf-8")
        raw = f"{len(message_bytes)}\n".encode("ascii") + message_bytes + b"<not a plist>"
        _, metadata = split_emlx(raw)
        self.assertEqual(metadata, {})

    def test_rejects_non_numeric_length_prefix(self):
        with self.assertRaises(EmlxParseError):
            split_emlx(b"not-a-number\nbody")

    def test_rejects_missing_newline(self):
        with self.assertRaises(EmlxParseError):
            split_emlx(b"12345")

    def test_accepts_padded_length_prefix(self):
        message_bytes = PLAIN_MESSAGE.encode("utf-8")
        raw = f"  {len(message_bytes)}  \n".encode("ascii") + message_bytes
        extracted, _ = split_emlx(raw)
        self.assertEqual(extracted, message_bytes)

    def test_multibyte_body_length_is_measured_in_bytes(self):
        body = "Subject: Ünïcode\n\nBody with émojis 🎉\n"
        raw = build_emlx_bytes(body)
        extracted, _ = split_emlx(raw)
        self.assertEqual(extracted.decode("utf-8"), body)


class ParsedMessageTests(unittest.TestCase):
    def test_reads_headers_and_plain_body(self):
        parsed = parse_emlx_bytes(build_emlx_bytes(PLAIN_MESSAGE), Path("1001.emlx"))
        self.assertEqual(parsed.header("Subject"), "Resales sample data")
        self.assertIn("alec@example.com", parsed.header("From"))
        self.assertIn("resales rollout timeline", parsed.text_body())
        self.assertFalse(parsed.is_partial)

    def test_falls_back_to_html_when_no_plain_part(self):
        parsed = parse_emlx_bytes(build_emlx_bytes(HTML_MESSAGE), Path("1002.emlx"))
        body = parsed.text_body()
        self.assertIn("Totals updated", body)
        self.assertIn("Second paragraph here", body)
        self.assertNotIn("ignored()", body)
        self.assertNotIn("color:red", body)

    def test_detects_partial_filename(self):
        parsed = parse_emlx_bytes(build_emlx_bytes(PLAIN_MESSAGE), Path("1004.partial.emlx"))
        self.assertTrue(parsed.is_partial)
        self.assertEqual(parsed.message_id, 1004)


class HtmlTextTests(unittest.TestCase):
    def test_strips_tags_and_collapses_blank_lines(self):
        text = html_to_text("<div>one</div><p>two</p><br><span>three</span>")
        self.assertEqual(text, "one\ntwo\nthree")

    def test_decodes_character_references(self):
        self.assertEqual(html_to_text("<p>a &amp; b &nbsp;c</p>").replace("\xa0", " "), "a & b  c")


class FilenameTests(unittest.TestCase):
    def test_parses_plain_and_partial_names(self):
        self.assertEqual(message_id_from_filename(Path("42.emlx")), 42)
        self.assertEqual(message_id_from_filename(Path("42.partial.emlx")), 42)

    def test_rejects_unrelated_names(self):
        self.assertIsNone(message_id_from_filename(Path("notes.txt")))
        self.assertIsNone(message_id_from_filename(Path("abc.emlx")))


if __name__ == "__main__":
    unittest.main()
