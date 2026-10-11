"""API Lens MCP server.

Exposes OpenAPI design review as MCP *tools* -- functions a model can
discover and invoke through the Model Context Protocol:

- ``review_openapi`` -- run the deterministic check suite against an
  OpenAPI 3.x spec (inline text or URL) and return structured findings.
- ``list_checks`` -- return the catalog of checks with one-line explanations.

Run locally::

    python server.py                 # streamable HTTP on 0.0.0.0:8000
    PORT=9000 python server.py       # custom port

The server speaks streamable HTTP so it can later sit behind an HTTPS
reverse proxy / host (Fly.io, Railway, etc.) and be registered as a remote
MCP server or ChatGPT app connector.
"""

from __future__ import annotations

import os

# Set before importing fastmcp: its settings are read from the environment at
# import time. The startup banner phones home to check for updates; disable
# that by default so a broken proxy config in the host environment can never
# prevent the server from starting. Override with
# FASTMCP_CHECK_FOR_UPDATES=stable if you want the check.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

import re
from typing import Any, Dict, Tuple

import httpx
import yaml
from fastmcp import FastMCP

from checks import list_check_catalog, run_all_checks

# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="api-lens",
    instructions=(
        "API Lens reviews OpenAPI 3.x API designs for common flaws. "
        "Use review_openapi to analyze a spec supplied as YAML/JSON text or an "
        "http(s) URL. Use list_checks to see what the reviewer looks for."
    ),
)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_FETCH_TIMEOUT = 15.0
_MAX_SPEC_BYTES = 1_000_000  # 1 MB sanity cap on fetched specs


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------

def _fetch_spec(url: str) -> Tuple[Dict[str, Any], str]:
    """Download an OpenAPI document from an http(s) URL and parse it.

    Uses ``trust_env=False`` so ambient proxy variables in the host
    environment cannot interfere with (or silently reroute) the fetch.
    """
    try:
        with httpx.Client(
            timeout=_FETCH_TIMEOUT, follow_redirects=True, trust_env=False
        ) as client:
            response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not fetch spec from {url}: {exc}") from exc
    if len(response.content) > _MAX_SPEC_BYTES:
        raise ValueError(
            f"Spec at {url} exceeds the {_MAX_SPEC_BYTES}-byte size limit."
        )
    return _parse_spec_text(response.text), f"url:{url}"


def _parse_spec_text(text: str) -> Dict[str, Any]:
    """Parse OpenAPI YAML or JSON text into a dict.

    JSON is a subset of YAML, so ``yaml.safe_load`` handles both; we try
    ``json`` first only to produce a clearer error for malformed JSON.
    """
    import json

    text = text.strip()
    if not text:
        raise ValueError("Empty spec: provide OpenAPI YAML/JSON text or a URL.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"Spec is not valid YAML or JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Spec must parse to a mapping (YAML/JSON object).")
    return data


def _load_spec(spec: str) -> Tuple[Dict[str, Any], str]:
    """Load a spec from inline text or a URL; validate it is OpenAPI 3.x."""
    spec = spec.strip()
    if _URL_RE.match(spec):
        # A bare URL on one line: fetch it. (Anything else is treated as
        # inline spec text, even if it merely mentions a URL.)
        if "\n" not in spec:
            data, source = _fetch_spec(spec)
        else:
            data, source = _parse_spec_text(spec), "inline"
    else:
        data, source = _parse_spec_text(spec), "inline"

    openapi_version = str(data.get("openapi", ""))
    if not openapi_version.startswith("3."):
        raise ValueError(
            "Not an OpenAPI 3.x document: missing or unsupported "
            f"'openapi' version field (got {openapi_version!r})."
        )
    return data, source


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool
def review_openapi(spec: str) -> Dict[str, Any]:
    """Review an OpenAPI 3.x API design for common flaws.

    Args:
        spec: The OpenAPI document, either as YAML/JSON text or as an
            http(s) URL pointing at the raw spec file.

    Returns a dict with a severity summary and the list of findings. Each
    finding has check_id, severity ("error"/"warning"/"info"), category,
    message, location, and suggestion fields.
    """
    try:
        data, source = _load_spec(spec)
    except ValueError as exc:
        return {"error": str(exc), "summary": None, "findings": []}

    findings = run_all_checks(data)
    severities = [f["severity"] for f in findings]
    operations = [
        1
        for paths in [data.get("paths") or {}]
        for item in paths.values()
        if isinstance(item, dict)
        for method in item
        if method.lower()
        in {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    ]

    return {
        "summary": {
            "total_findings": len(findings),
            "errors": severities.count("error"),
            "warnings": severities.count("warning"),
            "info": severities.count("info"),
            "operations_reviewed": len(operations),
            "checks_run": 10,
        },
        "meta": {"source": source, "openapi_version": str(data.get("openapi"))},
        "findings": findings,
    }


@mcp.tool
def list_checks() -> Dict[str, Any]:
    """List every design check API Lens can run, with a one-line explanation
    of what each check looks for."""
    return {"checks": list_check_catalog()}


# ---------------------------------------------------------------------------
# Entrypoint: streamable HTTP, PORT from env (default 8000)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
