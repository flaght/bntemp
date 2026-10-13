"""BS或Black76，价格反解IV；Delta相对对应模型的标的价格。"""
from dataclasses import dataclass
from math import erf, exp, log, sqrt, pi, isfinite


def cdf(x): return (1+erf(x/sqrt(2)))/2


@dataclass(frozen=True)
class Greeks:
    iv: float
    delta: float
    gamma: float
    vega: float


def price(x,k,t,r,q,vol,kind,model):
    if min(x,k,t,vol)<=0: raise ValueError("定价输入非正")
    forward=x if model=="black76" else x*exp((r-q)*t)
    root=vol*sqrt(t)
    d1=log(forward/k)/root+root/2
    d2=d1-root
    if kind=="C": return exp(-r*t)*(forward*cdf(d1)-k*cdf(d2))
    return exp(-r*t)*(k*cdf(-d2)-forward*cdf(-d1))


def implied_greeks(x,k,t,r,q,premium,kind,model):
    if not all(isfinite(n) for n in (x,k,t,r,q,premium)) or min(x,k,t,premium)<=0: return None
    forward=x if model=="black76" else x*exp((r-q)*t)
    disc=exp(-r*t)
    lower=disc*max((forward-k) if kind=="C" else (k-forward),0)
    upper=disc*(forward if kind=="C" else k)
    if not lower<premium<upper: return None
    lo,hi=1e-5,5.
    if not price(x,k,t,r,q,lo,kind,model)<=premium<=price(x,k,t,r,q,hi,kind,model): return None
    for _ in range(80):
        mid=(lo+hi)/2
        if price(x,k,t,r,q,mid,kind,model)>premium: hi=mid
        else: lo=mid
    iv=(lo+hi)/2
    d1=log(forward/k)/(iv*sqrt(t))+iv*sqrt(t)/2
    pdf=exp(-d1*d1/2)/sqrt(2*pi)
    factor=disc if model=="black76" else exp(-q*t)
    delta=factor*(cdf(d1) if kind=="C" else cdf(d1)-1)
    gamma=factor*pdf/(x*iv*sqrt(t))
    vega=disc*forward*pdf*sqrt(t)
    return Greeks(iv,delta,gamma,vega)
