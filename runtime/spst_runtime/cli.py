import argparse
import json

from spst_runtime.core.runtime_engine import RuntimeEngine
from spst_runtime.events.event import Event
from spst_runtime.pipeline.state_transition import StateTransitionPipeline
from spst_runtime.providers.local_codex_adapter import LocalCodexAdapter
from spst_runtime.providers.openai_model_adapter import OpenAIModelAdapter
from spst_runtime.runtime.runtime_loop import RuntimeLoop


def run_loop(steps: int) -> dict:
    runtime_loop = RuntimeLoop()
    runtime_loop.start()
    for _ in range(steps):
        runtime_loop.step()
    runtime_loop.stop()
    return {
        "running": runtime_loop.ctx.running,
        "tick": runtime_loop.ctx.tick,
        "history": runtime_loop.ctx.history,
    }


def run_dispatch(event_type: str, prompt: str | None = None, use_gpt: bool = False) -> dict:
    adapter = OpenAIModelAdapter() if use_gpt else LocalCodexAdapter() if prompt else None
    engine = RuntimeEngine(pipeline=StateTransitionPipeline(model_adapter=adapter))
    payload = {"prompt": prompt} if prompt else {}
    state = engine.dispatch(Event(type=event_type, payload=payload))
    return state.metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SPST-UEA reference runtime.")
    parser.add_argument("--steps", type=int, default=3, help="Number of runtime loop steps to execute.")
    parser.add_argument("--event", default="codex_run", help="Event type to dispatch through the pipeline.")
    parser.add_argument("--prompt", default=None, help="Prompt to send through the model adapter.")
    parser.add_argument(
        "--use-gpt",
        action="store_true",
        help="Use OpenAI GPT via OPENAI_API_KEY. Omit this for API-key-free local/Codex-mediated mode.",
    )
    args = parser.parse_args(argv)

    result = {
        "loop": run_loop(args.steps),
        "pipeline": run_dispatch(args.event, args.prompt, args.use_gpt),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
