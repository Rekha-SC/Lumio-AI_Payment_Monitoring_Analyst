"""Simulator service: loads three weeks of history, then streams the 10-day dataset bucket by bucket.

Run on its own with `python -m lumio.simulator` (Docker Compose does this), or inside run_local.py.
"""
import json, time, logging
from datetime import timedelta, datetime
from sqlalchemy import insert, delete, select, func
from . import config, db, world, ingest

log = logging.getLogger("lumio.simulator")
STEP = timedelta(minutes=config.BUCKET_MIN)

def load_history():
    """Three weeks of normal operation (with paydays, weekends and messy data) — the model learns 'normal' here."""
    t0 = time.time()
    ts = config.HISTORY_START
    m_batch, s_batch = [], []
    with db.engine().begin() as c:
        c.execute(delete(db.metrics))
        c.execute(delete(db.service_metrics))
    while ts < config.LIVE_START:
        b = world.generate_bucket(ts, [], live=False, samples=False)
        for r in b["metrics"]:
            r["source"] = "history"
        m_batch += b["metrics"]; s_batch += b["services"]
        if len(m_batch) > 20000:
            with db.engine().begin() as c:
                c.execute(insert(db.metrics), m_batch); c.execute(insert(db.service_metrics), s_batch)
            m_batch, s_batch = [], []
        ts += STEP
    with db.engine().begin() as c:
        if m_batch:
            c.execute(insert(db.metrics), m_batch); c.execute(insert(db.service_metrics), s_batch)
        db.set_state(c, history_ready=True, sim_time=config.LIVE_START, last_scan=config.LIVE_START)
    log.info("history loaded in %.1fs", time.time() - t0)

def reset_live(keep_learning=True):
    """Start the 10 days again: clear live data and incidents, restore the storyline."""
    with db.engine().begin() as c:
        for t in db.LIVE_TABLES:
            if "ts" in t.c and t.name in ("service_metrics", "bucket_scores"):
                c.execute(delete(t).where(t.c.ts >= config.LIVE_START))
            else:
                c.execute(delete(t))
        c.execute(delete(db.metrics).where(db.metrics.c.ts >= config.LIVE_START))
        if not keep_learning:
            c.execute(delete(db.sensitivity))
        st = db.state(c)
        db.set_state(c, sim_time=config.LIVE_START, last_scan=config.LIVE_START, running=False,
                     faults=json.dumps(world.story_faults()), run_id=(st.get("run_id") or 1) + 1)
        for g in world.story_truth():
            c.execute(insert(db.ground_truth).values(name=g["name"], kind=g["kind"], start_ts=g["start_ts"],
                      end_ts=g["end_ts"], scope=json.dumps(g["scope"]), cause=json.dumps(g["cause"]), custom=False,
                      day=g.get("day"), source=g.get("source")))
        for m in world.maintenance_windows():   # the change calendar is known in advance
            c.execute(insert(db.maintenance).values(start_ts=m["start"], end_ts=m["end"], scope=json.dumps(m["scope"]),
                                                    change=m["change"], note=m["note"]))
        db.set_state(c, turbo_until=None)
        db.log_event(c, config.LIVE_START, "system", "simulator", "Reset — Friday 7 February 2025, 00:00. Ten days ahead: six business days and two weekends. Press play.")

def inject(effect, scope):
    """Add a fault from the dashboard (or a test) starting 5 simulated minutes from now. Lumio is not told."""
    with db.engine().begin() as c:
        st = db.state(c); now = st["sim_time"]
        f = world.custom_fault(effect, scope, now)
        faults = json.loads(st["faults"] or "[]"); faults.append(f)
        db.set_state(c, faults=json.dumps(faults))
        c.execute(insert(db.ground_truth).values(name=f["name"], kind="data_quality" if f["kind"] == "dq_gap" else "incident",
                  start_ts=datetime.fromisoformat(f["start"]), end_ts=datetime.fromisoformat(f["end"]), scope=json.dumps(scope),
                  cause=json.dumps(f["cause"]), custom=True))
        db.log_event(c, now, "truth", "presenter", f"Presenter injected: {f['name']} (from {datetime.fromisoformat(f['start']):%H:%M}). Lumio is not told.")
    return f

