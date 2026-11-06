"""Replaceable source adapters feeding a stable event/product schema."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

from .contracts import ContractError, fingerprint, source_identity, file_hash, save_json, utc_ms, iso


def records(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        if path.name.endswith((".jsonl", ".jsonl.gz")):
            for line in f:
                yield json.loads(line)
        elif path.suffix == ".csv":
            yield from csv.DictReader(f)
        else:
            raise ContractError("Adapters accept CSV or JSONL(.gz), not executable loaders")


def identifier(value):
    if value is None or not isinstance(value, str) or not value.strip():
        raise ContractError("IDs must be nonempty strings")
    return value.strip()


def amazon_event(row, source):
    item = identifier(row.get("parent_asin"))
    entity = hashlib.sha256(identifier(row.get("user_id")).encode()).hexdigest()[:24]
    t = utc_ms(row["timestamp"])
    rating = float(row["rating"])
    if not 1 <= rating <= 5:
        raise ContractError("Invalid review rating")
    text = str(row.get("title") or "") + "\n" + str(row.get("text") or "")
    key = fingerprint([item, entity, t, rating, text])
    return (key, item, entity, t, t, 1.0, rating, text, f"{source['dataset_id']}#event={key}",json.dumps({"verified_purchase":row.get("verified_purchase")}))


def canonical_event(row, source):
    mapping = source.get("event_columns", {})
    row = {canonical: row.get(original) for canonical, original in mapping.items()} if mapping else row
    eid, item = identifier(row.get("event_id")), identifier(row.get("item_id"))
    t, available = utc_ms(row["event_time"]), utc_ms(row["available_at"])
    if available < t:
        raise ContractError("Event available_at cannot precede event_time")
    quantity = float(row.get("count", 1))
    if not quantity.is_integer() or not 0 <= quantity < 1e12:
        raise ContractError("Count tasks require finite nonnegative integers")
    entity = str(row.get("entity_id") or "")
    rating = float(row["rating"]) if row.get("rating") not in (None, "") else None
    attrs=row.get("attributes") or "{}"
    attrs=json.loads(attrs) if isinstance(attrs,str) else attrs
    if not isinstance(attrs,dict):raise ContractError("Event attributes must be a JSON object")
    return (eid, item, entity, t, available, quantity, rating, str(row.get("text") or ""), identifier(row.get("evidence_uri")),json.dumps(attrs))


def product_record(row, source, amazon):
    if not amazon and source.get("product_columns"):
        row={canonical:row.get(original) for canonical,original in source["product_columns"].items()}
    if amazon:
        attrs = row.get("details") or {}
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except json.JSONDecodeError:
                attrs = {"unparsed_details": attrs}
        if not isinstance(attrs, dict):
            attrs = {}
        attrs.update(store_snapshot=row.get("store"), price_snapshot=row.get("price"))
        description = "\n".join(str(x) for x in (row.get("description") or []) + (row.get("features") or []))
        item = identifier(row.get("parent_asin"))
        # Unknown historical availability is represented by NULL, never invented.
        return (item, None, None, str(row.get("title") or ""), description, json.dumps(attrs), f"{source['dataset_id']}#product={item}", 0)
    attrs = row.get("attributes") or "{}"
    attrs = json.loads(attrs) if isinstance(attrs, str) else attrs
    if not isinstance(attrs, dict):
        raise ContractError("Product attributes must be a JSON object")
    return (identifier(row.get("item_id")), utc_ms(row["available_at"]), utc_ms(row.get("effective_at") or row["available_at"]),
            str(row.get("title") or ""), str(row.get("description") or ""), json.dumps(attrs), identifier(row.get("evidence_uri")), 1)


def build(root, spec):
    root = Path(root)
    identity = source_identity(root, spec)
    data_id = fingerprint(identity)
    parent = root / "data/builds"
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / data_id
    if final.exists():
        from .contracts import open_build
        con, _ = open_build(root, spec)
        con.close()
        return data_id
    stage = parent / (".building-" + uuid.uuid4().hex)
    stage.mkdir()
    source = spec["source"]
    con = sqlite3.connect(stage / "canonical.sqlite")
    con.execute("CREATE TABLE events(event_id TEXT PRIMARY KEY,item_id TEXT,entity_id TEXT,event_time INTEGER,available_at INTEGER,count REAL,rating REAL,text TEXT,evidence_uri TEXT,attributes TEXT)")
    con.execute("CREATE TABLE products(item_id TEXT,available_at INTEGER,effective_at INTEGER,title TEXT,description TEXT,attributes TEXT,evidence_uri TEXT,availability_known INTEGER)")
    counts = {"events_read": 0, "events_inserted": 0, "duplicates": 0, "invalid": 0, "products": 0}
    try:
        adapter = amazon_event if source["adapter"] == "amazon_reviews_jsonl" else canonical_event
        end = utc_ms(source["coverage_end"])
        for row in records(root / source["events_path"]):
            counts["events_read"] += 1
            try:
                event = adapter(row, source)
                if not utc_ms("1990-01-01") <= event[3] < end or event[4] >= end:
                    raise ContractError("Event outside declared observation coverage")
                previous = con.execute("SELECT * FROM events WHERE event_id=?", (event[0],)).fetchone()
                if previous is not None:
                    if tuple(previous) != event:
                        raise ContractError("Conflicting records for the same event_id")
                    counts["duplicates"] += 1
                else:
                    con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", event)
                    counts["events_inserted"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                counts["invalid"] += 1
                if source.get("invalid_policy", "error") == "error":
                    raise ContractError(f"Invalid event row {counts['events_read']}: {exc}") from exc
        if source.get("products_path"):
            for row in records(root / source["products_path"]):
                product = product_record(row, source, source["adapter"] == "amazon_reviews_jsonl")
                con.execute("INSERT INTO products VALUES (?,?,?,?,?,?,?,?)", product)
                counts["products"] += 1
        if not counts["events_inserted"]:
            raise ContractError("Empty event input")
        con.execute("CREATE INDEX event_times ON events(event_time,available_at)")
        con.execute("CREATE INDEX item_times ON events(item_id,event_time)")
        con.execute("CREATE INDEX product_versions ON products(item_id,available_at,effective_at)")
        con.commit()
        con.close()
        if source_identity(root, spec) != identity:
            raise ContractError("Input files changed during build")
        save_json(stage / "manifest.json", {"schema_version": 2, "data_id": data_id, "identity": identity,
                  "database_sha256": file_hash(stage / "canonical.sqlite"), "counts": counts,
                  "limitations": source.get("limitations", [])})
        stage.replace(final)
    except BaseException:
        con.close()
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return data_id
