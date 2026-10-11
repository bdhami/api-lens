"""Deterministic OpenAPI 3.x design checks for API Lens.

Each check inspects a parsed OpenAPI document (a plain ``dict``) and returns a
list of findings. Findings are plain dicts so they serialize cleanly over MCP
as tool output::

    {
        "check_id":  str,   # stable id, e.g. "operation-id-missing"
        "severity":  str,   # "error" | "warning" | "info"
        "category":  str,   # grouping, e.g. "security", "naming"
        "message":   str,   # one-line human-readable description
        "location":  str,   # JSON-path-ish pointer, e.g. "paths./users.get"
        "suggestion": str,  # actionable remediation advice
    }

Checks are intentionally *deterministic* (no LLM calls): they encode API
design conventions the way a linter encodes style rules. This keeps results
reproducible, cheap, and easy to evaluate -- see ``evals/run_evals.py``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterator, List, Tuple

Spec = Dict[str, Any]
Finding = Dict[str, Any]
CheckFn = Callable[[Spec], List[Finding]]

HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

# Query parameter names that conventionally implement pagination.
PAGINATION_PARAMS = {
    "limit",
    "offset",
    "page",
    "per_page",
    "page_size",
    "pagesize",
    "cursor",
    "page_token",
    "next_token",
}

_VERSION_SEGMENT = re.compile(r"^v\d+$")          # e.g. v1, v2, v10
_CAMEL_CASE = re.compile(r"[a-z][A-Z]")          # e.g. userId, orderItems


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(
    check_id: str,
    severity: str,
    category: str,
    message: str,
    location: str,
    suggestion: str,
) -> Finding:
    """Build a single finding dict in the canonical shape."""
    return {
        "check_id": check_id,
        "severity": severity,
        "category": category,
        "message": message,
        "location": location,
        "suggestion": suggestion,
    }


def _iter_operations(spec: Spec) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
    """Yield (path, method, operation) for every operation in the spec."""
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() in HTTP_METHODS and isinstance(operation, dict):
                yield path, method.lower(), operation


def _is_plural_noun(segment: str) -> bool:
    """Heuristic: the last path segment looks like a plural collection noun.

    Matches ``users``, ``order_items``; avoids false positives on words like
    ``status`` or ``address``. Path parameters (``{id}``) never count.
    """
    seg = segment.strip().lower()
    if not seg or seg.startswith("{"):
        return False
    return seg.endswith("s") and not seg.endswith("ss") and len(seg) > 1


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_info_version(spec: Spec) -> List[Finding]:
    """Every API should declare an info.version so clients can pin behavior."""
    info = spec.get("info") or {}
    version = info.get("version")
    if not version or not str(version).strip():
        return [
            _finding(
                "info-version-missing",
                "error",
                "metadata",
                "info.version is missing: the API declares no version.",
                "info",
                "Set info.version (e.g. \"1.0.0\") and keep it in sync with "
                "the versioned path prefix (e.g. /v1).",
            )
        ]
    return []


def check_servers(spec: Spec) -> List[Finding]:
    """A spec with no servers leaves the base URL ambiguous for codegen."""
    servers = spec.get("servers")
    if not servers:
        return [
            _finding(
                "servers-missing",
                "warning",
                "metadata",
                "No servers are defined: clients cannot tell where the API lives.",
                "servers",
                "Add at least one server entry, e.g. "
                "{\"url\": \"https://api.example.com/v1\"}.",
            )
        ]
    return []


def check_operation_ids(spec: Spec) -> List[Finding]:
    """operationId is required by most code generators and SDK tooling."""
    findings = []
    for path, method, operation in _iter_operations(spec):
        if not operation.get("operationId"):
            findings.append(
                _finding(
                    "operation-id-missing",
                    "error",
                    "operations",
                    f"{method.upper()} {path} has no operationId.",
                    f"paths.{path}.{method}",
                    "Add a unique, verb-led operationId such as "
                    "\"listUsers\" or \"createOrder\".",
                )
            )
    return findings


def check_error_responses(spec: Spec) -> List[Finding]:
    """Operations should document 4xx/5xx responses, not just the happy path.

    A ``default`` response is treated as covering error cases, since it is the
    conventional OpenAPI way to describe the error envelope.
    """
    findings = []
    for path, method, operation in _iter_operations(spec):
        responses = operation.get("responses") or {}
        keys = [str(k) for k in responses.keys()]
        has_error = any(k == "default" or k[:1] in ("4", "5") for k in keys)
        if not has_error:
            findings.append(
                _finding(
                    "error-responses-missing",
                    "warning",
                    "reliability",
                    f"{method.upper()} {path} documents no 4xx/5xx error responses.",
                    f"paths.{path}.{method}.responses",
                    "Document at least a 400 and a 500 response (or a "
                    "\"default\" error envelope) so clients know how "
                    "failures are shaped.",
                )
            )
    return findings


def check_rate_limit_response(spec: Spec) -> List[Finding]:
    """Clients that respect 429 can back off gracefully instead of failing."""
    findings = []
    for path, method, operation in _iter_operations(spec):
        responses = operation.get("responses") or {}
        if "429" not in {str(k) for k in responses.keys()}:
            findings.append(
                _finding(
                    "rate-limit-response-missing",
                    "info",
                    "reliability",
                    f"{method.upper()} {path} does not document a 429 response.",
                    f"paths.{path}.{method}.responses",
                    "If the API rate-limits, document 429 with the "
                    "Retry-After header so clients can back off correctly.",
                )
            )
    return findings


def check_naming_consistency(spec: Spec) -> List[Finding]:
    """Path segments should stick to one casing convention (snake_case *or*
    camelCase); mixing them signals an unreviewed surface."""
    seen_snake, seen_camel = False, False
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return []
    for path in paths:
        for segment in path.split("/"):
            if not segment or segment.startswith("{"):
                continue
            if _VERSION_SEGMENT.match(segment):
                continue
            if "_" in segment and segment == segment.lower():
                seen_snake = True
            elif _CAMEL_CASE.search(segment):
                seen_camel = True
    if seen_snake and seen_camel:
        return [
            _finding(
                "naming-inconsistent",
                "warning",
                "naming",
                "Path segments mix snake_case and camelCase conventions.",
                "paths",
                "Pick one casing convention for resource names and apply it "
                "everywhere (snake_case is the most common for REST).",
            )
        ]
    return []


def check_security_schemes(spec: Spec) -> List[Finding]:
    """Without securitySchemes, auth requirements cannot be expressed at all."""
    schemes = (spec.get("components") or {}).get("securitySchemes")
    if not schemes:
        return [
            _finding(
                "security-schemes-missing",
                "error",
                "security",
                "No components.securitySchemes are defined.",
                "components.securitySchemes",
                "Define at least one scheme (e.g. http bearer, apiKey, or "
                "oauth2) so operations can declare their auth requirements.",
            )
        ]
    return []


def check_operation_security(spec: Spec) -> List[Finding]:
    """Flag operations that end up with no security requirement.

    Resolution order per the OpenAPI spec: an operation-level ``security``
    overrides the top-level ``security``. An explicit empty list (``[]``) is
    treated as a deliberate opt-out and is not flagged.
    """
    findings = []
    global_security = spec.get("security")
    for path, method, operation in _iter_operations(spec):
        if "security" in operation:
            secured = bool(operation["security"])  # [] = deliberate opt-out
        else:
            secured = bool(global_security)
        if not secured:
            findings.append(
                _finding(
                    "operation-security-missing",
                    "warning",
                    "security",
                    f"{method.upper()} {path} has no security requirement.",
                    f"paths.{path}.{method}",
                    "Add a security requirement, or set \"security\": [] "
                    "explicitly if the endpoint is intentionally public.",
                )
            )
    return findings


def check_pagination(spec: Spec) -> List[Finding]:
    """Collection GETs (plural-noun paths) should accept pagination params,
    otherwise clients are forced to pull unbounded result sets."""
    findings = []
    for path, method, operation in _iter_operations(spec):
        if method != "get":
            continue
        segments = [s for s in path.split("/") if s]
        if not segments or not _is_plural_noun(segments[-1]):
            continue
        params = operation.get("parameters") or []
        names = {
            str(p.get("name", "")).lower()
            for p in params
            if isinstance(p, dict) and p.get("in") == "query"
        }
        if not (names & PAGINATION_PARAMS):
            findings.append(
                _finding(
                    "pagination-missing",
                    "info",
                    "design",
                    f"GET {path} looks like a collection endpoint but accepts "
                    "no pagination parameters.",
                    f"paths.{path}.get.parameters",
                    "Add limit/offset or cursor-based pagination (e.g. "
                    "\"limit\" and \"page_token\" query parameters).",
                )
            )
    return findings


def check_version_prefix(spec: Spec) -> List[Finding]:
    """Versioned path prefixes (/v1, /v2) let an API evolve without breaking
    existing clients."""
    findings = []
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return []
    for path in paths:
        segments = [s for s in path.split("/") if s]
        if not segments:
            continue
        if not _VERSION_SEGMENT.match(segments[0]):
            findings.append(
                _finding(
                    "version-prefix-missing",
                    "warning",
                    "versioning",
                    f"Path {path} has no version prefix (e.g. /v1).",
                    f"paths.{path}",
                    "Prefix paths with a major version (\"/v1/...\") so "
                    "breaking changes can ship as /v2 without disturbing "
                    "existing integrations.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: The full check catalog: (check_id, title, one-line description, fn).
#: ``list_checks`` exposes this to MCP clients.
CHECKS: List[Tuple[str, str, str, CheckFn]] = [
    (
        "info-version-missing",
        "Missing info.version",
        "The API declares no version in info.version, so clients cannot pin behavior.",
        check_info_version,
    ),
    (
        "servers-missing",
        "No servers defined",
        "No servers array means the API's base URL is ambiguous for clients and codegen.",
        check_servers,
    ),
    (
        "operation-id-missing",
        "Missing operationId",
        "Operations without operationId break most SDK generators and client tooling.",
        check_operation_ids,
    ),
    (
        "error-responses-missing",
        "No error responses documented",
        "Operations that only document 2xx responses leave clients guessing how failures are shaped.",
        check_error_responses,
    ),
    (
        "rate-limit-response-missing",
        "No 429 response documented",
        "Without a documented 429, clients cannot implement correct backoff on rate limits.",
        check_rate_limit_response,
    ),
    (
        "naming-inconsistent",
        "Inconsistent path naming",
        "Mixing snake_case and camelCase path segments signals an unreviewed API surface.",
        check_naming_consistency,
    ),
    (
        "security-schemes-missing",
        "No securitySchemes defined",
        "Without securitySchemes, the spec cannot express any authentication requirements.",
        check_security_schemes,
    ),
    (
        "operation-security-missing",
        "Operation has no security requirement",
        "Operations with no effective security requirement may be unintentionally public.",
        check_operation_security,
    ),
    (
        "pagination-missing",
        "Collection GET without pagination",
        "Collection endpoints without pagination force clients to fetch unbounded result sets.",
        check_pagination,
    ),
    (
        "version-prefix-missing",
        "No version prefix in path",
        "Paths without a /vN prefix make it hard to ship breaking changes safely.",
        check_version_prefix,
    ),
]


def run_all_checks(spec: Spec) -> List[Finding]:
    """Run every registered check against a parsed spec, in catalog order."""
    findings: List[Finding] = []
    for _check_id, _title, _description, fn in CHECKS:
        findings.extend(fn(spec))
    return findings


def list_check_catalog() -> List[Dict[str, str]]:
    """Return the check catalog (id, title, one-line description) for clients."""
    return [
        {"check_id": check_id, "title": title, "description": description}
        for check_id, title, description, _fn in CHECKS
    ]
