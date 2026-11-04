"""Export the demo dataset to data/dataset/ (CSV, gzip) — the exact data the live replay streams.

    python -m tools.export_dataset
"""
import os, gzip, csv, json, sys
from datetime import timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lumio import config, world

OUT = os.path.join(config.DATA_DIR, "dataset")
STEP = timedelta(minutes=config.BUCKET_MIN)
MCOLS = ["ts", "bank", "channel", "platform", "region", "app_version", "volume", "success_count", "p95_ms", "avg_amount"]

def main():
    os.makedirs(OUT, exist_ok=True)
    faults = world.story_faults()
    def dump(name, start, end, faults):
        with gzip.open(os.path.join(OUT, f"payments_{name}.csv.gz"), "wt", newline="") as fm, \
             gzip.open(os.path.join(OUT, f"services_{name}.csv.gz"), "wt", newline="") as fs:
            wm, ws = csv.writer(fm), csv.writer(fs)
            wm.writerow(MCOLS); ws.writerow(["ts", "service", "p95_ms", "error_rate"])
            ts, n = start, 0
            while ts < end:
                b = world.generate_bucket(ts, faults, live=False, samples=False)
                for r in b["metrics"]:
                    wm.writerow([r["ts"].isoformat(sep=" ")] + [r[k] for k in MCOLS[1:]]); n += 1
                for r in b["services"]:
                    ws.writerow([r["ts"].isoformat(sep=" "), r["service"], r["p95_ms"], r["error_rate"]])
                ts += STEP
        print(name, n, "rows")
    dump("history_3weeks", config.HISTORY_START, config.LIVE_START, [])
    dump("demo_10days", config.LIVE_START, config.LIVE_END + STEP, faults)
    with open(os.path.join(OUT, "answer_key.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["day", "start", "end", "kind", "name", "scope", "cause", "source"])
        for t in world.story_truth():
            w.writerow([t.get("day"), t["start_ts"], t["end_ts"], t["kind"], t["name"], json.dumps(t["scope"]), json.dumps(t["cause"]), t.get("source", "simulated")])
    with open(os.path.join(OUT, "feeds.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["ts", "feed", "message"])
        for t, k, m, _ in world.feed_events():
            w.writerow([t, k, m])
    print("done →", OUT)

if __name__ == "__main__":
    main()
