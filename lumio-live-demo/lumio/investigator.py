"""Investigator: scripted root-cause analysis, escalating to the AI agent only when it gets stuck.
Then: severity, routing to departments, and the alert email."""
import json, time
from datetime import datetime, timedelta
from sqlalchemy import insert, update, select
from . import config, db, tools, agent, narrator, routing

def _save_steps(steps):
    if steps:
        with db.engine().begin() as c:
            c.execute(insert(db.investigation_steps), steps)

def investigate(incident_id, scope, segs, now, metric="performance", is_update=False):
    steps = []
    t_all = time.time()
    with db.engine().connect() as conn:
        def record(actor, tool, inp, finding, out, ms=0.0):
            steps.append({"incident_id": incident_id, "step": len(steps) + 1, "actor": actor, "tool": tool,
                          "input": json.dumps(inp, default=str), "finding": finding,
                          "output": json.dumps(out, default=str)[:20000], "ms": round(ms, 1)})
        def run(tool, fn, *a):
            t = time.time(); finding, out = fn(conn, *a)
            record("script", tool, {"args": [str(x) for x in a[:1]]}, finding, out, (time.time() - t) * 1000)
            return out

        where = run("where_and_peers", tools.where_and_peers, scope, segs, now)
        n = where["now"]
        vol_metric = "volume" in metric
        cp = run("change_point", tools.change_point, segs, now, metric)
        start = datetime.fromisoformat(cp["start"])
        cause, release, slice_filter, slice_share, slice_avg, explained_by = None, None, None, None, None, "script"

        # 1. did a release just go out here?
        rel = run("recent_releases", tools.recent_releases, scope, start)
        if rel["releases"]:
            r0 = rel["releases"][0]
            vmetric = "volume" if (vol_metric and n["vol_x"] < 0.8) else "performance"
            vs = run("version_split", tools.version_split, scope, start, now, r0["version"], vmetric)
            if vs["culprit"] and vs["culprit"] == r0["version"]:
                other = vs.get("other") or next(v["version"] for v in vs["versions"] if v["version"] != vs["culprit"])
                cause = {"type": "release", "detail": r0["version"], "version": r0["version"], "other": other, "metric": vmetric,
                         "lat_x": vs.get("lat_x"), "success_gap_pts": vs.get("success_gap_pts"), "vol_ratio": vs.get("vol_ratio"),
                         "confidence": 0.92}
                release = r0; slice_filter = {"app_version": vs["culprit"]}
        # 2. is an upstream service the bottleneck?
        if cause is None and not (vol_metric and n["vol_x"] > 1.5):
            dep = run("dependencies", tools.dependencies, segs, start, now, max(n["lat_x"] or 1, 1 + (n["exp_success"] - n["success"]) * 10))
            if dep["culprit"]:
                svc = next(s for s in dep["services"] if s["service"] == dep["culprit"])
                cause = {"type": "dependency", "detail": dep["culprit"], "service": dep["culprit"], "ratio": svc["ratio"],
                         "error_pct": svc["error_pct"] if svc["error_pct"] > 1 else None, "confidence": 0.88}
        # 3. one region, every bank → the network
        if cause is None and "region" in scope and "bank" not in scope:
            record("script", "regional_check", {"region": scope["region"]},
                   f"Only the {scope['region']} region is affected, across every bank and channel, while other regions are normal — "
                   "the common factor is the network path, not any bank or service.", {"region": scope["region"]})
            cause = {"type": "regional", "detail": scope["region"], "confidence": 0.85}
        # 4. a burst of tiny declined payments → card testing
        if cause is None and vol_metric and n["vol_x"] > 1.5:
            t = time.time()
            finding, out = tools.query_samples(conn, scope, start, now, "amount_band", "failures")
            record("script", "attack_pattern", {"group_by": "amount_band"}, finding, out, (time.time() - t) * 1000)
            tiny = next((g for g in out["groups"] if g["group"] == "< $5"), None)
            if tiny and tools.explains(tiny, "failures") and tiny["share_of_samples"] > 0.3:
                cause = {"type": "fraud", "detail": "card testing", "attempts": int(max(0, n["volume"] - n["exp_volume"])),
                         "decline_pct": round(tiny["failure_rate"] * 100), "confidence": 0.9}
                record("script", "attack_pattern", {}, f"Card-testing signature: volume {n['vol_x']}× normal, {tiny['share_of_samples']*100:.0f}% of payments under $5, "
                       f"{tiny['failure_rate']*100:.0f}% of those declined.", tiny)
        # 5. nothing pre-built explains it → the AI agent
        usage, mode = None, None
        if cause is None:
            explained_by = "agent"
            measure = "failures" if (n["exp_success"] or 0) - (n["success"] or 0) > 0.01 else "latency"
            context = {"scope": scope, "started": start.strftime("%H:%M"), "now": now.strftime("%H:%M"), "measure": measure,
                       "where": n, "releases": rel["releases"], "dependencies": "all normal"}
            cause, usage, mode = agent.investigate(conn, scope, start, now, record, context)
            if cause["type"] == "novel":
                cause["confidence"] = 0.8 if (cause.get("lift") or 0) >= 5 or (cause.get("lat_ratio") or 0) >= 2.5 else 0.65
                if cause["group_by"] == "amount_band":
                    _, smp = tools.query_samples(conn, scope, start, now, "amount_band", cause.get("measure", "failures"))
                    g = next(x for x in smp["groups"] if x["group"] == cause["group"])
                    slice_share = g["samples"] / max(1, smp["n"]); slice_avg = g["avg_amount"]
            else:
                cause["confidence"] = 0.4
        cause["metric"] = cause.get("metric") or ("volume" if vol_metric and n["vol_x"] < 0.8 else metric)
        t = time.time()
        finding, imp = tools.impact(conn, scope, segs, start, now, slice_filter, slice_share, slice_avg)
        record("script", "impact", {}, finding, imp, (time.time() - t) * 1000)

    facts = {"scope": scope, "cause": cause, "start": start, "impact": imp, "release": release, "peers": where["peers"], "now": n}
    alert = narrator.compose(facts)
    alert, n_usage = narrator.polish_with_llm(alert, facts)
    alert["confidence"] = cause.get("confidence")
    alert["evidence"] = [s["finding"] for s in steps if s["tool"] not in ("plan",)][:7]
    alert["attribution"] = _attribution(steps, cause)
    route = routing.route("incident", cause, scope, alert["severity"], alert)
    script_ms = (time.time() - t_all) * 1000 - (usage or {}).get("duration_ms", 0)
    _save_steps(steps)
    with db.engine().begin() as c:
        c.execute(insert(db.cost_log).values(incident_id=incident_id, ts=now, actor="script", purpose="investigation",
                                             llm_calls=0, tokens_in=0, tokens_out=0, cost_usd=0.0, duration_ms=round(script_ms, 1)))
        if usage:
            c.execute(insert(db.cost_log).values(incident_id=incident_id, ts=now, actor="agent-" + mode, purpose="root cause", **usage))
        if n_usage:
            c.execute(insert(db.cost_log).values(incident_id=incident_id, ts=now, actor="narrator-claude", purpose="wording", **n_usage))
        c.execute(update(db.incidents).where(db.incidents.c.id == incident_id).values(
            started_ts=start, severity=alert["severity"], cause=json.dumps(cause, default=str), alert=json.dumps(alert, default=str),
            headline=alert["headline"], narrative=alert["summary"], confidence=cause.get("confidence"), explained_by=explained_by,
            routing=json.dumps(route)))
        who = {"script": "scripted checks", "agent": f"the AI agent ({mode})"}[explained_by]
        teams = ", ".join(routing.DEPARTMENTS[d]["name"] for d in route["to"])
        db.log_event(c, now, "alert", "narrator", f"ALERT #{incident_id} · {alert['headline']}", {"incident_id": incident_id, "severity": alert["severity"]})
        db.log_event(c, now, "investigation", "investigator",
                     f"Incident #{incident_id}: cause found by {who} in {len(steps)} steps — {alert['likely_cause']} Routed to {teams}.", {"incident_id": incident_id})
    from . import mailer
    mailer.queue_alert(incident_id, now, update=is_update)
    return alert

