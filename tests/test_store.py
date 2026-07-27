from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import create_synthetic_mail_root
from mailgrep.locator import build_locator, scan_message_paths
from mailgrep.store import (
    AccessState,
    MAIL_ROOT_ENVIRONMENT_VARIABLE,
    MailStoreError,
    default_mail_root,
    describe_access,
    discover_version_directories,
    extract_application_bundle,
    format_full_disk_access_message,
    is_version_directory_name,
    open_mail_store,
)


class VersionDirectoryTests(unittest.TestCase):
    def test_recognises_version_directory_names(self):
        self.assertTrue(is_version_directory_name("V10"))
        self.assertTrue(is_version_directory_name("V9"))
        self.assertTrue(is_version_directory_name("V11"))
        self.assertFalse(is_version_directory_name("V"))
        self.assertFalse(is_version_directory_name("MailData"))
        self.assertFalse(is_version_directory_name("Vabc"))

    def test_picks_highest_version_numerically_not_lexically(self):
        with tempfile.TemporaryDirectory() as base:
            mail_root = Path(base) / "Mail"
            for name in ("V9", "V10", "V2", "MailData", "notes.txt"):
                (mail_root / name).mkdir(parents=True, exist_ok=True)
            discovered = discover_version_directories(mail_root)
            self.assertEqual([path.name for path in discovered], ["V10", "V9", "V2"])

    def test_open_mail_store_uses_newest_version(self):
        with tempfile.TemporaryDirectory() as base:
            mail_root = create_synthetic_mail_root(Path(base))
            mail_store = open_mail_store(mail_root)
            self.assertEqual(mail_store.version, 10)
            self.assertEqual(mail_store.version_directory.name, "V10")
            self.assertTrue(mail_store.envelope_index_path.exists())

    def test_open_mail_store_errors_when_no_version_directory(self):
        with tempfile.TemporaryDirectory() as base:
            mail_root = Path(base) / "Mail"
            mail_root.mkdir()
            with self.assertRaises(MailStoreError):
                open_mail_store(mail_root)


class MailRootResolutionTests(unittest.TestCase):
    def test_environment_variable_overrides_home_directory(self):
        previous = os.environ.get(MAIL_ROOT_ENVIRONMENT_VARIABLE)
        os.environ[MAIL_ROOT_ENVIRONMENT_VARIABLE] = "/tmp/example-mail-root"
        try:
            self.assertEqual(default_mail_root(), Path("/tmp/example-mail-root"))
        finally:
            if previous is None:
                del os.environ[MAIL_ROOT_ENVIRONMENT_VARIABLE]
            else:
                os.environ[MAIL_ROOT_ENVIRONMENT_VARIABLE] = previous

    def test_default_is_under_library(self):
        previous = os.environ.pop(MAIL_ROOT_ENVIRONMENT_VARIABLE, None)
        try:
            self.assertEqual(default_mail_root(), Path.home() / "Library" / "Mail")
        finally:
            if previous is not None:
                os.environ[MAIL_ROOT_ENVIRONMENT_VARIABLE] = previous


class AccessStateTests(unittest.TestCase):
    def test_readable_store_reports_readable(self):
        with tempfile.TemporaryDirectory() as base:
            mail_root = create_synthetic_mail_root(Path(base))
            state, detail = describe_access(mail_root)
            self.assertIs(state, AccessState.READABLE)
            self.assertIn("V10", detail)

    def test_missing_root_is_distinguished_from_denied_access(self):
        with tempfile.TemporaryDirectory() as base:
            state, detail = describe_access(Path(base) / "absent")
            self.assertIs(state, AccessState.MAIL_ROOT_MISSING)
            self.assertIn("never stored data", detail)

    def test_empty_root_reports_no_version_directory(self):
        with tempfile.TemporaryDirectory() as base:
            mail_root = Path(base) / "Mail"
            mail_root.mkdir()
            state, detail = describe_access(mail_root)
            self.assertIs(state, AccessState.NO_VERSION_DIRECTORY)
            self.assertIn("no local store", detail)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permissions")
    def test_unreadable_root_reports_full_disk_access_required(self):
        with tempfile.TemporaryDirectory() as base:
            mail_root = Path(base) / "Mail"
            (mail_root / "V10").mkdir(parents=True)
            mail_root.chmod(0o000)
            try:
                state, detail = describe_access(mail_root)
            finally:
                mail_root.chmod(stat.S_IRWXU)
            self.assertIs(state, AccessState.FULL_DISK_ACCESS_REQUIRED)
            self.assertIn("Full Disk Access", detail)


