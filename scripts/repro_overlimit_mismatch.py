#!/usr/bin/env python3
"""复现：超限源在源目录与策略附页上读数不一致。

用法：.venv/bin/python scripts/repro_overlimit_mismatch.py

只使用临时库，不触碰 data/app.db。
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app as A  # noqa: E402


def appendix_mode(conn, source):
    rows = conn.execute(
        "SELECT level FROM arrivals WHERE source=? ORDER BY id", (source,)
    ).fetchall()
    levels = [r[0] for r in rows]
    if "违约" in levels:
        return "冻结"
    if "告警" in levels:
        return "降级只读"
    return "可写"


def main():
    A.DB_PATH = Path(tempfile.mkdtemp()) / "repro.db"
    A.init_db()

    from fastapi.testclient import TestClient

    client = TestClient(A.app)
    token = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    headers = {"Authorization": "Bearer " + token}

    def snapshot(label):
        with A.connect() as conn:
            sources = {
                r["name"]: r["output_mode"]
                for r in conn.execute("SELECT * FROM sources").fetchall()
            }
            appendix = {
                name: appendix_mode(conn, name) for name in sources
            }
            arrivals = [
                (r["source"], r["lag_minutes"], r["level"])
                for r in conn.execute(
                    "SELECT * FROM arrivals ORDER BY id DESC LIMIT 4"
                ).fetchall()
            ]
        print(label)
        for name in sources:
            flag = "一致" if sources[name] == appendix[name] else "不一致"
            print(
                f"  {name}: 目录态={sources[name]} 附页读数={appendix[name]} [{flag}]"
            )
        print(f"  最近到达: {arrivals}")
        return sources, appendix

    print("=== 复现前置状态（seed） ===")
    snapshot("处置前")

    print("\n=== 场景：物流源仍有未补齐的违约到达，再登记一条 45 分告警到达 ===")
    r = client.post(
        "/api/arrivals",
        headers=headers,
        json={
            "source": "物流源",
            "source_clock": "2026-11-16T10:00:00",
            "ingest_clock": "2026-11-16T10:45:00",
        },
    )
    print("POST /api/arrivals ->", r.status_code, r.json())
    sources, appendix = snapshot("处置后")
    mismatch = [n for n in sources if sources[n] != appendix[n]]
    print("\n复现结论：", "已复现不一致 -> %s" % mismatch if mismatch else "未复现")
    return 0 if mismatch else 1


if __name__ == "__main__":
    raise SystemExit(main())
