"""超限处置失败测（gate 1）。

本文件只做两件事：
1. 半改输出态必须被判定为红（integrity 检查 + 五视图同态）。
2. 处置落库中途失败必须整笔撤回，留下可续推的影子意图，不得冒充已落库。

在本仓改造前，这些测试必须为红。
"""

import pytest
from fastapi.testclient import TestClient

import app as A


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "DB_PATH", tmp_path / "dispose.db")
    A.SESSIONS.clear()
    if hasattr(A, "FAILPOINTS"):
        A.FAILPOINTS.clear()
    A.init_db()
    yield
    if hasattr(A, "FAILPOINTS"):
        A.FAILPOINTS.clear()


def _client_and_headers():
    c = TestClient(A.app)
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return c, {"Authorization": "Bearer " + r.json()["token"]}


def _mode(c, h, name):
    items = c.get("/api/sources", headers=h).json()["items"]
    return {s["name"]: s["output_mode"] for s in items}[name]


def test_half_written_output_state_is_red():
    """只改输出牌面、不落处置，必须被 integrity 判红，且五视图不得双态。"""
    c, h = _client_and_headers()
    assert _mode(c, h, "物流源") == "冻结"

    # 模拟“半改输出态”：直接改 sources.output_mode，不写处置册/意图。
    with A.connect() as conn:
        conn.execute(
            "UPDATE sources SET output_mode='降级只读' WHERE name='物流源'"
        )

    r = c.get("/api/dispositions/integrity", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert any(m["source"] == "物流源" for m in body["mismatches"])

    board = c.get("/api/board", headers=h).json()
    row = {s["name"]: s for s in board["sources"]}["物流源"]
    # 半改后不得出现“牌面说一套、账本说另一套”却被判为一致的情况：
    # integrity 已红，且五视图都从同一输出态派生。
    assert row["output_mode"] == "降级只读"
    assert all(a["effective_mode"] == row["output_mode"] for a in board["arrivals"] if a["source"] == "物流源")
    policy = {p["source"]: p for p in board["policy"]}["物流源"]
    assert policy["effective_mode"] == row["output_mode"]
    queue = {q["source"]: q for q in board["catchup_queue"]}["物流源"]
    assert queue["effective_mode"] == row["output_mode"]
    assert board["watermarks"]["frozen"] + board["watermarks"]["degraded"] >= 1


def test_catchup_releases_register_and_stays_consistent():
    """补齐恢复必须解除生效处置，处置册与牌面不得打架，且不篡改处置类别。"""
    c, h = _client_and_headers()
    c.post(
        "/api/arrivals",
        headers=h,
        json={"source": "订单源", "source_clock": "2026-11-16T10:00:00",
              "ingest_clock": "2026-11-16T11:00:00"},
    )
    d = c.post("/api/dispositions", headers=h,
               json={"source": "订单源", "kind": "冻结", "operator": "甲"})
    assert d.status_code == 200 and d.json()["output_mode"] == "冻结"

    r = c.post("/api/catchup/订单源", headers=h)
    assert r.json()["output_mode"] == "可写"
    assert c.get("/api/dispositions/integrity", headers=h).json()["ok"] is True

    items = c.get("/api/dispositions", headers=h).json()["items"]
    assert items and items[0]["kind"] == "冻结" and items[0]["status"] == "已解除"


def _failpoints():
    fp = getattr(A, "FAILPOINTS", None)
    assert fp is not None, "超限处置失败注入口未落地"
    return fp


@pytest.mark.parametrize("point", ["after_intent_commit", "during_output_update"])
def test_disposition_failure_rolls_back_and_resumes(point):
    """乙路径：先影子意图，再条件更新输出态；中途失败整笔撤回、可续推。"""
    c, h = _client_and_headers()
    fp = _failpoints()

    # 人为把物流源牌面拨回可写，制造一次需要落库的冻结处置。
    with A.connect() as conn:
        conn.execute("UPDATE sources SET output_mode='可写' WHERE name='物流源'")

    def boom():
        raise RuntimeError("injected failure at %s" % point)

    fp[point] = boom
    r = c.post(
        "/api/dispositions",
        headers=h,
        json={"source": "物流源", "kind": "冻结", "operator": "甲"},
    )
    assert r.status_code in (500, 503), r.text

    # 整笔撤回：输出态不得停留在半改状态。
    assert _mode(c, h, "物流源") == "可写"

    # 影子意图仍在，状态为待落库，不得冒充已落库。
    with A.connect() as conn:
        intents = [
            dict(x)
            for x in conn.execute(
                "SELECT * FROM disposition_intents WHERE source='物流源'"
            ).fetchall()
        ]
    assert len(intents) == 1
    assert intents[0]["state"] == "待落库"

    # 续推后收敛。
    del fp[point]
    r = c.post("/api/dispositions/resume", headers=h)
    assert r.status_code == 200, r.text
    assert _mode(c, h, "物流源") == "冻结"
    assert c.get("/api/dispositions/integrity", headers=h).json()["ok"] is True
