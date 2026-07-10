from spst_runtime.always_on import CONFIG


def test_always_on_defaults_to_no_key_codex_mediated_mode():
    assert CONFIG.mode == "codex-chat-mediated"
    assert CONFIG.provider == "codex-mediated-local"
    assert CONFIG.requires_api_key is False
    assert CONFIG.default_event == "chat_turn"
    assert CONFIG.target_esi == 0.95
