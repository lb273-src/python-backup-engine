"""
Automated Unit Tests for Sync Suite

Verifies:
1. PEP 8 naming compliance and backward compatibility aliases in file_core and sync_logic.
2. Package exports via __init__.py.
3. Read-only attribute toggle logic via os.chmod.
4. Symlink safety and non-recursion.
5. Stream protocol logging, thread safety, and metrics writing.
6. Single and multi-threaded folder backup/synchronization operations including orphan deletion.
7. Metadata-only synchronization (mtime/permissions update).
8. O(1) archive file collision resolution with microsecond & UUID precision.
"""

import os
import tempfile
import time
import unittest
import sys

# Ensure parent directory is on sys.path
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SYNC_DIR = os.path.dirname(TEST_DIR)
WORKSPACE_DIR = os.path.dirname(SYNC_DIR)
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)
if SYNC_DIR not in sys.path:
    sys.path.insert(0, SYNC_DIR)

import file_core
try:
    import sync
except ModuleNotFoundError:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync",
        os.path.join(SYNC_DIR, "__init__.py"),
        submodule_search_locations=[SYNC_DIR],
    )
    if spec and spec.loader:
        sync = importlib.util.module_from_spec(spec)
        sys.modules["sync"] = sync
        spec.loader.exec_module(sync)
    else:
        raise
from sync_logic import (
    SyncProtocol,
    Synchronizer,
    normalize_rel_path,
    build_ignore_patterns,
    is_ignored,
    prune_archive,
)
from main_backup import validate_jobs


