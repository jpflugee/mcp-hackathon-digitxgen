#!/usr/bin/env python
"""Local MCP toolkit entrypoint for watsonx Orchestrate (stdio transport).

watsonx Orchestrate runs a LOCAL MCP toolkit by starting this script inside its
own runtime and speaking MCP over stdio. This entrypoint imports the GSA JDP
MCP server and runs it over stdio — no HTTP, no Code Engine, no container.

The register script stages this file NEXT TO server.py from
tools/gsa_jdp_mcp_toolkit/, so the GSA server module is importable directly.
We add that directory to sys.path defensively in case Orchestrate runs the
command from a different working directory.

Orchestrate invokes:  python server.py
"""

import os
import sys

# Ensure the directory containing this file is on sys.path so the GSA server
# module (server.py, staged alongside this file) is importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The GSA JDP server defines `mcp` at module level and guards mcp.run() under
# __main__, so importing it here is safe — it won't start the server on import.
import server as _gsa_server  # noqa: E402

if __name__ == "__main__":
    # stdio is the transport watsonx Orchestrate uses for local MCP toolkits.
    _gsa_server.mcp.run(transport="stdio")
