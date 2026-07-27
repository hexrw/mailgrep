from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from enum import Enum
from pathlib import Path
from typing import Iterator

from .emlx import ParsedMessage, decode_mime_header, is_attachment_part
from .locator import ATTACHMENTS_DIRECTORY_NAME

APPLE_CONTENT_LENGTH_HEADER = "X-Apple-Content-Length"


class AttachmentSource(str, Enum):
    INLINE = "inline"
    EXTERNAL = "external"
    NOT_DOWNLOADED = "not_downloaded"
    NOT_EXTRACTABLE = "not_extractable"


@dataclass
class Attachment:
    part_id: str
    filename: str
    content_type: str
    source: AttachmentSource
    declared_size: int | None
    external_path: Path | None
    reason: str | None

    @property
    def savable(self) -> bool:
        return self.source in (AttachmentSource.INLINE, AttachmentSource.EXTERNAL)


def enumerate_numbered_parts(part: Message, prefix: str) -> Iterator[tuple[Message, str]]:
    yield part, prefix
    if part.is_multipart():
        for index, child in enumerate(part.get_payload(), start=1):
            yield from enumerate_numbered_parts(child, f"{prefix}.{index}")


def enumerate_parts(message: Message) -> Iterator[tuple[Message, str]]:
    if message.is_multipart():
        for index, child in enumerate(message.get_payload(), start=1):
            yield from enumerate_numbered_parts(child, str(index))
    else:
        yield message, "1"


def attachments_root_for(message_path: Path, message_id: int) -> Path:
    return message_path.parent.parent / ATTACHMENTS_DIRECTORY_NAME / str(message_id)


def is_contained_within(candidate: Path, container: Path) -> bool:
    try:
        candidate.resolve().relative_to(container.resolve())
    except (ValueError, OSError):
        return False
    return True


def is_ignorable_directory_entry(entry: Path) -> bool:
    return entry.name.startswith(".")


def find_external_file(attachments_root: Path, part_id: str, filename: str) -> Path | None:
    if not attachments_root.is_dir():
        return None
    exact_directory = attachments_root / part_id
    if is_contained_within(exact_directory, attachments_root) and exact_directory.is_dir():
        candidate = exact_directory / filename
        if is_contained_within(candidate, attachments_root) and candidate.is_file():
            return candidate
        for entry in sorted(exact_directory.iterdir()):
            if entry.is_file() and not is_ignorable_directory_entry(entry):
                return entry
    for part_directory in sorted(attachments_root.iterdir()):
        if not part_directory.is_dir():
            continue
        candidate = part_directory / filename
        if is_contained_within(candidate, attachments_root) and candidate.is_file():
            return candidate
    return None


def read_declared_size(part: Message) -> int | None:
    raw = part.get(APPLE_CONTENT_LENGTH_HEADER)
    if raw and str(raw).strip().isdigit():
        return int(str(raw).strip())
    return None


def part_payload_bytes(part: Message) -> bytes | None:
    try:
        return part.get_payload(decode=True)
    except Exception:
        return None


def describe_attachment(
    part: Message,
    part_id: str,
    attachments_root: Path,
) -> Attachment:
    raw_filename = part.get_filename()
    filename = decode_mime_header(raw_filename) if raw_filename else f"part-{part_id}"
    content_type = part.get_content_type()
    declared_size = read_declared_size(part)
    payload = part_payload_bytes(part)
    if payload:
        return Attachment(
            part_id=part_id,
            filename=filename,
            content_type=content_type,
            source=AttachmentSource.INLINE,
            declared_size=declared_size or len(payload),
            external_path=None,
            reason=None,
        )
    external_path = find_external_file(attachments_root, part_id, filename)
    if external_path is not None:
        return Attachment(
            part_id=part_id,
            filename=filename,
            content_type=content_type,
            source=AttachmentSource.EXTERNAL,
            declared_size=external_path.stat().st_size,
            external_path=external_path,
            reason=None,
        )
    if declared_size is not None:
        return Attachment(
            part_id=part_id,
            filename=filename,
            content_type=content_type,
            source=AttachmentSource.NOT_DOWNLOADED,
            declared_size=declared_size,
            external_path=None,
            reason=(
                f"Apple Mail has not downloaded this attachment ({declared_size} bytes declared by the server). "
                "Open the message in Mail, or set the account's Download Attachments preference to All."
            ),
        )
    return Attachment(
        part_id=part_id,
        filename=filename,
        content_type=content_type,
        source=AttachmentSource.NOT_EXTRACTABLE,
        declared_size=None,
        external_path=None,
        reason=(
            "The MIME part declares an attachment but its body is empty and no file exists in the "
            "sibling Attachments directory."
        ),
    )


def list_attachments(parsed: ParsedMessage, message_id: int) -> list[Attachment]:
    attachments_root = attachments_root_for(parsed.path, message_id)
    found: list[Attachment] = []
    for part, part_id in enumerate_parts(parsed.message):
        if part.is_multipart():
            continue
        if not is_attachment_part(part):
            continue
        found.append(describe_attachment(part, part_id, attachments_root))
    return found


def attachment_bytes(parsed: ParsedMessage, attachment: Attachment) -> bytes:
    if attachment.source is AttachmentSource.EXTERNAL and attachment.external_path is not None:
        return attachment.external_path.read_bytes()
    if attachment.source is AttachmentSource.INLINE:
        for part, part_id in enumerate_parts(parsed.message):
            if part_id != attachment.part_id:
                continue
            payload = part_payload_bytes(part)
            if payload:
                return payload
    raise AttachmentUnavailableError(attachment)


class AttachmentUnavailableError(RuntimeError):
    def __init__(self, attachment: Attachment):
        self.attachment = attachment
        detail = attachment.reason or "attachment bytes are not present on this machine"
        super().__init__(f"{attachment.filename}: {detail}")


def sanitize_output_filename(filename: str) -> str:
    base_name = Path(filename.replace("\x00", "")).name.strip()
    cleaned = base_name.replace("/", "_").lstrip(".")
    return cleaned or "attachment"


def extract_attachment(parsed: ParsedMessage, attachment: Attachment, destination_directory: Path) -> Path:
    payload = attachment_bytes(parsed, attachment)
    if not payload:
        raise AttachmentUnavailableError(attachment)
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / sanitize_output_filename(attachment.filename)
    counter = 1
    while destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        destination = destination_directory / f"{stem}-{counter}{suffix}"
        counter += 1
    destination.write_bytes(payload)
    return destination
