# ChatGPT Project Snapshot Operations

## Input schema

The compiler accepts UTF-8 JSON with no duplicate keys:

```json
{
  "schema": "chatgpt-project-export-v1",
  "captured_at": "2026-07-29T12:00:00Z",
  "project": {
    "id": "32 lowercase hex characters",
    "slug": "project-url-slug",
    "name": "Project name",
    "url": "https://chatgpt.com/g/g-p-<id>-<slug>/project"
  },
  "completeness": {
    "status": "complete",
    "owner_reviewed": true,
    "chat_count": 1,
    "message_count": 2,
    "source_count": 0
  },
  "chats": [
    {
      "id": "UUID",
      "title": "Chat title",
      "url": "https://chatgpt.com/g/g-p-<id>-<slug>/c/<chat-id>",
      "captured_at": "2026-07-29T12:00:00Z",
      "messages": [
        {
          "id": "stable message id",
          "role": "user or assistant",
          "text": "message text",
          "created_at": "optional timestamp"
        }
      ]
    }
  ],
  "sources": [
    {
      "id": "stable source id",
      "title": "Source title",
      "url": "https or file URL",
      "captured_at": "2026-07-29T12:00:00Z",
      "text": "source contents"
    }
  ]
}
```

The owner or capture procedure is responsible for completeness. The compiler
requires `complete`, explicit owner review, and exact count agreement. A Project
page inventory without every message body is not a valid full snapshot.

## Verification

From `runtime/`:

```powershell
python -m spst_runtime.project_context_bridge status `
  --corpus ..\.spst\project-context\corpus.json
```

`ready` establishes deterministic internal digest and freshness checks only.
It does not authenticate ChatGPT as the source. `blocked` must not be bypassed.

## Data handling

The generated `.spst/project-context/` corpus is local and gitignored. Review
the snapshot for secrets before compilation. Use task-specific bounded query
packets instead of copying the corpus into a prompt. Refresh deliberately after
Project changes; the default freshness limit is 90 days.
