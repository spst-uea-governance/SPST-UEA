import hashlib
import json
import math
import subprocess
import unicodedata
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from spst_runtime.action_manifest import ActionManifestLedger
from spst_runtime.context_mediation import CONTEXT_AUTHORITY
from spst_runtime.memory.long_term_memory import (
    CURRENT_MEMORY_POLICY_VERSION,
    LongTermMemoryStore,
    MemoryRecord,
    memory_record_binding_sha256,
)
from spst_runtime.repository_identity import (
    RepositoryIdentityError,
    capture_repository_identity,
    resolve_repository_root,
    validate_repository_identity,
)
from spst_runtime.routing_receipt import (
    CONTEXT_ORIGIN_INDEX_SCHEMA,
    ROUTING_RECEIPT_SCHEMA_V3,
    ROUTING_RECEIPT_SCHEMA_V4,
    RoutingReceiptLedger,
)


EVIDENCE_CONTEXT_ARTIFACT_SCHEMA = "spst-evidence-context-artifact-v1"
EVIDENCE_CONTEXT_PROJECTION_SCHEMA = "spst-evidence-context-projection-v1"
EVIDENCE_CONTEXT_BINDING_TYPE = "evidence_context_artifact"
EVIDENCE_CONTEXT_COMPILER_PROFILE = "evidence-derived-context-v1"
EVIDENCE_CONTEXT_SOURCE = "evidence_context_compiler"
EVIDENCE_CONTEXT_METADATA_KEY = "evidence_context_artifact"
SUPPORTED_ARTIFACT_KINDS = frozenset(
    {
        "architecture_decision",
        "code_fact",
        "test_evidence",
        "failure_lesson",
        "rule_crystal",
    }
)
SUPPORTED_SOURCE_KINDS = frozenset({"repository_file", "git_commit", "action_result"})
QUALITY_ACTION_PROFILES = frozenset({"pytest", "ruff", "mypy", "git_diff_check"})
MAX_SOURCE_BYTES = 1_000_000
MAX_PROJECTION_CHARS = 1_500


class EvidenceContextError(ValueError):
    """Raised when an artifact cannot be compiled without inventing evidence."""


