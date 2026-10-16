"""Permission graph (Q8) and the audit trail. Nothing with a side effect runs without an Action node."""

from __future__ import annotations

import json
import re
from enum import Enum

from kairos.graph.client import Graph
from kairos.models import new_id
from kairos.timeutil import MINUTE_MS, now_ms

ALWAYS_CONFIRM_RISK = 3

# Read-only PowerShell cmdlets that may run under the low-risk `shell.read` capability.
SAFE_COMMANDS = re.compile(
    r"^\s*(?:Get-ChildItem|Get-Process|Get-Date|Get-Location|Get-Content|Test-Connection|Get-PSDrive|"
    r"Get-NetIPAddress|Get-NetIPConfiguration|Get-NetAdapter|Get-ComputerInfo|Get-CimInstance|Get-Volume|"
    r"Get-Service|Get-Item|Get-ItemProperty|Get-Disk|Get-Uptime|dir|ls|pwd|whoami|hostname|ipconfig|"
    r"systeminfo|tasklist)\b",
    re.I,
)
# Filters that may follow a pipe: they only reshape output, never change anything.
SAFE_FILTERS = re.compile(
    r"^\s*(?:Sort-Object|Select-Object|Where-Object|Format-Table|Format-List|Out-String|Measure-Object|"
    r"Select-String|Group-Object|ForEach-Object|Head|Select|Sort|Where|\?|%|ft|fl)\b",
    re.I,
)

DANGEROUS_TOKENS = re.compile(
    r"(?:;|&&?|>>?|\$\(|`|Invoke-|Start-|Remove-|Set-|New-|Stop-|Restart-|del\b|rm\b|rmdir\b|kill\b|format\b|reg\b|net\s+user)",
    re.I,
)


class Decision(str, Enum):
    AUTO = "auto"
    CONFIRM = "confirm"
    DENY = "deny"


def _clean_command(cmd: str) -> str:
    c = cmd.strip()
    # Strip leading powershell / pwsh invocations e.g. powershell -Command "Get-ChildItem"
    c = re.sub(r"^(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\s+(?:-(?:NoProfile|Command|c)\s+)?", "", c, flags=re.I).strip()
    if (c.startswith('"') and c.endswith('"')) or (c.startswith("'") and c.endswith("'")):
        c = c[1:-1].strip()
    # Handle parentheses wrappers e.g. (Get-ChildItem ...).Count or (Get-Process)
    c = re.sub(r"^\((.*?)\)(?:\.\w+)?$", r"\1", c).strip()
    return c


def is_safe_command(cmd: str) -> bool:
    """True for a read-only command, optionally piped through reshaping filters. No `;`, `&`, `>` or `$(`."""
    c = _clean_command(cmd)
    if not c or DANGEROUS_TOKENS.search(c):
        return False
    parts = [p.strip() for p in c.split("|") if p.strip()]
    if not parts or not SAFE_COMMANDS.match(parts[0]):
        return False
    return all(bool(SAFE_FILTERS.match(p)) for p in parts[1:])


def command_capability(cmd: str) -> str:
    return "shell.read" if is_safe_command(cmd) else "shell.exec"


class Policy:
    def __init__(self, graph: Graph):
        self.g = graph

    def check(self, device_id: str, capability: str) -> tuple[Decision, int]:
        row = self.g.one("check_permission", device_id=device_id, capability=capability)
        if not row:
            return Decision.DENY, 3
        risk = int(row.get("risk") or 0)
        mode = Decision(row.get("mode") or "deny")
        if mode == Decision.AUTO and risk >= ALWAYS_CONFIRM_RISK and not row.get("trusted"):
            mode = Decision.CONFIRM  # tier 3 asks unless the user explicitly said "always allow"
        return mode, risk

    def trust(self, device_id: str, capability: str) -> bool:
        """The user said "yes, always": this device may use this capability without asking again."""
        rows = self.g.run("trust_permission", device_id=device_id, capability=capability, now=now_ms())
        return bool(rows)

    def check_pair(self, target: str, origin: str | None, capability: str) -> tuple[Decision, int]:
        """Target device decides; for requests from a remote channel (`origin`, e.g. Telegram) that has its *own*
        rule for this capability, the stricter of the two applies. An origin without a rule doesn't block."""
        decision, risk = self.check(target, capability)
        if origin and origin != target:
            row = self.g.one("check_permission", device_id=origin, capability=capability)
            if row:
                o_mode = Decision(row.get("mode") or "deny")
                if _STRICTNESS[o_mode] > _STRICTNESS[decision]:
                    decision = o_mode
        return decision, risk


_STRICTNESS = {Decision.AUTO: 0, Decision.CONFIRM: 1, Decision.DENY: 2}


class AuditLog:
    def __init__(self, graph: Graph):
        self.g = graph

    @staticmethod
    def new_action_id() -> str:
        return f"act-{new_id()[:8]}"

    def record(self, tool: str, args: dict, capability: str, status: str, summary: str, device_id: str | None,
               causes: list[tuple[str, str]], result: str | None = None, action_id: str | None = None,
               origin_device: str | None = None, tainted: bool = False, taint_sources: str | None = None,
               influenced_by: list[str] = ()) -> str:
        aid = action_id or self.new_action_id()
        origin_device = origin_device or device_id
        props = {"id": aid, "tool": tool, "args_json": json.dumps(args, default=str), "status": status,
                 "summary": summary, "ts": now_ms(), "device_id": device_id, "result": result,
                 "origin_device": origin_device, "tainted": tainted, "taint_sources": taint_sources}
        self.g.run("create_action", props=props, capability=capability)
        for label, cid in causes:
            if cid:
                self.g.run_template("link_cause", label, action_id=aid, cause_id=cid)
        for eid in list(influenced_by)[:5]:  # external content the model had read when it chose this action
            self.g.run("link_influence", action_id=aid, event_id=eid)
        return aid

    def update(self, action_id: str, status: str, result: str | None = None) -> None:
        self.g.run("update_action", id=action_id, status=status, result=result, now=now_ms())

    def pending(self, window_ms: int = 5 * MINUTE_MS) -> dict | None:
        now = now_ms()
        self.g.run("expire_pending_actions", since=now - window_ms)
        return self.g.one("pending_action", since=now - window_ms)

    def last(self, tools: list[str]) -> str | None:
        row = self.g.one("last_action", tools=tools)
        return row["id"] if row else None
