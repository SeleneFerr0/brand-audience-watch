"""Trend and cold-start tasks share outcomes, not eligibility assumptions."""
from __future__ import annotations

import json
import numpy as np
import pandas as pd

from .contracts import ContractError, InsufficientEvidence, DAY_MS, utc_ms, iso

TREND_FEATURES = ["prior_count", "count_7d", "count_28d", "count_90d", "count_365d",
                  "rating_mean_90d", "days_since_first_event", "days_since_last_event"]


def product_versions(conn, cutoff):
    rows = conn.execute("SELECT * FROM products WHERE availability_known=1 AND available_at<? AND effective_at<? ORDER BY effective_at,available_at,item_id", (cutoff, cutoff))
    return {r["item_id"]: dict(r) for r in rows}


def numeric_features(spec):
    base = TREND_FEATURES if spec["task"]["kind"] == "trend" else ["days_since_product_record"]
    return base + ["lead_week"] + ["attr_" + x for x in spec["task"].get("numeric_attributes", [])] + ["dyn_"+x for x in spec["task"].get("dynamic_numeric_attributes",[])]


def task_frame(conn, spec, cutoff, labels_available_before=None):
    t = utc_ms(cutoff)
    task, source = spec["task"], spec["source"]
    h = task["horizon_weeks"]
    label_asof = utc_ms(labels_available_before or source["coverage_end"])
    if label_asof > utc_ms(source["coverage_end"]):
        raise ContractError("Label availability cannot exceed declared observation coverage")
    if t + h * 7 * DAY_MS > label_asof:
        raise InsufficientEvidence("Incomplete outcome observation window")
    products = product_versions(conn, t)
    sums = {r["item_id"]: dict(r) for r in conn.execute(f"""
        SELECT item_id,count(*) n_observations,sum(count) prior_count,
        sum(CASE WHEN event_time>={t-7*DAY_MS} THEN count ELSE 0 END) count_7d,
        sum(CASE WHEN event_time>={t-28*DAY_MS} THEN count ELSE 0 END) count_28d,
        sum(CASE WHEN event_time>={t-90*DAY_MS} THEN count ELSE 0 END) count_90d,
        sum(CASE WHEN event_time>={t-365*DAY_MS} THEN count ELSE 0 END) count_365d,
        avg(CASE WHEN event_time>={t-90*DAY_MS} THEN rating END) rating_mean_90d,
        min(event_time) first_event,max(event_time) last_event
        FROM events WHERE event_time<? AND available_at<? GROUP BY item_id
    """, (t,t))}
    if task["kind"] == "trend":
        items = sorted(item for item,r in sums.items() if r["n_observations"] >= task.get("min_prior_events",3) and r["last_event"] >= t-365*DAY_MS)
    else:
        items = sorted(set(products)-set(sums))
        if not items:
            raise InsufficientEvidence("No known-as-of product attributes for zero-history items; snapshot metadata cannot be backdated")
    maximum = task.get("max_items")
    if maximum and len(items) > maximum:
        # Deterministic past-only eligibility cap, explicitly recorded in task config.
        items = sorted(items, key=lambda item:(-sums.get(item,{}).get("count_365d",0),item))[:maximum]
    rows=[]
    for item in items:
        past=sums.get(item,{})
        product=products.get(item)
        history=np.zeros(52,dtype=float)
        for r in conn.execute("SELECT event_time,count FROM events WHERE item_id=? AND event_time>=? AND event_time<? AND available_at<?",(item,t-52*7*DAY_MS,t,t)):
            k=(r[0]-(t-52*7*DAY_MS))//(7*DAY_MS)
            history[k]+=r[1]
        history_text=list(conn.execute("SELECT text,evidence_uri FROM events WHERE item_id=? AND event_time>=? AND event_time<? AND available_at<? ORDER BY event_time DESC,event_id LIMIT 100",(item,t-28*DAY_MS,t,t)))
        attributes=json.loads(product["attributes"]) if product else {}
        text_parts=([product["title"],product["description"],json.dumps(attributes,sort_keys=True)] if product else [])+[r[0] for r in history_text]
        # No labels or future review text enter representations.
        text="\n".join(text_parts)[:task.get("max_text_chars",8000)]
        feature_evidence=([product["evidence_uri"]] if product else [])+[r[1] for r in history_text]
        values={key:past.get(key,0.0) for key in TREND_FEATURES}
        if past:
            values["days_since_first_event"]=(t-past["first_event"])/DAY_MS
            values["days_since_last_event"]=(t-past["last_event"])/DAY_MS
        values["days_since_product_record"]=(t-product["available_at"])/DAY_MS if product else 0.0
        for key in task.get("numeric_attributes",[]):
            value=attributes.get(key)
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value)):
                raise ContractError(f"Non-numeric declared attribute {key}")
            values["attr_"+key]=value
        dynamic=task.get("dynamic_numeric_attributes",[])
        if dynamic:
            found={}
            for record in conn.execute("SELECT attributes,evidence_uri FROM events WHERE item_id=? AND event_time<? AND available_at<? ORDER BY event_time DESC,available_at DESC,event_id LIMIT 100",(item,t,t)):
                attrs=json.loads(record[0] or "{}")
                for key in dynamic:
                    if key in attrs and key not in found:
                        value=attrs[key]
                        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value)):
                            raise ContractError(f"Non-numeric dynamic attribute {key}")
                        found[key]=value;feature_evidence.append(record[1])
            for key in dynamic:values["dyn_"+key]=found.get(key)
        for lead in range(1,h+1):
            start,end=t+(lead-1)*7*DAY_MS,t+lead*7*DAY_MS
            labels=list(conn.execute("SELECT count,evidence_uri FROM events WHERE item_id=? AND event_time>=? AND event_time<? AND available_at<?",(item,start,end,label_asof)))
            rows.append({"item_id":item,"cutoff":iso(t),"target_start":iso(start),"target_end":iso(end),
                         "labels_available_before":iso(label_asof),
                         "target":float(sum(r[0] for r in labels)),"lead_week":lead,"text":text,
                         "feature_evidence":json.dumps(feature_evidence),"label_evidence":json.dumps([r[1] for r in labels]),
                         "history":json.dumps(history.tolist()),**values})
    return pd.DataFrame(rows)


def rolling_folds(spec):
    horizon=spec["task"]["horizon_weeks"]*7*DAY_MS
    tests=sorted(set(spec["backtest"]["test_cutoffs"]),key=utc_ms)
    possible=sorted(set(spec["backtest"]["train_cutoffs"]),key=utc_ms)
    for test in tests:
        training=[c for c in possible if utc_ms(c)+horizon<=utc_ms(test)]
        if not training:
            raise InsufficientEvidence(f"No mature training windows before {test}")
        yield test,training
