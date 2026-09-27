from gateway.config import GatewayConfig
from hermes_cli.config import DEFAULT_CONFIG


def test_acp_opt_in_is_literal_boolean_and_survives_gateway_roundtrip():
    assert DEFAULT_CONFIG["gateway"]["acp"]["enabled"] is False
    for value in (None, False, "false", "true", 1, {}, []):
        assert not GatewayConfig.from_dict({"gateway": {"acp": {"enabled": value}}}).acp_enabled
    enabled = GatewayConfig.from_dict({"gateway": {"acp": {"enabled": True}}})
    assert enabled.acp_enabled
    assert GatewayConfig.from_dict(enabled.to_dict()).acp_enabled


def test_acp_opt_in_reaches_gateway_through_real_config_yaml(monkeypatch, tmp_path):
    # Real startup builds gw_data flat from config.yaml; an unbridged nested key is silently dropped.
    from gateway.config import load_gateway_config

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("gateway:\n  acp:\n    enabled: true\n")
    assert load_gateway_config().acp_enabled is True
    (hermes_home / "config.yaml").write_text("gateway:\n  acp:\n    enabled: false\n")
    assert load_gateway_config().acp_enabled is False
    # Only the nested gateway form opts in; a top-level ``acp:`` block is not this gateway's switch.
    (hermes_home / "config.yaml").write_text("acp:\n  enabled: true\n")
    assert load_gateway_config().acp_enabled is False