def _post(payload):
    import httpx
    r = httpx.post(config.INGEST_URL.rstrip("/") + "/api/ingest", json=ingest.to_json(payload), timeout=30)
    r.raise_for_status()

def step_once():
    """Deliver the next 5-minute bucket. Returns False when the evening is over."""
    st = db.state()
    ts = st["sim_time"]
    if ts is None or ts > config.LIVE_END:
        return False
    faults = json.loads(st["faults"] or "[]")
    b = world.generate_bucket(ts, faults, live=True, samples=True)
    with db.engine().begin() as c:
        for f in faults:   # deploy feed: releases and rollbacks become visible when they happen
            if f["kind"] != "release":
                continue
            st_ = datetime.fromisoformat(f["start"])
            if ts <= st_ < ts + STEP:
                c.execute(insert(db.releases).values(ts=st_, bank=f["bank"], platform=f["platform"], component="mobile app",
                                                     version=f["version"], note=f["note"]))
            rb = datetime.fromisoformat(f["rollback_at"]) if f.get("rollback_at") else None
            if rb and ts <= rb < ts + STEP and not (f.get("paused_at") and datetime.fromisoformat(f["paused_at"]) <= rb):
                db.log_event(c, rb, "deploy", "feed", f"Deploy feed: {f['bank']} {f['platform']} app v{f['version']} rolled back by the release team.")
        for et, kind, msg, meta in world.feed_events():
            if ts <= et < ts + STEP:
                db.log_event(c, et, kind, "feed", msg, meta)
    if config.INGEST_URL:
        _post(b)
    else:
        ingest.store(b)
    with db.engine().begin() as c:
        db.set_state(c, sim_time=ts + STEP)
    return True

def run_forever(stop=None):
    db.init_db()
    st = db.state()
    if not st.get("history_ready"):
        log.info("loading three weeks of history…")
        load_history()
        reset_live()
    while not (stop and stop.is_set()):
        st = db.state()
        if st.get("running") and st["sim_time"] and st["sim_time"] <= config.LIVE_END:
            # do not run ahead of the scanner by more than one scan window
            if st["last_scan"] and st["sim_time"] - st["last_scan"] > timedelta(minutes=config.SCAN_MIN + config.BUCKET_MIN):
                time.sleep(0.05); continue
            turbo = st.get("turbo_until")
            if turbo and st["sim_time"] >= turbo:
                if st["last_scan"] and st["sim_time"] - st["last_scan"] >= timedelta(minutes=config.SCAN_MIN):
                    time.sleep(0.05); continue    # let the scanner catch up before handing over
                with db.engine().begin() as c:     # arrived: pause so the presenter can take over
                    db.set_state(c, turbo_until=None, running=False)
                    db.log_event(c, st["sim_time"], "system", "control", f"Arrived at {st['sim_time']:%a %H:%M} — paused. Press play to continue.")
                continue
            t0 = time.time()
            if not step_once():
                with db.engine().begin() as c:
                    db.set_state(c, running=False)
            if not turbo:
                wait = config.SECONDS_PER_BUCKET / max(0.1, st.get("speed") or 1) - (time.time() - t0)
                time.sleep(max(0.02, wait))
        else:
            if st["sim_time"] and st["sim_time"] > config.LIVE_END and st.get("running"):
                with db.engine().begin() as c:
                    db.set_state(c, running=False)
                    db.log_event(c, config.LIVE_END, "system", "simulator", "End of the 10-day dataset — feed stopped.")
            time.sleep(0.2)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    while True:
        try:
            run_forever(); break
        except Exception as e:   # database not ready yet, etc.
            log.warning("simulator retrying: %s", e); time.sleep(3)
