"""Read-only favorite-song imports and persistent local model analysis jobs."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from ..taste.paths import data_dir
from .runtime import model_directory, python_executable

VERSION = "reference-clap-v1"


class ReferenceLibrary:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory else data_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "references.db"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS reference_jobs(
                id TEXT PRIMARY KEY,path TEXT,label TEXT,user_note TEXT,remember INTEGER,
                state TEXT,created REAL,updated REAL,pid INTEGER,result TEXT,error TEXT)""")

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def validate_path(path: str) -> Path:
        from ..samples import AUDIO_EXT

        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("Supply the absolute path of the song selected by the user")
        candidate = candidate.resolve(strict=True)
        if not candidate.is_file() or candidate.suffix.lower() not in AUDIO_EXT:
            raise ValueError("Choose a supported local audio file")
        if not 0 < candidate.stat().st_size <= 2_000_000_000:
            raise ValueError("Reference audio must be nonempty and no larger than 2 GB")
        return candidate

    def start(self, path: str, *, label: str = "", user_note: str = "", remember: bool = True) -> dict:
        candidate = self.validate_path(path)
        model_directory()
        executable = python_executable()
        if len(label) > 200 or len(user_note) > 1000:
            raise ValueError("Label/note is too long")
        # Avoid duplicate work for the same source while a job is alive; completed imports may be reanalyzed.
        with self.connection() as db:
            prior = db.execute("SELECT id FROM reference_jobs WHERE path=? AND state IN ('queued','running','cancelling') "
                               "AND updated>? ORDER BY created DESC LIMIT 1", (str(candidate), time.time() - 1800)).fetchone()
            if prior:
                return self.status(prior[0])
            job, now = uuid.uuid4().hex, time.time()
            db.execute("INSERT INTO reference_jobs VALUES (?,?,?,?,?,'queued',?,?,NULL,NULL,NULL)",
                       (job, str(candidate), label.strip(), user_note.strip(), int(remember), now, now))
        args = [executable, "-m", "live_agent_bridge.audio.reference_worker", "--directory", str(self.directory), "--job", job]
        environment = dict(os.environ, AGENT_BRIDGE_DATA_DIR=str(self.directory), OMP_NUM_THREADS="2",
                           OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2", HF_HUB_OFFLINE="1",
                           TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                           HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false", PYTHONDONTWRITEBYTECODE="1")
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + environment.get("PYTHONPATH", "")
        environment.pop("TRANSFORMERS_CACHE", None)
        cache = self.directory / "model-cache"
        environment.update(HF_HOME=str(cache / "huggingface"), TORCH_HOME=str(cache / "torch"),
                           NUMBA_CACHE_DIR=str(cache / "numba"), MPLCONFIGDIR=str(cache / "matplotlib"))
        log_dir = self.directory / "reference-logs"
        log_dir.mkdir(exist_ok=True)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            with (log_dir / (job + ".log")).open("wb") as log:
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=environment, **options)
            with self.connection() as db:
                db.execute("UPDATE reference_jobs SET pid=? WHERE id=?", (process.pid, job))
        except OSError:
            with self.connection() as db:
                db.execute("UPDATE reference_jobs SET state='error',error='Could not start local model worker' WHERE id=?", (job,))
            raise ValueError("Could not start the local model worker") from None
        return self.status(job)

    def status(self, job: str, include_result: bool = False) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM reference_jobs WHERE id=?", (job,)).fetchone()
        if row is None:
            raise ValueError("Reference analysis job does not exist")
        result = {key: row[key] for key in ("id", "label", "state", "created", "updated", "error")}
        result["stale"] = row["state"] in ("queued", "running", "cancelling") and time.time() - row["updated"] > 1800
        if include_result:
            result["analysis"] = json.loads(row["result"]) if row["result"] else None
        return result

    def list(self) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT id FROM reference_jobs ORDER BY created DESC LIMIT 30").fetchall()
        return {"results": [self.status(row[0]) for row in rows]}

    def cancel(self, job: str) -> dict:
        with self.connection() as db:
            db.execute("UPDATE reference_jobs SET state='cancelling' WHERE id=? AND state IN ('queued','running')", (job,))
        return self.status(job)
