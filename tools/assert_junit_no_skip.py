from __future__ import annotations

import sys
from pathlib import Path

from defusedxml import ElementTree


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_junit_no_skip.py <junit.xml>")
    root = ElementTree.parse(Path(sys.argv[1])).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    totals = {
        field: sum(int(suite.attrib.get(field, "0")) for suite in suites)
        for field in ("tests", "failures", "errors", "skipped")
    }
    if totals["tests"] < 1 or any(totals[field] for field in ("failures", "errors", "skipped")):
        raise SystemExit(f"JUnit gate failed: {totals}")
    print(f"JUnit gate passed: {totals['tests']} tests, no failure/error/skip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
