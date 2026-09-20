"""Execute CI gate scripts with synthetic results and an offline gh command."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None,
    reason="These workflow scripts run under bash on Ubuntu hosted runners",
)


def workflow_script(filename, job, step_name):
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
    )
    return next(
        step["run"] for step in workflow["jobs"][job]["steps"]
        if step.get("name") == step_name
    )


def run_script(script, environment, directory):
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=directory,
        env={"PATH": os.environ["PATH"], **environment},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )


@pytest.mark.parametrize(
    "results,expected_success",
    [
        ({"detect": "success", "tests": "success"}, True),
        ({"detect": "success", "tests": "skipped", "lint": "success"}, True),
        ({"detect": "cancelled", "tests": "skipped"}, False),
        ({"detect": "skipped", "tests": "skipped"}, False),
        ({"detect": "success", "tests": "cancelled"}, False),
        ({"detect": "success", "tests": "failure"}, False),
        ({"detect": "success", "tests": "unknown"}, False),
    ],
)
def test_aggregate_requires_successful_detection_and_no_incomplete_lanes(
    tmp_path, results, expected_success,
):
    output = tmp_path / "github-output"
    result = run_script(
        workflow_script("ci.yaml", "all-checks-pass", "Evaluate job results"),
        {
            "NEEDS": json.dumps({name: {"result": state} for name, state in results.items()}),
            "GITHUB_OUTPUT": str(output),
        },
        tmp_path,
    )
    assert (result.returncode == 0) == expected_success, result.stdout + result.stderr
    assert json.loads(output.read_text(encoding="utf-8").split("=", 1)[1]) == results
    if not expected_success:
        assert "::error::" in result.stdout


@pytest.fixture
def fake_gh(tmp_path):
    executable = tmp_path / "gh"
    executable.write_text(
        """#!/bin/bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$1 $2" in
  'run list') printf '123 %s\\n' "$TEST_RUN_STATUS" ;;
  'run watch') exit 1 ;;
  'api repos/example/project/pulls/7') printf '%s\\n' "$TEST_CURRENT_HEAD" ;;
  'run view')
    case "$*" in
      *'--json status'*) printf 'completed\\n' ;;
      *'--json conclusion'*)
        if [ "$TEST_CONCLUSION" = 'lookup-error' ]; then exit 1; fi
        printf '%s\\n' "$TEST_CONCLUSION" ;;
      *) exit 90 ;;
    esac ;;
  'run rerun') exit 0 ;;
  *) exit 91 ;;
esac
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    timeout = tmp_path / "timeout"
    timeout.write_text('#!/bin/bash\nshift\nexec "$@"\n', encoding="utf-8")
    timeout.chmod(0o700)
    return tmp_path / "gh-calls"


@pytest.mark.parametrize(
    "status,current_head,conclusion,should_rerun",
    [
        ("completed", "original-head", "failure", True),
        ("in_progress", "original-head", "failure", True),
        ("in_progress", "new-head", "failure", False),
        ("completed", "original-head", "cancelled", False),
        ("completed", "original-head", "success", False),
        ("completed", "", "failure", False),
        ("completed", "original-head", "lookup-error", False),
    ],
)
def test_label_rerun_checks_current_head_and_run_conclusion(
    tmp_path, fake_gh, status, current_head, conclusion, should_rerun,
):
    result = run_script(
        workflow_script(
            "label-rerun.yml", "rerun-review-labels",
            "Wait for CI run to finish, then rerun failed jobs",
        ),
        {
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "REPO": "example/project",
            "PR_NUMBER": "7",
            "HEAD_SHA": "original-head",
            "GH_CALLS": str(fake_gh),
            "TEST_RUN_STATUS": status,
            "TEST_CURRENT_HEAD": current_head,
            "TEST_CONCLUSION": conclusion,
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = fake_gh.read_text(encoding="utf-8").splitlines()
    reruns = [call for call in calls if call.startswith("run rerun ")]
    assert reruns == (["run rerun 123 --repo example/project --failed"] if should_rerun else [])
    if should_rerun:
        assert calls.index("api repos/example/project/pulls/7 --jq .head.sha") < len(calls) - 1
        assert calls[-2] == "run view 123 --repo example/project --json conclusion --jq .conclusion"
