"""API service: dashboard, live state, controls, settings, outbox, reports, dataset, and the email action links."""
import os, io, json, threading, time, logging, zipfile
from datetime import timedelta, datetime
from html import escape
import numpy as np, pandas as pd
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, and_, insert, update, desc
from . import config, db, ingest, world, simulator, worker, proof, tools, agent, settings, mailer, reports, nab
from .model import mix_p95
from .detector import ALL_SEGS
from .settings import DEPARTMENTS

log = logging.getLogger("lumio.api")
UI = os.path.join(config.ROOT, "ui")
MODE = os.environ.get("LUMIO_MODE", "all")       # all = simulator + worker run inside this process
app = FastAPI(title="Lumio — live demo", version="2.0")
_stop = threading.Event()

@app.on_event("startup")
def startup():
    db.init_db()
    if MODE == "all":
        threading.Thread(target=simulator.run_forever, args=(_stop,), daemon=True, name="simulator").start()
        threading.Thread(target=worker.run_forever, args=(_stop,), daemon=True, name="worker").start()

@app.on_event("shutdown")
def shutdown():
    _stop.set()

app.mount("/ui", StaticFiles(directory=UI), name="ui")

@app.get("/")
def index():
    return FileResponse(os.path.join(UI, "index.html"))

def _j(v, default=None):
    try:
        return json.loads(v) if v else default
    except Exception:
        return default

def _day_info(sim):
    idx = (sim.date() - config.LIVE_START.date()).days + 1
    days = reports.days()
    cur = days[min(max(idx, 1), config.DAYS) - 1]
    return {"index": min(max(idx, 1), config.DAYS), "of": config.DAYS, "business_day": cur.get("business_day"), "business": cur["business"],
            "label": cur["label"], "payday": cur["payday"]}

