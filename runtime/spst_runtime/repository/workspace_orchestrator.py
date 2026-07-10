from dataclasses import asdict, dataclass
from typing import Any

from spst_runtime.providers.memory_provider import MemoryProvider


@dataclass(frozen=True)
class WorkspaceRegistration:
    workspace_id: str
    policy_version: str


class WorkspaceOrchestrator:
    """Share explicitly published RuleCrystals across registered local workspaces."""

    def __init__(self, policy_version: str = "sovereign-policy-v1"):
        self.policy_version = policy_version
        self._providers: dict[str, MemoryProvider] = {}

    def register_workspace(self, workspace_id: str, provider: MemoryProvider) -> WorkspaceRegistration:
        if not workspace_id:
            raise ValueError("workspace_id is required")
        self._providers[workspace_id] = provider
        return WorkspaceRegistration(workspace_id=workspace_id, policy_version=self.policy_version)

    def publish_rule_crystal(self, workspace_id: str, text: str) -> dict[str, Any]:
        provider = self._provider_for(workspace_id)
        record = provider.long_term_memory.remember(
            text,
            kind="rule_crystal",
            source="workspace_federation",
            tags=["rule_crystal", "workspace_shared", self.policy_version],
            salience=0.95,
            metadata={
                "workspace_id": workspace_id,
                "policy_version": self.policy_version,
                "shareable": True,
            },
        )
        return asdict(record)

    def retrieve_context(self, workspace_id: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        self._provider_for(workspace_id)
        contexts: list[dict[str, Any]] = []
        for source_id in sorted(self._providers):
            if source_id == workspace_id:
                continue
            provider = self._providers[source_id]
            for record in provider.long_term_memory.search(query, limit=top_k):
                metadata = record.get("metadata", {})
                if (
                    record.get("kind") != "rule_crystal"
                    or not metadata.get("shareable")
                    or metadata.get("policy_version") != self.policy_version
                ):
                    continue
                contexts.append(
                    {
                        "key": f"workspace:{source_id}:{record['id']}",
                        "value": {"text": record["text"], "kind": record["kind"]},
                        "metadata": {"federated": True, **metadata},
                        "provenance": {
                            "workspace_id": source_id,
                            "policy_version": self.policy_version,
                            "record_id": record["id"],
                        },
                    }
                )
        contexts.sort(key=lambda item: (item["provenance"]["workspace_id"], item["key"]))
        return contexts[:top_k]

    def status(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "workspaces": [
                asdict(WorkspaceRegistration(workspace_id=workspace_id, policy_version=self.policy_version))
                for workspace_id in sorted(self._providers)
            ],
        }

    def _provider_for(self, workspace_id: str) -> MemoryProvider:
        try:
            return self._providers[workspace_id]
        except KeyError as exc:
            raise KeyError(f"Workspace is not registered: {workspace_id}") from exc
