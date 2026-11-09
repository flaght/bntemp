from pathlib import Path
from html import escape
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import json
import pandas as pd

OUT=Path(__file__).resolve().parent


def money(x):return f'{float(x):,.2f}'
def signed(x):return f'{float(x):+,.2f}'
def pct(x,n=2):return f'{float(x)*100:.{n}f}%'


def table(headers,rows,caption=''):
    head=''.join('<th scope="col">'+escape(str(x))+'</th>'for x in headers)
    body=[]
    for row in rows:
        cells=[]
        for i,value in enumerate(row):
            tag='th'if i==0 else'td';scope=' scope="row"'if i==0 else''
            cells.append(f'<{tag}{scope}>{escape(str(value))}</{tag}>')
        body.append('<tr>'+''.join(cells)+'</tr>')
    cap='<caption>'+escape(caption)+'</caption>'if caption else''
    return '<div class="scroll"><table>'+cap+'<thead><tr>'+head+'</tr></thead><tbody>'+''.join(body)+'</tbody></table></div>'


def link(path,label):
    return '<a href="'+escape(os.path.relpath(path,OUT),quote=True)+'">'+escape(label)+'</a>'


def effects(items,title):
    scale=320/max(abs(v)for _,v in items)
    height=85+58*len(items);zero=505
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1040 {height}" role="img" aria-label="{escape(title)}"><title>{escape(title)}</title>',
        f'<line x1="{zero}" y1="22" x2="{zero}" y2="{height-35}" stroke="#a3afc0"/>']
    for i,(label,value)in enumerate(items):
        y=42+i*58;color='#1d4ed8'if i==len(items)-1 else('#16805d'if value>=0 else'#b64e34')
        w=max(abs(value)*scale,2);x=zero if value>=0 else zero-w
        parts.extend([f'<text x="255" y="{y+5}" text-anchor="end" font-size="14" fill="#25364b">{escape(label)}</text>',
            f'<rect x="{x:.2f}" y="{y-14}" width="{w:.2f}" height="29" rx="4" fill="{color}"/>',
            f'<text x="1010" y="{y+5}" text-anchor="end" font-size="14" fill="{color}">{signed(value)} 元</text>'])
    parts.append(f'<text x="{zero}" y="{height-12}" text-anchor="middle" fill="#63718a" font-size="12">0 元</text></svg>')
    return '<figure>'+''.join(parts)+'<figcaption>'+escape(title)+'。各条为独立贡献，蓝色为合计。</figcaption></figure>'