def advisory(incident_id, now, info):
    """System-wide demand change: business information, not a page."""
    with db.engine().connect() as conn:
        segs = json.loads(conn.execute(select(db.incidents.c.scope).where(db.incidents.c.id == incident_id)).scalar())["segments"]
        _, imp = tools.impact(conn, {}, segs, info["start"], now)
        finding, where = tools.where_and_peers(conn, {}, segs, now)
    nw = where["now"]
    alert = narrator.compose_advisory(imp, nw, info, now)
    route = routing.route("advisory", {"type": "system_wide"}, {}, "info", alert)
    _save_steps([{"incident_id": incident_id, "step": 1, "actor": "script", "tool": "where_and_peers", "input": "{}", "finding": finding,
                  "output": json.dumps(where, default=str)[:20000], "ms": 0}])
    with db.engine().begin() as c:
        c.execute(update(db.incidents).where(db.incidents.c.id == incident_id).values(
            started_ts=info["start"], alert=json.dumps(alert, default=str), headline=alert["headline"], narrative=alert["summary"],
            cause=json.dumps({"type": "system_wide", "detail": "volume", "context": info.get("context")}), explained_by="script",
            routing=json.dumps(route), confidence=0.9))
    from . import mailer
    mailer.queue_alert(incident_id, now)

