"""The 'learns normal' model: a gradient-boosted forecast of what each segment should look like right now.

It knows the hour of day, weekends and paydays, so a payday rush is expected rather than alarming.
Residuals are scaled by sampling noise (small segments are naturally noisier) and by each segment's own spread.
"""
import os, math, time, logging
import numpy as np, pandas as pd
import joblib
from sklearn.ensemble import HistGradientBoostingRegressor
from sqlalchemy import select
from . import config, db, world

log = logging.getLogger("lumio.model")
CAT = {"bank": config.BANKS, "channel": config.CHANNELS, "platform": config.PLATFORMS, "region": config.REGIONS}
FEATS = ["hour", "dow", "weekend", "payday", "payday_eve", "bank_i", "channel_i", "platform_i", "region_i"]

def mix_p95(p95, vol, sigma=world.LAT_SIGMA):
    """95th percentile of a mixture of lognormal latency groups. p95, vol: arrays shaped (groups, parts)."""
    p95 = np.atleast_2d(np.asarray(p95, float)); vol = np.atleast_2d(np.asarray(vol, float))
    w = vol / np.clip(vol.sum(1, keepdims=True), 1e-9, None)
    mu = np.log(np.clip(p95, 1e-6, None)) - 1.645 * sigma
    lo = np.log(np.nanmin(np.where(w > 0, p95, np.inf), 1)) - 1.0
    hi = np.log(np.nanmax(np.where(w > 0, p95, 0), 1)) + 1.0
    from scipy.special import erf as erfv
    for _ in range(32):
        mid = (lo + hi) / 2
        cdf = (w * 0.5 * (1 + erfv((mid[:, None] - mu) / (sigma * math.sqrt(2))))).sum(1)
        hi = np.where(cdf > 0.95, mid, hi); lo = np.where(cdf > 0.95, lo, mid)
    return np.exp((lo + hi) / 2)

def features(df):
    ts = pd.to_datetime(df["ts"])
    out = pd.DataFrame(index=df.index)
    out["hour"] = ts.dt.hour + ts.dt.minute / 60
    out["dow"] = ts.dt.weekday
    out["weekend"] = (out["dow"] >= 5).astype(int)
    pay = ts.map(lambda t: world.is_payday(t.to_pydatetime()))
    out["payday"] = pay.astype(int)
    out["payday_eve"] = (pay & (out["hour"] >= 16.5) & (out["hour"] < 22.5)).astype(int)
    for k, vals in CAT.items():
        out[k + "_i"] = df[k].map({v: i for i, v in enumerate(vals)})
    return out[FEATS]

def aggregate(df, freq_min):
    """Roll per-version rows up to segments and time windows (deduplicated)."""
    df = df.drop_duplicates(subset=["ts", "bank", "channel", "platform", "region", "app_version"])
    df = df.copy()
    df["win"] = pd.to_datetime(df["ts"]).dt.floor(f"{freq_min}min")
    keys = ["win", "bank", "channel", "platform", "region"]
    g = df.groupby(keys, sort=False)
    agg = g.agg(volume=("volume", "sum"), success_count=("success_count", "sum"), avg_amount=("avg_amount", "mean")).reset_index()
    # p95 of the mixture of rows inside each group
    parts = g.cumcount()
    k = int(parts.max()) + 1
    idx = g.ngroup()
    P = np.full((len(agg), k), np.nan); V = np.zeros((len(agg), k))
    P[idx, parts] = df["p95_ms"].values; V[idx, parts] = df["volume"].values
    P = np.where(np.isnan(P), 1.0, P)
    agg["p95_ms"] = mix_p95(P, V)
    agg["success"] = agg["success_count"] / agg["volume"].clip(lower=1)
    agg = agg.rename(columns={"win": "ts"})
    return agg

def lat_noise(vol):
    return 0.025 + 0.45 / np.sqrt(np.clip(vol, 1, None) / 3.0) / math.sqrt(3)

def vol_noise(ev):
    return 1 / np.sqrt(np.clip(ev, 1, None)) + 0.03

def succ_noise(p, vol):
    return np.sqrt(np.clip(p * (1 - p), 1e-5, None) / np.clip(vol, 1, None)) + 0.0015

