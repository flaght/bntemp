"""Read-only desktop result audit and standalone HTML report builder."""
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from contextlib import redirect_stdout
from datetime import datetime
from zoneinfo import ZoneInfo
import importlib.util
import io
import json
import hashlib
import html
import os
import re
import csv
import sys

import numpy as np
import pandas as pd

BASE = Path('/Users/kerry/Desktop/cmp')
OUT = Path(__file__).resolve().parent
PREVIOUS = Path('/Users/kerry/work/bomber1/pro/temp/cmp')
CTA_NEW = BASE / 'kd/cta/88aa2527-c47d-4bca-994d-cb3e0e299740'
CTA_OLD = BASE / 'lxg/cta/output_20261101_103833'
OPT_NEW = BASE / 'kd/opt/e36c2e9f-a954-4228-a0b1-799110a23da7'
OPT_PREV = BASE / 'kd/opt/d14a7231-1535-443b-b354-1405ab8f795b'
if not OPT_PREV.is_dir():
    OPT_PREV = PREVIOUS / 'opt_by_pos/kd/d14a7231-1535-443b-b354-1405ab8f795b'
OPT_OLD = BASE / 'lxg/opt/output_20261102_145127'
D = Decimal


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(frame, path, index=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=index, encoding='utf-8-sig', float_format='%.10f')


def write_json(data, path):
    def default(x):
        if isinstance(x, (np.integer, np.floating)): return x.item()
        if isinstance(x, (Path, pd.Timestamp, Decimal)): return str(x)
        raise TypeError(type(x).__name__)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=default, allow_nan=False)+'\n')


def read_fills(path):
    df = pd.read_csv(path)
    df['timestamp'] = pd.to_datetime(df.timestamp)
    df['day'] = df.timestamp.dt.normalize()
    df['product'] = df.instrument_id.str.extract(r'^([A-Z]+)', expand=False)
    df['multiplier'] = df['product'].map({'IF':300,'IH':300,'IC':200,'IM':200,'MO':100,'IO':100,'HO':100})
    assert df.multiplier.notna().all()
    df['direction'] = df.side.map({'BUY':1,'SELL':-1})
    df['signed_qty'] = df.qty*df.direction
    df['gross_flow'] = -df.signed_qty*df.price*df.multiplier
    return df.sort_values('timestamp',kind='stable').reset_index(drop=True)


def group_fills(df, keys):
    g = df.groupby(keys,as_index=False).agg(qty=('qty','sum'),notional=('price','size'),
        gross_flow=('gross_flow','sum'),commission=('commission','sum'),
        first_time=('timestamp','min'),last_time=('timestamp','max'),raw_rows=('qty','size'))
    notionals = (df.price*df.qty).groupby([df[k] for k in keys]).sum().to_numpy()
    g['price'] = notionals/g.qty
    return g.drop(columns='notional')


def stats(series, initial):
    values=np.r_[initial,series.to_numpy(float)]
    ret=pd.Series(values).pct_change().dropna()
    dd=values/np.maximum.accumulate(values)-1
    std=ret.std(ddof=1)
    return dict(ending_equity=float(values[-1]),profit=float(values[-1]-initial),
        total_return=float(values[-1]/initial-1),maximum_drawdown=float(dd.min()),
        sharpe_252_calendar=float(np.sqrt(252)*ret.mean()/std) if std else None,
        annual_vol_252_calendar=float(std*np.sqrt(252)))


