"""
Python Backup & Synchronization Suite

@package     Sync
@subpackage  Core
@file        file_core.py
@description Low-level file system I/O, streaming copy with inline SHA-256 validation, retry logic, and path utilities.
@author      Dipl.-Ing. (FH) Ludger Bröring
@copyright   2026 JackTen Internetdienstleistungen GmbH
@link        https://github.com/lb273-src/python-backup-engine
@license     MIT
"""

import errno
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Union

PathLike = Union[str, Path]

HASH_BUFFER_SIZE = 1048576  # 1 MB buffer
MTIME_TOLERANCE_SECONDS = 2.0


def _long_path(path: PathLike) -> str:
    p = os.path.abspath(os.fspath(path))
    if sys.platform == "win32" and len(p) >= 240:
        if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
            return p
        if p.startswith("\\\\"):
            return "\\\\?\\UNC\\" + p[2:]
        return "\\\\?\\" + p
    return p


def remove_file(path: Optional[PathLike]) -> bool:
    if is_file(path):
        try:
            remove_readonly(path)
            os.remove(_long_path(path))
            return True
        except OSError as e:
            raise OSError(f"Error when deleting the file {path}: {e}") from e
    return False


def remove_directory(path: Optional[PathLike]) -> bool:
    if not is_dir(path):
        return False

    errors = []

    def _handle_removal_error(func, path_item, exc_info):
        try:
            remove_readonly(path_item)
            func(path_item)
        except Exception as err:
            errors.append(f"{path_item}: {err}")

    try:
        if sys.version_info >= (3, 12):
            def _on_exc(func, path_item, _exc):
                _handle_removal_error(func, path_item, _exc)
            shutil.rmtree(path, onexc=_on_exc)  # type: ignore[arg-type]
        else:
            shutil.rmtree(path, onerror=_handle_removal_error)  # type: ignore[arg-type]

        if path_exists(path) or errors:
            err_details = "; ".join(errors) if errors else "Directory still exists."
            raise OSError(f"Directory could not be fully deleted ({path}): {err_details}")
        return True
    except OSError as e:
        raise OSError(f"Error when deleting the directory {path}: {e}") from e


def make_directory(path: Optional[PathLike]) -> bool:
    if path is None:
        return True

    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as e:
        raise OSError(f"Error when creating directory {path}: {e}") from e


def copy_file(source: PathLike, destination: PathLike, follow_symlinks: bool = False, verify_hash: bool = True) -> bool:
    if not is_file(source):
        return False

    source_lp = _long_path(source)
    destination_lp = _long_path(destination)

    ro_flag = is_readonly(source)
    dest_dir = os.path.dirname(str(destination)) or "."
    make_directory(dest_dir)

    fd, tmp_destination = tempfile.mkstemp(prefix="tmp_sync_", dir=dest_dir)
    tmp_destination_lp = _long_path(tmp_destination)
    src_hasher = hashlib.sha256() if verify_hash else None
    dst_hasher = hashlib.sha256() if verify_hash else None

    try:
        with open(source_lp, "rb", buffering=0) as src, os.fdopen(fd, "wb", buffering=0) as dst:
            while True:
                buf = src.read(HASH_BUFFER_SIZE)
                if not buf:
                    break
                if verify_hash:
                    src_hasher.update(buf)  # type: ignore[union-attr]
                dst.write(buf)
                if verify_hash:
                    dst_hasher.update(buf)  # type: ignore[union-attr]
            dst.flush()
            try:
                os.fsync(dst.fileno())
            except OSError:
                pass

        shutil.copystat(source_lp, tmp_destination_lp, follow_symlinks=follow_symlinks)

        if verify_hash:
            src_digest = src_hasher.hexdigest()  # type: ignore[union-attr]
            dst_digest = dst_hasher.hexdigest()  # type: ignore[union-attr]
            if src_digest != dst_digest:
                raise OSError(f"Streaming hash verification failed! Source: {src_digest} != Dest: {dst_digest}")

        if is_file(destination):
            remove_readonly(destination)

        # Retry logic for Windows file-sharing violations (e.g. Virus scanner lock)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                os.replace(tmp_destination_lp, destination_lp)
                break
            except OSError as e:
                if attempt < max_retries - 1 and sys.platform == "win32" and e.winerror in (5, 32):  # Access denied / Sharing violation
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise

        if ro_flag:
            set_readonly(destination)
        return True
    except BaseException as e:
        if is_file(tmp_destination):
            try:
                os.remove(_long_path(tmp_destination))
            except OSError:
                pass
        if isinstance(e, OSError):
            raise OSError(f"Error copying file from {source} to {destination}: {e}") from e
        raise


def sync_metadata(source: PathLike, destination: PathLike, follow_symlinks: bool = False) -> bool:
    if is_file(source) and is_file(destination):
        try:
            stat_src = os.stat(_long_path(source))
            stat_dst = os.stat(_long_path(destination))

            mtime_diff = abs(stat_src.st_mtime - stat_dst.st_mtime)
            mode_diff = (stat_src.st_mode != stat_dst.st_mode)

            if mtime_diff > MTIME_TOLERANCE_SECONDS or mode_diff:
                if is_readonly(destination):
                    remove_readonly(destination)
                shutil.copystat(_long_path(source), _long_path(destination), follow_symlinks=follow_symlinks)
                return True
        except OSError as e:
            raise OSError(f"Metadata synchronization failed between {source} and {destination}: {e}") from e
    return False


