from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as default_policy
from html.parser import HTMLParser
from pathlib import Path

EMLX_SUFFIX = ".emlx"
PARTIAL_EMLX_SUFFIX = ".partial.emlx"


class EmlxParseError(RuntimeError):
    pass


class HtmlTextExtractor(HTMLParser):
    SKIPPED_TAGS = frozenset({"script", "style", "head"})
    BREAKING_TAGS = frozenset({"p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.suppression_depth = 0

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIPPED_TAGS:
            self.suppression_depth += 1
        elif tag in self.BREAKING_TAGS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED_TAGS and self.suppression_depth > 0:
            self.suppression_depth -= 1
        elif tag in self.BREAKING_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if self.suppression_depth == 0:
            self.fragments.append(data)

    def text(self) -> str:
        joined = "".join(self.fragments)
        collapsed_lines = [line.strip() for line in joined.splitlines()]
        return "\n".join(line for line in collapsed_lines if line)


def html_to_text(html: str) -> str:
    extractor = HtmlTextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


FLAG_BIT_POSITIONS = {
    "read": 0,
    "deleted": 1,
    "answered": 2,
    "encrypted": 3,
    "flagged": 4,
    "recent": 5,
    "draft": 6,
    "initial": 7,
    "forwarded": 8,
    "redirected": 9,
    "signed": 23,
    "junk": 24,
    "not_junk": 25,
}
ATTACHMENT_COUNT_BIT_OFFSET = 10
ATTACHMENT_COUNT_BIT_WIDTH = 6
PRIORITY_BIT_OFFSET = 16
PRIORITY_BIT_WIDTH = 7


@dataclass(frozen=True)
class MessageFlags:
    raw_value: int

    def has(self, name: str) -> bool:
        position = FLAG_BIT_POSITIONS[name]
        return bool(self.raw_value >> position & 1)

    @property
    def attachment_count(self) -> int:
        return self.raw_value >> ATTACHMENT_COUNT_BIT_OFFSET & (2**ATTACHMENT_COUNT_BIT_WIDTH - 1)

    @property
    def priority(self) -> int:
        return self.raw_value >> PRIORITY_BIT_OFFSET & (2**PRIORITY_BIT_WIDTH - 1)


@dataclass
class ParsedMessage:
    path: Path
    message: Message
    metadata: dict = field(default_factory=dict)

    @property
    def is_partial(self) -> bool:
        return self.path.name.endswith(PARTIAL_EMLX_SUFFIX)

    @property
    def flags(self) -> MessageFlags | None:
        raw_value = self.metadata.get("flags")
        if not isinstance(raw_value, int):
            return None
        return MessageFlags(raw_value=raw_value)

    @property
    def message_id(self) -> int | None:
        return message_id_from_filename(self.path)

    def header(self, name: str) -> str:
        raw = self.message.get(name)
        if raw is None:
            return ""
        return decode_mime_header(str(raw))

    def text_body(self) -> str:
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in self.message.walk():
            if part.get_content_maintype() != "text":
                continue
            if is_attachment_part(part):
                continue
            content = decode_text_part(part)
            if not content:
                continue
            if part.get_content_subtype() == "html":
                html_parts.append(content)
            else:
                plain_parts.append(content)
        if plain_parts:
            return "\n\n".join(plain_parts).strip()
        if html_parts:
            return html_to_text("\n\n".join(html_parts)).strip()
        return ""


def decode_mime_header(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


def is_attachment_part(part: Message) -> bool:
    disposition = part.get_content_disposition()
    if disposition == "attachment":
        return True
    return disposition == "inline" and bool(part.get_filename())


def decode_text_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def message_id_from_filename(path: Path) -> int | None:
    name = path.name
    if name.endswith(PARTIAL_EMLX_SUFFIX):
        stem = name[: -len(PARTIAL_EMLX_SUFFIX)]
    elif name.endswith(EMLX_SUFFIX):
        stem = name[: -len(EMLX_SUFFIX)]
    else:
        return None
    return int(stem) if stem.isdigit() else None


LENGTH_PREFIX_PATTERN = re.compile(rb"^\s*(\d+)\s+")
PLIST_TRAILER_MARKER = b"<?xml"
TRUNCATED_BOUNDARY_PEEK_LENGTH = 5


def repair_truncated_boundary(message_bytes: bytes, trailer: bytes) -> bytes:
    if not message_bytes.endswith(b"-") or message_bytes.endswith(b"--"):
        return message_bytes
    if trailer[:TRUNCATED_BOUNDARY_PEEK_LENGTH].lstrip()[: len(PLIST_TRAILER_MARKER)] != PLIST_TRAILER_MARKER:
        return message_bytes
    return message_bytes + b"-"


def split_emlx(raw: bytes) -> tuple[bytes, dict]:
    match = LENGTH_PREFIX_PATTERN.match(raw)
    if match is None:
        raise EmlxParseError("content did not start with a decimal payload length")
    declared_length = int(match.group(1))
    message_start = match.end()
    message_end = message_start + declared_length
    message_bytes = raw[message_start:message_end]
    trailer = raw[message_end:]
    message_bytes = repair_truncated_boundary(message_bytes, trailer)
    metadata: dict = {}
    stripped_trailer = trailer.strip()
    if stripped_trailer:
        try:
            loaded = plistlib.loads(stripped_trailer)
            if isinstance(loaded, dict):
                metadata = loaded
        except Exception:
            metadata = {}
    return message_bytes, metadata


def parse_emlx_bytes(raw: bytes, path: Path) -> ParsedMessage:
    message_bytes, metadata = split_emlx(raw)
    message = message_from_bytes(message_bytes, policy=default_policy)
    return ParsedMessage(path=path, message=message, metadata=metadata)


def read_emlx(path: Path) -> ParsedMessage:
    try:
        raw = path.read_bytes()
    except PermissionError:
        from .store import FullDiskAccessError

        raise FullDiskAccessError(path) from None
    return parse_emlx_bytes(raw, path)
