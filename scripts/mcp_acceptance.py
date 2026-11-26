"""One-command MCP acceptance: run the exhaustive real-Live suite and emit a machine-readable report.

Usage:
    python scripts/mcp_acceptance.py                 # run everything, write JSON next to the log
    python scripts/mcp_acceptance.py --json out.json # custom report path
    python scripts/mcp_acceptance.py --timeout 1500

Exit code: 0 when the run finished with 0 FAIL rows (EXPECTED/NOT_RUN are allowed), 1 otherwise.
The suite is run with ZZ_TEST_ALLOW_USER_SET=1 because it operates on the open set; it parks the playhead,
disables the loop for record tests, snapshots every user clip and diffs it afterwards.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "scripts" / "full_live_test.py"
PY = Path(sys.executable)
DEFAULT_JSON = Path.cwd() / "mcp-acceptance.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(DEFAULT_JSON))
    ap.add_argument("--fixture", type=Path, help="Exact stopped disposable .als currently open")
    ap.add_argument("--sample", type=Path, help="Optional WAV instead of the generated synthetic fixture")
    ap.add_argument("--timeout", type=float, default=1500.0)
    args = ap.parse_args()

    env = dict(os.environ)
    env["ZZ_TEST_ALLOW_USER_SET"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if args.fixture:
        env["ZZ_TEST_FIXTURE_FILE"] = str(args.fixture.resolve(strict=True))
    if args.sample:
        env["ZZ_TEST_FIXTURE_WAV"] = str(args.sample.resolve(strict=True))
    output_dir = Path(args.json).resolve().parent
    output_dir.mkdir(parents=True, exist_ok=True)
    if not env.get("AGENT_BRIDGE_DATA_DIR"):
        env["AGENT_BRIDGE_DATA_DIR"] = tempfile.mkdtemp(prefix="mcp-acceptance-state-", dir=output_dir)
    started = dt.datetime.now()
    try:
        rows_path = Path(args.json).with_name("full_live_test_rows.json")
        proc = subprocess.run([str(PY), "-u", str(HARNESS), str(rows_path)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=args.timeout)
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired as e:
        out, err, code = (e.stdout or ""), (e.stderr or ""), "timeout"
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
    finished = dt.datetime.now()

    counts = {}
    m = re.search(r"==== (\{[^}]*\})", out)
    if m:
        try:
            counts = json.loads(m.group(1).replace("'", '"'))
        except json.JSONDecodeError:
            counts = {}
    m2 = re.search(r"tools exercised (\d+)/(\d+)", out)
    exercised = int(m2.group(1)) if m2 else None
    total = int(m2.group(2)) if m2 else None
    m3 = re.search(r"not exercised: (\[.*?\])", out)
    missing = []
    if m3:
        try:
            missing = json.loads(m3.group(1).replace("'", '"'))
        except json.JSONDecodeError:
            missing = []
    fails = [ln.strip() for ln in out.splitlines() if ln.startswith("FAIL")]
    damage = [ln.strip() for ln in out.splitlines() if "USER-DAMAGE" in ln]
    user_ok = "user clips unchanged" in out and "added 0, removed 0" in out
    restored = "set restored" in out and "PASS  set restored" in out

    ok = (code == 0) and not fails and not damage and user_ok and restored and (exercised == total) and bool(total)
    report = {
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "duration_s": round((finished - started).total_seconds(), 1),
        "command": f"{PY} -u {HARNESS}",
        "env": {"ZZ_TEST_ALLOW_USER_SET": "1"},
        "exit_code": code,
        "counts": counts,
        "tools_exercised": exercised,
        "tools_total": total,
        "not_exercised": missing,
        "fail_rows": fails,
        "user_damage_rows": damage,
        "user_clips_unchanged": user_ok,
        "set_restored": restored,
        "ok": ok,
        "stdout_tail": "\n".join(out.splitlines()[-40:]),
        "stderr_tail": "\n".join(err.splitlines()[-20:]),
    }
    path = Path(args.json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"counts: {counts} | tools {exercised}/{total} | user clips unchanged: {user_ok} | "
          f"set restored: {restored} | FAIL rows: {len(fails)} | USER-DAMAGE: {len(damage)}")
    print(("ACCEPTANCE PASS" if ok else "ACCEPTANCE FAIL") + f"  ({report['duration_s']} s)  -> {path}")
    if not ok:
        for f in (fails + damage + ([f"not exercised: {missing}"] if missing else []))[:10]:
            print("  ", f)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

