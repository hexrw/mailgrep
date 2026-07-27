from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .emlx import EMLX_SUFFIX, PARTIAL_EMLX_SUFFIX, message_id_from_filename
from .store import MailStore, raise_walk_error

CACHE_DIRECTORY_ENVIRONMENT_VARIABLE = "MAILGREP_CACHE_DIR"
CACHE_FORMAT_VERSION = 1
MESSAGES_DIRECTORY_NAME = "Messages"
ATTACHMENTS_DIRECTORY_NAME = "Attachments"


def default_cache_directory() -> Path:
    override = os.environ.get(CACHE_DIRECTORY_ENVIRONMENT_VARIABLE)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Caches" / "mailgrep"


def is_message_filename(name: str) -> bool:
    return name.endswith(EMLX_SUFFIX) or name.endswith(PARTIAL_EMLX_SUFFIX)


@dataclass
class MessageLocator:
    mail_store: MailStore
    relative_paths: dict[int, str]

    def path_for(self, message_id: int) -> Path | None:
        relative = self.relative_paths.get(message_id)
        if relative is None:
            return None
        return self.mail_store.version_directory / relative

    def message_ids(self) -> set[int]:
        return set(self.relative_paths)

    def __len__(self) -> int:
        return len(self.relative_paths)


def scan_message_paths(mail_store: MailStore) -> dict[int, str]:
    version_directory = mail_store.version_directory
    mail_data_name = mail_store.mail_data_directory.name
    discovered: dict[int, str] = {}
    for root, directory_names, file_names in os.walk(version_directory, onerror=raise_walk_error):
        root_path = Path(root)
        if root_path == version_directory:
            directory_names[:] = [name for name in directory_names if name != mail_data_name]
        if ATTACHMENTS_DIRECTORY_NAME in directory_names and root_path.name != MESSAGES_DIRECTORY_NAME:
            directory_names.remove(ATTACHMENTS_DIRECTORY_NAME)
        for file_name in file_names:
            if not is_message_filename(file_name):
                continue
            file_path = root_path / file_name
            message_id = message_id_from_filename(file_path)
            if message_id is None:
                continue
            discovered[message_id] = str(file_path.relative_to(version_directory))
    return discovered


def cache_path_for(mail_store: MailStore, cache_directory: Path | None = None) -> Path:
    directory = cache_directory or default_cache_directory()
    return directory / f"message-paths-v{mail_store.version}.json"


def envelope_index_fingerprint(mail_store: MailStore) -> str:
    fingerprint_parts: list[str] = []
    for suffix in ("", "-wal"):
        path = mail_store.mail_data_directory / f"{mail_store.envelope_index_path.name}{suffix}"
        try:
            status = path.stat()
        except OSError:
            fingerprint_parts.append(f"{suffix}:absent")
            continue
        fingerprint_parts.append(f"{suffix}:{status.st_mtime_ns}:{status.st_size}")
    return "|".join(fingerprint_parts)


def read_cached_paths(cache_file: Path, version_directory: Path, fingerprint: str) -> dict[int, str] | None:
    try:
        payload = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        return None
    if payload.get("format_version") != CACHE_FORMAT_VERSION:
        return None
    if payload.get("version_directory") != str(version_directory):
        return None
    if payload.get("envelope_index_fingerprint") != fingerprint:
        return None
    entries = payload.get("relative_paths")
    if not isinstance(entries, dict):
        return None
    return {int(key): value for key, value in entries.items()}


def write_cached_paths(
    cache_file: Path,
    version_directory: Path,
    fingerprint: str,
    relative_paths: dict[int, str],
) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": CACHE_FORMAT_VERSION,
        "version_directory": str(version_directory),
        "envelope_index_fingerprint": fingerprint,
        "relative_paths": {str(key): value for key, value in relative_paths.items()},
    }
    temporary_file = cache_file.with_suffix(".json.tmp")
    temporary_file.write_text(json.dumps(payload))
    temporary_file.replace(cache_file)


def build_locator(
    mail_store: MailStore,
    cache_directory: Path | None = None,
    force_rescan: bool = False,
) -> MessageLocator:
    cache_file = cache_path_for(mail_store, cache_directory)
    fingerprint = envelope_index_fingerprint(mail_store)
    if not force_rescan:
        cached = read_cached_paths(cache_file, mail_store.version_directory, fingerprint)
        if cached is not None:
            return MessageLocator(mail_store=mail_store, relative_paths=cached)
    discovered = scan_message_paths(mail_store)
    write_cached_paths(cache_file, mail_store.version_directory, fingerprint, discovered)
    return MessageLocator(mail_store=mail_store, relative_paths=discovered)


def resolve_message_path(
    mail_store: MailStore,
    message_id: int,
    cache_directory: Path | None = None,
) -> Path | None:
    locator = build_locator(mail_store, cache_directory)
    path = locator.path_for(message_id)
    if path is not None and path.exists():
        return path
    locator = build_locator(mail_store, cache_directory, force_rescan=True)
    refreshed = locator.path_for(message_id)
    if refreshed is not None and refreshed.exists():
        return refreshed
    return None
