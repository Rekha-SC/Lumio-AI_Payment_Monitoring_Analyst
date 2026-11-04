"""Scoreboard: compares what Lumio did with the answer key (ground truth) the dataset carries."""
import json
from datetime import timedelta
from sqlalchemy import select
from . import db
from .detector import ALL_SEGS
from .tools import segs_matching

EXPECT = {"incident": "Alert to the right team, correct cause", "data_quality": "Hold bad data · ticket, no page",
          "not_incident": "No alert", "advisory": "One advisory, no segment pages", "transient": "Watch, don't page"}

def scoreboard(conn, now):
    truth = [dict(r) for r in conn.execute(select(db.ground_truth).order_by(db.ground_truth.c.start_ts)).mappings()]
    incs = [dict(r) for r in conn.execute(select(db.incidents).order_by(db.incidents.c.id)).mappings()]
    real = [i for i in incs if i["kind"] == "incident"]
    holds = conn.execute(select(db.dq_checks.c.ts, db.dq_checks.c.seg, db.dq_checks.c.status).where(db.dq_checks.c.status.in_(["hold", "dedup"]))).all()
    iseg = {i["id"]: set(json.loads(i["scope"])["segments"]) for i in incs}
    tseg = lambda t: set(segs_matching(json.loads(t["scope"]), ALL_SEGS)) if json.loads(t["scope"]) else set(ALL_SEGS)
    pairs = []
    for ti, t in enumerate(truth):
        if t["kind"] != "incident":
            continue
        ts_ = tseg(t)
        for i in real:
            inside = len(iseg[i["id"]] & ts_) / max(1, len(iseg[i["id"]]))
            if inside >= 0.5 and t["start_ts"] <= i["opened_ts"] <= (t["end_ts"] or now) + timedelta(minutes=30):
                pairs.append((len(iseg[i["id"]] & ts_) / len(iseg[i["id"]] | ts_), ti, i["id"]))
    assign, used = {}, set()
    for score, ti, iid in sorted(pairs, reverse=True):
        if ti not in assign and iid not in used:
            assign[ti] = iid; used.add(iid)
    by_id = {i["id"]: i for i in incs}
    rows = []
    for ti, t in enumerate(truth):
        cause = json.loads(t["cause"]); ts_ = tseg(t)
        started, over = t["start_ts"] <= now, now >= (t["end_ts"] or now) + timedelta(minutes=30)
        r = {"name": t["name"], "kind": t["kind"], "day": t["day"], "date": t["start_ts"].strftime("%a %d %b"), "start": t["start_ts"].strftime("%H:%M"),
             "end": t["end_ts"].strftime("%H:%M") if t["end_ts"] else None, "expected": EXPECT[t["kind"]], "outcome": "", "status": "pending",
             "detect_min": None, "incident_id": None, "custom": bool(t["custom"]), "source": t.get("source"), "teams": []}
        in_window = [i for i in real if t["start_ts"] <= i["opened_ts"] <= (t["end_ts"] or now) + timedelta(minutes=30)]
        if t["kind"] == "incident":
            hit = by_id.get(assign.get(ti))
            if hit:
                ic = json.loads(hit["cause"] or "{}")
                ok = ic.get("type") == cause["type"] or cause["type"] == "unexplained"
                if ok and cause["type"] == "dependency": ok = ic.get("service") == cause["detail"]
                if ok and cause["type"] in ("release", "novel"): ok = ic.get("detail") == cause["detail"]
                mins = (hit["opened_ts"] - t["start_ts"]).total_seconds() / 60
                route = json.loads(hit["routing"] or "{}")
                from .settings import DEPARTMENTS
                r["teams"] = [DEPARTMENTS[d]["name"] for d in route.get("to", [])]
                said = "cause correct" if cause["type"] != "unexplained" else ("reported cause unknown" if ic.get("type") == "unknown" else f"named: {ic.get('detail')}")
                r.update(status="pass" if ok else "partial", detect_min=round(mins), incident_id=hit["id"],
                         outcome=f"#{hit['id']} at {hit['opened_ts']:%H:%M} ({round(mins)} min) · {said if ok else 'cause wrong: ' + str(ic.get('detail') or ic.get('type'))}")
            elif started and now >= t["start_ts"] + timedelta(minutes=60):
                r.update(status="fail", outcome="Missed")
            else:
                r["outcome"] = "Not yet" if not started else "Watching…"
        elif t["kind"] == "data_quality":
            hits = [h for h in holds if h.seg in ts_ and t["start_ts"] <= h.ts <= (t["end_ts"] or now)]
            false = [i for i in in_window if iseg[i["id"]] <= ts_]
            if hits and not false:
                first = min(h.ts for h in hits) + timedelta(minutes=15)
                r.update(status="pass", detect_min=round((first - t["start_ts"]).total_seconds() / 60),
                         outcome=f"Gate flagged it at {first:%H:%M} — {'held, not scored' if any(h.status == 'hold' for h in hits) else 'duplicates removed'}; ticket to Monitoring & Data")
            elif false:
                r.update(status="fail", outcome="False alarm raised on bad data")
            elif started and now >= t["start_ts"] + timedelta(minutes=30):
                r.update(status="fail", outcome="Not flagged")
            else:
                r["outcome"] = "Not yet" if not started else "Watching…"
        elif t["kind"] == "advisory":
            adv = [i for i in incs if i["kind"] == "advisory" and t["start_ts"] - timedelta(hours=1) <= i["opened_ts"] <= t["end_ts"]]
            if adv:
                a = adv[0]
                r.update(status="pass", incident_id=a["id"], detect_min=round((a["opened_ts"] - t["start_ts"]).total_seconds() / 60),
                         outcome=f"Advisory #{a['id']} at {a['opened_ts']:%a %H:%M} to Business Operations; no segment pages")
            elif started and now >= t["start_ts"] + timedelta(hours=2):
                r.update(status="fail", outcome="Not recognised as system-wide")
            else:
                r["outcome"] = "Not yet" if not started else "Watching…"
        else:   # not_incident, transient
            if started:
                bad = [i for i in in_window if i["id"] not in used and (not json.loads(t["scope"]) or iseg[i["id"]] & ts_)]
                if bad:
                    r.update(status="fail", outcome=f"{len(bad)} false alarm(s)")
                elif over:
                    r.update(status="pass", outcome={"not_incident": "No alert raised" + (" (planned maintenance recognised)" if cause["type"] == "maintenance" else ""),
                                                     "transient": "Seen once, not paged"}[t["kind"]])
                else:
                    r["outcome"] = "Watching…"
            else:
                r["outcome"] = "Not yet"
        rows.append(r)
    false_alarms = [i for i in real if i["id"] not in used]
    naive = conn.execute(select(db.scans.c.naive_alerts)).all()
    cost = conn.execute(select(db.cost_log)).mappings().all()
    done = [r for r in rows if r["status"] != "pending"]
    return {"rows": rows, "false_alarms": [{"id": i["id"], "headline": i["headline"], "opened": i["opened_ts"].strftime("%a %H:%M")} for i in false_alarms],
            "passed": sum(r["status"] == "pass" for r in rows), "scored": len(done), "total": len(rows),
            "naive_pages": int(sum(n[0] or 0 for n in naive)), "lumio_alerts": len(real),
            "cost": {"llm_calls": sum(c["llm_calls"] or 0 for c in cost), "usd": round(sum(c["cost_usd"] or 0 for c in cost), 4),
                     "investigations": len({c["incident_id"] for c in cost})}}