def render(result,cta_daily,helper):
    c=result['cta'];o=result['opt'];f=result['fee_audit'];a=o['actual'];attr=o['attribution'];sc=attr['scenarios'];rr=o['rerun_comparison']
    cs=c['stats'];old=cs['old'];new=cs['new_common_valuation'];counts=c['counts'];root=c['root_file_check']
    optdir=Path(o['new_run']);olddir=Path(o['old_run']);ctadir=Path(c['new_run']);ctaold=Path(c['old_run'])
    optdaily=pd.read_csv(OUT/'opt/daily_attribution_nav.csv',index_col='date',parse_dates=True)
    cta_chart=helper.line_chart(cta_daily,[('old_net_nav','老版：扣除列示费用后','#1d4ed8'),('new_shared_net_nav','新版：共同估值后费后净值','#c2410c')])
    opt_chart=helper.line_chart(optdaily,[('old_nav_after_listed_fees','老版：扣除列示费用后','#1d4ed8'),('scenario_new_net_nav','新版：3 bp、补齐到期平仓后的归因场景','#c2410c')])
    gross_chart=helper.line_chart(optdaily,[('old_reported_gross_nav','老版：共同日期费前净值','#1d4ed8'),('scenario_new_gross_nav','新版：补齐到期平仓后的费前归因场景','#c2410c')])
    top=pd.read_csv(OUT/'cta/largest_price_effects.csv').head(10)
    cta_top=table(['时间','合约／方向','手数','新版价格','老版价格','费前影响（元）'],[
        [r.timestamp,r.instrument_id.replace('.CFFEX','')+' '+r.side,f'{r.qty_new:g}',money(r.price_new),money(r.price_old),signed(r.price_effect)]for r in top.itertuples()],
        '按绝对影响排序的前 10 组。合并同一时间、合约、方向的拆单后，以加权价格比较。')
    monthly=pd.read_csv(OUT/'cta/monthly_comparison.csv')
    cta_month=table(['月份','老版费后利润（元）','新版共同估值费后利润（元）','月度差额（元）'],[
        [r.date,money(r.old_month_profit),money(r.new_month_profit),signed(r.net_difference_month)]for r in monthly.itertuples()],
        '7 月仅包含 7 月 1～3 日；各月差额之和与总差额一致。')
    expiry=pd.read_csv(OUT/'opt/expiry_positions.csv')
    expiry_table=table(['合约','老版期末','新版 framework','新版原生','隐藏到期成交减仓'],[
        [r.instrument_id.replace('.CFFEX',''),f'{r.old_net_0709:g}',f'{r.framework_net:g}',f'{r.native_net:g}',f'{-r.hidden_expiry_delta:g}']for r in expiry.itertuples()]+
        [['合计（手）','0','271','181','90']], '这里只列已经到期的期货，不包含未到期的正常持仓。')
    hidden=pd.read_csv(OUT/'opt/hidden_expiry_fills.csv')
    hidden_table=table(['时间','合约','手数','价格','手续费（元）'],[
        [r.timestamp,r.instrument_id.replace('.CFFEX',''),f'{r.qty:g}',money(r.price),money(r.commission)]for r in hidden.itertuples()],
        '5 笔费用与原生持仓变化可见，但正常成交文件中没有对应成交；时间为北京时间。')
    may=pd.read_csv(OUT/'opt/may29_price_differences.csv').sort_values('timestamp_new')
    may_table=table(['动作','手数','新版时间／价格','老版时间／价格','费前影响（元）'],[
        ['卖出减仓'if r.side=='SELL'else'买入加仓',f'{r.qty:g}',r.timestamp_new[-8:]+'／'+money(r.price_new),
            r.timestamp_old[-8:]+'／'+money(r.price_old),signed(r.price_effect)]for r in may.itertuples()]+
        [['合计','5','均早 4 分钟','两笔减仓、一笔加仓',signed(attr['may29_three_future_trade_effect'])]],
        '2026-05-29，IM2606，合约乘数 200。费前影响＝新版减老版。')
    stats_table=table(['指标','CTA 老版','CTA 新版：共同估值','差额'],[
        ['共同区间成交记录数',counts['old_common']['rows'],counts['new_common']['rows'],'含反手拆单，记录数不等于独立交易次数'],
        ['共同区间成交量（手）',f"{counts['old_common']['qty']:g}",f"{counts['new_common']['qty']:g}",'0'],
        ['手续费（元）',money(counts['old_common']['fees']),money(counts['new_common']['fees']),signed(c['fee_delta'])],
        ['费后已实现利润（元）',money(c['realized_net_old_common']),money(c['realized_net_new_common']),signed(c['realized_net_new_common']-c['realized_net_old_common'])],
        ['费后盯市利润（元）',money(old['profit']),money(new['profit']),signed(c['net_difference'])],
        ['期末总权益（元）',money(old['ending_equity']),money(new['ending_equity']),signed(c['net_difference'])],
        ['盯市收益率',pct(old['total_return']),pct(new['total_return']),f"{(new['total_return']-old['total_return'])*100:+.4f} 个百分点"],
        ['最大回撤',pct(old['maximum_drawdown']),pct(new['maximum_drawdown']),f"改善 {(new['maximum_drawdown']-old['maximum_drawdown'])*100:.4f} 个百分点"],
        ['夏普',f"{old['sharpe_252_calendar']:.2f}",f"{new['sharpe_252_calendar']:.2f}",f"{new['sharpe_252_calendar']-old['sharpe_252_calendar']:+.2f}"],
    ],'共同区间：2026-01-05 至 2026-07-03；初始资金 15,000,000 元；双方有效费率均为 1 bp。')
    fee_table=table(['收费项目','记录数','成交／结算量（手）','核对标准','实际费用（元）'],[
        ['正常期货成交',493,'1,024','成交价 × 合约乘数 × 手数 × 0.0003',money(f['totals']['future_trade']['fee'])],
        ['普通期权成交',407,'1,169','手数 × 0.15',money(f['totals']['option_trade']['fee'])],
        ['期权到期结算',61,'1,023','结算手数 × 0.15',money(f['totals']['option_settlement']['fee'])],
        ['隐藏的期货到期成交',5,'90','成交价 × 200 × 手数 × 0.0003',money(f['totals']['hidden_future_expiry']['fee'])],
        ['全部实际费用',966,'—','commissions 与 settlements 相加',money(f['actual_fee_total'])],
    ],'费用记录与结算记录分别核验；上述记录数不等于正常导出的 961 条成交数。')
    scene_table=table(['口径','期末权益（元）','利润（元）','收益率','最大回撤'],[
        [label,money(sc[key]['ending_equity']),money(sc[key]['profit']),pct(sc[key]['total_return'],4),pct(sc[key]['maximum_drawdown'],4)]
        for label,key in [('老版：共同日期费前','old_reported_gross_nav'),('新版：补齐平仓后费前场景','scenario_new_gross_nav'),
            ('老版：扣列示费用后','old_nav_after_listed_fees'),('新版：3 bp、补齐平仓并扣费场景','scenario_new_net_nav')]],
        '期权组合共同区间：2026-01-05 至 2026-07-09；初始资金 40,000,000 元。新版两行均为归因场景。')
    added_fees=attr['new_fees_with_added_closes']-float(f['fills_fee_sum'])
    inputs=table(['比较对象','新版目录','老版目录','本次使用范围'],[
        ['CTA','kd/cta/88aa2527…','lxg/cta/output_20261101_103833','完整结果作为主比较；共同截止 7 月 3 日'],
        ['期权组合','kd/opt/e36c2e9f…','lxg/opt/output_20261102_145127','最新费率重跑；共同截止 7 月 9 日'],
        ['根目录独立成交文件','kd/fills_output.csv','lxg/fills_output.csv','单独检查版本，不混入主比较'],
    ])
    root_table=table(['项目','根目录 kd/fills_output.csv','完整 CTA 结果'],[
        ['成交时间','09:40','09:41'],['期货有效费率','约 0.23 bp','1 bp'],
        ['共同区间成交记录数','476','476'],['共同区间成交量（手）','665','665'],
        ['共同区间手续费（元）',money(root['new_root_common']['fees']),money(counts['new_common']['fees'])],
    ],'根目录 lxg/fills_output.csv 与老版 CTA 完整目录成交文件的 SHA256 完全相同；新版两份文件不同。')
    baseline=table(['旧快照对应组','本次文件数','内容与旧路径完全一致'],[
        [r['group'],r['files'],r['identical']]for r in result['prior_snapshot_comparison']],
        '比对按 SHA256，不按文件夹名或修改时间判断。期权新版已换成 e36c2e9f，另与存档 d14a7231 核对成交字段。')
    source_links=[
        (OUT/'cta/analysis_summary.json','CTA 结构化分析结果'),(OUT/'cta/daily_nav_comparison.csv','CTA 逐日共同估值与现金比较'),
        (OUT/'cta/aligned_fills.csv','CTA 全部对齐成交及价差'),(OUT/'cta/different_timestamp_fills.csv','CTA 未按同一时间匹配的四组记录'),
        (OUT/'cta/largest_price_effects.csv','CTA 价差影响最大的前 30 组'),(OUT/'cta/monthly_comparison.csv','CTA 月度绩效与差额'),
        (OUT/'cta/root_vs_full_cta.csv','根目录成交文件与完整 CTA 版本比较'),
        (OUT/'opt/fee_checks.csv','最新期权结果逐行费用复算（1,927 项）'),(OUT/'opt/fee_audit.json','最新期权结果费用核验汇总'),
        (OUT/'opt/positions_reconciliation.csv','期权组合全部合约的仓位对账'),(OUT/'opt/expiry_positions.csv','已到期期货仓位对账'),
        (OUT/'opt/hidden_expiry_fills.csv','5 笔隐藏到期成交与费用'),(OUT/'opt/scenario_added_future_closes.csv','归因场景补入的 5 笔完整平仓'),
        (OUT/'opt/daily_contract_price_effects.csv','期权组合逐日逐合约价格与费用影响'),(OUT/'opt/fill_pairs.csv','期权组合实际成交配对与时间差'),
        (OUT/'opt/may29_price_differences.csv','5 月 29 日三笔成交差异'),(OUT/'opt/daily_attribution_nav.csv','期权组合逐日归因净值与实际现金'),
        (OUT/'opt/old_terminal_price_adjustments.csv','老版期权到期价格的终值调整'),(OUT/'analysis_summary.json','全部结构化汇总'),
        (OUT/'source_manifest.json','源文件清单与 SHA256'),
    ]
    stamp=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
    verification=result.get('directory_verification',{})
    verification_note=(f'<p class="note">本次目录复核：{escape(verification["checked_at"])}；'
        f'{verification["files"]} 个结果文件的 SHA256 与详细分析所用输入一致，无新增、删除或内容变化。</p>') if verification else ''
    css='''
:root{--ink:#172033;--muted:#526176;--line:#dce4ee;--blue:#1d4ed8}*{box-sizing:border-box}body{font:15px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--ink);margin:0;background:#fff}main{max-width:1180px;margin:40px auto 70px;padding:0 26px}h1{font-size:29px;line-height:1.4;margin:0 0 12px;font-weight:650}h2{font-size:21px;line-height:1.5;margin:38px 0 16px;padding-top:12px;border-top:1px solid var(--line)}h3{font-size:17px;margin:24px 0 10px}p{margin:12px 0}.note,figcaption{font-size:12px;color:var(--muted)}.lead{font-size:16px}.box{padding:14px 18px;background:#f0f5fc;border-left:4px solid #1d4ed8;margin:18px 0}.critical{background:#fff5f5;border-color:#b91c1c}.caution{background:#fff9ed;border-color:#b7791f}a{color:var(--blue);text-underline-offset:3px}nav{display:flex;flex-wrap:wrap;gap:8px 19px;padding:14px 0;border-block:1px solid var(--line);font-size:13px;margin:22px 0}nav a{text-decoration:none}table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}caption{text-align:left;font-size:12px;color:var(--muted);padding-bottom:8px}thead th{background:#eaf0f7;padding:10px;border-bottom:1px solid #cbd5e1;text-align:right;white-space:nowrap}td,tbody th{padding:9px 10px;border-bottom:1px solid #e2e8f0;text-align:right;vertical-align:top}th:first-child{text-align:left;min-width:140px}tbody th{font-weight:500}.scroll{overflow-x:auto;margin:17px 0}tbody tr:hover{background:#f8fafc}.scroll table.numeric{white-space:nowrap}figure{border:1px solid var(--line);border-radius:6px;padding:12px;margin:22px 0}svg{display:block;width:100%;height:auto;font-family:inherit}.legend{display:inline-block;margin:6px 20px 6px 0;font-size:12px}figcaption{margin:6px 0 0}.formula{background:#f4f7fa;padding:12px 16px;font:13px/1.8 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}code{background:#edf2f7;padding:1px 5px;border-radius:3px;font-size:.92em}details{padding:12px 16px;border:1px solid var(--line);border-radius:5px;margin:16px 0}summary{cursor:pointer;font-weight:600}.path{font-size:12px;overflow-wrap:anywhere}li{padding:2px 0}ul,ol{padding-left:24px}.footer{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
@media(max-width:720px){main{padding:0 16px;margin-top:24px}h1{font-size:24px}table{min-width:680px}h2{font-size:19px}figure{padding:6px;overflow-x:auto}svg{min-width:660px}}
@media print{@page{size:A4;margin:15mm}main{margin:0;padding:0;max-width:none}body{font-size:10pt}h1{font-size:21pt}h2{font-size:15pt;break-after:avoid}h3{break-after:avoid}nav{display:none}.scroll{overflow:visible}table{min-width:0;font-size:8pt}td,th{padding:5px}figure,.box,tr{break-inside:avoid}thead{display:table-header-group}a{color:#172033}svg{min-width:0}}
'''
    content=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>新老版策略详细对比报告（2026-11-09）</title><style>{css}</style></head><body><main>
