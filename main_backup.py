"""
Python Backup & Synchronization Suite

@package     sync
@subpackage  Orchestrator
@file        main_backup.py
@description CLI orchestrator with stale-lock detection, O_EXCL exclusive lock, timestamp fallback, and robust job validation.
@author      Dipl.-Ing. (FH) Ludger Bröring
@copyright   2026 JackTen Internetdienstleistungen GmbH
@link        https://github.com/lb273-src/python-backup-engine
@license     MIT
"""

import argparse
import errno
import json
import os
import platform
import re
import string
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from . import file_core
    from .sync_logic import SyncProtocol, Synchronizer, prune_archive
except ImportError:
    import file_core  # type: ignore # pyright: ignore
    from sync_logic import SyncProtocol, Synchronizer, prune_archive  # type: ignore # pyright: ignore


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if process:
            kernel32.CloseHandle(process)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


class BackupDriveLock:
    STALE_LOCK_SECONDS = 7200  # 2 hours

    def __init__(self, drive_path: str):
        self.lock_file_path = os.path.join(drive_path, '.backup.lock')
        self.handle = None
        self._fallback_mode = False

    def _read_lock_info(self) -> Tuple[Optional[int], Optional[float]]:
        if not os.path.exists(self.lock_file_path):
            return None, None
        try:
            mtime = os.path.getmtime(self.lock_file_path)
            with open(self.lock_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            match = re.search(r'PID:\s*(\d+)', content)
            pid = int(match.group(1)) if match else None
            return pid, mtime
        except Exception:
            return None, None

    def _is_stale(self, pid: Optional[int], mtime: Optional[float]) -> bool:
        if mtime is None:
            return True
        age = datetime.now().timestamp() - mtime
        if age > self.STALE_LOCK_SECONDS:
            return True
        if pid is not None and not is_pid_running(pid):
            return True
        return False

    def acquire(self) -> bool:
        pid, mtime = self._read_lock_info()
        if self._is_stale(pid, mtime):
            try:
                os.remove(self.lock_file_path)
            except OSError:
                pass

        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            try:
                fd = os.open(self.lock_file_path, flags, 0o644)
                self.handle = os.fdopen(fd, 'w')
            except FileExistsError:
                return False

            try:
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError) as lock_err:
                if getattr(lock_err, 'errno', None) in (
                    errno.ENOLCK, errno.ENOSYS, errno.EACCES, errno.EINVAL
                ):
                    self._fallback_mode = True
                    print(
                        "WARNING: Filesystem does not support mandatory locking (SMB/NFS?). "
                        "Using timestamp-based advisory lock."
                    )
                else:
                    self.handle.close()
                    self.handle = None
                    try:
                        os.remove(self.lock_file_path)
                    except OSError:
                        pass
                    raise

            self.handle.write(
                f"PID: {os.getpid()}\n"
                f"Started: {file_core.backup_ts()}\n"
                f"Fallback: {self._fallback_mode}\n"
            )
            self.handle.flush()
            return True
        except (OSError, IOError):
            if self.handle:
                try:
                    self.handle.close()
                except OSError:
                    pass
                self.handle = None
            return False

    def release(self) -> None:
        if self.handle:
            try:
                self.handle.flush()
            except OSError:
                pass
            try:
                if not self._fallback_mode:
                    if sys.platform == 'win32':
                        import msvcrt
                        try:
                            self.handle.seek(0)
                            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                    else:
                        import fcntl
                        fcntl.flock(self.handle, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                try:
                    self.handle.close()
                except OSError:
                    pass
                self.handle = None

            try:
                if os.path.exists(self.lock_file_path):
                    owner_pid, _ = self._read_lock_info()
                    if owner_pid == os.getpid():
                        os.remove(self.lock_file_path)
            except OSError:
                pass

    def __enter__(self) -> 'BackupDriveLock':
        if not self.acquire():
            pid, _ = self._read_lock_info()
            pid_msg = f" (PID {pid})" if pid else ""
            raise RuntimeError(f"Backup drive is currently locked by another process{pid_msg}.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class NoOpLock:
    def __enter__(self) -> 'NoOpLock': return self
    def __exit__(self, exc_type, exc_val, exc_tb) -> None: pass


def find_backup_drive() -> Optional[str]:
    system = platform.system()
    candidate_paths: List[str] = []

    if system == "Windows":
        candidate_paths = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    elif system == "Darwin":
        volumes_dir = "/Volumes"
        if os.path.exists(volumes_dir):
            try:
                candidate_paths = [os.path.join(volumes_dir, d) for d in os.listdir(volumes_dir)]
            except PermissionError:
                pass
    else:
        user = os.environ.get("USER", "")
        search_dirs = [
            f"/run/media/{user}",
            f"/media/{user}",
            "/media",
            "/mnt"
        ]
        for sdir in search_dirs:
            if os.path.exists(sdir):
                try:
                    candidate_paths.extend([os.path.join(sdir, d) for d in os.listdir(sdir)])
                except PermissionError:
                    continue

    for drive in candidate_paths:
        marker = os.path.join(drive, '.backup_id')
        if os.path.exists(marker):
            return drive
    return None


def load_jobs(config_path: str = "jobs.json") -> List[Dict[str, Any]]:
    if not os.path.isabs(config_path):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, config_path)

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found.")

    with open(config_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"Configuration file '{config_path}' must contain a JSON array of job objects.")
        return data


def validate_jobs(jobs: List[Dict[str, Any]], base_drive: str) -> List[Dict[str, Any]]:
    validated: List[Dict[str, Any]] = []
    base_drive_resolved = Path(base_drive).resolve()

    for idx, raw_job in enumerate(jobs):
        if not isinstance(raw_job, dict):
            print(f"WARNING: Skipping invalid job entry at index {idx} (must be a JSON object).")
            continue

        job = {k.strip(): v for k, v in raw_job.items()}

        if "source" not in job or "target_dir" not in job:
            print(f"WARNING: Skipping job entry at index {idx} missing required 'source' or 'target_dir' keys.")
            continue

        raw_target = str(job["target_dir"]).strip()
        target_dir = os.path.normpath(raw_target)

        # Reject absolute paths, drive-scoped paths (e.g. C:foo), UNC paths, or directory traversal
        if os.path.isabs(target_dir) or re.match(r'^[a-zA-Z]:', target_dir) or target_dir.startswith('\\\\') or '..' in Path(target_dir).parts:
            print(f"SECURITY ERROR: Job {idx} uses absolute, traversal, or drive-scoped path '{target_dir}'. Skipping.")
            continue

        base_resolved = os.path.realpath(os.path.abspath(base_drive))
        planned_target = os.path.realpath(os.path.abspath(os.path.join(base_resolved, target_dir)))

        try:
            common = os.path.commonpath([base_resolved, planned_target])
            is_inside = (
                common.lower() == base_resolved.lower()
                if sys.platform == "win32"
                else common == base_resolved
            )
        except (ValueError, OSError):
            is_inside = False

        if not is_inside:
            print(f"SECURITY ERROR: Job {idx} target '{target_dir}' escapes backup drive! Skipping.")
            continue

        retention_days = None
        if job.get("retention_days") is not None:
            try:
                retention_days = int(job["retention_days"])
            except (ValueError, TypeError):
                retention_days = None

        case_sensitive_excludes = bool(job.get("case_sensitive_excludes", False))

        validated.append({
            "source": str(job["source"]).strip(),
            "target_dir": target_dir,
            "excludes": list(job.get("excludes", [])),
            "case_sensitive_excludes": case_sensitive_excludes,
            "force_hash": bool(job.get("force_hash", False)),
            "verify_copy": bool(job.get("verify_copy", True)),
            "max_workers": int(job.get("max_workers", 4)),
            "retention_days": retention_days
        })
    return validated


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Python Incremental Backup & Synchronization Orchestrator")
    parser.add_argument("-c", "--config", default="jobs.json", help="Path to jobs.json configuration file")
    parser.add_argument("-d", "--drive", default=None, help="Explicitly specify backup drive root directory")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Simulate synchronization without writing changes")
    parser.add_argument("-w", "--workers", type=int, default=None, help="Override worker thread count for all jobs")
    parser.add_argument("--no-verify", action="store_true", help="Disable inline streaming SHA-256 verification")
    parser.add_argument("--retention-days", type=int, default=None, help="Prune archived versions older than N days from recyclebin (overrides jobs.json)")
    parser.add_argument("--case-sensitive-excludes", action="store_true", help="Enforce case-sensitive matching for exclude patterns (overrides jobs.json)")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()

    print("Starting Backup Orchestrator...")
    if args.dry_run:
        print(">>> RUNNING IN DRY-RUN MODE: NO CHANGES WILL BE WRITTEN <<<")

    bdrive = args.drive or find_backup_drive()
    if not bdrive:
        print("ERROR: No valid Backup Drive with '.backup_id' found!")
        print("Please connect Backup Drive or specify via --drive <path>.")
        sys.exit(1)

    print(f"Backup Drive '{bdrive}' detected. Starting Backup Jobs.")

    lock_manager = NoOpLock() if args.dry_run else BackupDriveLock(bdrive)

    try:
        with lock_manager:
            protocol_file = os.path.join(bdrive, 'protocol.txt')
            archived_base = os.path.join(bdrive, 'recyclebin')

            if not args.dry_run:
                stale_cleaned = file_core.cleanup_stale_temp_files(bdrive)
                if stale_cleaned > 0:
                    print(f"Cleaned up {stale_cleaned} abandoned temporary file(s) on backup drive.")

            try:
                raw_jobs = load_jobs(args.config)
                backup_jobs = validate_jobs(raw_jobs, bdrive)
                if not backup_jobs:
                    print("ERROR: No valid backup jobs configured!")
                    sys.exit(1)
            except Exception as e:
                print(f"ERROR: Could not initialize backup jobs: {e}")
                sys.exit(1)

            with SyncProtocol(log_file=protocol_file, use_stdout=True) as protocol:
                try:
                    for job in backup_jobs:
                        source = job["source"]
                        bk_target = os.path.join(bdrive, job["target_dir"])
                        archive_target = os.path.join(archived_base, job["target_dir"])
                        job_excludes = job["excludes"]
                        force_hash = job["force_hash"]
                        verify_copy = False if args.no_verify else job["verify_copy"]
                        max_workers = args.workers or job["max_workers"]

                        print(f"\n--- Starting Job: {source} -> {bk_target} (Workers: {max_workers}) ---")

                        if not args.dry_run:
                            file_core.make_directory(bk_target)

                        case_sensitive = args.case_sensitive_excludes or job["case_sensitive_excludes"]
                        synchronizer = Synchronizer(
                            max_workers=max_workers,
                            dry_run=args.dry_run,
                            verify_copy=verify_copy,
                            case_sensitive_excludes=case_sensitive
                        )

                        synchronizer.synchronize(
                            origin_path=source,
                            backup_path=bk_target,
                            doSync=True,
                            archiv_path=archive_target,
                            protocol=protocol,
                            excludes=job_excludes,
                            force_hash=force_hash
                        )

                        retention = args.retention_days if args.retention_days is not None else job.get("retention_days")
                        if retention and retention > 0 and archive_target and os.path.exists(archive_target):
                            pruned = prune_archive(archive_target, retention, protocol, dry_run=args.dry_run)
                            if pruned.files > 0 or pruned.dirs > 0:
                                action_str = "[DRY-RUN] Would prune" if args.dry_run else "Pruned"
                                print(f"{action_str} {pruned.files} expired archive file(s) and {pruned.dirs} empty folder(s) in {archive_target}")
                except KeyboardInterrupt:
                    print("\nBackup aborted by user signal.")
                finally:
                    protocol.set_stop_ts()
                    protocol.write_statistics()
                    sys.stdout.flush()
                    print(f"\nProtocol successfully written to {protocol_file}")
                    print(f"Completed in {protocol.ts_delta_string()} with {protocol.errors} errors.")

            if not args.dry_run:
                try:
                    ts_file = os.path.join(bdrive, 'bkupts.txt')
                    with open(ts_file, 'w', encoding='utf-8') as f:
                        f.write(file_core.backup_ts())
                except OSError as e:
                    print(f"Error writing bkupts.txt: {e}")

    except RuntimeError as lock_err:
        print(f"ERROR: {lock_err}")
        sys.exit(1)


if __name__ == "__main__":
    main()