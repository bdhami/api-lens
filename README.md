# API Lens

An MCP server that reviews OpenAPI 3.x API designs for common flaws —
missing versions, undocumented error responses, inconsistent naming, auth
gaps, unpaginated collections, and more.

Built as a portfolio project demonstrating production MCP server development:
deterministic tool logic, structured JSON output, an eval harness, and
deployment-ready HTTP serving.

## What it does

API Lens exposes two MCP **tools**:

| Tool | What it does |
|---|---|
| `review_openapi(spec)` | Accepts an OpenAPI 3.x spec as YAML/JSON text **or** an `http(s)` URL, runs 10 deterministic design checks, and returns structured findings (`check_id`, `severity`, `category`, `message`, `location`, `suggestion`) plus a severity summary. |
| `list_checks()` | Returns the catalog of checks with a one-line explanation of each. |

## How the tools map to MCP concepts

- **Tools** are model-invokable functions. FastMCP's `@mcp.tool` decorator
  turns the plain Python functions in `server.py` into MCP tools, generating
  their JSON input schemas from type hints and their descriptions from
  docstrings — the docstrings are what the model reads during discovery, so
  they are written carefully.
- **Tool output** is plain JSON-serializable dicts, so any MCP client
  (Claude, ChatGPT via a remote connector, MCP Inspector) can consume
  findings without custom parsing.
- **Transport** is streamable HTTP (`transport="streamable-http"`), the
  standard for remote MCP servers, so this can sit behind HTTPS on any host
  and be registered as a connector.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python server.py                 # serves on 0.0.0.0:8000
PORT=9000 python server.py       # custom port
```

Quick smoke test with FastMCP's in-process client (no network needed):

```bash
python - <<'EOF'
import asyncio
from fastmcp import Client
from server import mcp

async def main():
    async with Client(mcp) as client:
        print(await client.call_tool("list_checks", {}))
        spec = open("evals/specs/messy.yaml").read()
        result = await client.call_tool("review_openapi", {"spec": spec})
        print(result.data["summary"])

asyncio.run(main())
EOF
```

Or point [MCP Inspector](https://github.com/modelcontextprotocol/inspector) at
`http://localhost:8000/mcp`.

## Project structure

```
api-lens/
├── server.py            # FastMCP server: tool definitions, spec loading (text or URL)
├── checks.py            # The 10 deterministic checks + check catalog registry
├── requirements.txt
├── evals/
│   ├── run_evals.py     # Eval harness: known-expectation fixtures
│   └── specs/           # clean.yaml, messy.yaml, minimal.json, remote.yaml
└── README.md
```

## Evals

Because the checks are deterministic, they can be evaluated like a linter:
each fixture spec has a known set of check IDs expected to fire.

```bash
python evals/run_evals.py
```

`remote.yaml` is served over local HTTP during the run to exercise the
spec-as-URL fetch path end to end. Exit code is 0 only if every expectation
holds (no missed findings, no false positives).

## Roadmap

- ChatGPT Apps SDK widget: render findings as an interactive report card.
- Severity configuration: let callers tune which checks run.
- SARIF output for CI integration.
- OAuth-authenticated spec fetching for private API registries.
