"""CLI for compiling and querying owner-supplied ChatGPT Project snapshots."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from spst_runtime.project_context import (
    compile_snapshot,
    dump_json,
    load_json,
    query_corpus,
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
            output = query_corpus(
                load_json(args.corpus),
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


if __name__ == "__main__":
    raise SystemExit(main())
