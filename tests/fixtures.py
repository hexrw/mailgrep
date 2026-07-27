from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

COCOA_EPOCH_OFFSET_SECONDS = 978307200
ACCOUNT_DIRECTORY_NAME = "1A2B3C4D-0000-0000-0000-000000000001"
MAILBOX_BUNDLE_UUID = "9F8E7D6C-0000-0000-0000-000000000002"

ENVELOPE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE messages (
        ROWID INTEGER PRIMARY KEY,
        message_id INTEGER,
        remote_id INTEGER,
        sender INTEGER,
        subject INTEGER,
        date_sent INTEGER,
        date_received INTEGER,
        mailbox INTEGER,
        flags INTEGER,
        read INTEGER,
        flagged INTEGER,
        deleted INTEGER,
        size INTEGER,
        conversation_id INTEGER
    )
    """,
    "CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT, normalized_subject TEXT)",
    "CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT)",
    """
    CREATE TABLE mailboxes (
        ROWID INTEGER PRIMARY KEY,
        url TEXT,
        total_count INTEGER,
        unread_count INTEGER,
        deleted_count INTEGER
    )
    """,
    """
    CREATE TABLE recipients (
        ROWID INTEGER PRIMARY KEY,
        message_id INTEGER,
        type INTEGER,
        address_id INTEGER,
        position INTEGER
    )
    """,
)


def to_cocoa_seconds(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp() - COCOA_EPOCH_OFFSET_SECONDS)


def build_emlx_bytes(message_text: str, metadata_plist: str | None = None) -> bytes:
    message_bytes = message_text.encode("utf-8")
    trailer = (metadata_plist or DEFAULT_METADATA_PLIST).encode("utf-8")
    return f"{len(message_bytes)}\n".encode("ascii") + message_bytes + trailer


DEFAULT_METADATA_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict><key>flags</key><integer>8623489</integer></dict>
</plist>
"""

PLAIN_MESSAGE = """From: Alec Katchur-Marsh <alec@example.com>
To: Petr <petr@example.com>
Subject: Resales sample data
Date: Mon, 15 Jun 2026 09:14:00 +0000
Message-ID: <plain-001@example.com>
Content-Type: text/plain; charset="utf-8"

Here is the note about the resales rollout timeline.
Nothing attached to this one.
"""

HTML_MESSAGE = """From: Notifications <noreply@example.com>
To: Petr <petr@example.com>
Subject: Weekly digest
Date: Tue, 16 Jun 2026 07:00:00 +0000
Message-ID: <html-002@example.com>
Content-Type: text/html; charset="utf-8"

<html><head><style>p{color:red}</style></head><body>
<p>Totals updated</p><div>Second paragraph here</div>
<script>ignored()</script>
</body></html>
"""

INLINE_ATTACHMENT_MESSAGE = """From: Alec Katchur-Marsh <alec@example.com>
To: Petr <petr@example.com>
Subject: Inline spreadsheet
Date: Wed, 17 Jun 2026 11:30:00 +0000
Message-ID: <inline-003@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY3"

--BOUNDARY3
Content-Type: text/plain; charset="utf-8"

Spreadsheet is attached inline.
--BOUNDARY3
Content-Type: text/csv; name="totals.csv"
Content-Disposition: attachment; filename="totals.csv"
Content-Transfer-Encoding: base64

cGlkLHRvdGFsCjEyMyw0NTYK
--BOUNDARY3--
"""

STRIPPED_ATTACHMENT_MESSAGE = """From: Alec Katchur-Marsh <alec@example.com>
To: Petr <petr@example.com>
Subject: Signed contract
Date: Thu, 18 Jun 2026 15:45:00 +0000
Message-ID: <stripped-004@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY4"

--BOUNDARY4
Content-Type: text/plain; charset="utf-8"

Contract attached, please review.
--BOUNDARY4
Content-Type: application/pdf; name="contract.pdf"
Content-Disposition: attachment; filename="contract.pdf"
Content-Transfer-Encoding: base64

--BOUNDARY4--
"""

NOT_DOWNLOADED_ATTACHMENT_MESSAGE = """From: Vendor <vendor@example.com>
To: Petr <petr@example.com>
Subject: Large deck not downloaded
Date: Fri, 19 Jun 2026 08:20:00 +0000
Message-ID: <notdownloaded-005@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY5"

--BOUNDARY5
Content-Type: text/plain; charset="utf-8"

Deck is attached but Mail never fetched it.
--BOUNDARY5
Content-Type: application/vnd.ms-powerpoint; name="deck.ppt"
Content-Disposition: attachment; filename="deck.ppt"
Content-Transfer-Encoding: base64
X-Apple-Content-Length: 1835008

--BOUNDARY5--
"""

EXTERNAL_ATTACHMENT_CONTENT = b"%PDF-1.7 synthetic contract bytes for regression coverage\n"


@dataclass
class SyntheticMessage:
    message_id: int
    subject: str
    sender_address: str
    sender_name: str
    received: datetime
    filename: str
    raw_message: str
    external_attachments: dict[str, tuple[str, bytes]] = field(default_factory=dict)


