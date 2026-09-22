"""Entry point so the server can be launched as `python -m mcp_server`.

The agent spawns it exactly this way as a subprocess (see docs/MCP.md).
"""

from mcp_server.server import main

if __name__ == "__main__":
    main()
