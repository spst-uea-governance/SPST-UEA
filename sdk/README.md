# SPST-UEA SDK

The Python SDK lives in `sdk/python` and provides a typed local cockpit client.
It is intentionally API-key-free and works with an in-process cockpit contract:

```python
from spst_uea_sdk import LocalRuntimeClient
from spst_runtime.web import CockpitRuntime

client = LocalRuntimeClient(CockpitRuntime())
result = client.dispatch(prompt="Inspect local runtime health.")
```

Install it after installing the runtime package:

```powershell
python -m pip install -e runtime
python -m pip install -e sdk/python
```
