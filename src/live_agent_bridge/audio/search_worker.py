"""Isolated, local-only model inference for sample retrieval.

One-shot mode reads one JSON request from stdin. ``--serve`` keeps the CLAP model loaded and
answers newline-delimited JSON requests ({"id", "description"|"similar_to", "limit"}) until stdin
closes, so repeated searches skip the multi-second model load.
"""

from __future__ import annotations

import argparse
import json
import sys

from .library import AudioLibrary


def _answer(library: AudioLibrary, request: dict, model=None) -> dict:
    try:
        payload = {k: request.get(k) for k in ("description", "similar_to", "limit") if request.get(k) is not None}
        return library._semantic_search_local(model=model, **payload)
    except Exception as error:  # noqa: BLE001 -- return a bounded protocol error from the child process
        return {"error": f"{type(error).__name__}: {str(error)[:400]}"}


def serve(library: AudioLibrary) -> int:
    from .semantic import LocalCLAP

    try:
        model = LocalCLAP()
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"{type(error).__name__}: {str(error)[:400]}"}), flush=True)
        return 1
    print(json.dumps({"ready": True, "model": model.model_id}), flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            print(json.dumps({"error": "bad request"}), flush=True)
            continue
        answer = _answer(library, request, model)
        answer["id"] = request.get("id")
        print(json.dumps(answer), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    library = AudioLibrary(args.db)
    if args.serve:
        return serve(library)
    try:
        request = json.loads(sys.stdin.read(10_000))
    except ValueError:
        print(json.dumps({"error": "bad request"}))
        return 1
    result = _answer(library, request)
    print(json.dumps(result))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
