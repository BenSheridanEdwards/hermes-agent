from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONCRETE_SECRET = re.compile(
    r"gh[pousr]_[A-Za-z0-9_]{30,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def test_packaged_skill_docs_do_not_ship_concrete_secret_shaped_examples() -> None:
    offenders: list[str] = []
    for markdown_path in sorted((ROOT / "skills").rglob("*.md")):
        for line_number, line in enumerate(
            markdown_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if CONCRETE_SECRET.search(line):
                offenders.append(f"{markdown_path.relative_to(ROOT)}:{line_number}")

    assert offenders == []
