
import pytest
from fastapi.testclient import TestClient
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path/"t.db")
    init_db(); yield

def test_lag_freeze_and_catchup():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "源到达SLA"
    h = {"Authorization": "Bearer " + c.post("/api/login", json={"username":"admin","password":"123456"}).json()["token"]}
    r = c.post("/api/arrivals", headers=h, json={"source":"订单源","source_clock":"2026-11-16T10:00:00","ingest_clock":"2026-11-16T11:00:00"})
    # 采集入库只登记与分级，不擅自改输出态。
    assert r.status_code == 200 and r.json()["level"] == "违约"
    assert r.json()["required_mode"] == "冻结"
    assert r.json()["output_mode"] == "可写"
    # 超限处置走写路径。
    d = c.post("/api/dispositions", headers=h, json={"source":"订单源","kind":"冻结","operator":"甲"})
    assert d.status_code == 200 and d.json()["output_mode"] == "冻结"
    # 补齐后恢复。
    assert c.post("/api/catchup/订单源", headers=h).json()["output_mode"] == "可写"
