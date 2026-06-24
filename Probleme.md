# Probleme & Befunde — Aether-Vault Code-Audit

Stand: 2026-06-23

Vollständiges Inventar aller im Audit gefundenen Probleme (Edge-Cases, Speicherlecks,
Logikfehler, Performance-Engpässe, Sicherheitsrisiken), jeweils **bewertet 1–10** nach
Wichtigkeit.

- **Status `✅ behoben`** = im Code gefixt (Schweregrad ≥ 5 und reine Code-Sicherheitslücken).
- **Status `🔸 offen`** = bewusst nicht geändert (Deployment-Config / zu umfangreich / Grad 1–4),
  hier dokumentiert zur späteren Behebung.

---

## ✅ Behoben (Schweregrad ≥ 5 + Code-Sicherheitslücken)

### [10] Paralleler Hash ≠ kanonischer SHA-256 → Content-Addressing & Remote-Upload kaputt
- **Datei:** `src/core.cpp` (Bindung von `hash_file`).
- **Problem:** `aether_core.hash_file` zeigte auf `hash_file_parallel`, das für Dateien ≥ 16 MB
  einen *Tree-Hash* (SHA-256 über die Konkatenation der Chunk-Hashes) statt des echten
  Datei-SHA-256 liefert. Der Python-Fallback und die Server-Verifikation
  (`storage.store_object`) nutzen aber den echten SHA-256.
- **Auswirkung:** Dieselbe Datei bekam je nach Core-Verfügbarkeit/Größe unterschiedliche
  Hashes; Whole-File-Uploads großer Nicht-`.safetensors`-Artefakte (`.pt`, `.parquet`,
  `.csv`, `.h5`) schlugen mit HTTP 400 fehl → Kernfunktion (Remote-Sync/Checkout) defekt.
- **Fix:** `hash_file` an `hash_file_sequential` (kanonischer SHA-256) gebunden;
  Tree-Hash weiterhin verfügbar als `hash_file_tree`; Invarianten-Kommentar ergänzt.

### [7] Path-Traversal / LFI über `ref_name`
- **Datei:** `python/av_server/server.py` (Ref-Endpoints), `python/av_server/storage.py`.
- **Problem:** `GET/PUT /api/refs/{ref_name:path}` reichte `ref_name` ungeprüft an die
  Filesystem-Fallbacks (`refs_dir / ref_name`) weiter; `../../…` ermöglichte Lesen/Schreiben
  außerhalb des Datenverzeichnisses.
- **Fix:** Zentrale `validate_ref_name()` (Whitelist, kein `..`/absolut/Backslash) in
  `update_ref`/`get_ref`; zusätzlich defensive `_safe_ref_path()` in `CASStorage`
  (resolve muss unterhalb `refs_dir` liegen).

### [6] `os.walk` durchläuft gesamten CAS-Objektspeicher + fehlerhafte Substring-Filter
- **Datei:** `python/av_cli/main.py` (`add`, `status`).
- **Problem:** `if ".av" in root` ist (a) ein Substring-Test (Ordner wie `data.average`
  wurden fälschlich übersprungen) und (b) prunt nicht → `os.walk` stieg in `.av/objects`
  (zehntausende Shards) hinab.
- **Fix:** Gemeinsamer Helper `iter_working_files()` mit In-Place-Pruning
  (`dirnames[:] = …`) und Pfadkomponenten-Prüfung.

### [6] `av add` re-hasht jede Datei vollständig, auch unveränderte
- **Datei:** `python/av_cli/main.py` (`add`).
- **Problem:** Jede Datei wurde unabhängig von Änderungen voll eingelesen/gehasht.
- **Fix:** Vor dem Hashen `compare_meta_safe` gegen den Index; bei Übereinstimmung
  (Größe + mtime) wird die Datei übersprungen.

### [5] Speicher-Spike: Layer-Extraktion liest ganze Layer in den RAM
- **Datei:** `python/av_cli/main.py` (`add`, safetensors-Pfad).
- **Problem:** `dst_f.write(src_f.read(l_size))` lud einen kompletten Layer (bis GB) in den
  Speicher.
- **Fix:** Gechunktes Kopieren in 8-MB-Blöcken.

### [5] C++ `split_and_hash_safetensors`: fehlende Validierung → OOM/DoS
- **Datei:** `src/core.cpp`.
- **Problem:** Unvalidierte 8-Byte-`header_size` → beliebig große Allokation; `end - start`
  konnte underflowen, Offsets über EOF.
