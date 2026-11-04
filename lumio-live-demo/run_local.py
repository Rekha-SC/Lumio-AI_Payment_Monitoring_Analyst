"""One command to run the whole Lumio live demo on a laptop — no Docker, no database server.

    python run_local.py              # then open http://localhost:8000
    python run_local.py --fresh      # wipe the local database and start over
    python run_local.py --port 8080

All services (simulator, ingest API, data-quality gate, detector, investigator, AI agent, narrator,
dashboard) run inside this one process and share a SQLite file, lumio.db.
"""
import os, sys, argparse, threading, time, webbrowser, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--fresh", action="store_true", help="delete lumio.db and the trained model first")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    os.environ.setdefault("LUMIO_DATABASE_URL", "sqlite:///" + os.path.join(HERE, "lumio.db"))
    os.environ.setdefault("LUMIO_MODEL_DIR", os.path.join(HERE, "models"))
    os.environ["LUMIO_MODE"] = "all"
    if a.fresh:
        for f in ("lumio.db", "lumio.db-wal", "lumio.db-shm"):
            if os.path.exists(f): os.remove(f)
        shutil.rmtree("models", ignore_errors=True)
    url = f"http://localhost:{a.port}"
    print("\n  Lumio — live demo (10 days · 6 business days · real NAB data)")
    print("  -------------------------------------------------------------")
    print(f"  Dashboard: {url}")
    print("  First start loads three weeks of history and trains the models (about 30 seconds).")
    print("  Email: open Settings in the dashboard to connect your mailbox (see EMAIL_SETUP.md).")
    print("  Agent mode:", "Claude (ANTHROPIC_API_KEY found)" if os.environ.get("ANTHROPIC_API_KEY") else "offline planner (set ANTHROPIC_API_KEY to use Claude)")
    print("  Press Ctrl+C to stop.\n")
    if not a.no_browser:
        threading.Thread(target=lambda: (time.sleep(2.5), webbrowser.open(url)), daemon=True).start()
    import uvicorn
    uvicorn.run("lumio.api:app", host="0.0.0.0", port=a.port, log_level="warning")

if __name__ == "__main__":
    main()
