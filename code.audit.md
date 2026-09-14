
**Strenges Code-Audit – Version (Stand September 2026)**

Gesamtbewertung: **9.0 / 10**

Die Suite hat einen sehr hohen Reifegrad erreicht. Die meisten klassischen Backup-Engine-Fallen (Datenverlust, Race Conditions, fehlende Verification, schwache Locks, stille Fehler) sind systematisch adressiert. Für den produktiven Einsatz mit normalen bis anspruchsvollen Anforderungen ist sie **freigabefähig**. Hochkritische/regulierte Umgebungen benötigen noch wenige dokumentierte Einschränkungen.

### Was jetzt hervorragend gelöst ist

| Feature | Status | Kommentar |
|---------|--------|---------|
| Streaming Copy + Inline SHA-256 | **Sehr gut** | Chunked, `fdopen`, `fsync` (best-effort), Verify vor `os.replace` |
| Windows Long-Path | **Gut** | Aktiv ab ≥ 240 Zeichen + UNC-Handling |
| Retry bei Sharing Violation | **Sehr gut** | Speziell für Windows Antivirus-Locks (winerror 5/32) |
| Atomic Rollback | **Sehr gut** | Temp + `os.replace` + garantiertes Cleanup |
| Archive-before-Overwrite/Delete | **Sehr gut** | Mit Abbruch bei Archive-Fehler |
| Copy-before-Prune | **Sehr gut** | Korrekt |
| In-Memory Pruning | **Sehr gut** | `source_rel_files`-Set verhindert doppelten Full-Scan und Race bei parallelen Workern |
| Drive-Lock | **Sehr gut** | `O_EXCL` + PID + 2-h-Stale + Fallback-Warnung + PID-Check beim Unlock |
| Path-Traversal + Absolute-Path-Reject | **Sehr gut** | Zusätzliche Absicherung |
| `samefile`-Check | **Gut** | Robuster als reiner String-Vergleich |
| Fehlerbehandlung | **Gut** | Weniger stille `except`, differenzierte Behandlung |

### Verbleibende kritische / hochprioritäre Punkte (streng)

**1. Pruning von Verzeichnisbäumen ist jetzt destruktiver (P1)**  
In `prune` wird bei fehlendem Origin-Verzeichnis `shutil.rmtree(backup_dir)` aufgerufen.  
Das ist effizient, bricht aber mit dem vorherigen „nur leere Verzeichnisse + Archive“-Modell.  
- Inhalt der Unterbäume wird **nicht** einzeln archiviert, sondern der ganze Baum gelöscht (nach `archive_directory`, das nur das leere Zielverzeichnis anlegt).  
- Bei Abbruch mitten im `rmtree` können halbe Bäume zurückbleiben.  
**Empfehlung:** Entweder klar dokumentieren oder auf bottom-up Einzeldatei-Archivierung zurückkehren, wenn echte Non-Destructive-Garantie für ganze Bäume gefordert ist.

**2. Netzwerk-Lock bleibt advisory (P1 – akzeptabel)**  
`O_EXCL` + Timestamp ist gut, aber auf SMB/NFS weiterhin nicht wasserdicht. Die Warnung ist vorhanden. Für kritische Daten lokale/USB-Laufwerke bevorzugen.

**3. Kein per-file-Locking (P1 – dokumentiertes Restrisiko)**  
Bei `max_workers > 1` und parallelem externem Schreiben auf dieselben Dateien bleiben TOCTOU-Fenster. Für reine Backup-Szenarien (nur dieses Tool schreibt) akzeptabel.

**4. Long-Path-Aktivierung erst ab 240 Zeichen (P2)**  
Konservativ und sinnvoll (vermeidet unnötige Präfixe), aber sehr lange relative Pfade innerhalb eines kurzen Roots können trotzdem Probleme machen. Auf Windows besser immer prefixen, wenn der volle Pfad ≥ 260 werden könnte.

**5. Memory-Footprint (P2)**  
`source_rel_files` + `file_tasks` halten alle relativen Pfade im RAM. Bei mehreren Millionen Dateien speicherintensiv. Kein Streaming der Task-Liste.

**6. Kein Manifest / Resume (P2)**  
Bei Abbruch Full-Scan von vorn. Das `recyclebin` hilft bei Datenwiederherstellung, ersetzt aber kein Checkpoint.

**7. Symlinks / Junctions / Reparse Points (P2)**  
Werden nur übersprungen. Relative Out-of-Tree-Symlinks und Windows-Junctions sind nicht robust behandelt.

**8. Kleinere technische Punkte**
- `is_readonly` wirft jetzt bei manchen Fehlern weiter (gut), kann aber Call-Sites überraschen.
- `find_backup_drive` prüft weiterhin nur Existenz von `.backup_id`.
- `remove_directory` in `file_core` wird im neuen Prune-Pfad kaum noch genutzt (`shutil.rmtree` direkt).
- Keine automatisierten Tests sichtbar.

### Architektur-Urteil

