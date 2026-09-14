"""
Python Backup & Synchronization Suite

@package     Sync
@file        __init__.py
@description Package initializer exporting public API components and metadata.
@author      Dipl.-Ing. (FH) Ludger Bröring
@copyright   2026 JackTen Internetdienstleistungen GmbH
@link        https://github.com/lb273-src/python-backup-engine
@license     MIT
"""

from .file_core import (
    backup_ts,
    check_paths,
    cleanup_stale_temp_files,
    copy_file,
    format_line,
    get_sha256,
    is_child_path,
    is_different,
    is_dir,
    is_file,
    is_readonly,
    is_subdirectory,
    is_symlink,
    make_directory,
    remove_directory,
    remove_file,
    remove_readonly,
    set_readonly,
    sync_metadata,
    utf8_str,
    # Backward compatibility aliases
    checkPaths,
    copyFile,
    getSha256,
    isDifferent,
    isDir,
    isFile,
    isReadonly,
    makeDirectory,
    pathExists,
    removeDirectory,
    removeFile,
    syncMetadata,
)
from .main_backup import (
    BackupDriveLock,
    find_backup_drive,
    load_jobs,
    main as run_backup,
    validate_jobs,
)
from .sync_logic import (
    DEFAULT_EXCLUDES,
    Synchronizer,
    SyncProtocol,
    build_ignore_patterns,
    is_ignored,
    normalize_rel_path,
    prune_archive,
)

__version__ = "1.2.3"
__author__ = "Dipl.-Ing. (FH) Ludger Bröring"
__license__ = "MIT"

__all__ = [
    # Engine & Protocol
    "Synchronizer",
    "SyncProtocol",
    "DEFAULT_EXCLUDES",
    "build_ignore_patterns",
    "is_ignored",
    "normalize_rel_path",
    "prune_archive",
    # Core File Operations
    "check_paths",
    "cleanup_stale_temp_files",
    "copy_file",
    "get_sha256",
    "is_child_path",
    "is_different",
    "is_dir",
    "is_file",
    "is_readonly",
    "is_subdirectory",
    "is_symlink",
    "make_directory",
    "remove_directory",
    "remove_file",
    "remove_readonly",
    "set_readonly",
    "sync_metadata",
    "backup_ts",
    "utf8_str",
    "format_line",
    # Backward compatibility aliases
    "checkPaths",
    "copyFile",
    "getSha256",
    "isDifferent",
    "isDir",
    "isFile",
    "isReadonly",
    "makeDirectory",
    "pathExists",
    "removeDirectory",
    "removeFile",
    "syncMetadata",
    # Orchestrator & Lock
    "BackupDriveLock",
    "find_backup_drive",
    "load_jobs",
    "validate_jobs",
    "run_backup",
]