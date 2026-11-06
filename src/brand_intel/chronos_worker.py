"""Isolated backend: pinned local weights, same prepared task and row ordering."""
import argparse,json
from pathlib import Path
import numpy as np
import pandas as pd
from .contracts import validate_prepared,ContractError


def main():
    p=argparse.ArgumentParser()
    for name in ["root","spec","manifest","test","output"]: p.add_argument("--"+name,type=Path,required=True)
    a=p.parse_args();spec=json.loads(a.spec.read_text());manifest=json.loads(a.manifest.read_text())
    validate_prepared(manifest,spec,[a.test])
    if spec["task"]["kind"]!="trend": raise ContractError("Chronos worker only supports trend histories")
    from chronos import Chronos2Pipeline
    settings=spec["chronos"]
    pipeline=Chronos2Pipeline.from_pretrained(settings.get("model_id","amazon/chronos-2"),revision=settings["revision"],
                  local_files_only=settings.get("local_files_only",True),device_map="cpu",cache_dir=str(a.root/settings.get("cache_dir",".cache/huggingface/hub")))
    df=pd.read_csv(a.test)
    first=df.groupby("item_id",sort=False).first()
    inputs=[np.asarray(json.loads(h),dtype=np.float32) for h in first.history]
    quantiles,_=pipeline.predict_quantiles(inputs=inputs,prediction_length=spec["task"]["horizon_weeks"],
                  quantile_levels=[.1,.5,.9],batch_size=16,cross_learning=False)
    by_item={item:np.asarray(q.cpu()).reshape(spec["task"]["horizon_weeks"],3) for item,q in zip(first.index,quantiles)}
    q=np.array([by_item[r.item_id][r.lead_week-1] for r in df.itertuples()])
    np.savez(a.output,quantiles=np.sort(np.maximum(0,q),axis=1))


if __name__=="__main__": main()
