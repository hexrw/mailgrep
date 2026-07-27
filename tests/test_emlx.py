from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import DEFAULT_METADATA_PLIST, HTML_MESSAGE, PLAIN_MESSAGE, build_emlx_bytes
from mailgrep.emlx import (
    EmlxParseError,
    MessageFlags,
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


class LengthPrefixWhitespaceTests(unittest.TestCase):
    def test_accepts_space_terminated_prefix(self):
        message_bytes = PLAIN_MESSAGE.encode("utf-8")
        raw = f"{len(message_bytes)} ".encode("ascii") + message_bytes
        extracted, _ = split_emlx(raw)
        self.assertEqual(extracted, message_bytes)

    def test_accepts_crlf_terminated_prefix(self):
        message_bytes = PLAIN_MESSAGE.encode("utf-8")
        raw = f"{len(message_bytes)}\r\n".encode("ascii") + message_bytes
        extracted, _ = split_emlx(raw)
        self.assertEqual(extracted, message_bytes)

    def test_consumes_all_whitespace_after_the_length(self):
        message_bytes = PLAIN_MESSAGE.encode("utf-8")
        raw = f"  {len(message_bytes)} \t \n".encode("ascii") + message_bytes
        extracted, _ = split_emlx(raw)
        self.assertEqual(extracted, message_bytes)


class TruncatedBoundaryTests(unittest.TestCase):
    def test_repairs_single_dash_terminated_final_boundary(self):
        payload = b"Content-Type: multipart/mixed; boundary=B\n\n--B\n\nbody\n--B-"
        raw = f"{len(payload)}\n".encode("ascii") + payload + DEFAULT_METADATA_PLIST.encode("utf-8")
        extracted, metadata = split_emlx(raw)
        self.assertTrue(extracted.endswith(b"--B--"))
        self.assertEqual(metadata.get("flags"), 8623489)

    def test_leaves_correctly_terminated_boundary_alone(self):
        payload = b"Content-Type: multipart/mixed; boundary=B\n\n--B\n\nbody\n--B--"
        raw = f"{len(payload)}\n".encode("ascii") + payload + DEFAULT_METADATA_PLIST.encode("utf-8")
        extracted, _ = split_emlx(raw)
        self.assertEqual(extracted, payload)

    def test_does_not_repair_when_no_plist_follows(self):
        payload = b"some text ending in a dash -"
        raw = f"{len(payload)}\n".encode("ascii") + payload
        extracted, _ = split_emlx(raw)
        self.assertEqual(extracted, payload)


class MessageFlagsTests(unittest.TestCase):
    def test_decodes_read_and_flagged_bits(self):
        parsed = parse_emlx_bytes(build_emlx_bytes(PLAIN_MESSAGE), Path("1.emlx"))
        flags = parsed.flags
        self.assertIsNotNone(flags)
        self.assertTrue(flags.has("read"))
        self.assertFalse(flags.has("flagged"))
        self.assertFalse(flags.has("deleted"))

    def test_decodes_flagged_when_bit_four_set(self):
        flags = MessageFlags(raw_value=1 << 4)
        self.assertTrue(flags.has("flagged"))
        self.assertFalse(flags.has("read"))

    def test_attachment_count_and_priority_occupy_their_own_bit_ranges(self):
        flags = MessageFlags(raw_value=(3 << 10) | (5 << 16))
        self.assertEqual(flags.attachment_count, 3)
        self.assertEqual(flags.priority, 5)
        self.assertFalse(flags.has("read"))

    def test_junk_and_signed_bits_are_above_the_gap(self):
        self.assertTrue(MessageFlags(raw_value=1 << 23).has("signed"))
        self.assertTrue(MessageFlags(raw_value=1 << 24).has("junk"))
        self.assertTrue(MessageFlags(raw_value=1 << 25).has("not_junk"))

    def test_absent_trailer_yields_no_flags(self):
        message_bytes = PLAIN_MESSAGE.encode("utf-8")
        raw = f"{len(message_bytes)}\n".encode("ascii") + message_bytes
        parsed = parse_emlx_bytes(raw, Path("1.emlx"))
        self.assertIsNone(parsed.flags)


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