# ------------------------------------------------------------------------------------------------ live state
@app.get("/api/state")
def get_state():
    st = db.state()
    sim = st.get("sim_time") or config.LIVE_START
    with db.engine().connect() as c:
        last_b = c.execute(select(func.max(db.bucket_scores.c.ts)).where(db.bucket_scores.c.ts >= config.LIVE_START)).scalar()
        kpi = {}
        if last_b:
            b = pd.read_sql(select(db.bucket_scores).where(db.bucket_scores.c.ts == last_b), c)
            if len(b):
                kpi = {"payments_per_min": round(b.volume.sum() / config.BUCKET_MIN),
                       "normal_per_min": round(float(b.exp_volume.sum()) / config.BUCKET_MIN),
                       "success": round(float(np.average(b.success, weights=b.volume)) * 100, 2),
                       "exp_success": round(float(np.average(b.exp_success, weights=b.volume)) * 100, 2),
                       "p95_s": round(float(mix_p95(b.p95_ms.values[None], b.volume.values[None])[0]) / 1000, 2),
                       "exp_p95_s": round(float(mix_p95(b.exp_p95_ms.values[None], b.volume.values[None])[0]) / 1000, 2),
                       "bucket": last_b.strftime("%H:%M")}
        last_w = c.execute(select(func.max(db.window_scores.c.ts)).where(db.window_scores.c.ts >= config.LIVE_START)).scalar()
        grid = []
        if last_w:
            for r in c.execute(select(db.window_scores).where(db.window_scores.c.ts == last_w)).mappings():
                grid.append({"seg": r["seg"], "status": r["status"], "flags": r["flags"] or "",
                             "z": round(max(r["z_lat"] or 0, -(r["z_succ"] or 0), abs(r["z_vol"] or 0) * 0.75), 1),
                             "p95": round((r["p95_ms"] or 0) / 1000, 2), "exp": round((r["exp_p95_ms"] or 0) / 1000, 2),
                             "succ": round((r["success"] or 0) * 100, 1), "vol": r["volume"], "exp_vol": round(r["exp_volume"] or 0),
                             "naive": bool(r["naive_flag"])})
        delta = {r.seg: r.threshold for r in c.execute(select(db.sensitivity))}
        incs = [_inc_summary(dict(r)) for r in c.execute(select(db.incidents).order_by(desc(db.incidents.c.id))).mappings()]
        evs = [{"id": e["id"], "ts": e["ts"].strftime("%H:%M") if e["ts"] else "", "day": e["ts"].strftime("%a") if e["ts"] else "",
                "kind": e["kind"], "source": e["source"], "message": e["message"], "meta": _j(e["meta"], {})}
               for e in c.execute(select(db.events).where(db.events.c.kind != "truth").order_by(desc(db.events.c.id)).limit(80)).mappings()]
        scans = c.execute(select(func.count(), func.sum(db.scans.c.naive_alerts))).first()
        cost = c.execute(select(func.sum(db.cost_log.c.cost_usd), func.sum(db.cost_log.c.llm_calls))).first()
        mail = dict(c.execute(select(db.outbox.c.status, func.count()).group_by(db.outbox.c.status)).all())
    rules = settings.get("rules"); email = settings.get("email")
    for g in grid:
        g["threshold"] = rules["sensitivity"] + delta.get(g["seg"], 0)
    total = (config.LIVE_END - config.LIVE_START).total_seconds() + 300
    return {"clock": sim.strftime("%a %d %b %Y · %H:%M"), "time": sim.strftime("%H:%M"), "date": sim.strftime("%a %d %b"),
            "sim_iso": sim.isoformat(), "running": bool(st.get("running")), "speed": st.get("speed") or 1, "speeds": config.SPEEDS,
            "turbo_until": st["turbo_until"].strftime("%a %H:%M") if st.get("turbo_until") else None,
            "progress": max(0, min(1, (sim - config.LIVE_START).total_seconds() / total)), "day": _day_info(sim),
            "history_ready": bool(st.get("history_ready")), "model_ready": bool(st.get("model_ready")),
            "ended": sim > config.LIVE_END, "agent_mode": "claude" if agent.llm_available() else "offline",
            "last_scan": st["last_scan"].strftime("%H:%M") if st.get("last_scan") else None,
            "next_scan": (st["last_scan"] + timedelta(minutes=config.SCAN_MIN)).strftime("%H:%M") if st.get("last_scan") else None,
            "kpi": kpi, "grid": grid, "grid_ts": last_w.strftime("%a %H:%M") if last_w else None,
            "incidents": incs, "events": evs, "scans": scans[0] or 0, "naive_pages": int(scans[1] or 0),
            "cost_usd": round(cost[0] or 0, 4), "llm_calls": int(cost[1] or 0),
            "email": {"enabled": bool(email.get("enabled")), "configured": bool(email.get("host") and (email.get("demo_inbox") or any(d.get("email") for d in settings.get("departments").values()))),
                      "sent": mail.get("sent", 0), "failed": mail.get("failed", 0), "preview": mail.get("preview", 0), "queued": mail.get("queued", 0)},
            "rules": rules, "dims": {"banks": config.BANKS, "channels": config.CHANNELS, "platforms": config.PLATFORMS, "regions": config.REGIONS},
            "payday_evening": world.calendar(sim)[3], "days": reports.days()}

def _inc_summary(r):
    a = _j(r["alert"], {}) or {}
    sc = _j(r["scope"], {}) or {}
    route = _j(r["routing"], {}) or {}
    return {"id": r["id"], "kind": r["kind"] or "incident", "status": r["status"], "severity": r["severity"],
            "headline": r["headline"] or f"{r['signature']} — investigating…",
            "opened": r["opened_ts"].strftime("%H:%M") if r["opened_ts"] else None, "opened_day": r["opened_ts"].strftime("%a %d") if r["opened_ts"] else None,
            "started": r["started_ts"].strftime("%H:%M") if r["started_ts"] else None,
            "resolved": r["resolved_ts"].strftime("%a %H:%M") if r["resolved_ts"] else None,
            "summary": a.get("summary"), "tiles": a.get("tiles", []), "likely_cause": a.get("likely_cause"),
            "next_step": a.get("next_step"), "confidence": r["confidence"], "feedback": r["feedback"],
            "explained_by": r["explained_by"], "scope": sc.get("filter", {}), "segments": sc.get("segments", []),
            "scope_label": tools.scope_label(sc.get("filter", {})), "updated": a.get("updated"), "metric": r["metric"],
            "teams": [DEPARTMENTS[d]["name"] for d in route.get("to", [])], "cc": [DEPARTMENTS[d]["name"] for d in route.get("cc", [])],
            "acked_by": r["acked_by"], "acked": r["acked_ts"].strftime("%H:%M") if r["acked_ts"] else None, "escalated": bool(r["escalated"]),
            "actions": _j(r["actions"], []), "ctas": route.get("ctas", []), "token": r["token"]}

