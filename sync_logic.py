"""
Python Backup & Synchronization Suite

@package     Sync
@subpackage  Engine
@file        sync_logic.py
@description Core synchronization algorithm with In-Memory Pruning, atomic rollbacks, and unified locking.
@author      Dipl.-Ing. (FH) Ludger Bröring
@copyright   2026 JackTen Internetdienstleistungen GmbH
@link        https://github.com/lb273-src/python-backup-engine
@license     MIT
"""

import concurrent.futures
import fnmatch
import os
import shutil
import sys
import tempfile
import threading
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, List, Optional, Set, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from . import file_core
except ImportError:
    import file_core  # type: ignore # pyright: ignore

DEFAULT_EXCLUDES: Set[str] = {
    '.tmp.driveupload',
    'desktop.ini',
    'thumbs.db',
    '.git',
    '$recycle.bin',
    'system volume information',
    '.backup.lock'
}

MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB per protocol file
MAX_LOG_BACKUPS = 5


def build_ignore_patterns(excludes: Optional[List[str]] = None) -> Tuple[Set[str], Set[str]]:
    active_patterns = set(DEFAULT_EXCLUDES)
    negations = set()

    if excludes:
        for pattern in excludes:
            p_clean = pattern.strip()
            if p_clean.startswith('!'):
                neg_target = p_clean[1:].lower()
                negations.add(neg_target)
                active_patterns.discard(neg_target)
            else:
                active_patterns.add(p_clean.lower())

    return active_patterns, negations


def is_ignored(name: str, rel_path: str, ignore_patterns: Set[str], negations: Set[str]) -> bool:
    norm_name = name.replace('\\', '/').lower()
    norm_rel = rel_path.replace('\\', '/').lower()

    for pattern in negations:
        if fnmatch.fnmatch(norm_name, pattern) or fnmatch.fnmatch(norm_rel, pattern):
            return False

    for pattern in ignore_patterns:
        if fnmatch.fnmatch(norm_name, pattern) or fnmatch.fnmatch(norm_rel, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in norm_rel.split('/')):
            return True
    return False


def normalize_rel_path(path: str, platform: Optional[str] = None) -> str:
    """
    Normalizes relative paths for cross-platform matching.
    On Windows, lowercases to ensure case-insensitive matching without false conflicts.
    On POSIX (Linux/macOS), preserves case to support case-sensitive file sisters.
    """
    norm = path.replace('\\', '/')
    plat = platform or sys.platform
    if plat == "win32":
        return norm.lower()
    return norm


