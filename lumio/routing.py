"""Who gets told, and what each team should do. Pure rules, easy to read and change."""
from . import config
from .settings import DEPARTMENTS

SERVICE_OWNER = {"card-network": ("cards", "Card Operations & Partners"), "fraud-check": ("fraud", "Fraud & Risk Operations"),
                 "upi-switch": ("sre", "Site Reliability Engineering")}
SERVICE_OWNER.update({c: ("corebank", "Core Banking Platform") for c in config.CORE.values()})

def route(kind, cause, scope, severity, alert):
    t = cause.get("type") if cause else None
    label = alert.get("scope_label", "")
    to, cc, actions, ctas = [], [], [], []
    def act(dept, text): actions.append({"dept": dept, "team": DEPARTMENTS[dept]["name"], "text": text})
    if kind == "data_quality":
        to = ["data"]
        act("data", f"Check the monitoring collector for {label}: restart it or fix the pipeline, then confirm data is flowing in Lumio.")
        act("data", "No business action needed: Lumio held this data back, so no false alarm was raised.")
        ctas = [("ticket", "Create data ticket", "primary")]
        why = "Data-quality problems go to the team that owns the monitoring pipeline — never to on-call engineers."
    elif kind == "advisory":
        to, cc = ["bizops"], ["support"]
        act("bizops", "Volume is far below normal at every bank while success rates and payment times are healthy: this is demand, not a fault. Adjust today's forecasts and campaigns.")
        act("support", "Expect fewer contacts; no customer-facing incident to communicate.")
        ctas = [("ack", "Acknowledge", "primary")]
        why = "System-wide demand changes are business information, not a page."
    elif t == "release":
        v = cause.get("version")
        to = ["release", "mobile"]
        if cause.get("metric") == "volume":
            act("release", f"Roll back v{v} now — payments from users on v{v} have almost stopped (one click below).")
            act("mobile", f"Users on v{v} cannot reach payment: check the login/session flow changed in v{v}.")
            act("support", f"Customers on the {scope.get('bank', '')} {scope.get('platform', '')} app v{v} may be unable to pay; suggest the website meanwhile.")
            ctas = [("runbook", f"Roll back v{v}", "danger")]
        else:
            act("release", f"Pause the v{v} rollout now (one click below) and keep it paused until a fix ships.")
            act("mobile", f"v{v} is {cause.get('lat_x')}× slower than v{cause.get('other')} for {scope.get('channel', 'all')} payments on the same phones: check what v{v} changed in the payment flow.")
            act("support", f"Prepare a message: some {scope.get('bank', '')} {scope.get('platform', '')} customers may see slow payments; updating later today helps.")
            ctas = [("runbook", f"Pause v{v} rollout", "danger")]
        cc = ["support"] + (["duty"] if severity == "high" else [])
        why = f"Caused by an app release → Release Management owns the rollout, Mobile App Engineering owns the fix."
    elif t == "dependency":
        svc = cause.get("service"); owner = SERVICE_OWNER.get(svc, ("sre", ""))[0]
        to = [owner] + (["sre"] if owner != "sre" else [])
        act(owner, f"Page the {svc} on-call: it is running at {cause.get('ratio')}× its normal response time" +
            (f" with {cause.get('error_pct')}% errors." if cause.get("error_pct") else "."))
        if owner != "sre":
            act("sre", f"Check {svc} capacity and recent changes; prepare failover if it does not recover within 15 minutes.")
        else:
            act("sre", f"Check {svc} memory, CPU and recent changes; restart or fail over if it keeps degrading.")
        if svc == "fraud-check":
            act("product", "Decide whether to fail open for low-risk card payments under $50 while fraud-check recovers.")
            cc.append("product")
        act("support", f"Customers paying via {label} may see slow or failed payments; use the holding message.")
        cc += ["support"] + (["duty"] if severity == "high" else [])
        ctas = [("page", f"Page {svc} on-call", "danger")]
        why = f"An upstream service ({svc}) is the bottleneck → its owning team plus SRE."
    elif t == "regional":
        to = ["network", "sre"]
        act("network", f"Check internet/peering providers for the {cause.get('detail')} region — every bank there is affected at once.")
        act("sre", "Confirm our own infrastructure in that region is healthy; consider routing traffic through another region.")
        act("support", f"Customers in the {cause.get('detail')} region may see failed payments; post a status update.")
        cc = ["support"] + (["duty"] if severity == "high" else [])
        ctas = [("bridge", "Open bridge call", "danger")]
        why = "Only one region, across all banks → a network problem, not any one bank."
    elif t == "fraud":
        to = ["fraud", "cards"]
        act("fraud", f"Block the card-testing pattern now: bursts of payments under $5 at {scope.get('bank', '')} — {cause.get('attempts', 'many')} attempts, {cause.get('decline_pct', '')}% declined.")
        act("cards", "Notify the card scheme and watch for chargebacks on the tested cards over the next 48 hours.")
        cc = ["duty"]
        ctas = [("block", "Block pattern (fraud rule)", "danger")]
        why = "A burst of tiny, mostly declined card payments is the signature of card testing → Fraud & Risk."
    elif t == "novel":
        g = cause.get("group"); gb = (cause.get("group_by") or "").replace("_", " ")
        to = ["product"] + (["cards"] if scope.get("channel") == "Card" else ["sre"])
        act("product", f"Check limit, rule or configuration changes that affect {gb} {g} at {scope.get('bank', 'these banks')}.")
        act(to[1], f"Confirm with the processor whether {gb} {g} payments are being held or timing out on their side.")
        act("support", f"Customers making payments in {gb} {g} may be affected; route their calls to the payments desk.")
        cc = ["support"] + (["duty"] if severity == "high" else [])
        ctas = [("ack", "Take ownership", "primary")]
        why = "Not explained by releases, regions or services; the AI agent found the pattern in transaction samples → Payments Product owns the rules."
    else:
        to, cc = ["sre"], ["duty"]
        act("sre", "Investigate with the evidence attached; Lumio could not isolate a single cause.")
        ctas = [("ack", "Take ownership", "primary")]
        why = "Unknown cause → SRE triage with the Duty Manager informed."
    cc = [d for d in dict.fromkeys(cc) if d not in to]
    return {"to": to, "cc": cc, "why": why, "actions": actions,
            "ctas": [{"action": a, "label": l, "style": s} for a, l, s in ctas]}
