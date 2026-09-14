# 🛡️ Sync – Standalone Cross-Platform Python Backup Engine

A production-grade, dependency-free backup and directory synchronization suite written in pure Python for **Windows**, **Linux**, and **macOS**.  
Designed around the principle of **zero data loss**, this engine replaces destructive synchronization with non-destructive versioned archiving, atomic write swaps, streaming checksum validation, and strict drive-level concurrency controls.

**Version:** 1.2.3  
**License:** MIT  
**Requirements:** Python ≥ 3.8 (standard library only)

---

## 📑 Table of Contents
- [✨ Key Highlights](#-key-highlights)
- [🏗️ System Architecture](#-system-architecture)
- [🚀 Quick Start](#-quick-start)
- [⚙️ Configuration Guide (`jobs.json`)](#-configuration-guide-jobsjson)
- [🖥️ Command-Line Interface (CLI)](#-command-line-interface-cli)
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
* **Archive Retention Policy** – optional automatic pruning of archived versions older than N days (`retention_days` or `--retention-days`).
* **Atomic Rollbacks** – on copy/hash failure the previously archived version is restored via `os.replace`.
* **Sharing-Violation Resilience** – automatic retry delays on Windows when files are briefly locked by antiviruses or indexers (`WinError 32 / 5`).
* **Hardware-Aware Drive Detection** – automatically finds USB drives / external volumes by looking for a `.backup_id` marker.
* **Cross-Platform Drive Lock** – prevents concurrent runs on the same medium (`fcntl` / `msvcrt`) with stale-PID recovery and verified release.
* **FAT32 / exFAT Friendly** – 2-second mtime tolerance avoids false re-syncs.
* **Windows Long-Path Support** – paths exceeding 240 characters are handled via conditional `\\?\` prefixing.
* **Cross-Platform Case-Awareness** – case-folding on Windows avoids collisions; optional case-sensitive pattern matching.
* **Strict Path-Traversal Isolation** – `os.path.commonpath` verification prevents directory escape (`..`, drive-scoped, or UNC).
* **Automatic Crash Recovery & Stale-Temp Cleanup** – cleans up abandoned staging files (`tmp_sync_*`, `tmp_rollback_*`) older than 30 minutes at startup.
* **Dry-Run Mode** – full simulation without modifying the target medium.

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

---

## 🔒 Data Safety & Integrity Invariants
* **Copy-before-Prune** – the backup transfer always finishes before any prune analysis or removal starts.
* **Archive-before-Overwrite / Delete** – an obsolete or modified file is only overwritten or removed after being safely copied to `recyclebin`.
* **Streaming Verification** – when `verify_copy=true`, SHA-256 digests of source and target are computed simultaneously during block streaming.
* **Atomic Replace** – staged writes happen via unique temporary files in the destination directory, finalized via atomic `os.replace`.
* **Path-Traversal Barrier** – all configured job destinations are strictly bounded to the backup drive root via `os.path.commonpath`.
* **Stale-Temp Cleanup** – orphaned staging files from aborted previous processes are swept on orchestrator startup.
* **Drive Lock** – exclusive medium lock prevents duplicate process execution; locks are freed strictly after ownership validation.
* **Stale-Lock Recovery** – abandoned locks from dead PIDs or processes older than 2 hours are recycled safely.

---

## ⚠️ Known Limitations & Edge Cases

| Topic | Limitation | Recommendation |
| --- | --- | --- |
| **Network filesystems (SMB/NFS)** | Mandatory locking may be unavailable. The engine falls back to an advisory lock with explicit warnings. | Prefer local / USB drives for critical data. Avoid starting concurrent jobs on the same network share. |
| **Directory Pruning** | When an entire directory subtree no longer exists at source, the orphan tree is moved atomically to the versioned archive (`recyclebin`) with timestamp and UUID token. | Reversible zero-data-loss behavior; directory structures and files remain fully preserved in archive. |
| **Symlinks / Junctions** | Symbolic links and Windows junction points are **skipped** to avoid infinite traversal loops. | Keep mission-critical data in regular directory trees. |
| **Special File Attributes** | Hardlinks, sparse files, ADS, POSIX ACLs, and extended attributes are not preserved. | Use archive formats (e.g. tar/squashfs) if file system metadata beyond mtime/permissions is required. |
| **Memory Footprint** | All relative paths per job are cataloged in an in-memory set to maximize I/O throughput. | Recommended for datasets up to ~2-3 million files per job. |

---

## 📤 Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Success (or dry-run completed cleanly) |
| `1` | Configuration error, missing backup medium, lock conflict, or execution error |

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
├── tests/               # Automated unit & regression test suite (20 tests)
│   └── test_sync.py
├── LICENSE              # MIT License
└── README.md            # This file
```

---

*Dipl.-Ing. (FH) Ludger Bröring – JackTen Internetdienstleistungen GmbH – 2026*