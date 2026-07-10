class IdentityManager:
    """Verify identity continuity using stable identity hashes."""

    def verify_identity_continuity(self, previous_hash: str, current_hash: str) -> bool:
        return bool(previous_hash) and previous_hash == current_hash
