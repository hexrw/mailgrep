from __future__ import annotations

import plistlib
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


@dataclass
class ParsedMessage:
    path: Path
    message: Message
    metadata: dict = field(default_factory=dict)

    @property
    def is_partial(self) -> bool:
        return self.path.name.endswith(PARTIAL_EMLX_SUFFIX)

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


def split_emlx(raw: bytes) -> tuple[bytes, dict]:
    newline_position = raw.find(b"\n")
    if newline_position < 0:
        raise EmlxParseError("missing length prefix line")
    length_token = raw[:newline_position].strip()
    if not length_token.isdigit():
        raise EmlxParseError(f"length prefix is not numeric: {length_token!r}")
    declared_length = int(length_token)
    message_start = newline_position + 1
    message_end = message_start + declared_length
    message_bytes = raw[message_start:message_end]
    trailer = raw[message_end:].strip()
    metadata: dict = {}
    if trailer:
        try:
            loaded = plistlib.loads(trailer)
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