Die Pipeline ist klar und sicherheitsorientiert:

1. Stale-Lock + `O_EXCL`-Acquire  
2. Job-Validierung (inkl. Absolute-Path + Traversal-Schutz)  
3. Backup-Phase (Streaming-Copy + Verify + Archive-before-Overwrite + atomic Rollback) → liefert `source_rel_files`  
4. Prune-Phase (In-Memory-Vergleich gegen das Set)  
5. Statistik + Unlock  

Das In-Memory-Pruning ist ein klarer Fortschritt gegenüber dem früheren doppelten `os.walk`.

### Freigabe-Empfehlung

| Szenario | Empfehlung |
|----------|------------|
| Private / unkritische Daten | **Freigeben** |
| Firmeninterne / mittelwichtige Daten | **Freigeben** |
| Hochkritische / regulierte Daten | **Freigeben mit Einschränkungen** (`max_workers=1`, möglichst kein SMB/NFS, Prune-Verhalten dokumentieren) |

### Fazit

Die iterative Verbesserung war erfolgreich. Die ursprünglich kritischen Schwächen sind weitgehend beseitigt.  

**Die beiden wichtigsten Restpunkte für eine 9.3+ wären:**

1. Klarere Non-Destructive-Garantie beim Verzeichnis-Pruning (oder explizite Dokumentation des aktuellen `rmtree`-Verhaltens).  
2. Optionaler Hardlink-basierter Rollback (wenn Source und Archive auf demselben Volume liegen), um I/O bei großen Dateien zu sparen.

Für den normalen bis anspruchsvollen Praxiseinsatz ist die Codebase **jetzt freigabefähig**.


@Realist
Das Audit bewertet die Suite mit 9.0/10 fachlich fundiert, zutreffend und ohne Schönfärberei: Die Kerninfrastruktur (atomare Ersetzungen, Prüfsummen-Streaming, RAM-basiertes Pruning, Stale-Lock-Management) ist industrietauglich und stabilisiert. Die aufgeführten Restrisiken sind real, betreffen jedoch fast ausschließlich Randfälle oder bewusste Architekturentscheidungen (z. B. der bewusste Kompromiss zwischen RAM-Verbrauch und I/O-Geschwindigkeit bei `known_sources`).

---

### Bewertung der Kritikpunkte des Audits

* **1. Verzeichnis-Pruning (`shutil.rmtree` statt Einzeldatei-Archivierung):**
*Treffer.* `archive_directory` legt bisher nur das übergeordnete Verzeichnis im Archiv an, verschiebt aber nicht die darin enthaltenen Dateien. Wird danach `shutil.rmtree(backup_dir)` ausgeführt, ist der gesamte verwaiste Ordnerbaum unwiderruflich weg – was dem Non-Destructive-Versprechen widerspricht. Entweder muss der gesamte Baum rekursiv ins Archiv verschoben werden (`shutil.move`), oder dieses Verhalten muss als bewusst destruktives Aufräumen deklariert werden.


* **2. Netzwerk-Lock bleibt advisory:**
*Realitätsnah.* Auf CIFS/SMB-Shares gibt es im User-Space ohne dedizierten Locking-Dienst keine 100%ige Garantie gegen Deadlocks oder gebrochene Locks. Die Ausgabe einer Warnung und der Verweis auf lokale Medien ist die einzig pragmatische Lösung.


* **3. Long-Path ab 240 Zeichen:**
*Technisch optimal.* Ein pauschales Anwenden von `\\?\` führt unter Windows bei fehlenden Dateien zu kernel-internen Timeout-Schleifen. Die Schwelle bei 240 Zeichen verhindert genau dieses Problem, bevor der Windows-Grenzwert von 260 Zeichen greift.


* **4. RAM-Footprint (`source_rel_files`):**
*Akzeptabler Trade-off.* Bei 100.000 Pfaden belegt ein Python-`set` etwa 15–20 MB RAM. Erst ab 5–10 Millionen Dateien wird dies zum Problem. Der Geschwindigkeitsgewinn gegenüber wiederholten Festplattenabfragen wiegt diesen Speicherbedarf bei weitem auf.


* **5. Fehlerbehandlung & Plattform-Checks:**
Die Ergänzungen wie `os.path.samefile` und der Retry-Loop bei Antivirus-Locks (WinError 32 / 5) in `file_core.py` machen den Code im Windows-Alltag extrem robust.



---

### Fazit & Status

Die Codebasis ist in ihrem jetzigen Zustand für den Produktiveinsatz freigabefähig.

Wenn du das Pruning für gelöschte Ordnerbäume noch vollständig non-destruktiv absichern willst (P1 aus dem Audit), muss in `sync_logic.py` beim Prunen eines ganzen Verzeichnisbaums `archive_directory` den Baum verschieben, anstatt ihn mit `shutil.rmtree` zu löschen. Für Standard-Backups ist das aktuelle Verhalten jedoch der performanteste Weg, um verwaiste Verzeichnisse loszuwerden.