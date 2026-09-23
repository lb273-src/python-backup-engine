"""
Python Backup & Synchronization Suite

@package     sync
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
import functools
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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


def build_ignore_patterns(excludes: Optional[List[str]] = None, case_sensitive: bool = False) -> Tuple[Set[str], Set[str]]:
    active_patterns = set(DEFAULT_EXCLUDES) if case_sensitive else {p.lower() for p in DEFAULT_EXCLUDES}
    negations = set()

    if excludes:
        for pattern in excludes:
            p_clean = pattern.strip()
            if p_clean.startswith('!'):
                neg_target = p_clean[1:] if case_sensitive else p_clean[1:].lower()
                negations.add(neg_target)
                active_patterns.discard(neg_target)
            else:
                p = p_clean if case_sensitive else p_clean.lower()
                active_patterns.add(p)

    return active_patterns, negations


def is_ignored(name: str, rel_path: str, ignore_patterns: Set[str], negations: Set[str], case_sensitive: bool = False) -> bool:
    norm_name = name.replace('\\', '/') if case_sensitive else name.replace('\\', '/').lower()
    norm_rel = rel_path.replace('\\', '/') if case_sensitive else rel_path.replace('\\', '/').lower()
    match_fn = fnmatch.fnmatchcase if case_sensitive else fnmatch.fnmatch

    for pattern in negations:
        if match_fn(norm_name, pattern) or match_fn(norm_rel, pattern):
            return False

    for pattern in ignore_patterns:
        if match_fn(norm_name, pattern) or match_fn(norm_rel, pattern):
            return True
        if any(match_fn(part, pattern) for part in norm_rel.split('/')):
            return True
    return False


def normalize_rel_path(path: str, platform: Optional[str] = None, case_sensitive: bool = False) -> str:
    """
    Normalizes relative paths for cross-platform matching.
    When case_sensitive is True, preserves case unconditionally.
    On Windows (when case_sensitive is False), lowercases to ensure case-insensitive matching without false conflicts.
    On POSIX (Linux/macOS), preserves case to support case-sensitive file sisters.
    """
    norm = path.replace('\\', '/')
    if case_sensitive:
        return norm
    plat = platform or sys.platform
    if plat in ("win32", "darwin"):
        return norm.lower()
    return norm


@functools.lru_cache(maxsize=2048)
def _listdir_lower_map(directory: str) -> Optional[Dict[str, str]]:
    try:
        entries = os.listdir(directory)
        return {e.lower(): e for e in entries}
    except OSError:
        return None


def clear_case_cache() -> None:
    """Clear the LRU cache of directory listings used for case-insensitive checks."""
    _listdir_lower_map.cache_clear()


def _exists_case_insensitive(base_path: str, rel_path: str) -> bool:
    """
    Check if rel_path exists under base_path, resolving each path component
    case-insensitively when on case-sensitive filesystems.
    Utilizes an LRU-cached directory map to eliminate redundant disk roundtrips.
    """
    parts = [p for p in rel_path.replace('\\', '/').split('/') if p and p != '.']
    current = base_path
    for part in parts:
        exact = os.path.join(current, part)
        if os.path.exists(exact):
            current = exact
            continue
        entries_map = _listdir_lower_map(current)
        if entries_map is not None and part.lower() in entries_map:
            current = os.path.join(current, entries_map[part.lower()])
        else:
            return False
    return True


class SyncProtocol:
    def __init__(self, log_file: Optional[str] = None, use_stdout: bool = True):
        self.log_file = log_file
        self.use_stdout = use_stdout
        self.stats: Counter = Counter()
        self.start_ts: Optional[datetime] = None
        self.stop_ts: Optional[datetime] = None
        self._target_drive: Optional[str] = None
        self.initial_disk_usage: Optional[Any] = None
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

    # Type annotations for IDE autocompletion & static typing
    errors: int
    files_checked: int
    files_updated: int  # Cumulative count of files written (files_created + files_modified)
    files_deleted: int
    directories_checked: int
    directories_created: int
    directories_deleted: int
    archive_files_pruned: int
    archive_directories_pruned: int
    files_created: int
    files_modified: int
    metadata_updated: int
    files_archived: int
    directories_archived: int
    symlinks_skipped: int
    bytes_transferred: int
    archive_bytes_pruned: int

    STAT_FIELDS: Set[str] = frozenset({
        'errors',
        'files_checked',
        'files_updated',
        'files_deleted',
        'directories_checked',
        'directories_created',
        'directories_deleted',
        'archive_files_pruned',
        'archive_directories_pruned',
        'files_created',
        'files_modified',
        'metadata_updated',
        'files_archived',
        'directories_archived',
        'symlinks_skipped',
        'bytes_transferred',
        'archive_bytes_pruned',
    })

    ALLOWED_INSTANCE_FIELDS: Set[str] = frozenset({
        'log_file',
        'use_stdout',
        'stats',
        'start_ts',
        'stop_ts',
        '_target_drive',
        'target_drive',
        'initial_disk_usage',
        '_file_handle',
        '_lock',
    })

    def get_stat(self, stat_name: str) -> int:
        with self._lock:
            return self.stats[stat_name]

    def set_stat(self, stat_name: str, value: int) -> None:
        with self._lock:
            self.stats[stat_name] = int(value)

    def __getattr__(self, name: str) -> Any:
        if name in self.STAT_FIELDS:
            with self._lock:
                return self.stats[name]
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in getattr(self, 'STAT_FIELDS', ()):
            with self._lock:
                self.stats[name] = int(value)
        elif (
            name in getattr(self, 'ALLOWED_INSTANCE_FIELDS', ())
            or name.startswith('_')
            or hasattr(type(self), name)
        ):
            super().__setattr__(name, value)
        else:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{name}'. "
                f"To update statistics, use one of STAT_FIELDS or set_stat()."
            )

    @property
    def errors(self) -> int:
        """Convenience typed property for IDE inspection and tooling."""
        with self._lock:
            return self.stats['errors']

    @errors.setter
    def errors(self, value: int) -> None:
        with self._lock:
            self.stats['errors'] = value

    @property
    def target_drive(self) -> Optional[str]:
        with self._lock:
            return self._target_drive

    @target_drive.setter
    def target_drive(self, value: Optional[str]) -> None:
        with self._lock:
            self._target_drive = value
            if value and os.path.exists(value) and self.initial_disk_usage is None:
                self.record_initial_disk_usage(value)

    def record_initial_disk_usage(self, drive: Optional[str] = None) -> None:
        with self._lock:
            target = drive or self._target_drive
            if target and os.path.exists(target):
                try:
                    self.initial_disk_usage = shutil.disk_usage(target)
                except OSError:
                    pass

    def inc_stat(self, stat_name: str, value: int = 1) -> None:
        with self._lock:
            self.stats[stat_name] += value

    def add_protocol_entry(self, value: str) -> None:
        msg = file_core.utf8_str(value)
        with self._lock:
            if self._file_handle:
                try:
                    self._file_handle.write(msg)
                    self._file_handle.flush()
                except (ValueError, OSError):
                    pass
        if self.use_stdout:
            print(value)
            sys.stdout.flush()

    def set_start_ts(self, force: bool = False) -> None:
        with self._lock:
            if self.start_ts is None or force:
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
            files_checked = self.stats['files_checked']
            files_created = self.stats['files_created']
            files_modified = self.stats['files_modified']
            files_updated = self.stats['files_updated'] or (files_created + files_modified)
            errors = self.stats['errors']
            files_unchanged = max(0, files_checked - (files_created + files_modified + errors))

            bytes_transferred = self.stats['bytes_transferred']
            archive_bytes_pruned = self.stats['archive_bytes_pruned']

            total_seconds = self.ts_delta()[0]
            if total_seconds > 0 and bytes_transferred > 0:
                speed = bytes_transferred / total_seconds
                rate_str = f" ({file_core.format_bytes(speed)}/s)"
            else:
                rate_str = ""

            free_space_str = ""
            target = self._target_drive
            if target and os.path.exists(target):
                try:
                    curr_usage = shutil.disk_usage(target)
                    if self.initial_disk_usage:
                        init_free = self.initial_disk_usage.free
                        init_used = self.initial_disk_usage.used
                        curr_free = curr_usage.free
                        curr_used = curr_usage.used
                        total = curr_usage.total

                        delta_free = curr_free - init_free
                        if delta_free >= 0:
                            delta_str = f"+{file_core.format_bytes(delta_free)} net freed"
                        else:
                            delta_str = f"-{file_core.format_bytes(abs(delta_free))} net consumed"

                        free_space_str = (
                            f"TargetSpaceBefore: {file_core.format_bytes(init_free)} free ({file_core.format_bytes(init_used)} used of {file_core.format_bytes(total)}){nl}"
                            f"TargetSpaceAfter:  {file_core.format_bytes(curr_free)} free ({file_core.format_bytes(curr_used)} used of {file_core.format_bytes(total)}){nl}"
                            f"TargetSpaceDelta:  {delta_str}{nl}"
                            f"TargetFreeSpace:   {file_core.format_bytes(curr_free)} of {file_core.format_bytes(total)} free{nl}"
                        )
                    else:
                        free_space_str = f"TargetFreeSpace: {file_core.format_bytes(curr_usage.free)} of {file_core.format_bytes(curr_usage.total)} free{nl}"
                except OSError:
                    pass

            stats_text = (
                f"{nl}--- Backup Sync Statistics ---{nl}"
                f"{file_core.backup_ts()}{nl}"
                f"FilesChecked: {files_checked}{nl}"
                f"FilesUnchanged: {files_unchanged}{nl}"
                f"FilesCreated: {files_created}{nl}"
                f"FilesModified: {files_modified}{nl}"
                f"FilesUpdated: {files_updated}{nl}"
                f"MetadataUpdated: {self.stats['metadata_updated']}{nl}"
                f"FilesDeleted: {self.stats['files_deleted']}{nl}"
                f"FilesArchived: {self.stats['files_archived']}{nl}"
                f"DirectoriesChecked: {self.stats['directories_checked']}{nl}"
                f"DirectoriesCreated: {self.stats['directories_created']}{nl}"
                f"DirectoriesDeleted: {self.stats['directories_deleted']}{nl}"
                f"DirectoriesArchived: {self.stats['directories_archived']}{nl}"
                f"ArchiveFilesPruned: {self.stats['archive_files_pruned']}{nl}"
                f"ArchiveDirectoriesPruned: {self.stats['archive_directories_pruned']}{nl}"
                f"DataTransferred: {file_core.format_bytes(bytes_transferred)}{rate_str}{nl}"
                f"ArchiveSpaceReclaimed: {file_core.format_bytes(archive_bytes_pruned)} reclaimed{nl}"
                f"{free_space_str}"
                f"SymlinksSkipped: {self.stats['symlinks_skipped']}{nl}"
                f"Errors Encountered: {errors}{nl}"
                f"Total Time: {self.ts_delta_string()}{nl}{nl}"
            )
            default_handle = self._file_handle

        # File I/O and terminal output performed outside of self._lock to prevent deadlocks and contention
        target_handle = default_handle
        should_close_custom = False

        if filename and filename != self.log_file:
            try:
                target_handle = open(filename, 'a', encoding='utf-8')
                should_close_custom = True
            except OSError:
                target_handle = None

        if target_handle:
            try:
                target_handle.write(stats_text)
                target_handle.flush()
            except (ValueError, OSError):
                pass
            finally:
                if should_close_custom:
                    try:
                        target_handle.close()
                    except OSError:
                        pass

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
        self.set_start_ts()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class ArchiveAbortError(RuntimeError):
    """Raised when archiving a file before overwrite fails, aborting the replacement safely."""
    pass


class ArchiveResult(tuple):
    """
    Result of an archive operation.
    Subclasses tuple (success, path) for backwards compatibility,
    while exposing .success, .path, and .method ('hardlink', 'move', 'dry_run', 'none').
    """
    method: str

    def __new__(cls, success: bool, path: Optional[str], method: str = "move"):
        obj = super().__new__(cls, (success, path))
        obj.method = method
        return obj

    @property
    def success(self) -> bool:
        return self[0]

    @property
    def path(self) -> Optional[str]:
        return self[1]


class Synchronizer:
    def __init__(
        self,
        max_workers: int = 1,
        dry_run: bool = False,
        verify_copy: bool = True,
        case_sensitive_excludes: bool = False,
        heartbeat_callback: Optional[Callable[[], None]] = None,
        heartbeat_interval: float = 15.0
    ):
        self.max_workers = max(1, max_workers)
        self.dry_run = dry_run
        self.verify_copy = verify_copy
        self.case_sensitive_excludes = case_sensitive_excludes
        self.heartbeat_callback = heartbeat_callback
        self.heartbeat_interval = heartbeat_interval
        self._last_heartbeat = 0.0
        self._heartbeat_lock = threading.Lock()

    def trigger_heartbeat(self, force: bool = False) -> None:
        """
        Invokes the heartbeat callback if configured and the interval has elapsed.
        If force is True, invokes unconditionally.
        Propagates any exceptions (e.g. RuntimeError if drive lock was lost or stolen) immediately.
        """
        if not self.heartbeat_callback:
            return
        now = time.monotonic()
        if force or (now - self._last_heartbeat >= self.heartbeat_interval):
            with self._heartbeat_lock:
                if force or (now - self._last_heartbeat >= self.heartbeat_interval):
                    self._last_heartbeat = now
                    self.heartbeat_callback()


    def archive_file(
        self,
        backup_file: str,
        backup_path: str,
        archive_path: Optional[str] = None,
        protocol: Optional[SyncProtocol] = None,
        prefer_hardlink: bool = False,
        **kwargs
    ) -> ArchiveResult:
        if archive_path is None and "archiv_path" in kwargs:
            archive_path = kwargs.pop("archiv_path")

        if not archive_path or not file_core.is_dir(archive_path):
            return ArchiveResult(False, None, method="none")

        if self.dry_run:
            if protocol:
                protocol.inc_stat('files_archived')
                protocol.add_protocol_entry(f'[DRY-RUN] Would archive file: {backup_file}')
            return ArchiveResult(True, None, method="dry_run")

        rel_path = os.path.relpath(backup_file, backup_path)
        archive_target = os.path.join(archive_path, rel_path)

        b_dir = os.path.dirname(archive_target)
        b_name, b_ext = os.path.splitext(os.path.basename(archive_target))

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        unique_token = uuid.uuid4().hex[:10]
        archived_file = os.path.join(b_dir, f"{b_name}_{timestamp}_{unique_token}{b_ext}")

        try:
            file_core.make_directory(b_dir)

            # Target must be removed on Windows prior to link or move
            if file_core.path_exists(archived_file):
                file_core.remove_file(archived_file)

            if prefer_hardlink:
                try:
                    src_lp = file_core._long_path(backup_file)
                    dst_lp = file_core._long_path(archived_file)
                    os.link(src_lp, dst_lp)
                    if protocol:
                        protocol.inc_stat('files_archived')
                        protocol.add_protocol_entry(f'archived (hardlink) file {backup_file} -> {archived_file}')
                    return ArchiveResult(True, archived_file, method="hardlink")
                except OSError as link_err:
                    if protocol:
                        protocol.add_protocol_entry(
                            f'Hardlink archive unavailable for {backup_file} ({link_err}), falling back to copy.'
                        )
                # Copy-First Fallback: preserve original file at backup_file until os.replace!
                shutil.copy2(file_core._long_path(backup_file), file_core._long_path(archived_file))
                if protocol:
                    protocol.inc_stat('files_archived')
                    protocol.add_protocol_entry(f'archived (copied) file {backup_file} -> {archived_file}')
                return ArchiveResult(True, archived_file, method="copy")

            file_core.remove_readonly(backup_file)
            shutil.move(backup_file, archived_file)
            if protocol:
                protocol.inc_stat('files_archived')
                protocol.add_protocol_entry(f'archived (moved) file {backup_file} -> {archived_file}')
            return ArchiveResult(True, archived_file, method="move")
        except FileNotFoundError:
            return ArchiveResult(False, None, method="none")
        except Exception as e:
            if protocol:
                protocol.add_protocol_entry(f'Archive move error for {backup_file}: {e}')
                protocol.inc_stat('errors')
            return ArchiveResult(False, None, method="none")

    def archive_directory(
        self,
        backup_directory: str,
        backup_path: str,
        archive_path: Optional[str] = None,
        protocol: Optional[SyncProtocol] = None,
        **kwargs
    ) -> Tuple[bool, Optional[str]]:
        if archive_path is None and "archiv_path" in kwargs:
            archive_path = kwargs.pop("archiv_path")

        if not archive_path or not file_core.is_dir(archive_path):
            return False, None

        if self.dry_run:
            if protocol:
                protocol.inc_stat('directories_archived')
                protocol.add_protocol_entry(f'[DRY-RUN] Would archive directory tree: {backup_directory}')
            return True, None

        rel_path = os.path.relpath(backup_directory, backup_path)
        archive_target = os.path.join(archive_path, rel_path)

        b_parent = os.path.dirname(archive_target)
        b_name = os.path.basename(archive_target)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        unique_token = uuid.uuid4().hex[:10]
        archived_dir = os.path.join(b_parent, f"{b_name}_{timestamp}_{unique_token}")

        try:
            file_core.make_directory(b_parent)

            # Target directory collision cleanup (safety check)
            if file_core.path_exists(archived_dir):
                file_core.remove_directory(archived_dir)

            file_core.remove_readonly(backup_directory)

            # Step 1: Direct atomic filesystem rename (single inode/MFT pointer switch)
            moved_atomically = False
            max_retries = 3
            src_lp = file_core._long_path(backup_directory)
            dst_lp = file_core._long_path(archived_dir)

            for attempt in range(max_retries):
                try:
                    os.replace(src_lp, dst_lp)
                    moved_atomically = True
                    break
                except OSError as e:
                    # Windows access lock / sharing violation retry
                    if attempt < max_retries - 1 and sys.platform == "win32" and getattr(e, 'winerror', None) in (5, 32):
                        time.sleep(0.1 * (attempt + 1))
                        continue
                    # Cross-device link or unsupported directory replace -> fall back to staged copy
                    break

            if not moved_atomically:
                # Step 2: Staged copy for cross-filesystem moves.
                # Copy into a staging folder in destination first.
                staging_dir = os.path.join(b_parent, f".tmp_arch_{b_name}_{timestamp}_{unique_token}")
                staging_lp = file_core._long_path(staging_dir)
                try:
                    if file_core.path_exists(staging_dir):
                        file_core.remove_directory(staging_dir)
                    shutil.copytree(src_lp, staging_lp, symlinks=False)
                    # Atomically rename staging folder to final archive folder on the same volume
                    os.replace(staging_lp, dst_lp)
                    file_core.sync_directory(b_parent)
                    # Only remove original from backup after archive is 100% verified and in place
                    file_core.remove_directory(backup_directory)
                except Exception as stage_err:
                    if file_core.path_exists(staging_dir):
                        try:
                            file_core.remove_directory(staging_dir)
                        except OSError:
                            pass
                    raise stage_err

            if protocol:
                protocol.inc_stat('directories_archived')
                protocol.add_protocol_entry(f'archived (moved) directory tree {backup_directory} -> {archived_dir}')
            return True, archived_dir
        except FileNotFoundError:
            return False, None
        except Exception as e:
            if protocol:
                protocol.add_protocol_entry(f'Archive directory move error for {backup_directory}: {e}')
                protocol.inc_stat('errors')
            return False, None

    def _sync_single_file(self, origin_file: str, backup_file: str, backup_path: str, archive_path: Optional[str], protocol: SyncProtocol, force_hash: bool) -> None:
        protocol.inc_stat('files_checked')

        if file_core.is_symlink(origin_file):
            protocol.inc_stat('symlinks_skipped')
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
                file_size = 0
                try:
                    file_size = os.path.getsize(origin_file)
                except OSError:
                    pass

                exists_in_backup = file_core.path_exists(backup_file)

                if self.dry_run:
                    protocol.inc_stat('files_updated')
                    if exists_in_backup:
                        protocol.inc_stat('files_modified')
                        if archive_path:
                            protocol.inc_stat('files_archived')
                            protocol.add_protocol_entry(f'[DRY-RUN] Would archive file: {backup_file}')
                    else:
                        protocol.inc_stat('files_created')
                    protocol.inc_stat('bytes_transferred', file_size)
                    protocol.add_protocol_entry(f'[DRY-RUN] Would copy {origin_file} -> {backup_file}')
                    return

                archived_state = {
                    'archived': False,
                    'location': None,
                    'method': 'none'
                }

                def pre_replace_hook(dest_path: str) -> None:
                    if exists_in_backup and archive_path:
                        res = self.archive_file(dest_path, backup_path, archive_path, protocol, prefer_hardlink=True)
                        success = res[0]
                        path = res[1]
                        method = getattr(res, 'method', 'move')
                        if not success:
                            protocol.add_protocol_entry(f'ABORT OVERWRITE: Could not archive {dest_path}')
                            protocol.inc_stat('errors')
                            raise ArchiveAbortError(f'Could not archive {dest_path}')
                        archived_state['archived'] = True
                        archived_state['location'] = path
                        archived_state['method'] = method

                should_verify = self.verify_copy or force_hash
                try:
                    file_core.copy_file(
                        origin_file,
                        backup_file,
                        verify_hash=should_verify,
                        pre_replace_callback=pre_replace_hook if (exists_in_backup and archive_path) else None
                    )
                    protocol.inc_stat('files_updated')
                    if exists_in_backup:
                        protocol.inc_stat('files_modified')
                    else:
                        protocol.inc_stat('files_created')
                    protocol.inc_stat('bytes_transferred', file_size)
                    protocol.add_protocol_entry(f'copy file {origin_file} to: {backup_file}')
                except ArchiveAbortError:
                    # Overwrite aborted before destination was replaced; error is already counted.
                    return
                except Exception as copy_err:
                    if archived_state['archived'] and archived_state['location'] and file_core.path_exists(archived_state['location']):
                        if archived_state['method'] in ('hardlink', 'copy'):
                            if file_core.path_exists(backup_file):
                                try:
                                    file_core.remove_file(archived_state['location'])
                                    protocol.inc_stat('files_archived', -1)
                                    protocol.add_protocol_entry(f'CLEANUP: Removed pending {archived_state["method"]} archive {archived_state["location"]} after failed copy.')
                                except OSError:
                                    pass
                            else:
                                try:
                                    os.replace(archived_state['location'], backup_file)
                                    protocol.add_protocol_entry(f'ATOMIC ROLLBACK: Restored {backup_file} from archive.')
                                except Exception as rb_err:
                                    protocol.add_protocol_entry(f'CRITICAL ROLLBACK FAILURE for {backup_file}: {rb_err}')
                        else:
                            tmp_rb = None
                            try:
                                dest_dir = os.path.dirname(backup_file) or "."
                                fd, tmp_rb = tempfile.mkstemp(prefix="tmp_rollback_", dir=dest_dir)
                                os.close(fd)  # Close immediately to prevent descriptor leaks / access locks
                                shutil.copy2(archived_state['location'], tmp_rb)
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
                            protocol.inc_stat('metadata_updated')
                            protocol.add_protocol_entry(f'sync metadata for: {backup_file}')
                    except OSError as meta_err:
                        protocol.inc_stat('errors')
                        protocol.add_protocol_entry(f'Metadata error on {backup_file}: {meta_err}')
        except file_core.FatalBackupError as fbe:
            protocol.inc_stat('errors')
            protocol.add_protocol_entry(f'FATAL STORAGE ERROR on {origin_file}: {fbe}')
            raise
        except Exception as e:
            protocol.inc_stat('errors')
            protocol.add_protocol_entry(f'File sync error on {origin_file}: {e}')

    def backup(
        self,
        source_path: str,
        backup_path: str,
        archive_path: Optional[str] = None,
        protocol: Optional[SyncProtocol] = None,
        excludes: Optional[List[str]] = None,
        force_hash: bool = False,
        **kwargs
    ) -> Set[str]:
        if archive_path is None and "archiv_path" in kwargs:
            archive_path = kwargs.pop("archiv_path")

        ignore_patterns, negations = build_ignore_patterns(excludes, case_sensitive=self.case_sensitive_excludes)
        if protocol:
            protocol.add_protocol_entry(f'#backup  {source_path}  {backup_path}')

        file_tasks: List[Tuple[str, str]] = []
        source_rel_files: Set[str] = set()

        for root, dirs, files in os.walk(source_path, topdown=True):
            self.trigger_heartbeat()
            rel_root = os.path.relpath(root, source_path)
            rel_root_clean = "" if rel_root == "." else rel_root

            valid_dirs = []
            for d in dirs:
                full_d = os.path.join(root, d)
                rel_d = os.path.join(rel_root_clean, d)
                if is_ignored(d, rel_d, ignore_patterns, negations, case_sensitive=self.case_sensitive_excludes):
                    continue
                if file_core.is_symlink(full_d):
                    if protocol:
                        protocol.inc_stat('symlinks_skipped')
                        protocol.add_protocol_entry(f'Skipping symbolic link directory traversal: {full_d}')
                    continue
                valid_dirs.append(d)
            dirs[:] = valid_dirs

            files[:] = [f for f in files if not is_ignored(f, os.path.join(rel_root_clean, f), ignore_patterns, negations, case_sensitive=self.case_sensitive_excludes)]

            for dir_name in dirs:
                origin_directory = os.path.join(root, dir_name)
                rel_dir = os.path.relpath(origin_directory, source_path)
                backup_directory = os.path.join(backup_path, rel_dir)
                if protocol:
                    protocol.inc_stat('directories_checked')
                if not file_core.path_exists(backup_directory):
                    if not self.dry_run:
                        try:
                            file_core.make_directory(backup_directory)
                            if protocol:
                                protocol.inc_stat('directories_created')
                        except file_core.FatalBackupError:
                            if protocol:
                                protocol.inc_stat('errors')
                            raise
                        except Exception as e:
                            if protocol:
                                protocol.inc_stat('errors')
                                protocol.add_protocol_entry(f'Dir create error {backup_directory}: {e}')
                    else:
                        if protocol:
                            protocol.inc_stat('directories_created')

            for file_name in files:
                origin_file = os.path.join(root, file_name)
                rel_file = normalize_rel_path(os.path.relpath(origin_file, source_path), case_sensitive=self.case_sensitive_excludes)
                source_rel_files.add(rel_file)
                backup_file = os.path.join(backup_path, os.path.relpath(origin_file, source_path))
                file_tasks.append((origin_file, backup_file))

        if self.max_workers > 1 and len(file_tasks) > 1:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
            try:
                futures = [
                    executor.submit(self._sync_single_file, orig, bk, backup_path, archive_path, protocol, force_hash)
                    for orig, bk in file_tasks
                ]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        fut.result()
                    except file_core.FatalBackupError as fbe:
                        try:
                            executor.shutdown(wait=False, cancel_futures=True)
                        except TypeError:
                            executor.shutdown(wait=False)
                        raise fbe
                    except Exception:
                        try:
                            executor.shutdown(wait=False, cancel_futures=True)
                        except TypeError:
                            executor.shutdown(wait=False)
                        raise
                    try:
                        self.trigger_heartbeat()
                    except Exception:
                        try:
                            executor.shutdown(wait=False, cancel_futures=True)
                        except TypeError:
                            executor.shutdown(wait=False)
                        raise
            except (KeyboardInterrupt, SystemExit):
                try:
                    executor.shutdown(wait=True, cancel_futures=True)
                except TypeError:
                    executor.shutdown(wait=True)
                raise
            else:
                executor.shutdown(wait=True)
        else:
            for orig, bk in file_tasks:
                self._sync_single_file(orig, bk, backup_path, archive_path, protocol, force_hash)
                self.trigger_heartbeat()

        self.trigger_heartbeat(force=True)
        return source_rel_files

    def prune(
        self,
        source_path: str,
        backup_path: str,
        archive_path: Optional[str] = None,
        protocol: Optional[SyncProtocol] = None,
        excludes: Optional[List[str]] = None,
        known_source_files: Optional[Set[str]] = None,
        **kwargs
    ) -> None:
        if archive_path is None and "archiv_path" in kwargs:
            archive_path = kwargs.pop("archiv_path")

        if not os.path.exists(backup_path):
            return

        ignore_patterns, negations = build_ignore_patterns(excludes, case_sensitive=self.case_sensitive_excludes)
        if protocol:
            protocol.add_protocol_entry(f'#prune {backup_path}')

        known_sources = known_source_files or set()
        known_sources_lower: Dict[str, str] = {s.lower(): s for s in known_sources}

        for root, dirs, files in os.walk(backup_path, topdown=True):
            self.trigger_heartbeat()
            rel_root = os.path.relpath(root, backup_path)
            rel_root_clean = "" if rel_root == "." else rel_root

            dirs[:] = [d for d in dirs if not is_ignored(d, os.path.join(rel_root_clean, d), ignore_patterns, negations, case_sensitive=self.case_sensitive_excludes)]

            surviving_dirs = []
            for d in dirs:
                backup_dir = os.path.join(root, d)
                rel_dir = os.path.relpath(backup_dir, backup_path)
                origin_dir = os.path.join(source_path, rel_dir)

                dir_exists = os.path.exists(origin_dir)
                if not dir_exists and not self.case_sensitive_excludes:
                    dir_exists = _exists_case_insensitive(source_path, rel_dir)

                if not dir_exists:
                    if protocol:
                        protocol.inc_stat('directories_checked')
                    if self.dry_run:
                        if protocol:
                            protocol.inc_stat('directories_deleted')
                            protocol.add_protocol_entry(f'[DRY-RUN] Would prune directory tree: {backup_dir}')
                    else:
                        if archive_path:
                            success, arch_dir = self.archive_directory(backup_dir, backup_path, archive_path, protocol)
                            if success:
                                if protocol:
                                    protocol.inc_stat('directories_deleted')
                            else:
                                if protocol:
                                    protocol.add_protocol_entry(f'PRESERVED: Directory tree kept due to archive error: {backup_dir}')
                        else:
                            try:
                                file_core.remove_directory(backup_dir)
                                if protocol:
                                    protocol.inc_stat('directories_deleted')
                                    protocol.add_protocol_entry(f'remove directory tree {backup_dir}')
                            except OSError as e:
                                if protocol:
                                    protocol.inc_stat('errors')
                                    protocol.add_protocol_entry(f'Remove dir error {backup_dir}: {e}')
                else:
                    surviving_dirs.append(d)

            dirs[:] = surviving_dirs

            for f in files:
                if is_ignored(f, os.path.join(rel_root_clean, f), ignore_patterns, negations, case_sensitive=self.case_sensitive_excludes):
                    continue

                backup_file = os.path.join(root, f)
                raw_rel = os.path.relpath(backup_file, backup_path).replace('\\', '/')
                rel_file = normalize_rel_path(raw_rel, case_sensitive=self.case_sensitive_excludes)

                is_orphan = False
                if rel_file not in known_sources:
                    if not self.case_sensitive_excludes and rel_file.lower() in known_sources_lower:
                        canonical_source_rel = known_sources_lower[rel_file.lower()]
                        candidate_source = os.path.join(source_path, canonical_source_rel)
                        if file_core.path_exists(candidate_source) or _exists_case_insensitive(source_path, raw_rel):
                            continue
                        is_orphan = True
                    else:
                        is_orphan = True

                if is_orphan:
                    if protocol:
                        protocol.inc_stat('files_checked')
                    if self.dry_run:
                        if protocol:
                            protocol.inc_stat('files_deleted')
                            protocol.add_protocol_entry(f'[DRY-RUN] Would prune orphan file: {backup_file}')
                    else:
                        if archive_path:
                            success, _ = self.archive_file(backup_file, backup_path, archive_path, protocol)
                            if success:
                                if protocol:
                                    protocol.inc_stat('files_deleted')
                            else:
                                if protocol:
                                    protocol.add_protocol_entry(f'PRESERVED: File kept due to archive error: {backup_file}')
                        else:
                            if file_core.remove_file(backup_file):
                                if protocol:
                                    protocol.inc_stat('files_deleted')
                                    protocol.add_protocol_entry(f'remove file {backup_file}')

        self.trigger_heartbeat(force=True)
    def synchronize(
        self,
        origin_path: str,
        backup_path: str,
        do_sync: bool = True,
        archive_path: Optional[str] = None,
        protocol: Optional[SyncProtocol] = None,
        excludes: Optional[List[str]] = None,
        force_hash: bool = False,
        **kwargs
    ) -> bool:
        if 'doSync' in kwargs:
            do_sync = bool(kwargs.pop('doSync'))
        if 'archiv_path' in kwargs:
            archive_path = kwargs.pop('archiv_path')

        clear_case_cache()
        if protocol:
            protocol.set_start_ts()
        job_start = time.time()
        try:
            valid, msg = file_core.check_paths(origin_path, backup_path, allow_missing_backup=self.dry_run)
            if not valid:
                if protocol:
                    protocol.add_protocol_entry(f'Invalid paths: {msg}')
                    protocol.inc_stat('errors')
                return False

            if archive_path and not self.dry_run:
                file_core.make_directory(archive_path)

            source_files = self.backup(origin_path, backup_path, archive_path, protocol, excludes, force_hash)

            if do_sync:
                clear_case_cache()
                self.prune(origin_path, backup_path, archive_path, protocol, excludes, known_source_files=source_files)

        except file_core.FatalBackupError as fbe:
            if protocol:
                protocol.add_protocol_entry(f'FATAL STORAGE ERROR: Aborting backup job immediately to prevent data loss: {fbe}')
            raise
        except (KeyboardInterrupt, SystemExit):
            if protocol:
                protocol.add_protocol_entry('synchronize aborted by user signal.')
            raise
        except RuntimeError as r_err:
            if protocol:
                protocol.add_protocol_entry(f'CRITICAL RUNTIME ERROR: {r_err}')
            raise
        except Exception as err:
            if protocol:
                protocol.add_protocol_entry(f'synchronize fatal error: {err}')
                protocol.inc_stat('errors')
        finally:
            clear_case_cache()
            job_elapsed = time.time() - job_start
            if protocol:
                protocol.set_stop_ts()
                protocol.add_protocol_entry(f'Job completed in {job_elapsed:.2f}s ({origin_path} -> {backup_path})')
        return True


class PruneResult(int):
    """
    Result of an archive prune operation.
    Subclasses int (equal to files purged) for seamless backwards compatibility,
    while exposing .files, .dirs, and .total attributes.
    """
    files: int
    dirs: int

    def __new__(cls, files: int, dirs: int):
        obj = super().__new__(cls, files)
        obj.files = files
        obj.dirs = dirs
        return obj

    @property
    def total(self) -> int:
        return self.files + self.dirs


def prune_archive(
    archive_path: Optional[str] = None,
    retention_days: int = 0,
    protocol: Optional[SyncProtocol] = None,
    dry_run: bool = False,
    heartbeat_callback: Optional[Callable[[], None]] = None,
    **kwargs
) -> PruneResult:
    """
    Prunes archived files and empty directory trees in archive_path that are older than retention_days.
    Returns a PruneResult (int subclass) representing the number of pruned files, with .dirs and .total attributes.
    """
    if archive_path is None and "archiv_path" in kwargs:
        archive_path = kwargs.pop("archiv_path")

    if retention_days <= 0 or not archive_path or not file_core.is_dir(archive_path):
        return PruneResult(0, 0)

    cutoff_time = time.time() - (retention_days * 86400.0)
    files_pruned = 0
    dirs_pruned = 0
    if protocol:
        protocol.add_protocol_entry(f'#retention-check {archive_path} (limit: {retention_days} days)')

    last_hb = time.monotonic()
    # Topdown=False ensures child files and subdirs are removed before their parent directories
    for root, dirs, files in os.walk(archive_path, topdown=False):
        if heartbeat_callback and (time.monotonic() - last_hb >= 15.0):
            last_hb = time.monotonic()
            heartbeat_callback()
        for f in files:
            f_path = os.path.join(root, f)
            try:
                stat_info = os.stat(f_path)
                if stat_info.st_mtime < cutoff_time:
                    f_size = stat_info.st_size
                    if dry_run:
                        if protocol:
                            protocol.add_protocol_entry(f'[DRY-RUN] Would prune expired archive file ({retention_days}d limit): {f_path}')
                            protocol.inc_stat('archive_files_pruned')
                            protocol.inc_stat('archive_bytes_pruned', f_size)
                        files_pruned += 1
                    else:
                        if file_core.remove_file(f_path):
                            if protocol:
                                protocol.add_protocol_entry(f'Pruned expired archive file: {f_path}')
                                protocol.inc_stat('archive_files_pruned')
                                protocol.inc_stat('archive_bytes_pruned', f_size)
                            files_pruned += 1
            except OSError as e:
                if protocol:
                    protocol.inc_stat('errors')
                    protocol.add_protocol_entry(f"WARNING: Error inspecting/pruning archive file '{f_path}': {e}")

        for d in dirs:
            d_path = os.path.join(root, d)
            try:
                # If directory is now empty, prune empty dir
                if not os.listdir(d_path):
                    if dry_run:
                        if protocol:
                            protocol.add_protocol_entry(f'[DRY-RUN] Would remove empty archive folder: {d_path}')
                            protocol.inc_stat('archive_directories_pruned')
                        dirs_pruned += 1
                    else:
                        if file_core.remove_directory(d_path):
                            dirs_pruned += 1
                            if protocol:
                                protocol.inc_stat('archive_directories_pruned')
            except OSError as e:
                if protocol:
                    protocol.inc_stat('errors')
                    protocol.add_protocol_entry(f"WARNING: Error inspecting/pruning archive folder '{d_path}': {e}")

    if files_pruned > 0 or dirs_pruned > 0:
        if protocol:
            protocol.add_protocol_entry(f'Retention prune completed: {files_pruned} file(s) and {dirs_pruned} empty folder(s) purged.')
    if heartbeat_callback:
        heartbeat_callback()
    return PruneResult(files_pruned, dirs_pruned)