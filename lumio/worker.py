"""Worker service: scores every new 5-minute bucket and runs the 15-minute scan as the clock moves.

Run on its own with `python -m lumio.worker` (Docker Compose does this), or inside run_local.py.
"""
import time, logging
from datetime import timedelta
from sqlalchemy import select, func, and_
from . import config, db
from .detector import Detector

log = logging.getLogger("lumio.worker")
B = timedelta(minutes=config.BUCKET_MIN)
W = timedelta(minutes=config.SCAN_MIN)

def run_forever(stop=None):
    db.init_db()
    while not db.state().get("history_ready"):
        log.info("waiting for history…"); time.sleep(1)
    det = Detector()
    from . import mailer
    import threading
    threading.Thread(target=mailer.run_sender, args=(stop,), daemon=True, name="mailer").start()
    # chart context: score the last six hours before the evening starts
    with db.engine().connect() as c:
        have = c.execute(select(func.count()).select_from(db.bucket_scores).where(db.bucket_scores.c.ts < config.LIVE_START)).scalar()
    if not have:
        det.score_buckets(config.LIVE_START - timedelta(hours=6), config.LIVE_START)
    run_id = None
    next_bucket = None
    log.info("worker ready")
    while not (stop and stop.is_set()):
        st = db.state()
        if st.get("run_id") != run_id:
            run_id = st.get("run_id"); det.reset_memory()
            with db.engine().connect() as c:
                last = c.execute(select(func.max(db.bucket_scores.c.ts)).where(db.bucket_scores.c.ts >= config.LIVE_START)).scalar()
            next_bucket = (last + B) if last else config.LIVE_START
        sim = st.get("sim_time")
        busy = False
        if sim:
            while next_bucket + B <= sim:
                det.score_buckets(next_bucket, next_bucket + B); next_bucket += B; busy = True
            st = db.state()
            if st.get("run_id") == run_id and st["last_scan"] and st["last_scan"] + W <= sim:
                end = st["last_scan"] + W
                res = det.scan(end)
                with db.engine().begin() as c:
                    if db.state(c).get("run_id") == run_id:
                        db.set_state(c, last_scan=end)
                log.info("scan %s %s", end.strftime("%H:%M"), res)
                busy = True
        if not busy:
            time.sleep(0.1)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    while True:
        try:
            run_forever(); break
        except Exception as e:
            log.exception("worker retrying: %s", e); time.sleep(3)
