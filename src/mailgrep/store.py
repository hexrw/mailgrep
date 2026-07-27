from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

MAIL_ROOT_ENVIRONMENT_VARIABLE = "MAILGREP_MAIL_ROOT"
MAIL_DATA_DIRECTORY_NAME = "MailData"
ENVELOPE_INDEX_NAME = "Envelope Index"
MAILBOX_BUNDLE_SUFFIX = ".mbox"
MAXIMUM_PROCESS_ANCESTRY_DEPTH = 16


class AccessState(str, Enum):
    READABLE = "readable"
    FULL_DISK_ACCESS_REQUIRED = "full_disk_access_required"
    MAIL_ROOT_MISSING_OR_DENIED = "mail_root_missing_or_denied"
    NO_VERSION_DIRECTORY = "no_version_directory"
    UNDETERMINED = "undetermined"


class MailStoreError(RuntimeError):
    pass


class FullDiskAccessError(MailStoreError):
    def __init__(self, blocked_path: Path):
        self.blocked_path = blocked_path
        self.responsible_application = detect_responsible_application()
        super().__init__(format_full_disk_access_message(blocked_path, self.responsible_application))


def default_mail_root() -> Path:
    override = os.environ.get(MAIL_ROOT_ENVIRONMENT_VARIABLE)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Mail"


def read_process_executable(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "comm=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    executable = result.stdout.strip()
    return executable or None


def read_parent_pid(pid: int) -> int | None:
    result = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    parent = result.stdout.strip()
    if not parent.isdigit():
        return None
    return int(parent)


def extract_application_bundle(executable_path: str) -> Path | None:
    parts = Path(executable_path).parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].endswith(".app"):
            return Path(*parts[: index + 1])
    return None


def detect_responsible_application() -> Path | None:
    pid: int | None = os.getpid()
    for _ in range(MAXIMUM_PROCESS_ANCESTRY_DEPTH):
        if pid is None or pid <= 1:
            return None
        executable = read_process_executable(pid)
        if executable:
            bundle = extract_application_bundle(executable)
            if bundle is not None:
                return bundle
        pid = read_parent_pid(pid)
    return None


def format_full_disk_access_message(blocked_path: Path, responsible_application: Path | None) -> str:
    application_label = str(responsible_application) if responsible_application else "your terminal application"
    lines = [
        f"macOS denied access to {blocked_path}.",
        "",
        "Apple Mail's data is protected by macOS privacy controls (TCC). Grant Full Disk Access to",
        f"the application that launched this process: {application_label}",
        "",
        "  System Settings -> Privacy & Security -> Full Disk Access -> add the application above",
        "",
        f"Then quit and reopen {application_label} completely; the grant only applies to newly launched processes.",
        "",
        "macOS attributes file access to the launching application, not to this script, so granting",
        "Full Disk Access to the mailgrep executable itself has no effect.",
    ]
    return "\n".join(lines)


class AmbiguousAccessError(MailStoreError):
    def __init__(self, blocked_path: Path):
        self.blocked_path = blocked_path
        super().__init__(format_ambiguous_access_message(blocked_path))


class UndeterminedAccessError(MailStoreError):
    pass


def format_ambiguous_access_message(blocked_path: Path) -> str:
    return "\n".join(
        [
            f"{blocked_path} reports that it does not exist.",
            "",
            "This has two possible causes and macOS does not let us tell them apart:",
            "  1. Apple Mail has never stored data for this user account.",
            "  2. Full Disk Access is denied, and macOS is hiding the directory rather than",
            "     reporting a permission error.",
            "",
            "If Apple Mail is set up and has messages, treat this as cause 2 and grant Full Disk",
            f"Access to {detect_responsible_application() or 'your terminal application'},",
            "then quit and reopen it completely.",
        ]
    )


def scan_directory(directory: Path) -> list[os.DirEntry]:
    try:
        return list(os.scandir(directory))
    except PermissionError:
        raise FullDiskAccessError(directory) from None
    except FileNotFoundError:
        raise AmbiguousAccessError(directory) from None
    except OSError as error:
        raise UndeterminedAccessError(
            f"could not read {directory}: {error.strerror or error}. This is not a permission problem; "
            "retry, and do not change privacy settings on account of it."
        ) from None


def is_version_directory_name(name: str) -> bool:
    return len(name) > 1 and name[0] == "V" and name[1:].isdigit()


def discover_version_directories(mail_root: Path) -> list[Path]:
    versions: list[tuple[int, Path]] = []
    for entry in scan_directory(mail_root):
        if not is_version_directory_name(entry.name):
            continue
        try:
            if not entry.is_dir():
                continue
        except PermissionError:
            raise FullDiskAccessError(Path(entry.path)) from None
        versions.append((int(entry.name[1:]), Path(entry.path)))
    versions.sort(reverse=True)
    return [path for _, path in versions]


def describe_access(mail_root: Path | None = None) -> tuple[AccessState, str]:
    root = mail_root or default_mail_root()
    try:
        versions = discover_version_directories(root)
    except FullDiskAccessError as error:
        return AccessState.FULL_DISK_ACCESS_REQUIRED, str(error)
    except AmbiguousAccessError as error:
        return AccessState.MAIL_ROOT_MISSING_OR_DENIED, str(error)
    except UndeterminedAccessError as error:
        return AccessState.UNDETERMINED, str(error)
    except MailStoreError as error:
        return AccessState.UNDETERMINED, str(error)
    if not versions:
        return (
            AccessState.NO_VERSION_DIRECTORY,
            f"{root} is readable but contains no V<n> directory. Apple Mail has no local store yet.",
        )
    return AccessState.READABLE, f"Readable, using {versions[0]}"


@dataclass(frozen=True)
class MailStore:
    version_directory: Path

    @property
    def version(self) -> int:
        return int(self.version_directory.name[1:])

    @property
    def mail_data_directory(self) -> Path:
        return self.version_directory / MAIL_DATA_DIRECTORY_NAME

    @property
    def envelope_index_path(self) -> Path:
        return self.mail_data_directory / ENVELOPE_INDEX_NAME

    def mailbox_bundles(self) -> list[Path]:
        bundles: list[Path] = []
        for root, directory_names, _ in os.walk(self.version_directory, onerror=raise_walk_error):
            if Path(root).name == MAIL_DATA_DIRECTORY_NAME:
                directory_names[:] = []
                continue
            for name in directory_names:
                if name.endswith(MAILBOX_BUNDLE_SUFFIX):
                    bundles.append(Path(root) / name)
        bundles.sort()
        return bundles


def raise_walk_error(error: OSError) -> None:
    if isinstance(error, PermissionError):
        raise FullDiskAccessError(Path(error.filename or "")) from None
    raise error


def open_mail_store(mail_root: Path | None = None) -> MailStore:
    root = mail_root or default_mail_root()
    versions = discover_version_directories(root)
    if not versions:
        raise MailStoreError(
            f"No V<n> directory found under {root}. Apple Mail has no local store for this user."
        )
    return MailStore(version_directory=versions[0])
