import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const out = path.dirname(fileURLToPath(import.meta.url));
const source = path.dirname(out);
const start = '2026-06-10', end = '2026-07-10', baseline = '2026-06-09';
async function csv(name) {
  const lines = (await fs.readFile(path.join(source,name),'utf8')).replace(/^\uFEFF/,'').trim().split(/\r?\n/);
  const keys=lines.shift().split(',');
  return lines.map(line => {
    if(line.includes('"')) throw Error('Quoted CSV requires a full CSV parser');
    const cells=line.split(',');
    if(cells.length!==keys.length) throw Error('CSV column mismatch');
    return Object.fromEntries(keys.map((key,i)=>[key,cells[i]]));
  });
}
const fills = await csv('fills_output.csv');
const trades = await csv('trades.csv');
const equities = await csv('net_value.csv');
const key=(r,trade=false)=>[r[trade?'datetime':'timestamp'],r[trade?'instrument':'instrument_id'],r.side,+r.qty,+r.price].join('|');
if(JSON.stringify(fills.map(r=>key(r)).sort())!==JSON.stringify(trades.map(r=>key(r,true)).sort())) throw Error('Fills and trades do not match');
if(new Set(equities.map(r=>r.date)).size!==equities.length) throw Error('Duplicate equity date');
const base=+equities.find(r=>r.date===baseline)?.equity;
if(!Number.isFinite(base) || base<=0) throw Error('Missing baseline equity');
const selected=fills.filter(r=>r.timestamp.slice(0,10)>=start && r.timestamp.slice(0,10)<=end);
if(!selected.length) throw Error('No fills in requested interval');
const dailyFill=new Map();
for(const f of selected) {
  const day=f.timestamp.slice(0,10);
  const v=dailyFill.get(day)||{count:0,qty:0,fee:0};
  v.count++;v.qty+=+f.qty;v.fee+=+f.commission;dailyFill.set(day,v);
}
let previous=base,peak=base;
const daily=equities.filter(r=>r.date>=start && r.date<=end).sort((a,b)=>a.date.localeCompare(b.date)).map(r=>{
  const equity=+r.equity;
  if(!Number.isFinite(equity)) throw Error('Invalid equity');
  peak=Math.max(peak,equity);
  const row={date:r.date,equity,daily_pnl:equity-previous,daily_return:equity/previous-1,cumulative_pnl:equity-base,cumulative_return:equity/base-1,drawdown:equity/peak-1,...(dailyFill.get(r.date)||{count:0,qty:0,fee:0})};
  previous=equity;return row;
});
const dates=Array.from({length:31},(_,i)=>new Date(Date.UTC(2026,5,10+i)).toISOString().slice(0,10));
if(JSON.stringify(dates)!==JSON.stringify(daily.map(r=>r.date))) throw Error('Incomplete daily equity coverage');
const last=daily.at(-1),worst=daily.reduce((a,b)=>a.drawdown<b.drawdown?a:b);
const sum=k=>daily.reduce((s,r)=>s+r[k],0);
const summary={start,end,baseline,baseline_equity:base,ending_equity:last.equity,profit:last.cumulative_pnl,return:last.cumulative_return,max_drawdown:worst.drawdown,max_drawdown_date:worst.date,fill_count:selected.length,fill_days:dailyFill.size,quantity:sum('qty'),commission:sum('fee'),best_day:daily.reduce((a,b)=>a.daily_pnl>b.daily_pnl?a:b).date,worst_day:daily.reduce((a,b)=>a.daily_pnl<b.daily_pnl?a:b).date,method:'Account equity from net_value.csv. Baseline is 2026-06-09 close. Fees are shown separately and not deducted again. Calendar-day series retained. No annualization.'};
if(Math.abs(sum('daily_pnl')-summary.profit)>0.00001) throw Error('Daily PnL reconciliation failed');
if(Math.abs(summary.profit-258417.34)>0.005 || Math.abs(summary.commission-93816.468)>0.0001 || summary.quantity!==565 || summary.fill_count!==165 || Math.abs(summary.max_drawdown-(-0.00341771715171445))>1e-10) throw Error('Independent control mismatch');
await fs.writeFile(path.join(out,'summary.json'),JSON.stringify(summary,null,2));
const headers=Object.keys(daily[0]);
await fs.writeFile(path.join(out,'daily_performance.csv'),'\uFEFF'+headers.join(',')+'\n'+daily.map(r=>headers.map(h=>r[h]).join(',')).join('\n')+'\n');