- **Fix:** Bounds-Checks (`header_size` muss in die Datei passen, `end >= start`,
  `base_offset + end <= file_size`).

### [5] Nach `checkout` erscheint alles als „modified"/„staged"
- **Datei:** `python/av_cli/main.py` (`checkout`).
- **Problem:** Index-Einträge wurden mit `mtime_ns=0` geschrieben und durch das
  Neu-Einfügen in einen geleerten Index als `staged` markiert.
- **Fix:** Nach dem Materialisieren reale `size`/`mtime_ns` erfassen und `staged=False`
  setzen → sauberer Arbeitsbaum nach Checkout.

### [5] `checkout` überschreibt/löscht Arbeitskopie ohne Dirty-Check → Datenverlust
- **Datei:** `python/av_cli/main.py` (`checkout`).
- **Problem:** Getrackte Dateien wurden bedingungslos überschrieben/gelöscht.
- **Fix:** Dirty-Check (modifizierte/gelöschte/staged Dateien) bricht ab; neues `--force`/`-f`
  zum bewussten Verwerfen.

### [5] DB-Spalte `size` als 32-bit `Integer` → Overflow bei > 2 GB
- **Datei:** `python/av_server/models.py` (`DBObject.size`, `DBTree.size`).
- **Problem:** Postgres `INTEGER` (max ~2,1 GB) für ein Tool, das Multi-GB-Dateien
  versioniert.
- **Fix:** `BigInteger`. **Achtung:** greift nur bei **frischer DB** — der Server nutzt
  `Base.metadata.create_all` ohne Migrationen. Für eine bestehende DB ist ein manuelles
  `ALTER TABLE objects ALTER COLUMN size TYPE BIGINT;` (analog `trees`) bzw. Alembic nötig.

### [5] `/api/stats` macht bei jedem Dashboard-Refresh einen vollen Filesystem-Walk
- **Datei:** `python/av_server/server.py` (`get_stats`).
- **Problem:** Bei jedem Aufruf (WebUI pollt ~alle 15 s) Walk + `stat()` über alle Shards.
- **Fix:** DB-Aggregate (`count`/`sum`); Filesystem-Walk nur noch als Fallback bei leerer DB.

### [5] Server ignoriert Autoren-Zeit des Commits → falsche Sortierung
- **Datei:** `python/av_server/server.py` (`push_commit`).
- **Problem:** `DBCommit.timestamp` defaultete auf Insert-Zeit; zusammen mit der
  Pending-Push-Queue sortierte das Dashboard Commits falsch.
- **Fix:** `commit_data["timestamp"]` (ISO 8601) wird geparst und gesetzt; Fallback `utcnow()`.

---

## 🔸 Offen — Deployment-Sicherheit (bewusst nur dokumentiert)

> Auf Wunsch nicht im Code/Config geändert. Für produktiven Einsatz dringend zu beheben.

### [7] Keine Authentifizierung + offene Angriffsfläche
- **Dateien:** `python/av_server/server.py`, `docker-compose.yml`.
- **Punkte:**
  - Keinerlei Auth/AuthZ auf irgendeinem Endpoint.
  - CORS `allow_origins=["*"]` (`server.py`, `add_middleware`).
  - **Destruktiver** `POST /api/admin/gc` ist unauthentifiziert — jeder erreichbare Client
    kann Storage löschen.
  - Postgres-Port `5432` ist im Compose nach außen gemappt; Default-Credentials
    `av_user/av_password` hartkodiert.
  - Redis-Port `6379` ebenfalls offen, ohne Passwort.
- **Empfehlung:** API-Token/Reverse-Proxy mit Auth, CORS auf bekannte Origins beschränken,
  Admin-/GC-Endpoint absichern, DB/Redis-Ports nicht nach außen binden, Secrets über
  Umgebung/Secret-Store statt Defaults.

---

## ✅ Behoben — vormals „Architektur" (Grad 4–5)

### [5] GC ist Mark-and-Sweep ohne Locking (Race Condition)
- **Datei:** `python/av_server/server.py` (`run_garbage_collection`, `purge_orphans`).
- **Problem:** Ein paralleler Upload, dessen Commit noch nicht erfasst ist, konnte gelöscht
  werden (Live-Objekt wurde als verwaist eingestuft).
- **Fix:** Grace-Period (`GC_GRACE_SECONDS`, 1 h). Objekte, deren DB-Row `created_at` bzw.
  deren Shard-Datei-`mtime` jünger als das GC-Startfenster ist, werden nie gelöscht — der
  Zeitraum zwischen Objekt-Upload und Commit-Push ist damit geschützt, ohne globalen Lock.

