from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .accounts import group_accounts, mailbox_display_name
from .envelope import (
    EnvelopeSchemaError,
    all_message_ids,
    count_messages,
    introspect_schema,
    list_mailbox_urls,
    open_envelope_index,
)
from .locator import build_locator
from .store import (
    AccessState,
    default_mail_root,
    describe_access,
    discover_version_directories,
    open_mail_store,
)

STALENESS_WARNING_THRESHOLD = timedelta(hours=24)
SAMPLE_DIVERGENT_IDENTIFIER_COUNT = 10


def mail_application_is_running() -> bool | None:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Mail"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.returncode == 0


def newest_modification_time(paths: list[Path]) -> datetime | None:
    newest: float | None = None
    for path in paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or modified > newest:
            newest = modified
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=timezone.utc)


def build_report(mail_root: Path | None = None, force_rescan: bool = False) -> dict:
    access_state, access_detail = describe_access(mail_root)
    report: dict = {
        "access_state": access_state.value,
        "access_detail": access_detail,
    }
    if access_state is not AccessState.READABLE:
        return report

    version_directories = discover_version_directories(mail_root or default_mail_root())
    mail_store = open_mail_store(mail_root)
    report["version_directories"] = [str(path) for path in version_directories]
    report["version_directory"] = str(mail_store.version_directory)
    report["envelope_index_path"] = str(mail_store.envelope_index_path)

    locator = build_locator(mail_store, force_rescan=force_rescan)
    on_disk_ids = locator.message_ids()
    report["messages_on_disk"] = len(on_disk_ids)

    report["mail_application_running"] = mail_application_is_running()
    envelope_modified = newest_modification_time(
        [
            mail_store.envelope_index_path,
            mail_store.mail_data_directory / f"{mail_store.envelope_index_path.name}-wal",
        ]
    )
    report["last_local_write"] = envelope_modified.isoformat() if envelope_modified else None
    if envelope_modified is not None:
        age = datetime.now(tz=timezone.utc) - envelope_modified
        report["last_local_write_age_hours"] = round(age.total_seconds() / 3600, 2)
        report["staleness_warning"] = age > STALENESS_WARNING_THRESHOLD

    try:
        with open_envelope_index(mail_store.envelope_index_path) as connection:
            schema = introspect_schema(connection)
            indexed_ids = all_message_ids(connection)
            report["schema_ok"] = True
            report["timestamp_offset_seconds"] = schema.timestamp_offset_seconds
            report["resolved_message_columns"] = dict(sorted(schema.message_columns.items()))
            report["has_recipients_table"] = schema.has_recipients_table
            report["messages_indexed"] = count_messages(connection, schema)
            mailbox_counts = list_mailbox_urls(connection, schema)
            report["mailboxes"] = [
                {"url": url, "display_name": mailbox_display_name(url), "message_count": total}
                for url, total in mailbox_counts
            ]
            report["accounts"] = [account.as_dict() for account in group_accounts(mailbox_counts)]
    except EnvelopeSchemaError as error:
        report["schema_ok"] = False
        report["schema_error"] = str(error)
        return report

    indexed_without_file = sorted(indexed_ids - on_disk_ids)
    file_without_index = sorted(on_disk_ids - indexed_ids)
    report["indexed_without_file_count"] = len(indexed_without_file)
    report["file_without_index_count"] = len(file_without_index)
    report["indexed_without_file_sample"] = indexed_without_file[:SAMPLE_DIVERGENT_IDENTIFIER_COUNT]
    report["file_without_index_sample"] = file_without_index[:SAMPLE_DIVERGENT_IDENTIFIER_COUNT]
    report["coverage_complete"] = not indexed_without_file and not file_without_index
    report["readable_message_count"] = len(indexed_ids & on_disk_ids)
    return report


def render_report(report: dict) -> str:
    lines: list[str] = []
    state = report["access_state"]
    if state != AccessState.READABLE.value:
        lines.append(f"access: {state}")
        lines.append("")
        lines.append(report["access_detail"])
        return "\n".join(lines)

    lines.append("access:                 ok")
    lines.append(f"version directory:      {report['version_directory']}")
    other_versions = [path for path in report.get("version_directories", []) if path != report["version_directory"]]
    if other_versions:
        lines.append(f"older version dirs:     {', '.join(other_versions)}")
    lines.append(f"envelope index:         {report['envelope_index_path']}")

    if not report.get("schema_ok", False):
        lines.append("")
        lines.append("SCHEMA ERROR")
        lines.append(report.get("schema_error", "unknown schema error"))
        return "\n".join(lines)

    lines.append(f"timestamp offset:       {report['timestamp_offset_seconds']}s")
    lines.append(f"recipients table:       {'yes' if report['has_recipients_table'] else 'no'}")
    lines.append("")
    lines.append(f"messages in index:      {report['messages_indexed']}")
    lines.append(f"message files on disk:  {report['messages_on_disk']}")
    lines.append(f"readable messages:      {report['readable_message_count']}")

    if report["coverage_complete"]:
        lines.append("coverage:               complete, index and disk agree exactly")
    else:
        lines.append("coverage:               INCOMPLETE")
        if report["indexed_without_file_count"]:
            lines.append(
                f"  indexed but no file:  {report['indexed_without_file_count']} "
                f"(sample ids: {report['indexed_without_file_sample']})"
            )
        if report["file_without_index_count"]:
            lines.append(
                f"  file but not indexed: {report['file_without_index_count']} "
                f"(sample ids: {report['file_without_index_sample']})"
            )
        lines.append("  messages missing from the index cannot be found by metadata search.")
        lines.append("  let Mail finish indexing, then re-run with --reindex.")

    lines.append("")
    running = report.get("mail_application_running")
    running_label = {True: "yes", False: "NO", None: "unknown"}[running]
    lines.append(f"Mail.app running:       {running_label}")
    age_hours = report.get("last_local_write_age_hours")
    if age_hours is not None:
        lines.append(f"last local write:       {age_hours}h ago")
    if running is False:
        lines.append("  Apple Mail does not fetch mail while it is closed, so anything that arrived since it")
        lines.append("  last ran is absent from this store. Open Mail and let it sync.")
    if report.get("staleness_warning"):
        lines.append("  No local write in over 24h, which suggests Mail has not synced recently.")
    lines.append("  Note: last local write is a LOWER BOUND on freshness, not a guarantee. Mail writes for")
    lines.append("  local reasons too (marking read, flagging), and a quiet mailbox that synced seconds ago")
    lines.append("  looks identical to one that has not synced in a week. macOS exposes no last-sync time.")

    accounts = report.get("accounts", [])
    if accounts:
        lines.append("")
        lines.append(f"accounts ({len(accounts)}):")
        for account in accounts:
            lines.append(
                f"  {account['identifier']}  "
                f"{account['mailbox_count']} mailboxes, {account['message_count']} messages"
            )
    return "\n".join(lines)