class EvidenceContextVerifier:
    """Independently revalidate typed context records against their live sources."""

    def __init__(
        self,
        *,
        repository_root: str | Path,
        session_path: str | Path,
        repository_identity: dict[str, Any] | None = None,
    ):
        self.repository_root = _resolve_repository_root(repository_root)
        self.repository_identity = repository_identity or capture_repository_identity(
            self.repository_root
        )
        valid, reason = validate_repository_identity(self.repository_identity)
        if not valid:
            raise EvidenceContextError(reason or "repository_identity_invalid")
        self.session_path = str(Path(session_path).expanduser())
        self.routing = RoutingReceiptLedger(self.session_path, read_only=True)
        self.actions = ActionManifestLedger(self.session_path, read_only=True)

    def producer_binding(self, receipt_id: str) -> dict[str, Any]:
        if not _is_sha256(receipt_id):
            raise EvidenceContextError("artifact_producer_receipt_id_invalid")
        result = self.routing.verify(receipt_id)
        if result.get("verified") is not True:
            raise EvidenceContextError(
                f"artifact_producer_receipt_unverified:{result.get('reason', 'unknown')}"
            )
        if result.get("schema") not in {
            ROUTING_RECEIPT_SCHEMA_V3,
            ROUTING_RECEIPT_SCHEMA_V4,
        }:
            raise EvidenceContextError("artifact_producer_receipt_repository_unbound")
        governance = result.get("governance", {})
        if governance.get("authorized") is not True:
            raise EvidenceContextError("artifact_producer_governance_unauthorized")
        repository = result.get("repository", {})
        if (
            repository.get("bound") is not True
            or repository.get("binding_verified") is not True
            or not _is_sha256(repository.get("identity_sha256"))
        ):
            raise EvidenceContextError("artifact_producer_repository_unverified")
        session = result.get("session", {})
        turn = session.get("turn")
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            raise EvidenceContextError("artifact_producer_turn_invalid")
        binding = result.get("binding", {})
        if any(
            not isinstance(binding.get(field), int)
            or isinstance(binding.get(field), bool)
            or int(binding[field]) < 1
            for field in ("receipt_sequence", "session_sequence")
        ) or any(
            not _is_sha256(binding.get(field))
            for field in ("receipt_record_hash", "receipt_chain_hash")
        ):
            raise EvidenceContextError("artifact_producer_provenance_invalid")
        return {
            "receipt_id": result["receipt_id"],
            "receipt_schema": result["schema"],
            "session_turn": turn,
            "governance_authorized": True,
            "repository": {
                "identity_sha256": repository["identity_sha256"],
                "head_revision": repository.get("head_revision"),
                "worktree_sha256": repository.get("worktree_sha256"),
            },
            "provenance_binding": {
                field: binding[field]
                for field in (
                    "receipt_sequence",
                    "receipt_record_hash",
                    "receipt_chain_hash",
                    "session_sequence",
                )
            },
        }

    def repository_file_source(self, relative_path: str) -> dict[str, Any]:
        normalized, candidate = _safe_repository_file(self.repository_root, relative_path)
        size = candidate.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise EvidenceContextError("artifact_source_file_too_large")
        content_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        git_blob_oid = _run_git(
            self.repository_root,
            "hash-object",
            "--no-filters",
            "--",
            normalized,
            reason="artifact_source_git_blob_unavailable",
        )
        unsigned = {
            "schema": "spst-evidence-source-v1",
            "kind": "repository_file",
            "relative_path": normalized,
            "byte_count": size,
            "content_sha256": content_sha256,
            "git_blob_oid": git_blob_oid,
            "object_format": self.repository_identity["object_format"],
            "compiled_repository_identity_sha256": self.repository_identity[
                "identity_sha256"
            ],
            "compiled_head_revision": self.repository_identity["head_revision"],
        }
        return {**unsigned, "source_sha256": _canonical_hash(unsigned)}

    def git_commit_source(self, revision: str) -> dict[str, Any]:
        if not isinstance(revision, str) or not revision.strip():
            raise EvidenceContextError("artifact_source_commit_missing")
        commit = _run_git(
            self.repository_root,
            "rev-parse",
            "--verify",
            f"{revision.strip()}^{{commit}}",
            reason="artifact_source_commit_unresolved",
        )
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=self.repository_root,
            check=False,
            capture_output=True,
        )
        if ancestor.returncode == 1:
            raise EvidenceContextError("artifact_source_commit_not_ancestor")
        if ancestor.returncode != 0:
            raise EvidenceContextError("artifact_source_commit_ancestry_unresolved")
        tree = _run_git(
            self.repository_root,
            "rev-parse",
            "--verify",
            f"{commit}^{{tree}}",
            reason="artifact_source_commit_tree_unresolved",
        )
        subject = _run_git(
            self.repository_root,
            "show",
            "-s",
            "--format=%s",
            commit,
            reason="artifact_source_commit_subject_unresolved",
        )
        unsigned = {
            "schema": "spst-evidence-source-v1",
            "kind": "git_commit",
            "commit_revision": commit,
            "tree_revision": tree,
            "subject_sha256": hashlib.sha256(subject.encode("utf-8")).hexdigest(),
            "object_format": self.repository_identity["object_format"],
            "compiled_repository_identity_sha256": self.repository_identity[
                "identity_sha256"
            ],
        }
        return {**unsigned, "source_sha256": _canonical_hash(unsigned)}

    def action_result_source(self, action_id: str) -> dict[str, Any]:
        if not _is_sha256(action_id):
            raise EvidenceContextError("artifact_source_action_id_invalid")
        result = self.actions.verify(action_id)
        if result.get("verified") is not True or result.get("execution_verified") is not True:
            raise EvidenceContextError(
                f"artifact_source_action_unverified:{result.get('reason', 'unknown')}"
            )
        action = result.get("action", {})
        execution = result.get("execution", {})
        transition = result.get("repository_transition", {})
        after = transition.get("after_repository_identity")
        if (
            transition.get("transition_verified") is not True
            or not isinstance(after, dict)
            or not _is_sha256(after.get("identity_sha256"))
        ):
            raise EvidenceContextError("artifact_source_action_repository_unverified")
        output_digest = execution.get("output_digest")
        evidence_binding = execution.get("binding", {})
        manifest_binding = result.get("binding", {}).get("manifest", {})
        if not _is_sha256(output_digest) or not _is_sha256(
            evidence_binding.get("record_hash")
        ) or not _is_sha256(manifest_binding.get("record_hash")):
            raise EvidenceContextError("artifact_source_action_evidence_invalid")
        unsigned = {
            "schema": "spst-evidence-source-v1",
            "kind": "action_result",
            "action_id": action_id,
            "parent_receipt_id": result.get("parent_receipt_id"),
            "operation": action.get("operation"),
            "profile": action.get("profile"),
            "successful": result.get("successful") is True,
            "execution_status": execution.get("status"),
            "returncode": execution.get("returncode"),
            "output_digest": output_digest,
            "manifest_record_hash": manifest_binding["record_hash"],
            "evidence_record_hash": evidence_binding["record_hash"],
            "after_repository_identity_sha256": after["identity_sha256"],
        }
        return {**unsigned, "source_sha256": _canonical_hash(unsigned)}

    def verify_record(
        self,
        record: dict[str, Any] | MemoryRecord,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        data = record.as_dict() if isinstance(record, MemoryRecord) else record
        if not isinstance(data, dict):
            return _verification_failure("artifact_record_invalid")
        if data.get("source") != EVIDENCE_CONTEXT_SOURCE:
            return _verification_failure("not_evidence_context_record")
        metadata = data.get("metadata")
        artifact = metadata.get(EVIDENCE_CONTEXT_METADATA_KEY) if isinstance(metadata, dict) else None
        artifact_reason = _artifact_reason(artifact)
        if artifact_reason is not None or not isinstance(artifact, dict):
            return _verification_failure(artifact_reason or "artifact_metadata_missing")
        if data.get("kind") != artifact.get("artifact_kind"):
            return _verification_failure("artifact_record_kind_mismatch")
        if data.get("policy_version") != artifact.get("policy", {}).get("version"):
            return _verification_failure("artifact_record_policy_mismatch")
        if data.get("confidence") != artifact.get("policy", {}).get("confidence"):
            return _verification_failure("artifact_record_confidence_mismatch")
        if data.get("expires_at") != artifact.get("policy", {}).get("expires_at"):
            return _verification_failure("artifact_record_expiry_mismatch")
        if data.get("status") != "active" or artifact.get("lifecycle", {}).get("status") != "active":
            return _verification_failure("artifact_not_active")
        try:
            expiry = _parse_datetime(str(data.get("expires_at")))
        except ValueError:
            return _verification_failure("artifact_expiry_invalid")
        if expiry <= _utc(now):
            return _verification_failure("artifact_expired")
        expected_projection = _projection(artifact)
        if data.get("text") != _canonical_json(expected_projection):
            return _verification_failure("artifact_projection_mismatch")
        expected_projection_sha256 = hashlib.sha256(
            str(data["text"]).encode("utf-8")
        ).hexdigest()
        if not isinstance(metadata, dict) or metadata.get(
            "evidence_context_projection_sha256"
        ) != expected_projection_sha256:
            return _verification_failure("artifact_projection_digest_mismatch")
        try:
            producer = self.producer_binding(str(artifact.get("producer", {}).get("receipt_id", "")))
        except EvidenceContextError as error:
            return _verification_failure(str(error))
        if producer != artifact.get("producer"):
            return _verification_failure("artifact_producer_binding_mismatch")
        try:
            source_binding = self._verify_source(artifact["source"])
        except EvidenceContextError as error:
            return _verification_failure(str(error))
        if source_binding.get("current_match") is not True:
            return _verification_failure(str(source_binding.get("reason")))
        try:
            record_sha256 = memory_record_binding_sha256(data)
        except (TypeError, ValueError):
            return _verification_failure("artifact_record_binding_unresolved")

        repository = producer["repository"]
        current_identity_sha256 = self.repository_identity["identity_sha256"]
        origin = {
            "binding_type": EVIDENCE_CONTEXT_BINDING_TYPE,
            "receipt_verified": True,
            "receipt_id": producer["receipt_id"],
            "receipt_schema": producer["receipt_schema"],
            "session_turn": producer["session_turn"],
            "record_id": data.get("id"),
            "policy_version": data.get("policy_version"),
            "record_text_sha256": hashlib.sha256(str(data.get("text", "")).encode("utf-8")).hexdigest(),
            "record_sha256": record_sha256,
            "record_source": data.get("source"),
            "record_kind": data.get("kind"),
            "record_created_turn": data.get("created_turn"),
            "repository": {
                "bound": True,
                "binding_verified": True,
                "identity_sha256": repository["identity_sha256"],
                "head_revision": repository.get("head_revision"),
                "worktree_sha256": repository.get("worktree_sha256"),
                "current_match": repository["identity_sha256"] == current_identity_sha256,
                "current_identity_sha256": current_identity_sha256,
                "reason": (
                    None
                    if repository["identity_sha256"] == current_identity_sha256
                    else "producer_repository_identity_historical"
                ),
            },
            "provenance_binding": producer["provenance_binding"],
            "artifact": {
                "schema": artifact["schema"],
                "artifact_sha256": artifact["artifact_sha256"],
                "artifact_kind": artifact["artifact_kind"],
                "compiler_profile": artifact["compiler_profile"],
                "semantic_claim": artifact["semantic_claim"],
            },
            "source_binding": source_binding,
        }
        return {
            "verified": True,
            "reason": None,
            "artifact_sha256": artifact["artifact_sha256"],
            "origin": origin,
        }

    def augment_origin_index(
        self,
        origin_index: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Add independently verified artifact origins without writing either DB."""

        if not _origin_index_valid(origin_index):
            return origin_index
        artifact_records = [record for record in records if _looks_like_artifact_record(record)]
        if not artifact_records:
            return origin_index
        payload = deepcopy({key: value for key, value in origin_index.items() if key != "index_sha256"})
        bindings = payload.get("bindings")
        if not isinstance(bindings, dict):
            return origin_index
        rejections: dict[str, str] = {}
        reason_counts: dict[str, int] = {}
        verified_count = 0
        for record in artifact_records:
            result = self.verify_record(record, now=now)
            record_id = str(record.get("id", ""))
            if result.get("verified") is True:
                bindings.setdefault(record_id, []).append(result["origin"])
                verified_count += 1
            else:
                reason = str(result.get("reason") or "artifact_verification_failed")
                rejections[record_id] = reason
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        payload["bindings"] = bindings
        payload["bound_record_count"] = len(bindings)
        payload["artifact_compiler"] = {
            "profile": EVIDENCE_CONTEXT_COMPILER_PROFILE,
            "candidate_count": len(artifact_records),
            "verified_count": verified_count,
            "rejected_count": len(rejections),
            "rejection_reasons": dict(sorted(reason_counts.items())),
        }
        payload["artifact_rejections"] = dict(sorted(rejections.items()))
        return {**payload, "index_sha256": _canonical_hash(payload)}

    def _verify_source(self, source: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(source, dict):
            raise EvidenceContextError("artifact_source_invalid")
        if source.get("schema") != "spst-evidence-source-v1":
            raise EvidenceContextError("artifact_source_schema_mismatch")
        source_sha256 = source.get("source_sha256")
        unsigned = {key: value for key, value in source.items() if key != "source_sha256"}
        if not _is_sha256(source_sha256) or _canonical_hash(unsigned) != source_sha256:
            raise EvidenceContextError("artifact_source_digest_mismatch")
        kind = source.get("kind")
        if kind == "repository_file":
            current = self.repository_file_source(str(source.get("relative_path", "")))
            fields: tuple[str, ...] = (
                "relative_path",
                "byte_count",
                "content_sha256",
                "git_blob_oid",
                "object_format",
            )
            current_match = all(source.get(field) == current.get(field) for field in fields)
            reason = None if current_match else "artifact_source_file_digest_mismatch"
        elif kind == "git_commit":
            current = self.git_commit_source(str(source.get("commit_revision", "")))
            fields = ("commit_revision", "tree_revision", "subject_sha256", "object_format")
            current_match = all(source.get(field) == current.get(field) for field in fields)
            reason = None if current_match else "artifact_source_commit_digest_mismatch"
        elif kind == "action_result":
            current = self.action_result_source(str(source.get("action_id", "")))
            stable = {key: value for key, value in current.items() if key != "source_sha256"}
            claimed = {key: value for key, value in source.items() if key != "source_sha256"}
            evidence_match = stable == claimed
            repository_match = (
                source.get("after_repository_identity_sha256")
                == self.repository_identity.get("identity_sha256")
            )
            current_match = evidence_match and repository_match
            reason = (
                None
                if current_match
                else "artifact_source_action_repository_stale"
                if evidence_match
                else "artifact_source_action_digest_mismatch"
            )
        else:
            raise EvidenceContextError("artifact_source_kind_unsupported")
        return {
            "schema": "spst-evidence-source-binding-v1",
            "kind": kind,
            "source_sha256": source_sha256,
            "verified": True,
            "current_match": current_match,
            "reason": reason,
            "compiled_repository_identity_sha256": source.get(
                "compiled_repository_identity_sha256"
            ),
            "current_repository_identity_sha256": self.repository_identity[
                "identity_sha256"
            ],
        }


class EvidenceContextCompiler:
    """Compile evidence projections into the existing governed memory store."""

    def __init__(
        self,
        *,
        repository_root: str | Path,
        session_path: str | Path,
        memory_path: str | Path,
        read_only: bool = False,
    ):
        self.read_only = read_only
        self.verifier = EvidenceContextVerifier(
            repository_root=repository_root,
            session_path=session_path,
        )
        self.memory = LongTermMemoryStore(str(memory_path), read_only=read_only)

    def compile_repository_file(
        self,
        *,
        producer_receipt_id: str,
        artifact_kind: str,
        title: str,
        statement: str,
        relative_path: str,
        tags: list[str] | None = None,
        confidence: float = 0.9,
        ttl_seconds: int = 7_776_000,
        supersedes_artifact_sha256: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._compile(
            producer_receipt_id=producer_receipt_id,
            artifact_kind=artifact_kind,
            title=title,
            statement=statement,
            source=self.verifier.repository_file_source(relative_path),
            tags=tags,
            confidence=confidence,
            ttl_seconds=ttl_seconds,
            supersedes_artifact_sha256=supersedes_artifact_sha256,
            now=now,
        )

    def compile_git_commit(
        self,
        *,
        producer_receipt_id: str,
        artifact_kind: str,
        title: str,
        statement: str,
        revision: str,
        tags: list[str] | None = None,
        confidence: float = 0.9,
        ttl_seconds: int = 7_776_000,
        supersedes_artifact_sha256: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._compile(
            producer_receipt_id=producer_receipt_id,
            artifact_kind=artifact_kind,
            title=title,
            statement=statement,
            source=self.verifier.git_commit_source(revision),
            tags=tags,
            confidence=confidence,
            ttl_seconds=ttl_seconds,
            supersedes_artifact_sha256=supersedes_artifact_sha256,
            now=now,
        )

    def compile_action_result(
        self,
        *,
        producer_receipt_id: str,
        artifact_kind: str,
        title: str,
        statement: str,
        action_id: str,
        tags: list[str] | None = None,
        confidence: float = 0.9,
        ttl_seconds: int = 2_592_000,
        supersedes_artifact_sha256: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        source = self.verifier.action_result_source(action_id)
        if artifact_kind == "test_evidence":
            if source.get("profile") not in QUALITY_ACTION_PROFILES:
                raise EvidenceContextError("test_evidence_requires_quality_action")
            if source.get("successful") is not True:
                raise EvidenceContextError("test_evidence_requires_successful_action")
        return self._compile(
            producer_receipt_id=producer_receipt_id,
            artifact_kind=artifact_kind,
            title=title,
            statement=statement,
            source=source,
            tags=tags,
            confidence=confidence,
            ttl_seconds=ttl_seconds,
            supersedes_artifact_sha256=supersedes_artifact_sha256,
            now=now,
        )

    def _compile(
        self,
        *,
        producer_receipt_id: str,
        artifact_kind: str,
        title: str,
        statement: str,
        source: dict[str, Any],
        tags: list[str] | None,
        confidence: float,
        ttl_seconds: int,
        supersedes_artifact_sha256: str | None,
        now: datetime | None,
    ) -> dict[str, Any]:
        if self.read_only:
            raise EvidenceContextError("evidence_context_store_read_only")
        normalized_kind = _artifact_kind(artifact_kind)
        normalized_title = _bounded_text(title, "artifact_title", 120)
        normalized_statement = _bounded_text(statement, "artifact_statement", 700)
        normalized_tags = _tags(tags)
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise EvidenceContextError("artifact_confidence_invalid")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 31_536_000:
            raise EvidenceContextError("artifact_ttl_invalid")
        if supersedes_artifact_sha256 is not None and not _is_sha256(
            supersedes_artifact_sha256
        ):
            raise EvidenceContextError("artifact_supersedes_digest_invalid")
        producer = self.verifier.producer_binding(producer_receipt_id)
        reference_time = _utc(now)
        expires_at = (reference_time + timedelta(seconds=ttl_seconds)).isoformat()
        payload = {
            "schema": EVIDENCE_CONTEXT_ARTIFACT_SCHEMA,
            "compiler_profile": EVIDENCE_CONTEXT_COMPILER_PROFILE,
            "artifact_kind": normalized_kind,
            "title": normalized_title,
            "statement": normalized_statement,
            "tags": normalized_tags,
            "authority": CONTEXT_AUTHORITY,
            "semantic_claim": "asserted_not_independently_established",
            "source": source,
            "producer": producer,
            "policy": {
                "version": CURRENT_MEMORY_POLICY_VERSION,
                "confidence": float(confidence),
                "compiled_at": reference_time.isoformat(),
                "expires_at": expires_at,
            },
            "lifecycle": {
                "status": "active",
                "supersedes_artifact_sha256": supersedes_artifact_sha256,
            },
        }
        artifact = {**payload, "artifact_sha256": _canonical_hash(payload)}
        projection_text = _canonical_json(_projection(artifact))
        if len(projection_text) > MAX_PROJECTION_CHARS:
            raise EvidenceContextError("artifact_projection_too_large")

        superseded = self._superseded_record(
            supersedes_artifact_sha256,
            expected_kind=normalized_kind,
        )
        record = self.memory.remember(
            projection_text,
            kind=normalized_kind,
            source=EVIDENCE_CONTEXT_SOURCE,
            tags=sorted(
                set(normalized_tags)
                | {"evidence_context", normalized_kind, str(source["kind"])}
            ),
            salience=0.9,
            created_turn=int(producer["session_turn"]),
            metadata={
                EVIDENCE_CONTEXT_METADATA_KEY: artifact,
                "evidence_context_projection_sha256": hashlib.sha256(
                    projection_text.encode("utf-8")
                ).hexdigest(),
            },
            confidence=float(confidence),
            policy_version=CURRENT_MEMORY_POLICY_VERSION,
            expires_at=expires_at,
            now=reference_time,
        )
        verification = self.verifier.verify_record(record, now=reference_time)
        if verification.get("verified") is not True:
            self.memory.retire(
                record.id,
                reason="compiled_artifact_self_verification_failed",
            )
            raise EvidenceContextError(
                f"compiled_artifact_self_verification_failed:{verification.get('reason')}"
            )
        if superseded is not None and superseded.id != record.id:
            self.memory.retire(
                superseded.id,
                reason="superseded_by_evidence_context_artifact",
                superseded_by=record.id,
            )
        return {
            "schema": "spst-evidence-context-compilation-v1",
            "artifact": artifact,
            "memory_record": record.as_dict(),
            "verification": verification,
            "memory_provenance": self.memory.repository.verify_provenance(),
        }

    def _superseded_record(
        self,
        artifact_sha256: str | None,
        *,
        expected_kind: str,
    ) -> MemoryRecord | None:
        if artifact_sha256 is None:
            return None
        matches = []
        for record in self.memory.list_all():
            metadata = record.metadata
            artifact = metadata.get(EVIDENCE_CONTEXT_METADATA_KEY)
            if isinstance(artifact, dict) and artifact.get("artifact_sha256") == artifact_sha256:
                matches.append(record)
        if len(matches) != 1:
            raise EvidenceContextError(
                "artifact_supersedes_missing" if not matches else "artifact_supersedes_ambiguous"
            )
        record = matches[0]
        if record.kind != expected_kind:
            raise EvidenceContextError("artifact_supersedes_kind_mismatch")
        if record.status != "active":
            raise EvidenceContextError("artifact_supersedes_not_active")
        return record


def reject_unverifiable_artifact_origins(
    origin_index: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    """Annotate artifact rejection when repository-scoped verification is unavailable."""

    if not _origin_index_valid(origin_index):
        return origin_index
    artifacts = [record for record in records if _looks_like_artifact_record(record)]
    if not artifacts:
        return origin_index
    payload = deepcopy({key: value for key, value in origin_index.items() if key != "index_sha256"})
    rejections = {str(record.get("id", "")): reason for record in artifacts}
    payload["artifact_compiler"] = {
        "profile": EVIDENCE_CONTEXT_COMPILER_PROFILE,
        "candidate_count": len(artifacts),
        "verified_count": 0,
        "rejected_count": len(artifacts),
        "rejection_reasons": {reason: len(artifacts)},
    }
    payload["artifact_rejections"] = dict(sorted(rejections.items()))
    return {**payload, "index_sha256": _canonical_hash(payload)}


def _artifact_reason(artifact: Any) -> str | None:
    if not isinstance(artifact, dict):
        return "artifact_metadata_missing"
    if artifact.get("schema") != EVIDENCE_CONTEXT_ARTIFACT_SCHEMA:
        return "artifact_schema_mismatch"
    if artifact.get("compiler_profile") != EVIDENCE_CONTEXT_COMPILER_PROFILE:
        return "artifact_compiler_profile_mismatch"
    try:
        _artifact_kind(str(artifact.get("artifact_kind", "")))
    except EvidenceContextError as error:
        return str(error)
    if artifact.get("authority") != CONTEXT_AUTHORITY:
        return "artifact_authority_mismatch"
    if artifact.get("semantic_claim") != "asserted_not_independently_established":
        return "artifact_semantic_claim_invalid"
    if not isinstance(artifact.get("title"), str) or not artifact["title"]:
        return "artifact_title_invalid"
    if not isinstance(artifact.get("statement"), str) or not artifact["statement"]:
        return "artifact_statement_invalid"
    tags = artifact.get("tags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag for tag in tags):
        return "artifact_tags_invalid"
    source = artifact.get("source")
    if not isinstance(source, dict) or source.get("kind") not in SUPPORTED_SOURCE_KINDS:
        return "artifact_source_kind_unsupported"
    source_digest = source.get("source_sha256")
    source_unsigned = {key: value for key, value in source.items() if key != "source_sha256"}
    if not _is_sha256(source_digest) or _canonical_hash(source_unsigned) != source_digest:
        return "artifact_source_digest_mismatch"
    if source["kind"] == "repository_file" and not isinstance(
        source.get("relative_path"), str
    ):
        return "artifact_source_file_invalid"
    if source["kind"] == "git_commit" and not isinstance(
        source.get("commit_revision"), str
    ):
        return "artifact_source_commit_invalid"
    if source["kind"] == "action_result" and not _is_sha256(source.get("action_id")):
        return "artifact_source_action_id_invalid"
    producer = artifact.get("producer")
    if not isinstance(producer, dict) or not _is_sha256(producer.get("receipt_id")):
        return "artifact_producer_binding_invalid"
    digest = artifact.get("artifact_sha256")
    unsigned = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
    if not _is_sha256(digest) or _canonical_hash(unsigned) != digest:
        return "artifact_digest_mismatch"
    policy = artifact.get("policy")
    if not isinstance(policy, dict) or policy.get("version") != CURRENT_MEMORY_POLICY_VERSION:
        return "artifact_policy_mismatch"
    confidence = policy.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        return "artifact_confidence_invalid"
    try:
        _parse_datetime(str(policy.get("compiled_at")))
        _parse_datetime(str(policy.get("expires_at")))
    except ValueError:
        return "artifact_policy_time_invalid"
    lifecycle = artifact.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get("status") != "active":
        return "artifact_lifecycle_invalid"
    supersedes = lifecycle.get("supersedes_artifact_sha256")
    if supersedes is not None and not _is_sha256(supersedes):
        return "artifact_supersedes_digest_invalid"
    return None


def _projection(artifact: dict[str, Any]) -> dict[str, Any]:
    source = artifact["source"]
    reference: dict[str, Any]
    if source["kind"] == "repository_file":
        reference = {"relative_path": source["relative_path"]}
    elif source["kind"] == "git_commit":
        reference = {"commit_revision": source["commit_revision"]}
    else:
        reference = {
            "action_id": source["action_id"],
            "profile": source.get("profile"),
            "successful": source.get("successful"),
        }
    return {
        "schema": EVIDENCE_CONTEXT_PROJECTION_SCHEMA,
        "artifact_sha256": artifact["artifact_sha256"],
        "artifact_kind": artifact["artifact_kind"],
        "title": artifact["title"],
        "statement": artifact["statement"],
        "tags": artifact["tags"],
        "source": {
            "kind": source["kind"],
            "source_sha256": source["source_sha256"],
            **reference,
        },
        "producer_receipt_id": artifact["producer"]["receipt_id"],
        "policy_version": artifact["policy"]["version"],
        "authority": artifact["authority"],
        "semantic_claim": artifact["semantic_claim"],
    }


def _resolve_repository_root(value: str | Path) -> Path:
    try:
        return resolve_repository_root(value)
    except (OSError, RuntimeError, RepositoryIdentityError) as error:
        raise EvidenceContextError("artifact_repository_root_missing") from error


def _safe_repository_file(root: Path, value: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceContextError("artifact_source_file_missing")
    normalized_input = value.strip().replace("\\", "/")
    relative = PurePosixPath(normalized_input)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise EvidenceContextError("artifact_source_path_invalid")
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise EvidenceContextError("artifact_source_file_missing") from error
    if resolved != root and root not in resolved.parents:
        raise EvidenceContextError("artifact_source_path_escapes_repository")
    if candidate.is_symlink() or not resolved.is_file():
        raise EvidenceContextError("artifact_source_file_invalid")
    return resolved.relative_to(root).as_posix(), resolved


def _run_git(root: Path, *arguments: str, reason: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if completed.returncode != 0:
        raise EvidenceContextError(reason)
    return completed.stdout.rstrip("\r\n")


def _artifact_kind(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in SUPPORTED_ARTIFACT_KINDS:
        raise EvidenceContextError("artifact_kind_unsupported")
    return normalized


def _bounded_text(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise EvidenceContextError(f"{field}_invalid")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise EvidenceContextError(f"{field}_missing")
    if len(normalized) > maximum:
        raise EvidenceContextError(f"{field}_too_large")
    if any(ord(character) < 32 for character in normalized) or any(
        unicodedata.category(character) == "Cf" for character in normalized
    ):
        raise EvidenceContextError(f"{field}_unsafe_characters")
    return normalized


def _tags(values: list[str] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 10:
        raise EvidenceContextError("artifact_tags_invalid")
    return sorted({_bounded_text(value, "artifact_tag", 40).lower() for value in values})


def _origin_index_valid(value: dict[str, Any]) -> bool:
    if not isinstance(value, dict) or value.get("schema") != CONTEXT_ORIGIN_INDEX_SCHEMA:
        return False
    digest = value.get("index_sha256")
    unsigned = {key: item for key, item in value.items() if key != "index_sha256"}
    return _is_sha256(digest) and _canonical_hash(unsigned) == digest


def _looks_like_artifact_record(record: Any) -> bool:
    return isinstance(record, dict) and (
        record.get("source") == EVIDENCE_CONTEXT_SOURCE
        or (
            isinstance(record.get("metadata"), dict)
            and EVIDENCE_CONTEXT_METADATA_KEY in record["metadata"]
        )
    )


def _verification_failure(reason: str) -> dict[str, Any]:
    return {"verified": False, "reason": reason}


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise EvidenceContextError("artifact_not_canonicalizable") from error


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
