import sys
from pathlib import Path

from spst_runtime.web import CockpitRuntime


def test_python_sdk_distribution_exports_local_client():
    source_root = Path(__file__).resolve().parents[2] / "sdk" / "python" / "src"
    sys.path.insert(0, str(source_root))
    try:
        from spst_uea_sdk import LocalRuntimeClient
    finally:
        sys.path.remove(str(source_root))

    client = LocalRuntimeClient(CockpitRuntime())

    assert client.status()["requires_api_key"] is False
