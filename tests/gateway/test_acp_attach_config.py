from gateway.config import GatewayConfig
from hermes_cli.config import DEFAULT_CONFIG


def test_acp_opt_in_is_literal_boolean_and_survives_gateway_roundtrip():
    assert DEFAULT_CONFIG["gateway"]["acp"]["enabled"] is False
    for value in (None, False, "false", "true", 1, {}, []):
        assert not GatewayConfig.from_dict({"gateway": {"acp": {"enabled": value}}}).acp_enabled
    enabled = GatewayConfig.from_dict({"gateway": {"acp": {"enabled": True}}})
    assert enabled.acp_enabled
    assert GatewayConfig.from_dict(enabled.to_dict()).acp_enabled