### [4] N+1-DB-Queries beim Tree-Traversal
- **Datei:** `python/av_server/server.py` (`resolve_tree`, `_collect_alive_in_memory`).
- **Problem:** Eine separate DB-Query pro Tree-Knoten → langsam bei tiefen/breiten Bäumen.
- **Fix:** `get_commit` traversiert jetzt level-weise mit **einer** gebatchten Query pro
  Tiefenebene (dedup-sicher über Pfad-Präfixe). Die GC-Mark-Phase lädt **alle** `DBTree`-Rows
  in **einer** Query und traversiert rein im Speicher (`_collect_alive_in_memory`).
- **Zusatz:** Löschungen in GC laufen jetzt gebatcht (`_GC_DELETE_BATCH`), um die
  Bind-Parameter-Grenze von asyncpg nicht zu sprengen (ehemals separater Grad-3-Punkt).

### [4] Cross-Language-mtime-Inkonsistenz
- **Datei:** `python/av_cli/main.py` (`get_file_meta_safe`/`compare_meta_safe`).
- **Problem:** C++ `fs::last_write_time` (impl.-definierte Epoche, z.B. 1601) vs. Python
  `st_mtime_ns` (Unix-Epoche). Bei gemischten Pfaden spurious „modified".
- **Fix:** Metadaten (Größe/mtime) laufen jetzt **durchgängig** über Pythons `os.stat`
  (eine einzige Unix-Epoche, exakt selbstkonsistent). Der C++-Core wird nur noch fürs
  Hashing genutzt; der unbenutzte C++-Metadaten-Pfad wurde aus der CLI entfernt.

---

## ✅ Behoben — vormals „Kleinere Punkte" (Grad 1–4)

| Grad | Datei | Problem | Fix |
|---|---|---|---|
| 4 | `python/av_cli/pointer.py` (`is_pointer_file`) | Las Binärdateien im Textmodus via `readline()`; bei Datei ohne frühes Newline potenziell großer Read. | **Behoben:** Liest nur noch die festen Magic-Bytes (`_POINTER_MAGIC`) im Binärmodus. |
| 4 | `python/av_cli/main.py` (`commit`) | Commit-JSON und Ref wurden nicht atomar geschrieben (Crash-Fenster). | **Behoben:** `atomic_write_text`/`atomic_write_json` (Temp-Datei + `fsync` + `os.replace`); Commit-Objekt wird vor dem Ref geschrieben. |
| 4 | `webui/src/lib/api.ts` (`fetchCommitsForBranches`) | Lud Commits seriell über die Parent-Kette (Waterfall, N Round-Trips). | **Behoben:** Neue `fetchCommits()` holt die neuesten Commits in **einem** `/api/commits`-Request; Dashboard-Fetches laufen parallel via `Promise.all`. |
| 3 | `python/av_server/server.py` (`upload_object`) | Parallele Uploads desselben Hashes → `IntegrityError`/HTTP 500. | **Behoben:** `IntegrityError` wird abgefangen → idempotent HTTP 409. |
| 3 | `python/av_server/server.py` (`push_commit`) | Vertraute unbegrenzten Client-`metrics`/`tree` (DoS-Potenzial). | **Behoben:** Limits (`MAX_TREE_ENTRIES`, `MAX_METRICS`, `MAX_TAGS`, `MAX_TAG_LEN`, `MAX_MESSAGE_LEN`) → HTTP 422 bei Überschreitung. |
| 3 | `python/av_server/models.py` (`DBCommit.parent_hash` FK) | FK-Verletzung, wenn Parent-Commit nicht auf dem Server lag → 500. | **Behoben:** FK auf `parent_hash` entfernt (shallow/out-of-order Pushes erlaubt; Spalte indexiert); zusätzlich `IntegrityError`→409 in `push_commit`. |
| 3 | `python/av_server/server.py` (`run_garbage_collection`) | `dead_hashes.in_(list)` konnte asyncpg-Parametergrenze sprengen. | **Behoben:** Löschen in Batches (`_GC_DELETE_BATCH`). |
| 2 | `python/av_server/models.py`, `server.py` | Deprecations: `datetime.utcnow()`, `@app.on_event("startup")`. | **Behoben:** `utcnow_naive()` (tz-aware → naive UTC) überall; FastAPI-`lifespan`-Handler statt `on_event`. |
| 2 | `python/av_cli/main.py` (`save_pending_push`, `update_registry`, `save_config`) | Nicht-atomare JSON-Schreibvorgänge. | **Behoben:** über `atomic_write_json`/`atomic_write_text`. |
| 2 | `src/core.cpp` (`hash_file_parallel`) | ThreadPool-Overhead bei Dateien knapp über 2× Chunkgröße. | **Behoben:** Parallelisierung erst ab `PARALLEL_MIN_CHUNKS` (8 Chunks ≈ 64 MB). |
| 1 | `python/av_cli/client.py` (`VaultClient.session`) | `requests.Session` wurde nie geschlossen (kein echtes Leck). | **Behoben:** `close()` + Context-Manager (`__enter__`/`__exit__`) + defensiver `__del__`. |

