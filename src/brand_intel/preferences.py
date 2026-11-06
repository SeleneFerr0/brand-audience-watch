"""Hierarchical attribute utilities, consumer holdout and posterior diagnostics.

Synthetic data are accepted only via an explicit validation-only flag; Amazon
reviews are never converted into choice observations.
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.special import softmax

from .contracts import ContractError,InsufficientEvidence,file_hash,save_json
from .runs import run


def validate(frame,features,allow_synthetic=False):
    required={"respondent_id","choice_set_id","alternative_id","chosen","is_outside_option","data_origin","evidence_uri",*features}
    if required-set(frame): raise ContractError(f"Missing preference columns: {sorted(required-set(frame))}")
    if frame.empty or frame[list(required)].isna().any().any(): raise ContractError("Null or empty choice data")
    if not all(frame[k].astype(str).str.strip().ne("").all() for k in ["respondent_id","choice_set_id","alternative_id","evidence_uri"]): raise ContractError("Empty choice identifiers or evidence")
    origin=set(frame.data_origin)
    if origin not in ({"observed_choice_experiment"},{"synthetic_fixture"}): raise ContractError("Mixed or unsupported provenance; reviews are not choice experiments")
    if origin=={"synthetic_fixture"} and not allow_synthetic: raise ContractError("Synthetic inputs require explicit validation-only opt-in")
    if not frame.chosen.isin([0,1]).all() or not frame.is_outside_option.isin([0,1]).all(): raise ContractError("Choice flags must be binary")
    if frame.duplicated(["respondent_id","choice_set_id","alternative_id"]).any(): raise ContractError("Duplicate alternative in a task")
    values=frame[features].apply(pd.to_numeric,errors="raise").to_numpy(dtype=float)
    if not np.isfinite(values).all(): raise ContractError("Nonfinite choice attributes")
    if "price" in features and (frame.price<0).any(): raise ContractError("Negative prices")
    if "currency" in frame and frame.currency.nunique()!=1: raise ContractError("Convert prices to one declared currency before fitting")
    groups=frame.groupby(["respondent_id","choice_set_id"],sort=True)
    if not groups.size().ge(2).all() or not groups.chosen.sum().eq(1).all() or not groups.is_outside_option.sum().eq(1).all():
        raise ContractError("Each choice task needs >=2 alternatives, one choice and one outside option")
    if (frame.loc[frame.is_outside_option==1,features].to_numpy()!=0).any(): raise ContractError("Outside utility attributes must be zero-coded")
    # Require identifiable attribute contrasts, not arbitrary scores with no variation.
    contrasts=np.concatenate([(g[features]-g[features].mean()).to_numpy(dtype=float) for _,g in groups])
    if np.linalg.matrix_rank(contrasts)<len(features): raise InsufficientEvidence("Choice attribute contrasts are rank deficient")
    return frame


def split_consumers(frame,fraction=.25,seed=42):
    if not 0<fraction<1: raise ContractError("Consumer holdout fraction must lie in (0,1)")
    users=np.array(sorted(frame.respondent_id.astype(str).unique()))
    if len(users)<4: raise InsufficientEvidence("At least four consumers required even for interface validation")
    users=np.random.default_rng(seed).permutation(users)
    n=max(1,min(len(users)-2,int(np.ceil(len(users)*fraction))))
    holdout=set(users[:n]); mask=frame.respondent_id.astype(str).isin(holdout)
    return frame.loc[~mask].copy(),frame.loc[mask].copy()


def choice_arrays(frame,features,scale):
    groups=list(frame.groupby(["respondent_id","choice_set_id"],sort=True))
    users=sorted(frame.respondent_id.astype(str).unique());user_map={u:i for i,u in enumerate(users)}
    max_alts=max(len(g) for _,g in groups)
    x=np.zeros((len(groups),max_alts,len(features)))
    mask=np.zeros((len(groups),max_alts),dtype=bool); y=[];person=[]
    for i,((u,_),g) in enumerate(groups):
        g=g.sort_values("alternative_id")
        x[i,:len(g)]=g[features].to_numpy(dtype=float)/scale
        mask[i,:len(g)]=True
        y.append(int(np.flatnonzero(g.chosen.to_numpy())[0]));person.append(user_map[str(u)])
    return x,mask,np.asarray(y),np.asarray(person),users


def diagnostics(idata,rhat_limit=1.01,ess_min=400):
    import arviz as az
    summary=az.summary(idata,var_names=["population_mean","population_sd","consumer_offset"],kind="diagnostics",round_to="none")
    finite=bool(np.isfinite(summary[["r_hat","ess_bulk","ess_tail"]].to_numpy()).all())
    rhat=float(summary.r_hat.max()) if finite else None
    bulk=float(summary.ess_bulk.min()) if finite else None
    tail=float(summary.ess_tail.min()) if finite else None
    divergences=int(idata.sample_stats.diverging.sum())
    passed=bool(finite and rhat<=rhat_limit and min(bulk,tail)>=ess_min and divergences==0)
    return {"rhat_max":rhat,"ess_bulk_min":bulk,"ess_tail_min":tail,"divergences":divergences,
            "thresholds":{"rhat_max":rhat_limit,"ess_min":ess_min,"divergences":0},"passed":passed,
            "scope":"sampling diagnostics do not establish causal validity or population representativeness"}


def score_unseen_consumers(idata,frame,features,scale,seed):
    x,mask,y,people,users=choice_arrays(frame,features,scale)
    pop=idata.posterior.population_mean.stack(sample=("chain","draw")).transpose("sample","attribute").values
    sd=idata.posterior.population_sd.stack(sample=("chain","draw")).transpose("sample","attribute").values
    rng=np.random.default_rng(seed)
    # New consumers receive draws from the learned population, never fitted to their held-out choices.
    selected=np.linspace(0,len(pop)-1,min(500,len(pop)),dtype=int)
    probs=np.zeros(mask.shape)
    for j in selected:
        beta=pop[j]+rng.normal(size=(len(users),len(features)))*sd[j]
        utility=np.einsum("tak,tk->ta",x,beta[people]);utility[~mask]=-1e30
        probs+=softmax(utility,axis=1)
    probs/=len(selected)
    chosen=probs[np.arange(len(y)),y]
    losses=-np.log(np.maximum(chosen,1e-300))
    by_person=[float(losses[people==i].mean()) for i in range(len(users))]
    uniform=-np.log(1/mask.sum(axis=1))
    return {"consumers":len(users),"choice_tasks":len(y),"mean_nll":float(losses.mean()),
            "consumer_macro_nll":float(np.mean(by_person)),"uniform_nll":float(uniform.mean()),
            "accuracy":float((probs.argmax(axis=1)==y).mean()),
            "prediction_semantics":"posterior population predictive for unseen consumers; no personal history available",
            "holdout_outcomes_used_for_fitting":False}


def fit_preferences(root,spec,input_path,allow_synthetic=False):
    frame=pd.read_csv(input_path,dtype={"respondent_id":str,"choice_set_id":str,"alternative_id":str})
    features=spec.get("features",["price","attribute"])
    validate(frame,features,allow_synthetic)
    input_sha=file_hash(input_path)
    run_spec={**spec,"input_sha256":input_sha,"allow_synthetic_validation":allow_synthetic}
    with run(root,"hierarchical_preferences",run_spec) as (folder,state):
        import pymc as pm
        import pytensor.tensor as pt
        train,test=split_consumers(frame,spec.get("holdout_fraction",.25),spec.get("seed",42))
        validate(train,features,allow_synthetic)
        # Scale without centering so outside-option utility remains exactly zero.
        scale=np.maximum(train[features].std().to_numpy(dtype=float),1e-6)
        x,mask,y,people,users=choice_arrays(train,features,scale)
        save_json(folder/"consumer_split.json",{"training_consumers":users,"held_out_consumers":sorted(test.respondent_id.unique()),
             "input_sha256":input_sha,"feature_scale_training_only":scale.tolist(),"features":features})
        coords={"attribute":features,"consumer":users,"task":np.arange(len(y)),"alternative_slot":np.arange(x.shape[1])}
        with pm.Model(coords=coords):
            population_mean=pm.Normal("population_mean",mu=0,sigma=1.5,dims="attribute")
            population_sd=pm.HalfNormal("population_sd",sigma=1,dims="attribute")
            offset=pm.Normal("consumer_offset",0,1,dims=("consumer","attribute"))
            beta=pm.Deterministic("consumer_beta",population_mean+population_sd*offset,dims=("consumer","attribute"))
            utility=pt.sum(pt.as_tensor_variable(x)*beta[people,None,:],axis=2)
            utility=pt.where(mask,utility,-1e30)
            probability=pm.math.softmax(utility,axis=1)
            pm.Categorical("choice",p=probability,observed=y,dims="task")
            idata=pm.sample(draws=spec.get("draws",1000),tune=spec.get("tune",1000),chains=spec.get("chains",4),cores=spec.get("cores",2),
                            target_accept=spec.get("target_accept",.95),random_seed=spec.get("seed",42),progressbar=False,
                            nuts_sampler=spec.get("nuts_sampler","pymc"))
        diagnostic=diagnostics(idata,spec.get("rhat_limit",1.01),spec.get("ess_min",400))
        metrics=score_unseen_consumers(idata,test,features,scale,spec.get("seed",42)+1)
        idata.to_netcdf(folder/"posterior.nc")
        origin=frame.data_origin.iloc[0]
        save_json(folder/"diagnostics.json",diagnostic)
        save_json(folder/"metrics.json",{**metrics,"data_origin":origin,"sampling_diagnostics_passed":diagnostic["passed"],
            "observed_choice_evaluation":origin=="observed_choice_experiment",
            "real_market_validation":False,
            "limitation":"Observed-choice data and convergence alone do not validate population sales or price causality"})
        if origin=="synthetic_fixture": state["status"]="completed_synthetic_validation_only" if diagnostic["passed"] else "synthetic_validation_diagnostics_failed"
        elif not diagnostic["passed"]: state["status"]="diagnostics_failed"
    return folder


def synthetic_choice_fixture(path,seed=42,consumers=32,tasks=8):
    """Only used to test recovery/interfaces; this is not consumer evidence."""
    rng=np.random.default_rng(seed);rows=[]
    for u in range(consumers):
        beta=np.array([-1.2,1.0])+rng.normal(0,[.35,.5])
        for t in range(tasks):
            x=np.array([[rng.uniform(.5,3),rng.uniform(0,2)],[rng.uniform(.5,3),rng.uniform(0,2)],[0,0]])
            chosen=int(rng.choice(3,p=softmax(x@beta)))
            for a in range(3): rows.append({"respondent_id":f"synthetic_{u}","choice_set_id":f"task_{t}","alternative_id":["A","B","Outside"][a],
                "price":x[a,0],"attribute":x[a,1],"chosen":int(a==chosen),"is_outside_option":int(a==2),
                "data_origin":"synthetic_fixture","evidence_uri":f"synthetic://seed-{seed}/person-{u}/task-{t}"})
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    pd.DataFrame(rows).to_csv(path,index=False)
