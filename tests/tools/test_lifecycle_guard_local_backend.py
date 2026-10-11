"""Local terminal preflight must not spend remote reads on JS documentation.

All inputs are inert fixtures. Tests call the preflight, never a shell, Node,
ESLint, or a gateway lifecycle operation.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.terminal_tool_guards import gateway_lifecycle_block


@pytest.mark.require_symlinks
def test_local_eslint_declaration_chain_never_uses_backend_reader(monkeypatch, tmp_path):
    """Reproduce #105758's ESLint -> package -> declarations -> 64 URL misses."""
    package = tmp_path / "node_modules" / "eslint"
    (package / "bin").mkdir(parents=True)
    (package / "lib" / "types").mkdir(parents=True)
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir()
    (bin_dir / "eslint").symlink_to(package / "bin" / "eslint.js")
    (package / "bin" / "eslint.js").write_text(
        '#!/usr/bin/env node\nrequire("../package.json");\n', encoding="utf-8"
    )
    (package / "package.json").write_text(
        json.dumps({"typesVersions": {"*": {"*": ["lib/types/rules.d.ts"]}}}, indent=2),
        encoding="utf-8",
    )
    (package / "lib" / "types" / "rules.d.ts").write_text(
        "/**\n" + "".join(
            f" * [rule {i}](https://example.invalid/rules/rule-{i})\n" for i in range(80)
        ) + " */\nexport interface Rules {}\n",
        encoding="utf-8",
    )
    # The backend callback is the seam under test. It records missing-file
    # reads but must never execute the supplied command or any fixture.
    execute = Mock(return_value={"output": "", "returncode": 1})
    env = SimpleNamespace(cwd=str(tmp_path), execute=execute)
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd", lambda _: None)

    result = gateway_lifecycle_block(
        command="env CI=true /bin/bash -c './node_modules/.bin/eslint target.ts --fix'",
        env=env,
        env_type="local",
        cwd=str(tmp_path),
        workdir=None,
        session_key="local-eslint-scan-regression",
    )

    assert result is None, result
    execute.assert_not_called()
