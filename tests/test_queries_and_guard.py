import re

import pytest

from kairos.agent.tools import TOOL_SPECS, Toolbox
from kairos.graph.guard import UnsafeQuery, ensure_read_only
from kairos.graph.queries import QUERIES, render, split_statements
from kairos.policy import command_capability

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _names_used_in_code() -> set[str]:
    """Every query name the code calls: graph.run("x") / .one("x") / run_template("x") / run_in_threadpool(.., "x")."""
    used: set[str] = set()
    for f in (ROOT / "kairos").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        used |= set(re.findall(r"""\.(?:run|one|run_template)\(\s*["'](\w+)["']""", text))
        used |= set(re.findall(r"""graph\.run,\s*["'](\w+)["']""", text))
    from kairos.watchers import EVENT_MATCHERS
    return used | set(EVENT_MATCHERS)


USED_IN_CODE = sorted(_names_used_in_code())


def test_all_queries_present_and_nonempty():
    missing = [q for q in USED_IN_CODE if q not in QUERIES]
    assert not missing
    assert all(QUERIES[q].strip() for q in USED_IN_CODE)


def test_no_comment_lines_leak_into_queries():
    assert not any(line.strip().startswith("//") for q in QUERIES.values() for line in q.splitlines())


def test_templates_render_only_allowlisted_labels():
    assert "Event:Notification" in render(QUERIES["create_event"], "Notification")
    with pytest.raises(ValueError):
        render(QUERIES["create_event"], "Notification) DETACH DELETE (x")


def test_watcher_matchers_share_return_shape():
    for name in ["match_entity_state", "match_event_type", "match_trip_eta", "match_app_text", "due_time_watchers"]:
        ret = QUERIES[name].split("RETURN")[-1]
        assert re.findall(r"AS (\w+)", ret) == ["watcher_id", "description", "notify_device", "event_text"]


def test_schema_splits():
    assert len(split_statements(QUERIES["schema"])) > 10


@pytest.mark.parametrize("q", [
    "MATCH (n) RETURN n LIMIT 5",
    "MATCH (p:Person {name: 'SET me free'}) RETURN p",
    "CALL db.idx.vector.queryNodes('Fact', 'embedding', 3, vecf32([1.0])) YIELD node RETURN node",
])
def test_guard_allows_reads(q):
    assert ensure_read_only(q) == q


@pytest.mark.parametrize("q", [
    "MATCH (n) DETACH DELETE n",
    "MERGE (x:Person {name: 'a'})",
    "MATCH (n) SET n.x = 1",
    "CALL db.idx.fulltext.drop('Event')",
    "MATCH (n) RETURN n; MATCH (m) DELETE m",
])
def test_guard_blocks_writes(q):
    with pytest.raises(UnsafeQuery):
        ensure_read_only(q)


def test_command_capability():
    assert command_capability("Get-ChildItem C:\\Users") == "shell.read"
    assert command_capability("Get-ChildItem; Remove-Item x") == "shell.exec"
    assert command_capability("Remove-Item -Recurse C:\\") == "shell.exec"


def test_every_tool_spec_has_an_implementation():
    assert all(hasattr(Toolbox, f"t_{name}") for name in TOOL_SPECS)


def test_sandbox_path(tmp_path):
    class K:
        class settings:
            sandbox_dir = str(tmp_path)

    tb = Toolbox(K())
    assert tb.sandbox_path("temp folder") == str(tmp_path / "temp")
    assert tb.sandbox_path("..\\..\\Windows") is None
    assert tb.sandbox_path("folder") == str(tmp_path / "temp")
