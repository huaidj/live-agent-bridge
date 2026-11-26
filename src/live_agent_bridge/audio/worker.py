"""One isolated, resumable analysis job. No Live API calls."""

from __future__ import annotations

import argparse
import time

from ..taste.locking import file_lock
from .library import AudioLibrary


def run(library: AudioLibrary, job: str, semantic: bool = False) -> None:
    from pathlib import Path

    try:
        with file_lock(Path(library.path + ".audio-worker.lock"), timeout=0.1):
            with library.connection() as db:
                state = db.execute("SELECT state FROM audio_jobs WHERE id=?", (job,)).fetchone()
                if state is None or state[0] == "cancelling":
                    db.execute("UPDATE audio_jobs SET state='cancelled' WHERE id=?", (job,))
                    return
                db.execute("UPDATE audio_jobs SET state='running',updated=? WHERE id=?", (time.time(), job))
            model = None
            if semantic:
                from .semantic import LocalCLAP
                model = LocalCLAP()
            while True:
                with library.connection() as db:
                    state = db.execute("SELECT state FROM audio_jobs WHERE id=?", (job,)).fetchone()[0]
                    if state == "cancelling":
                        db.execute("UPDATE audio_jobs SET state='cancelled',updated=? WHERE id=?", (time.time(), job))
                        return
                    row = db.execute("SELECT path FROM audio_queue WHERE job=? AND state='queued' ORDER BY path LIMIT 1",
                                     (job,)).fetchone()
                    if row is None:
                        failed = db.execute("SELECT count(*) FROM audio_queue WHERE job=? AND state='error'", (job,)).fetchone()[0]
                        db.execute("UPDATE audio_jobs SET state=?,updated=? WHERE id=?",
                                   ("partial" if failed else "complete", time.time(), job))
                        return
                    path = row[0]
                    db.execute("UPDATE audio_queue SET state='running' WHERE job=? AND path=?", (job, path))
                    db.execute("UPDATE audio_jobs SET updated=? WHERE id=?", (time.time(), job))
                try:
                    library.analyze(path, model)
                    state, error = "done", None
                except Exception as exc:  # noqa: BLE001 -- one corrupt file must not abort the batch
                    # Paths and file contents never leave this local DB; no traceback on protocol stdout.
                    state, error = "error", f"{type(exc).__name__}: {str(exc)[:400]}"
                with library.connection() as db:
                    db.execute("UPDATE audio_queue SET state=?,error=? WHERE job=? AND path=?", (state, error, job, path))
                    db.execute("UPDATE audio_jobs SET updated=? WHERE id=?", (time.time(), job))
    except Exception as exc:  # noqa: BLE001 -- persist worker failure for the status API
        with library.connection() as db:
            db.execute("UPDATE audio_jobs SET state='error',error=?,updated=? WHERE id=?",
                       (f"{type(exc).__name__}: {str(exc)[:400]}", time.time(), job))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--semantic", action="store_true")
    args = parser.parse_args()
    run(AudioLibrary(args.db), args.job, args.semantic)


if __name__ == "__main__":
    main()
