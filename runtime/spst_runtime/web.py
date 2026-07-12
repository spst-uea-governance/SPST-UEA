import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

from spst_runtime.chat_bridge import run_chat_turn
from spst_runtime.events.event import Event
from spst_runtime.models.subject_state import SubjectState
from spst_runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from spst_runtime.runtime.runtime_loop import RuntimeLoop


DEFAULT_COCKPIT_DB_ENV = "SPST_COCKPIT_DB_PATH"
DEFAULT_COCKPIT_DB_PATH = "spst_cockpit.db"


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
        <div class="metric">Evidence <strong id="cockpit-evidence">-</strong></div>
        <div class="metric">Verification <strong id="cockpit-verification">-</strong></div>
        <div class="metric">Evaluation <strong id="cockpit-evaluation">-</strong></div>
        <div class="metric">Calibration <strong id="cockpit-calibration">-</strong></div>
        <div class="metric">Corpus <strong id="cockpit-corpus">-</strong></div>
        <div class="metric">Shadow <strong id="cockpit-shadow">-</strong></div>
        <div class="metric">Artifacts <strong id="cockpit-artifacts">-</strong></div>
        <div class="metric">Promotions <strong id="cockpit-promotions">-</strong></div>
      </div>
      <h3>Pipeline Trace</h3>
      <div id="cockpit-trace" class="trace"></div>
      <h3>Goals</h3>
      <pre id="cockpit-goals">Loading...</pre>
      <h3>Human Approval Queue</h3>
      <div id="cockpit-approvals">No pending approvals.</div>
      <h3>Decision Explanation</h3>
      <pre id="cockpit-decision">No decision recorded.</pre>
      <h3>Local Verification</h3>
      <pre id="cockpit-verification-detail">No verification run recorded.</pre>
      <h3>Capability Evaluation</h3>
      <pre id="cockpit-evaluation-detail">No capability evaluation recorded.</pre>
      <h3>Calibration Registry</h3>
      <pre id="cockpit-calibration-detail">No calibration history recorded.</pre>
      <h3>Operational Corpus</h3>
      <pre id="cockpit-corpus-detail">No consented tasks recorded.</pre>
      <h3>Operational Shadow Evaluation</h3>
      <pre id="cockpit-shadow-detail">No shadow evaluation recorded.</pre>
      <h3>Artifact Outcome Evidence</h3>
      <pre id="cockpit-artifacts-detail">No artifact outcome recorded.</pre>
      <h3>Longitudinal Promotions</h3>
      <pre id="cockpit-promotions-detail">No promotion proposal recorded.</pre>
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
      document.getElementById("cockpit-evidence").textContent = status.evidence?.status ?? "-";
      document.getElementById("cockpit-verification").textContent = status.verification?.status ?? "-";
      document.getElementById("cockpit-evaluation").textContent = status.evaluation?.status ?? "-";
      document.getElementById("cockpit-calibration").textContent = status.calibration?.comparison?.status ?? "-";
      document.getElementById("cockpit-corpus").textContent = status.corpus?.active_count ?? "-";
      document.getElementById("cockpit-shadow").textContent = status.shadow?.status ?? "-";
      document.getElementById("cockpit-artifacts").textContent = status.artifact_outcomes?.coverage?.eligible_count ?? "-";
      document.getElementById("cockpit-promotions").textContent = status.promotions?.coverage?.active_count ?? "-";
      document.getElementById("cockpit-trace").innerHTML = (status.last_trace || []).map((step) => `<span>${step}</span>`).join("");
      document.getElementById("cockpit-decision").textContent = JSON.stringify(status.decision_explanation || {}, null, 2);
      document.getElementById("cockpit-verification-detail").textContent = JSON.stringify(status.verification || {}, null, 2);
      document.getElementById("cockpit-evaluation-detail").textContent = JSON.stringify(status.evaluation || {}, null, 2);
      const calibrations = await (await fetch("/api/calibrations")).json();
      document.getElementById("cockpit-calibration-detail").textContent = JSON.stringify(calibrations, null, 2);
      const corpus = await (await fetch("/api/corpus")).json();
      document.getElementById("cockpit-corpus-detail").textContent = JSON.stringify(corpus, null, 2);
      const shadows = await (await fetch("/api/shadow-evaluations")).json();
      document.getElementById("cockpit-shadow-detail").textContent = JSON.stringify(shadows, null, 2);
      const artifacts = await (await fetch("/api/artifact-outcomes")).json();
      document.getElementById("cockpit-artifacts-detail").textContent = JSON.stringify(artifacts, null, 2);
      const promotions = await (await fetch("/api/promotions")).json();
      document.getElementById("cockpit-promotions-detail").textContent = JSON.stringify(promotions, null, 2);
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

    def __init__(
        self,
        db_path: str | None = None,
        orchestrator: RuntimeOrchestrator | None = None,
    ):
        resolved_db_path = (
            db_path
            if db_path is not None
            else os.environ.get(DEFAULT_COCKPIT_DB_ENV, DEFAULT_COCKPIT_DB_PATH)
        )
        self.state = SubjectState(metadata={"version": 0, "goals": []})
        self.orchestrator = orchestrator or RuntimeOrchestrator(db_path=resolved_db_path)
        self.loop = RuntimeLoop(orchestrator=self.orchestrator, state=self.state)
        self.last_state = self.state
        self.last_swarm_result: dict[str, dict[str, Any]] = {}
        self.last_shadow: dict[str, Any] = {}
        self.last_artifact_outcome: dict[str, Any] = {}
        self.last_promotion: dict[str, Any] = {}

    def status(self) -> dict:
        metadata = self.last_state.metadata
        artifact_outcomes = self.orchestrator.artifact_outcomes()
        promotions = self.orchestrator.longitudinal_promotions()
        return {
            "running": self.loop.ctx.running,
            "tick": self.loop.ctx.tick,
            "esi": metadata.get("phase2_reflection", {}).get("esi", 1.0),
            "last_trace": metadata.get("last_trace", []),
            "requires_api_key": self.orchestrator.model_provider.health().get("requires_api_key", False),
            "memory_stats": self.orchestrator.memory_provider.long_term_memory.stats(),
            "subjects": self.subjects()["subjects"],
            "pending_approvals": self.orchestrator.pending_approvals(),
            "evidence": metadata.get("evidence", {}),
            "decision_explanation": metadata.get("decision_explanation", {}),
            "covenant_policy": metadata.get("covenant_policy", {}),
            "verification": metadata.get("verification_run", {}),
            "evaluation": metadata.get("capability_evaluation", {}),
            "calibration": metadata.get("calibration_registry", {}),
            "corpus": self.orchestrator.operational_corpus.stats(),
            "shadow": self.last_shadow,
            "artifact_outcomes": {
                "latest": self.last_artifact_outcome or artifact_outcomes["latest"],
                "coverage": artifact_outcomes["coverage"],
            },
            "promotions": {
                "latest": self.last_promotion or promotions["latest"],
                "coverage": promotions["coverage"],
            },
        }

    def goals(self) -> dict:
        return {"goals": self.last_state.metadata.get("goals", [])}

    def evidence(self) -> dict:
        return {
            "evidence": self.last_state.metadata.get("evidence", {}),
            "decision_explanation": self.last_state.metadata.get(
                "decision_explanation",
                {},
            ),
        }

    def verification(self) -> dict:
        return {"verification": self.last_state.metadata.get("verification_run", {})}

    def evaluations(self) -> dict:
        return {"evaluation": self.last_state.metadata.get("capability_evaluation", {})}

    def calibrations(self) -> dict:
        records = self.orchestrator.calibration_registry.history()
        return {
            "latest": records[-1] if records else {},
            "records": records,
        }

    def corpus(self) -> dict:
        return {
            "manifest": self.orchestrator.operational_corpus.manifest(),
            "stats": self.orchestrator.operational_corpus.stats(),
        }

    def register_corpus_task(self, payload: dict) -> dict:
        return self.orchestrator.register_operational_task(payload)

    def shadow_evaluations(self) -> dict:
        records = self.orchestrator.operational_shadow_history()
        return {
            "latest": self.last_shadow or (records[-1] if records else {}),
            "records": records,
        }

    def run_shadow_evaluation(self, payload: dict) -> dict:
        self.last_shadow = self.orchestrator.run_operational_shadow(payload)
        return self.last_shadow

    def artifact_outcomes(self) -> dict:
        return self.orchestrator.artifact_outcomes()

    def record_artifact_outcome(self, payload: dict) -> dict:
        self.last_artifact_outcome = self.orchestrator.record_artifact_outcome(payload)
        return self.last_artifact_outcome

    def promotions(self) -> dict:
        return self.orchestrator.longitudinal_promotions()

    def propose_promotion(self, payload: dict) -> dict:
        self.last_promotion = self.orchestrator.propose_longitudinal_promotion(payload)
        return self.last_promotion

    def resolve_promotion(self, promotion_id: str, *, approved: bool) -> dict:
        self.last_promotion = self.orchestrator.resolve_longitudinal_promotion(
            promotion_id,
            approved=approved,
        )
        return self.last_promotion

    def rollback_promotion(self, promotion_id: str, *, approved: bool) -> dict:
        self.last_promotion = self.orchestrator.rollback_longitudinal_promotion(
            promotion_id,
            approved=approved,
        )
        return self.last_promotion

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
            "evidence": self.last_state.metadata.get("evidence", {}),
            "decision_explanation": self.last_state.metadata.get(
                "decision_explanation",
                {},
            ),
            "verification": self.last_state.metadata.get("verification_run", {}),
            "evaluation": self.last_state.metadata.get("capability_evaluation", {}),
            "calibration": self.last_state.metadata.get("calibration_registry", {}),
            "goal": (self.last_state.metadata.get("goals") or [{}])[0],
            "amplification": self.last_state.metadata.get("intelligence_amplification", {}),
        }

    def approve(self, approval_id: str, *, approved: bool) -> dict:
        self.last_state = self.orchestrator.resolve_approval(approval_id, approved=approved)
        return {
            "version": self.last_state.metadata.get("version"),
            "trace": self.last_state.metadata.get("last_trace", []),
            "governance": self.last_state.metadata.get("governance", {}),
            "evidence": self.last_state.metadata.get("evidence", {}),
            "decision_explanation": self.last_state.metadata.get(
                "decision_explanation",
                {},
            ),
            "verification": self.last_state.metadata.get("verification_run", {}),
            "evaluation": self.last_state.metadata.get("capability_evaluation", {}),
            "calibration": self.last_state.metadata.get("calibration_registry", {}),
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
            "evidence": executor_state.metadata.get("evidence", {}),
            "decision_explanation": executor_state.metadata.get(
                "decision_explanation",
                {},
            ),
            "verification": executor_state.metadata.get("verification_run", {}),
            "evaluation": executor_state.metadata.get("capability_evaluation", {}),
            "calibration": executor_state.metadata.get("calibration_registry", {}),
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


_DEFAULT_COCKPIT: CockpitRuntime | None = None
_DEFAULT_COCKPIT_LOCK = Lock()


def get_default_cockpit() -> CockpitRuntime:
    """Create the process default only when an unscoped request actually needs it."""
    global _DEFAULT_COCKPIT
    cockpit = _DEFAULT_COCKPIT
    if cockpit is None:
        with _DEFAULT_COCKPIT_LOCK:
            cockpit = _DEFAULT_COCKPIT
            if cockpit is None:
                cockpit = CockpitRuntime()
                _DEFAULT_COCKPIT = cockpit
    return cockpit


def handle_cockpit_request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    runtime: CockpitRuntime | None = None,
) -> tuple[HTTPStatus, dict[str, Any]]:
    runtime = runtime if runtime is not None else get_default_cockpit()
    parsed = urlparse(path)

    if method == "GET" and parsed.path == "/api/status":
        return HTTPStatus.OK, runtime.status()

    if method == "GET" and parsed.path == "/api/goals":
        return HTTPStatus.OK, runtime.goals()

    if method == "GET" and parsed.path == "/api/subjects":
        return HTTPStatus.OK, runtime.subjects()

    if method == "GET" and parsed.path == "/api/evidence":
        return HTTPStatus.OK, runtime.evidence()

    if method == "GET" and parsed.path == "/api/verification":
        return HTTPStatus.OK, runtime.verification()

    if method == "GET" and parsed.path == "/api/evaluations":
        return HTTPStatus.OK, runtime.evaluations()

    if method == "GET" and parsed.path == "/api/calibrations":
        return HTTPStatus.OK, runtime.calibrations()

    if method == "GET" and parsed.path == "/api/corpus":
        return HTTPStatus.OK, runtime.corpus()

    if method == "GET" and parsed.path == "/api/shadow-evaluations":
        return HTTPStatus.OK, runtime.shadow_evaluations()

    if method == "GET" and parsed.path == "/api/artifact-outcomes":
        return HTTPStatus.OK, runtime.artifact_outcomes()

    if method == "GET" and parsed.path == "/api/promotions":
        return HTTPStatus.OK, runtime.promotions()

    if method == "GET" and parsed.path == "/api/memory/search":
        query = parse_qs(parsed.query).get("q", [""])[0]
        return HTTPStatus.OK, runtime.memory_search(query)

    if method == "POST" and parsed.path == "/api/dispatch":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.dispatch(payload)

    if method == "POST" and parsed.path == "/api/corpus/tasks":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.register_corpus_task(payload)

    if method == "POST" and parsed.path == "/api/shadow-evaluations":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.run_shadow_evaluation(payload)

    if method == "POST" and parsed.path == "/api/artifact-outcomes":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.record_artifact_outcome(payload)

    if method == "POST" and parsed.path == "/api/promotions":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.propose_promotion(payload)

    if method == "POST" and parsed.path == "/api/promotions/approval":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.resolve_promotion(
            str(payload["promotion_id"]),
            approved=bool(payload.get("approved")),
        )

    if method == "POST" and parsed.path == "/api/promotions/rollback":
        payload = json.loads((body or b"{}").decode("utf-8"))
        return HTTPStatus.OK, runtime.rollback_promotion(
            str(payload["promotion_id"]),
            approved=bool(payload.get("approved")),
        )

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

        if parsed.path in {
            "/api/status",
            "/api/goals",
            "/api/subjects",
            "/api/evidence",
            "/api/verification",
            "/api/evaluations",
            "/api/calibrations",
            "/api/corpus",
            "/api/shadow-evaluations",
            "/api/artifact-outcomes",
            "/api/promotions",
            "/api/memory/search",
        }:
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
