"""使用分钟收盘价的Delta研究选约回放；不擅自补充交易方向。"""
import argparse
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

if __package__ in (None,""):
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

from demos.option_delta.model import Config

PAIRS={"IO":("IF","000300"),"MO":("IM","000852"),"HO":("IH","000016")}


def run_case(args):
    import pandas as pd
    from market.basic.base import DataType, InstrumentId, InstrumentMeta
    from market.replay.base import FileReplayFeed
    from trader import DataBinding, RuntimeMode, UnifiedStrategyRunner
    from demos.option_delta.local_input import inventory,load_options,load_futures,read_frame,resolve_paths
    from demos.option_delta.parser import FrameReader,SnapshotParser
    from demos.option_delta.strategy import OptionDeltaStrategy
    args=resolve_paths(args)
    config=Config(product=args.product,index_code=args.index_code,future_product=args.future_product,
        model=args.model,sides=tuple(args.sides.split(",")),delta_min=args.delta_min,delta_max=args.delta_max,
        target_delta=args.target_delta,fallback_min=args.fallback_min,fallback_max=args.fallback_max,
        allow_fallback=not args.no_fallback,dte_min=args.dte_min,dte_max=args.dte_max,target_dte=args.target_dte,
        min_remaining_days=args.min_remaining_days,expiry_month=args.expiry_month,select_slots=tuple(args.select_slots.split(",")),
        rate=args.rate,dividend_yield=args.dividend_yield,max_quote_age_seconds=args.max_quote_age_seconds,
        min_open_interest=args.min_open_interest,min_volume=args.min_volume)
    options=load_options(args.opt_basic,args.product,args.index_code,args.option_tick)
    paths=inventory(args.opt_dir,args.start_day,args.end_day,lambda key:key in options)
    indexes=inventory(args.index_dir,args.start_day,args.end_day,lambda key:key==args.index_code)
    futures=load_futures(args.fut_basic,args.future_product) if args.model=="black76" else {}
    future_paths=inventory(args.fut_dir,args.start_day,args.end_day,lambda key:key in futures) if futures else {}
    days=sorted({day for day,key in paths}|{day for day,key in indexes})
    if not days: raise ValueError("请求区间没有期权和指数行情")
    needed={}
    used_futures={}
    for day in days:
        if (day,args.index_code) not in indexes: raise FileNotFoundError(f"{day}: 缺少指数{args.index_code}行情")
        needed[day,args.index_code]=(indexes[day,args.index_code],"index")
        for (file_day,key),path in paths.items():
            option=options[key]
            dte=(option.expiry-day).days
            if file_day!=day or option.kind not in config.sides or not option.listed<=day<=option.expiry or dte<config.min_remaining_days: continue
            if config.expiry_month and option.month!=config.expiry_month: continue
            if not config.expiry_month and not config.dte_min<=dte<=config.dte_max: continue
            needed[day,key]=(path,"option")
            if args.model=="black76":
                future=args.future_product+option.month[2:]
                row=futures.get(future)
                if row is not None and row["listDate"]<=day<=row["lastTradeDate"]:
                    if (day,future) not in future_paths: raise FileNotFoundError(f"{day}: 已上市同月期货{future}缺行情")
                    needed[day,future]=(future_paths[day,future],"future")
                    used_futures[option.month]=future
        if not any(d==day for d,key in paths): raise FileNotFoundError(f"{day}: 缺少{args.product}期权链")
    # 期限选择必须基于静态合约全集；缺行情的目标月份不能被静默替换。
    selected_options=options
    frames={identity:read_frame(path,identity[1],identity[0],kind,args.bar_timestamp) for identity,(path,kind) in needed.items()}
    print(f"选约回放: product={args.product} model={args.model} 日期={days[0]}..{days[-1]} days={len(days)} options={len(selected_options)} mode=SELECT_ONLY price_source=close")
    feed=FileReplayFeed("OPTION_DELTA_REPLAY")
    all_keys={key for day,key in needed}
    ids={key:InstrumentId.from_str(key+(".XSHG" if key==args.index_code else ".CFFEX")) for key in all_keys}
    for key,item in ids.items():
        tick=Decimal(str(selected_options[key].tick)) if key in selected_options else Decimal("0.0001")
        feed.register_instrument(InstrumentMeta(item,price_precision=max(0,-tick.normalize().as_tuple().exponent),
            size_precision=0,price_increment=tick,exchange=str(item.venue)))
    for identity in sorted(frames):
        feed.add_source(needed[identity][0],FrameReader(frames[identity]),SnapshotParser(ids[identity[1]]),"bar")
    strategy=OptionDeltaStrategy("option-delta",config,selected_options,used_futures)
    runner=UnifiedStrategyRunner(RuntimeMode.HISTORICAL)
    runner.add_data_feed("snapshots",feed)
    runner.add_strategy(strategy,
        data_bindings=tuple(DataBinding(key,"snapshots",item,DataType.CUSTOM_BAR,"1-MINUTE") for key,item in ids.items()),
        execution_routes=())
    try:
        replay=runner.run_replay()
    finally:
        runner.stop()
    expected={(day,slot) for day in days for slot in config.select_slots}
    actual={(date.fromisoformat(row["date"]),row["slot"]) for row in strategy.records}
    missing=sorted(expected-actual)
    report=Path(args.report_dir)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from uuid import uuid4
    output=report/(datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")+"_"+uuid4().hex[:8])
    output.mkdir(parents=True,exist_ok=False)
    pd.DataFrame(strategy.selections,columns=list(strategy.selections[0]) if strategy.selections else ["date","slot","symbol","kind","month","delta","close","price_source","zero_volume_bar","fallback"]).to_csv(output/"selections.csv",index=False)
    decisions_frame=pd.DataFrame(strategy.records) if strategy.records else pd.DataFrame(columns=["date","slot","status","count"])
    decisions_frame.to_csv(output/"decisions.csv",index=False)
    audit_frame=pd.DataFrame(strategy.audits) if strategy.audits else pd.DataFrame(columns=["date","slot","symbol","reason","selected"])
    audit_frame.to_csv(output/"candidates.csv",index=False)
    summary={"mode":"SELECT_ONLY","requested":[str(args.start_day),str(args.end_day)],"actual":[str(days[0]),str(days[-1])],
        "config":asdict(config),"snapshots":replay.custom_bars,"decisions":len(strategy.records),"selections":len(strategy.selections),
        "missing_slots":[(str(day),slot) for day,slot in missing],"orders":0,"fills":0,
        "price_source":"close","research_only":True,"order_book_checks":False,
        "price_time_policy":"completed minute bar at signal time; not proof of last trade update",
        "zero_volume_policy":"allowed if min_volume=0; flagged by zero_volume_bar; close may be carried forward",
        "source_document":"中金所股指期权Delta选约策略说明.docx"}
    (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
    print(f"decisions={len(strategy.records)} selections={len(strategy.selections)} 报告={output}")
    if missing: raise ValueError(f"部分选约时点没有行情: {missing[:10]}；报告已保存")
    if args.require_selection and not strategy.selections: raise ValueError("未选出合约；检查candidates.csv中的拒绝原因")


def main():
    p=argparse.ArgumentParser(description="使用分钟close的中金所25至30绝对Delta研究选约，只输出结果，不下单")
    p.add_argument("--product",choices=tuple(PAIRS),default="MO")
    p.add_argument("--index-code")
    p.add_argument("--future-product")
    p.add_argument("--start-day",type=date.fromisoformat,required=True)
    p.add_argument("--end-day",type=date.fromisoformat,required=True)
    p.add_argument("--bar-timestamp",choices=("start","end"),required=True)
    for name in ("data-root","opt-dir","index-dir","fut-dir","opt-basic","fut-basic"):
        p.add_argument("--"+name,type=Path)
    p.add_argument("--option-tick",type=float,default=float(os.environ["OPT_PRICE_INCREMENT"]) if os.environ.get("OPT_PRICE_INCREMENT") else None)
    p.add_argument("--model",choices=("bs","black76"),default="bs")
    p.add_argument("--sides",default="C,P")
    p.add_argument("--expiry-month")
    p.add_argument("--select-slots",default="13:58")
    for name,default in (("delta-min",.25),("delta-max",.30),("target-delta",.275),("fallback-min",.225),("fallback-max",.325),
        ("target-dte",32.5),("rate",.02),("dividend-yield",0.),
        ("min-open-interest",1.),("min-volume",0.)):
        p.add_argument("--"+name,type=float,default=default)
    for name,default in (("dte-min",20),("dte-max",45),("min-remaining-days",7)):
        p.add_argument("--"+name,type=int,default=default)
    p.add_argument("--max-quote-age-seconds",type=int,default=60,
        help="兼容原参数名：仅用于完整分钟帧最大迟到秒数，不检验成交价更新时间")
    p.add_argument("--no-fallback",action="store_true")
    p.add_argument("--require-selection",action="store_true")
    p.add_argument("--report-dir",type=Path,default=Path(__file__).resolve().parent/"results")
    args=p.parse_args()
    pair=PAIRS[args.product]
    args.future_product=(args.future_product or pair[0]).strip().upper()
    args.index_code=(args.index_code or pair[1]).strip().zfill(6)
    args.sides=",".join(item.strip().upper() for item in args.sides.split(","))
    args.select_slots=",".join(item.strip() for item in args.select_slots.split(","))
    if args.end_day<args.start_day or not args.index_code.isdigit() or len(args.index_code)!=6: p.error("日期或指数代码无效")
    try: run_case(args)
    except (ValueError,FileNotFoundError) as exc: p.error(str(exc))


if __name__=="__main__": main()
