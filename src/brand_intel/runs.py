"""Persistent experiment states; a failed run never publishes completed output."""
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.metadata
from pathlib import Path
import platform
import shutil
import traceback
import uuid

from .contracts import fingerprint, file_hash, save_json


def code_identity():
    return {p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))}


@contextmanager
def run(root, kind, spec, data_id=None):
    identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
    folder = Path(root) / "runs/v2" / identifier
    folder.mkdir(parents=True)
    versions = {}
    for package in ["numpy", "scipy", "scikit-learn", "pandas", "pymc", "arviz", "chronos-forecasting"]:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    state = {"run_id": identifier, "kind": kind, "status": "running", "spec": spec,
             "task_fingerprint": fingerprint(spec), "data_id": data_id, "code": code_identity(),
             "python": platform.python_version(), "versions": versions,
             "started_at": datetime.now(timezone.utc).isoformat()}
    snapshot=folder/"code";snapshot.mkdir()
    for source in Path(__file__).parent.glob("*.py"):
        shutil.copy2(source,snapshot/source.name)
    save_json(folder / "state.json", state)
    try:
        yield folder, state
    except BaseException as exc:
        state.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        (folder / "error.txt").write_text(traceback.format_exc())
        raise
    else:
        if state["status"] == "running":
            state["status"] = "completed"
    finally:
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_json(folder / "state.json", state)
