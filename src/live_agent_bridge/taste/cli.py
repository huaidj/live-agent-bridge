"""Local human-facing commands, separate from the model-facing MCP surface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .store import SOURCES, TasteStore, json_text


def add_commands(sub) -> None:
    taste = sub.add_parser("taste", help="portable local preferences and evidence")
    commands = taste.add_subparsers(dest="action", required=True)
    show = commands.add_parser("show")
    show.add_argument("--domain")
    remember = commands.add_parser("remember")
    remember.add_argument("statement")
    remember.add_argument("--domain", default="general")
    remember.add_argument("--inferred", action="store_true")
    remember.add_argument("--writer", default="local-cli/user")
    recall = commands.add_parser("recall")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=10)
    export = commands.add_parser("export")
    export.add_argument("output", help="new JSON file; also writes a readable Markdown companion")
    load = commands.add_parser("import")
    load.add_argument("input")
    forget = commands.add_parser("forget")
    group = forget.add_mutually_exclusive_group(required=True)
    group.add_argument("--preference-id")
    group.add_argument("--source", choices=sorted(SOURCES))
    group.add_argument("--all", action="store_true")
    forget.add_argument("--yes", action="store_true", help="confirm the selected deletion scope")
    rb = commands.add_parser("import-rekordbox")
    rb.add_argument("input", help="rekordbox XML export or clean database snapshot")
    model = commands.add_parser("model-config", help="select an existing verified local audio model and Python runtime")
    model.add_argument("--model", required=True)
    model.add_argument("--python")

    reference = sub.add_parser("reference", help="analyze a favorite local song with an audio model")
    commands = reference.add_subparsers(dest="action", required=True)
    start = commands.add_parser("analyze")
    start.add_argument("path", help="explicit local audio file; original stays unchanged")
    start.add_argument("--label", default="")
    start.add_argument("--note", default="", help="what you like about it, in your own words")
    start.add_argument("--test-only", action="store_true", help="do not add the test audio to your memory")
    for name in ("status", "result", "cancel"):
        item = commands.add_parser(name)
        item.add_argument("job_id")
    commands.add_parser("list")

    independent = sub.add_parser("taste-serve", help="run just the memory MCP, without connecting to Live")
    independent.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    independent.add_argument("--http-port", type=int, default=8766)

    samples = sub.add_parser("samples", help="analyze sample audio in an isolated worker")
    commands = samples.add_subparsers(dest="action", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("--path")
    analyze.add_argument("--folder", help="explicit additional sample root")
    analyze.add_argument("--limit", type=int, default=100)
    analyze.add_argument("--semantic", action="store_true", help="use the existing local CLAP model")
    for name in ("status", "cancel", "resume"):
        item = commands.add_parser(name)
        item.add_argument("job_id", nargs="?" if name == "status" else None)
    result = commands.add_parser("result")
    result.add_argument("path")


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def export_memory(store: TasteStore, destination: str) -> dict:
    path = Path(destination).resolve()
    companion = path.with_suffix(".md")
    if path == companion or path.exists() or companion.exists():
        raise ValueError("Choose a new JSON path and a free Markdown companion path")
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = store.profile()
    with path.open("x", encoding="utf-8") as out:
        out.write(json_text(store.export()) + "\n")
    lines = ["# Local music preferences", "", profile["summary"], "", "## Confirmed preferences", ""]
    for pref in profile["preferences"]:
        lines.append(f"- {pref['statement']} (domain: {pref['domain']}; evidence: {', '.join(pref['evidence'])})")
    lines += ["", "## Reference songs — model observations, not confirmed feature preferences", ""]
    for reference in profile["references"]:
        lines.append(f"- {reference['label']} (evidence: {', '.join(reference['evidence'])})")
        if reference["user_note"]:
            lines.append("  User note: " + reference["user_note"])
        for group, candidates in reference["observations"].items():
            lines.append("  " + group + ": " + ", ".join(f"{c['label']} ({c['cosine_similarity']:.3f})" for c in candidates))
    lines += ["", "## Library evidence", "", json.dumps(profile["library_distribution"], ensure_ascii=False, indent=2),
              "", "Credentials and audio files are excluded. Model observations remain separate from confirmed preferences.", ""]
    with companion.open("x", encoding="utf-8") as out:
        out.write("\n".join(lines))
    return {"json": str(path), "markdown": str(companion), "sha256": store.digest()}


def run(args: argparse.Namespace) -> int:
    if args.cmd == "taste-serve":
        from .tools import standalone
        mcp = standalone()
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = args.http_port
        mcp.run(transport=args.transport)
        return 0
    if args.cmd == "reference":
        from ..audio.references import ReferenceLibrary
        library = ReferenceLibrary()
        if args.action == "analyze":
            result = library.start(args.path, label=args.label, user_note=args.note, remember=not args.test_only)
        elif args.action == "list":
            result = library.list()
        elif args.action == "cancel":
            result = library.cancel(args.job_id)
        else:
            result = library.status(args.job_id, include_result=args.action == "result")
        _print(result)
        return 0
    if args.cmd == "taste":
        if args.action == "model-config":
            from ..audio.runtime import configure
            _print(configure(args.model, args.python))
            return 0
        store = TasteStore()
        if args.action == "show":
            result = store.profile(args.domain)
        elif args.action == "remember":
            origin = "inference" if args.inferred else "user"
            event = store.event("preference", args.statement, source=origin, data={"domain": args.domain}, writer=args.writer)
            result = store.remember(args.statement, [event], domain=args.domain,
                                    status="pending" if args.inferred else "confirmed", writer=args.writer)
        elif args.action == "recall":
            result = store.recall(args.query, args.limit)
        elif args.action == "export":
            result = export_memory(store, args.output)
        elif args.action == "import":
            source = Path(args.input)
            if source.stat().st_size > 100_000_000:
                raise ValueError("Memory import exceeds the 100 MB limit")
            result = store.import_data(json.loads(source.read_text(encoding="utf-8-sig")))
        elif args.action == "forget":
            if not args.yes:
                raise ValueError("Add --yes to confirm deletion of exactly the selected scope")
            result = store.forget(preference_id=args.preference_id, source=args.source, all_data=args.all)
        else:
            from .rekordbox import import_rekordbox
            result = import_rekordbox(args.input, store)
        _print(result)
        return 0
    if args.cmd == "samples":
        from ..audio.library import AudioLibrary
        from ..samples import SampleIndex
        index = SampleIndex()
        library = AudioLibrary(index.db_path)
        if args.action == "analyze":
            if args.folder:
                index.add_root(args.folder)
                if not args.path:
                    index.scan()
            result = library.start(args.path, args.limit, args.semantic)
        elif args.action == "result":
            library.validate_path(args.path)
            result = library.result(args.path) or {"state": "not_analyzed"}
        elif args.action == "status":
            result = library.status(args.job_id)
        elif args.action == "cancel":
            result = library.cancel(args.job_id)
        else:
            result = library.resume(args.job_id)
        _print(result)
        return 0
    print("Unsupported command", file=sys.stderr)
    return 2
