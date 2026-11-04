"""Database schema and helpers (SQLAlchemy Core, works on SQLite and PostgreSQL)."""
import json
from sqlalchemy import (create_engine, MetaData, Table, Column, Integer, BigInteger, Float, String, Text,
                        DateTime, Boolean, Index, select, delete, update, insert, func, text)
from . import config

metadata = MetaData()

sim_state = Table("sim_state", metadata,
    Column("id", Integer, primary_key=True),
    Column("sim_time", DateTime),            # end of the last bucket that has been delivered
    Column("last_scan", DateTime),           # end of the last 15-minute window Lumio scanned
    Column("running", Boolean, default=False),
    Column("speed", Float, default=1.0),
    Column("faults", Text, default="[]"),    # active/scheduled faults (JSON)
    Column("history_ready", Boolean, default=False),
    Column("model_ready", Boolean, default=False),
    Column("run_id", Integer, default=1),
    Column("turbo_until", DateTime),
)

metrics = Table("metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),          # bucket start
    Column("bank", String(40)), Column("channel", String(40)), Column("platform", String(40)),
    Column("region", String(40)), Column("app_version", String(40)),
    Column("volume", Integer), Column("success_count", Integer), Column("p95_ms", Float),
    Column("avg_amount", Float), Column("received_at", DateTime), Column("source", String(40)),
)
Index("ix_metrics_ts", metrics.c.ts)

service_metrics = Table("service_metrics", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False), Column("service", String(40)),
    Column("p95_ms", Float), Column("error_rate", Float),
)
Index("ix_service_ts", service_metrics.c.ts)

txn_samples = Table("txn_samples", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("bank", String(40)), Column("channel", String(40)), Column("platform", String(40)),
    Column("region", String(40)), Column("app_version", String(40)),
    Column("amount", Float), Column("merchant_category", String(40)),
    Column("latency_ms", Float), Column("status", String(40)),
)
Index("ix_samples_ts", txn_samples.c.ts)

releases = Table("releases", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("bank", String(40)), Column("platform", String(40)),
    Column("component", String(40)), Column("version", String(40)), Column("note", Text),
)

events = Table("events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("kind", String(40)), Column("source", String(40)),
    Column("message", Text), Column("meta", Text, default="{}"),
)

ground_truth = Table("ground_truth", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(60)), Column("kind", String(40)),   # incident | not_incident | data_quality
    Column("start_ts", DateTime), Column("end_ts", DateTime),
    Column("scope", Text), Column("cause", Text), Column("custom", Boolean, default=False),
    Column("day", Integer), Column("source", String(60)),
)

bucket_scores = Table("bucket_scores", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("seg", String(48)),
    Column("volume", Integer), Column("p95_ms", Float), Column("success", Float),
    Column("exp_p95_ms", Float), Column("exp_success", Float),
    Column("band_lo", Float), Column("band_hi", Float), Column("status", String(40)),
    Column("exp_volume", Float), Column("alert_hi", Float),
)
Index("ix_bscores_ts", bucket_scores.c.ts)

window_scores = Table("window_scores", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("seg", String(48)),
    Column("volume", Integer), Column("p95_ms", Float), Column("success", Float),
    Column("exp_p95_ms", Float), Column("exp_success", Float),
    Column("z_lat", Float), Column("z_succ", Float), Column("status", String(40)), Column("naive_flag", Boolean),
    Column("exp_volume", Float), Column("z_vol", Float), Column("flags", String(40)),
)
Index("ix_wscores_ts", window_scores.c.ts)

scans = Table("scans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("scored", Integer), Column("abnormal", Integer),
    Column("dq_hold", Integer), Column("naive_alerts", Integer), Column("suppressed", Integer),
    Column("duration_ms", Float), Column("note", Text),
)

dq_checks = Table("dq_checks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("seg", String(48)),
    Column("completeness", Float), Column("duplicates", Integer), Column("stale_min", Float),
    Column("status", String(40)), Column("note", Text),
)
Index("ix_dq_ts", dq_checks.c.ts)

