"""检查真实 Bomber/market 扩展的行情数值；不创建会话或调用交易端口。"""
from __future__ import annotations

import argparse
from array import array
from decimal import Decimal
import json
from pathlib import Path
import runpy
import sys


def _equal(actual, expected):
    if actual != expected:
        raise AssertionError(f'expected {expected!r}, got {actual!r}')


def collect_report(project_root=None):
    """供独立 Python 或宿主内嵌脚本调用；不卸载已加载模块。"""
    probe = runpy.run_path(str(Path(__file__).with_name('environment_probe.py')))
    environment = probe['collect_report'](project_root)
    report = {'schema_version': 1, 'scope': 'native_market_values',
              'environment': environment, 'checks': [], 'native_values_ready': False,
              'not_verified': ['完整原生 ABI', '宿主 GIL/回调线程',
                               'HostPort/快照边界', '真实委托及回报']}
    if not environment['windows_imports_ready']:
        return report

    previous_path = list(sys.path)
    try:
        if project_root is not None:
            sys.path.insert(0, str(Path(project_root).resolve()))
        from bomber.model.data import Data, TradeTick, QuoteTick
        from bomber.model.enums import AggressorSide
        from bomber.model.identifiers import InstrumentId
        from bomber.framework.market.basic import base, fast_factory

        instrument = InstrumentId.from_str('rb2701.SHFE')
        stamp = 1_792_547_964_603_975_000

        def record(name, check):
            result = {'name': name, 'passed': False}
            try:
                result['values'] = check()
                result['passed'] = True
            except Exception as error:
                result.update(error_type=type(error).__name__, error=str(error))
            report['checks'].append(result)

        def verify_tick(tick, price, size, trade_id, ts_event, ts_init,
                        price_precision, size_precision):
            _equal(isinstance(tick, TradeTick), True)
            _equal(tick.instrument_id, instrument)
            _equal(tick.price.as_decimal(), Decimal(price))
            _equal(tick.size.as_decimal(), Decimal(size))
            _equal(tick.price.precision, price_precision)
            _equal(tick.size.precision, size_precision)
            _equal(str(tick.trade_id), trade_id)
            _equal(tick.aggressor_side, AggressorSide.BUYER)
            _equal(tick.ts_event, ts_event)
            _equal(tick.ts_init, ts_init)
            return {'price': str(tick.price), 'size': str(tick.size),
                    'ts_event': tick.ts_event, 'ts_init': tick.ts_init}

        def trades():
            values = []
            for price, size, pp, sp in [('3500', '3', 0, 0),
                                       ('123.45', '2.75', 2, 2),
                                       ('0.125', '0.0005', 3, 4)]:
                tick = fast_factory.fast_make_trade_tick(
                    instrument, float(price), float(size), pp, sp,
                    'probe-trade', stamp, stamp + 123, 1)
                values.append(verify_tick(tick, price, size, 'probe-trade',
                                         stamp, stamp + 123, pp, sp))
            return values

        def quote():
            meta = base.InstrumentMeta(instrument, price_precision=2, size_precision=2)
            tick = base.make_quote_tick(instrument, '123.45', '123.55',
                                        '2.75', '4.25', stamp, stamp + 123, meta)
            _equal(isinstance(tick, QuoteTick), True)
            _equal(tick.instrument_id, instrument)
            values = {}
            for field, expected in [('bid_price', '123.45'), ('ask_price', '123.55'),
                                    ('bid_size', '2.75'), ('ask_size', '4.25')]:
                value = getattr(tick, field)
                _equal(value.as_decimal(), Decimal(expected))
                _equal(value.precision, 2)
                values[field] = str(value)
            _equal(tick.ts_event, stamp)
            _equal(tick.ts_init, stamp + 123)
            return values

        def batch():
            ticks = fast_factory.fast_make_trade_ticks_from_arrays(
                instrument, 2, 2, array('d', [123.45, 123.55]),
                array('d', [2.75, 4.25]), array('Q', [stamp, stamp + 1]),
                array('Q', [stamp + 123, stamp + 124]), ['batch-1', 'batch-2'], 1)
            _equal(len(ticks), 2)
            return [verify_tick(tick, price, size, f'batch-{i + 1}',
                                stamp + i, stamp + 123 + i, 2, 2)
                    for i, (tick, price, size) in enumerate(
                        zip(ticks, ['123.45', '123.55'], ['2.75', '4.25']))]

        def custom_bar():
            bar = base.make_bar(instrument, '123.45', '124.00', '123.00',
                                '123.55', '7', stamp, stamp + 123)
            custom = base.make_custom_bar(bar, {'probe_factor': 1.25})
            _equal(isinstance(custom, Data), True)
            _equal(custom.bar, bar)
            _equal(custom.instrument_id, instrument)
            _equal(custom.ts_event, stamp)
            _equal(custom.ts_init, stamp + 123)
            _equal(custom.get_factor('probe_factor'), 1.25)
            _equal(custom.has_factor('probe_factor'), True)
            _equal(custom.get_factor('missing', -1.0), -1.0)
            values = {}
            for field, expected in [('open', '123.45'), ('high', '124.00'),
                                    ('low', '123.00'), ('close', '123.55'), ('volume', '7')]:
                value = getattr(custom.bar, field)
                _equal(value.as_decimal(), Decimal(expected))
                values[field] = str(value)
            return values

        record('trade_tick_values', trades)
        record('quote_tick_values', quote)
        record('batch_trade_tick_values', batch)
        record('custom_bar_values', custom_bar)
        report['native_values_ready'] = all(c['passed'] for c in report['checks'])
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
    finally:
        sys.path[:] = previous_path
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    report = collect_report(args.project_root)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding='utf-8')
        print(str(output.resolve()))
        print('Native value check: ' + ('PASS' if report['native_values_ready'] else 'BLOCKED'))
    else:
        print(payload, end='')
    return 0 if report['native_values_ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