SYNTHETIC_MESSAGES = [
    SyntheticMessage(
        message_id=1001,
        subject="Resales sample data",
        sender_address="alec@example.com",
        sender_name="Alec Katchur-Marsh",
        received=datetime(2026, 6, 15, 9, 14),
        filename="1001.emlx",
        raw_message=PLAIN_MESSAGE,
    ),
    SyntheticMessage(
        message_id=1002,
        subject="Weekly digest",
        sender_address="noreply@example.com",
        sender_name="Notifications",
        received=datetime(2026, 6, 16, 7, 0),
        filename="1002.emlx",
        raw_message=HTML_MESSAGE,
    ),
    SyntheticMessage(
        message_id=1003,
        subject="Inline spreadsheet",
        sender_address="alec@example.com",
        sender_name="Alec Katchur-Marsh",
        received=datetime(2026, 6, 17, 11, 30),
        filename="1003.emlx",
        raw_message=INLINE_ATTACHMENT_MESSAGE,
    ),
    SyntheticMessage(
        message_id=1004,
        subject="Signed contract",
        sender_address="alec@example.com",
        sender_name="Alec Katchur-Marsh",
        received=datetime(2026, 6, 18, 15, 45),
        filename="1004.partial.emlx",
        raw_message=STRIPPED_ATTACHMENT_MESSAGE,
        external_attachments={"2": ("contract.pdf", EXTERNAL_ATTACHMENT_CONTENT)},
    ),
    SyntheticMessage(
        message_id=1005,
        subject="Large deck not downloaded",
        sender_address="vendor@example.com",
        sender_name="Vendor",
        received=datetime(2026, 6, 19, 8, 20),
        filename="1005.partial.emlx",
        raw_message=NOT_DOWNLOADED_ATTACHMENT_MESSAGE,
    ),
]

MAILBOX_URL = "imap://petr%40example.com@imap.example.com/INBOX"


def mailbox_data_directory(version_directory: Path) -> Path:
    return (
        version_directory
        / ACCOUNT_DIRECTORY_NAME
        / "INBOX.mbox"
        / MAILBOX_BUNDLE_UUID
        / "Data"
    )


def write_messages(version_directory: Path) -> None:
    data_directory = mailbox_data_directory(version_directory)
    messages_directory = data_directory / "Messages"
    messages_directory.mkdir(parents=True, exist_ok=True)
    for synthetic in SYNTHETIC_MESSAGES:
        (messages_directory / synthetic.filename).write_bytes(build_emlx_bytes(synthetic.raw_message))
        for part_id, (filename, content) in synthetic.external_attachments.items():
            attachment_directory = data_directory / "Attachments" / str(synthetic.message_id) / part_id
            attachment_directory.mkdir(parents=True, exist_ok=True)
            (attachment_directory / filename).write_bytes(content)


def write_envelope_index(version_directory: Path, use_unix_timestamps: bool = False) -> None:
    mail_data_directory = version_directory / "MailData"
    mail_data_directory.mkdir(parents=True, exist_ok=True)
    database_path = mail_data_directory / "Envelope Index"
    connection = sqlite3.connect(database_path)
    try:
        for statement in ENVELOPE_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO mailboxes (ROWID, url, total_count, unread_count, deleted_count) VALUES (?, ?, ?, ?, ?)",
            (1, MAILBOX_URL, len(SYNTHETIC_MESSAGES), 1, 0),
        )
        address_rowids: dict[str, int] = {}
        subject_rowids: dict[str, int] = {}
        for synthetic in SYNTHETIC_MESSAGES:
            if synthetic.sender_address not in address_rowids:
                rowid = len(address_rowids) + 1
                address_rowids[synthetic.sender_address] = rowid
                connection.execute(
                    "INSERT INTO addresses (ROWID, address, comment) VALUES (?, ?, ?)",
                    (rowid, synthetic.sender_address, synthetic.sender_name),
                )
            if synthetic.subject not in subject_rowids:
                rowid = len(subject_rowids) + 1
                subject_rowids[synthetic.subject] = rowid
                connection.execute(
                    "INSERT INTO subjects (ROWID, subject, normalized_subject) VALUES (?, ?, ?)",
                    (rowid, synthetic.subject, synthetic.subject.casefold()),
                )
        recipient_rowid = len(address_rowids) + 1
        connection.execute(
            "INSERT INTO addresses (ROWID, address, comment) VALUES (?, ?, ?)",
            (recipient_rowid, "petr@example.com", "Petr"),
        )
        for position, synthetic in enumerate(SYNTHETIC_MESSAGES, start=1):
            timestamp = (
                int(synthetic.received.replace(tzinfo=timezone.utc).timestamp())
                if use_unix_timestamps
                else to_cocoa_seconds(synthetic.received)
            )
            connection.execute(
                "INSERT INTO messages (ROWID, sender, subject, date_sent, date_received, mailbox, "
                "read, flagged, deleted, size, conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    synthetic.message_id,
                    address_rowids[synthetic.sender_address],
                    subject_rowids[synthetic.subject],
                    timestamp,
                    timestamp,
                    1,
                    0 if synthetic.message_id == 1005 else 1,
                    1 if synthetic.message_id == 1001 else 0,
                    0,
                    len(synthetic.raw_message),
                    synthetic.message_id,
                ),
            )
            connection.execute(
                "INSERT INTO recipients (message_id, type, address_id, position) VALUES (?, ?, ?, ?)",
                (synthetic.message_id, 1, recipient_rowid, position),
            )
        connection.commit()
    finally:
        connection.close()


def create_synthetic_mail_root(
    base_directory: Path,
    version_name: str = "V10",
    use_unix_timestamps: bool = False,
    include_older_version: bool = True,
) -> Path:
    mail_root = base_directory / "Mail"
    version_directory = mail_root / version_name
    version_directory.mkdir(parents=True, exist_ok=True)
    if include_older_version:
        (mail_root / "V9" / "MailData").mkdir(parents=True, exist_ok=True)
    write_messages(version_directory)
    write_envelope_index(version_directory, use_unix_timestamps=use_unix_timestamps)
    return mail_root
