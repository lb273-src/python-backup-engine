# 🛡️ Sync – Standalone Cross-Platform Python Backup Engine

A production-grade, dependency-free backup and directory synchronization suite written in pure Python for **Windows**, **Linux**, and **macOS**.  
Designed around the principle of **zero data loss**, this engine replaces destructive synchronization with non-destructive versioned archiving, atomic write swaps, streaming checksum validation, and strict drive-level concurrency controls.

**Version:** 1.3.0  
**License:** MIT  
**Requirements:** Python ≥ 3.9 (standard library only)

---

## 📑 Table of Contents
- [✨ Key Highlights](#-key-highlights)
- [🎯 Design Philosophy & Scope](#-design-philosophy--scope)
- [🏗️ System Architecture](#-system-architecture)
- [🚀 Quick Start](#-quick-start)
- [⚙️ Configuration Guide (`jobs.json`)](#-configuration-guide-jobsjson)
- [🖥️ Command-Line Interface (CLI)](#-command-line-interface-cli)
- [📊 Protocol & Statistics Example](#-protocol--statistics-example)
- [🔒 Data Safety & Integrity Invariants](#-data-safety--integrity-invariants)
- [⚠️ Known Limitations & Edge Cases](#-known-limitations--edge-cases)
- [📤 Exit Codes](#-exit-codes)
- [📦 Package Layout](#-package-layout)

---

## ✨ Key Highlights
* **Zero Third-Party Dependencies** – pure Python standard library.
* **Copy-before-Prune Guarantee** – new/updated files are fully transferred and verified before any orphaned files are pruned.
* **Inline Streaming SHA-256 Verification** – source and destination checksums are calculated simultaneously during the transfer.
* **In-Memory Differential Pruning** – source paths are cataloged in-memory during backup, eliminating thousands of redundant disk roundtrips.
* **Non-Destructive Archiving (`recyclebin`)** – replaced/deleted files and orphaned directory subtrees are moved to a versioned archive with microsecond timestamp + 10-char UUID token.
* **Pre-Backup Archive Retention Policy** – automatic early pruning of archived versions older than N days (`retention_days` or `--retention-days`) before backup transfers begin, immediately reclaiming disk space.
* **Atomic Rollbacks** – on copy/hash failure the previously archived version is restored via `os.replace`.
* **Sharing-Violation Resilience** – automatic retry delays on Windows when files are briefly locked by antiviruses or indexers (`WinError 32 / 5`).
* **Hardware-Aware Drive Detection** – automatically finds USB drives / external volumes by looking for a `.backup_id` marker.
* **Cross-Platform Drive Lock** – prevents concurrent runs on the same medium (`fcntl` / `msvcrt`) with stale-PID recovery and verified release.
* **FAT32 / exFAT Friendly** – 2-second mtime tolerance avoids false re-syncs.
* **Windows Long-Path Support** – paths exceeding 240 characters are handled via conditional `\\?\` prefixing.
* **Cross-Platform Case-Awareness** – case-folding on Windows avoids collisions; optional case-sensitive pattern matching.
* **Strict Path-Traversal Isolation** – `os.path.commonpath` verification prevents directory escape (`..`, drive-scoped, or UNC).
* **Automatic Crash Recovery & Stale-Temp Cleanup** – cleans up abandoned staging files (`tmp_sync_*`, `tmp_rollback_*`) older than 30 minutes at startup.
* **Comprehensive Storage & Performance Metrics** – tracks transferred volume, speed (MB/s), reclaimed storage, remaining free disk space, and breakdown of created, modified, archived, and unchanged files.
* **Dry-Run Mode** – full simulation without modifying the target medium.

---

## 🎯 Design Philosophy & Scope

### Target Audience & Core Strengths
* **Cleartext File-System Mirroring** – backups are stored directly as standard files and directories on target media. No proprietary containers, opaque chunk databases, or encrypted silos that risk catastrophic unrecoverable corruption from a single damaged sector.
* **Zero Third-Party Dependencies** – relies exclusively on Python standard library modules (`os`, `shutil`, `hashlib`, `uuid`). Completely immune to broken pip dependencies or external package supply-chain risks.
* **Single-Admin / Trusted Workstations & Home-Servers** – engineered for developers, power users, and system administrators backing up their workstations, servers, or external USB drives where `jobs.json` is centrally and trustedly configured.

### Non-Goals / Threat Model Boundaries
* **Multi-Tenant / Shared Hosting** – not designed for environments where untrusted local users have interactive shell access to source directories and could attempt race-condition attacks (e.g. symlink swaps during active traversal).
* **Enterprise Daemons & Webhooks** – operates as a clean, single-run CLI tool. For email or webhook alerting on failures, integrate the process exit code directly into your scheduling wrapper:
  ```bash
  # Linux / macOS cron example
  ./bkup.sh || curl -X POST -d "Backup failed!" https://alert.example.com/webhook
  ```
  ```cmd
  :: Windows Task Scheduler / batch example
  call bkup.bat
  if %errorlevel% neq 0 (
      powershell -Command "Invoke-RestMethod -Uri 'https://alert.example.com/webhook' -Method Post -Body 'Backup failed'"
  )
  ```

---

## 🏗️ System Architecture

```text
 ┌────────────────────────────────────────────────────────┐
 │                    main_backup.py                      │
 │   CLI Parsing | Drive Detection | Drive Lock & Jobs    │
 └──────────────────────────┬─────────────────────────────┘
                            │ invokes
 ┌──────────────────────────▼─────────────────────────────┐
 │                    sync_logic.py                       │
 │  Sync Pipeline | Archive Manager | Multi-Thread Pool   │
 └──────────────────────────┬─────────────────────────────┘
                            │ executes I/O via
 ┌──────────────────────────▼─────────────────────────────┐
 │                    file_core.py                        │
 │  Streaming Copy | Inline SHA-256 | Atomic os.replace   │
 └────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

1. Create a marker file on the root of your backup drive:
   ```bash
   # Windows (PowerShell)
   New-Item -Path "D:\.backup_id" -ItemType File

   # Linux / macOS
   touch /media/user/BACKUP/.backup_id
   ```

2. Configure your synchronization jobs in `jobs.json`.

3. Run the backup:
   ```bash
   # Windows
   .\bkup.bat

   # Linux / macOS
   ./bkup.sh
   ```

---

## ⚙️ Configuration Guide (`jobs.json`)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `source` | string | – | Absolute path of the source directory |
| `target_dir` | string | – | Relative directory name under the backup drive (must not be absolute) |
| `excludes` | list | `[]` | Glob patterns to ignore (`!pattern` = whitelist) |
| `force_hash` | bool | `false` | Always compare via SHA-256 (ignore mtime/size) |
| `verify_copy` | bool | `true` | Perform inline streaming SHA-256 verification |
| `max_workers` | int | `4` | Thread-pool size for parallel file operations |
| `retention_days` | int | `null` | Optional archive retention limit in days (purges older files from `recyclebin`) |
| `case_sensitive_excludes` | bool | `false` | Enforce case-sensitive exclude pattern matching |

---

## 🖥️ Command-Line Interface (CLI)

```bash
python main_backup.py [OPTIONS]
```

| Option | Description |
| --- | --- |
| `-c, --config PATH` | Path to `jobs.json` (default: `jobs.json`) |
| `-d, --drive PATH` | Explicit backup-drive root (skips auto-detection) |
| `-n, --dry-run` | Simulate only – no files are written or archived |
| `-w, --workers N` | Override `max_workers` for all configured jobs |
| `--no-verify` | Disable inline streaming SHA-256 verification |
| `--retention-days N` | Prune archived versions older than N days from `recyclebin` (overrides jobs.json) |
| `--case-sensitive-excludes` | Enforce case-sensitive matching for exclude patterns (overrides jobs.json) |
| `--force-unlock` | Forcefully break and remove any existing drive lock (use with caution) |
| `--lock-timeout SECONDS` | Override stale lock timeout in seconds (default: 7200 / 2 hours) |

---

## 📊 Protocol & Statistics Example

Every execution records an append-only audit trail to `protocol.txt` and outputs a comprehensive real-time statistics block upon completion:

```text
--- Backup Sync Statistics ---
#21-09-2026-16:38:00
FilesChecked:             1,250
FilesUnchanged:           1,235
FilesCreated:                 8
FilesModified:                7
FilesUpdated:                15
MetadataUpdated:              2
FilesDeleted:                 3
FilesArchived:               10
DirectoriesChecked:         140
DirectoriesCreated:           1
DirectoriesDeleted:           0
DirectoriesArchived:          0
ArchiveFilesPruned:          45
ArchiveDirectoriesPruned:     4
DataTransferred:         1.42 GB (42.50 MB/s)
ArchiveSpaceReclaimed:   5.80 GB reclaimed
TargetSpaceBefore:       179.80 GB free (320.20 GB used of 500.00 GB)
TargetSpaceAfter:        184.20 GB free (315.80 GB used of 500.00 GB)
TargetSpaceDelta:        +4.40 GB net freed
TargetFreeSpace:         184.20 GB of 500.00 GB free
SymlinksSkipped:              0
Errors Encountered:           0
Total Time:              33.40 seconds (0h 0m 33.40s)
```

---

## 🔒 Data Safety & Integrity Invariants
* **Copy-before-Prune** – the backup transfer always finishes completely before any prune analysis or orphan removal starts.
* **Zero-Gap Overwrite Protection (Hardlink-First & Copy-First)** – before replacing an existing backup file, it is linked (`os.link` on NTFS/ext4/APFS/btrfs) or duplicated (`shutil.copy2` on FAT32/exFAT) into the versioned archive. The original file remains continuously in place at its target path until atomic `os.replace` swaps it with the validated new version. There is zero window of non-existence.
* **Fatal Storage Abort Guarantee (`FatalBackupError`)** – if the backup medium runs out of disk space (`errno.ENOSPC` / WinError 112) or becomes read-only (`errno.EROFS`), the engine immediately halts worker threads (`cancel_futures=True`) and skips subsequent pruning, ensuring valid existing backup data is never deleted under disk pressure.
* **Streaming Verification** – when `verify_copy=true`, SHA-256 digests of source and target are computed simultaneously during block streaming.
* **Atomic Replace** – staged writes happen via unique temporary files in the destination directory, finalized via atomic `os.replace`.
* **Pre-Backup Storage Reclamation** – expired archive files and empty directories in `recyclebin` are pruned *before* file copying starts, ensuring maximum free capacity and preventing out-of-disk-space errors.
* **Path-Traversal Barrier** – all configured job destinations are strictly bounded to the backup drive root via `os.path.commonpath`.
* **Automatic Crash Recovery** – orphaned staging files (`tmp_sync_*`, `tmp_rollback_*`) older than 30 minutes and empty lock files older than 10 seconds are swept on orchestrator startup.
* **Multi-Host Drive Lock** – exclusive medium lock with `PID`, `Hostname`, `UUID`, and timestamp prevents concurrent runs across different machines; locks are freed strictly after ownership validation.
* **Stale-Lock Recovery & Force-Unlock** – abandoned locks from dead local PIDs or foreign hosts older than 2 hours (configurable via `--lock-timeout`) are recycled safely. Administrators can use `--force-unlock` for immediate override.

---

## ⚠️ Known Limitations & Edge Cases

| Topic | Limitation | Recommendation |
| --- | --- | --- |
| **Network filesystems (SMB/NFS)** | Mandatory locking may be unavailable. The engine falls back to an advisory lock with explicit warnings. | Prefer local / USB drives for critical data. Avoid starting concurrent jobs on the same network share. |
| **Directory Pruning** | When an entire directory subtree no longer exists at source, the orphan tree is moved atomically to the versioned archive (`recyclebin`) with timestamp and UUID token. | Reversible zero-data-loss behavior; directory structures and files remain fully preserved in archive. |
| **Symlinks / Junctions** | Symbolic links and Windows junction points are **skipped** to avoid infinite traversal loops. Tracked and reported in `SymlinksSkipped`. | Keep mission-critical data in regular directory trees. |
| **Special File Attributes** | Hardlinks, sparse files, ADS, POSIX ACLs, and extended attributes are not preserved. | Use archive formats (e.g. tar/squashfs) if file system metadata beyond mtime/permissions is required. |
| **Memory Footprint** | All relative paths per job are cataloged in an in-memory set to maximize I/O throughput. | Recommended for datasets up to ~2-3 million files per job. |
| **Copy-First on FAT32/exFAT** | On filesystems without hardlink support (`os.link`), the engine duplicates files (`shutil.copy2`) into the archive before overwriting. During transfer of large files, up to ~2× the file size of free disk space is temporarily needed until the old target is replaced. | Format backup media with NTFS (Windows), ext4/btrfs (Linux), or APFS (macOS) whenever possible to enable instantaneous, zero-storage Hardlink-First archiving. |

---

## 📤 Exit Codes

The orchestrator returns distinct exit codes for reliable integration with cron, systemd, Nagios, Zabbix, and monitoring alerts:

| Code | Status | Meaning & Recommended Action |
| --- | --- | --- |
| `0` | **Success** | All jobs completed cleanly with 0 errors (or dry-run finished). |
| `1` | **Configuration / Drive Error** | `jobs.json` missing/invalid, CLI syntax error, or backup drive marker (`.backup_id`) not found. |
| `2` | **Drive Lock Conflict** | The target drive is currently locked by an active process or another host. If a previous run crashed, use `--force-unlock`. |
| `3` | **Fatal Storage Error** | Disk full (`ENOSPC` / WinError 112) or read-only volume (`EROFS`). Synchronisation was aborted immediately and pruning was skipped to prevent data loss. Alert admin immediately. |
| `4` | **Partial Errors** | Backup completed, but one or more individual files encountered non-fatal read/write/permission errors. Review `protocol.txt`. |

---

## 📦 Package Layout

```text
.
├── __init__.py          # Public API exports
├── file_core.py         # Low-level I/O, streaming copy, retry logic, long-path support
├── sync_logic.py        # Core synchronizer, protocol handler, in-memory pruning
├── main_backup.py       # CLI orchestrator & drive locking
├── bkup.sh / bkup.bat   # Cross-platform convenience launchers
├── jobs.json            # Synchronization job definitions (user-specific, git-ignored)
├── jobs.json.example    # Configuration template
├── tests/               # Automated unit & regression test suite (57 tests)
│   └── test_sync.py
├── LICENSE              # MIT License
└── README.md            # This file
```

---

*Dipl.-Ing. (FH) Ludger Bröring – JackTen Internetdienstleistungen GmbH – 2026*