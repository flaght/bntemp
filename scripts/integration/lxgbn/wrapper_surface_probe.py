"""在 Windows 检查 stgWrapper 实际导出面；不调用 Start/initialize/交易方法。

只导入指定目录的 stgWrapper 并读取类型及文档；不导入厂商策略示例或配置。
原生模块自己的导入初始化行为由库实现决定，请在独立测试进程运行。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import struct
import sys


METHODS = ('initialize', 'subInstrument', 'subBar', 'get_product',
           'sendOrder', 'cancelOrder', 'getOrder',
           'onOrderAck', 'onOrderFill', 'onOrderFinished',
           'onMarketData', 'onBar', 'onBatchBar', 'onBatchBars',
           'onIVData', 'onGreeksData')
FIELDS = {
    'Order': ('Id', 'GetInst', 'direction', 'price', 'volumePlanned',
              'volumeSubmitted', 'volumeTraded', 'volumeCancelled', 'offset',
              'hedgeFlag', 'status', 'timestampSubmitted', 'timestampAck',
              'timestampCancelled', 'strategyId', 'portfolioId',
              'accountId', 'attribute', 'errorMsg', 'isAlgoOrder'),
    'Trade': ('tradeId', 'orderId', 'price', 'volume', 'direction', 'offset',
              'strategyId', 'accountId', 'hedgeFlag', 'timestamp', 'GetInst'),
}
ENUMS = ('OrderType', 'OrderMsg_dir_t', 'OrderMsg_offset_t',
         'HedgeFlag_type_t', 'OrderMsg_attr_t', 'status_t', 'bar_time_unit_t')


def collect_report(lib_dir):
    directory = Path(lib_dir).resolve()
    report = {'schema_version': 1, 'scope': 'wrapper_surface_only',
              'python': platform.python_version(), 'platform': sys.platform,
              'bits': struct.calcsize('P') * 8, 'executable': sys.executable,
              'lib_dir': str(directory), 'methods': {}, 'fields': {}, 'enums': {},
              'required_names_present': False,
              'not_verified': ['函数调用/返回语义', '回调参数数量', '字段实际值/单位',
                               '身份与查询完整性', '线程/GIL', '真实交易']}
    previous = list(sys.path)
    handle = None
    try:
        if sys.platform != 'win32':
            raise RuntimeError('此导出库检查须在 Windows 的独立测试进程运行')
        if not directory.is_dir():
            raise ValueError('lib-dir 不存在')
        existing = sys.modules.get('stgWrapper')
        if existing is not None:
            raise RuntimeError('stgWrapper 已加载；请用新的独立 Python 进程检查指定产物')
        if hasattr(os, 'add_dll_directory'):
            handle = os.add_dll_directory(str(directory))
        sys.path.insert(0, str(directory))
        module = importlib.import_module('stgWrapper')
        origin = Path(module.__file__).resolve()
        if origin.parent != directory:
            raise RuntimeError(f'导入路径不符: {origin}')
        report['module_file'] = str(origin)
        report['sha256'] = hashlib.sha256(origin.read_bytes()).hexdigest()
        strategy = getattr(module, 'IPyStrategy', None)
        for name in METHODS:
            member = getattr(strategy, name, None)
            report['methods'][name] = {'present': member is not None,
                                      'doc': getattr(member, '__doc__', None)}
        for name, fields in FIELDS.items():
            cls = getattr(module, name, None)
            report['fields'][name] = {field: hasattr(cls, field) for field in fields}
        for name in ENUMS:
            matches = [raw for raw in dir(module) if raw.strip() == name]
            entries = []
            for raw in matches:
                enum = getattr(module, raw)
                names = getattr(enum, 'names', {})
                values = {}
                for key, value in names.items():
                    try:
                        values[str(key)] = int(value)
                    except (TypeError, ValueError):
                        values[str(key)] = str(value)
                entries.append({'python_name': raw, 'values': values})
            report['enums'][name] = entries
        required = ('sendOrder', 'cancelOrder', 'getOrder',
                    'onOrderAck', 'onOrderFill', 'onOrderFinished')
        report['required_names_present'] = all(report['methods'][n]['present'] for n in required)
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
    finally:
        sys.path[:] = previous
        if handle is not None:
            handle.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lib-dir', required=True)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    report = collect_report(args.lib_dir)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding='utf-8')
        print(str(output.resolve()))
        print('Required wrapper names: ' + ('PRESENT' if report['required_names_present'] else 'MISSING/BLOCKED'))
    else:
        print(payload, end='')
    return 0 if report['required_names_present'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
