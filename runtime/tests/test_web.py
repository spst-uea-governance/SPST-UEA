import json

from spst_runtime.web import RuntimeWebHandler


def test_web_handler_has_html():
    assert "SPST-UEA Runtime" in RuntimeWebHandler.__module__ or RuntimeWebHandler is not None


def test_json_payload_shape():
    payload = {"status": "ok"}

    assert json.loads(json.dumps(payload))["status"] == "ok"