class Baseline:
    def __init__(self):
        self.lat = None; self.succ = None; self.seg_lat = {}; self.seg_succ = {}

    def train(self):
        t0 = time.time()
        with db.engine().connect() as c:
            df = pd.read_sql(select(db.metrics).where(db.metrics.c.ts < config.LIVE_START), c)
        w = aggregate(df, config.SCAN_MIN)
        X = features(w)
        cat_mask = [f.endswith("_i") for f in FEATS]
        self.lat = HistGradientBoostingRegressor(max_iter=350, learning_rate=0.08, max_leaf_nodes=48,
                                                 categorical_features=cat_mask, random_state=7)
        self.lat.fit(X, np.log(w["p95_ms"]), sample_weight=np.sqrt(w["volume"]))
        self.succ = HistGradientBoostingRegressor(max_iter=250, learning_rate=0.08, max_leaf_nodes=32,
                                                  categorical_features=cat_mask, random_state=7)
        self.succ.fit(X, w["success"], sample_weight=w["volume"])
        self.vol = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.08, max_leaf_nodes=48,
                                                 categorical_features=cat_mask, random_state=7)
        self.vol.fit(X, np.log(w["volume"].clip(lower=1)))
        # each segment's own spread, after accounting for sampling noise
        r_lat = (np.log(w["p95_ms"]) - self.lat.predict(X)) / lat_noise(w["volume"])
        r_suc = (w["success"] - self.succ.predict(X)) / succ_noise(w["success"].clip(0.5, 0.9999), w["volume"])
        ev = np.exp(self.vol.predict(X))
        r_vol = (np.log(w["volume"].clip(lower=1)) - np.log(ev)) / vol_noise(ev)
        self.seg_vol = {}
        segs = w["bank"] + "|" + w["channel"] + "|" + w["platform"] + "|" + w["region"]
        for s, idx in segs.groupby(segs).groups.items():
            self.seg_vol[s] = max(0.8, 1.4826 * float(np.median(np.abs(r_vol[idx] - np.median(r_vol[idx])))))
            self.seg_lat[s] = max(0.8, 1.4826 * float(np.median(np.abs(r_lat[idx] - np.median(r_lat[idx])))))
            self.seg_succ[s] = max(0.8, 1.4826 * float(np.median(np.abs(r_suc[idx] - np.median(r_suc[idx])))))
        self.n_train = len(w); self.train_s = time.time() - t0
        log.info("model trained on %d windows in %.1fs", len(w), self.train_s)
        return self

    def precompute(self, start, end):
        """What Lumio expects depends only on the time and the segment, not on the data that arrives,
        so the expectations for the whole replay are computed once up front and looked up afterwards."""
        t0 = time.time()
        ts = pd.date_range(start, end, freq=f"{config.BUCKET_MIN}min")
        grid = pd.DataFrame([(t, b, c, p, r) for t in ts for b in config.BANKS for c in config.CHANNELS
                             for p in config.PLATFORMS for r in config.REGIONS], columns=["ts", "bank", "channel", "platform", "region"])
        X = features(grid)
        grid["lat"] = np.exp(self.lat.predict(X)); grid["succ"] = np.clip(self.succ.predict(X), 0.5, 0.9999)
        grid["vol"] = np.exp(self.vol.predict(X))
        grid["key"] = grid.ts.astype("int64").astype(str) + "|" + grid.bank + "|" + grid.channel + "|" + grid.platform + "|" + grid.region
        self._cache = grid.set_index("key")[["lat", "succ", "vol"]]
        log.info("expectations precomputed for %d buckets in %.1fs", len(ts), time.time() - t0)

    def _expect(self, agg):
        keys = (pd.to_datetime(agg["ts"]).astype("int64").astype(str) + "|" + agg["bank"] + "|" + agg["channel"] + "|" + agg["platform"] + "|" + agg["region"]).values
        cache = getattr(self, "_cache", None)
        if cache is not None:
            hit = cache.reindex(keys)
            if not hit.isna().any().any():
                return hit["lat"].values, hit["succ"].values, hit["vol"].values
        X = features(agg)
        return np.exp(self.lat.predict(X)), np.clip(self.succ.predict(X), 0.5, 0.9999), np.exp(self.vol.predict(X))

    def predict(self, agg):
        """agg: aggregated rows (ts, bank, channel, platform, region, volume, p95_ms, success)."""
        exp_lat, exp_succ, _ = self._expect(agg)
        segs = (agg["bank"] + "|" + agg["channel"] + "|" + agg["platform"] + "|" + agg["region"]).values
        sl = np.array([self.seg_lat.get(s, 1.0) for s in segs]); ss = np.array([self.seg_succ.get(s, 1.0) for s in segs])
        lat_sd = lat_noise(agg["volume"].values) * sl
        suc_sd = succ_noise(exp_succ, agg["volume"].values) * ss
        z_lat = (np.log(np.clip(agg["p95_ms"].values, 1, None)) - np.log(exp_lat)) / lat_sd
        z_succ = (agg["success"].values - exp_succ) / suc_sd
        return exp_lat, exp_succ, z_lat, z_succ, lat_sd

    def predict_volume(self, agg):
        """Expected payments in each 15-minute window and how unusual the actual count is."""
        _, _, ev = self._expect(agg)
        segs = (agg["bank"] + "|" + agg["channel"] + "|" + agg["platform"] + "|" + agg["region"]).values
        sv = np.array([self.seg_vol.get(s, 1.0) for s in segs])
        z = (np.log(np.clip(agg["volume"].values, 1, None)) - np.log(ev)) / (vol_noise(ev) * sv)
        return ev, z

    def save(self):
        os.makedirs(config.MODEL_DIR, exist_ok=True)
        joblib.dump(self, os.path.join(config.MODEL_DIR, "baseline.joblib"))

    @staticmethod
    def load():
        p = os.path.join(config.MODEL_DIR, "baseline.joblib")
        return joblib.load(p) if os.path.exists(p) else None