class ResponsibleApplicationTests(unittest.TestCase):
    def test_extracts_bundle_from_executable_path(self):
        bundle = extract_application_bundle("/Applications/Warp.app/Contents/MacOS/stable")
        self.assertEqual(bundle, Path("/Applications/Warp.app"))

    def test_extracts_outermost_bundle_for_nested_helpers(self):
        bundle = extract_application_bundle(
            "/Applications/Foo.app/Contents/Frameworks/Bar.app/Contents/MacOS/helper"
        )
        self.assertEqual(bundle, Path("/Applications/Foo.app/Contents/Frameworks/Bar.app"))

    def test_returns_none_for_plain_executables(self):
        self.assertIsNone(extract_application_bundle("/usr/bin/python3"))

    def test_message_names_the_application_and_rejects_binary_grants(self):
        message = format_full_disk_access_message(
            Path("/Users/example/Library/Mail"), Path("/Applications/Ghostty.app")
        )
        self.assertIn("/Applications/Ghostty.app", message)
        self.assertIn("Full Disk Access", message)
        self.assertIn("has no effect", message)

    def test_message_falls_back_when_application_unknown(self):
        message = format_full_disk_access_message(Path("/Users/example/Library/Mail"), None)
        self.assertIn("your terminal application", message)


class MailboxDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.mail_root = create_synthetic_mail_root(base)
        self.cache_directory = base / "cache"
        self.mail_store = open_mail_store(self.mail_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_finds_mailbox_bundles(self):
        bundles = self.mail_store.mailbox_bundles()
        self.assertEqual([bundle.name for bundle in bundles], ["INBOX.mbox"])

    def test_scan_finds_every_message_including_partials(self):
        discovered = scan_message_paths(self.mail_store)
        self.assertEqual(set(discovered), {1001, 1002, 1003, 1004, 1005})
        self.assertTrue(discovered[1004].endswith("1004.partial.emlx"))

    def test_scan_ignores_attachment_files(self):
        discovered = scan_message_paths(self.mail_store)
        self.assertTrue(all("Attachments" not in path for path in discovered.values()))

    def test_locator_cache_round_trips(self):
        first = build_locator(self.mail_store, cache_directory=self.cache_directory)
        cache_files = list(self.cache_directory.iterdir())
        self.assertEqual(len(cache_files), 1)
        second = build_locator(self.mail_store, cache_directory=self.cache_directory)
        self.assertEqual(first.relative_paths, second.relative_paths)

    def test_cache_is_rejected_when_version_directory_changes(self):
        build_locator(self.mail_store, cache_directory=self.cache_directory)
        other_base = Path(self.temporary_directory.name) / "other"
        other_root = create_synthetic_mail_root(other_base)
        other_store = open_mail_store(other_root)
        locator = build_locator(other_store, cache_directory=self.cache_directory)
        self.assertTrue(str(locator.path_for(1001)).startswith(str(other_root)))

    def test_force_rescan_picks_up_new_message(self):
        locator = build_locator(self.mail_store, cache_directory=self.cache_directory)
        self.assertIsNone(locator.path_for(1006))
        messages_directory = locator.path_for(1001).parent
        (messages_directory / "1006.emlx").write_bytes(b"5\nhello")
        refreshed = build_locator(self.mail_store, cache_directory=self.cache_directory, force_rescan=True)
        self.assertIsNotNone(refreshed.path_for(1006))


if __name__ == "__main__":
    unittest.main()
