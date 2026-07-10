# RFC-0003 Runtime Lifecycle

Status: Draft

Lifecycle:

Uninitialized -> Initializing -> Active -> Paused/Degraded -> Recovering -> Active -> Archived

Every transition MUST emit an event and audit record.
