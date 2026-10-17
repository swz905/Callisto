"""`kairos doctor` helpers: every query gets well-typed dummy params; report and offline behaviour."""

import re
from pathlib import Path

from kairos import doctor
from kairos.config import load_settings
from kairos.graph.client import _columns
from kairos.graph.queries import QUERIES


def test_every_query_gets_all_its_params():
    for name in QUERIES:
        q = doctor.query_for(name)
        assert "{label}" not in q
        params = doctor.dummy_params(q)
        assert set(params) == set(re.findall(r"\$(\w+)", q)), name


def test_param_types_match_how_queries_use_them():
    for name in QUERIES:
        q = doctor.query_for(name)
        p = doctor.dummy_params(q)
        for var in re.findall(r"(?:UNWIND|IN) \$(\w+)", q):
            assert isinstance(p[var], list), (name, var)
        for var in re.findall(r"\+= \$(\w+)", q):
            assert isinstance(p[var], dict), (name, var)
        for var in re.findall(r"vecf32\(\$(\w+)\)", q):
            assert isinstance(p[var], list) and p[var], (name, var)


def test_doctor_reports_missing_falkordb(monkeypatch):
    monkeypatch.setenv("FALKORDB_PORT", "1")  # nothing listens here
    checks, ok = doctor.run(load_settings(), Path("."))
    assert not ok
    db = next(c for c in checks if c.name == "FalkorDB reachable")
    assert not db.ok and "docker compose up -d falkordb" in db.detail
    text = doctor.report(checks, ok)
    assert "[FAIL] FalkorDB reachable" in text and "NOT READY" in text


def test_optional_packages_never_block():
    assert all(not c.required for c in doctor.check_packages() if "optional" in c.detail or "optional" in c.name)


def test_demo_pdf_endpoint_serves_a_valid_pdf():
    from fastapi.testclient import TestClient

    from kairos.hub.app import app

    client = TestClient(app)  # no `with`: lifespan (FalkorDB) not started; this route doesn't need it
    r = client.get("/demo/Q3_report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    body = r.content
    assert body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF") and b"Q3_report" in body
    xref = int(body.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert body[xref:xref + 4] == b"xref"  # offset table points at the right place
    assert client.get("/demo/notes.txt").status_code == 404


def test_result_header_names_decode():
    assert _columns([[1, "a"], [1, b"b"], "c"]) == ["a", "b", "c"]
