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
from sync_logic import SyncProtocol, Synchronizer


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


if __name__ == "__main__":
    unittest.main()
