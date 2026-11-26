"""SQLite memory with evidence, deterministic profiles and transactional portable exports.

User choices and model observations are stored separately; audio originals are never copied here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import data_dir

SCHEMA_VERSION = 1
SOURCES = {"user", "inference", "sample", "reference", "rekordbox", "live"}
STATES = {"confirmed", "pending", "retired"}
SENSITIVE = re.compile(r"^(access_token|refresh_token|client_secret|password|authorization|api_key|cookie)$", re.IGNORECASE)
TABLES = {
    "events": ("id", "occurred_at", "source", "kind", "subject", "data", "writer"),
    "facts": ("id", "source", "kind", "name", "data", "event_id"),
    "preferences": ("id", "statement", "domain", "status", "confidence", "evidence", "updated_at", "writer"),
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def checked_time(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include an explicit UTC offset")
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_data(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE.fullmatch(str(key)):
                raise ValueError("Credentials cannot be stored in taste memory")
            validate_data(item)
    elif isinstance(value, list):
        for item in value:
            validate_data(item)
    if len(json_text(value)) > 100_000:
        raise ValueError("Memory item is too large (100000 characters maximum)")


def normalized(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def fact_text(name: str, data: dict) -> str:
    words = [name, str(data.get("artist", "")), str(data.get("genre", "")), str(data.get("user_note", ""))]
    for candidates in data.get("observations", {}).values():
        words.extend(str(candidate.get("label", "")) for candidate in candidates)
    return " ".join(words)


class TasteStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else data_dir() / "taste.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f"Unsupported memory schema {version}; original database was preserved")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, source TEXT NOT NULL,
                    kind TEXT NOT NULL, subject TEXT NOT NULL, data TEXT NOT NULL, writer TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_source ON events(source, occurred_at);
                CREATE TABLE IF NOT EXISTS facts(
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL,
                    name TEXT NOT NULL, data TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES events(id));
                CREATE TABLE IF NOT EXISTS preferences(
                    id TEXT PRIMARY KEY, statement TEXT NOT NULL, domain TEXT NOT NULL,
                    status TEXT NOT NULL, confidence REAL NOT NULL, evidence TEXT NOT NULL,
                    updated_at TEXT NOT NULL, writer TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    item_id UNINDEXED, kind UNINDEXED, text, tokenize='unicode61');
            """)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextlib.contextmanager
    def connection(self, write: bool = False):
        db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _source(source: str) -> None:
        if source not in SOURCES:
            raise ValueError("Unsupported memory source")

    @staticmethod
    def _index(db, item_id: str, kind: str, text: str):
        db.execute("DELETE FROM memory_fts WHERE item_id=? AND kind=?", (item_id, kind))
        db.execute("INSERT INTO memory_fts(item_id,kind,text) VALUES (?,?,?)", (item_id, kind, text))

    def event(self, kind: str, subject: str, *, source: str = "user", data: dict | None = None,
              writer: str = "unknown", event_id: str | None = None, occurred_at: str | None = None) -> str:
        self._source(source)
        payload = data or {}
        validate_data(payload)
        if not kind.strip() or len(subject) > 8000 or len(writer) > 200:
            raise ValueError("Invalid event kind, subject or writer")
        eid = event_id or uuid.uuid4().hex
        with self.connection(True) as db:
            previous = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
            if previous:
                if (previous["source"], previous["kind"], previous["subject"], previous["data"], previous["writer"]) != (
                        source, kind, subject, json_text(payload), writer):
                    raise ValueError("Event ID already exists with different evidence")
                return eid
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
                eid, checked_time(occurred_at or utc_now()), source, kind, subject, json_text(payload), writer))
            self._index(db, eid, "event", subject)
        return eid

    def fact(self, *, source: str, source_id: str, name: str, data: dict, event_id: str,
             kind: str = "track") -> str:
        self._source(source)
        validate_data(data)
        fid = source + ":" + source_id
        with self.connection(True) as db:
            evidence = db.execute("SELECT source FROM events WHERE id=?", (event_id,)).fetchone()
            if evidence is None or evidence[0] != source:
                raise ValueError("Fact requires evidence from its own source")
            db.execute("INSERT INTO facts VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "kind=excluded.kind,name=excluded.name,data=excluded.data,event_id=excluded.event_id",
                       (fid, source, kind, name, json_text(data), event_id))
            self._index(db, fid, "fact", fact_text(name, data))
        return fid

    @staticmethod
    def _check_evidence(db, evidence: list[str], status: str):
        if not evidence or len(evidence) > 100 or len(set(evidence)) != len(evidence):
            raise ValueError("Supply 1 to 100 unique evidence event IDs")
        rows = [db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone() for eid in evidence]
        if any(row is None for row in rows):
            raise ValueError("Evidence event does not exist")
        if status in ("confirmed", "retired") and not any(row["source"] == "user" and row["kind"] in (
                "preference", "choice", "reject", "correction", "confirmation") for row in rows):
            raise ValueError("Only explicit user evidence can confirm a preference; use pending for an inference")

    def remember(self, statement: str, evidence: list[str], *, domain: str = "general",
                 status: str = "pending", preference_id: str | None = None, writer: str = "unknown",
                 contradicts: list[str] | None = None) -> dict:
        if status not in STATES or not statement.strip() or len(statement) > 1000 or len(domain) > 80:
            raise ValueError("Invalid preference statement, domain or status")
        pid = preference_id or uuid.uuid4().hex
        with self.connection(True) as db:
            self._check_evidence(db, evidence, status)
            old = db.execute("SELECT * FROM preferences WHERE id=?", (pid,)).fetchone()
            if preference_id and old is None:
                raise ValueError("Preference ID does not exist")
            if old:
                evidence = sorted(set(evidence) | set(json.loads(old["evidence"])))
            if old and old["status"] == "confirmed" and status == "pending":
                raise ValueError("An inference cannot replace or downgrade a confirmed preference")
            if old and old["status"] == "retired" and status == "pending":
                raise ValueError("Retired preferences require explicit user confirmation to reactivate")
            if old is None and status == "pending" and any(
                    normalized(row[0]) == normalized(statement)
                    for row in db.execute("SELECT statement FROM preferences WHERE status='retired'")):
                raise ValueError("This inference was retired previously; do not propose it again")
            for other in contradicts or []:
                if other == pid or not db.execute("SELECT 1 FROM preferences WHERE id=?", (other,)).fetchone():
                    raise ValueError("Contradiction must reference another existing preference")
            timestamp = utc_now()
            confidence = 1.0 if status == "confirmed" else min(0.75, 0.35 + 0.08 * len(evidence))
            db.execute("INSERT INTO preferences VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "statement=excluded.statement,domain=excluded.domain,status=excluded.status,"
                       "confidence=excluded.confidence,evidence=excluded.evidence,updated_at=excluded.updated_at,"
                       "writer=excluded.writer", (pid, statement.strip(), domain, status, confidence,
                                                  json_text(evidence), timestamp, writer))
            # Lifecycle changes retain the previous assertion as auditable evidence.
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, timestamp, "inference", "preference_revision", pid,
                json_text({"previous": dict(old) if old else None, "statement": statement, "status": status}), writer))
            for other in contradicts or []:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (
                    uuid.uuid4().hex, timestamp, "inference", "preference_conflict", pid,
                    json_text({"other": other, "evidence": evidence}), writer))
            self._index(db, pid, "preference", statement + " " + domain)
        return {"id": pid, "statement": statement.strip(), "domain": domain, "status": status,
                "confidence": confidence, "evidence": evidence}

    def profile(self, domain: str | None = None) -> dict:
        with self.connection() as db:
            prefs = [dict(r) for r in db.execute("SELECT * FROM preferences ORDER BY domain,id")
                     if domain is None or r["domain"] == domain]
            facts = [dict(r) for r in db.execute("SELECT * FROM facts ORDER BY id")]
            counts = {r[0]: r[1] for r in db.execute("SELECT source,count(*) FROM events GROUP BY source")}
            span = db.execute("SELECT min(occurred_at),max(occurred_at) FROM events").fetchone()
            links = [(r[0], json.loads(r[1])["other"]) for r in db.execute(
                "SELECT subject,data FROM events WHERE kind='preference_conflict'")]
        now = datetime.now(UTC)
        confirmed, pending, retired = [], [], []
        for pref in prefs:
            age = max(0, (now - datetime.fromisoformat(pref["updated_at"])).days)
            confidence = round(pref["confidence"] * 0.5 ** (age / 730), 3)
            item = {k: pref[k] for k in ("id", "statement", "domain")}
            item.update(confidence=confidence, evidence=json.loads(pref["evidence"]))
            if pref["status"] == "confirmed" and confidence >= 0.55:
                confirmed.append(item)
            elif pref["status"] == "retired":
                retired.append(item)
            else:
                pending.append(item)
        genres, artists, bpms, keys = Counter(), Counter(), Counter(), Counter()
        active = {pref["id"] for pref in prefs if pref["status"] != "retired"}
        conflicts = [(a, b) for a, b in links if a in active and b in active]
        disputed = {pid for pair in conflicts for pid in pair}
        for item in confirmed:
            if item["id"] in disputed:
                item["needs_user_resolution"] = True
        for fact in facts:
            data = json.loads(fact["data"])
            if data.get("genre"):
                genres[str(data["genre"])] += 1
            if data.get("artist"):
                artists[str(data["artist"])] += 1
            bpm = data.get("bpm")
            if isinstance(bpm, (float, int)) and math.isfinite(bpm) and 30 <= bpm <= 300:
                bpms[str(int(bpm // 10) * 10) + "–" + str(int(bpm // 10) * 10 + 9)] += 1
            if data.get("key"):
                keys[str(data["key"])] += 1

        def ranked(counter):
            return [{"value": k, "count": n} for k, n in sorted(counter.items(), key=lambda x: (-x[1], x[0]))[:8]]

        return {"schema_version": SCHEMA_VERSION, "evidence": {"event_counts": counts,
                "from": span[0], "to": span[1], "library_items": len(facts)},
                "preferences": confirmed[:16], "pending_count": len(pending),
                "conflicts": [{"preferences": list(pair), "state": "needs_user_resolution"} for pair in conflicts[:8]],
                "retired": retired[:8], "library_distribution": {
                    k: ranked(c) for k, c in (("genres", genres), ("artists", artists), ("bpm", bpms), ("keys", keys)) if c},
                "references": [{"id": fact["id"], "label": fact["name"],
                                "observations": json.loads(fact["data"]).get("observations", {}),
                                "user_note": json.loads(fact["data"]).get("user_note", ""),
                                "selection_evidence": json.loads(fact["data"]).get("selection_evidence"),
                                "evidence": [fact["event_id"]], "model_inference": True}
                               for fact in facts if fact["source"] == "reference"][:4],
                "limitations": ["Library ownership is not listening frequency or an explicit preference.",
                                "Liking a reference song does not confirm every feature inferred by its model.",
                                "Confidence decays with a 730-day half-life; thresholds are provisional."],
                "summary": "; ".join(p["statement"] for p in confirmed if p["id"] not in disputed)[:2000]
                or "No unambiguous confirmed preferences yet."}

    def recall(self, query: str, limit: int = 10, offset: int = 0) -> dict:
        if not query.strip() or len(query) > 500 or not 1 <= limit <= 50 or offset < 0:
            raise ValueError("Supply a query (1–500 characters), limit 1–50 and nonnegative offset")
        terms = re.findall(r"\w+", query, re.UNICODE)
        if not terms:
            return {"total": 0, "results": []}
        match = " AND ".join('"' + term + '"*' for term in terms)
        with self.connection() as db:
            # LIKE fallback supports CJK substrings that unicode61 does not segment into words.
            rows = db.execute("SELECT item_id,kind,text FROM memory_fts WHERE memory_fts MATCH ? "
                              "UNION SELECT item_id,kind,text FROM memory_fts WHERE text LIKE ? ORDER BY kind,item_id",
                              (match, "%" + query + "%")).fetchall()
            results = []
            for row in rows[offset:offset + limit]:
                result = {"id": row[0], "kind": row[1], "text": row[2]}
                if row[1] == "preference":
                    pref = db.execute("SELECT status,evidence FROM preferences WHERE id=?", (row[0],)).fetchone()
                    result.update(status=pref[0], evidence=json.loads(pref[1]))
                elif row[1] == "fact":
                    fact = db.execute("SELECT source,event_id FROM facts WHERE id=?", (row[0],)).fetchone()
                    result.update(source=fact[0], evidence=[fact[1]])
                else:
                    event = db.execute("SELECT source,occurred_at FROM events WHERE id=?", (row[0],)).fetchone()
                    result.update(source=event[0], occurred_at=event[1], evidence=[row[0]])
                results.append(result)
        return {"total": len(rows), "results": results}

    def export(self) -> dict:
        with self.connection() as db:
            db.execute("BEGIN")
            tables = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY id")] for table in TABLES}
        return {"format": "live-agent-taste", "schema_version": SCHEMA_VERSION, **tables}

    def import_data(self, document: dict, *, replace_source: str | None = None) -> dict:
        if document.get("format") != "live-agent-taste" or document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported memory export format/version")
        if set(document) != {"format", "schema_version", *TABLES}:
            raise ValueError("Unexpected export fields; credentials cannot be imported")
        validate_data(document) if len(json_text(document)) < 100_000 else self._validate_large(document)
        added = {table: 0 for table in TABLES}
        with self.connection(True) as db:
            if replace_source:
                self._source(replace_source)
                if document["preferences"] or any(row["source"] != replace_source for table in ("events", "facts")
                                                   for row in document[table]):
                    raise ValueError("Source replacement must contain only that source and no preferences")
                db.execute("DELETE FROM facts WHERE source=?", (replace_source,))
            for table, columns in TABLES.items():
                rows = document[table]
                if not isinstance(rows, list) or len(rows) > 1_000_000:
                    raise ValueError("Invalid export row count")
                for row in rows:
                    if set(row) != set(columns):
                        raise ValueError(f"Invalid columns for {table}")
                    if table in ("events", "facts"):
                        self._source(row["source"])
                        validate_data(json.loads(row["data"]))
                    if table == "events":
                        checked_time(row["occurred_at"])
                    if table == "facts":
                        parent = db.execute("SELECT source FROM events WHERE id=?", (row["event_id"],)).fetchone()
                        if parent is None or parent[0] != row["source"]:
                            raise ValueError("Invalid fact evidence")
                    if table == "preferences":
                        if row["status"] not in STATES or not 0 <= float(row["confidence"]) <= 1:
                            raise ValueError("Invalid preference status/confidence")
                        self._check_evidence(db, json.loads(row["evidence"]), row["status"])
                        checked_time(row["updated_at"])
                    old = db.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone()
                    if old and dict(old) != row:
                        raise ValueError(f"Conflicting {table} ID; existing memory preserved: {row['id']}")
                    if old:
                        continue
                    db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                               [row[c] for c in columns])
                    added[table] += 1
            self._reindex(db)
        return {"imported": added}

    @staticmethod
    def _validate_large(document):
        for table in TABLES:
            for row in document[table]:
                validate_data(row)

    def _reindex(self, db):
        db.execute("DELETE FROM memory_fts")
        for eid, subject in db.execute("SELECT id,subject FROM events WHERE kind!='preference_revision'").fetchall():
            self._index(db, eid, "event", subject)
        for fid, name, data in db.execute("SELECT id,name,data FROM facts").fetchall():
            data = json.loads(data)
            self._index(db, fid, "fact", fact_text(name, data))
        for pid, statement, domain in db.execute("SELECT id,statement,domain FROM preferences").fetchall():
            self._index(db, pid, "preference", statement + " " + domain)

    def forget(self, *, preference_id: str | None = None, source: str | None = None, all_data: bool = False) -> dict:
        if sum((bool(preference_id), bool(source), bool(all_data))) != 1:
            raise ValueError("Choose exactly one of preference_id, source or all_data")
        with self.connection(True) as db:
            if preference_id:
                old = db.execute("SELECT evidence FROM preferences WHERE id=?", (preference_id,)).fetchone()
                count = db.execute("DELETE FROM preferences WHERE id=?", (preference_id,)).rowcount
                # Remove prior assertion text, including revision audit payloads, on explicit forgetting.
                db.execute("DELETE FROM events WHERE kind='preference_revision' AND subject=?", (preference_id,))
                if old:
                    used = {eid for row in db.execute("SELECT evidence FROM preferences") for eid in json.loads(row[0])}
                    for eid in json.loads(old[0]):
                        if eid not in used and not db.execute("SELECT 1 FROM facts WHERE event_id=?", (eid,)).fetchone():
                            db.execute("DELETE FROM events WHERE id=?", (eid,))
            elif all_data:
                count = db.execute("SELECT count(*) FROM events").fetchone()[0]
                for table in ("preferences", "facts", "events"):
                    db.execute(f"DELETE FROM {table}")
            else:
                self._source(source)
                ids = {r[0] for r in db.execute("SELECT id FROM events WHERE source=?", (source,))}
                for row in db.execute("SELECT id,evidence FROM preferences").fetchall():
                    if ids.intersection(json.loads(row[1])):
                        db.execute("DELETE FROM preferences WHERE id=?", (row[0],))
                        db.execute("DELETE FROM events WHERE kind='preference_revision' AND subject=?", (row[0],))
                db.execute("DELETE FROM facts WHERE source=?", (source,))
                count = db.execute("DELETE FROM events WHERE source=?", (source,)).rowcount
            self._reindex(db)
        return {"deleted": count, "scope": preference_id or source or "all"}

    def backup(self, target: Path) -> None:
        if target.resolve() == self.path.resolve() or target.exists():
            raise ValueError("Backup destination must be a new file")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db, sqlite3.connect(str(target)) as out:
            db.backup(out)

    def digest(self) -> str:
        return hashlib.sha256(json_text(self.export()).encode()).hexdigest()
