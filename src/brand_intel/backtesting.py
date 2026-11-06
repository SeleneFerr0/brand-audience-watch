"""Rolling development backtests with immutable inputs and per-backend status."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import numpy as np
import pandas as pd

from .contracts import open_build,save_json,fingerprint,file_hash,InsufficientEvidence,validate_prepared
from .tasks import task_frame,rolling_folds,numeric_features
from .representations import Representation
from .count_models import CountModel
from .evaluation import evaluate,naive_predictions
from .runs import run


def chronos_prediction(root,spec,folder,manifest,test):
    settings=spec.get("chronos",{})
    if not settings.get("enabled",False):
        return None,{"status":"not_requested"}
    revision=settings.get("revision")
    if not revision or len(revision)!=40 or any(c not in "0123456789abcdef" for c in revision):
        return None,{"status":"unavailable","reason":"A pinned 40-character model revision is required; no main-branch weight guessing"}
    cache=Path(root)/settings.get("cache_dir",".cache/huggingface/hub")
    snapshot=cache/("models--"+settings.get("model_id","amazon/chronos-2").replace("/","--"))/"snapshots"/revision
    if settings.get("local_files_only",True) and not all((snapshot/f).is_file() for f in ["config.json","model.safetensors"]):
        return None,{"status":"unavailable","reason":"Pinned weights not present in the declared local cache; retrieve them separately before benchmarking","revision":revision}
    executable=Path(root)/settings.get("python",".venv/bin/python")
    output=folder/"chronos_predictions.npz"
    command=[str(executable),"-B","-m","brand_intel.chronos_worker","--root",str(root),"--spec",str(folder/"spec.json"),"--manifest",str(folder/"prepared.json"),"--test",str(folder/"test.csv"),"--output",str(output)]
    try:
        with (folder/"chronos.log").open("w") as log:
            p=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=settings.get("timeout_seconds",150))
        if p.returncode: return None,{"status":"failed","reason":f"Backend exit {p.returncode}; see chronos.log"}
        q=np.load(output,allow_pickle=False)["quantiles"]
        if q.shape!=(len(test),3): raise ValueError("Chronos output shape mismatch")
        return {"point":q[:,1],"point_semantics":"median","quantiles":q,"hit_probability":None,"log_prob":None},{"status":"completed","revision":revision}
    except (OSError,subprocess.TimeoutExpired,ValueError) as exc:
        return None,{"status":"failed","reason":str(exc)}


def backtest(root,spec):
    conn,data_manifest=open_build(root,spec)
    results=[]
    with run(root,"backtest",spec,data_manifest["data_id"]) as (folder,state):
        try:
            frames={}
            failed_optional=False
            for index,(cutoff,training) in enumerate(rolling_folds(spec)):
                fold=folder/f"fold-{index:02d}";fold.mkdir()
                # Past training labels may have arrived after their outcome week;
                # only use those visible at this fold's forecast origin.
                for c in training:
                    key=(c,cutoff)
                    if key not in frames: frames[key]=task_frame(conn,spec,c,labels_available_before=cutoff)
                test_key=(cutoff,"evaluation")
                if test_key not in frames: frames[test_key]=task_frame(conn,spec,cutoff)
                train=pd.concat([frames[(c,cutoff)] for c in training],ignore_index=True)
                test=frames[test_key].copy()
                if train.empty or test.empty: raise InsufficientEvidence(f"Empty task population at {cutoff}")
                train.to_csv(fold/"train.csv",index=False);test.to_csv(fold/"test.csv",index=False)
                save_json(fold/"spec.json",spec)
                prepared={"task_fingerprint":fingerprint(spec),"data_id":data_manifest["data_id"],
                          "task_code_sha256":file_hash(Path(__file__).with_name("tasks.py")),
                          "training_labels_available_before":cutoff,
                          "files":{name:file_hash(fold/name) for name in ["train.csv","test.csv"]}}
                save_json(fold/"prepared.json",prepared)
                validate_prepared(prepared,spec,[fold/"train.csv",fold/"test.csv"])
                threshold=max(2,int(np.ceil(np.quantile(train.target,.9))))
                predictions=naive_predictions(test)
                statuses={name:{"status":"completed"} for name in predictions}
                transforms={}
                for representation in spec.get("representations",["numeric","word_tfidf","char_tfidf"]):
                    transform=Representation(numeric_features(spec),representation,spec.get("seed",42))
                    x=transform.fit_transform(train);xt=transform.transform(test)
                    transforms[representation]={"text_status":transform.text_status,"columns":numeric_features(spec),"dimensions":x.shape[1],
                         "vocabulary_sha":fingerprint({k:int(v) for k,v in transform.vectorizer.vocabulary_.items()}) if transform.vectorizer is not None else None}
                    if representation!="numeric" and transform.text_status!="fitted_on_training_only":
                        for kind in spec.get("models",["poisson","negative_binomial","hurdle","quantile"]):
                            statuses[kind+"/"+representation]={"status":"unsupported_missing_text","reason":"No training text vocabulary; numeric fallback is not a text comparison"}
                        continue
                    for kind in spec.get("models",["poisson","negative_binomial","hurdle","quantile"]):
                        name=kind+"/"+representation
                        try:
                            model=CountModel(kind,spec.get("seed",42)).fit(x,train.target.to_numpy())
                            predictions[name]=model.predict(xt,threshold)
                            statuses[name]={"status":"completed","dispersion_alpha":getattr(model,"alpha",None)}
                        except (ValueError,RuntimeError,FloatingPointError) as exc:
                            statuses[name]={"status":"failed","reason":str(exc)}
                if spec["task"]["kind"]=="trend":
                    pred,chronos_status=chronos_prediction(root,spec,fold,prepared,test)
                    statuses["chronos"]=chronos_status
                    if pred is not None: predictions["chronos"]=pred
                    if chronos_status["status"] in ("failed","unavailable"): failed_optional=True
                else:
                    statuses["chronos"]={"status":"unsupported","reason":"This adapter requires observed product histories; cold-start reference trajectory model is separate"}
                scores={}
                for name,pred in predictions.items():
                    try:
                        overall=evaluate(test.target,pred,threshold)
                        by_lead={}
                        for lead,part in test.groupby("lead_week"):
                            ix=part.index.to_numpy()
                            sliced={k:(v[ix] if isinstance(v,np.ndarray) else v) for k,v in pred.items()}
                            # Distribution callable closes over full parameters. Evaluate aggregate NLL only.
                            sliced["log_prob"]=None
                            by_lead[str(lead)]=evaluate(part.target,sliced,threshold)
                        scores[name]={"overall":overall,"by_lead_week":by_lead}
                        prediction_table=test[["item_id","cutoff","lead_week","target_start","target_end","target"]].copy()
                        prediction_table["point"]=pred["point"]
                        for j,q in enumerate(["p10","p50","p90"]): prediction_table[q]=pred["quantiles"][:,j]
                        prediction_table.to_csv(fold/(name.replace("/","--")+".csv"),index=False)
                    except ValueError as exc:
                        statuses[name]={"status":"failed","reason":str(exc)}
                save_json(fold/"representations.json",transforms)
                save_json(fold/"scores.json",scores);save_json(fold/"models.json",statuses)
                results.append({"cutoff":cutoff,"training_cutoffs":training,"train_rows":len(train),"test_rows":len(test),"scores":scores,"models":statuses})
            summary=[]
            names=sorted({name for f in results for name in f["scores"]})
            for name in names:
                ss=[f["scores"][name]["overall"] for f in results if name in f["scores"]]
                summary.append({"model":name,"completed_folds":len(ss),"expected_folds":len(results),
                    "macro_mae_point":float(np.mean([s["mae_point"] for s in ss])),
                    "macro_mae_median":float(np.mean([s["mae_median"] for s in ss])),
                    "macro_pinball":float(np.mean([s["mean_pinball_loss"] for s in ss]))})
            failures=[(f["cutoff"],name) for f in results for name,s in f["models"].items() if name!="chronos" and s["status"]=="failed"]
            save_json(folder/"summary.json",{"evaluation_role":"development_backtest_not_untouched_final_test","target_unit":spec["source"]["target_unit"],
                 "task_kind":spec["task"]["kind"],"outcome_semantics":"one weekly count at each lead; same items/cutoffs/horizons for all models", "folds":results,"leaderboard":summary,
                 "required_model_failures":failures,"inference":"Do not treat overlapping item/period rows as independent; no global superiority claim from this pilot"})
            if failures: state["status"]="partial_required_model_failures"
            elif failed_optional: state["status"]="completed_with_unavailable_optional_backend"
            state["completed_folds"]=len(results)
        except InsufficientEvidence as exc:
            state.update(status="insufficient_evidence",reason=str(exc))
            save_json(folder/"capabilities.json",{"supported":False,"reason":str(exc)})
        finally:
            conn.close()
    return folder
