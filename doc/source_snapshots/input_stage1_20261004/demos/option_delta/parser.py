"""通过既有FileReplayFeed发出收盘价及分钟流动性字段的CustomBar。"""
from math import isfinite
from market.basic.base import DataType, make_bar, make_custom_bar
from market.replay.parsers.base import ParsedEvent


class FrameReader:
    suffixes=frozenset({".feather"})
    def __init__(self,frame): self.frame=frame
    def read(self,path):
        for number,row in enumerate(self.frame.to_dict("records"),1): yield number,row


class SnapshotParser:
    def __init__(self,instrument_id): self.instrument_id=instrument_id
    def reset(self): pass
    def parse(self,row,context):
        ns=int(row["datetime"].value)
        factors={name:row[name] for name in ("close","open_interest","volume","bar_ns") if name in row}
        # CustomBar仅为研究选约载体，不送撮合；无效收盘价由选约核记录拒绝原因。
        carrier=float(row["close"])
        if not isfinite(carrier) or carrier<=0: carrier=1.0
        bar=make_bar(self.instrument_id,carrier,carrier,carrier,carrier,0,ns,ns,
                     meta=context.require_meta(self.instrument_id),bar_type="1-MINUTE")
        yield ParsedEvent(DataType.CUSTOM_BAR,self.instrument_id,make_custom_bar(bar,factors),"1-MINUTE")
