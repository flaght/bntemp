"""在线运行报告；策略只提供自己的指标，不参与账户或生命周期处理。"""

from pathlib import Path
from datetime import datetime
import csv
import os
import json
import time


class LiveRunReport:
    def __init__(self, report_dir, *, prefix="live", metadata=None, describe=None, clock_ns=None):
        self.root = Path(report_dir)
        self.prefix = prefix
        self.metadata = dict(metadata or {})
        self.describe = describe or (lambda session: {})
        self.clock_ns = clock_ns or (lambda: time.time_ns())
        self.path = None

    def begin(self):
        path = self.root / f"{self.prefix}-{self.clock_ns()}"
        path.mkdir(parents=True, exist_ok=False)
        self.path = path

    def write(self, *, status, fields, session, events):
        result = {**self.metadata, **self.describe(session), **fields, "status": status}
        (self.path / "summary.json").write_text(json.dumps(result, ensure_ascii=False,
            indent=2, default=str) + "\n", encoding="utf-8")
        if session is not None:
            (self.path / "events.txt").write_text("\n".join(map(str, events)) + "\n", encoding="utf-8")
        print(f"停机结果: {result}\n记录目录: {self.path}", flush=True)
        return result


class TradingDayLiveReport(LiveRunReport):
    """同一交易日追加明细；summary是最新会话，sessions保留各次会话摘要。

    trading_day必须由会话日历/交易通道提供，不能取本机自然日期。
    一个报告对象对应一次会话；新对象沿用同日目录，游标只排除本次重复采集。
    同一策略的进程互斥仍由运行时的state-file锁负责。
    """
    def __init__(self, report_dir, *, trading_day, prefix="live", metadata=None,
                 describe=None, clock_ns=None):
        super().__init__(report_dir, prefix=prefix, metadata=metadata,
                         describe=describe, clock_ns=clock_ns)
        self.trading_day = str(trading_day)
        if datetime.strptime(self.trading_day, "%Y%m%d").strftime("%Y%m%d") != self.trading_day:
            raise ValueError("报告交易日须为YYYYMMDD")
        self.run_id = None
        self._cursors = {}

    def begin(self):
        self.run_id = str(self.clock_ns())
        self.path = self.root / f"{self.prefix}-{self.trading_day}"
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "events.txt").touch(exist_ok=True)
        print(f"交易日报告目录: {self.path}；本次会话编号: {self.run_id}", flush=True)

    def _new_rows(self, name, rows):
        cursor = self._cursors.get(name, 0)
        if len(rows) < cursor:
            raise RuntimeError(f"报告记录在会话中被清空: {name}")
        return cursor, rows[cursor:]

    @staticmethod
    def _sync(stream):
        stream.flush()
        os.fsync(stream.fileno())

    def record_events(self, events):
        """追加新回报；重复poll不会再次写入同一批内存记录。"""
        cursor, fresh = self._new_rows("events.txt", events)
        if fresh:
            with (self.path / "events.txt").open("a", encoding="utf-8") as stream:
                stream.write("\n".join(map(str, fresh)) + "\n")
                self._sync(stream)
            self._cursors["events.txt"] = cursor + len(fresh)

    def record_csv(self, name, rows):
        """同日重启继续追加；保留一份表头，并携带交易日与本次会话编号。"""
        cursor, fresh = self._new_rows(name, rows)
        if not fresh:
            return
        records = [dict(row, trading_day=self.trading_day, run_id=self.run_id) for row in fresh]
        path = self.path / name
        columns = list(records[0])
        header = None
        if path.exists() and path.stat().st_size:
            with path.open(encoding="utf-8", newline="") as stream:
                header = next(csv.reader(stream))
            if set(header) != set(columns):
                raise ValueError(f"交易日报告列不一致，保留原文件: {path}")
            columns = header
        with path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            if header is None:
                writer.writeheader()
            writer.writerows(records)
            self._sync(stream)
        self._cursors[name] = cursor + len(fresh)

    def record_jsonl(self, name, rows):
        """追加异构业务记录，保留交易日/会话号及逐文件游标。"""
        cursor, fresh = self._new_rows(name, rows)
        if fresh:
            with (self.path / name).open("a", encoding="utf-8") as stream:
                for row in fresh:
                    stream.write(json.dumps({**row, "trading_day": self.trading_day,
                        "run_id": self.run_id}, ensure_ascii=False, default=str, allow_nan=False) + "\n")
                self._sync(stream)
            self._cursors[name] = cursor + len(fresh)

    def write(self, *, status, fields, session, events):
        # 收尾再采集一次，包括shutdown期间产生的成交/撤单回报。
        self.record_events(events)
        result = {**self.metadata, **self.describe(session), **fields, "status": status,
            "trading_day": self.trading_day, "run_id": self.run_id,
            "summary_scope": "latest_session"}
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        with (self.path / "sessions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
            self._sync(stream)
        (self.path / "summary.json").write_text(json.dumps(result, ensure_ascii=False,
            indent=2, default=str) + "\n", encoding="utf-8")
        print(f"停机结果: {result}\n记录目录: {self.path}", flush=True)
        return result