<header><h1>新老版策略详细对比报告</h1><p class="note">数据来源：Desktop/cmp/kd 与 Desktop/cmp/lxg。期权新版采用 e36c2e9f-a954-4228-a0b1-799110a23da7。生成时间：{stamp}（北京时间）。金额单位：人民币元；差额均为新版减老版。</p>
{verification_note}<p class="lead"><strong>最新期权回测的手续费计算正确，期货已达到 3 bp，期权成交和到期结算均为 0.15 元／手；5 笔期货到期订单的成交回报与残仓错误仍在。</strong> CTA 在共同估值后新版利润高 104,233.36 元；期权组合补齐平仓、采用相同费用标准后的归因场景，新版利润低 60,308.61 元。</p></header>
<nav aria-label="报告目录"><a href="#versions">目录与版本</a><a href="#fees">本轮手续费核验</a><a href="#cta">CTA 绩效</a><a href="#expiry">到期处理与持仓</a><a href="#opt">期权组合绩效</a><a href="#timing">时间与价格</a><a href="#actions">待解决问题</a><a href="#sources">验证与明细</a></nav>

<section id="versions"><h2>1. 使用哪些结果，如何避免版本混用</h2>{inputs}{baseline}
<p>CTA 完整新版 13 个文件、CTA 老版 12 个文件及期权老版 12 个文件，共 37 个文件与此前路径内容完全相同。最新期权 e36c2e9f 是本次新加入的手续费重跑结果；此前 d14a7231 已从桌面对比目录移走，本报告只把工作区存档用于核对重跑变化。</p>
<div class="box caution"><strong>根目录新版成交文件是另一个执行版本。</strong> 它以 09:40 成交、约 0.23 bp 扣费；完整 CTA 结果以 09:41 成交、1 bp 扣费。下表及报告主绩效均使用完整 CTA 结果。</div>
{root_table}<p>按同日、同合约、同方向聚合，根目录新版与完整 CTA 的数量一致；根目录版本相对完整 CTA 的成交价格现金流影响为 {signed(root['root_vs_main_price_cashflow'])} 元，费用少 {money(root['root_vs_main_fee_advantage'])} 元。这两个版本的价格、时点和费率同时不同，不能从两份文件中任选一列拼接报表。</p></section>

