"""MCP server exposing KAIROS graph tools (Track 01: tool selection through MCP).

Lets Claude Desktop / Claude Code use the same memory. Needs the `mcp` package.
  kairos mcp      (stdio transport)
"""

from __future__ import annotations

import json


def build_server():
    from mcp.server.fastmcp import FastMCP

    from kairos.agent.tools import Turn
    from kairos.runtime import Kairos
    from kairos.timeutil import DAY_MS, now_ms

    k = Kairos()
    mcp = FastMCP("kairos")

    def turn() -> Turn:
        return Turn(device_id=k.devices.get("pc", "pc-main"), utterance_id=None, session={}, now=now_ms())

    @mcp.tool()
    def recall_file(person: str, ext: str | None = None, days: int = 30) -> str:
        """Find a file a person sent: walks Person→Message→Link→Download→File. Returns the answer and evidence path."""
        res = k.agent.toolbox.t_recall_file(turn(), person=person, ext=ext, since=now_ms() - days * DAY_MS)
        return json.dumps({"answer": res.text, "data": res.data}, default=str)

    @mcp.tool()
    def graph_query(cypher: str) -> str:
        """Run a READ-ONLY Cypher query against the user's memory graph (writes are rejected)."""
        return json.dumps(k.graph.read_only(cypher), default=str)

    @mcp.tool()
    def explain_action(action_id: str) -> str:
        """Explain why KAIROS took an action: the chain of events/watchers/utterances behind it."""
        exp = k.explainer.explain(action_id)
        return json.dumps({"text": exp.text, "chain": exp.chain}, default=str)

    @mcp.tool()
    def list_watchers() -> str:
        """Active 'tell me when...' watchers."""
        return json.dumps(k.watchers.list_active(), default=str)

    @mcp.tool()
    def current_fact(key: str) -> str:
        """Current value of a versioned fact, e.g. office_location."""
        return json.dumps(k.memory.get_fact(key), default=str)

    @mcp.tool()
    def ask(text: str) -> str:
        """Send a command to KAIROS as if spoken on the PC (goes through the permission graph)."""
        return k.chat(text, k.devices.get("pc", "pc-main")).text

    return mcp


def main() -> None:
    try:
        server = build_server()
    except ImportError:
        raise SystemExit("MCP needs the `mcp` package: pip install mcp")
    server.run()


if __name__ == "__main__":
    main()
