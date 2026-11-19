"""超限处置争用失败测（gate 2）。

两名值班同时对同一超限源抢处置：先到者生效，后到者不得生效，
且必须留下超限处置争用痕迹；缺争用留痕即红。

在本仓改造前，本测试必须为红。
"""

import threading

import pytest
from fastapi.testclient import TestClient

import app as A


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "contention.db")
    A.SESSIONS.clear()
    if hasattr(A, "FAILPOINTS"):
        A.FAILPOINTS.clear()
    A.init_db()
    yield
    if hasattr(A, "FAILPOINTS"):
        A.FAILPOINTS.clear()


def _failpoints():
    fp = getattr(A, "FAILPOINTS", None)
    assert fp is not None, "超限处置争用注入口未落地"
    return fp


def _login(c, user="admin", pwd="123456"):
    r = c.post("/api/login", json={"username": user, "password": pwd})
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_two_operators_contend_for_same_overlimit_source():
    c1 = TestClient(A.app)
    c2 = TestClient(A.app)
    h1 = _login(c1)
    h2 = _login(c2)

    fp = _failpoints()
    entered = threading.Event()
    release = threading.Event()

    def hold_after_claim():
        entered.set()
        release.wait(5)

    fp["after_claim"] = hold_after_claim

    first = {}

    def first_dispose():
        first["r"] = c1.post(
            "/api/dispositions",
            headers=h1,
            json={"source": "支付源", "kind": "降级只读", "operator": "甲"},
        )

    t = threading.Thread(target=first_dispose)
    t.start()
    assert entered.wait(3), "先到值班未能进入处置"
    assert release.wait(0) is False

    # 后到值班：同一超限源抢处置，只记争用，不得生效。
    second = c2.post(
        "/api/dispositions",
        headers=h2,
        json={"source": "支付源", "kind": "降级只读", "operator": "乙"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "disposition_contention"
    contention_id = second.json()["contention_id"]

    release.set()
    t.join(5)
    assert first["r"].status_code == 200, first["r"].text

    with A.connect() as conn:
        rows = [
            dict(x)
            for x in conn.execute(
                "SELECT * FROM disposition_contention WHERE id=?", (contention_id,)
            ).fetchall()
        ]
        active = conn.execute(
            "SELECT COUNT(*) c FROM dispositions WHERE source='支付源' AND status='生效' "
            "AND operator='甲'"
        ).fetchone()["c"]
        same_kind = conn.execute(
            "SELECT COUNT(DISTINCT kind) k FROM dispositions WHERE source='支付源' AND status='生效'"
        ).fetchone()["k"]
        modes = conn.execute(
            "SELECT output_mode FROM sources WHERE name='支付源'"
        ).fetchone()["output_mode"]
    assert len(rows) == 1
    assert rows[0]["operator"] == "乙"
    # 先到者甲只落一笔生效处置；后到者乙未生效。
    assert active == 1
    assert same_kind == 1
    assert modes == "降级只读"

    # 同一源不得出现冻结与降级只读双牌。
    board = c1.get("/api/board", headers=h1).json()
    row = {s["name"]: s for s in board["sources"]}["支付源"]
    assert row["output_mode"] == "降级只读"
    assert row["disposition_kind"] == "降级只读"