<section id="fees"><h2>2. 最新 e36c2e9f 手续费是否正确</h2>
<div class="box"><strong>逐笔计算通过。</strong> 已分别复算 905 条费用记录、61 条结算记录和 961 条成交记录，共 1,927 项核验，计算不符项为 0；正常成交的订单级费用与费用／结算明细逐单一致。</div>
<p>本轮基础期货费率为 <code>0.000023</code>，IM 品种倍数为 <code>13.043478260869565</code>，有效费率约为 <code>0.0003 = 3 bp = 0.03%</code>。期权 MO 使用系数 1，普通成交与结算均为 0.15 元／手。IF、IH、IC 倍数已写入配置，但本轮期货成交只有 IM，因此其他三个品种没有成交可供实证核验。</p>
{fee_table}
<h3>2.1 第一笔期货的复算例子</h3><p>2026-01-05，IM2601 买入 30 手，价格 7,672.2，合约乘数 200：</p>
<div class="formula">7,672.2 × 200 × 30 × 0.0003 = 13,809.96 元<br>费用明细 actual_commission = 13,809.96 元</div>
<p class="note">倍数的小数位会产生远小于 1 分的数值近似；按人民币分四舍五入后，本轮全部期货手续费与直接使用 3 bp 的结果一致。</p>
<h3>2.2 三个费用合计为什么不同</h3>
{table(['文件／口径','费用合计（元）','包含范围'],[
    ['commissions.csv',money(f['commission_record_sum']),'905 条撮合费用记录，包含 5 笔隐藏到期成交'],
    ['settlements.csv',money(f['settlement_fee_sum']),'61 条期权结算费用，不重复加入普通撮合费用记录'],
    ['全部实际费用',money(f['actual_fee_total']),'前两项相加'],
    ['fills_output.csv',money(f['fills_fee_sum']),'961 条正常导出成交，已含期权结算费用'],
    ['实际费用减正常成交列示费用',money(f['hidden_fee_total']),'恰好是 5 笔异常到期订单的费用'],
])}
<div class="box critical"><strong>手续费公式正确，正常成交报表的费用覆盖仍不完整。</strong> 46,245.92 元对应的原生到期成交没有形成正常的 framework 成交回报。因此不能仅汇总 <code>fills_output.csv</code>，就认定这是账户实际总费用。</div>
<h3>2.3 本轮与此前 0.23 bp 结果的变化</h3>
{table(['项目','此前 d14a7231','最新 e36c2e9f','变化'],[
    ['正常导出成交数','961','961','时间、合约、方向、数量、价格全部相同'],
    ['全部实际费用（元）',money(rr['previous_actual_fees']),money(rr['current_actual_fees']),signed(rr['fee_increase'])],
    ['期末现金（元）',money(rr['previous_cash']),money(rr['current_cash']),signed(rr['cash_change'])],
])}
<p>现金减少额与费用增加额逐分一致，证明这次参数修改已实际进入账户扣费。期末现金包含期权权利金及尚未了结的持仓影响，不能直接作为策略总权益。</p></section>

