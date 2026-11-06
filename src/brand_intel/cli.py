"""All artifact paths are relative to --root; public inputs are explicit."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import urllib.request

DAY = 86400000
ASPECTS = {
    "scent": ("scent", "fragrance", "smell"),
    "texture": ("texture", "greasy", "sticky"),
    "packaging": ("packaging", "bottle", "pump", "leak"),
    "value": ("price", "expensive", "affordable", "value"),
}
FEATURES = ["prior_reviews", "reviews_7d", "reviews_30d", "reviews_90d",
            "reviews_365d", "rating_mean_90d", "verified_share_90d",
            "days_since_first_observed_review", "days_since_last_review",
            "recent_to_previous_30d_ratio", *[f"{a}_mentions_90d" for a in ASPECTS]]


def stamp(date: str) -> int:
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


def date_of(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def write_csv(path: Path, rows: list[dict], fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0]) if rows else [])
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(root, cfg, metadata=False):
    """Atomic, bounded download; no upstream Python loader is executed."""
    prefix = "meta_" if metadata else ""
    url = cfg["metadata_url"] if metadata else cfg["reviews_url"]
    dest = root / f"data/raw/{prefix}{cfg['category']}.jsonl.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    cached = dest.exists()
    if not dest.exists():
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, part.open("wb") as f:
                total = 0
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(block)
                    if total > 250_000_000:
                        raise ValueError("Download exceeds the 250 MB category budget")
                    f.write(block)
            # Validate full gzip stream before atomically marking the input complete.
            with gzip.open(part, "rb") as f:
                for _ in iter(lambda: f.read(1024 * 1024), b""):
                    pass
            part.replace(dest)
        except Exception:
            part.unlink(missing_ok=True)
            raise
    write_json(root / f"data/raw/{prefix}source-manifest.json", {
        "url": url, "source_page": cfg["source_page"],
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "reused_complete_local_file": cached,
        "sha256": digest(dest), "bytes": dest.stat().st_size,
        "license_status": cfg["license_status"], "confidential_inputs": False,
    })
    print(f"Public input ready: {dest}", flush=True)


def catalog(root, cfg):
    """Optional metadata snapshot for descriptive lookup, never past predictors."""
    raw=root/f"data/raw/meta_{cfg['category']}.jsonl.gz"
    rows=[]
    with gzip.open(raw,"rt",encoding="utf-8") as f:
        for line in f:
            record=json.loads(line)
            details=record.get("details") or {}
            if isinstance(details,str):
                try: details=json.loads(details)
                except json.JSONDecodeError: details={}
            if not isinstance(details,dict): details={}
            rows.append({"product_id":record["parent_asin"],"title_snapshot":record.get("title"),
                "store_snapshot":record.get("store"),"brand_reported_snapshot":details.get("Brand") or details.get("Brand Name"),
                "category_snapshot":record.get("main_category"),"price_snapshot":record.get("price"),
                "historical_availability_known":False,"use_in_historical_predictors":False})
    write_csv(root/"data/processed/product_catalog_snapshot.csv",rows)
    print(f"Saved {len(rows)} descriptive catalog records; excluded from historical features",flush=True)


def require_category(root, cfg, filename):
    manifest=json.loads((root/f"data/processed/{filename}").read_text())
    if manifest.get("category")!=cfg["category"]:
        raise ValueError("Processed data belong to a different category; use a separate --root")
    from .contracts import fingerprint, ContractError
    if filename == "task-manifest.json":
        expected = fingerprint({"config": cfg, "features": FEATURES, "schema": 2})
        if manifest.get("task_fingerprint") != expected:
            raise ContractError("Legacy task configuration mismatch; rerun prepare or use v2")
        for name, expected_hash in manifest.get("files", {}).items():
            if digest(root / "data/processed" / name) != expected_hash:
                raise ContractError("Legacy task input hash mismatch")
    if filename == "data-quality.json":
        if digest(root / f"data/raw/{cfg['category']}.jsonl.gz") != manifest.get("raw_sha256"):
            raise ContractError("Raw input changed; rerun ingest")
    return manifest


def ingest(root, cfg):
    raw = root / f"data/raw/{cfg['category']}.jsonl.gz"
    db = root / "data/processed/reviews.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    temp = db.with_suffix(".tmp")
    temp.unlink(missing_ok=True)
    conn = sqlite3.connect(temp)
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, product_id TEXT, reviewer_id TEXT, ts INTEGER, rating REAL, verified INTEGER, scent INTEGER, texture INTEGER, packaging INTEGER, value INTEGER)")
    invalid = duplicates = count = 0
    batch = []
    with gzip.open(raw, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                ts, rating = int(r["timestamp"]), float(r["rating"])
                product, user = str(r["parent_asin"]), str(r["user_id"])
                if not product or not user or not 1 <= rating <= 5 or not stamp("1990-01-01") <= ts < stamp("2023-10-01"):
                    raise ValueError("Invalid event")
                text = (str(r.get("title", "")) + " " + str(r.get("text", ""))).lower()
                key = hashlib.sha256(json.dumps([product, user, ts, rating, text], ensure_ascii=False).encode()).hexdigest()
                # Derived pseudonym, never a demographic inference.
                reviewer = hashlib.sha256(user.encode()).hexdigest()[:24]
                flags = [int(any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)) for words in ASPECTS.values()]
                batch.append((key, product, reviewer, ts, rating, int(r.get("verified_purchase") is True), *flags))
                count += 1
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                invalid += 1
            if len(batch) >= 10000:
                before = conn.total_changes
                conn.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                duplicates += len(batch) - (conn.total_changes - before)
                batch.clear()
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    duplicates += len(batch) - (conn.total_changes - before)
    conn.execute("CREATE INDEX event_time ON events(ts)")
    conn.execute("CREATE INDEX product_time ON events(product_id,ts)")
    conn.commit()
    bounds = conn.execute("SELECT min(ts),max(ts),count(*),count(DISTINCT product_id),count(DISTINCT reviewer_id) FROM events").fetchone()
    conn.close()
    if not bounds[2]:
        temp.unlink()
        raise ValueError("No valid review events")
    temp.replace(db)
    write_json(root / "data/processed/data-quality.json", {
        "category": cfg["category"], "raw_sha256": digest(raw),
        "valid_input_rows": count, "invalid_rows": invalid, "exact_duplicates_removed": duplicates,
        "events": bounds[2], "products": bounds[3], "reviewers": bounds[4],
        "min_timestamp": date_of(bounds[0]), "max_timestamp": date_of(bounds[1]),
        "availability_caveat": "Retrospective review publication timestamps; historical ingestion/revision times unavailable.",
        "excluded_predictors": ["metadata price", "metadata average_rating", "metadata rating_number", "helpful_vote", "raw user_id", "demographics"],
    })
    print(f"Ingested {bounds[2]:,} reviews, {bounds[3]:,} products", flush=True)


def snapshots(conn, cutoff, horizon_days, minimum):
    """Features strictly before cutoff; labels in [cutoff, cutoff+horizon)."""
    t = stamp(cutoff)
    conn.row_factory = sqlite3.Row
    aspect_sql = ",".join(f"sum(CASE WHEN ts >= {t-90*DAY} THEN {a} ELSE 0 END) AS {a}_mentions_90d" for a in ASPECTS)
    sql = f"""SELECT product_id,count(*) prior_reviews,
       sum(ts >= {t-7*DAY}) reviews_7d, sum(ts >= {t-30*DAY}) reviews_30d,
       sum(ts >= {t-90*DAY}) reviews_90d, sum(ts >= {t-365*DAY}) reviews_365d,
       avg(CASE WHEN ts >= {t-90*DAY} THEN rating END) rating_mean_90d,
       avg(CASE WHEN ts >= {t-90*DAY} THEN verified END) verified_share_90d,
       ({t}-min(ts))/{DAY}.0 days_since_first_observed_review,
       ({t}-max(ts))/{DAY}.0 days_since_last_review, {aspect_sql}
       FROM events WHERE ts < ? GROUP BY product_id
       HAVING count(*) >= ? AND max(ts) >= ? ORDER BY product_id"""
    features = [dict(row) for row in conn.execute(sql, (t, minimum, t-365*DAY))]
    labels = dict(conn.execute("SELECT product_id,count(*) FROM events WHERE ts >= ? AND ts < ? GROUP BY product_id", (t, t+horizon_days*DAY)))
    for row in features:
        row.update(as_of_date=cutoff, future_reviews=labels.get(row["product_id"], 0))
        previous_60 = row["reviews_90d"] - row["reviews_30d"]
        row["recent_to_previous_30d_ratio"] = (row["reviews_30d"] + 1) / (previous_60 / 2 + 1)
    return features


def prepare(root, cfg):
    import numpy as np
    require_category(root,cfg,"data-quality.json")
    conn = sqlite3.connect(root / "data/processed/reviews.sqlite")
    last = conn.execute("SELECT max(ts) FROM events").fetchone()[0]
    splits = {"train": cfg["train_cutoffs"], "validation": [cfg["validation_cutoff"]], "test": [cfg["test_cutoff"]]}
    all_rows = {}
    # No training label extends into the validation prediction period.
    if max(stamp(c) for c in cfg["train_cutoffs"]) + cfg["horizon_days"]*DAY > stamp(cfg["validation_cutoff"]):
        raise ValueError("Training label windows overlap validation cutoff")
    if stamp(cfg["validation_cutoff"]) + cfg["horizon_days"]*DAY > stamp(cfg["test_cutoff"]):
        raise ValueError("Validation labels overlap test cutoff")
    for split, cutoffs in splits.items():
        rows = []
        for cutoff in cutoffs:
            if stamp(cutoff) + cfg["horizon_days"]*DAY > last:
                raise ValueError(f"Incomplete label window: {cutoff}")
            rows.extend(snapshots(conn, cutoff, cfg["horizon_days"], cfg["min_prior_reviews"]))
        if not rows:
            raise ValueError(f"No eligible {split} examples")
        all_rows[split] = rows
    threshold = max(cfg["min_hit_reviews"], int(math.ceil(np.quantile([r["future_reviews"] for r in all_rows["train"]], cfg["hit_quantile_train_only"]))))
    summary = {"category":cfg["category"], "target": cfg["target"], "hit_threshold_train_only": threshold, "features": FEATURES,
               "population": "previously reviewed products active within 365 days, NOT zero-history launches", "splits": {}}
    for split, rows in all_rows.items():
        for row in rows:
            row["high_review_activity"] = int(row["future_reviews"] >= threshold)
        write_csv(root / f"data/processed/{split}.csv", rows)
        summary["splits"][split] = {"rows": len(rows), "positive_rate": float(np.mean([r["high_review_activity"] for r in rows])), "cutoffs": splits[split]}
    write_json(root / "data/processed/task-manifest.json", summary)
    prepare_forecast(conn, root, cfg)
    audience(conn, root, cfg["test_cutoff"])
    from .contracts import fingerprint
    summary["task_fingerprint"] = fingerprint({"config": cfg, "features": FEATURES, "schema": 2})
    summary["files"] = {name: digest(root / "data/processed" / name) for name in ["train.csv", "validation.csv", "test.csv", "weekly_reviews.csv"]}
    write_json(root / "data/processed/task-manifest.json", summary)
    conn.close()
    print(json.dumps(summary["splits"], indent=2), flush=True)


def prepare_forecast(conn, root, cfg):
    cutoff, h = stamp(cfg["forecast_cutoff"]), cfg["forecast_weeks"]
    if datetime.fromtimestamp(cutoff/1000, timezone.utc).weekday() != 0:
        raise ValueError("Forecast cutoff must be a Monday")
    last = conn.execute("SELECT max(ts) FROM events").fetchone()[0]
    if cutoff + h*7*DAY > last:
        raise ValueError("Forecast horizon not completely observed")
    items = [r[0] for r in conn.execute("SELECT product_id FROM events WHERE ts >= ? AND ts < ? GROUP BY product_id ORDER BY count(*) DESC,product_id LIMIT ?", (cutoff-365*DAY, cutoff, cfg["forecast_series"]))]
    rows = []
    for item in items:
        bins = {}
        for row in conn.execute("SELECT ts FROM events WHERE product_id=? AND ts>=? AND ts<?", (item, cutoff-52*7*DAY, cutoff+h*7*DAY)):
            k = (row[0]-cutoff)//(7*DAY)
            bins[k] = bins.get(k, 0)+1
        for k in range(-52, h):
            rows.append({"item_id": item, "timestamp": date_of(cutoff+k*7*DAY)[:10], "target": bins.get(k, 0), "split": "history" if k < 0 else "test"})
    write_csv(root / "data/processed/weekly_reviews.csv", rows)


def audience(conn, root, cutoff):
    """Mention cohorts describe reviewers, not purchase intent or demographics."""
    t = stamp(cutoff)
    aspect_sql = ",".join(f"sum({a}) {a}_mentions" for a in ASPECTS)
    rows = [dict(r) for r in conn.execute(f"SELECT reviewer_id,count(*) review_count,avg(rating) mean_rating,{aspect_sql} FROM events WHERE ts>=? AND ts<? GROUP BY reviewer_id HAVING count(*)>=2 ORDER BY reviewer_id", (t-365*DAY, t))]
    write_csv(root / "data/processed/reviewer_profiles.csv", rows)
    cohorts = []
    for a in ASPECTS:
        users = [r for r in rows if r[f"{a}_mentions"] > 0]
        cohorts.append({"cohort": f"{a}_mentioning_reviewers", "reviewers": len(users), "overlapping_cohorts": True, "meaning": "keyword mention, NOT stated need or positive preference"})
    write_json(root / "data/processed/audience-summary.json", {"as_of_date": cutoff, "cohorts": cohorts, "population": "reviewers with >=2 category reviews in preceding 365 days; not representative consumers"})


def baseline(root, cfg, seconds):
    import numpy as np
    import pandas as pd
    from autogluon.tabular import TabularPredictor
    from sklearn.metrics import average_precision_score, brier_score_loss, mean_absolute_error
    require_category(root,cfg,"task-manifest.json")
    frames = {s: pd.read_csv(root / f"data/processed/{s}.csv") for s in ("train", "validation", "test")}
    output = root / "artifacts/baseline"
    if output.exists():
        raise ValueError("Baseline artifacts already exist; choose a fresh --root or archive the run explicitly")
    output.mkdir(parents=True)
    train, val, test = [frames[s] for s in ("train", "validation", "test")]
    label = "high_review_activity"
    if train[label].nunique() < 2 or val[label].nunique() < 2:
        raise ValueError("Need positive and negative training/validation examples")
    classifier = TabularPredictor(label=label, problem_type="binary", eval_metric="average_precision", path=str(output/"classifier"), verbosity=2).fit(
        train[FEATURES+[label]], tuning_data=val[FEATURES+[label]], time_limit=seconds,
        hyperparameters={"GBM": {"seed":cfg["seed"]}}, num_bag_folds=0, num_stack_levels=0, fit_weighted_ensemble=False)
    p = classifier.predict_proba(test[FEATURES])[1].to_numpy()
    count_train, count_val = train[FEATURES].copy(), val[FEATURES].copy()
    count_train["log_future_reviews"] = np.log1p(train.future_reviews)
    count_val["log_future_reviews"] = np.log1p(val.future_reviews)
    reg = TabularPredictor(label="log_future_reviews", problem_type="regression", eval_metric="mean_absolute_error", path=str(output/"regressor"), verbosity=2).fit(
        count_train, tuning_data=count_val, time_limit=seconds, hyperparameters={"GBM": {"seed":cfg["seed"]}},
        num_bag_folds=0, num_stack_levels=0, fit_weighted_ensemble=False)
    counts = np.maximum(0, np.expm1(reg.predict(test[FEATURES]).to_numpy()))
    y = test[label].to_numpy()
    k = min(100, len(test))
    def precision(scores):
        # stable deterministic tie-break by product ID order
        return float(y[np.argsort(-np.asarray(scores), kind="stable")[:k]].mean())
    rate = float(train[label].mean())
    metrics = {
        "target": cfg["target"], "scope": "temporal evaluation of existing-product review activity; not sales or unlaunched concepts",
        "test_rows": len(test), "test_positives":int(y.sum()), "test_prevalence": float(y.mean()), "precision_k": k,
        "autogluon": {"average_precision": float(average_precision_score(y,p)) if y.sum() else None, "brier": float(brier_score_loss(y,p)), "precision_at_k": precision(p), "review_count_mae": float(mean_absolute_error(test.future_reviews,counts))},
        "baselines": {"train_prevalence_brier": float(brier_score_loss(y,np.full(len(y),rate))), "no_skill_ap": float(y.mean()), "persistence_average_precision":float(average_precision_score(y,test.reviews_90d)) if y.sum() else None, "persistence_precision_at_k": precision(test.reviews_90d), "persistence_count_mae": float(mean_absolute_error(test.future_reviews,test.reviews_90d))},
        "versions": {x: importlib.metadata.version(x) for x in ("autogluon.tabular","lightgbm","pandas","numpy")},
        "config": cfg,
    }
    test[["product_id","as_of_date","future_reviews",label]].assign(predicted_probability=p,predicted_review_count=counts).to_csv(output/"predictions.csv",index=False)
    write_json(output/"metrics.json",metrics)
    print(json.dumps(metrics,indent=2),flush=True)


def forecast(root, cfg, use_chronos):
    import numpy as np
    import pandas as pd
    require_category(root,cfg,"task-manifest.json")
    data = pd.read_csv(root / "data/processed/weekly_reviews.csv")
    truth = data[data.split == "test"].sort_values(["item_id","timestamp"])
    pred_rows = []
    histories = []
    ids = []
    for item, df in data[data.split == "history"].groupby("item_id",sort=True):
        history = df.sort_values("timestamp").target.to_numpy(dtype=float)
        histories.append(history)
        ids.append(item)
        for ts in truth[truth.item_id==item].timestamp:
            pred_rows.append({"item_id":item,"timestamp":ts,"zero":0.0,"last_value":history[-1],"mean_last_4":float(history[-4:].mean())})
    predictions = pd.DataFrame(pred_rows)
    if use_chronos:
        import torch
        from chronos import Chronos2Pipeline
        pipeline = Chronos2Pipeline.from_pretrained("amazon/chronos-2", device_map="cpu")
        quantiles, _ = pipeline.predict_quantiles(
            inputs=[torch.tensor(x,dtype=torch.float32) for x in histories], prediction_length=cfg["forecast_weeks"],
            quantile_levels=[0.1,0.5,0.9], batch_size=16, cross_learning=False)
        for i,item in enumerate(ids):
            q = quantiles[i].detach().cpu().numpy().reshape(cfg["forecast_weeks"],3)
            mask = predictions.item_id == item
            for j,name in enumerate(["chronos_p10","chronos_p50","chronos_p90"]):
                predictions.loc[mask,name] = np.maximum(0,q[:,j])
    merged = truth.merge(predictions,on=["item_id","timestamp"],validate="one_to_one")
    metrics = {"target":"weekly_published_review_count_NOT_sales","series":len(ids),"horizon_weeks":cfg["forecast_weeks"],"cutoff":cfg["forecast_cutoff"],"chronos_executed":use_chronos}
    for model in ["zero","last_value","mean_last_4"] + (["chronos_p50"] if use_chronos else []):
        metrics[model+"_mae"] = float(np.mean(np.abs(merged.target-merged[model])))
    if use_chronos:
        metrics["chronos_interval_80_coverage"] = float(((merged.target>=merged.chronos_p10)&(merged.target<=merged.chronos_p90)).mean())
        losses=[]
        for tau,name in [(0.1,"chronos_p10"),(0.5,"chronos_p50"),(0.9,"chronos_p90")]:
            err=merged.target-merged[name]
            losses.append(float(np.mean(np.maximum(tau*err,(tau-1)*err))))
        metrics["chronos_mean_pinball_loss"] = float(np.mean(losses))
        metrics["chronos_interval_mean_width"] = float((merged.chronos_p90-merged.chronos_p10).mean())
    name = "chronos" if use_chronos else "forecast-naive"
    out=root/"artifacts"/name
    out.mkdir(parents=True,exist_ok=True)
    merged.to_csv(out/"predictions.csv",index=False)
    write_json(out/"metrics.json",metrics)
    print(json.dumps(metrics,indent=2),flush=True)


def validate_choices(path):
    """Never derive offered alternatives, prices or nonchoices from reviews."""
    import pandas as pd
    df = pd.read_csv(path)
    required = {"respondent_id","choice_set_id","alternative_id","price","attribute_score","chosen","is_outside_option","data_origin"}
    missing = required-set(df.columns)
    if missing:
        raise ValueError(f"Missing choice columns: {sorted(missing)}")
    if df.empty or df[list(required)].isna().any().any():
        raise ValueError("Choice data must be nonempty and required fields non-null")
    if df.duplicated(["respondent_id","choice_set_id","alternative_id"]).any():
        raise ValueError("Duplicate choice alternative")
    if not df.data_origin.eq("observed_choice_experiment").all():
        raise ValueError("Only observed_choice_experiment data can fit the choice model; Amazon reviews are not choices")
    if not df.chosen.isin([0,1]).all() or not df.is_outside_option.isin([0,1]).all():
        raise ValueError("chosen and is_outside_option must be binary")
    if (df.price < 0).any() or not all(math.isfinite(float(x)) for x in df.price) or not all(math.isfinite(float(x)) for x in df.attribute_score):
        raise ValueError("Prices must be nonnegative and attributes numeric finite")
    groups = df.groupby(["respondent_id","choice_set_id"])
    if not groups.chosen.sum().eq(1).all() or not groups.size().ge(2).all() or not groups.is_outside_option.sum().eq(1).all():
        raise ValueError("Each task must have >=2 alternatives, exactly one observed choice and one outside option")
    altsets = groups.alternative_id.apply(lambda s:tuple(sorted(s)))
    if altsets.nunique() != 1:
        raise ValueError("This first adapter requires fixed alternative IDs per task")
    outside = df[df.is_outside_option==1]
    if outside.alternative_id.nunique()!=1 or (outside.price!=0).any() or (outside.attribute_score!=0).any():
        raise ValueError("Use a consistent outside option with zero-coded price and attributes")
    return df


def choice_fit(root, path):
    df = validate_choices(path)
    from pymc_marketing.customer_choice.mnl_logit import MNLogit
    # This intentionally small model is an adapter, not a calibrated sales simulator.
    import pandas as pd
    alternatives = sorted(df.alternative_id.unique())
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*",str(a)) for a in alternatives):
        raise ValueError("Alternative IDs must be formula-safe identifiers")
    rows=[]
    for (user,task), group in df.groupby(["respondent_id","choice_set_id"]):
        row={"respondent_id":user,"choice_set_id":task,"choice":group.loc[group.chosen==1,"alternative_id"].iloc[0]}
        for r in group.itertuples():
            row[f"{r.alternative_id}_price"]=r.price
            row[f"{r.alternative_id}_attribute"]=r.attribute_score
        rows.append(row)
    wide=pd.DataFrame(rows)
    equations=[f"{a} ~ {a}_price + {a}_attribute | 1" for a in alternatives]
    model=MNLogit(choice_df=wide,utility_equations=equations,depvar="choice",covariates=["price","attribute"])
    model.fit(draws=500,tune=500,chains=2,random_seed=42)
    out=root/"artifacts/choice"
    out.mkdir(parents=True,exist_ok=True)
    model.save(str(out/"model.nc"))
    write_json(out/"manifest.json",{"input_sha256":digest(path),"data_origin":"observed_choice_experiment","scope":"choice preference, not population sales","model":"MNLogit","validated_on_amazon":False})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=Path.cwd())
    parser.add_argument("--config",type=Path,default=Path("configs/amazon_subscription_boxes.json"))
    commands=parser.add_subparsers(dest="command",required=True)
    for name in ["ingest","prepare","catalog"]:
        commands.add_parser(name)
    d=commands.add_parser("download"); d.add_argument("--metadata",action="store_true")
    b=commands.add_parser("baseline"); b.add_argument("--seconds",type=int,default=60)
    f=commands.add_parser("forecast"); f.add_argument("--chronos",action="store_true")
    for name in ["validate-choices","fit-choice"]:
        c=commands.add_parser(name); c.add_argument("--input",type=Path,required=True)
    args=parser.parse_args()
    root=args.root.resolve()
    config=args.config if args.config.is_absolute() else root/args.config
    cfg=json.loads(config.read_text())
    if args.command=="download": download(root,cfg,args.metadata)
    elif args.command=="catalog": catalog(root,cfg)
    elif args.command=="ingest": ingest(root,cfg)
    elif args.command=="prepare": prepare(root,cfg)
    elif args.command=="baseline": baseline(root,cfg,args.seconds)
    elif args.command=="forecast": forecast(root,cfg,args.chronos)
    elif args.command=="validate-choices":
        df=validate_choices(args.input); print(f"Validated {len(df)} observed choice alternatives")
    elif args.command=="fit-choice": choice_fit(root,args.input)


if __name__=="__main__":
    main()