incidents = Table("incidents", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signature", String(160)), Column("status", String(40)),     # open | resolved
    Column("severity", String(40)),
    Column("opened_ts", DateTime), Column("started_ts", DateTime), Column("last_ts", DateTime),
    Column("resolved_ts", DateTime), Column("normal_scans", Integer, default=0),
    Column("scope", Text), Column("cause", Text), Column("alert", Text),
    Column("headline", Text), Column("narrative", Text), Column("confidence", Float),
    Column("feedback", String(40)), Column("explained_by", String(40)),
    Column("kind", String(40), default="incident"),          # incident | advisory | data_quality
    Column("metric", String(40)), Column("routing", Text), Column("token", String(48)),
    Column("acked_by", String(80)), Column("acked_ts", DateTime), Column("escalated", Boolean, default=False),
    Column("actions", Text, default="[]"),
)

investigation_steps = Table("investigation_steps", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("incident_id", Integer), Column("step", Integer), Column("actor", String(40)),
    Column("tool", String(40)), Column("input", Text), Column("finding", Text),
    Column("output", Text), Column("ms", Float),
)

cost_log = Table("cost_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("incident_id", Integer), Column("ts", DateTime), Column("actor", String(40)),
    Column("purpose", String(40)), Column("llm_calls", Integer), Column("tokens_in", Integer),
    Column("tokens_out", Integer), Column("cost_usd", Float), Column("duration_ms", Float),
)

feedback = Table("feedback", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("incident_id", Integer), Column("ts", DateTime), Column("action", String(40)),
    Column("segments", Text), Column("change", Text),
)

sensitivity = Table("sensitivity", metadata,
    Column("seg", String(48), primary_key=True), Column("threshold", Float),
)

settings = Table("settings", metadata,
    Column("key", String(60), primary_key=True), Column("value", Text),
)

outbox = Table("outbox", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("sent_at", DateTime), Column("incident_id", Integer), Column("kind", String(40)),
    Column("to_addrs", Text), Column("cc_addrs", Text), Column("subject", Text), Column("html", Text), Column("text", Text),
    Column("status", String(40)), Column("error", Text), Column("departments", Text), Column("images", Text),
)

maintenance = Table("maintenance", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("start_ts", DateTime), Column("end_ts", DateTime), Column("scope", Text), Column("change", String(40)), Column("note", Text),
)

_engine = None

def engine():
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        kw = {"future": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kw["pool_pre_ping"] = True
        _engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            with _engine.begin() as c:
                c.exec_driver_sql("PRAGMA journal_mode=WAL")
    return _engine

def init_db():
    metadata.create_all(engine())
    with engine().begin() as c:
        if c.execute(select(sim_state.c.id)).first() is None:
            c.execute(insert(sim_state).values(id=1, sim_time=None, last_scan=None, running=False, speed=1.0,
                                               faults="[]", history_ready=False, model_ready=False, run_id=1))

def state(conn=None):
    def q(c):
        r = c.execute(select(sim_state).where(sim_state.c.id == 1)).mappings().first()
        return dict(r) if r else {}
    if conn is not None:
        return q(conn)
    with engine().connect() as c:
        return q(c)

def set_state(conn, **kw):
    conn.execute(update(sim_state).where(sim_state.c.id == 1).values(**kw))

def log_event(conn, ts, kind, source, message, meta=None):
    conn.execute(insert(events).values(ts=ts, kind=kind, source=source, message=message,
                                       meta=json.dumps(meta or {})))

def seg_key(bank, channel, platform, region):
    return f"{bank}|{channel}|{platform}|{region}"

def seg_parts(key):
    b, c, p, r = key.split("|")
    return {"bank": b, "channel": c, "platform": p, "region": r}

LIVE_TABLES = [service_metrics, txn_samples, releases, events, ground_truth, bucket_scores, window_scores,
               scans, dq_checks, incidents, investigation_steps, cost_log, feedback, outbox, maintenance]