<section id="cta"><h2>3. CTA：统一日期、费用与估值后的结果</h2>{stats_table}
<div class="box caution"><strong>共同估值方法：</strong>用老版日净值作为估值基准，加入两边累计成交现金流差，以及不同仓位在同一报价下的估值差，再分别扣除费用。5 月 29～31 日缺少老版当日报价，沿用 5 月 28 日价格；该曲线不是新版按自身行情导出的原生净值。</div>
<p class="note">夏普与波动率按自然日收益、样本标准差、252 日年化、无风险利率 0 计算；首日相对初始资金计入。老版快照在 180 个自然日中有 179 日可复算其净值；7 月 3 日快照价格与老版最终净值估值时点不同，两边当日末仓一致，所以终值差额不依赖该日缺失的绝对报价。</p>
<figure>{cta_chart}<figcaption>CTA 费后共同估值曲线，单位：百万元。截止 2026-07-03。</figcaption></figure>
{effects([('对齐交易成交价差',c['price_effect_aligned']),('两笔跨日期执行价差',c['price_effect_different_dates']),('费用差额的利润影响',-c['fee_delta']),('费后盯市利润差额',c['net_difference'])],'CTA 费后利润差额归因')}
<p>合并拆单后，369 组的时间、合约、方向、手数一致，其中 365 组价格不同。另有 IH2606、IM2606 各卖 1 手：新版 5 月 29 日 09:41 执行，老版 6 月 1 日 09:41 执行。其间两边仓位不同，6 月 1 日重新一致。</p>
<h3>3.1 月度利润对比</h3>{cta_month}<h3>3.2 成交价差影响最大的交易</h3>{cta_top}
<p>新版原始结果还包含 7 月 6～9 日的 12 条成交、17 手及 2,599.95 元费用，这段现金利润增加 51,440.05 元，已从共同区间排除。老版报告利润为 1,464,360 元，净值 CSV 对应利润为 1,481,320 元，差额 16,960 元体现了已实现与盯市口径的不同。</p></section>

