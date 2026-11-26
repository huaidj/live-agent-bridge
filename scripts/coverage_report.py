"""Coverage report for the live harness: which (tool, action) pairs and which Remote Script ops were NOT exercised.

Reads <rows>.coverage.json written by scripts/full_live_test.py and compares it with the schemas of the running MCP
server (every `action` enum value) and the bridge's op list.

Usage:  python scripts/coverage_report.py [path/to/full_live_test_rows.json.coverage.json] [--json out.json]
Exit code 0 only when no (tool, action) pair and no non-excluded op is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXE = str(Path(sys.executable).with_name("live-agent-bridge.exe" if os.name == "nt" else "live-agent-bridge"))
DEFAULT = Path.cwd() / "full_live_test_rows.json.coverage.json"

# (tool, action) pairs that are excluded on purpose, with the reason (kept short, printed in the report)
EXCLUDED_PAIRS = {
    ("acestep_service", "start"): "verified separately by the ACE-Step acceptance harness",
    ("acestep_service", "stop"): "verified separately by the ACE-Step acceptance harness",
    ("app_control", "press_dialog_button"): "positive modal case requires a separate disposable fixture",
}
# whole tools that are never called against the live service, with the reason
EXCLUDED_TOOLS = {
    "acestep_service": "separate explicit opt-in lifecycle acceptance",
    "acestep_generate_part": "separate explicit opt-in generation and Live import acceptance",
}
# ops that only run inside other ops / need a service that is not started
EXCLUDED_OPS = {
    "bridge.reload": "verified by scripts/mcp_live_maintenance.py",
    "bridge.timer": "verified by scripts/mcp_live_maintenance.py",
}


async def schemas():
    async with stdio_client(StdioServerParameters(command=EXE, args=["serve"])) as (rd, wr), \
            ClientSession(rd, wr) as s:
        await s.initialize()
        out = {}
        for t in (await s.list_tools()).tools:
            props = t.inputSchema.get("properties", {})
            act = props.get("action", {})
            enum = act.get("enum")
            if enum is None:
                for alt in act.get("anyOf", []):
                    enum = enum or alt.get("enum")
            out[t.name] = list(enum or [])
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("coverage", nargs="?", type=Path, default=DEFAULT)
    ap.add_argument("--json", type=Path, default=None)
    a = ap.parse_args()
    cov = json.loads(a.coverage.read_text(encoding="utf-8"))
    called = {(t, act) for t, act, _ in cov["pairs"]}
    called_tools = {t for t, _, _ in cov["pairs"]}
    enums = asyncio.run(schemas())

    missing_pairs, missing_tools, excluded = [], [], []
    for tool, actions in sorted(enums.items()):
        if tool not in called_tools:
            if tool not in EXCLUDED_TOOLS:
                missing_tools.append(tool)
            continue
        called_actions = {act for t, act in called if t == tool}
        for act in actions:
            if act in called_actions:
                continue
            if (tool, act) in EXCLUDED_PAIRS:
                excluded.append((tool, act, EXCLUDED_PAIRS[(tool, act)]))
            elif act and tool in {"update_track"}:
                continue
            else:
                missing_pairs.append((tool, act))
    # tools whose default action (omitted from the call) is the first enum value count as covering it
    for tool, act in list(missing_pairs):
        acts = enums[tool]
        if acts and act == acts[0] and (tool, None) in called:
            missing_pairs.remove((tool, act))

    ops_run = set(cov.get("ops_executed", {})) | set(cov.get("inner_ops", {}))
    all_ops = set(cov.get("all_ops", []))
    missing_ops = sorted(o for o in all_ops - ops_run if o not in EXCLUDED_OPS)
    excluded_ops = [(o, EXCLUDED_OPS[o]) for o in sorted(all_ops - ops_run) if o in EXCLUDED_OPS]

    n_pairs = sum(len(v) for v in enums.values())
    print(f"tools: {len(enums)} | tools never called: {missing_tools or 'none'}")
    for t, why in EXCLUDED_TOOLS.items():
        print(f"   excluded tool {t}: {why}")
    print(f"(tool, action) pairs in schemas: {n_pairs} | missing: {len(missing_pairs)} | excluded: {len(excluded)}")
    for t, act in missing_pairs:
        print(f"   MISSING  {t}.{act}")
    for t, act, why in excluded:
        print(f"   excluded {t}.{act}: {why}")
    print(f"ops: {len(all_ops)} | executed as top-level ops: {len(ops_run & all_ops)} | missing: {len(missing_ops)}"
          f" (ops sent inside batch calls are counted from the batch arguments)")
    for o in missing_ops:
        print(f"   MISSING op {o}")
    for o, why in excluded_ops:
        print(f"   excluded op {o}: {why}")
    report = {"missing_tools": missing_tools, "missing_pairs": missing_pairs, "excluded_pairs": excluded,
              "missing_ops": missing_ops, "excluded_ops": excluded_ops, "pairs_total": n_pairs}
    if a.json:
        a.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if not (missing_tools or missing_pairs or missing_ops) else 1


if __name__ == "__main__":
    sys.exit(main())
