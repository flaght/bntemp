from datetime import datetime
from zoneinfo import ZoneInfo
from trader.template import StrategyTemplate
from .selection import select


class OptionDeltaStrategy(StrategyTemplate):
    """文档仅规定选约，因此默认只输出结果，不推断买卖方向。"""
    def __init__(self,strategy_id,config,options,futures,callback=None):
        super().__init__(strategy_id)
        self.config=config
        self.options=options
        self.futures=futures
        self.callback=callback
        self.latest={}
        self.pending_ns=None
        self.last_ns=-1
        self.slots=set()
        self.records=[]
        self.selections=[]
        self.audits=[]

    def on_custom_bar(self,data_key,event):
        stamp=event.ts_event
        if stamp<self.last_ns: raise ValueError("行情时间倒退")
        if stamp % 60_000_000_000:
            raise ValueError("分钟快照时间必须对齐整分钟")
        if self.pending_ns is not None and stamp>self.pending_ns:
            self._evaluate(self.pending_ns,stamp)
        if data_key in self.latest and self.latest[data_key]["event_ns"]==stamp:
            raise ValueError(f"同一分钟重复行情: {data_key}")
        self.last_ns=stamp
        self.pending_ns=stamp
        self.latest[data_key]={**event.factors,"event_ns":stamp}

    def _evaluate(self,stamp,observed_ns):
        now=datetime.fromtimestamp(stamp/1e9,ZoneInfo("Asia/Shanghai"))
        key=(now.date(),now.strftime("%H:%M"))
        if key[1] not in self.config.select_slots or key in self.slots: return
        self.slots.add(key)
        if observed_ns-stamp>self.config.max_quote_age_seconds*1e9:
            self.records.append(dict(signal_ns=stamp,date=str(key[0]),slot=key[1],status="LATE_FRAME",count=0))
            return
        def current_price(symbol):
            row=self.latest.get(symbol)
            return row["close"] if row and row["event_ns"]==stamp else None
        index=current_price(self.config.index_code)
        forwards={month:current_price(symbol) for month,symbol in self.futures.items()}
        # 一致时点：只取本帧收盘价，不用旧帧价格或下一帧价格补缺。
        quotes={symbol:row for symbol,row in self.latest.items() if symbol in self.options and row["event_ns"]==stamp}
        chosen,audit,month=select(self.options,quotes,index,forwards,now,self.config)
        status="SELECTED" if len(chosen)==len(self.config.sides) else "PARTIAL" if chosen else "NO_SELECTION"
        record=dict(signal_ns=stamp,observed_ns=observed_ns,date=str(key[0]),slot=key[1],month=month,status=status,count=len(chosen))
        self.records.append(record)
        self.selections.extend({**record,**row} for row in chosen)
        self.audits.extend({**record,**row} for row in audit)
        if self.callback is not None: self.callback(record,chosen)

    def on_stop(self):
        # 选约报告可结束时flush，因为不产生任何交易委托。
        if self.pending_ns is not None: self._evaluate(self.pending_ns,self.pending_ns)
