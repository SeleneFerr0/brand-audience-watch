"""Comparable count models with explicit distribution semantics."""
import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln,digamma,expit
from scipy.stats import poisson,nbinom
from sklearn.linear_model import PoissonRegressor,LogisticRegression
from sklearn.ensemble import GradientBoostingRegressor


class CountModel:
    def __init__(self,kind,seed=42):
        self.kind,self.seed=kind,seed

    def fit(self,x,y):
        y=np.asarray(y,dtype=float)
        if self.kind=="poisson":
            self.constant_mu=0.5/(len(y)+1) if not y.any() else None
            self.model=None if self.constant_mu is not None else PoissonRegressor(alpha=0.1,max_iter=400).fit(x,y)
        elif self.kind=="negative_binomial":
            design=np.column_stack([np.ones(len(x)),x]); penalty=0.05
            initial=np.zeros(design.shape[1]+1);initial[0]=np.log(max(y.mean(),0.01))
            initial[-1]=np.log(max((y.var()-y.mean())/max(y.mean()**2,0.01),0.1))
            initial[-1]=np.clip(initial[-1],-6,6)
            def objective(theta):
                beta,alpha=theta[:-1],np.exp(theta[-1]);r=1/alpha
                eta=design@beta;mu=np.exp(np.clip(eta,-20,20))
                ll=gammaln(y+r)-gammaln(r)-gammaln(y+1)+r*np.log(r/(r+mu))+y*np.log(np.maximum(mu,1e-12)/(r+mu))
                loss=-ll.mean()+penalty*np.square(beta[1:]).sum()/2
                grad_beta=-(design.T@(r*(y-mu)/(r+mu)*((eta>=-20)&(eta<=20))))/len(y)
                grad_beta[1:]+=penalty*beta[1:]
                d_r=digamma(y+r)-digamma(r)+np.log(r/(r+mu))+1-(r+y)/(r+mu)
                grad_alpha=float(np.mean(r*d_r))
                return loss,np.r_[grad_beta,grad_alpha]
            bounds=[(-10,10)]*design.shape[1]+[(-6,6)]
            result=minimize(objective,initial,method="L-BFGS-B",jac=True,bounds=bounds,options={"maxiter":500})
            if not result.success: raise RuntimeError("Negative binomial optimization failed: "+result.message)
            self.beta,self.alpha=result.x[:-1],float(np.exp(result.x[-1]))
        elif self.kind=="hurdle":
            positive=y>0
            self.prob_constant=float((positive.sum()+.5)/(len(y)+1)) if np.unique(positive).size==1 else None
            self.gate=None if self.prob_constant is not None else LogisticRegression(C=1,max_iter=400,random_state=self.seed).fit(x,positive)
            self.beta=np.zeros(x.shape[1]+1)
            self.beta[0]=np.log(max(float(y[positive].mean()) if positive.any() else 0.1,0.1))
            if positive.any():
                d=np.column_stack([np.ones(positive.sum()),x[positive]])
                yp=y[positive]
                def objective(beta):
                    eta=d@beta;mu=np.exp(np.clip(eta,-20,15));norm=-np.expm1(-mu)
                    ll=yp*np.log(mu)-mu-gammaln(yp+1)-np.log(norm)
                    g=-(d.T@((yp-mu/np.maximum(norm,1e-12))*((eta>=-20)&(eta<=15))))/len(yp)
                    g[1:]+=0.05*beta[1:]
                    return -ll.mean()+0.025*np.square(beta[1:]).sum(),g
                result=minimize(objective,self.beta,method="L-BFGS-B",jac=True,bounds=[(-10,10)]*d.shape[1],options={"maxiter":500})
                if not result.success: raise RuntimeError("Hurdle optimization failed: "+result.message)
                self.beta=result.x
        elif self.kind=="quantile":
            self.models={q:GradientBoostingRegressor(loss="quantile",alpha=q,n_estimators=60,max_depth=2,min_samples_leaf=10,random_state=self.seed).fit(x,y) for q in (.1,.5,.9)}
        else: raise ValueError("Unknown count model")
        return self

    def predict(self,x,threshold):
        n=len(x); d=np.column_stack([np.ones(n),x])
        if self.kind=="poisson":
            mu=np.full(n,self.constant_mu) if self.model is None else np.maximum(self.model.predict(x),1e-10)
            dist=poisson(mu)
            return self._distribution(dist,mu,threshold)
        if self.kind=="negative_binomial":
            mu=np.exp(np.clip(d@self.beta,-20,20));r=1/self.alpha
            return self._distribution(nbinom(r,r/(r+mu)),mu,threshold)
        if self.kind=="hurdle":
            pi=np.full(n,self.prob_constant) if self.gate is None else self.gate.predict_proba(x)[:,1]
            mu=np.exp(np.clip(d@self.beta,-20,15));p0=np.exp(-mu);norm=np.maximum(-np.expm1(-mu),1e-12)
            mean=pi*mu/norm
            qs=[]
            for q in [.1,.5,.9]:
                pos=np.clip((q-(1-pi))/np.maximum(pi,1e-12),0,1-1e-12)
                quant=poisson.ppf(np.minimum(p0+pos*norm,1-1e-12),mu)
                qs.append(np.where(q<=1-pi,0,np.maximum(1,quant)))
            def log_prob(y):
                return np.where(y==0,np.log(np.maximum(1-pi,1e-300)),np.log(np.maximum(pi,1e-300))+poisson.logpmf(y,mu)-np.log(norm))
            return {"point":mean,"point_semantics":"mean","quantiles":np.column_stack(qs),
                    "hit_probability":pi*poisson.sf(threshold-1,mu)/norm,"log_prob":log_prob}
        quantiles=np.sort(np.maximum(0,np.column_stack([self.models[q].predict(x) for q in [.1,.5,.9]])),axis=1)
        return {"point":quantiles[:,1],"point_semantics":"median","quantiles":quantiles,
                "hit_probability":None,"log_prob":None,"quantile_crossing_policy":"rowwise_rearrangement"}

    @staticmethod
    def _distribution(dist,mean,threshold):
        return {"point":mean,"point_semantics":"mean","quantiles":np.column_stack([dist.ppf(q) for q in [.1,.5,.9]]),
                "hit_probability":dist.sf(threshold-1),"log_prob":dist.logpmf}
