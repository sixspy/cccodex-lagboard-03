
import hashlib, json, secrets, sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SPEC_PATH = ROOT / "docs" / "超限处置说明.md"
SESSIONS: dict[str, str] = {}

# 超限处置失败注入口（测试用；生产为空）。
FAILPOINTS: dict[str, object] = {}

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

# 严酷度排序：冻结 > 降级只读 > 可写。
SEVERITY = {"可写": 0, "降级只读": 1, "冻结": 2}
LEVEL_MODE = {"正常": "可写", "告警": "降级只读", "违约": "冻结"}
DISPOSITION_KINDS = ("冻结", "降级只读")
PATH = "乙"  # 本仓只走乙路径；甲路径（同笔事务）不启用，二者不得混用。

SPEC_FALLBACK = """# 超限处置说明（钉死）

- 处置路径：乙路径。先落入处置意图影子表，再条件更新各源输出态；中断可续推。
- 甲路径（冻结/降级与输出牌面同笔事务）不启用，甲乙口径不得混用。
- 超限处置只能是「冻结」或「降级只读」中的一种，二者互斥；可写不是处置。
- 超限级别决定处置类别：违约→冻结，告警→降级只读；同时存在时取最严（冻结）。
- 偏离本说明的处置请求一律拒绝，并记入「处置偏了」。
- 落定后写入处置册并钉死；超限当天不得改口处置册。
- 源目录、到达详情、策略附页、补采队列、水位卡片五视图同态。
"""


