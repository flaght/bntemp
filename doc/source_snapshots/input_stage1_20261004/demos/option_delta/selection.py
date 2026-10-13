"""收盘价研究选约：先期限，再分别筛选Call和Put，绝不跨月补腿。"""
from math import isfinite
from .model import years_to_expiry
from .option_math import implied_greeks


def select(options, quotes, index, forward_by_month, now, config):
    rejected=[]
    def reject(symbol,reason): rejected.append({"symbol":symbol,"reason":reason})
    if index is None or not isfinite(index) or index<=0:
        return [],[{"symbol":config.index_code,"reason":"MISSING_INDEX"}],None
    months={}
    for option in options.values():
        dte=(option.expiry-now.date()).days
        if option.kind not in config.sides or not option.listed<=now.date()<=option.expiry: continue
        if dte<config.min_remaining_days: continue
        if config.expiry_month:
            if option.month!=config.expiry_month: continue
        elif not config.dte_min<=dte<=config.dte_max: continue
        months[option.month]=(option.expiry,dte)
    if not months: return [],[{"symbol":"","reason":"NO_ELIGIBLE_EXPIRY"}],None
    # 只按期限确定月份；不使用其他月份的质量/Delta结果反向选择期限。
    month=min(months,key=lambda m:(abs(months[m][1]-config.target_dte),months[m][0],m))
    x=index if config.model=="bs" else forward_by_month.get(month)
    if x is None or not isfinite(x) or x<=0:
        return [],[{"symbol":month,"reason":"MISSING_SAME_MONTH_FORWARD"}],month
    qualified=[]
    for option in options.values():
        if option.month!=month or option.kind not in config.sides or not option.listed<=now.date()<=option.expiry: continue
        if (option.kind=="C" and option.strike<=index) or (option.kind=="P" and option.strike>=index):
            reject(option.symbol,"NOT_OTM"); continue
        quote=quotes.get(option.symbol)
        if quote is None: reject(option.symbol,"MISSING_BAR"); continue
        if "bar_ns" not in quote or not isfinite(quote["bar_ns"]):
            reject(option.symbol,"MISSING_BAR_TIME"); continue
        if quote["bar_ns"]!=int(now.timestamp()*1e9): reject(option.symbol,"MISMATCHED_BAR_TIME"); continue
        fields=("close","open_interest","volume")
        if any(key not in quote or not isfinite(quote[key]) for key in fields): reject(option.symbol,"MISSING_BAR_FIELDS"); continue
        if any(quote[key]<0 for key in ("open_interest","volume")):
            reject(option.symbol,"INVALID_BAR_FIELDS"); continue
        close=quote["close"]
        if close<=0: reject(option.symbol,"INVALID_CLOSE"); continue
        if quote["open_interest"]<config.min_open_interest or quote["volume"]<config.min_volume:
            reject(option.symbol,"LOW_LIQUIDITY"); continue
        g=implied_greeks(x,option.strike,years_to_expiry(now,option.expiry),config.rate,config.dividend_yield,close,option.kind,config.model)
        if g is None: reject(option.symbol,"INVALID_IV_OR_ARBITRAGE_BOUND"); continue
        qualified.append(dict(symbol=option.symbol,kind=option.kind,month=month,expiry=str(option.expiry),
            strike=option.strike,dte=months[month][1],index=index,model_input=x,model=config.model,
            rate=config.rate,dividend_yield=config.dividend_yield,close=close,bar_ns=quote["bar_ns"],price_source="close",
            iv=g.iv,delta=g.delta,gamma=g.gamma,vega=g.vega,distance=abs(abs(g.delta)-config.target_delta),
            open_interest=quote["open_interest"],volume=quote["volume"],zero_volume_bar=quote["volume"]==0))
    chosen=[]
    for kind in config.sides:
        candidates=[row for row in qualified if row["kind"]==kind and config.delta_min<=abs(row["delta"])<=config.delta_max]
        fallback=False
        if not candidates and config.allow_fallback:
            candidates=[row for row in qualified if row["kind"]==kind and config.fallback_min<=abs(row["delta"])<=config.fallback_max]
            fallback=True
        if candidates:
            best=min(candidates,key=lambda row:(row["distance"],-row["open_interest"],-row["volume"],row["symbol"]))
            chosen.append({**best,"fallback":fallback})
        else: reject(kind,"NO_DELTA_CANDIDATE")
    for row in qualified:
        rejected.append({**row,"reason":"CANDIDATE","selected":any(r["symbol"]==row["symbol"] for r in chosen)})
    return chosen,rejected,month
