# 📜 Regelwerk für Formatierung & Code-Qualität (Python Backup Engine)

Dieses Dokument definiert die verbindlichen Formatierungs-, Qualitäts- und Architekturstandards für den Quellcode der **Python Backup & Synchronization Suite** (`python-backup-engine`).

---

## 1. Einrückung & Whitespace (Indentation & Whitespace)
- **4 Leerzeichen:** Die Einrückung beträgt ausnahmslos 4 Leerzeichen pro Ebene (PEP 8 Standard).
- **Keine Tabulatoren:** Tab-Zeichen (`\t`) sind im Quellcode verboten und werden durch 4 Leerzeichen ersetzt.
- **Kein Trailing Whitespace:** Nachstehende Leerzeichen am Ende von Zeilen werden konsequent entfernt.
- **Code-Start in Zeile 1:** Jede Datei startet direkt in **Zeile 1** mit dem Modul-Docstring (keine führenden Leerzeilen).
- **Dateiende (POSIX / Git-Standard):** Am Ende jeder Datei befindet sich **exakt ein Zeilenumbruch (LF)**. Mehrfache leere Zeilen am Dateiende werden entfernt.

---

## 2. Dateikodierung & Zeilenumbrüche (Encoding & EOL)
- **Zeichenkodierung:** Alle Dateien sind in **UTF-8 ohne BOM** (Byte Order Mark) gespeichert.
- **Zeilenumbruch-Typ:** Einheitlich **LF** (Unix-Format `\n`) für alle Python-Dateien (`.py`), Shell-Skripte (`.sh`), JSON- und Markdown-Dateien. Windows-Batch-Dateien (`.bat`) dürfen systemspezifisch CRLF nutzen.

---

## 3. Dateikopf & Moduldokumentation (Module Header Docstring)
- **Standard-Header:** Jede Python-Datei beginnt ab Zeile 1 mit einem standardisierten Modul-Docstring im folgenden Format:

```python
"""
Python Backup & Synchronization Suite

@package     sync
@subpackage  Core
@file        file_core.py
@description Low-level file system I/O, streaming copy with inline SHA-256 validation, retry logic, and path utilities.
@author      Dipl.-Ing. (FH) Ludger Bröring
@copyright   2026 JackTen Internetdienstleistungen GmbH
@link        https://github.com/lb273-src/python-backup-engine
@license     MIT
"""
```

---

## 4. Typisierung & Methodendokumentation (Type Hints & Docstrings)
- **Vollständige Type Annotations (PEP 484):** Alle Funktionen, Methoden und Konstruktoren sind mit Type Hints für Parameter und Rückgabewerte versehen (`typing`-Modul).
- **Docstrings (PEP 257):** Jede Klasse, Methode und öffentliche Funktion wird mit einem klaren Docstring beschrieben.
- **Englische Kommentare:** Alle Kommentare, Docstrings, Log-Texte und internen Fehlermeldungen sind ausnahmslos auf **Englisch** zu verfassen (*All code comments and docstrings must be in English*).
- **Keine redundanten Kommentare:** Kommentare erklären das *Warum*, Randfälle oder Invarianten, nicht offensichtliche Python-Syntax.

---

## 5. Architektur- & Qualitätsprinzipien

### A. Zero External Dependencies (Standardbibliothek pur)
- Das gesamte Projekt nutzt **ausschließlich die Python-Standardbibliothek**.
- Externe Abhängigkeiten via `pip` (z. B. `psutil`, `click`, `requests`, `pydantic`) sind strikt unzulässig, um maximale Portabilität und wartungsfreie Ausführung auf beliebigen Systemen zu garantieren.
- Mindestanforderung: **Python ≥ 3.9**.

### B. Datenintegrität & Atomarität (Data Safety First)
- **Atomare Ersetzung:** Dateien werden immer in temporäre Zwischendateien (`tmp_sync_*`) im selben Zielverzeichnis geschrieben und nach Hash-Prüfung atomar per `os.replace` platziert.
- **Resource Management:** Dateideskriptoren (`mkstemp`) werden im Exception-Pfad sofort geschlossen, um Windows File-Sharing-Locks (`WinError 32`) zu verhindern.
- **Fail-Safe Rollback:** Überschreibungen werden vorab gesichert (Hardlink oder Kopie in `recyclebin`), bei Fehlern erfolgt ein atomarer Rollback.
- **Drive Locking:** Vor Schreibzugriffen wird ein exklusiver Multi-Host-Drive-Lock erworben und während langer Kopiervorgänge per periodischem Heartbeat validiert. Bei Entzug oder Lock-Diebstahl bricht das Programm sofort mit `RuntimeError` ab.

### C. Plattformunabhängigkeit
- Pfadoperationen unterstützen Windows Long Paths (`\\?\`), Posix-Berechtigungen sowie Case-Preserving/Case-Insensitive Dateisysteme (Windows NTFS, macOS APFS).

### D. Testgetriebene Absicherung
- Jede neue Funktionalität, Optimierung oder Fehlerbehebung muss durch automatisierte Tests in `tests/test_sync.py` (unter Verwendung des Standard-`unittest`-Frameworks) abgedeckt werden. Alle Tests müssen fehlerfrei und ohne Warnungen durchlaufen.
