"""Windows CPython 3.12 导入检查；可独立运行或由 LXGBN 内嵌 Python 调用。

只检查环境与模块导入，不构造会话、不调用宿主交易端口。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import platform
import struct
import sys
import sysconfig


_CHECKS = (
    ('sdk_contract', ('bomber.framework.lxgbn',)),
    ('bomber_engine', ('bomber.model.data', 'bomber.model.identifiers', 'bomber.model.objects')),
    ('market_basic', ('bomber.framework.market.basic.custom_bar',
                      'bomber.framework.market.basic.fast_factory', 'bomber.framework.market.basic.base')),
    ('session_factory', ('bomber.framework.trader.runtime.lxgbn',)),
)


def collect_report(project_root=None):
    """返回普通字典；宿主直接调用此函数，不执行 argparse 或 sys.exit。"""
    root = None if project_root is None else Path(project_root).resolve()
    report = {
        'schema_version': 1,
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'imports_only',
        'runtime': {
            'python_version': platform.python_version(),
            'implementation': platform.python_implementation(),
            'platform': sys.platform,
            'machine': platform.machine(),
            'interpreter_bits': struct.calcsize('P') * 8,
            'executable': sys.executable,
            'prefix': sys.prefix,
            'soabi': sysconfig.get_config_var('SOABI'),
            'extension_suffix': sysconfig.get_config_var('EXT_SUFFIX'),
            'debug_build': bool(sysconfig.get_config_var('Py_DEBUG')),
        },
        'project_root': None if root is None else str(root),
        'target_matches': (sys.platform == 'win32' and sys.version_info[:2] == (3, 12)
                           and platform.python_implementation() == 'CPython'),
        'checks': [],
        'not_verified': ['native ABI/value correctness', 'embedded GIL and callback threads',
                         'host trading port and snapshot boundary', 'real orders and callbacks'],
    }
    previous_path = list(sys.path)
    try:
        if root is not None:
            if not (root / 'bomber' / 'framework' / 'lxgbn.py').is_file():
                raise ValueError('project_root 应包含 bomber/framework/lxgbn.py')
            sys.path.insert(0, str(root))
        for name, modules in _CHECKS:
            check = {'name': name, 'passed': False, 'modules': {}}
            try:
                for module_name in modules:
                    module = importlib.import_module(module_name)
                    origin = getattr(module, '__file__', None)
                    check['modules'][module_name] = origin
                    if root is not None and module_name.startswith('bomber.framework.'):
                        if origin is None or not Path(origin).resolve().is_relative_to(root / 'bomber' / 'framework'):
                            raise RuntimeError('已加载其他目录的 framework；请核对模块路径，探针不会卸载或替换宿主已加载模块')
                if name in {'sdk_contract', 'session_factory'}:
                    if not callable(getattr(module, 'create_session', None)):
                        raise RuntimeError('未找到本期 create_session 入口')
                check['passed'] = True
            except Exception as error:
                check['error_type'] = type(error).__name__
                check['error'] = str(error)
            report['checks'].append(check)
    except Exception as error:
        report['configuration_error'] = str(error)
    finally:
        # 不长期修改宿主搜索路径，也不尝试卸载已导入的 Python/原生模块。
        sys.path[:] = previous_path
    report['windows_imports_ready'] = (report['target_matches'] and not report.get('configuration_error')
        and len(report['checks']) == len(_CHECKS) and all(c['passed'] for c in report['checks']))
    report['next_steps'] = _next_steps(report)
    return report


def _next_steps(report):
    steps = []
    if report.get('configuration_error'):
        return ['修正 project_root 后重新运行。']
    if not report['target_matches']:
        steps.append('在 Windows 的 CPython 3.12，尤其 LXGBN 实际内嵌解释器中重新检查；当前报告不能替代宿主结果。')
    passed = {check['name']: check['passed'] for check in report['checks']}
    if not passed.get('sdk_contract'):
        steps.append('核对本期 framework 源码的部署目录及报告中的实际导入路径。')
    if not passed.get('bomber_engine'):
        steps.append('提供匹配宿主位数/CPython 3.12 的 Windows Bomber 内核；已有兼容产物可复用，没有才需要构建内核。')
    if not passed.get('market_basic'):
        steps.append('先确保 Bomber 内核可导入，再准备兼容的 market/basic .pyd；没有产物时使用匹配的 Python/内核及其 Cython 接口构建。')
    if not passed.get('session_factory'):
        steps.append('按 session_factory 的具体导入错误补齐依赖；契约模块通过不代表完整会话可加载。')
    if report['windows_imports_ready']:
        steps.append('导入检查通过；下一步进行原生模型数值测试和宿主只读绑定联调，暂不据此授权真实交易。')
    return steps


def write_report(report, output):
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return str(path.resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', help='含 bomber/framework 的项目根目录；省略时检查当前安装版本')
    parser.add_argument('--output', help='保存 JSON 报告的路径；省略时输出到标准输出')
    args = parser.parse_args(argv)
    report = collect_report(args.project_root)
    if args.output:
        print(write_report(report, args.output))
        print('Windows import check: ' + ('PASS' if report['windows_imports_ready'] else 'BLOCKED'))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['windows_imports_ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
