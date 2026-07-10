import argparse
import json

from spst_runtime.cli import run_dispatch, run_loop
from spst_runtime.chat_session import ChatSessionStore, record_turn, summarize_session


def run_chat_turn(prompt: str, steps: int = 3, event: str = "chat_turn") -> dict:
    """Run one Codex-mediated chat turn through SPST-UEA without an API key."""

    loop = run_loop(steps)
    pipeline = run_dispatch(event, prompt=prompt)
    session = record_turn(prompt, event, pipeline)

    return {
        "mode": "codex-chat-mediated",
        "requires_api_key": False,
        "input": {
            "prompt": prompt,
            "event": event,
            "steps": steps,
        },
        "runtime": {
            "loop": loop,
            "pipeline": pipeline,
        },
        "session": {
            "turn_count": session["turn_count"],
            "goals": session["goals"],
            "evaluation": session["evaluation"],
            "summary": session["summary"],
            "amplification": session["amplification"],
            "memory": session["memory"],
            "latest_audit": session["audit"][-1],
        },
    }


def get_chat_status() -> dict:
    state = ChatSessionStore().load()
    return {
        "mode": state.get("mode"),
        "provider": state.get("provider"),
        "requires_api_key": False,
        "turn_count": state.get("turn_count", 0),
        "evaluation": state.get("evaluation", {}),
        "summary": state.get("summary", summarize_session(state)),
        "amplification": state.get("amplification", {}),
        "memory": state.get("memory", {}),
        "latest_audit": state.get("audit", [None])[-1],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a Codex chat prompt through the SPST-UEA runtime boundary."
    )
    parser.add_argument("prompt", nargs="?", help="Prompt received from the Codex chat.")
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--event", default="chat_turn")
    parser.add_argument("--status", action="store_true", help="Print persisted chat session status.")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(get_chat_status(), indent=2, sort_keys=True))
        return 0

    if not args.prompt:
        parser.error("prompt is required unless --status is used")

    print(json.dumps(run_chat_turn(args.prompt, args.steps, args.event), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
