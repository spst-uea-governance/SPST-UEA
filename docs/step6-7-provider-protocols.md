# Step 6/7 Provider Protocols

## Step 6: Model Adapter Protocol

`ModelAdapter` defines a vendor-neutral model boundary with three methods:

- `infer(prompt, context)`
- `health()`
- `get_capabilities()`

`ModelProvider` composes an optional primary adapter with the API-key-free
`LocalCodexAdapter` fallback. If no primary provider is configured, or if the
primary provider reports unhealthy status, inference remains deterministic and
available through the local Codex-mediated adapter.

## Step 7: Memory Adapter Protocol

`Memory` defines a pipeline-safe memory boundary with four methods:

- `store(key, value, metadata)`
- `retrieve(key)`
- `search(query, top_k)`
- `health()`

`MemoryProvider` bridges this interface to `SQLiteRepository`, preserving WAL
mode and deterministic ordering for search results. It is designed for future
loose coupling from the transition pipeline's retrieve and commit phases.

## Migration Impact

Existing direct adapters remain valid once they implement `health()` and
`get_capabilities()`. The default model provider requires no API key and does
not couple runtime logic to any single external vendor.