const W=1600,H=1210;
const C={ink:'#15283F',muted:'#66788D',grid:'#E8EDF3',blue:'#2563B3',green:'#158373',red:'#C45F5F',bg:'#F5F7FA'};
let parts=[`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${C.blue}" stop-opacity=".15"/><stop offset="100%" stop-color="${C.blue}" stop-opacity=".02"/></linearGradient></defs><rect width="1600" height="1210" fill="${C.bg}"/><g font-family="Hiragino Sans GB, PingFang SC, Arial, sans-serif">`];
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function text(x,y,s,size=20,color=C.ink,weight=400,anchor='start'){parts.push(`<text x="${x}" y="${y}" font-size="${size}" fill="${color}" font-weight="${weight}" text-anchor="${anchor}">${esc(s)}</text>`);}
function line(x1,y1,x2,y2,color=C.grid,width=1,dash=''){parts.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="${width}" ${dash?`stroke-dasharray="${dash}"`:''}/>`);}
function rect(x,y,w,h,fill,rx=0,stroke='none'){parts.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}" fill="${fill}" stroke="${stroke}"/>`);}
const fmt=(v,n=2)=>v.toLocaleString('en-US',{minimumFractionDigits:n,maximumFractionDigits:n});
const pct=(v,n=3)=>(v>=0?'+':'')+(v*100).toFixed(n)+'%';
text(60,68,'策略绩效',38,C.ink,650);
text(60,107,'2026-06-10 至 2026-07-10',23,C.muted);
text(1540,68,'IM / MO',22,C.blue,600,'end');
text(1540,107,'账户权益口径 · 人民币',19,C.muted,400,'end');
line(60,131,1540,131,'#D6DEE8');
const cards=[['区间收益率',pct(summary.return),C.green],['区间盈亏','+'+fmt(summary.profit/10000)+' 万元',C.green],['最大回撤',pct(summary.max_drawdown),C.red],['成交笔数','165 笔',C.ink],['成交手数','565 手',C.ink],['成交手续费',fmt(summary.commission/10000)+' 万元',C.ink]];
cards.forEach(([label,value,color],i)=>{const x=60+i*250;rect(x,156,230,105,'#FFFFFF',10);text(x+19,187,label,18,C.muted);text(x+19,232,value,29,color,600);});

function panel(x,y,w,h,title,sub){rect(x,y,w,h,'#FFFFFF',12);text(x+24,y+35,title,23,C.ink,600);if(sub)text(x+w-24,y+35,sub,16,C.muted,400,'end');}
panel(60,286,1480,402,'累计收益率','期初权益 6,071.91 万元  /  期末权益 6,097.75 万元');
const plot={x:150,y:359,w:1245,h:257};
const curve=[{date:baseline,cumulative_return:0},...daily];
const xx=(i)=>plot.x+i/31*plot.w;
const yy=v=>plot.y+plot.h-(v-(-0.002))/0.01*plot.h;
for(const v of [-0.002,0,0.002,0.004,0.006,0.008]){line(plot.x,yy(v),plot.x+plot.w,yy(v),v===0?'#BECADA':C.grid,1,v===0?'5 4':'');text(plot.x-18,yy(v)+6,(v*100).toFixed(1)+'%',17,C.muted,400,'end');}
const xy=curve.map((r,i)=>[xx(i),yy(r.cumulative_return)]);
const d=xy.map(([x,y],i)=>(i?'L':'M')+x.toFixed(2)+' '+y.toFixed(2)).join(' ');
parts.push(`<path d="${d} L ${xx(31)} ${yy(0)} L ${xx(0)} ${yy(0)} Z" fill="url(#area)"/><path d="${d}" stroke="${C.blue}" stroke-width="3.5" stroke-linejoin="round" fill="none"/>`);
for(const i of [1,6,11,16,21,26,31])text(xx(i),651,curve[i].date.slice(5),17,C.muted,400,'middle');
parts.push(`<circle cx="${xx(31)}" cy="${yy(last.cumulative_return)}" r="5" fill="${C.blue}"/>`);
text(xx(31)+18,yy(last.cumulative_return)+7,pct(last.cumulative_return),23,C.blue,600);
text(150,677,'06-09 为期初基准，收益覆盖 06-10 当日',15,C.muted);

