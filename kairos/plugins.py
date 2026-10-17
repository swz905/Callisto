"""Plugins via MCP: KAIROS as an MCP *client*, so any MCP tool server becomes an agent tool.

Config: `plugins.json` in the repo root (or KAIROS_PLUGINS_FILE):
{
  "servers": {
    "files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Documents"],
              "env": {}, "mode": "confirm"}
  }
}
Each server's tools are exposed to the LLM as `mcp__<server>__<tool>` and guarded by the permission graph:
capability `plugin.<server>` (risk 2) with the configured mode (`confirm` by default, `auto` or `deny`) on
every device. Plugin output is *external* content: it taints the turn (see kairos/trust.py).
Needs the `mcp` package; without it (or without a config) plugins are simply off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairos.runtime import Kairos

log = logging.getLogger(__name__)
PREFIX = "mcp__"
CALL_TIMEOUT_S = 60


class PluginError(RuntimeError):
    pass


def tool_name(server: str, tool: str) -> str:
    """OpenAI-compatible function name: ^[a-zA-Z0-9_-]{1,64}$."""
    raw = f"{PREFIX}{server}__{tool}"
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)[:64]


@dataclass
class PluginTool:
    server: str
    tool: str
    name: str
    description: str
    schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

    def spec(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": f"[plugin {self.server}] {self.description}"[:1000],
            "parameters": self.schema if self.schema.get("type") == "object" else {"type": "object", "properties": {}}}}


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("servers", {})
    except Exception as exc:
        log.warning("plugins config %s unreadable: %s", path, exc)
        return {}


def result_text(result: Any) -> str:
    parts = []
    for c in getattr(result, "content", None) or []:
        text = getattr(c, "text", None)
        parts.append(text if text is not None else f"[{getattr(c, 'type', 'content')}]")
    return "\n".join(parts).strip() or "(no output)"


class PluginManager:
    def __init__(self, k: Kairos | None, config_path: Path):
        self.k = k
        self.config = load_config(config_path)
        self.tools: dict[str, PluginTool] = {}
        self.sessions: dict[str, Any] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stack: AsyncExitStack | None = None
        self.errors: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.tools)

    def has(self, name: str) -> bool:
        return name in self.tools

    def capability(self, name: str) -> str:
        return f"plugin.{self.tools[name].server}"

    def specs(self) -> list[dict]:
        return [t.spec() for t in self.tools.values()]

    # --- lifecycle -------------------------------------------------------------------
    def start(self, timeout: float = 30) -> None:
        if not self.config:
            return
        try:
            import mcp  # noqa: F401
        except ImportError:
            log.warning("plugins configured but the `mcp` package is missing (pip install mcp)")
            return
        ready = threading.Event()
        threading.Thread(target=self._run_loop, args=(ready,), name="kairos-plugins", daemon=True).start()
        ready.wait(timeout)
        self._seed_permissions()

    def _run_loop(self, ready: threading.Event) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect_all())
        ready.set()
        self.loop.run_forever()

    async def _connect_all(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        for server, cfg in self.config.items():
            try:
                params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []),
                                               env=cfg.get("env") or None)
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                self.sessions[server] = session
                for t in listed.tools:
                    pt = PluginTool(server, t.name, tool_name(server, t.name), t.description or t.name,
                                    dict(getattr(t, "inputSchema", None) or {"type": "object", "properties": {}}))
                    self.tools[pt.name] = pt
                log.info("plugin %s: %d tool(s)", server, len(listed.tools))
            except Exception as exc:
                self.errors[server] = repr(exc)[:200]
                log.warning("plugin %s failed to start: %s", server, exc)

    def _seed_permissions(self) -> None:
        if self.k is None:
            return
        g = self.k.graph
        for server, cfg in self.config.items():
            if server not in self.sessions:
                continue
            cap = f"plugin.{server}"
            g.run("seed_capability", name=cap, risk=2, description=f"MCP plugin {server}")
            mode = cfg.get("mode", "confirm") if cfg.get("mode") in ("auto", "confirm", "deny") else "confirm"
            for device_id in self.k.devices.values():
                g.run("seed_permission", device_id=device_id, capability=cap, mode=mode)

    # --- calls ---------------------------------------------------------------------------
    def call(self, name: str, args: dict) -> str:
        t = self.tools.get(name)
        if t is None or self.loop is None:
            raise PluginError(f"unknown plugin tool {name}")
        session = self.sessions[t.server]
        fut = asyncio.run_coroutine_threadsafe(session.call_tool(t.tool, args or {}), self.loop)
        result = fut.result(CALL_TIMEOUT_S)
        text = result_text(result)
        if getattr(result, "isError", False):
            raise PluginError(text[:300])
        return text[:4000]
