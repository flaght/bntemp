"""CTP完整期货分钟的追加式JSONL存档；持仓检查点与行情文件分别管理。"""
from collections import deque
from decimal import Decimal
from pathlib import Path
from threading import RLock
import json
import os
import re

from .base import HistoryBar, HistoryError

RAW_FIELDS = ('open', 'high', 'low', 'close', 'volume', 'value', 'open_interest')


class FuturesMinuteArchive:
    """按真实合约单写入；每根完成分钟fsync后才允许向策略发布。

    采用框架HistoryBar字段，FileHistoryProvider也能读取这些JSONL行。
    附带来源和合约乘数，防止重启时混入其他口径。原始时间戳不平移、不填缺口。
    """
    def __init__(self, path, *, instrument_id, contract_multiplier, max_bars=1500):
        self.path = Path(path)
        self.instrument_id = str(instrument_id)
        self.multiplier = Decimal(str(contract_multiplier))
        if (self.path.suffix.lower() != '.jsonl'
                or re.fullmatch(r'[A-Za-z0-9_.-]+', self.instrument_id) is None
                or not self.multiplier.is_finite() or self.multiplier <= 0
                or type(max_bars) is not int or max_bars < 1):
            raise HistoryError('分钟存档须指定JSONL文件、真实合约、正乘数和正窗口根数')
        self._recent = deque(maxlen=max_bars)
        self._last = None
        self._stream = None
        self._lock = RLock()
        self.recorded_bars = 0

    def _validate(self, bar):
        if (not isinstance(bar, HistoryBar) or bar.instrument_id != self.instrument_id
                or any(getattr(bar, key) is None for key in RAW_FIELDS)
                or bar.adjusted_close is not None or bar.cumulative_factor is not None
                or bar.volume <= 0 or bar.volume != bar.volume.to_integral_value()):
            raise HistoryError('分钟存档只接受同一真实合约的完整、未复权、正整数成交量行情')
        if self._last is not None:
            if bar.ts_event < self._last.ts_event:
                raise HistoryError('分钟存档时间倒退，不能改写已保存历史')
            if bar.ts_event == self._last.ts_event:
                if bar != self._last:
                    raise HistoryError('分钟存档同一分钟内容冲突')
                return False
        return True

    def _remember(self, bar):
        self._last = bar
        self._recent.append(bar)
        self.recorded_bars += 1

    def _decode(self, row):
        if not isinstance(row, dict):
            raise HistoryError('分钟存档每行须为JSON对象')
        if (row.get('schema_version') != 1 or row.get('source') != 'ctp'
                or Decimal(str(row.get('contract_multiplier'))) != self.multiplier):
            raise HistoryError('分钟存档格式、来源或合约乘数不一致')
        return HistoryBar(instrument_id=row['instrument_id'], ts_event=row['ts_event'],
            **{key: row[key] for key in RAW_FIELDS})

    def open(self):
        import fcntl
        with self._lock:
            if self._stream is not None:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open('a+', encoding='utf-8')
            try:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise HistoryError(f'分钟存档已有写入进程: {self.path}') from error
                self._recent.clear()
                self._last, self.recorded_bars = None, 0
                stream.seek(0)
                for line_number, line in enumerate(stream, 1):
                    try:
                        if not line.endswith('\n'):
                            raise HistoryError('尾行未完整写入')
                        bar = self._decode(json.loads(line))
                        if self._validate(bar):
                            self._remember(bar)
                    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
                        raise HistoryError(f'分钟存档读取失败: {self.path}:{line_number}: {error}') from error
                self._stream = stream
            except Exception:
                stream.close()
                raise
            return self

    def append(self, bar):
        with self._lock:
            if self._stream is None:
                raise HistoryError('分钟存档尚未打开')
            if not self._validate(bar):
                return False
            row = dict(schema_version=1, source='ctp', contract_multiplier=str(self.multiplier),
                instrument_id=bar.instrument_id, ts_event=bar.ts_event,
                **{key: str(getattr(bar, key)) for key in RAW_FIELDS})
            line = json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
            if self._stream.write(line) != len(line):
                raise HistoryError('分钟存档未完整写入')
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._remember(bar)
            return True

    def recent(self, *, before_ns, limit):
        with self._lock:
            if self._stream is None:
                raise HistoryError('分钟存档尚未打开')
            if type(before_ns) is not int or type(limit) is not int or not 1 <= limit <= self._recent.maxlen:
                raise HistoryError('分钟存档读取边界或窗口根数无效')
            return tuple(bar for bar in self._recent if bar.ts_event < before_ns)[-limit:]

    def close(self):
        with self._lock:
            if self._stream is not None:
                self._stream.close()  # 释放文件描述符时也释放单写锁。
                self._stream = None
