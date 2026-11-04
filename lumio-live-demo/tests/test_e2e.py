"""End-to-end test: plays the whole payday evening as fast as possible and checks the scoreboard.

    python -m tests.test_e2e            (uses a throw-away SQLite file)
"""
import os, sys, time, json, tempfile, logging
from datetime import timedelta

if "LUMIO_DATABASE_URL" not in os.environ:
    d = tempfile.mkdtemp()
    os.environ["LUMIO_DATABASE_URL"] = f"sqlite:///{d}/e2e.db"
    os.environ.setdefault("LUMIO_MODEL_DIR", f"{d}/models")

from lumio import config, db, simulator
from lumio.detector import Detector
from lumio import proof
from sqlalchemy import select

def main(verbose=True, inject=False):
    logging.basicConfig(level=logging.WARNING)
    t0 = time.time()
    db.init_db()
    if not db.state().get("history_ready"):
        simulator.load_history()
    simulator.reset_live()
    det = Detector()
    B, W = timedelta(minutes=config.BUCKET_MIN), timedelta(minutes=config.SCAN_MIN)
    nb = config.LIVE_START
    from datetime import datetime as D
    plan = {D(2025, 2, 10, 14, 0): ("failures", {"bank": "Bank W", "channel": "UPI"}),
            D(2025, 2, 13, 17, 0): ("slowdown", {"bank": "Bank Z", "channel": "Card"})} if inject else {}
    upto = D.fromisoformat(os.environ["E2E_UNTIL"]) if os.environ.get("E2E_UNTIL") else None
    while simulator.step_once():
        st = db.state()
        if upto and st["sim_time"] >= upto:
            break
        if st["sim_time"] in plan:
            simulator.inject(*plan[st["sim_time"]])
        while nb + B <= st["sim_time"]:
            det.score_buckets(nb, nb + B); nb += B
        if st["last_scan"] + W <= st["sim_time"]:
            end = st["last_scan"] + W
            det.scan(end)
            with db.engine().begin() as c:
                db.set_state(c, last_scan=end)
    with db.engine().connect() as c:
        ev = c.execute(select(db.events).order_by(db.events.c.id)).mappings().all()
        incs = c.execute(select(db.incidents)).mappings().all()
        sb = proof.scoreboard(c, db.state()["sim_time"])
    if verbose:
        for e in ev:
            if e["kind"] not in ("system", "watching", "email") or os.environ.get("E2E_ALL"):
                print(f"{e['ts']:%a %H:%M} [{e['kind']}] {e['message'][:230]}")
        print()
        for i in incs:
            a = json.loads(i["alert"] or "{}")
            print(f"#{i['id']} {i['kind']} {i['status']} opened {i['opened_ts']:%a %H:%M} started {(i['started_ts'] or i['opened_ts']):%H:%M} sev={i['severity']} by={i['explained_by']}")
            print("   ", a.get("headline")); print("   ", a.get("summary")); print("   ", a.get("likely_cause")); print("   ", [t["value"] for t in a.get("tiles", [])])
        print()
        for r in sb["rows"]:
            print(f"{r['status']:8} {r['date']} {r['name'][:58]:58} {r['outcome']}")
        print("false alarms:", sb["false_alarms"], "| naive pages:", sb["naive_pages"], "| cost:", sb["cost"])
        print(f"done in {time.time()-t0:.0f}s")
    return sb

if __name__ == "__main__":
    sb = main(inject="--inject" in sys.argv)
    ok = all(r["status"] in ("pass", "pending") for r in sb["rows"]) and not sb["false_alarms"]
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