# ------------------------------------------------------------------------------------------------ chart series
@app.get("/api/series")
def series(bank: str = "", channel: str = "", platform: str = "", region: str = "", hours: float = 6):
    scope = {k: v for k, v in dict(bank=bank, channel=channel, platform=platform, region=region).items() if v}
    segs = tools.segs_matching(scope, ALL_SEGS)
    st = db.state(); sim = st.get("sim_time") or config.LIVE_START
    since = max(config.LIVE_START - timedelta(hours=3), sim - timedelta(hours=hours))
    with db.engine().connect() as c:
        bs = pd.read_sql(select(db.bucket_scores).where(and_(db.bucket_scores.c.ts >= since, db.bucket_scores.c.ts < sim,
                                                             db.bucket_scores.c.seg.in_(segs))), c)
        incs = [dict(r) for r in c.execute(select(db.incidents).where(db.incidents.c.opened_ts >= since)).mappings()]
        rel = [dict(r) for r in c.execute(select(db.releases).where(db.releases.c.ts >= since)).mappings()]
    pts = []
    if len(bs):
        bs = bs.sort_values("ts")
        g = bs.groupby("ts", sort=True)
        idx, part = g.ngroup().values, g.cumcount().values
        G, K = idx.max() + 1, part.max() + 1
        def mat(col):
            M = np.ones((G, K)); M[idx, part] = bs[col].fillna(bs["band_hi"] if col == "alert_hi" else 1.0).values; return M
        V = np.zeros((G, K)); V[idx, part] = bs.volume.values
        p95, exp, lo, hi, al = (mix_p95(mat(c), V) / 1000 for c in ("p95_ms", "exp_p95_ms", "band_lo", "band_hi", "alert_hi"))
        agg = g.apply(lambda d: pd.Series({"succ": np.average(d.success, weights=d.volume), "exp_succ": np.average(d.exp_success, weights=d.volume),
                                            "vol": d.volume.sum(), "exp_vol": d.exp_volume.fillna(0).sum()}), include_groups=False)
        for k, (ts, r) in enumerate(agg.iterrows()):
            pts.append({"t": ts.strftime("%H:%M"), "p95": round(float(p95[k]), 3), "exp": round(float(exp[k]), 3), "lo": round(float(lo[k]), 3),
                        "hi": round(float(hi[k]), 3), "alert": round(float(al[k]), 3), "succ": round(float(r.succ) * 100, 2),
                        "exp_succ": round(float(r.exp_succ) * 100, 2), "vol": round(float(r.vol) / config.BUCKET_MIN, 1),
                        "exp_vol": round(float(r.exp_vol) / config.BUCKET_MIN, 1)})
    marks = []
    for i in incs:
        isegs = set(_j(i["scope"], {}).get("segments", []))
        if isegs & set(segs) and i["kind"] == "incident":
            marks.append({"t": i["opened_ts"].strftime("%H:%M"), "label": f"#{i['id']} alert", "kind": "alert"})
            if i["started_ts"] and i["started_ts"] >= since:
                marks.append({"t": i["started_ts"].strftime("%H:%M"), "label": f"#{i['id']} began", "kind": "start"})
    for r in rel:
        if (not bank or r["bank"] == bank) and (not platform or r["platform"] == platform):
            marks.append({"t": (r["ts"] - timedelta(minutes=r["ts"].minute % 5)).strftime("%H:%M"), "label": f"v{r['version']} release", "kind": "release"})
    rules = settings.get("rules")
    return {"scope": scope, "label": tools.scope_label(scope), "segments": len(segs), "points": pts, "marks": marks,
            "guard": {"latency": rules["latency_ceiling"], "success": rules["success_floor"]}}

