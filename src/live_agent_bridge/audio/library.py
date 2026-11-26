"""Persistent analysis jobs and content cache in the existing sample index."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .features import VERSION


def content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class AudioLibrary:
    def __init__(self, db_path: str):
        self.path = str(db_path)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS audio_content(
                    hash TEXT, version TEXT, result TEXT NOT NULL, PRIMARY KEY(hash,version));
                CREATE TABLE IF NOT EXISTS audio_paths(
                    path TEXT PRIMARY KEY, hash TEXT, size INTEGER, mtime_ns INTEGER, version TEXT);
                CREATE TABLE IF NOT EXISTS audio_jobs(
                    id TEXT PRIMARY KEY, state TEXT, created REAL, updated REAL, error TEXT, pid INTEGER);
                CREATE TABLE IF NOT EXISTS audio_queue(
                    job TEXT, path TEXT, state TEXT, error TEXT, PRIMARY KEY(job,path));
                CREATE TABLE IF NOT EXISTS audio_embeddings(
                    hash TEXT, model TEXT, vector TEXT NOT NULL, PRIMARY KEY(hash,model));
            """)

    def connection(self):
        # Explicit close at call sites (sqlite's context manager commits but does not close).
        from contextlib import contextmanager

        @contextmanager
        def connect():
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
        return connect()

    def validate_path(self, path: str) -> Path:
        from ..samples import AUDIO_EXT, SampleIndex

        candidate = Path(path).resolve(strict=True)
        roots = SampleIndex(self.path).roots()
        if (candidate.suffix.lower() not in AUDIO_EXT or not candidate.is_file()
                or not any(candidate.is_relative_to(Path(root.path).resolve()) for root in roots)):
            raise ValueError("Choose an audio file inside an indexed sample root; add the folder first")
        if candidate.stat().st_size > 2_000_000_000:
            raise ValueError("Audio file exceeds the 2 GB per-file analysis limit")
        return candidate

    def result(self, path: str) -> dict | None:
        candidate = Path(path)
        try:
            stat = candidate.stat()
        except OSError:
            return None
        with self.connection() as db:
            row = db.execute("SELECT c.result,p.size,p.mtime_ns FROM audio_paths p JOIN audio_content c "
                             "ON c.hash=p.hash AND c.version=p.version WHERE p.path=? AND p.version=?",
                             (str(candidate.resolve()), VERSION)).fetchone()
        if row and row[1] == stat.st_size and row[2] == stat.st_mtime_ns:
            return json.loads(row[0])
        return None

    def analyze(self, path: str, semantic=None) -> dict:
        from .features import analyze_file

        candidate = self.validate_path(path)
        before = candidate.stat()
        digest = content_hash(candidate)
        with self.connection() as db:
            row = db.execute("SELECT result FROM audio_content WHERE hash=? AND version=?", (digest, VERSION)).fetchone()
        result = json.loads(row[0]) if row else analyze_file(str(candidate))
        after = candidate.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Audio changed during analysis; retry after the writer finishes")
        vector = None
        if semantic is not None:
            # embeddings depend only on content + model, not on the signal-analysis version: reuse them
            with self.connection() as db:
                cached = db.execute("SELECT vector FROM audio_embeddings WHERE hash=? AND model=?",
                                    (digest, semantic.model_id)).fetchone()
            vector = json.loads(cached[0]) if cached else semantic.audio(str(candidate))
        result["sha256"] = digest
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO audio_content VALUES (?,?,?)", (digest, VERSION, json.dumps(result)))
            db.execute("INSERT OR REPLACE INTO audio_paths VALUES (?,?,?,?,?)",
                       (str(candidate), digest, after.st_size, after.st_mtime_ns, VERSION))
            if vector is not None:
                db.execute("INSERT OR REPLACE INTO audio_embeddings VALUES (?,?,?)",
                           (digest, semantic.model_id, json.dumps(vector)))
        return result

    def start(self, path: str | None = None, limit: int = 100, semantic: bool = False) -> dict:
        if not 1 <= limit <= 10000:
            raise ValueError("Analysis batch limit must be 1 to 10000")
        if path:
            paths = [str(self.validate_path(path))]
        else:
            model = None
            if semantic:
                from .runtime import model_directory
                from .semantic import weights_fingerprint

                model = weights_fingerprint(model_directory())
            with self.connection() as db:
                paths = []
                for row in db.execute("SELECT path FROM samples ORDER BY path").fetchall():
                    # Skip files that are already complete for this request, so a follow-up batch
                    # continues after an earlier one instead of re-queueing it.
                    if self.result(row[0]) is None or (model and not self._has_embedding(db, row[0], model)):
                        paths.append(row[0])
                    if len(paths) >= limit:
                        break
        if not paths:
            return {"state": "complete", "queued": 0}
        job = uuid.uuid4().hex
        now = time.time()
        with self.connection() as db:
            db.execute("INSERT INTO audio_jobs VALUES (?,?,?,?,?,?)", (job, "queued", now, now, None, None))
            db.executemany("INSERT INTO audio_queue VALUES (?,?,'queued',NULL)", [(job, p) for p in paths])
        return self._spawn(job, semantic)

    @staticmethod
    def _has_embedding(db, path: str, model: str) -> bool:
        return db.execute("SELECT 1 FROM audio_paths p JOIN audio_embeddings e ON e.hash=p.hash "
                          "WHERE p.path=? AND p.version=? AND e.model=?",
                          (str(Path(path).resolve()), VERSION, model)).fetchone() is not None

    def _spawn(self, job: str, semantic: bool = False) -> dict:
        from .runtime import python_executable

        args = [python_executable() if semantic else sys.executable, "-m", "live_agent_bridge.audio.worker", "--db", self.path, "--job", job]
        if semantic:
            args.append("--semantic")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            with open(os.devnull, "wb") as sink:
                environment = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + environment.get("PYTHONPATH", "")
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
                                           env=environment, **options)
            with self.connection() as db:
                db.execute("UPDATE audio_jobs SET pid=? WHERE id=?", (process.pid, job))
        except OSError:
            with self.connection() as db:
                db.execute("UPDATE audio_jobs SET state='error',error='Worker could not start' WHERE id=?", (job,))
            raise ValueError("Could not start the analysis worker") from None
        return self.status(job)

    def resume(self, job: str, semantic: bool = False) -> dict:
        from ..taste.locking import file_lock

        with file_lock(Path(self.path + ".audio-worker.lock"), timeout=0.1), self.connection() as db:
            row = db.execute("SELECT state,updated FROM audio_jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise ValueError("Analysis job does not exist")
            if row[0] in ("running", "queued", "cancelling") and time.time() - row[1] < 300:
                raise ValueError("Worker is active; wait for cancellation before resuming")
            db.execute("UPDATE audio_queue SET state='queued',error=NULL WHERE job=? AND state!='done'", (job,))
            db.execute("UPDATE audio_jobs SET state='queued',error=NULL,updated=? WHERE id=?", (time.time(), job))
        return self._spawn(job, semantic)

    def cancel(self, job: str) -> dict:
        with self.connection() as db:
            db.execute("UPDATE audio_jobs SET state='cancelling' WHERE id=? AND state IN ('running','queued')", (job,))
        return self.status(job)

    def status(self, job: str | None = None) -> dict:
        with self.connection() as db:
            if job:
                row = db.execute("SELECT * FROM audio_jobs WHERE id=?", (job,)).fetchone()
                if row is None:
                    raise ValueError("Analysis job does not exist")
                counts = {r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM audio_queue WHERE job=? GROUP BY state", (job,))}
                return {**dict(row), "counts": counts, "stale": row["state"] in ("running", "queued", "cancelling")
                        and time.time() - row["updated"] > 300}
            return {"analyzed_paths": db.execute("SELECT count(*) FROM audio_paths").fetchone()[0],
                    "jobs": [dict(r) for r in db.execute("SELECT * FROM audio_jobs ORDER BY created DESC LIMIT 10")]}

    def semantic_search(self, *, description: str | None = None, similar_to: str | None = None,
                        limit: int = 20) -> dict:
        from .runtime import model_directory, python_executable

        model_directory()
        if bool(description) == bool(similar_to) or not 1 <= limit <= 200 or len(description or "") > 500:
            raise ValueError("Choose one description/reference path and limit 1–200")
        environment = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1",
                           OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", PYTHONDONTWRITEBYTECODE="1")
        environment.pop("TRANSFORMERS_CACHE", None)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + environment.get("PYTHONPATH", "")
        request = {"description": description, "similar_to": similar_to, "limit": limit}
        if os.environ.get("AGENT_BRIDGE_CLAP_WARM", "1") != "0":
            from .clap_service import service_for
            try:
                document = service_for(self.path, python_executable(), environment).search(request)
            except (OSError, TimeoutError, ValueError) as error:
                document = {"error": str(error)}
            if "error" not in document:
                return document
            if not str(document["error"]).startswith(("Local CLAP worker", "Timeout")):
                raise ValueError(document["error"])  # a real request error, not a worker failure
        args = [python_executable(), "-m", "live_agent_bridge.audio.search_worker", "--db", self.path]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            response = subprocess.run(args, input=json.dumps({"description": description, "similar_to": similar_to,
                                     "limit": limit}), capture_output=True, text=True, env=environment,
                                      timeout=120, check=False, **options)
        except subprocess.TimeoutExpired:
            raise ValueError("Local semantic search exceeded 120 seconds; no data was modified") from None
        try:
            document = json.loads(response.stdout)
        except ValueError:
            raise ValueError("Local semantic model worker failed; check the configured runtime/model") from None
        if response.returncode or "error" in document:
            raise ValueError(document.get("error", "Local semantic model worker failed"))
        return document

    def _embedding_matrix(self, model_id: str):
        """All current embeddings as a row-normalised matrix, cached until the table changes."""
        with self.connection() as db:
            signature = (model_id, VERSION,
                         tuple(db.execute("SELECT count(*), coalesce(max(rowid),0) FROM audio_embeddings "
                                          "WHERE model=?", (model_id,)).fetchone()),
                         tuple(db.execute("SELECT count(*), coalesce(max(rowid),0) FROM audio_paths "
                                          "WHERE version=?", (VERSION,)).fetchone()))
            cached = getattr(self, "_matrix_cache", None)
            if cached and cached[0] == signature:
                return cached[1], cached[2]
            rows = db.execute("SELECT p.path,e.vector,p.hash FROM audio_paths p JOIN audio_embeddings e "
                              "ON e.hash=p.hash WHERE e.model=? AND p.version=? ORDER BY p.path",
                              (model_id, VERSION)).fetchall()
        # One row per audio content: identical copies in several folders share a vector.
        groups: dict[str, list] = {}
        for path, vector, digest in rows:
            if digest in groups:
                groups[digest][0].append(path)
            else:
                groups[digest] = [[path], vector]
        paths = [g[0] for g in groups.values()]
        vectors = [json.loads(g[1]) for g in groups.values()]
        try:
            import numpy as np

            matrix = np.asarray(vectors, dtype=np.float32).reshape(len(vectors), -1)
            if len(vectors):
                matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
        except ImportError:
            matrix = vectors
        self._matrix_cache = (signature, paths, matrix)
        return paths, matrix

    def _semantic_search_local(self, *, description: str | None = None, similar_to: str | None = None,
                               limit: int = 20, model=None) -> dict:
        from .semantic import LocalCLAP

        if bool(description) == bool(similar_to) or not 1 <= limit <= 200:
            raise ValueError("Choose one of description or similar_to, and a limit from 1 to 200")
        model = model or LocalCLAP()
        reference = model.text(description) if description else model.audio(str(self.validate_path(similar_to)))

        paths, matrix = self._embedding_matrix(model.model_id)
        if not paths:
            return {"total": 0, "results": [], "note": "No analyzed embeddings yet; run sample_analyze semantic=true"}
        try:
            import numpy as np

            query = np.asarray(reference, dtype=np.float32)
            query /= max(float(np.linalg.norm(query)), 1e-12)
            sims = matrix @ query
            order = np.argsort(-sims)
        except ImportError:  # pure-Python fallback
            norms = [math.sqrt(sum(v * v for v in row)) or 1.0 for row in matrix]
            qn = math.sqrt(sum(v * v for v in reference)) or 1.0
            sims = [sum(a * b for a, b in zip(row, reference, strict=True)) / (n * qn)
                    for row, n in zip(matrix, norms, strict=True)]
            order = sorted(range(len(sims)), key=lambda i: -sims[i])
        hits = []
        for i in order:  # validate only the best candidates (files may have changed or moved)
            group = paths[int(i)]
            path = next((p for p in group if self.result(p) is not None), None)
            if path is None:
                continue
            hit = {"path": path, "similarity": round(float(sims[int(i)]), 5),
                   "source": "local_clap", "model": model.model_id}
            others = [p for p in group if p != path]
            if others:
                hit["same_content_paths"] = others
            hits.append(hit)
            if len(hits) >= limit:
                break
        return {"total": len(paths), "results": hits,
                "note": "Cosine similarity is a ranking score, not a calibrated confidence probability."}
