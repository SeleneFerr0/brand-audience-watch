"""Scores respect point/quantile/probability semantics; missing is not zero."""
import numpy as np
from sklearn.metrics import average_precision_score


def evaluate(y,prediction,threshold):
    y=np.asarray(y,dtype=float)
    point=np.asarray(prediction["point"],dtype=float)
    quant=np.asarray(prediction["quantiles"],dtype=float)
    if point.shape!=y.shape or quant.shape!=(len(y),3): raise ValueError("Prediction shape mismatch")
    if not np.isfinite(point).all() or not np.isfinite(quant).all() or (point<0).any() or (quant<0).any() or (np.diff(quant,axis=1)<0).any():
        raise ValueError("Invalid count prediction support or quantile order")
    error=y[:,None]-quant
    tau=np.array([.1,.5,.9])
    high=y>=threshold
    k=min(10,len(y))
    # Average over possible orderings within the boundary score tie. A constant
    # baseline must not benefit from the incidental order of candidate IDs.
    boundary=np.sort(point)[-k]
    above=point>boundary; tied=point==boundary
    tie_precision=(high[above].sum()+(k-above.sum())*high[tied].mean())/k
    result={"rows":len(y),"positive_count":int(high.sum()),"zero_fraction":float((y==0).mean()),
            "point_semantics":prediction["point_semantics"],"mae_point":float(np.abs(y-point).mean()),
            "mae_median":float(np.abs(y-quant[:,1]).mean()),"mean_pinball_loss":float(np.maximum(tau*error,(tau-1)*error).mean()),
            "interval_80_coverage":float(((y>=quant[:,0])&(y<=quant[:,2])).mean()),
            "interval_80_width":float((quant[:,2]-quant[:,0]).mean()),"precision_k":k,
            "precision_at_k":float(tie_precision),"ranking_ap_point":float(average_precision_score(high,point)) if high.any() else None,
            "precision_ties":"expected precision under uniform ordering of equal scores",
            "hit_threshold":int(threshold),"hit_brier":None,"nll":None,
            "inference_warning":"Insufficient positive events for strong hit-discovery conclusions" if high.sum()<10 else "No automatic statistical adequacy claim"}
    p=prediction.get("hit_probability")
    if p is not None:
        p=np.asarray(p,dtype=float)
        if p.shape!=y.shape or not np.isfinite(p).all() or (p<0).any() or (p>1).any(): raise ValueError("Invalid tail probabilities")
        result["hit_brier"]=float(np.square(high-p).mean())
    log_prob=prediction.get("log_prob")
    if log_prob is not None:
        lp=log_prob(y)
        if not np.isfinite(lp).all(): raise ValueError("Nonfinite predictive log likelihood")
        result["nll"]=float(-lp.mean())
    return result


def naive_predictions(frame):
    values={"zero":np.zeros(len(frame)),"last_week":frame.count_7d.to_numpy(),"recent_mean":frame.count_28d.to_numpy()/4}
    return {name:{"point":point,"point_semantics":"constant_zero" if name=="zero" else "historical_level",
                  "quantiles":np.repeat(point[:,None],3,axis=1),"hit_probability":None,"log_prob":None} for name,point in values.items()}
