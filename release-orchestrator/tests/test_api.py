import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

EXAMPLES = Path(__file__).resolve().parent.parent / "app" / "static" / "examples"
client = TestClient(app)


def post(name: str):
    payload = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    return client.post("/api/schedule", json=payload)


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "发布窗口编排" in r.text


def test_feasible_via_http():
    r = post("feasible.json")
    assert r.status_code == 200
    assert r.json()["status"] == "feasible"


def test_infeasible_via_http():
    r = post("infeasible.json")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "infeasible"
    assert body["blocking_constraints"]


def test_dst_error_via_http():
    r = post("dst_error.json")
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_time"


def test_malformed_body():
    r = client.post("/api/schedule", json={"services": []})
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_request"