def path_exists(path: Optional[PathLike]) -> bool:
    if path is None:
        return False
    return os.path.exists(_long_path(path))


def is_file(path: Optional[PathLike]) -> bool:
    if path is None:
        return False
    return os.path.isfile(_long_path(path))


def is_dir(path: Optional[PathLike]) -> bool:
    if path is None:
        return False
    return os.path.isdir(_long_path(path))


def is_symlink(path: Optional[PathLike]) -> bool:
    if path is None:
        return False
    try:
        return os.path.islink(_long_path(path))
    except (OSError, ValueError):
        return False


def is_child_path(parent: PathLike, child: PathLike) -> bool:
    try:
        p = Path(parent).resolve()
        c = Path(child).resolve()
        if p == c:
            return False
        return c.is_relative_to(p)
    except (ValueError, RuntimeError, OSError, AttributeError):
        try:
            Path(child).resolve().relative_to(Path(parent).resolve())
            return Path(parent).resolve() != Path(child).resolve()
        except (ValueError, RuntimeError, OSError):
            return False


is_subdirectory = is_child_path


def check_paths(origin_path: PathLike, backup_path: PathLike) -> Tuple[bool, str]:
    try:
        p_orig = Path(origin_path).resolve()
        p_back = Path(backup_path).resolve()
    except Exception as e:
        return False, f"Failed resolving paths: {e}"

    if not is_dir(p_orig):
        return False, f"{origin_path} is not an existing directory."
    if not is_dir(p_back):
        return False, f"{backup_path} is not an existing directory."

    try:
        if os.path.samefile(p_orig, p_back):
            return False, "Origin and backup paths are identical."
    except OSError:
        if sys.platform == "win32" and str(p_orig).lower() == str(p_back).lower():
            return False, "Origin and backup paths are identical."
        elif str(p_orig) == str(p_back):
            return False, "Origin and backup paths are identical."

    if is_child_path(p_orig, p_back):
        return False, f"{backup_path} is subdirectory of {origin_path}"
    if is_child_path(p_back, p_orig):
        return False, f"{origin_path} is subdirectory of {backup_path}"

    return True, "Paths are valid."


def remove_readonly(file_path: PathLike) -> None:
    try:
        if is_readonly(file_path):
            lp = _long_path(file_path)
            current_permissions = os.stat(lp).st_mode
            os.chmod(lp, current_permissions | stat.S_IWRITE | stat.S_IWUSR)
    except OSError as e:
        if e.errno == errno.EPERM and os.access(_long_path(file_path), os.W_OK):
            return
        raise PermissionError(f"Error removing write protection from {file_path}: {e}") from e


def set_readonly(file_path: PathLike) -> None:
    try:
        if not is_readonly(file_path):
            lp = _long_path(file_path)
            current_permissions = os.stat(lp).st_mode
            mode = current_permissions & ~(stat.S_IWRITE | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            os.chmod(lp, mode)
    except OSError as e:
        if e.errno == errno.EPERM and not os.access(_long_path(file_path), os.W_OK):
            return
        raise PermissionError(f"Error setting write protection for {file_path}: {e}") from e


def is_readonly(file_path: PathLike) -> bool:
    try:
        lp = _long_path(file_path)
        if not os.path.isfile(lp):
            return False

        result = not os.access(lp, os.W_OK)
        file_stat = os.stat(lp)
        if sys.platform == "win32":
            result = result or not bool(file_stat.st_mode & stat.S_IWRITE)
        else:
            result = not bool(file_stat.st_mode & stat.S_IWUSR)
        return result
    except (OSError, PermissionError) as e:
        if isinstance(e, PermissionError) or (isinstance(e, OSError) and e.errno in (errno.EACCES, errno.EPERM)):
            return True
        raise OSError(f"Could not check permissions for {file_path}: {e}") from e


def get_sha256(file_path: PathLike) -> str:
    sha256_hash = hashlib.sha256()
    try:
        with open(_long_path(file_path), "rb") as f:
            for byte_block in iter(lambda: f.read(HASH_BUFFER_SIZE), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except OSError as e:
        raise OSError(f"Error reading file for hashing {file_path}: {e}") from e


def is_different(f0: PathLike, f1: PathLike, force_hash: bool = False) -> bool:
    if not is_file(f0):
        if not path_exists(f0):
            raise FileNotFoundError(f"Source file does not exist: {f0}")
        return False

    if not path_exists(f1) or not is_file(f1):
        return True

    try:
        if not force_hash:
            stat0 = os.stat(_long_path(f0))
            stat1 = os.stat(_long_path(f1))
            if stat0.st_size == stat1.st_size and abs(stat0.st_mtime - stat1.st_mtime) <= MTIME_TOLERANCE_SECONDS:
                return False

        return get_sha256(f0) != get_sha256(f1)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ESTALE):
            return True
        raise


def format_line(value: str) -> str:
    return f"{value}\n"


utf8_str = format_line


def backup_ts() -> str:
    return f"#{datetime.now().strftime('%d-%m-%Y-%H:%M:%S')}"