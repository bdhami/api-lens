"""Eval harness for API Lens.

Runs the deterministic check suite against fixture specs with *known*
expectations and reports which expected findings were caught vs missed, plus
any unexpected findings (false positives).

Fixtures live in ``evals/specs/``:
- clean.yaml    -- well-designed API; expect zero findings
- messy.yaml    -- deliberately bad API; expect all 10 checks to fire
- minimal.json  -- edge case: smallest valid OpenAPI 3.x doc
- remote.yaml   -- served over local HTTP to exercise the spec-as-URL path

Usage:
    python evals/run_evals.py        # from the project root

Exit code is 0 only if every expectation holds.
"""

from __future__ import annotations

import functools
import http.server
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from checks import run_all_checks  # noqa: E402
from server import _load_spec  # noqa: E402  (exercises the URL fetch path)

SPECS_DIR = ROOT / "evals" / "specs"

ALL_CHECK_IDS = {
    "info-version-missing",
    "servers-missing",
    "operation-id-missing",
    "error-responses-missing",
    "rate-limit-response-missing",
    "naming-inconsistent",
    "security-schemes-missing",
    "operation-security-missing",
    "pagination-missing",
    "version-prefix-missing",
}

# check_ids we expect to fire (at least once) per fixture.
EXPECTATIONS = {
    "clean.yaml": set(),
    "messy.yaml": set(ALL_CHECK_IDS),
    "minimal.json": {
        "servers-missing",
        "operation-id-missing",
        "error-responses-missing",
        "rate-limit-response-missing",
        "security-schemes-missing",
        "operation-security-missing",
        "version-prefix-missing",
    },
    "remote.yaml": {
        "info-version-missing",
        "servers-missing",
    },
}


def _serve_specs_locally() -> tuple[http.server.HTTPServer, int]:
    """Serve evals/specs over HTTP on 127.0.0.1 (ephemeral port)."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(SPECS_DIR)
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _evaluate(name: str, spec_input: str) -> tuple[bool, set[str], set[str], set[str]]:
    """Run checks; return (passed, expected, missed, unexpected)."""
    data, _source = _load_spec(spec_input)
    fired = {f["check_id"] for f in run_all_checks(data)}
    expected = EXPECTATIONS[name]
    missed = expected - fired
    unexpected = fired - expected
    return not missed and not unexpected, expected, missed, unexpected


def main() -> int:
    http_server, port = _serve_specs_locally()
    failures = 0

    print("API Lens evals")
    print("=" * 60)
    try:
        for name in EXPECTATIONS:
            if name == "remote.yaml":
                # Exercise the spec-as-URL code path end to end.
                spec_input = f"http://127.0.0.1:{port}/{name}"
            else:
                spec_input = (SPECS_DIR / name).read_text()

            passed, expected, missed, unexpected = _evaluate(name, spec_input)
            status = "PASS" if passed else "FAIL"
            print(f"\n[{status}] {name}")
            print(f"  expected to fire : {sorted(expected) or 'none'}")
            if missed:
                print(f"  MISSED           : {sorted(missed)}")
            if unexpected:
                print(f"  UNEXPECTED       : {sorted(unexpected)}")
            if not passed:
                failures += 1
    finally:
        http_server.shutdown()

    print("\n" + "=" * 60)
    total = len(EXPECTATIONS)
    print(f"{total - failures}/{total} fixtures passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