<section id="expiry"><h2>4. 期权组合：期货到期处理仍存在异常</h2>
<div class="box critical"><strong>本轮仍有 5 笔到期订单停留在 INITIALIZED，filled_qty 为 0。</strong> framework 的已到期期货残仓为 271 手，原生账户残仓为 181 手；相差 90 手，正好对应已计费用但未正常导出成交的部分。</div>
{expiry_table}{hidden_table}
<p>五笔自动到期原生部分成交共 90 手，费用 46,245.92 元；剩余 181 手没有完成到期了结。这表明问题涉及订单生命周期、成交回报和持仓同步。<code>report_errors=[]</code> 没有覆盖这些未归属的到期订单，不能用它证明本轮已对账。</p>
{table(['共同区间实际结果','老版','新版 e36c2e9f'],[
    ['正常导出成交数',a['old_fill_rows_common'],a['new_fill_rows']],['期货成交量（手）','1,295','1,024'],['期权成交／结算量（手）','2,192','2,192'],
    ['正常导出成交费用（元）',money(attr['old_fees_common']),money(f['fills_fee_sum'])],['新版全部实际费用（元）','—',money(f['actual_fee_total'])],
])}
<p>老版另有 7 月 10 日的 8 笔成交，净值延伸至 7 月 17 日。主比较统一截止 7 月 9 日。老版 <code>metrics.txt</code> 利润 2,315,774.19 元可复算为其费前现金变化，不能与其最终净值利润 2,188,972.51 元直接混用。</p></section>

