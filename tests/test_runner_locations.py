"""Observe the canonical child's actual scratch/cache and guard contract.

Run through scripts/run_tests.sh. Repository guards always apply; the external
home plugin is optional and must load normally when present.
"""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


_HOME_AT_IMPORT = os.environ["HOME"]


def test_canonical_child_locations_and_existing_guards(tmp_path, request, capsys):
    assert os.environ["HOME"] == _HOME_AT_IMPORT
    assert {"_hermetic_environment", "_kanban_write_guard"} <= set(request.fixturenames)
    assert os.environ.get("HERMES_TEST_ISOLATION") == os.environ["HERMES_HOME"]
    assert Path(os.environ["HERMES_HOME"]).resolve().is_relative_to(tmp_path.resolve())

    scratch = Path(tempfile.gettempdir()).resolve()
    # Optional smoke evidence supplied BEFORE invocation by the calling process,
    # not a second child-side reading of HOME masquerading as parent identity.
    parent_record = scratch / "runner-parent.json"
    if parent_record.is_file():
        assert os.environ["HOME"] == json.loads(parent_record.read_text(encoding="utf-8"))["home"]
    if os.environ.get("TMPDIR"):
        assert scratch == Path(os.environ["TMPDIR"]).resolve()
    assert tmp_path.resolve().is_relative_to(scratch)
    if os.environ.get("PYTHONPYCACHEPREFIX"):
        assert sys.pycache_prefix is not None
        assert Path(sys.pycache_prefix).resolve() == Path(os.environ["PYTHONPYCACHEPREFIX"]).resolve()
    if os.environ.get("PYTHONDONTWRITEBYTECODE"):
        assert sys.dont_write_bytecode

    source = tmp_path / "cache_destination_probe.py"
    source.write_text("VALUE = 17\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("cache_destination_probe", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.VALUE == 17
    bytecode = Path(importlib.util.cache_from_source(str(source)))
    assert bytecode.exists() == (not sys.dont_write_bytecode)
    if sys.pycache_prefix:
        assert bytecode.resolve().is_relative_to(Path(sys.pycache_prefix).resolve())

    external_guard = Path(_HOME_AT_IMPORT) / ".hermes" / "pytest_live_guard.py"
    if external_guard.is_file():
        assert request.config.pluginmanager.hasplugin("pytest_live_guard")
    observation = {
        "home": _HOME_AT_IMPORT, "tempfile": str(scratch),
        "fixture_home": os.environ["HERMES_HOME"],
        "pycache_prefix": sys.pycache_prefix,
        "dont_write_bytecode": sys.dont_write_bytecode,
        "bytecode_destination": str(bytecode), "bytecode_written": bytecode.exists(),
        "repository_guards": True,
        "external_guard_present": external_guard.is_file(),
        "external_guard_loaded": request.config.pluginmanager.hasplugin("pytest_live_guard"),
        "parent_identity_bound": parent_record.is_file(),
    }
    if parent_record.is_file():
        mode = "no-bytecode" if sys.dont_write_bytecode else "cache-enabled"
        (scratch / f"runner-observation-{mode}.json").write_text(json.dumps(observation), encoding="utf-8")
    with capsys.disabled():
        print(json.dumps(observation, sort_keys=True))


def test_default_precompile_and_no_bytecode_contract_on_inert_source(tmp_path):
    """Exercise default precompile without writing caches beside real source/deps.

    Only the shell's final dispatcher is an inert recorder in this fixture;
    the other test and readiness smoke exercise the actual canonical dispatcher.
    -B protects the shared interpreter while explicit compileall still writes
    its real default cache for the one tracked, task-local source file.
    """
    root = tmp_path / "runner-fixture"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/run_tests.sh", scripts / "run_tests.sh")
    (scripts / "run_tests_parallel.py").write_text(
        "import os, sys\nprint('INERT_DISPATCH', sys.dont_write_bytecode, os.environ['HOME'])\n",
        encoding="utf-8",
    )
    source = root / "inert.py"
    source.write_text("VALUE = 17\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "inert.py"], check=True)
    interpreter = root / "python"
    interpreter.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -B \"$@\"\n", encoding="utf-8")
    interpreter.chmod(0o700)
    # Preserve HOME and optional guard discovery. No broad caller env inheritance.
    env = {"PATH": os.environ["PATH"], "HOME": _HOME_AT_IMPORT,
           "HERMES_PYTHON": str(interpreter), "TMPDIR": str(tmp_path)}
    cache = root / "__pycache__"
    for dont_write in (False, True):
        shutil.rmtree(cache, ignore_errors=True)
        env["PYTHONDONTWRITEBYTECODE"] = "1" if dont_write else ""
        result = subprocess.run(["bash", str(scripts / "run_tests.sh")], env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "INERT_DISPATCH" in result.stdout
        assert ("pre-compiling bytecode cache" in result.stdout) == (not dont_write)
        assert bool(list(cache.glob("inert.*.pyc"))) == (not dont_write)
    # Invalid selected locations fail before Python probes or default fallback.
    for variable in ("TMPDIR", "PYTHONPYCACHEPREFIX"):
        for location in ("relative", str(root / "does-not-exist")):
            result = subprocess.run(["bash", str(scripts / "run_tests.sh")],
                                    env={**env, variable: location}, capture_output=True,
                                    text=True, timeout=30)
            assert result.returncode == 2
            assert f"error: {variable} must be" in result.stderr
            assert "INERT_DISPATCH" not in result.stdout