def fee_audit():
    read=lambda filename:list(csv.DictReader((OPT_NEW/filename).open(encoding='utf-8-sig',newline='')))
    fees,fills,settles,orders=[read(n) for n in ('commissions.csv','fills_output.csv','settlements.csv','orders.csv')]
    quant=lambda value:value.quantize(D('.01'),rounding=ROUND_HALF_UP)
    fill_ids={r['order_id'] for r in fills}
    totals=defaultdict(lambda:dict(rows=0,qty=D(0),fee=D(0)))
    checks=[]
    def add(source,row_id,instrument,quantity,actual,expected,category):
        actual=D(actual)
        checks.append(dict(source=source,row_id=row_id,instrument_id=instrument,quantity=quantity,
            actual=actual,expected=expected,difference=actual-expected,category=category))
    for r in fees:
        opt=r['instrument_id'].startswith(('MO','IO','HO'))
        expected=quant(D(r['quantity'])*D('.15')) if opt else quant(D(r['quantity'])*D(r['fill_price'])*D(r['contract_multiplier'])*D('.0003'))
        category=('option_trade' if opt else 'future_trade') if r['client_order_id'] in fill_ids else 'hidden_future_expiry'
        totals[category]['rows']+=1; totals[category]['qty']+=D(r['quantity']);totals[category]['fee']+=D(r['actual_commission'])
        add('commissions',r['client_order_id'],r['instrument_id'],r['quantity'],r['actual_commission'],expected,category)
    for r in settles:
        expected=quant(D(r['quantity'])*D('.15'))
        category='option_settlement';totals[category]['rows']+=1;totals[category]['qty']+=D(r['quantity']);totals[category]['fee']+=D(r['commission'])
        add('settlements',r['client_order_id'],r['instrument_id'],r['quantity'],r['commission'],expected,category)
    for r in fills:
        expected=quant(D(r['qty'])*D('.15')) if r['instrument_id'].startswith(('MO','IO','HO')) else quant(D(r['qty'])*D(r['price'])*D(200)*D('.0003'))
        add('fills_output',r['order_id'],r['instrument_id'],r['qty'],r['commission'],expected,'visible_fill')
    assert not any(r['difference'] for r in checks), 'Unexpected fee calculation mismatch'
    by_fee=defaultdict(lambda:D(0));by_fill=defaultdict(lambda:D(0))
    for r in fees:by_fee[r['client_order_id']]+=D(r['actual_commission'])
    for r in settles:by_fee[r['client_order_id']]+=D(r['commission'])
    for r in fills:by_fill[r['order_id']]+=D(r['commission'])
    assert all(by_fee[k]==v for k,v in by_fill.items()), 'Fee reports disagree for visible orders'
    result=dict(run=str(OPT_NEW),calculation_mismatches=0,checked_rows=len(checks),
        commission_rows=len(fees),settlement_rows=len(settles),fill_rows=len(fills),totals=dict(totals),
        commission_record_sum=sum(D(r['actual_commission']) for r in fees),
        settlement_fee_sum=sum(D(r['commission']) for r in settles),
        fills_fee_sum=sum(by_fill.values()),actual_fee_total=sum(by_fee.values()),
        hidden_fee_total=sum(v for k,v in by_fee.items() if k not in by_fill),
        first_future_example=next(r for r in fees if r['instrument_id'].startswith('IM')))
    dump(pd.DataFrame(checks),OUT/'opt/fee_checks.csv')
    write_json(result,OUT/'opt/fee_audit.json')
    return result