def data_ticket(incident_id, grp, note, now):
    alert = narrator.compose_dq(grp, note, now)
    route = routing.route("data_quality", {"type": "data_quality"}, grp["scope"], "low", alert)
    with db.engine().begin() as c:
        c.execute(update(db.incidents).where(db.incidents.c.id == incident_id).values(alert=json.dumps(alert), narrative=alert["summary"],
                  routing=json.dumps(route)))
    from . import mailer
    mailer.queue_alert(incident_id, now)

def refresh_alert(inc, now):
    """Keep an open alert's numbers current (cheap: no agent, no LLM)."""
    sc = json.loads(inc["scope"]); cause = json.loads(inc["cause"] or "{}"); alert = json.loads(inc["alert"])
    slice_filter = {"app_version": cause["version"]} if cause.get("type") == "release" else None
    with db.engine().connect() as conn:
        slice_share = slice_avg = None
        if cause.get("type") == "novel" and cause.get("group_by") == "amount_band":
            _, smp = tools.query_samples(conn, sc["filter"], inc["started_ts"], now, "amount_band", cause.get("measure", "failures"))
            g = next((x for x in smp["groups"] if x["group"] == cause["group"]), None)
            if g: slice_share, slice_avg = g["samples"] / max(1, smp["n"]), g["avg_amount"]
        _, imp = tools.impact(conn, sc["filter"], sc["segments"], inc["started_ts"], now, slice_filter, slice_share, slice_avg)
        _, where = tools.where_and_peers(conn, sc["filter"], sc["segments"], now)
    fresh = narrator.compose({"scope": sc["filter"], "cause": cause, "start": inc["started_ts"], "impact": imp,
                              "release": {"minutes_before_start": 0}, "now": where["now"]})
    alert["tiles"] = fresh["tiles"]; alert["updated"] = now.strftime("%a %H:%M")
    alert["value_at_risk"] = fresh["value_at_risk"]; alert["affected"] = fresh["affected"]
    with db.engine().begin() as c:
        c.execute(update(db.incidents).where(db.incidents.c.id == inc["id"]).values(alert=json.dumps(alert, default=str)))

def _attribution(steps, cause):
    """Small chart data for the incident drawer and email: whichever split explained the cause."""
    for s in reversed(steps):
        out = json.loads(s["output"]) if s["output"] else {}
        if s["tool"] == "version_split" and out.get("culprit"):
            if cause.get("metric") == "volume":
                return {"title": "Payments by app version since the release", "unit": "",
                        "bars": [{"label": "v" + v["version"], "value": v["volume"], "hot": v["version"] == out["culprit"]} for v in out["versions"]]}
            return {"title": "Payment time by app version", "unit": "s",
                    "bars": [{"label": "v" + v["version"], "value": round(v["p95_ms"] / 1000, 2), "hot": v["version"] == out["culprit"]} for v in out["versions"]]}
        if s["tool"] == "dependencies" and out.get("culprit"):
            return {"title": "Upstream services vs normal", "unit": "×",
                    "bars": [{"label": v["service"], "value": v["ratio"], "hot": v["service"] == out["culprit"]} for v in out["services"]]}
        if s["tool"] == "attack_pattern" and out.get("groups"):
            return {"title": "Decline rate by payment amount", "unit": "%",
                    "bars": [{"label": g["group"], "value": round(g["failure_rate"] * 100, 1), "hot": g["group"] == "< $5"} for g in out["groups"]]}
        if s["tool"] == "query_samples" and out.get("groups") and s["actor"] == "agent" and out.get("group_by") == cause.get("group_by"):
            if out.get("measure") == "latency":
                return {"title": f"Payment time vs the rest, by {out['group_by'].replace('_', ' ')}", "unit": "×",
                        "bars": [{"label": g["group"], "value": g["lat_ratio"], "hot": g["group"] == cause.get("group")} for g in out["groups"]]}
            return {"title": f"Failure rate by {out['group_by'].replace('_', ' ')}", "unit": "%",
                    "bars": [{"label": g["group"], "value": round(g["failure_rate"] * 100, 1), "hot": g["group"] == cause.get("group")} for g in out["groups"]]}
    return None