<section id="opt"><h2>5. 期权组合：相同费率下的绩效归因场景</h2>
<div class="box caution"><strong>这里仍是分析场景，不是修复后的回测。</strong> 从新版正常成交出发，按老版实际价格补入缺失的 5 笔完整期货到期平仓，以该方案替代原生隐藏的部分成交；双方采用共同日终估值。新版补入平仓费用按本轮 3 bp 计算，普通期权及结算按 0.15 元／手。</div>
{scene_table}
{table(['场景费用对账步骤','金额（元）'],[
    ['新版正常成交列示费用',money(f['fills_fee_sum'])],['补入 5 笔完整期货到期平仓的费用',money(added_fees)],
    ['新版补齐后的场景费用',money(attr['new_fees_with_added_closes'])],['老版共同区间费用',money(attr['old_fees_common'])],
    ['老版费用减新版场景费用',signed(attr['fee_advantage'])],
])}
<p>如果从全部实际费用 553,349.87 元出发，需要先移除隐藏部分成交费用 46,245.92 元，再补入完整平仓费用 {money(added_fees)} 元，结果同样是 644,436.81 元。这样避免把部分到期成交与完整平仓重复计费。</p>
<figure>{gross_chart}<figcaption>期权组合费前归因曲线，单位：百万元。新版场景按老版实际价格补齐漏平。</figcaption></figure>
<figure>{opt_chart}<figcaption>期权组合费后归因曲线，双方期货 3 bp、期权成交与结算 0.15 元／手；新版仍为分析场景。</figcaption></figure>
{effects([('费前成交价格影响',attr['gross_price_effect']),('费用差额的利润影响',attr['fee_advantage']),('费后场景利润差额',attr['net_difference'])],'期权组合相同收费标准下的利润差额')}
<p><strong>费率统一后，费用差只剩 24.10 元；场景费后利润差为 −60,308.61 元。</strong> 该费用差可由成交价不同与逐笔舍入解释。此前低费率场景中的约 59.49 万元费用优势不再适用于本轮结果。</p>
<details><summary>归因场景的计算边界</summary><ol>{''.join('<li>'+escape(s)+'</li>'for s in attr['assumptions'])}</ol><p class="note">老版 61 笔期权到期平仓价按新版官方终值替换，合计调整 −11,091.89 元，已计入场景构造，不再重复算入普通成交价差。每日、合约、方向聚合并补齐平仓后，716 组买卖数量全部一致。</p></details></section>

<section id="timing"><h2>6. 成交时间与价格差异集中在哪里</h2>
{table(['交易类型','时间差：新版减老版','数量／范围','解释'],[
    ['普通期货','0 分钟','490 笔','共同区间可配对成交时间一致'],['5 月 29 日期货','−4 分钟','3 笔、5 手','IM2606 两笔减仓、一笔加仓'],
    ['普通期权','+4 分钟','399 笔、1,157 手','多数从老版 13:56 移至新版 14:00'],['到期期权','+61 分钟','61 笔、1,023 手','老版 14:59 市场平仓，新版 16:00 最终价结算'],
], '配对按同日、同合约、同方向、同数量及出现顺序进行；拆单未直接配对的记录保留在明细中。')}
{may_table}
<p>5 月 29 日三笔期货贡献 −61,040 元，其余交易合计 +707.29 元，合计为 −60,332.71 元费前价格差。老版当天 IM2606 报价与前一日收盘价 8,524.4 高度重合，存在缺行情或旧报价复用嫌疑，但缺少运行时完整行情记录，尚不能把嫌疑认定为原因。</p>
<p>结果中 490 笔普通期货成交时间相同、仅三笔延后，已经不支持“老版期货统一等期权 5 分钟时点成交”的解释。两边成交时间相同也不代表撮合基础价相同；CTA 的 365 组价差和两套原始行情差异仍需使用同一份 Bar 输入验证。</p></section>

