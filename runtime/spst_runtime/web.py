import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from spst_runtime.chat_bridge import run_chat_turn
from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.runtime.runtime_loop import RuntimeLoop


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SPST-UEA Runtime</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; background: radial-gradient(circle at top, #1d3347, #101418 45%); color: #eef3f8; }
    main { max-width: 900px; margin: auto; }
    button { padding: .7rem 1rem; border: 0; border-radius: .5rem; background: #76d0ff; cursor: pointer; }
    input { padding: .65rem; border-radius: .5rem; border: 1px solid #52616f; background: #17212b; color: #eef3f8; }
    pre { padding: 1rem; border-radius: .75rem; background: #17212b; overflow: auto; }
    .card { border: 1px solid #2a3744; border-radius: 1rem; padding: 1rem; margin-top: 1rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: .75rem; }
    .metric { background: #17212b; border-radius: .75rem; padding: .8rem; }
    .metric strong { display: block; font-size: 1.4rem; }
    .trace { display: flex; flex-wrap: wrap; gap: .4rem; }
    .trace span { background: #26394a; border: 1px solid #3b5164; border-radius: 999px; padding: .35rem .6rem; }
    textarea { width: 100%; min-height: 5rem; padding: .65rem; border-radius: .5rem; border: 1px solid #52616f; background: #17212b; color: #eef3f8; }
  </style>
</head>
<body>
  <main>
    <h1>SPST-UEA Runtime</h1>
    <p>Local sovereign cockpit for the SPST-UEA autonomous runtime.</p>
    <div class="card">
      <h2>Live Cockpit</h2>
      <div class="grid">
        <div class="metric">Running <strong id="cockpit-running">-</strong></div>
        <div class="metric">Tick <strong id="cockpit-tick">-</strong></div>
        <div class="metric">ESI <strong id="cockpit-esi">-</strong></div>
        <div class="metric">Memory <strong id="cockpit-memory">-</strong></div>
      </div>
      <h3>Pipeline Trace</h3>
      <div id="cockpit-trace" class="trace"></div>
      <h3>Goals</h3>
      <pre id="cockpit-goals">Loading...</pre>
      <h3>Human Approval Queue</h3>
      <div id="cockpit-approvals">No pending approvals.</div>
    </div>
    <div class="card">
      <label>Steps <input id="steps" type="number" value="3" min="0"></label>
      <label>Event <input id="event" value="browser_run"></label>
      <label>Prompt <input id="prompt" value="Say hello from SPST-UEA in no-key mode."></label>
      <p>No-key mode: prompt processing stays inside the local/Codex-mediated model boundary.</p>
      <button id="run">Run Runtime</button>
    </div>
    <div class="card">
      <h2>Dispatch To Autonomous Runtime</h2>
      <textarea id="dispatch-prompt">Implement cockpit observability update.</textarea>
      <button id="dispatch">Dispatch</button>
    </div>
    <pre id="output">Ready.</pre>
    <div class="card">
      <h2>Always-On Session</h2>
      <div class="grid">
        <div class="metric">Turns <strong id="turns">-</strong></div>
        <div class="metric">ESI <strong id="esi">-</strong></div>
        <div class="metric">Governance <strong id="authorized">-</strong></div>
        <div class="metric">Amplification <strong id="amp">-</strong></div>
        <div class="metric">Memories <strong id="memory">-</strong></div>
      </div>
    </div>
  </main>
  <script>
    function updateMetrics(data) {
      const session = data.session || {};
      document.getElementById("turns").textContent = session.turn_count ?? "-";
      document.getElementById("esi").textContent = session.evaluation?.esi ?? "-";
      document.getElementById("authorized").textContent = session.latest_audit?.authorized ?? "-";
      document.getElementById("amp").textContent = session.amplification?.amplification_score ?? "-";
      document.getElementById("memory").textContent = session.memory?.stats?.total_records ?? "-";
    }
    async function run() {
      const steps = document.getElementById("steps").value;
      const event = encodeURIComponent(document.getElementById("event").value);
      const prompt = encodeURIComponent(document.getElementById("prompt").value);
      const response = await fetch(`/api/run?steps=${steps}&event=${event}&prompt=${prompt}`);
      const data = await response.json();
      updateMetrics(data);
      document.getElementById("output").textContent = JSON.stringify(data, null, 2);
    }
    async function refreshCockpit() {
      const status = await (await fetch("/api/status")).json();
      document.getElementById("cockpit-running").textContent = status.running;
      document.getElementById("cockpit-tick").textContent = status.tick;
      document.getElementById("cockpit-esi").textContent = status.esi;
      document.getElementById("cockpit-memory").textContent = status.memory_stats?.total_records ?? "-";
      document.getElementById("cockpit-trace").innerHTML = (status.last_trace || []).map((step) => `<span>${step}</span>`).join("");
      const goals = await (await fetch("/api/goals")).json();
      document.getElementById("cockpit-goals").textContent = JSON.stringify(goals.goals, null, 2);
      renderApprovals(status.pending_approvals || []);
    }
    function renderApprovals(approvals) {
      const container = document.getElementById("cockpit-approvals");
      container.replaceChildren();
      if (!approvals.length) {
        container.textContent = "No pending approvals.";
        return;
      }
      approvals.forEach((approval) => {
        const card = document.createElement("div");
        card.className = "card";
        const details = document.createElement("pre");
        details.textContent = JSON.stringify(approval, null, 2);
        const approve = document.createElement("button");
        approve.textContent = "Approve";
        approve.addEventListener("click", () => resolveApproval(approval.approval_id, true));
        const reject = document.createElement("button");
        reject.textContent = "Reject";
        reject.addEventListener("click", () => resolveApproval(approval.approval_id, false));
        card.append(details, approve, reject);
        container.append(card);
      });
    }
    async function resolveApproval(approvalId, approved) {
      const response = await fetch("/api/approval", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({approval_id: approvalId, approved})
      });
      const data = await response.json();
      document.getElementById("output").textContent = JSON.stringify(data, null, 2);
      await refreshCockpit();
    }
    async function dispatchPrompt() {
      const prompt = document.getElementById("dispatch-prompt").value;
      const response = await fetch("/api/dispatch", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({prompt})
      });
      const data = await response.json();
      document.getElementById("output").textContent = JSON.stringify(data, null, 2);
      await refreshCockpit();
    }
    document.getElementById("run").addEventListener("click", run);
    document.getElementById("dispatch").addEventListener("click", dispatchPrompt);
    refreshCockpit();
  </script>
</body>
</html>
"""


class CockpitRuntime:
    """Live local runtime backing the sovereign web cockpit."""

    def __init__(self, db_path: str = "spst_cockpit.db"):
        self.state = SubjectState(metadata={"version": 0, "goals": []})
        self.orchestrator = RuntimeOrchestrator(db_path=db_path)
        self.loop = RuntimeLoop(orchestrator=self.orchestrator, state=self.state)
        self.last_state = self.state
        self.last_swarm_result: dict[str, dict[str, Any]] = {}

    def status(self) -> dict:
        metadata = self.last_state.metadata
        return {
            "running": self.loop.ctx.running,
            "tick": self.loop.ctx.tick,
            "esi": metadata.get("phase2_reflection", {}).get("esi", 1.0),
            "last_trace": metadata.get("last_trace", []),
            "requires_api_key": self.orchestrator.model_provider.health().get("requires_api_key", False),
            "memory_stats": self.orchestrator.memory_provider.long_term_memory.stats(),
            "subjects": self.subjects()["subjects"],
            "pending_approvals": self.orchestrator.pending_approvals(),
        }

    def goals(self) -> dict:
        return {"goals": self.last_state.metadata.get("goals", [])}

    def subjects(self) -> dict:
        subjects = []
        for subject in self.orchestrator.subject_repository.list_subjects():
            metadata = subject.metadata
            if metadata.get("subject_id"):
                subjects.append(
                    {
                        "id": metadata.get("subject_id"),
                        "role": metadata.get("role"),
                        "goals": metadata.get("goals", []),
                        "swarm": metadata.get("swarm", {}),
                    }
                )
        return {"subjects": subjects}

    def dispatch(self, payload: dict) -> dict:
        if payload.get("swarm"):
            return self._dispatch_swarm(payload)

        goal = payload.get("goal")
        if goal:
            goals = self.last_state.metadata.setdefault("goals", [])
            goals.append({**goal, "status": goal.get("status", "pending")})

        event = Event(type=payload.get("event", "user_action"), payload=dict(payload))
        if not self.loop.ctx.running:
            self.loop.start()
        self.last_state = self.loop.step(self.last_state, event) or self.last_state
        return {
            "version": self.last_state.metadata.get("version"),
            "trace": self.last_state.metadata.get("last_trace", []),
            "transition_log": self.last_state.metadata.get("transition_log", []),
            "governance": self.last_state.metadata.get("governance", {}),
            "goal": (self.last_state.metadata.get("goals") or [{}])[0],
            "amplification": self.last_state.metadata.get("intelligence_amplification", {}),
        }

    def approve(self, approval_id: str, *, approved: bool) -> dict:
        self.last_state = self.orchestrator.resolve_approval(approval_id, approved=approved)
        return {
            "version": self.last_state.metadata.get("version"),
            "trace": self.last_state.metadata.get("last_trace", []),
            "governance": self.last_state.metadata.get("governance", {}),
            "pending_approvals": self.orchestrator.pending_approvals(),
        }

    def _dispatch_swarm(self, payload: dict) -> dict:
        mission = payload.get("prompt", "")
        goal = {
            "id": payload.get("goal_id", "genesis_mission"),
            "description": mission,
            "status": "pending",
            "priority": 1.0,
        }
        self.orchestrator.create_subject("planner", role="PlannerSubject", goals=[goal])
        self.orchestrator.create_subject("executor", role="ExecutorSubject", goals=[{**goal, "priority": 0.9}])
        planner_state = self.orchestrator.dispatch_subject(
            "planner",
            Event(
                type="inter_subject_message",
                payload={
                    "to": "executor",
                    "task": mission,
                    "prompt": f"Plan Genesis Mission: {mission}",
                },
            ),
        )
        executor_state = self.orchestrator.dispatch_subject(
            "executor",
            Event(
                type="user_action",
                payload={
                    "prompt": mission,
                    "simulate_tool_error": payload.get("self_repair", True),
                },
            ),
        )
        self.last_state = executor_state
        self.last_swarm_result = {
            "planner": planner_state.metadata,
            "executor": executor_state.metadata,
        }
        return {
            "trace": executor_state.metadata.get("last_trace", []),
            "governance": executor_state.metadata.get("governance", {}),
            "subjects": self.subjects()["subjects"],
            "next_actions": executor_state.metadata.get("next_actions", []),
            "self_repair": executor_state.metadata.get("self_repair", {}),
            "amplification": executor_state.metadata.get("intelligence_amplification", {}),
        }

    def memory_search(self, query: str, top_k: int = 5) -> dict:
        return {
            "query": query,
            "results": self.orchestrator.memory_provider.search(query, top_k=top_k),
        }


DEFAULT_COCKPIT = CockpitRuntime()


def handle_cockpit_request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    runtime: CockpitRuntime | None = None,
) -> tuple[HTTPStatus, dict[str, Any]]:
    runtime = runtime or DEFAULT_COCKPIT
    parsed = urlparse(path)

    if method == "GET" and parsed.path == "/api/status":
        return HTTPStatus.OK, runtime.status()

    if method == "GET" and parsed.path == "/api/goals":
        return HTTPStatus.OK, runtime.goals()

    if method == "GET" and parsed.path == "/api/subjects":
        return HTTPStatus.OK, runtime.subjects()

    if method == "GET" and parsed.path == "/api/memory/search":
        query = parse_qs(parsed.query).get("q", [""])[0]
        return HTTPStatus.OK, runtime.memory_search(query)

    if method == "POST" and parsed.path == "/api/dispatch":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.dispatch(payload)

    if method == "POST" and parsed.path == "/api/approval":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.approve(
            str(payload["approval_id"]),
            approved=bool(payload.get("approved")),
        )

    return HTTPStatus.NOT_FOUND, {"error": "not_found"}


class RuntimeWebHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", HTML)
            return

        if parsed.path == "/api/run":
            query = parse_qs(parsed.query)
            steps = int(query.get("steps", ["3"])[0])
            event = query.get("event", ["browser_run"])[0]
            prompt = query.get("prompt", [None])[0]
            payload = run_chat_turn(prompt or "", steps=steps, event=event)
            self._send_json(HTTPStatus.OK, payload)
            return

        if parsed.path in {"/api/status", "/api/goals", "/api/subjects", "/api/memory/search"}:
            status, payload = handle_cockpit_request("GET", self.path)
            self._send_json(status, payload)
            return

        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        status, payload = handle_cockpit_request("POST", self.path, body=body)
        self._send_json(status, payload)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        self._send(status, "application/json", json.dumps(payload, sort_keys=True).encode())

    def _send(self, status: HTTPStatus, content_type: str, body: str | bytes) -> None:
        encoded = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), RuntimeWebHandler)
    print(f"SPST-UEA Runtime Web running at http://{host}:{port}/", flush=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the SPST-UEA runtime browser UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