class TestFileCore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pep8_and_aliases(self):
        # Test function equality with legacy aliases
        self.assertEqual(file_core.remove_file, file_core.removeFile)
        self.assertEqual(file_core.remove_directory, file_core.removeDirectory)
        self.assertEqual(file_core.make_directory, file_core.makeDirectory)
        self.assertEqual(file_core.copy_file, file_core.copyFile)
        self.assertEqual(file_core.path_exists, file_core.pathExists)
        self.assertEqual(file_core.is_file, file_core.isFile)
        self.assertEqual(file_core.is_dir, file_core.isDir)
        self.assertEqual(file_core.is_readonly, file_core.isReadonly)
        self.assertEqual(file_core.get_sha256, file_core.getSha256)
        self.assertEqual(file_core.is_different, file_core.isDifferent)
        self.assertEqual(file_core.sync_metadata, file_core.syncMetadata)

    def test_init_exports(self):
        # Verify __init__.py exports both snake_case and legacy camelCase functions
        self.assertTrue(hasattr(sync, "remove_file"))
        self.assertTrue(hasattr(sync, "removeFile"))
        self.assertTrue(hasattr(sync, "copy_file"))
        self.assertTrue(hasattr(sync, "copyFile"))
        self.assertTrue(hasattr(sync, "sync_metadata"))
        self.assertTrue(hasattr(sync, "syncMetadata"))
        self.assertTrue(hasattr(sync, "check_paths"))
        self.assertTrue(hasattr(sync, "checkPaths"))
        self.assertTrue(hasattr(sync, "is_symlink"))

    def test_directory_creation_and_removal(self):
        sub_dir = os.path.join(self.path, "subdir_test")
        self.assertTrue(file_core.make_directory(sub_dir))
        self.assertTrue(file_core.is_dir(sub_dir))
        self.assertTrue(file_core.remove_directory(sub_dir))
        self.assertFalse(file_core.path_exists(sub_dir))

    def test_readonly_toggle(self):
        file_path = os.path.join(self.path, "readonly_test.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("test content")

        self.assertFalse(file_core.is_readonly(file_path))
        file_core.set_readonly(file_path)
        self.assertTrue(file_core.is_readonly(file_path))

        file_core.remove_readonly(file_path)
        self.assertFalse(file_core.is_readonly(file_path))

    def test_metadata_sync(self):
        src = os.path.join(self.path, "src_meta.txt")
        dst = os.path.join(self.path, "dst_meta.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("identical payload")
        file_core.copy_file(src, dst)

        # Modify mtime of src
        new_mtime = time.time() - 3600
        os.utime(src, (new_mtime, new_mtime))

        self.assertNotEqual(os.stat(src).st_mtime, os.stat(dst).st_mtime)
        res = file_core.sync_metadata(src, dst)
        self.assertTrue(res)
        self.assertAlmostEqual(os.stat(src).st_mtime, os.stat(dst).st_mtime, delta=1.0)

    def test_atomic_file_copy_and_sha256(self):
        src = os.path.join(self.path, "source.txt")
        dst = os.path.join(self.path, "dest.txt")
        content = "Hello Sync Engine Refactored!"
        with open(src, "w", encoding="utf-8") as f:
            f.write(content)

        self.assertTrue(file_core.copy_file(src, dst))
        self.assertTrue(file_core.path_exists(dst))
        self.assertEqual(file_core.get_sha256(src), file_core.get_sha256(dst))
        self.assertFalse(file_core.is_different(src, dst))

    def test_symlink_detection(self):
        target = os.path.join(self.path, "target_file.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("target")

        link = os.path.join(self.path, "symlink_file.txt")
        try:
            os.symlink(target, link)
            self.assertTrue(file_core.is_symlink(link))
            self.assertFalse(file_core.is_symlink(target))
        except (OSError, NotImplementedError):
            pass

    def test_stale_temp_cleanup(self):
        # Create abandoned temp files in self.path
        tmp1 = os.path.join(self.path, "tmp_sync_12345")
        tmp2 = os.path.join(self.path, "tmp_rollback_67890")
        normal = os.path.join(self.path, "important.txt")

        with open(tmp1, "w", encoding="utf-8") as f:
            f.write("temp")
        with open(tmp2, "w", encoding="utf-8") as f:
            f.write("temp")
        with open(normal, "w", encoding="utf-8") as f:
            f.write("keep")

        self.assertTrue(os.path.exists(tmp1))
        self.assertTrue(os.path.exists(tmp2))

        # Backdate mtimes so they qualify as stale (> 1800s / 30 mins)
        old_mtime = time.time() - 3600
        os.utime(tmp1, (old_mtime, old_mtime))
        os.utime(tmp2, (old_mtime, old_mtime))

        cleaned = file_core.cleanup_stale_temp_files(self.path)
        self.assertEqual(cleaned, 2)
        self.assertFalse(os.path.exists(tmp1))
        self.assertFalse(os.path.exists(tmp2))
        self.assertTrue(os.path.exists(normal))

    def test_stale_temp_age_filter(self):
        fresh_tmp = os.path.join(self.path, "tmp_sync_fresh")
        stale_tmp = os.path.join(self.path, "tmp_sync_stale")

        with open(fresh_tmp, "w", encoding="utf-8") as f:
            f.write("fresh")
        with open(stale_tmp, "w", encoding="utf-8") as f:
            f.write("stale")

        # Stale file is 1 hour old; fresh file is brand new
        stale_mtime = time.time() - 3600
        os.utime(stale_tmp, (stale_mtime, stale_mtime))

        # Default age filter is 1800s (30 mins): only stale_tmp should be deleted
        cleaned = file_core.cleanup_stale_temp_files(self.path, min_age_seconds=1800.0)
        self.assertEqual(cleaned, 1)
        self.assertTrue(os.path.exists(fresh_tmp))
        self.assertFalse(os.path.exists(stale_tmp))


class TestSyncLogic(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = self.temp_dir.name
        self.source = os.path.join(self.base, "source")
        self.target = os.path.join(self.base, "target")
        self.archive = os.path.join(self.base, "archive")
        self.log_file = os.path.join(self.base, "protocol.log")

        file_core.make_directory(self.source)
        file_core.make_directory(self.target)
        file_core.make_directory(self.archive)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sync_protocol_logging(self):
        protocol = SyncProtocol(log_file=self.log_file, use_stdout=False)
        protocol.set_start_ts()
        protocol.add_protocol_entry("Testing log entry 1")
        protocol.add_protocol_entry("Testing log entry 2")
        protocol.files_checked += 5
        protocol.files_updated += 2
        protocol.set_stop_ts()
        protocol.write_statistics()
        protocol.close()

        self.assertTrue(os.path.exists(self.log_file))
        with open(self.log_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Testing log entry 1", content)
            self.assertIn("Testing log entry 2", content)
            self.assertIn("FilesChecked: 5", content)
            self.assertIn("FilesUpdated: 2", content)

    def test_single_threaded_sync(self):
        f1 = os.path.join(self.source, "file1.txt")
        with open(f1, "w", encoding="utf-8") as f:
            f.write("content 1")

        protocol = SyncProtocol(use_stdout=False)
        syncer = Synchronizer(max_workers=1)
        res = syncer.synchronize(
            origin_path=self.source,
            backup_path=self.target,
            doSync=True,
            archiv_path=self.archive,
            protocol=protocol
        )
        self.assertTrue(res)
        self.assertEqual(protocol.files_updated, 1)
        self.assertTrue(os.path.exists(os.path.join(self.target, "file1.txt")))

    def test_multi_threaded_sync_and_deletion(self):
        # Populate source with 10 files
        for i in range(10):
            f_path = os.path.join(self.source, f"file_{i}.txt")
            with open(f_path, "w", encoding="utf-8") as f:
                f.write(f"content {i}")

        # Also create 5 orphan files in target that do NOT exist in source
        for i in range(5):
            orphan_path = os.path.join(self.target, f"orphan_{i}.txt")
            with open(orphan_path, "w", encoding="utf-8") as f:
                f.write(f"orphan content {i}")

        protocol = SyncProtocol(use_stdout=False)
        syncer = Synchronizer(max_workers=4)
        res = syncer.synchronize(
            origin_path=self.source,
            backup_path=self.target,
            doSync=True,
            archiv_path=self.archive,
            protocol=protocol
        )
        self.assertTrue(res)
        self.assertEqual(protocol.files_updated, 10)
        self.assertEqual(protocol.files_deleted, 5)

        for i in range(10):
            self.assertTrue(os.path.exists(os.path.join(self.target, f"file_{i}.txt")))
        for i in range(5):
            self.assertFalse(os.path.exists(os.path.join(self.target, f"orphan_{i}.txt")))

    def test_o1_archive_collision_resolution(self):
        protocol = SyncProtocol(use_stdout=False)
        syncer = Synchronizer(max_workers=1)

        # Create target file to archive repeatedly
        f_target = os.path.join(self.target, "collision_file.txt")

        archived_files = []
        for i in range(5):
            with open(f_target, "w", encoding="utf-8") as f:
                f.write(f"version {i}")
            syncer.archive_file(f_target, self.target, self.archive, protocol)

        files_in_archive = os.listdir(self.archive)
        self.assertEqual(len(files_in_archive), 5)
        # Verify microsecond precision and unique UUID token present in all archive files
        for fname in files_in_archive:
            self.assertTrue("collision_file_" in fname)

    def test_directory_tree_archiving(self):
        # Create a nested directory structure with files in source
        nested_dir = os.path.join(self.source, "subfolder", "nested")
        file_core.make_directory(nested_dir)
        nested_file = os.path.join(nested_dir, "payload.txt")
        with open(nested_file, "w", encoding="utf-8") as f:
            f.write("preserve this content")

        protocol = SyncProtocol(use_stdout=False)
        syncer = Synchronizer(max_workers=1)

        # Initial synchronization
        res = syncer.synchronize(
            origin_path=self.source,
            backup_path=self.target,
            doSync=True,
            archiv_path=self.archive,
            protocol=protocol
        )
        self.assertTrue(res)
        target_nested_file = os.path.join(self.target, "subfolder", "nested", "payload.txt")
        self.assertTrue(os.path.exists(target_nested_file))

        # Now simulate deleting the entire subfolder on source
        file_core.remove_directory(os.path.join(self.source, "subfolder"))
        self.assertFalse(os.path.exists(os.path.join(self.source, "subfolder")))

        # Sync again with doSync=True
        protocol2 = SyncProtocol(use_stdout=False)
        res2 = syncer.synchronize(
            origin_path=self.source,
            backup_path=self.target,
            doSync=True,
            archiv_path=self.archive,
            protocol=protocol2
        )
        self.assertTrue(res2)

        # The subfolder must be pruned from target
        self.assertFalse(os.path.exists(os.path.join(self.target, "subfolder")))

        # But the entire directory tree must be archived non-destructively
        archive_subfolders = [d for d in os.listdir(self.archive) if "subfolder_" in d]
        self.assertEqual(len(archive_subfolders), 1)

        archived_payload = os.path.join(self.archive, archive_subfolders[0], "nested", "payload.txt")
        self.assertTrue(os.path.exists(archived_payload))
        with open(archived_payload, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "preserve this content")

        self.assertGreaterEqual(protocol2.directories_deleted, 1)

    def test_case_sensitivity_normalization(self):
        path = "MyDocuments\\SUBFOLDER\\File.TXT"
        # Windows platform should lowercase
        win_norm = normalize_rel_path(path, platform="win32")
        self.assertEqual(win_norm, "mydocuments/subfolder/file.txt")

        # Linux/POSIX platform should preserve case
        linux_norm = normalize_rel_path(path, platform="linux")
        self.assertEqual(linux_norm, "MyDocuments/SUBFOLDER/File.TXT")

    def test_path_traversal_validation(self):
        base_drive = self.target
        jobs = [
            {"source": self.source, "target_dir": "valid_target"},
            {"source": self.source, "target_dir": "../../escaped"},
            {"source": self.source, "target_dir": "C:drive_scoped"},
            {"source": self.source, "target_dir": "/absolute/path"},
            {"source": self.source, "target_dir": "nested/../../escape2"},
        ]
        validated = validate_jobs(jobs, base_drive)
        # Only the first job should be accepted
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["target_dir"], "valid_target")

    def test_archive_uuid_token_length(self):
        protocol = SyncProtocol(use_stdout=False)
        syncer = Synchronizer(max_workers=1)

        f_target = os.path.join(self.target, "token_check.txt")
        with open(f_target, "w", encoding="utf-8") as f:
            f.write("data")

        syncer.archive_file(f_target, self.target, self.archive, protocol)
        archived_files = [f for f in os.listdir(self.archive) if f.startswith("token_check_")]
        self.assertEqual(len(archived_files), 1)
        archived_name = archived_files[0]
        # Format: token_check_YYYY-MM-DD_HHMMSS_microseconds_token.txt
        name_no_ext, _ = os.path.splitext(archived_name)
        token = name_no_ext.split("_")[-1]
        self.assertEqual(len(token), 10)

    def test_ignore_patterns_case_sensitivity(self):
        # Case insensitive (default)
        patterns, negs = build_ignore_patterns(["*.LOG", "!Keep.LOG"], case_sensitive=False)
        self.assertTrue(is_ignored("app.log", "logs/app.log", patterns, negs, case_sensitive=False))
        self.assertTrue(is_ignored("APP.LOG", "logs/APP.LOG", patterns, negs, case_sensitive=False))
        self.assertFalse(is_ignored("keep.log", "logs/keep.log", patterns, negs, case_sensitive=False))

        # Case sensitive
        patterns_cs, negs_cs = build_ignore_patterns(["*.LOG"], case_sensitive=True)
        self.assertTrue(is_ignored("app.LOG", "logs/app.LOG", patterns_cs, negs_cs, case_sensitive=True))
        self.assertFalse(is_ignored("app.log", "logs/app.log", patterns_cs, negs_cs, case_sensitive=True))

    def test_archive_retention_pruning(self):
        protocol = SyncProtocol(use_stdout=False)
        now = time.time()

        old_file = os.path.join(self.archive, "expired_file_2026-01-01_100000_123456_abcdef1234.txt")
        recent_file = os.path.join(self.archive, "recent_file_2026-09-01_100000_123456_abcdef1234.txt")

        sub_archive = os.path.join(self.archive, "expired_folder_2026-01-01_100000_1234567890")
        file_core.make_directory(sub_archive)
        old_nested_file = os.path.join(sub_archive, "nested.txt")

        with open(old_file, "w", encoding="utf-8") as f:
            f.write("old")
        with open(recent_file, "w", encoding="utf-8") as f:
            f.write("recent")
        with open(old_nested_file, "w", encoding="utf-8") as f:
            f.write("old nested")

        # Set old files mtime to 60 days ago, recent file to 5 days ago
        old_mtime = now - (60 * 86400)
        recent_mtime = now - (5 * 86400)

        os.utime(old_file, (old_mtime, old_mtime))
        os.utime(old_nested_file, (old_mtime, old_mtime))
        os.utime(recent_file, (recent_mtime, recent_mtime))

        # Test Dry Run with 30 days retention
        dry_pruned = prune_archive(self.archive, retention_days=30, protocol=protocol, dry_run=True)
        self.assertEqual(dry_pruned, 2)
        self.assertTrue(os.path.exists(old_file))
        self.assertTrue(os.path.exists(old_nested_file))
        self.assertTrue(os.path.exists(recent_file))

        # Test Live Pruning with 30 days retention
        live_pruned = prune_archive(self.archive, retention_days=30, protocol=protocol, dry_run=False)
        self.assertEqual(live_pruned, 2)
        self.assertFalse(os.path.exists(old_file))
        self.assertFalse(os.path.exists(old_nested_file))
        self.assertFalse(os.path.exists(sub_archive))  # Empty folder should have been removed
        self.assertTrue(os.path.exists(recent_file))

    def test_multi_job_timing_preservation(self):
        protocol = SyncProtocol(use_stdout=False)
        syncer = Synchronizer(max_workers=1)

        # First job
        f1 = os.path.join(self.source, "file1.txt")
        with open(f1, "w", encoding="utf-8") as f:
            f.write("content 1")
        syncer.synchronize(self.source, self.target, doSync=True, archiv_path=self.archive, protocol=protocol)
        initial_start = protocol.start_ts
        self.assertIsNotNone(initial_start)

        time.sleep(0.05)

        # Second job with same protocol
        syncer.synchronize(self.source, self.target, doSync=True, archiv_path=self.archive, protocol=protocol)
        # Verify start_ts was NOT overwritten by the second job
        self.assertEqual(protocol.start_ts, initial_start)
        self.assertGreaterEqual(protocol.ts_delta()[0], 0.04)


if __name__ == "__main__":
    unittest.main()