---

## ✅ Behoben — Weight Diffing (Visual) Feature-Implementierung (2026-06-24)

Neues Feature: vollständig client-seitige visuelle Diff-UI im Web UI (Sidebar-Tab „Weight
Diff"). Checkpoints lassen sich per Drag-and-Drop (oder Klick) aus einer Liste in zwei
Vergleichsslots ziehen; ein Heatmap-Grid und ein Recharts-Balkendiagramm zeigen, welche
`.safetensors`-Layer sich zwischen zwei Commits geändert haben — ohne neue Server-Endpunkte,
da `GET /api/commits/{hash}` die Per-Layer-Hashes bereits liefert. Beim End-to-End-Test mit
echten synthetischen Checkpoints (zwei `.safetensors`-Versionen, 5 Tensoren, 1 absichtlich
verändert) wurden folgende **vorbestehende** Bugs aufgedeckt, die das Feature (und teils auch
die bereits ausgelieferte CLI-Funktion `av handoff --diff-weights`) komplett unbrauchbar
gemacht hätten:

### [10] Commits werden vor ihren Objekten gepusht → Server-Sync für Artefakte komplett unbrauchbar
- **Dateien:** `python/av_cli/main.py` (`commit`, `flush_pending_push`),
  `python/av_server/server.py` (`push_commit`), `python/av_server/models.py` (`DBTree`).
- **Problem (zwei sich verstärkende Bugs):**
  1. **Falsche Reihenfolge:** `commit` rief `client.push_commit(commit_data)` auf, **bevor**
     die referenzierten Objekte/Layer-Shards hochgeladen wurden (`flush_pending_push` lud sie
     überhaupt **nie** hoch — der Upload-Code existierte nur inline im Live-Commit-Pfad). Der
     Server speichert pro Tree-Eintrag aber `object_hash` als **Foreign Key** auf `objects.hash`
     ([models.py:41](python/av_server/models.py#L41) vor dem Fix). Das Insert in `trees`
     schlug daher praktisch **immer** mit `ForeignKeyViolationError` fehl.
  2. **Falsch zugeordnete Fehlerbehandlung:** `push_commit` fing *jeden* `IntegrityError` ab
     und gab pauschal `409 "Commit already exists"` zurück
     ([server.py:307](python/av_server/server.py#L307) vor dem Fix) — unabhängig davon, ob der
     Commit wirklich schon existierte oder ob (wie hier) ein ganz anderer Constraint
     (Tree→Objekt-FK, später auch Ref→Commit-FK) verletzt wurde. Der Client behandelt 409
     bewusst als idempotenten Erfolg (Design für gleichzeitige Pushes desselben Hashes) — und
     wurde so über einen Totalausfall hinweggetäuscht: `av commit`/`av push` meldeten
     durchgehend Erfolg, aber **Commits und Refs landeten nie in der Datenbank**
     (`SELECT * FROM commits` → 0 Zeilen, trotz „✓ Pushed 2 commit(s)" auf der Konsole).
  3. **Tiefere Ursache:** Selbst mit korrekter Reihenfolge schlägt das Insert für **jede**
     Layer-gesplittete `.safetensors`-Datei weiterhin fehl: Beim Layer-Split wird die
     Ganzdatei (`object_hash`) bewusst **nie** als eigenes Objekt hochgeladen (nur die
     Layer-Shards, um doppelte Speicherung zu vermeiden) — der FK auf `objects.hash` verlangt
     aber genau das.
  4. Zusätzlich wurde der Rückgabewert von `client.update_ref(...)` nirgends geprüft — ein
     fehlgeschlagenes Ref-Update wurde als „erledigt" behandelt statt erneut in die
     Pending-Queue gestellt.
- **Auswirkung:** Jeder Commit mit einer `.safetensors`-Datei über der LFS-Schwelle (also
  genau der Use-Case dieses Tools) konnte **nie erfolgreich synchronisiert** werden — weder
  live noch über die Offline-Pending-Queue. Das gesamte „Weight Diffing"-Feature (CLI **und**
  neue Web-UI) lief leer, weil schlicht keine zweite Version eines Modells je auf dem Server
  ankam.
- **Fix:**
  - Gemeinsamer Helper `upload_commit_objects()` (statt dupliziertem Inline-Code), der **vor**
    `push_commit()` aufgerufen wird — sowohl im Live-Commit-Pfad als auch in
    `flush_pending_push()` (vorher: Objekte wurden beim Queue-Replay nie hochgeladen).
  - `DBTree.object_hash` verliert seinen `ForeignKey("objects.hash")` (analog zur bereits
    vorher entfernten `parent_hash`-FK) — der Hash bleibt als Inhalts-Identität erhalten, ohne
    eine physische Objekt-Zeile zu erzwingen, die für Layer-gesplittete Dateien nie existiert.
  - `push_commit` prüft nach einem `IntegrityError` jetzt **erneut per Hash**, ob der Commit
    tatsächlich existiert, bevor es 409 zurückgibt; andernfalls 500 mit echter Fehlermeldung
    (kein falsches „Erfolg" mehr für den Client).
  - `flush_pending_push`/`commit` prüfen jetzt das Ergebnis von `update_ref()` und behalten den
    Commit in der Pending-Queue, falls das Ref-Update fehlschlägt.
- **Verifiziert (End-to-End gegen echten Docker-Stack):** Vier reale Commits mit
  Layer-gesplitteten Synthetik-Checkpoints erzeugt; vor dem Fix landeten 0 von 2 Commits in
  der DB (`SELECT hash FROM commits` leer) trotz Erfolgsmeldung. Nach dem Fix: Live-Push UND
  Offline-Queue-Push landen beide korrekt in `commits`/`refs`; `GET /api/commits/{hash}`
  liefert die erwarteten Per-Layer-Hashes; ein Node-Skript, das exakt die Browser-Diff-Logik
  nachbildet, bestätigt den korrekten Layer-Diff zwischen zwei echten Server-Commits.
- **Hinweis:** Bestehende Dev-Datenbanken (vor diesem Fix erstellt via `create_all`, keine
  Migrationen) behalten die alte FK-Constraint physisch im Schema, bis sie manuell entfernt
  wird (`ALTER TABLE trees DROP CONSTRAINT trees_object_hash_fkey;`) oder die DB neu angelegt
  wird — bereits dokumentiertes Migrations-Caveat, siehe Eintrag zu `BigInteger`/`parent_hash`.

### [9] `av add` persistiert Per-Layer-Hashes nie auf die Festplatte
- **Datei:** `python/av_cli/main.py` (`add`), `python/av_cli/index.py` (`Index.add_entry`).
- **Problem:** `idx.add_entry(...)` ruft intern bereits `self.save()` auf (Parameter
  `auto_save=True`), **bevor** der Aufrufer in `add` die Zeile
  `idx.entries[rel_path]["layers"] = layers` ausführt. Die Layer-Liste landet damit nur im
  In-Memory-Dict des bereits beendeten `add`-Prozesses — die `.av/index`-Datei auf der Platte
  hat nie `"layers"`. Da `av commit` den Index in einem **neuen** Prozess frisch von der Platte
  lädt, war `tree[rel_path]["layers"]` in jedem Commit-Objekt immer `[]`, unabhängig von der
  Konsolenausgabe „Staged [ARTIFACT] … (LFS, 6 layers)". Folge: Sowohl `av handoff
  --diff-weights` als auch die neue Web-UI fielen für **jede** `.safetensors`-Datei auf den
  Ganzdatei-Hash-Vergleich zurück (`status: changed`, aber keine einzelne geänderte Layer
  gemeldet) — das komplette „Per-Layer Weight Diffing"-Feature aus README Phase 11 war seit
  dessen Einführung wirkungslos.
- **Fix:** Nach dem Setzen von `idx.entries[rel_path]["layers"]` wird explizit `idx.save()`
  aufgerufen, damit die Layer-Daten tatsächlich persistiert werden.
- **Verifiziert:** Zwei synthetische `.safetensors`-Commits erzeugt (Layer `layer2.weight`
  geändert); `av handoff --diff-weights` meldet danach korrekt
  `changed layers: layer2.weight`, vorher nur `status: changed` ohne Detail.

### [4] `atomic_write_text`-Temp-Dateiname kann Windows-`MAX_PATH` sprengen
- **Datei:** `python/av_cli/main.py` (`atomic_write_text`).
- **Problem:** Der Temp-Suffix `f".tmp.{os.getpid()}.{uuid.uuid4().hex}"` (PID + voller
  32-stelliger UUID4-Hex) macht zusammen mit einem 64-Zeichen-Commit-Hash-Dateinamen und einem
  tief verschachtelten Repo-Pfad (z.B. CI-Runner-Tempverzeichnisse, OneDrive-Sync-Ordner) den
  Gesamtpfad leicht länger als Windows' 260-Zeichen-`MAX_PATH`. Statt „nur" lang zu sein,
  schlägt der `open()`-Aufruf dann mit `FileNotFoundError` fehl — der gesamte `av commit`
  bricht ab, obwohl das eigentliche Ziel (atomare, crash-sichere Schreibvorgänge) das Gegenteil
  von „Operation schlägt fehl" sein sollte. Beim Testen dieses Features reproduzierbar
  aufgetreten (Repo unter einem tiefen Temp-Pfad).
- **Fix:** Temp-Suffix auf einen kurzen 8-stelligen Zufalls-Hex reduziert (PID entfernt, war
  ohnehin redundant zur UUID für Kollisionsvermeidung).

### [3] Synthetischer `__header__`-Pseudo-Layer verschmutzt die Diff-Ansicht
- **Datei:** `webui/src/lib/diffWeights.ts` (neu).
- **Problem:** `aether_core.split_and_hash_safetensors` gibt neben den echten Tensoren einen
  zusätzlichen Eintrag `__header__` zurück (Hash über den safetensors-JSON-Header, für die
  Rekonstruktions-Integrität). `av_cli/main.py` übernimmt diesen ungefiltert in
  `idx.entries[rel_path]["layers"]`. In der bisherigen CLI-Textausgabe (`--diff-weights`) fiel
  das kaum auf; in einer **visuellen** Layer-für-Layer-Ansicht wäre es jedoch ein verwirrender,
  nicht-Tensor-Eintrag in Heatmap und Drift-Chart.
- **Fix:** `diffFile()` filtert Layer-Einträge mit Namen `__header__` heraus, bevor sie in die
  UI-Diff-Struktur einfließen (rein clientseitig, keine Änderung an Core/Server/Index-Format).

### 🔸 Offen — Checkpoint-Liste löst N Commits über N parallele Requests auf
- **Datei:** `webui/src/components/WeightDiffPanel.tsx`.
- **Problem:** `GET /api/commits` (Listen-Endpoint) liefert **keine** Tree-/Layer-Daten (nur
  Metadaten); um die Checkpoint-Liste mit `rel_path`/Layer-Infos zu befüllen, muss für jeden
  in Frage kommenden Commit einzeln `GET /api/commits/{hash}` aufgerufen werden. Um nicht exakt
  das N+1-Request-Muster zu wiederholen, das in diesem Projekt bereits einmal für
  `fetchCommitsForBranches` behoben wurde, laufen diese Requests **parallel** (`Promise.all`,
  nicht seriell) und sind hart auf `CHECKPOINT_FETCH_LIMIT = 30` begrenzt.
- **Warum nicht behoben:** Eine echte Lösung bräuchte einen neuen Server-Endpunkt (z.B.
  `GET /api/commits?include_layers=true`), was laut Scope-Entscheidung für dieses Feature
  explizit vermieden werden sollte (keine Server-/API-Änderungen). Bei Repos mit nur einigen
  Dutzend Commits unproblematisch; bei sehr langer Historie mit vielen Checkpoints wäre ein
  Aggregat-Endpunkt nötig.

### 🔸 Offen — `Index.save()` ist nicht atomar
- **Datei:** `python/av_cli/index.py` (`Index.save`).
- **Problem:** Schreibt `.av/index` direkt (`open(..., 'w')`), ohne das in `main.py` bereits
  etablierte Temp-Datei+`fsync`+`os.replace`-Muster (`atomic_write_text`/`atomic_write_json`).
  Ein Crash mitten im Schreiben kann eine abgeschnittene/leere Index-Datei hinterlassen.
- **Warum nicht behoben:** Außerhalb des Scopes dieses Features; konsistent mit der bereits in
  diesem Dokument unter „Kleinere Punkte" gelisteten Kategorie nicht-atomarer Schreibvorgänge
  — zur späteren Behebung vorgemerkt.