class Conn(sqlite3.Connection):
    """sqlite3 的 with 只提交/回滚不关闭连接；这里补上关闭，避免句柄泄漏。"""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, factory=Conn)
    conn.row_factory = sqlite3.Row
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_text() -> str:
    return date.today().isoformat()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def ensure_spec(conn):
    """写入并钉死超限处置说明；已存在则绝不改写。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS disposition_spec(
        id INTEGER PRIMARY KEY CHECK(id=1),
        path TEXT NOT NULL,
        statement TEXT NOT NULL,
        pinned_sha TEXT NOT NULL,
        pinned_at TEXT NOT NULL)""")
    if conn.execute("SELECT 1 FROM disposition_spec WHERE id=1").fetchone():
        return
    statement = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else SPEC_FALLBACK
    conn.execute(
        "INSERT INTO disposition_spec(id,path,statement,pinned_sha,pinned_at) VALUES (1,?,?,?,?)",
        (PATH, statement, sha(statement), now_text()),
    )


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_recovery(
          source TEXT PRIMARY KEY,
          cleared_through_arrival_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS disposition_intents(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          kind TEXT NOT NULL,
          operator TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dispositions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          kind TEXT NOT NULL,
          status TEXT NOT NULL,
          operator TEXT NOT NULL,
          register_day TEXT NOT NULL,
          path TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS disposition_contention(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          operator TEXT NOT NULL,
          requested_kind TEXT NOT NULL,
          active_operator TEXT NOT NULL,
          message TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS disposition_deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          operator TEXT NOT NULL,
          requested_kind TEXT NOT NULL,
          required_kind TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        # 同一源同一时刻只允许一条待落库意图，杜绝双态并存。
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_intent_pending "
                     "ON disposition_intents(source) WHERE state='待落库'")
        ensure_spec(conn)
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))
            # 种子源的非可写输出态必须有处置册背书，否则视为半改输出态。
            seed_day = "2026-11-16"
            for name, kind in (("支付源", "降级只读"), ("物流源", "冻结")):
                conn.execute("INSERT INTO dispositions(source,kind,status,operator,register_day,path,note,created_at) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (name, kind, "生效", "系统种子", seed_day, PATH, "种子基线处置", seed_day + "T00:00:00"))


def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"


def mode_of(level):
    return LEVEL_MODE[level]


@app.on_event("startup")
def _s(): init_db()


# ---------- 一致性口径 ----------

def required_mode(conn, source: str) -> str:
    """自最近一次补齐以来仍未消解的到达所要求的最严输出态。"""
    row = conn.execute(
        "SELECT cleared_through_arrival_id FROM source_recovery WHERE source=?", (source,)
    ).fetchone()
    cleared = row["cleared_through_arrival_id"] if row else 0
    levels = [r["level"] for r in conn.execute(
        "SELECT level FROM arrivals WHERE source=? AND id>?", (source, cleared)
    ).fetchall()]
    if not levels:
        return "可写"
    return max((LEVEL_MODE[l] for l in levels), key=lambda m: SEVERITY[m])


def effective_disposition(conn, source: str):
    return conn.execute(
        "SELECT * FROM dispositions WHERE source=? AND status='生效' ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()


def build_board(conn):
    """五视图共用同一输出态，禁止各算各的。"""
    rows = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id").fetchall()]
    mode_by = {s["name"]: s["output_mode"] for s in rows}
    for s in rows:
        req = required_mode(conn, s["name"])
        disp = effective_disposition(conn, s["name"])
        s["required_mode"] = req
        s["pending"] = s["output_mode"] != req
        s["disposition_kind"] = disp["kind"] if disp else None

    arrivals = []
    for r in conn.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall():
        d = dict(r)
        d["effective_mode"] = mode_by.get(d["source"], "未知")
        arrivals.append(d)

    policy = [{
        "source": s["name"],
        "sla_minutes": s["sla_minutes"],
        "output_mode": s["output_mode"],
        "required_mode": s["required_mode"],
        "effective_mode": s["output_mode"],
        "disposition_kind": s["disposition_kind"],
        "pending": s["pending"],
    } for s in rows]

    # 补采队列按源一行，与源目录同态；历史补齐记录只作为最近状态附注。
    last_catchup = {
        r["source"]: r for r in conn.execute(
            "SELECT * FROM catchups ORDER BY id ASC"
        ).fetchall()
    }
    catchup_queue = []
    for s in rows:
        last = last_catchup.get(s["name"])
        catchup_queue.append({
            "source": s["name"],
            "output_mode": s["output_mode"],
            "required_mode": s["required_mode"],
            "effective_mode": s["output_mode"],
            "pending": s["pending"],
            "needs_catchup": s["output_mode"] != "可写",
            "last_status": last["status"] if last else None,
            "last_created_at": last["created_at"] if last else None,
        })

    watermarks = {
        "sources": len(rows),
        "frozen": sum(1 for s in rows if s["output_mode"] == "冻结"),
        "degraded": sum(1 for s in rows if s["output_mode"] == "降级只读"),
        "pending": sum(1 for s in rows if s["pending"]),
    }
    return {
        "sources": rows,
        "arrivals": arrivals,
        "policy": policy,
        "catchup_queue": catchup_queue,
        "watermarks": watermarks,
    }


def integrity_report(conn):
    mismatches = []
    for s in conn.execute("SELECT * FROM sources ORDER BY id").fetchall():
        req = required_mode(conn, s["name"])
        disp = effective_disposition(conn, s["name"])
        if s["output_mode"] != req:
            mismatches.append({
                "source": s["name"],
                "output_mode": s["output_mode"],
                "required_mode": req,
                "reason": "输出态与超限要求不一致（待处置或半改输出态）",
            })
            continue
        if disp and disp["kind"] != s["output_mode"]:
            mismatches.append({
                "source": s["name"],
                "output_mode": s["output_mode"],
                "required_mode": req,
                "reason": "处置册与输出牌面不一致",
            })
            continue
        # 非可写牌面必须有生效处置背书；否则就是「半改输出态冒充落库」。
        if s["output_mode"] != "可写" and not disp:
            mismatches.append({
                "source": s["name"],
                "output_mode": s["output_mode"],
                "required_mode": req,
                "reason": "输出牌面无处置册背书（半改输出态冒充落库）",
            })
    # 同一源同一天不得并存两种生效处置。
    for r in conn.execute(
        "SELECT source, register_day, COUNT(DISTINCT kind) k FROM dispositions "
        "WHERE status='生效' GROUP BY source, register_day HAVING k>1"
    ).fetchall():
        mismatches.append({
            "source": r["source"],
            "output_mode": None,
            "required_mode": None,
            "reason": "同一业务日并存冻结与降级双态",
        })
    return {"ok": not mismatches, "mismatches": mismatches}


# ---------- 基础接口 ----------

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}


class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str


@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    """采集入库：只登记到达与分级，不擅自改输出牌面；超限须走处置写路径。"""
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        lag = int((datetime.fromisoformat(body.ingest_clock) - datetime.fromisoformat(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        req = required_mode(c, body.source)
        return {"lag_minutes": lag, "level": lv, "required_mode": req,
                "output_mode": src["output_mode"], "pending": src["output_mode"] != req}


@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}


@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = now_text()
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        top = c.execute("SELECT COALESCE(MAX(id),0) m FROM arrivals WHERE source=?", (source,)).fetchone()["m"]
        c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
        c.execute("INSERT INTO source_recovery(source,cleared_through_arrival_id) VALUES (?,?) "
                  "ON CONFLICT(source) DO UPDATE SET cleared_through_arrival_id=excluded.cleared_through_arrival_id",
                  (source, top))
        c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                  (source, "成功", "可写", "补齐后恢复可写", now))
        # 补齐后解除处置；只改状态，不改处置类别，处置册历史不动。
        c.execute("UPDATE dispositions SET status='已解除' WHERE source=? AND status='生效'", (source,))
        return {"ok": True, "output_mode": "可写", "required_mode": "可写", "pending": False}


@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}


# ---------- 超限处置（乙路径） ----------

class DispositionIn(BaseModel):
    source: str
    kind: str
    operator: str | None = None


def _fire(name: str):
    fn = FAILPOINTS.get(name)
    if callable(fn):
        fn()


def _record_deviation(conn, source, operator, requested, required, detail):
    conn.execute(
        "INSERT INTO disposition_deviations(source,operator,requested_kind,required_kind,detail,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (source, operator, requested, required, detail, now_text()),
    )


@app.post("/api/dispositions")
def dispose(body: DispositionIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    operator = (body.operator or user).strip()
    now = now_text()

    with connect() as c:
        if not c.execute("SELECT 1 FROM sources WHERE name=?", (body.source,)).fetchone():
            raise HTTPException(404, "未知源")

    # 口径校验：只能走冻结/降级之一，且必须与超限级别相符。
    with connect() as c:
        req = required_mode(c, body.source)
        if req == "可写":
            return JSONResponse(status_code=409, content={"ok": False, "code": "not_overlimit",
                                                          "detail": "该源当前未超限，无可处置"})
        if body.kind not in DISPOSITION_KINDS or body.kind != req:
            with connect() as c2:
                _record_deviation(c2, body.source, operator, body.kind, req,
                                  "偏离超限处置说明：要求 %s，请求 %s" % (req, body.kind))
            return JSONResponse(status_code=422, content={"ok": False, "code": "disposition_deviation",
                                                          "detail": "处置偏了：要求 %s，请求 %s" % (req, body.kind)})

    # 超限当天不许改口处置册：当天只要记过另一种处置类别，就不得改口。
    today = today_text()
    with connect() as c:
        recorded = sorted({r["kind"] for r in c.execute(
            "SELECT kind FROM dispositions WHERE source=? AND register_day=?",
            (body.source, today)).fetchall()})
    if recorded and body.kind not in recorded:
        with connect() as c:
            _record_deviation(c, body.source, operator, body.kind, req,
                              "超限当天不许改口超限处置册（当天已记 %s）" % "/".join(recorded))
        return JSONResponse(status_code=409, content={"ok": False, "code": "register_locked_same_day",
                                                      "detail": "超限当天不许改口超限处置册"})

    # 乙路径第一步：落入处置意图影子表（同时是争用占位）。
    try:
        with connect() as c:
            cur = c.execute(
                "INSERT INTO disposition_intents(source,kind,operator,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (body.source, body.kind, operator, "待落库", now, now))
            intent_id = cur.lastrowid
    except sqlite3.IntegrityError:
        with connect() as c:
            active = c.execute(
                "SELECT * FROM disposition_intents WHERE source=? AND state='待落库'",
                (body.source,)).fetchone()
        # 同一值班、同一类别的重试视为续推自己的意图，不记争用。
        if active and active["operator"] == operator and active["kind"] == body.kind:
            intent_id = active["id"]
        else:
            with connect() as c:
                cur = c.execute(
                    "INSERT INTO disposition_contention(source,operator,requested_kind,active_operator,message,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (body.source, operator, body.kind, active["operator"] if active else "",
                     "超限处置争用：同一超限源已有值班在处置", now))
                cid = cur.lastrowid
            return JSONResponse(status_code=409, content={"ok": False, "code": "disposition_contention",
                                                          "detail": "超限处置争用，后到只记争用",
                                                          "contention_id": cid})

    # 乙路径第二步：条件更新输出态 + 处置册，同笔提交；失败整笔撤回。
    try:
        _fire("after_claim")
        _fire("after_intent_commit")
        with connect() as c:
            cur = c.execute("UPDATE sources SET output_mode=? WHERE name=? AND output_mode<>?",
                            (body.kind, body.source, body.kind))
            if cur.rowcount == 0:
                current = c.execute("SELECT output_mode FROM sources WHERE name=?",
                                    (body.source,)).fetchone()["output_mode"]
                if current != body.kind:
                    raise RuntimeError("条件更新未命中，输出态未被改动")
            _fire("during_output_update")
            c.execute("UPDATE disposition_intents SET state='已落库', updated_at=? WHERE id=?",
                      (now_text(), intent_id))
            # 当日已记同类生效处置时不重复记账（幂等）；跨业务日仍各记一笔。
            same = c.execute(
                "SELECT 1 FROM dispositions WHERE source=? AND kind=? AND status='生效' AND register_day=?",
                (body.source, body.kind, today)).fetchone()
            if not same:
                c.execute("INSERT INTO dispositions(source,kind,status,operator,register_day,path,note,created_at) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (body.source, body.kind, "生效", operator, today, PATH,
                           "超限处置落定并钉死", now_text()))
    except Exception as exc:
        # 输出态随事务整笔撤回；意图保留「待落库」供续推，不冒充已落库。
        return JSONResponse(status_code=503, content={"ok": False, "code": "disposition_failed",
                                                      "detail": str(exc), "intent_id": intent_id})

    return {"ok": True, "source": body.source, "output_mode": body.kind,
            "required_mode": req, "path": PATH, "intent_id": intent_id}


@app.post("/api/dispositions/resume")
def resume_dispositions(authorization: str | None = Header(default=None)):
    """续推中断的处置意图；逐条条件更新，仍禁止双态并存。"""
    require_user(authorization)
    resumed, failed = [], []
    with connect() as c:
        intents = [dict(r) for r in c.execute(
            "SELECT * FROM disposition_intents WHERE state='待落库' ORDER BY id").fetchall()]
    for it in intents:
        try:
            with connect() as c:
                req = required_mode(c, it["source"])
                if req != it["kind"]:
                    c.execute("UPDATE disposition_intents SET state='已撤回', updated_at=? WHERE id=?",
                              (now_text(), it["id"]))
                    failed.append({"id": it["id"], "source": it["source"], "reason": "已不符合当前超限要求",
                                   "required_mode": req})
                    continue
                cur = c.execute("UPDATE sources SET output_mode=? WHERE name=? AND output_mode<>?",
                                (it["kind"], it["source"], it["kind"]))
                if cur.rowcount == 0:
                    current = c.execute("SELECT output_mode FROM sources WHERE name=?",
                                        (it["source"],)).fetchone()["output_mode"]
                    if current != it["kind"]:
                        raise RuntimeError("条件更新未命中")
                c.execute("UPDATE disposition_intents SET state='已落库', updated_at=? WHERE id=?",
                          (now_text(), it["id"]))
                same = c.execute(
                    "SELECT 1 FROM dispositions WHERE source=? AND kind=? AND status='生效' AND register_day=?",
                    (it["source"], it["kind"], today_text())).fetchone()
                if not same:
                    c.execute("INSERT INTO dispositions(source,kind,status,operator,register_day,path,note,created_at) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (it["source"], it["kind"], "生效", it["operator"], today_text(), PATH,
                               "超限处置续推落定", now_text()))
            resumed.append({"id": it["id"], "source": it["source"], "output_mode": it["kind"]})
        except Exception as exc:
            failed.append({"id": it["id"], "source": it["source"], "reason": str(exc)})
    return {"ok": not failed, "resumed": resumed, "failed": failed}


@app.get("/api/dispositions")
def list_dispositions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM dispositions ORDER BY id DESC").fetchall()],
            "intents": [dict(r) for r in c.execute(
                "SELECT * FROM disposition_intents ORDER BY id DESC").fetchall()],
            "contention": [dict(r) for r in c.execute(
                "SELECT * FROM disposition_contention ORDER BY id DESC").fetchall()],
            "deviations": [dict(r) for r in c.execute(
                "SELECT * FROM disposition_deviations ORDER BY id DESC").fetchall()]}


@app.get("/api/dispositions/integrity")
def dispositions_integrity(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return integrity_report(c)


@app.get("/api/dispositions/spec")
def dispositions_spec(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM disposition_spec WHERE id=1").fetchone()
    if not row:
        raise HTTPException(500, "超限处置说明未钉死")
    file_sha = sha(SPEC_PATH.read_text(encoding="utf-8")) if SPEC_PATH.exists() else None
    return {"path": row["path"], "statement": row["statement"], "pinned_sha": row["pinned_sha"],
            "pinned_at": row["pinned_at"], "file_sha": file_sha,
            "pinned": file_sha is None or file_sha == row["pinned_sha"]}


@app.get("/api/board")
def board(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return build_board(c)


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