# ------------------------------------------------------------------------------------------------ incidents
@app.get("/api/incidents/{iid}")
def incident(iid: int):
    with db.engine().connect() as c:
        r = c.execute(select(db.incidents).where(db.incidents.c.id == iid)).mappings().first()
        if not r:
            raise HTTPException(404)
        steps = [dict(s) for s in c.execute(select(db.investigation_steps).where(db.investigation_steps.c.incident_id == iid)
                                            .order_by(db.investigation_steps.c.id)).mappings()]
        cost = [dict(x) for x in c.execute(select(db.cost_log).where(db.cost_log.c.incident_id == iid)).mappings()]
        mails = [dict(x) for x in c.execute(select(db.outbox.c.id, db.outbox.c.ts, db.outbox.c.kind, db.outbox.c.subject, db.outbox.c.status,
                                                   db.outbox.c.to_addrs, db.outbox.c.cc_addrs).where(db.outbox.c.incident_id == iid).order_by(db.outbox.c.id)).mappings()]
    out = _inc_summary(dict(r))
    out["alert"] = _j(r["alert"], {}); out["cause"] = _j(r["cause"], {}); out["routing"] = _j(r["routing"], {})
    out["steps"] = [{"step": i + 1, "actor": s["actor"], "tool": s["tool"], "input": _j(s["input"], {}), "finding": s["finding"], "ms": s["ms"]} for i, s in enumerate(steps)]
    for x in cost:
        x["ts"] = x["ts"].strftime("%H:%M") if x.get("ts") else None
    out["cost"] = cost
    out["emails"] = [{"id": m["id"], "ts": m["ts"].strftime("%a %H:%M"), "kind": m["kind"], "subject": m["subject"], "status": m["status"],
                      "to": _j(m["to_addrs"], []), "cc": _j(m["cc_addrs"], [])} for m in mails]
    return out

def _record_action(iid, label, by, now, kind="action"):
    with db.engine().begin() as c:
        r = c.execute(select(db.incidents.c.actions).where(db.incidents.c.id == iid)).first()
        acts = _j(r[0], []) if r else []
        acts.append({"label": label, "by": by, "at": now.strftime("%a %H:%M")})
        c.execute(update(db.incidents).where(db.incidents.c.id == iid).values(actions=json.dumps(acts)))
        db.log_event(c, now, kind, "action", f"#{iid} · {label} — by {by} at {now:%H:%M}.", {"incident_id": iid})

def do_action(iid, action, by="dashboard"):
    """Shared by dashboard buttons and email buttons."""
    st = db.state(); now = st["sim_time"]
    with db.engine().connect() as c:
        inc = dict(c.execute(select(db.incidents).where(db.incidents.c.id == iid)).mappings().first())
    cause = _j(inc["cause"], {}) or {}
    msg = ""
    if action == "ack":
        with db.engine().begin() as c:
            if not inc["acked_ts"]:
                c.execute(update(db.incidents).where(db.incidents.c.id == iid).values(acked_by=by, acked_ts=now))
                db.log_event(c, now, "ack", "action", f"#{iid} acknowledged by {by} at {now:%H:%M} — escalation stopped.", {"incident_id": iid})
        msg = f"Acknowledged by {by}. Escalation is stopped."
    elif action == "dismiss":
        _feedback(iid, "dismiss", now)
        msg = "Marked as not useful. Lumio will need a bigger deviation before alerting on these segments again."
    elif action == "runbook" and cause.get("type") == "release":
        faults = _j(st["faults"], [])
        for f in faults:
            if f.get("kind") == "release" and f.get("version") == cause.get("version") and not f.get("paused_at"):
                f["paused_at"] = now.isoformat()
        with db.engine().begin() as c:
            db.set_state(c, faults=json.dumps(faults))
        label = "Rolled back" if cause.get("metric") == "volume" else "Paused"
        _record_action(iid, f"{label} v{cause.get('version')} rollout (runbook)", by, now)
        _ack_if_needed(iid, by, now, inc)
        msg = f"{label} the v{cause.get('version')} rollout. Lumio keeps watching and closes the incident after two normal scans."
    elif action == "block" and cause.get("type") == "fraud":
        faults = _j(st["faults"], [])
        for f in faults:
            if f.get("attack") and world.active(f, now):
                f["end"] = (now + timedelta(minutes=10)).isoformat()
        with db.engine().begin() as c:
            db.set_state(c, faults=json.dumps(faults))
        _record_action(iid, "Blocked the card-testing pattern (fraud rule live)", by, now)
        _ack_if_needed(iid, by, now, inc)
        msg = "Fraud rule is live: payments under $5 from new devices at this bank are blocked. The attack stops within minutes."
    elif action in ("page", "bridge", "ticket"):
        label = {"page": f"Paged the {cause.get('service', 'service')} on-call", "bridge": "Opened a bridge call with Network Operations",
                 "ticket": "Created a data-pipeline ticket"}[action]
        _record_action(iid, label, by, now)
        _ack_if_needed(iid, by, now, inc)
        msg = label + "."
    elif action == "confirm":
        _feedback(iid, "confirm", now)
        msg = "Confirmed as a real incident."
    else:
        raise HTTPException(400, "unknown action")
    return msg

