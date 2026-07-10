# Runtime Baseline 0.1

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m mypy spst_runtime
```

## Minimal Verification

If development dependencies are not installed, the baseline test suite can still be checked with:

```bash
python -m pytest -q
```

## Run Locally

```bash
python -m spst_runtime --steps 3 --event codex_run
```

To route a prompt through the API-key-free local/Codex-mediated model boundary:

```bash
python -m spst_runtime --steps 3 --event codex_run --prompt "Say hello from SPST-UEA."
```

Optional: to call OpenAI GPT directly from the runtime itself, set `OPENAI_API_KEY` in your shell and pass `--use-gpt`:

```bash
python -m spst_runtime --steps 3 --event gpt_run --use-gpt --prompt "Say hello from SPST-UEA."
```

## Run In A Browser

```bash
python -m spst_runtime.web --host 127.0.0.1 --port 8767
```

Then open `http://127.0.0.1:8767/`.

On Windows, use the persistent launcher:

```powershell
.\run_spst_web_8767.ps1
```

To stop the persistent browser UI:

```powershell
.\stop_spst_web_8767.ps1
```

The browser UI runs in API-key-free local/Codex-mediated mode by default.

## Run From This Codex Chat

For API-key-free operation from this chat, use the Codex-mediated chat bridge:

```bash
python -m spst_runtime.chat_bridge "Your prompt here"
```

Check the persisted always-on session:

```bash
python -m spst_runtime.chat_bridge --status
```

Run local maintenance:

```bash
python -m spst_runtime.maintenance
```

In this thread, requests addressed to SPST-UEA can be routed through that bridge by Codex.

After `python -m pip install -e ".[dev]"`, the console script is also available:

```bash
spst-runtime --steps 3 --event codex_run
```