panel(60,710,720,330,'区间回撤','含期初基准');
const ax={x:147,y:787,w:591,h:188};
const dx=i=>ax.x+i/31*ax.w,dy=v=>ax.y+(-v)/0.004*ax.h;
for(const v of [0,-0.001,-0.002,-0.003,-0.004]){line(ax.x,dy(v),ax.x+ax.w,dy(v));text(ax.x-16,dy(v)+6,(v*100).toFixed(1)+'%',16,C.muted,400,'end');}
const dd=[0,...daily.map(r=>r.drawdown)];
const dp=dd.map((v,i)=>(i?'L':'M')+dx(i).toFixed(2)+' '+dy(v).toFixed(2)).join(' ');
parts.push(`<path d="${dp} L ${dx(31)} ${dy(0)} Z" fill="${C.red}" opacity=".13"/><path d="${dp}" stroke="${C.red}" stroke-width="2.8" fill="none"/>`);
const wi=daily.indexOf(worst)+1;
parts.push(`<circle cx="${dx(wi)}" cy="${dy(worst.drawdown)}" r="4.5" fill="${C.red}"/>`);
text(dx(wi)+12,dy(worst.drawdown)-13,'06-22  '+pct(worst.drawdown),18,C.red,600);
for(const i of [1,11,21,31])text(dx(i),1007,curve[i].date.slice(5),16,C.muted,400,'middle');

panel(800,710,740,330,'每日盈亏','单位：万元');
const bx={x:886,y:787,w:610,h:188};
const by=v=>bx.y+(25-v)/50*bx.h;
for(const v of [-25,-12.5,0,12.5,25]){line(bx.x,by(v),bx.x+bx.w,by(v),v===0?'#BECADA':C.grid);text(bx.x-13,by(v)+6,String(v),16,C.muted,400,'end');}
daily.forEach((r,i)=>{const x=bx.x+(i+.5)*bx.w/31;const y=by(r.daily_pnl/10000);rect(x-6.4,Math.min(y,by(0)),12.8,Math.max(.7,Math.abs(y-by(0))),r.daily_pnl>=0?C.green:C.red,1);});
for(const i of [0,10,20,30])text(bx.x+(i+.5)*bx.w/31,1007,daily[i].date.slice(5),16,C.muted,400,'middle');
text(60,1080,'计算口径',18,C.ink,600);
text(160,1080,'收益与盈亏取自 net_value.csv；成交笔数、手数及手续费取自 fills_output.csv。',17,C.muted);
text(60,1111,'以 2026-06-09 收盘权益为基准，回撤按区间内历史权益高点计算；保留自然日，未做年化。',17,C.muted);
text(60,1142,'手续费为成交记录汇总，未从权益变化中重复扣减。账户权益包含跨期持仓影响，成交记录本身不含每日估值。',17,C.muted);
text(60,1180,'数据覆盖 31 个自然日，其中 22 日有成交。',16,C.muted);
parts.push('</g></svg>');
const svg=parts.join('\n');
await fs.writeFile(path.join(out,'performance_20260610_20260710.svg'),svg);
const output=path.join(out,'performance_20260610_20260710.png');
await sharp(Buffer.from(svg),{density:144}).png().toFile(output);
const metadata=await sharp(output).metadata();
console.log(JSON.stringify({output,width:metadata.width,height:metadata.height,...summary},null,2));