def _ack_if_needed(iid, by, now, inc):
    if not inc["acked_ts"]:
        with db.engine().begin() as c:
            c.execute(update(db.incidents).where(db.incidents.c.id == iid).values(acked_by=by, acked_ts=now))

def _feedback(iid, action, now):
    with db.engine().begin() as c:
        r = c.execute(select(db.incidents).where(db.incidents.c.id == iid)).mappings().first()
        segs = _j(r["scope"], {}).get("segments", [])
        label = tools.scope_label(_j(r["scope"], {}).get("filter", {}))
        change = {}
        if action == "dismiss":
            delta = {x.seg: x.threshold for x in c.execute(select(db.sensitivity))}
            for s in segs:
                old = delta.get(s, 0.0); new = min(4.0, old + 1.0); change[s] = [old, new]
                if s in delta:
                    c.execute(update(db.sensitivity).where(db.sensitivity.c.seg == s).values(threshold=new))
                else:
                    c.execute(insert(db.sensitivity).values(seg=s, threshold=new))
            c.execute(update(db.incidents).where(db.incidents.c.id == iid).values(feedback="dismissed", status="resolved", resolved_ts=now))
            db.log_event(c, now, "learning", "feedback", f"Learning loop: #{iid} dismissed as not useful — {label} now needs a bigger deviation before alerting (+1.0).", {"incident_id": iid})
        else:
            c.execute(update(db.incidents).where(db.incidents.c.id == iid).values(feedback="confirmed"))
            db.log_event(c, now, "learning", "feedback", f"Learning loop: #{iid} confirmed — kept as a true positive.", {"incident_id": iid})
        c.execute(insert(db.feedback).values(incident_id=iid, ts=now, action=action, segments=json.dumps(segs), change=json.dumps(change)))

@app.post("/api/incidents/{iid}/action")
def incident_action(iid: int, body: dict = Body(...)):
    return {"ok": True, "message": do_action(iid, body.get("action"), body.get("by") or "Lumio dashboard")}