<section id="actions"><h2>7. 已解决与尚待处理的事项</h2>
{table(['事项','本次证据','下一步'],[
    ['CTA 费率一致','完整结果两边均 1 bp','保持相同基础元数据与倍数，继续核对行情与成交价'],
    ['期权组合费率一致','本轮所有实际期货费用均符合 3 bp，期权均为 0.15 元／手','费用参数本身无需再次调整'],
    ['期货到期平仓及仓位同步','5 笔 INITIALIZED、90 手隐藏成交、181 手原生残仓','修复订单生命周期、回报归属及到期了结后重跑'],
    ['5 月 29 日行情与时序','CTA 跨日期执行；期权组合三笔早 4 分钟','记录目标、行情进入、批次回调、创建订单、放行、成交时间线'],
    ['统一绩效输出','现金、已实现、盯市以及费前费后口径并存','导出包含浮动盈亏的日终总权益，用同一公式生成绩效'],
])}
<p>最终验收应满足：同一目标持仓输入、同一行情源、同一结束日期和费用规则；已到期期货无异常残仓；原生、framework 与策略仓位一致；成交、结算、费用与账户总权益均可独立对账。</p></section>

<section id="sources"><h2>8. 验证方法、限制与可追溯明细</h2>
<ul><li>本次直接读取桌面目录文件重新计算。源文件在分析前后核对 SHA256，未修改原始回测结果。</li>
<li>费用使用 Decimal 与人民币分四舍五入逐行核验；可见订单的费用明细和成交列示费用逐单一致。费用从现金中实际扣减的证据是改费重跑的现金差额与费用增量完全一致。</li>
<li>CTA 现金终值由加权平均成本实现盈亏与费用重建，误差 0.00 元。期权组合现金含隐藏成交与期权权利金处理后复算误差约 0.0091 元，老版期权共同截止日净值复算残差小于 0.000001 元。</li>
<li>原始目标持仓 CSV 及完整服务器行情未随结果提供。新版 input_manifest 记录的部分输入证据仅有文件大小和修改时间，不能替代两边原件的内容哈希比较。</li>
<li>CTA 共同估值中保留缺失日期报价前填规则；7 月 3 日老版快照与最终净值估值时点不同，绝对估值仍以老版净值为锚。各场景不代表真实修复后重新撮合结果。</li>
<li>本报告不直接比较原报表的胜率、原始交易次数和夏普，因为成交拆单、统计对象及收益曲线口径不同；主表采用明确列示的共同口径。</li></ul>
<h3>分析明细</h3><ul>{''.join('<li>'+link(path,label)+'</li>'for path,label in source_links)}</ul>
<details><summary>直接查看原始结果文件</summary><ul>
<li>{link(optdir/'commission_config.json','本轮期权手续费配置')}</li><li>{link(optdir/'commissions.csv','本轮期权撮合费用明细')}</li><li>{link(optdir/'settlements.csv','本轮期权结算明细')}</li><li>{link(optdir/'fills_output.csv','本轮期权正常成交文件')}</li>
<li>{link(optdir/'orders.csv','本轮期权订单状态')}</li><li>{link(optdir/'positions.csv','本轮期权原生持仓')}</li><li>{link(optdir/'summary.json','本轮期权策略与 framework 汇总')}</li>
<li>{link(olddir/'fills_output.csv','老版期权成交文件')}</li><li>{link(olddir/'net_value.csv','老版期权日净值')}</li><li>{link(ctadir/'fills_output.csv','完整 CTA 新版成交文件')}</li><li>{link(ctaold/'fills_output.csv','完整 CTA 老版成交文件')}</li></ul>
<p class="path">CTA 新版：{escape(str(ctadir))}<br>CTA 老版：{escape(str(ctaold))}<br>期权新版：{escape(str(optdir))}<br>期权老版：{escape(str(olddir))}<br>期权此前结果存档：{escape(rr['previous_run'])}</p></details></section>
<footer class="footer">新老版策略详细对比报告 · 2026-11-09<br>HTML 内嵌样式与图表，可离线打开；分析明细位于报告同目录，原始数据链接指向桌面对比目录及明确列示的历史存档。</footer>
</main></body></html>'''
    (OUT/'comparison_report.html').write_text(content,encoding='utf-8')
