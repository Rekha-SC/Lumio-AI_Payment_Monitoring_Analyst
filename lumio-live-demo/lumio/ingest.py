"""Ingestion service: accepts metric batches from monitoring collectors and stores them unchanged.

Validation here is structural only (types, required fields). Judging whether the data is complete or
trustworthy is the data-quality gate's job, at scan time.
"""
from datetime import datetime
from sqlalchemy import insert
from . import db

REQUIRED = ("ts", "bank", "channel", "platform", "region", "app_version", "volume", "success_count", "p95_ms")

def _ts(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)

def store(payload, conn=None, source="collector"):
    now = datetime.utcnow()
    m = []
    for r in payload.get("metrics", []):
        if any(k not in r for k in REQUIRED):
            continue
        m.append({**{k: r[k] for k in REQUIRED}, "ts": _ts(r["ts"]), "avg_amount": r.get("avg_amount"),
                  "received_at": now, "source": source})
    s = [{"ts": _ts(r["ts"]), "service": r["service"], "p95_ms": r["p95_ms"], "error_rate": r.get("error_rate", 0)}
         for r in payload.get("services", [])]
    t = [{**r, "ts": _ts(r["ts"])} for r in payload.get("samples", [])]

    def run(c):
        if m: c.execute(insert(db.metrics), m)
        if s: c.execute(insert(db.service_metrics), s)
        if t: c.execute(insert(db.txn_samples), t)
    if conn is not None:
        run(conn)
    else:
        with db.engine().begin() as c:
            run(c)
    return {"metrics": len(m), "services": len(s), "samples": len(t)}

def to_json(payload):
    def conv(rows):
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]
    return {"ts": payload["ts"].isoformat(), "metrics": conv(payload["metrics"]),
            "services": conv(payload["services"]), "samples": conv(payload["samples"])}