class SyncProtocol:
    def __init__(self, log_file: Optional[str] = None, use_stdout: bool = True):
        self.log_file = log_file
        self.use_stdout = use_stdout
        self.stats: Counter = Counter()
        self.start_ts: Optional[datetime] = None
        self.stop_ts: Optional[datetime] = None
        self._file_handle: Optional[Any] = None
        self._lock = threading.RLock()

        if self.log_file:
            log_dir = os.path.dirname(self.log_file)
            if log_dir and not file_core.path_exists(log_dir):
                file_core.make_directory(log_dir)
            self._rotate_log_if_needed()
            self._file_handle = open(self.log_file, 'a', encoding='utf-8')

    def _rotate_log_if_needed(self) -> None:
        if not self.log_file or not os.path.exists(self.log_file):
            return
        try:
            if os.path.getsize(self.log_file) >= MAX_LOG_SIZE:
                for i in range(MAX_LOG_BACKUPS - 1, 0, -1):
                    sfn = f"{self.log_file}.{i}"
                    dfn = f"{self.log_file}.{i + 1}"
                    if os.path.exists(sfn):
                        os.replace(sfn, dfn)
                os.replace(self.log_file, f"{self.log_file}.1")
        except OSError:
            pass

    @property
    def errors(self) -> int:
        with self._lock:
            return self.stats['errors']

    @errors.setter
    def errors(self, value: int) -> None:
        with self._lock:
            self.stats['errors'] = value

    @property
    def files_checked(self) -> int:
        with self._lock:
            return self.stats['files_checked']

    @files_checked.setter
    def files_checked(self, value: int) -> None:
        with self._lock:
            self.stats['files_checked'] = value

    @property
    def files_updated(self) -> int:
        with self._lock:
            return self.stats['files_updated']

    @files_updated.setter
    def files_updated(self, value: int) -> None:
        with self._lock:
            self.stats['files_updated'] = value

    @property
    def files_deleted(self) -> int:
        with self._lock:
            return self.stats['files_deleted']

    @files_deleted.setter
    def files_deleted(self, value: int) -> None:
        with self._lock:
            self.stats['files_deleted'] = value

    @property
    def directories_checked(self) -> int:
        with self._lock:
            return self.stats['directories_checked']

    @directories_checked.setter
    def directories_checked(self, value: int) -> None:
        with self._lock:
            self.stats['directories_checked'] = value

    @property
    def directories_created(self) -> int:
        with self._lock:
            return self.stats['directories_created']

    @directories_created.setter
    def directories_created(self, value: int) -> None:
        with self._lock:
            self.stats['directories_created'] = value

    @property
    def directories_deleted(self) -> int:
        with self._lock:
            return self.stats['directories_deleted']

    @directories_deleted.setter
    def directories_deleted(self, value: int) -> None:
        with self._lock:
            self.stats['directories_deleted'] = value

    def inc_stat(self, stat_name: str, value: int = 1) -> None:
        with self._lock:
            self.stats[stat_name] += value

    def add_protocol_entry(self, value: str) -> None:
        msg = file_core.utf8_str(value)
        with self._lock:
            if self._file_handle:
                try:
                    self._file_handle.write(msg)
                except ValueError:
                    pass
            if self.use_stdout:
                print(value)
                sys.stdout.flush()

    def set_start_ts(self) -> None:
        with self._lock:
            self.start_ts = datetime.now()

    def set_stop_ts(self) -> None:
        with self._lock:
            self.stop_ts = datetime.now()

    def ts_delta(self) -> Tuple[float, float, float, float]:
        with self._lock:
            start = self.start_ts
            stop = self.stop_ts or datetime.now()
        if not start:
            return 0.0, 0.0, 0.0, 0.0
        total_seconds = (stop - start).total_seconds()
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return total_seconds, hours, minutes, seconds

    def ts_delta_string(self) -> str:
        total_seconds, hours, minutes, seconds = self.ts_delta()
        return f'{total_seconds:.2f} seconds ({hours:.0f}h {minutes:.0f}m {seconds:.2f}s)'

    def write_statistics(self, filename: Optional[str] = None) -> None:
        nl = '\n'
        with self._lock:
            stats_text = (
                f"{nl}--- Backup Sync Statistics ---{nl}"
                f"{file_core.backup_ts()}{nl}"
                f"FilesChecked: {self.stats['files_checked']}{nl}"
                f"FilesUpdated: {self.stats['files_updated']}{nl}"
                f"FilesDeleted: {self.stats['files_deleted']}{nl}"
                f"DirectoriesChecked: {self.stats['directories_checked']}{nl}"
                f"DirectoriesCreated: {self.stats['directories_created']}{nl}"
                f"DirectoriesDeleted: {self.stats['directories_deleted']}{nl}"
                f"Errors Encountered: {self.stats['errors']}{nl}"
                f"Total Time: {self.ts_delta_string()}{nl}{nl}"
            )

            target_handle = self._file_handle
            should_close_custom = False

            if filename and filename != self.log_file:
                target_handle = open(filename, 'a', encoding='utf-8')
                should_close_custom = True

            if target_handle:
                try:
                    target_handle.write(stats_text)
                    target_handle.flush()
                except ValueError:
                    pass
                finally:
                    if should_close_custom:
                        target_handle.close()

            if self.use_stdout:
                print(stats_text)
                sys.stdout.flush()

    def close(self) -> None:
        with self._lock:
            if self._file_handle:
                try:
                    self._file_handle.flush()
                    self._file_handle.close()
                except (OSError, ValueError):
                    pass
                self._file_handle = None

    def __enter__(self) -> 'SyncProtocol':
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class Synchronizer:
    def __init__(self, max_workers: int = 1, dry_run: bool = False, verify_copy: bool = True):
        self.max_workers = max_workers
        self.dry_run = dry_run
        self.verify_copy = verify_copy

    def archive_file(self, backup_file: str, backup_path: str, archiv_path: Optional[str], protocol: SyncProtocol) -> Tuple[bool, Optional[str]]:
        if not archiv_path or not file_core.is_dir(archiv_path):
            return False, None

        if self.dry_run:
            protocol.add_protocol_entry(f'[DRY-RUN] Would archive file: {backup_file}')
            return True, None

        rel_path = os.path.relpath(backup_file, backup_path)
        archiv_target = os.path.join(archiv_path, rel_path)

        b_dir = os.path.dirname(archiv_target)
        b_name, b_ext = os.path.splitext(os.path.basename(archiv_target))

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        unique_token = uuid.uuid4().hex[:6]
        archiv_file = os.path.join(b_dir, f"{b_name}_{timestamp}_{unique_token}{b_ext}")

        try:
            file_core.make_directory(b_dir)

            # Target must be removed on Windows prior to shutil.move
            if file_core.path_exists(archiv_file):
                file_core.remove_file(archiv_file)

            file_core.remove_readonly(backup_file)
            shutil.move(backup_file, archiv_file)
            protocol.add_protocol_entry(f'archived (moved) file {backup_file} -> {archiv_file}')
            return True, archiv_file
        except FileNotFoundError:
            return False, None
        except Exception as e:
            protocol.add_protocol_entry(f'Archive move error for {backup_file}: {e}')
            protocol.inc_stat('errors')
            return False, None

    def archive_directory(self, backup_directory: str, backup_path: str, archiv_path: Optional[str], protocol: SyncProtocol) -> Tuple[bool, Optional[str]]:
        if not archiv_path or not file_core.is_dir(archiv_path):
            return False, None

        if self.dry_run:
            protocol.add_protocol_entry(f'[DRY-RUN] Would archive directory tree: {backup_directory}')
            return True, None

        rel_path = os.path.relpath(backup_directory, backup_path)
        archiv_target = os.path.join(archiv_path, rel_path)

        b_parent = os.path.dirname(archiv_target)
        b_name = os.path.basename(archiv_target)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        unique_token = uuid.uuid4().hex[:6]
        archiv_dir = os.path.join(b_parent, f"{b_name}_{timestamp}_{unique_token}")

        try:
            file_core.make_directory(b_parent)

            # Target directory collision cleanup (safety check)
            if file_core.path_exists(archiv_dir):
                file_core.remove_directory(archiv_dir)

            file_core.remove_readonly(backup_directory)
            shutil.move(backup_directory, archiv_dir)
            protocol.add_protocol_entry(f'archived (moved) directory tree {backup_directory} -> {archiv_dir}')
            return True, archiv_dir
        except FileNotFoundError:
            return False, None
        except Exception as e:
            protocol.add_protocol_entry(f'Archive directory move error for {backup_directory}: {e}')
            protocol.inc_stat('errors')
            return False, None

    def _sync_single_file(self, origin_file: str, backup_file: str, backup_path: str, archiv_path: Optional[str], protocol: SyncProtocol, force_hash: bool) -> None:
        protocol.inc_stat('files_checked')

        if file_core.is_symlink(origin_file):
            protocol.add_protocol_entry(f'Skipping symbolic link file: {origin_file}')
            return

        try:
            try:
                different = file_core.is_different(origin_file, backup_file, force_hash=force_hash)
            except OSError as cmp_err:
                protocol.inc_stat('errors')
                protocol.add_protocol_entry(f'Compare error on {origin_file}: {cmp_err}')
                return

            if different:
                if self.dry_run:
                    protocol.inc_stat('files_updated')
                    protocol.add_protocol_entry(f'[DRY-RUN] Would copy {origin_file} -> {backup_file}')
                    return

                archived = False
                archived_backup_location = None

                if file_core.path_exists(backup_file) and archiv_path:
                    archived, archived_backup_location = self.archive_file(backup_file, backup_path, archiv_path, protocol)
                    if not archived:
                        protocol.add_protocol_entry(f'ABORT OVERWRITE: Could not archive {backup_file}')
                        protocol.inc_stat('errors')
                        return

                should_verify = self.verify_copy or force_hash
                try:
                    file_core.copy_file(origin_file, backup_file, verify_hash=should_verify)
                    protocol.inc_stat('files_updated')
                    protocol.add_protocol_entry(f'copy file {origin_file} to: {backup_file}')
                except Exception as copy_err:
                    if archived and archived_backup_location and file_core.path_exists(archived_backup_location):
                        tmp_rb = None
                        try:
                            dest_dir = os.path.dirname(backup_file) or "."
                            fd, tmp_rb = tempfile.mkstemp(prefix="tmp_rollback_", dir=dest_dir)
                            os.close(fd)  # Close immediately to prevent descriptor leaks / access locks
                            shutil.copy2(archived_backup_location, tmp_rb)
                            os.replace(tmp_rb, backup_file)
                            tmp_rb = None
                            protocol.add_protocol_entry(f'ATOMIC ROLLBACK: Restored {backup_file} from archive.')
                        except Exception as rb_err:
                            protocol.add_protocol_entry(f'CRITICAL ROLLBACK FAILURE for {backup_file}: {rb_err}')
                        finally:
                            if tmp_rb and os.path.exists(tmp_rb):
                                try:
                                    os.remove(tmp_rb)
                                except OSError:
                                    pass
                    raise copy_err
            else:
                if not self.dry_run:
                    try:
                        if file_core.sync_metadata(origin_file, backup_file):
                            protocol.add_protocol_entry(f'sync metadata for: {backup_file}')
                    except OSError as meta_err:
                        protocol.inc_stat('errors')
                        protocol.add_protocol_entry(f'Metadata error on {backup_file}: {meta_err}')
        except Exception as e:
            protocol.inc_stat('errors')
            protocol.add_protocol_entry(f'File sync error on {origin_file}: {e}')

    def backup(self, source_path: str, backup_path: str, archiv_path: Optional[str], protocol: SyncProtocol, excludes: Optional[List[str]] = None, force_hash: bool = False) -> Set[str]:
        ignore_patterns, negations = build_ignore_patterns(excludes)
        protocol.add_protocol_entry(f'#backup  {source_path}  {backup_path}')

        file_tasks: List[Tuple[str, str]] = []
        source_rel_files: Set[str] = set()

        for root, dirs, files in os.walk(source_path, topdown=True):
            rel_root = os.path.relpath(root, source_path)
            rel_root_clean = "" if rel_root == "." else rel_root

            valid_dirs = []
            for d in dirs:
                full_d = os.path.join(root, d)
                rel_d = os.path.join(rel_root_clean, d)
                if is_ignored(d, rel_d, ignore_patterns, negations):
                    continue
                if file_core.is_symlink(full_d):
                    protocol.add_protocol_entry(f'Skipping symbolic link directory traversal: {full_d}')
                    continue
                valid_dirs.append(d)
            dirs[:] = valid_dirs

            files[:] = [f for f in files if not is_ignored(f, os.path.join(rel_root_clean, f), ignore_patterns, negations)]

            for dir_name in dirs:
                origin_directory = os.path.join(root, dir_name)
                rel_dir = os.path.relpath(origin_directory, source_path)
                backup_directory = os.path.join(backup_path, rel_dir)
                protocol.inc_stat('directories_checked')
                if not file_core.path_exists(backup_directory):
                    if not self.dry_run:
                        try:
                            file_core.make_directory(backup_directory)
                            protocol.inc_stat('directories_created')
                        except Exception as e:
                            protocol.inc_stat('errors')
                            protocol.add_protocol_entry(f'Dir create error {backup_directory}: {e}')
                    else:
                        protocol.inc_stat('directories_created')

            for file_name in files:
                origin_file = os.path.join(root, file_name)
                rel_file = normalize_rel_path(os.path.relpath(origin_file, source_path))
                source_rel_files.add(rel_file)
                backup_file = os.path.join(backup_path, os.path.relpath(origin_file, source_path))
                file_tasks.append((origin_file, backup_file))

        if self.max_workers > 1 and len(file_tasks) > 1:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
            try:
                futures = [
                    executor.submit(self._sync_single_file, orig, bk, backup_path, archiv_path, protocol, force_hash)
                    for orig, bk in file_tasks
                ]
                concurrent.futures.wait(futures)
            except (KeyboardInterrupt, SystemExit):
                executor.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
        else:
            for orig, bk in file_tasks:
                self._sync_single_file(orig, bk, backup_path, archiv_path, protocol, force_hash)

        return source_rel_files

    def prune(self, source_path: str, backup_path: str, archiv_path: Optional[str], protocol: SyncProtocol, excludes: Optional[List[str]] = None, known_source_files: Optional[Set[str]] = None) -> None:
        if not os.path.exists(backup_path):
            return

        ignore_patterns, negations = build_ignore_patterns(excludes)
        protocol.add_protocol_entry(f'#prune {backup_path}')

        known_sources = known_source_files or set()

        for root, dirs, files in os.walk(backup_path, topdown=True):
            rel_root = os.path.relpath(root, backup_path)
            rel_root_clean = "" if rel_root == "." else rel_root

            dirs[:] = [d for d in dirs if not is_ignored(d, os.path.join(rel_root_clean, d), ignore_patterns, negations)]

            surviving_dirs = []
            for d in dirs:
                backup_dir = os.path.join(root, d)
                rel_dir = os.path.relpath(backup_dir, backup_path)
                origin_dir = os.path.join(source_path, rel_dir)

                if not os.path.exists(origin_dir):
                    protocol.inc_stat('directories_checked')
                    if self.dry_run:
                        protocol.inc_stat('directories_deleted')
                        protocol.add_protocol_entry(f'[DRY-RUN] Would prune directory tree: {backup_dir}')
                    else:
                        if archiv_path:
                            success, arch_dir = self.archive_directory(backup_dir, backup_path, archiv_path, protocol)
                            if success:
                                protocol.inc_stat('directories_deleted')
                            else:
                                protocol.add_protocol_entry(f'PRESERVED: Directory tree kept due to archive error: {backup_dir}')
                        else:
                            try:
                                file_core.remove_directory(backup_dir)
                                protocol.inc_stat('directories_deleted')
                                protocol.add_protocol_entry(f'remove directory tree {backup_dir}')
                            except OSError as e:
                                protocol.inc_stat('errors')
                                protocol.add_protocol_entry(f'Remove dir error {backup_dir}: {e}')
                else:
                    surviving_dirs.append(d)

            dirs[:] = surviving_dirs

            for f in files:
                if is_ignored(f, os.path.join(rel_root_clean, f), ignore_patterns, negations):
                    continue

                backup_file = os.path.join(root, f)
                rel_file = normalize_rel_path(os.path.relpath(backup_file, backup_path))

                if rel_file not in known_sources:
                    protocol.inc_stat('files_checked')
                    if self.dry_run:
                        protocol.inc_stat('files_deleted')
                        protocol.add_protocol_entry(f'[DRY-RUN] Would prune orphan file: {backup_file}')
                    else:
                        if archiv_path:
                            success, _ = self.archive_file(backup_file, backup_path, archiv_path, protocol)
                            if success:
                                protocol.inc_stat('files_deleted')
                            else:
                                protocol.add_protocol_entry(f'PRESERVED: File kept due to archive error: {backup_file}')
                        else:
                            if file_core.remove_file(backup_file):
                                protocol.inc_stat('files_deleted')
                                protocol.add_protocol_entry(f'remove file {backup_file}')

    def synchronize(self, origin_path: str, backup_path: str, doSync: bool, archiv_path: Optional[str], protocol: SyncProtocol, excludes: Optional[List[str]] = None, force_hash: bool = False) -> bool:
        protocol.set_start_ts()
        try:
            valid, msg = file_core.check_paths(origin_path, backup_path, allow_missing_backup=self.dry_run)
            if not valid:
                protocol.add_protocol_entry(f'Invalid paths: {msg}')
                protocol.inc_stat('errors')
                return False

            if archiv_path and not self.dry_run:
                file_core.make_directory(archiv_path)

            source_files = self.backup(origin_path, backup_path, archiv_path, protocol, excludes, force_hash)

            if doSync:
                self.prune(origin_path, backup_path, archiv_path, protocol, excludes, known_source_files=source_files)

        except (KeyboardInterrupt, SystemExit):
            protocol.add_protocol_entry('synchronize aborted by user signal.')
            raise
        except Exception as err:
            protocol.add_protocol_entry(f'synchronize fatal error: {err}')
            protocol.inc_stat('errors')
        finally:
            protocol.set_stop_ts()
        return True