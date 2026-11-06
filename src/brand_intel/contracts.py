"""Versioned contracts. Paths locate inputs; hashes identify their content."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 2
DAY_MS = 86_400_000


class ContractError(ValueError):
    pass


class InsufficientEvidence(ValueError):
    pass


def utc_ms(value):
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value,str) and value.isdecimal() and len(value)>=12:
        return int(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".writing")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def load_spec(path, root):
    spec = json.loads(Path(path).read_text())
    if spec.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("Unsupported schema_version")
    source = spec["source"]
    if source.get("adapter") not in ("amazon_reviews_jsonl", "canonical_csv"):
        raise ContractError("Unknown source adapter")
    if source.get("target_unit") not in ("published_reviews", "units_sold", "events"):
        raise ContractError("Explicit target_unit required")
    if not source.get("coverage_end"):
        raise ContractError("Explicit exclusive coverage_end required")
    utc_ms(source["coverage_end"])
    task = spec["task"]
    if task.get("kind") not in ("trend", "cold_start"):
        raise ContractError("Task kind must be trend or cold_start")
    if not isinstance(task.get("horizon_weeks"), int) or task["horizon_weeks"] <= 0:
        raise ContractError("Positive integral horizon_weeks required")
    if task.get("min_prior_events", 0) < 0:
        raise ContractError("Negative eligibility threshold")
    if any(datetime.fromtimestamp(utc_ms(c) / 1000, timezone.utc).weekday() for c in spec["backtest"]["train_cutoffs"] + spec["backtest"]["test_cutoffs"]):
        raise ContractError("Weekly tasks require Monday cutoffs")
    if not spec["backtest"]["train_cutoffs"] or not spec["backtest"]["test_cutoffs"]:
        raise ContractError("Train and test cutoffs required")
    return spec


def source_identity(root, spec):
    source = spec["source"]
    hashes = {}
    for name in ("events_path", "products_path"):
        if source.get(name):
            path = Path(root) / source[name]
            if not path.is_file():
                raise ContractError(f"Missing declared input: {path}")
            hashes[name] = file_hash(path)
    config = {k: v for k, v in source.items() if k not in ("events_path", "products_path")}
    identity = {"schema_version": SCHEMA_VERSION, "source": config, "input_hashes": hashes,
                "normalizer_sha256":file_hash(Path(__file__).with_name("data.py"))}
    return identity


def open_build(root, spec):
    identity = source_identity(root, spec)
    data_id = fingerprint(identity)
    folder = Path(root) / "data/builds" / data_id
    if not (folder / "manifest.json").exists():
        raise ContractError("No matching immutable data build; run data-build after changing source inputs")
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("identity") != identity or manifest.get("data_id") != data_id:
        raise ContractError("Data identity mismatch")
    if file_hash(folder / "canonical.sqlite") != manifest["database_sha256"]:
        raise ContractError("Canonical data content changed after build")
    conn = sqlite3.connect(f"file:{folder / 'canonical.sqlite'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, manifest


def validate_prepared(manifest, spec, files):
    """Reusable guard for task tables loaded independently of the runner."""
    if manifest.get("task_fingerprint") != fingerprint(spec):
        raise ContractError("Task configuration changed: regenerate task artifacts")
    if manifest.get("task_code_sha256") != file_hash(Path(__file__).with_name("tasks.py")):
        raise ContractError("Task implementation changed: regenerate task artifacts")
    for path in files:
        if manifest["files"].get(Path(path).name) != file_hash(path):
            raise ContractError("Task table hash mismatch")
