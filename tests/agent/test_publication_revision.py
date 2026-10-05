"""Publication v2 reader accepts committed revisions and stays backward compatible with v1."""
import json
import pytest

from agent.credential_policy import CredentialPolicyError, load_policy


def setup(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    policy = home / "assignments.json"
    gate = tmp_path / "authority-publication.json"
    home.joinpath("config.yaml").write_text(
        f"credential_policy:\n  file: {policy}\n", encoding="utf-8"
    )
    data = {
        "version": 1,
        "environment": {},
        "managed_environment": ["REVOKED_KEY"],
        "accounts": {},
        "publication": {"file": str(gate), "revision": 1},
    }
    policy.write_text(json.dumps(data), encoding="utf-8")
    gate.write_text(
        json.dumps({"version": 1, "revision": 1, "state": "committed"}), encoding="utf-8"
    )
    return home, policy, gate, data


def test_legacy_v1_without_publication_still_loads(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    policy = home / "assignments.json"
    home.joinpath("config.yaml").write_text(
        f"credential_policy:\n  file: {policy}\n", encoding="utf-8"
    )
    policy.write_text(
        json.dumps(
            {
                "version": 1,
                "environment": {"GITHUB_TOKEN": "fleet_access"},
                "managed_environment": ["GITHUB_TOKEN"],
                "accounts": {},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_policy(home)
    assert loaded is not None
    assert "GITHUB_TOKEN" in loaded.managed_environment


@pytest.mark.parametrize(
    "state",
    [
        None,
        [],
        {"version": 1, "revision": 1, "state": "publishing"},
        {"version": 1, "revision": 2, "state": "committed"},
    ],
)
def test_publication_stages_never_fall_back_to_an_older_grant(tmp_path, state):
    home, policy, gate, data = setup(tmp_path)
    assert load_policy(home) is not None
    if state is None:
        gate.unlink()
    else:
        gate.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    assert json.loads(policy.read_text(encoding="utf-8")) == data


def test_revision_two_requires_coherent_republication_and_retains_revocations(tmp_path):
    home, policy, gate, data = setup(tmp_path)
    original = load_policy(home)
    gate.write_text(
        json.dumps({"version": 1, "revision": 2, "state": "publishing"}), encoding="utf-8"
    )
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    data["version"] = 2
    data["publication"]["revision"] = 2
    policy.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    gate.write_text(
        json.dumps({"version": 1, "revision": 2, "state": "committed"}), encoding="utf-8"
    )
    current = load_policy(home)
    assert original is not None and current is not None
    assert current.revision != original.revision
    assert "REVOKED_KEY" in current.managed_environment


def test_v2_without_publication_is_rejected(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    policy = home / "assignments.json"
    home.joinpath("config.yaml").write_text(
        f"credential_policy:\n  file: {policy}\n", encoding="utf-8"
    )
    policy.write_text(
        json.dumps(
            {
                "version": 2,
                "environment": {},
                "managed_environment": [],
                "accounts": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CredentialPolicyError):
        load_policy(home)


def test_assignment_under_assignments_dir_must_point_at_owner_gate(tmp_path):
    root = tmp_path / "access"
    home = tmp_path / "profile"
    home.mkdir()
    assignments = root / "assignments"
    assignments.mkdir(parents=True)
    policy = assignments / "agent.json"
    gate = root / "authority-publication.json"
    foreign = tmp_path / "foreign-gate.json"
    home.joinpath("config.yaml").write_text(
        f"credential_policy:\n  file: {policy}\n", encoding="utf-8"
    )
    foreign.write_text(
        json.dumps({"version": 1, "revision": 1, "state": "committed"}), encoding="utf-8"
    )
    gate.write_text(
        json.dumps({"version": 1, "revision": 1, "state": "committed"}), encoding="utf-8"
    )
    policy.write_text(
        json.dumps(
            {
                "version": 2,
                "environment": {},
                "managed_environment": [],
                "accounts": {},
                "publication": {"file": str(foreign), "revision": 1},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CredentialPolicyError):
        load_policy(home)
    policy.write_text(
        json.dumps(
            {
                "version": 2,
                "environment": {},
                "managed_environment": [],
                "accounts": {},
                "publication": {"file": str(gate), "revision": 1},
            }
        ),
        encoding="utf-8",
    )
    assert load_policy(home) is not None


def test_capabilities_advertise_publication_v2(capsys):
    from agent.credential_policy import capabilities_command

    capabilities_command(None)
    assert json.loads(capsys.readouterr().out)["credential_policy"] == 2