# ------------------------------------------------------------------------------------------------ email buttons
PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lumio</title>
<link rel="stylesheet" href="/ui/style.css"></head><body class="act-page"><div class="act-card"><img src="/ui/logo.png" alt="" width="44">
{body}</div></body></html>"""

@app.get("/act/{token}/{action}", response_class=HTMLResponse)
def act_get(token: str, action: str):
    with db.engine().connect() as c:
        inc = c.execute(select(db.incidents).where(db.incidents.c.token == token)).mappings().first()
    if not inc:
        return HTMLResponse(PAGE.format(body="<h1>Link expired</h1><p>This incident belongs to an earlier run of the demo.</p><a class='btn primary' href='/'>Open Lumio</a>"), 404)
    if action == "open":
        return RedirectResponse(f"/#incident={inc['id']}")
    route = _j(inc["routing"], {}) or {}
    teams = route.get("to", []) + route.get("cc", []) + ["duty"]
    from urllib.parse import quote
    who = "".join(f"<a class='btn' href='/act/{token}/{action}/do?by={quote(DEPARTMENTS[d]['name'])}'>{escape(DEPARTMENTS[d]['name'])}</a>" for d in dict.fromkeys(teams))
    label = {"ack": "Acknowledge", "dismiss": "Mark as not useful", "runbook": "Run the rollout runbook", "page": "Page the on-call",
             "bridge": "Open a bridge call", "block": "Turn on the fraud rule", "ticket": "Create the data ticket"}.get(action, action)
    body = (f"<div class='kicker'>INC-{inc['id']:04d}</div><h1>{escape(inc['headline'] or '')}</h1>"
            f"<p>{escape(label)} — who is taking this?</p><div class='act-who'>{who}</div>")
    return HTMLResponse(PAGE.format(body=body))

@app.get("/act/{token}/{action}/do", response_class=HTMLResponse)
def act_do(token: str, action: str, by: str = "email recipient"):
    with db.engine().connect() as c:
        inc = c.execute(select(db.incidents).where(db.incidents.c.token == token)).mappings().first()
    if not inc:
        raise HTTPException(404)
    try:
        msg = do_action(inc["id"], action, f"{by} (from email)")
    except HTTPException:
        msg = "That action does not apply to this incident."
    body = (f"<div class='kicker'>INC-{inc['id']:04d} · done ✓</div><h1>{escape(msg)}</h1><p>{escape(inc['headline'] or '')}</p>"
            f"<a class='btn primary' href='/#incident={inc['id']}'>Open in Lumio</a>")
    return HTMLResponse(PAGE.format(body=body))

# ------------------------------------------------------------------------------------------------ controls
@app.post("/api/control")
def control(body: dict = Body(...)):
    a = body.get("action")
    if a == "reset":
        simulator.reset_live(keep_learning=bool(body.get("keep_learning", False)))
        return {"ok": True}
    with db.engine().begin() as c:
        st = db.state(c)
        if a == "play":
            if not st.get("history_ready"):
                raise HTTPException(409, "history still loading")
            db.set_state(c, running=True)
        elif a == "pause":
            db.set_state(c, running=False, turbo_until=None)
        elif a == "speed":
            db.set_state(c, speed=float(body.get("value", 1)))
        elif a == "jump":
            target = datetime.fromisoformat(body["to"])
            if target <= st["sim_time"]:
                raise HTTPException(400, "Lumio replays forward only — press Reset to go back to the start.")
            db.set_state(c, turbo_until=min(target, config.LIVE_END), running=True)
            db.log_event(c, st["sim_time"], "system", "control", f"Fast-forward to {target:%a %H:%M}… Lumio processes every scan on the way.")
        elif a == "next":
            truth = [r for r in c.execute(select(db.ground_truth.c.start_ts).where(db.ground_truth.c.start_ts > st["sim_time"] + timedelta(minutes=20))
                                          .order_by(db.ground_truth.c.start_ts))]
            target = (truth[0][0] - timedelta(minutes=15)) if truth else config.LIVE_END
            db.set_state(c, turbo_until=target, running=True)
            db.log_event(c, st["sim_time"], "system", "control", f"Skipping ahead to {target:%a %H:%M}…")
        else:
            raise HTTPException(400, "unknown action")
    return {"ok": True}

@app.post("/api/inject")
def inject(body: dict = Body(...)):
    scope = {k: body[k] for k in ("bank", "channel", "platform", "region") if body.get(k)}
    if not scope:
        raise HTTPException(400, "choose at least a bank or region")
    f = simulator.inject(body.get("effect", "slowdown"), scope)
    return {"ok": True, "fault": f}

# ------------------------------------------------------------------------------------------------ settings
@app.get("/api/settings")
def get_settings():
    deps = settings.get("departments")
    for k, v in deps.items():
        v["address"] = settings.address_for(k); v["key"] = k
    return {"rules": settings.get("rules"), "email": settings.public_email(), "departments": deps,
            "presets": settings.PRESETS, "defaults": config.DEFAULT_RULES}

@app.put("/api/settings/{section}")
def put_settings(section: str, body: dict = Body(...)):
    if section not in ("rules", "email", "departments"):
        raise HTTPException(404)
    if section == "email" and not body.get("password"):
        body.pop("password", None)          # keep the stored password unless a new one is typed
    settings.put(section, body)
    st = db.state()
    with db.engine().begin() as c:
        db.log_event(c, st["sim_time"], "system", "settings", f"Settings updated: {section}.")
    return get_settings()

@app.post("/api/settings/test-email")
def test_email(body: dict = Body(...)):
    to = body.get("to") or settings.get("email").get("demo_inbox")
    if not to:
        raise HTTPException(400, "Enter an address to send the test to")
    try:
        mailer.send_test(to)
        return {"ok": True, "message": f"Test email sent to {to}. Check the inbox (and spam folder)."}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}

# ------------------------------------------------------------------------------------------------ outbox
@app.get("/api/outbox")
def outbox():
    with db.engine().connect() as c:
        rows = [dict(r) for r in c.execute(select(db.outbox.c.id, db.outbox.c.ts, db.outbox.c.kind, db.outbox.c.subject, db.outbox.c.status,
                                                  db.outbox.c.error, db.outbox.c.to_addrs, db.outbox.c.cc_addrs, db.outbox.c.incident_id,
                                                  db.outbox.c.departments, db.outbox.c.sent_at).order_by(desc(db.outbox.c.id)).limit(300)).mappings()]
    return [{"id": r["id"], "ts": r["ts"].strftime("%a %d %b %H:%M"), "kind": r["kind"], "subject": r["subject"], "status": r["status"],
             "error": r["error"], "to": _j(r["to_addrs"], []), "cc": _j(r["cc_addrs"], []), "incident_id": r["incident_id"],
             "teams": [DEPARTMENTS[d]["name"] for d in _j(r["departments"], {}).get("to", [])]} for r in rows]

@app.get("/api/outbox/{oid}/html", response_class=HTMLResponse)
def outbox_html(oid: int):
    with db.engine().connect() as c:
        r = c.execute(select(db.outbox).where(db.outbox.c.id == oid)).mappings().first()
    if not r:
        raise HTTPException(404)
    h = r["html"]
    for k, v in (_j(r["images"], {}) or {}).items():
        h = h.replace(f"cid:{k}", f"data:image/png;base64,{v}")
    return HTMLResponse(h)

@app.post("/api/outbox/{oid}/send")
def outbox_send(oid: int):
    with db.engine().begin() as c:
        c.execute(update(db.outbox).where(db.outbox.c.id == oid).values(status="queued", error=None))
    mailer._wake.set()
    return {"ok": True}

@app.post("/api/outbox/send-all-previews")
def outbox_send_all():
    with db.engine().begin() as c:
        n = c.execute(update(db.outbox).where(db.outbox.c.status.in_(["preview", "failed"])).values(status="queued", error=None)).rowcount
    mailer._wake.set()
    return {"ok": True, "queued": n}

# ------------------------------------------------------------------------------------------------ reports
@app.get("/api/reports")
def reports_summary():
    return reports.summary()

@app.get("/api/reports/{d}")
def report_day(d: str):
    return reports.day_report(d)

@app.get("/api/reports/{d}/csv")
def report_csv(d: str):
    return Response(reports.day_csv(d), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=lumio_{d}_segments.csv"})

@app.post("/api/reports/{d}/email")
def report_email(d: str):
    st = db.state()
    oid = mailer.queue_daily_report(datetime.fromisoformat(d).date(), st["sim_time"])
    return {"ok": True, "outbox_id": oid}

# ------------------------------------------------------------------------------------------------ quality, learning, proof
@app.get("/api/quality")
def quality_view():
    with db.engine().connect() as c:
        last = c.execute(select(func.max(db.dq_checks.c.ts))).scalar()
        cur = [dict(r) for r in c.execute(select(db.dq_checks).where(db.dq_checks.c.ts == last)).mappings()] if last else []
        tot = c.execute(select(db.dq_checks.c.status, func.count()).group_by(db.dq_checks.c.status)).all()
        evs = [dict(e) for e in c.execute(select(db.events).where(db.events.c.kind == "data_quality").order_by(desc(db.events.c.id)).limit(100)).mappings()]
        n_metrics = c.execute(select(func.count()).select_from(db.metrics).where(db.metrics.c.ts >= config.LIVE_START)).scalar()
    for r in cur:
        r["ts"] = r["ts"].strftime("%H:%M")
    return {"window": last.strftime("%a %H:%M") if last else None, "segments": cur, "totals": {s: n for s, n in tot},
            "events": [{"ts": e["ts"].strftime("%a %H:%M"), "message": e["message"]} for e in evs], "records": n_metrics}

@app.get("/api/learning")
def learning_view():
    with db.engine().connect() as c:
        sens = [{"seg": r.seg, "delta": r.threshold} for r in c.execute(select(db.sensitivity))]
        evs = [dict(e) for e in c.execute(select(db.events).where(db.events.c.kind.in_(["learning", "action", "ack"])).order_by(desc(db.events.c.id)).limit(100)).mappings()]
    return {"default": settings.get("rules")["sensitivity"], "sensitivity": sens,
            "events": [{"ts": e["ts"].strftime("%a %H:%M"), "kind": e["kind"], "message": e["message"]} for e in evs]}

@app.get("/api/proof")
def proof_view():
    st = db.state()
    with db.engine().connect() as c:
        sb = proof.scoreboard(c, st["sim_time"] or config.LIVE_START)
        cost_rows = [dict(r) for r in c.execute(select(db.cost_log)).mappings()]
        incs = [dict(r) for r in c.execute(select(db.incidents).where(db.incidents.c.kind == "incident").order_by(db.incidents.c.id)).mappings()]
        nsteps = dict(c.execute(select(db.investigation_steps.c.incident_id, func.count()).group_by(db.investigation_steps.c.incident_id)).all())
    per = []
    for i in incs:
        rows = [r for r in cost_rows if r["incident_id"] == i["id"]]
        per.append({"id": i["id"], "headline": i["headline"], "explained_by": i["explained_by"], "steps": nsteps.get(i["id"], 0),
                    "llm_calls": sum(r["llm_calls"] or 0 for r in rows), "tokens": sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in rows),
                    "usd": round(sum(r["cost_usd"] or 0 for r in rows), 4), "ms": round(sum(r["duration_ms"] or 0 for r in rows))})
    sb["per_incident"] = per
    sb["stress_test"] = {"rows": [{"name": "Lumio redesigned (statistics + data-quality gate + peer check)", "caught": 100},
                                  {"name": "Statistical rule alone", "caught": 96}, {"name": "Original combined design", "caught": 80},
                                  {"name": "Forecast model (ML) alone", "caught": 80}, {"name": "Isolation Forest (ML)", "caught": 4}]}
    return sb

# ------------------------------------------------------------------------------------------------ dataset & architecture
DATASET_DIR = os.path.join(config.DATA_DIR, "dataset")

@app.get("/api/dataset")
def dataset_info():
    files = []
    if os.path.isdir(DATASET_DIR):
        for f in sorted(os.listdir(DATASET_DIR)):
            files.append({"name": f, "size_kb": round(os.path.getsize(os.path.join(DATASET_DIR, f)) / 1024)})
    feed = [{"ts": t.strftime("%a %d %b %H:%M"), "kind": k, "message": m} for t, k, m, _ in world.feed_events()]
    return {"files": files, "nab": nab.sources(), "feed": feed, "days": reports.days(), "start": config.LIVE_START.isoformat(),
            "end": config.LIVE_END.isoformat(), "segments": len(ALL_SEGS), "bucket_min": config.BUCKET_MIN}

@app.get("/api/dataset/{name}")
def dataset_file(name: str):
    p = os.path.join(DATASET_DIR, os.path.basename(name))
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, filename=os.path.basename(name))

@app.get("/api/architecture")
def architecture():
    with db.engine().connect() as c:
        q = lambda t, *w: c.execute(select(func.count()).select_from(t).where(*w) if w else select(func.count()).select_from(t)).scalar()
        counts = {"history_rows": q(db.metrics, db.metrics.c.ts < config.LIVE_START), "live_rows": q(db.metrics, db.metrics.c.ts >= config.LIVE_START),
                  "samples": q(db.txn_samples), "service_rows": q(db.service_metrics), "scans": q(db.scans), "dq_checks": q(db.dq_checks),
                  "incidents": q(db.incidents), "steps": q(db.investigation_steps), "events": q(db.events), "emails": q(db.outbox),
                  "feedback": q(db.feedback)}
    from .model import Baseline
    m = Baseline.load()
    return {"database": "PostgreSQL" if config.DATABASE_URL.startswith("postgres") else "SQLite", "mode": MODE,
            "agent": "Claude (" + config.LLM_MODEL + ")" if agent.llm_available() else "Offline planner (no API key)",
            "model": {"windows": getattr(m, "n_train", None), "train_s": round(getattr(m, "train_s", 0) or 0, 1)} if m else None,
            "counts": counts, "segments": len(ALL_SEGS), "scan_min": config.SCAN_MIN, "bucket_min": config.BUCKET_MIN}

@app.post("/api/ingest")
def ingest_endpoint(payload: dict = Body(...)):
    return ingest.store(payload)

@app.get("/api/health")
def health():
    st = db.state()
    return {"ok": True, "history_ready": bool(st.get("history_ready")), "model_ready": bool(st.get("model_ready"))}