def option_analysis():
    # Reuse the prior verified reconciliation, adapting only paths and the now changed fees.
    source=(PREVIOUS/'opt_by_pos/comparison_20261102_d14a7231/compare_results.py').read_text()
    source=source.replace('BASE = Path(__file__).resolve().parents[1]',f'BASE = Path({str(BASE)!r})')
    source=source.replace('NEW = BASE / "kd/d14a7231-1535-443b-b354-1405ab8f795b"',f'NEW = Path({str(OPT_NEW)!r})')
    source=source.replace('OLD = BASE / "lxg/output_20261102_145127"',f'OLD = Path({str(OPT_OLD)!r})')
    source=source.replace('Decimal("0.000023")','Decimal("0.0003")')
    source=source.replace('actual_new_fees = float(commissions.actual_commission.sum())',
        'actual_new_fees = float(commissions.actual_commission.sum() + settlements.commission.sum())')
    source=source.replace('新版补入平仓按现有0.23bp手续费计算，旧版费用采用成交文件列示值',
        '新版补入平仓按本轮3bp手续费计算；双方期权成交及结算均按0.15元/手')
    # The old HTML prose hardcodes historical fee rates. Generate numerical artifacts only.
    marker='    # 报告保留实际报表异常与归因假设'
    assert marker in source
    source=source.split(marker)[0]+'\n    return findings\n'
    path=OUT/'opt/reconcile.py';path.write_text(source)
    spec=importlib.util.spec_from_file_location('desktop_option_reconcile',path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    result=module.main()
    result['actual']['new_actual_fees_total']=result['actual'].pop('new_actual_fees_in_commissions')
    result['actual']['new_commission_record_fees']=float(pd.read_csv(OPT_NEW/'commissions.csv').actual_commission.sum())
    result['actual']['new_settlement_fees']=float(pd.read_csv(OPT_NEW/'settlements.csv').commission.sum())
    # Compare the new fee rerun with the previous same-market run.
    current,previous=read_fills(OPT_NEW/'fills_output.csv'),read_fills(OPT_PREV/'fills_output.csv')
    cols=['timestamp','instrument_id','side','qty','price']
    a=current.sort_values(cols,kind='stable')[cols].reset_index(drop=True)
    b=previous.sort_values(cols,kind='stable')[cols].reset_index(drop=True)
    assert a.equals(b), 'Trading changed as well as fees; inspect before claiming fee-only rerun'
    old_cash=float(pd.read_csv(OPT_PREV/'account.csv').total.iloc[-1])
    new_cash=float(pd.read_csv(OPT_NEW/'account.csv').total.iloc[-1])
    previous_actual=float(pd.read_csv(OPT_PREV/'commissions.csv').actual_commission.sum()+pd.read_csv(OPT_PREV/'settlements.csv').commission.sum())
    result['rerun_comparison']=dict(same_time_instrument_side_qty_price=True,fill_rows=len(current),
        previous_run=str(OPT_PREV),previous_actual_fees=previous_actual,current_actual_fees=result['actual']['new_actual_fees_total'],
        previous_cash=old_cash,current_cash=new_cash,
        cash_change=new_cash-old_cash,fee_increase=result['actual']['new_actual_fees_total']-previous_actual)
    assert abs((new_cash-old_cash)+result['rerun_comparison']['fee_increase'])<.03
    write_json(result,OUT/'opt/analysis_summary.json')
    return result,module


def cta_analysis():
    out=OUT/'cta';out.mkdir(exist_ok=True)
    nfull=read_fills(CTA_NEW/'fills_output.csv');ofull=read_fills(CTA_OLD/'fills_output.csv')
    initial=15_000_000
    end=min(nfull.day.max(),ofull.day.max())
    start=max(nfull.day.min(),ofull.day.min())
    n=nfull[nfull.day.between(start,end)].copy();o=ofull[ofull.day.between(start,end)].copy()
    index=pd.date_range(start,end,freq='D');instruments=sorted(set(n.instrument_id)|set(o.instrument_id))
    keys=['timestamp','instrument_id','side']
    pairs=group_fills(n,keys).merge(group_fills(o,keys),on=keys,how='outer',suffixes=('_new','_old'),indicator=True)
    both=pairs[pairs._merge.eq('both')].copy()
    assert np.allclose(both.qty_new,both.qty_old)
    both['price_effect']=both.gross_flow_new-both.gross_flow_old
    both['fee_effect']=both.commission_old-both.commission_new
    unmatched=pairs[~pairs._merge.eq('both')].copy()
    sameprice=np.isclose(both.price_new,both.price_old,atol=1e-9,rtol=0)
    dump(both,out/'aligned_fills.csv');dump(unmatched,out/'different_timestamp_fills.csv')
    daily_groups=group_fills(n,['day','instrument_id','side']).merge(group_fills(o,['day','instrument_id','side']),
        on=['day','instrument_id','side'],how='outer',suffixes=('_new','_old'),indicator=True)
    daily_groups['net_qty_delta']=daily_groups.qty_new.fillna(0)-daily_groups.qty_old.fillna(0)
    dump(daily_groups,out/'daily_trade_groups.csv')
    def daily_column(frame,column): return frame.groupby('day')[column].sum().reindex(index,fill_value=0)
    def qty_matrix(frame):return frame.pivot_table(index='day',columns='instrument_id',values='signed_qty',aggfunc='sum',fill_value=0).reindex(index=index,columns=instruments,fill_value=0).cumsum()
    nq,oq=qty_matrix(n),qty_matrix(o)
    delta=nq-oq
    snap=pd.read_csv(CTA_OLD/'positions_output.csv');snap['timestamp']=pd.to_datetime(snap.timestamp);snap['day']=snap.timestamp.dt.normalize()
    valid=snap[snap.instrument_id.ne('__DAY_END__') & snap.price.gt(0) & snap.day.le(end)]
    last=valid.sort_values('timestamp',kind='stable').groupby(['day','instrument_id']).tail(1)
    marks=last.pivot(index='day',columns='instrument_id',values='price').reindex(index=index,columns=instruments).ffill()
    missing=((delta.abs()>1e-9)&marks.isna())
    assert not missing.any().any(), 'Missing quote for a different position'
    multipliers=pd.Series({inst:(300 if inst.startswith(('IF','IH')) else 200) for inst in instruments})
    mark_effect=(delta*marks.fillna(0)*multipliers).sum(axis=1)
    curve=pd.read_csv(CTA_OLD/'net_value.csv');curve['date']=pd.to_datetime(curve.date)
    original=curve.set_index('date').equity.reindex(index)
    assert original.notna().all()
    daily=pd.DataFrame(index=index);daily.index.name='date'
    daily['old_gross_nav']=original
    daily['old_fee_cumulative']=daily_column(o,'commission').cumsum()
    daily['new_fee_cumulative']=daily_column(n,'commission').cumsum()
    daily['execution_flow_difference']=daily_column(n,'gross_flow')-daily_column(o,'gross_flow')
    daily['execution_flow_difference_cumulative']=daily.execution_flow_difference.cumsum()
    daily['position_mark_difference']=mark_effect
    daily['old_net_nav']=original-daily.old_fee_cumulative
    daily['new_shared_gross_nav']=original+daily.execution_flow_difference_cumulative+mark_effect
    daily['new_shared_net_nav']=daily.new_shared_gross_nav-daily.new_fee_cumulative
    daily['net_difference']=daily.new_shared_net_nav-daily.old_net_nav
    # Independently reconstruct the old marks where snapshots permit it.
    oldvaluation=(oq*marks.fillna(0)*multipliers).sum(axis=1)
    daily['old_gross_from_snapshot_marks']=initial+daily_column(o,'gross_flow').cumsum()+oldvaluation
    daily['old_nav_mark_reconstruction_residual']=daily.old_gross_from_snapshot_marks-original
    dump(daily,out/'daily_nav_comparison.csv',index=True)
    diffs=delta.stack().rename('new_minus_old_qty');diffs=diffs[diffs.abs()>1e-9].reset_index()
    diffs.columns=['date','instrument_id','new_minus_old_qty'];dump(diffs,out/'position_different_days.csv')
    def realized(frame):
        state={};total=0.;rows=[]
        for r in frame.itertuples():
            qty,avg=state.get(r.instrument_id,(0.,0.));change=r.signed_qty;px=r.price
            if qty*change>=0:avg=(abs(qty)*avg+abs(change)*px)/(abs(qty)+abs(change))
            else:
                total+=min(abs(qty),abs(change))*(px-avg)*np.sign(qty)*r.multiplier
                if abs(change)>abs(qty):avg=px
                elif abs(change)==abs(qty):avg=0.
            state[r.instrument_id]=(qty+change,avg)
            rows.append((r.timestamp,total))
        return total,state,pd.DataFrame(rows,columns=['timestamp','realized_gross'])
    nr,ns,nledger=realized(nfull);or_,os_,_=realized(o)
    account=pd.read_csv(CTA_NEW/'account.csv');account['timestamp']=pd.to_datetime(account.iloc[:,0],utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    cash=account.set_index('timestamp').total.resample('D').last().reindex(index).ffill()
    daily['actual_new_cash_not_nav']=cash;dump(daily,out/'daily_nav_comparison.csv',index=True)
    ncommon,_,_=realized(n)
    cash_ending=float(account.total.iloc[-1]);cash_reconstructed=initial+nr-float(nfull.commission.sum())
    assert abs(cash_reconstructed-cash_ending)<.02
    main_stats={'old':stats(daily.old_net_nav,initial),'new_common_valuation':stats(daily.new_shared_net_nav,initial)}
    alignedeffect=float(both.price_effect.sum());allprice=float(daily.execution_flow_difference.sum())
    fee_delta=float(n.commission.sum()-o.commission.sum())
    assert abs(float(daily.net_difference.iloc[-1])-(allprice-fee_delta))<.01
    totals=lambda frame:dict(rows=len(frame),qty=float(frame.qty.sum()),fees=float(frame.commission.sum()),first=str(frame.timestamp.min()),last=str(frame.timestamp.max()))
    result=dict(common_period=[str(start.date()),str(end.date())],initial_capital=initial,
        new_run=str(CTA_NEW),old_run=str(CTA_OLD),counts={'new_common':totals(n),'old_common':totals(o),'new_full':totals(nfull)},
        stats=main_stats,matched_groups=len(both),matched_price_different_groups=int((~sameprice).sum()),unmatched_groups=len(unmatched),
        price_effect_aligned=alignedeffect,price_effect_different_dates=allprice-alignedeffect,price_effect_total=allprice,
        fee_delta=fee_delta,net_difference=allprice-fee_delta,position_different_days=diffs.to_dict('records'),
        realized_net_new_common=ncommon-float(n.commission.sum()),realized_net_old_common=or_-float(o.commission.sum()),
        new_cash_common=float(cash.iloc[-1]),new_cash_full=cash_ending,new_cash_reconstruction_residual=cash_reconstructed-cash_ending,
        old_mark_reconciled_dates=int((daily.old_nav_mark_reconstruction_residual.abs()<.01).sum()),
        old_mark_total_dates=len(daily),old_mark_last_day_residual=float(daily.old_nav_mark_reconstruction_residual.iloc[-1]),
        actual_effective_fees={'new_rate_bps':1,'old_rate_bps':1})
    commissions=pd.read_csv(CTA_NEW/'commissions.csv')
    np.testing.assert_allclose(commissions.rate*commissions.commission_multiplier,.0001,atol=1e-15)
    np.testing.assert_allclose(o.commission/(o.qty*o.price*o.multiplier),.0001,atol=1e-15)
    first20=both.sort_values(['timestamp','instrument_id']).head(20);dump(first20,out/'first20_aligned_groups.csv')
    top=both.loc[both.price_effect.abs().sort_values(ascending=False).index].head(30);dump(top,out/'largest_price_effects.csv')
    month=daily[['old_net_nav','new_shared_net_nav']].groupby(daily.index.to_period('M')).last()
    previous_values=month.shift(1);previous_values.iloc[0]=initial
    month[['old_month_profit','new_month_profit']]=(month[['old_net_nav','new_shared_net_nav']].to_numpy()-previous_values.to_numpy())
    month['net_difference_month']=month.new_month_profit-month.old_month_profit;dump(month,out/'monthly_comparison.csv',index=True)
    root=read_fills(BASE/'kd/fills_output.csv');rootold=read_fills(BASE/'lxg/fills_output.csv')
    rc=root[root.day.between(start,end)]
    rootdaily=group_fills(rc,['day','instrument_id','side']).merge(group_fills(n,['day','instrument_id','side']),
        on=['day','instrument_id','side'],suffixes=('_root','_main'),how='outer',indicator=True)
    assert rootdaily._merge.eq('both').all() and np.allclose(rootdaily.qty_root,rootdaily.qty_main)
    rootdaily['root_minus_main_price_flow']=rootdaily.gross_flow_root-rootdaily.gross_flow_main
    dump(rootdaily,out/'root_vs_full_cta.csv')
    root_effective=rc.commission/(rc.qty*rc.price*rc.multiplier)*10000
    result['root_file_check']=dict(old_root_equals_full=sha(BASE/'lxg/fills_output.csv')==sha(CTA_OLD/'fills_output.csv'),
        new_root_equals_full=sha(BASE/'kd/fills_output.csv')==sha(CTA_NEW/'fills_output.csv'),
        new_root_common=totals(rc),root_rate_bps_min=float(root_effective.min()),root_rate_bps_max=float(root_effective.max()),
        root_time_counts=rc.timestamp.dt.strftime('%H:%M').value_counts().to_dict(),
        main_time_counts=n.timestamp.dt.strftime('%H:%M').value_counts().to_dict(),
        root_vs_main_price_cashflow=float(rootdaily.root_minus_main_price_flow.sum()),
        root_vs_main_fee_advantage=float(n.commission.sum()-rc.commission.sum()))
    write_json(result,out/'analysis_summary.json')
    return result,daily


def inventory():
    files=[p for p in BASE.rglob('*') if p.is_file() and p.suffix in {'.csv','.json','.txt','.html','.png'}]
    records=[]
    for p in sorted(files):
        r=dict(path=str(p),relative_path=str(p.relative_to(BASE)),bytes=p.stat().st_size,sha256=sha(p))
        if p.suffix=='.csv':
            with p.open(encoding='utf-8-sig',newline='') as stream:
                reader=csv.reader(stream);columns=next(reader,[]);count=sum(1 for _ in reader)
            r.update(rows=count,columns=columns)
        records.append(r)
    write_json(records,OUT/'source_manifest.json')
    baseline=[]
    for name,new,old in [('CTA新版',CTA_NEW,PREVIOUS/'kd/results'),('CTA老版',CTA_OLD,PREVIOUS/'lxg/results'),
        ('期权此前新版',OPT_PREV,PREVIOUS/'opt_by_pos/kd/d14a7231-1535-443b-b354-1405ab8f795b'),
        ('期权老版',OPT_OLD,PREVIOUS/'opt_by_pos/lxg/output_20261102_145127')]:
        if not new.is_relative_to(BASE):
            continue
        newfiles=[p for p in new.iterdir() if p.is_file() and p.suffix in {'.csv','.json','.txt','.html','.png'}]
        matched=[p for p in newfiles if (old/p.name).is_file() and sha(p)==sha(old/p.name)]
        baseline.append(dict(group=name,files=len(newfiles),identical=len(matched),not_identical=[p.name for p in newfiles if p not in matched]))
    write_json(baseline,OUT/'prior_snapshot_comparison.json')
    return records,baseline


def main():
    for folder in ('cta','opt'): (OUT/folder).mkdir(parents=True,exist_ok=True)
    manifest,baseline=inventory()
    fa=fee_audit()
    opt,module=option_analysis()
    cta,daily=cta_analysis()
    all_result=dict(generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        source_root=str(BASE),prior_snapshot_comparison=baseline,fee_audit=fa,cta=cta,opt=opt)
    write_json(all_result,OUT/'analysis_summary.json')
    # HTML rendering lives beside this analysis and reads these verified outputs.
    render_path=OUT/'render_report.py'
    if render_path.exists():
        spec=importlib.util.spec_from_file_location('desktop_report_render',render_path)
        renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
        renderer.render(all_result,daily,module)
    for r in manifest: assert sha(Path(r['path']))==r['sha256'],r['path']
    print(json.dumps({'output':str(OUT),'fee_audit':fa,'cta':cta,'opt_attribution':opt['attribution'],
        'opt_reconciliation':opt['reconciliation'],'rerun':opt['rerun_comparison'],'prior_snapshot_comparison':baseline},
        ensure_ascii=False,default=str,indent=2))


if __name__=='__main__':main()
