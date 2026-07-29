"""CLI for compiling and querying owner-supplied ChatGPT Project snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from spst_runtime.project_context import (
    compile_snapshot,
    dump_json,
    load_json,
    load_verified_corpus,
    query_verified_corpus,
    verify_corpus,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("--snapshot", required=True)
    compile_parser.add_argument("--corpus", required=True)
    compile_parser.add_argument("--max-age-days", type=int, default=90)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--corpus", required=True)

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--corpus", required=True)
    query_parser.add_argument("--query", required=True)
    query_parser.add_argument("--max-items", type=int, default=6)
    query_parser.add_argument("--max-chars", type=int, default=8_000)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--corpus", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args.corpus)
        if args.command == "compile":
            result = compile_snapshot(
                load_json(args.snapshot),
                max_age_days=args.max_age_days,
            )
            dump_json(args.corpus, result)
            output = verify_corpus(result)
        elif args.command == "status":
            output = verify_corpus(load_json(args.corpus))
        else:
            output = query_verified_corpus(
                load_verified_corpus(args.corpus),
                args.query,
                max_items=args.max_items,
                max_chars=args.max_chars,
            )
    except (OSError, TypeError, ValueError) as error:
        output = {
            "schema": "spst-chatgpt-project-context-cli-result-v1",
            "status": "blocked",
            "reason": str(error) or error.__class__.__name__,
        }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if output["status"] in {"ready", "empty"} else 2


def _serve(corpus_path: str) -> int:
    """Serve independent JSONL queries while retaining only verified search indexes."""

    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict) or not isinstance(request.get("query"), str):
                raise ValueError("query_request_invalid")
            output = query_verified_corpus(
                load_verified_corpus(corpus_path),
                request["query"],
                max_items=request.get("max_items", 6),
                max_chars=request.get("max_chars", 8_000),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            output = {
                "schema": "spst-chatgpt-project-context-cli-result-v1",
                "status": "blocked",
                "reason": str(error) or error.__class__.__name__,
            }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
