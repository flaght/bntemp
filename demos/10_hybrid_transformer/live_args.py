"""HybridTransformer Live 的命令行参数与输入校验。"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import math
from pathlib import Path

from bomber.framework.dataprep.live_references import live_factor_policy
from bomber.framework.dataprep.session_inputs import add_session_arguments, validate_session_arguments
from bomber.framework.dataprep.sources import DataSourcePurpose
from bomber.framework.market.sources import add_market_source_arguments, market_config_from_args
from bomber.framework.trader.execution.policy import (
    add_execution_policy_arguments, execution_policy_from_args,
)
from bomber.framework.trader.runtime.live.channels.ctp import (
    add_environment_arguments, validate_environment_arguments,
)

from .signal_config import load_product_signal


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='HybridTransformer Live：CTP/DolphinDB 行情，Recording/SimNow 执行。')
    add_market_source_arguments(parser)
    add_environment_arguments(parser)
    add_session_arguments(parser)
    add_execution_policy_arguments(parser)
    parser.set_defaults(account_reconciliation='off', strategy_position_source='fills')

    run = parser.add_argument_group('运行与输出')
    run.add_argument('--connect', action='store_true')
    run.add_argument('--mode', choices=('recording', 'simnow'), default='recording')
    run.add_argument('--product', required=True)
    run.add_argument('--seconds', type=float, default=600)
    run.add_argument('--query-timeout', type=float, default=15)
    run.add_argument('--report-dir', type=Path, default=Path(__file__).parent/'results'/'live')

    model = parser.add_argument_group('模型与信号')
    model.add_argument('--release-path', type=Path, required=True)
    model.add_argument('--directions-file', type=Path, required=True)
    model.add_argument('--signal-config', type=Path, required=True)
    model.add_argument('--device', choices=('auto', 'cpu', 'cuda'), default='cpu')
    model.add_argument('--history-bars', type=int, default=1500)
    model.add_argument('--min-raw-bars', type=int, default=300)
    model.add_argument('--model-bar-timestamp', choices=('start', 'end'), default='start')

    history = parser.add_argument_group('历史预热')
    history.add_argument('--history-minutes', type=int, default=0)
    history.add_argument('--history-config', type=Path)
    history.add_argument('--history-file', type=Path)

    reference = parser.add_argument_group('参考资料')
    reference.add_argument('--expected-trading-day', required=True)
    reference.add_argument('--expected-source-day', required=True)
    reference.add_argument('--reference-database')
    reference.add_argument('--reference-timeout', type=int, default=15)
    reference.add_argument('--reference-refresh-seconds', type=float, default=5)
    reference.add_argument('--factor-date-basis', choices=('source', 'trading'), default='source')
    reference.add_argument('--factor-availability', choices=('explicit', 'observed-on-read'), default='observed-on-read')

    strategy = parser.add_argument_group('持仓与风控')
    strategy.add_argument('--max-contracts', type=int, default=10)
    strategy.add_argument('--rebalance-minutes', type=int, default=1)
    strategy.add_argument('--max-notional', type=Decimal, default=Decimal('1000000'))
    strategy.add_argument('--limit-offset-ticks', type=int, default=1)
    strategy.add_argument('--max-session-orders', type=int, default=100)

    recovery = parser.add_argument_group('交易与恢复')
    recovery.add_argument('--state-file', type=Path)
    recovery.add_argument('--resume', action='store_true')
    recovery.add_argument('--enable-orders', action='store_true')
    recovery.add_argument('--confirm-simnow', action='store_true')

    args = parser.parse_args(argv)
    try:
        return _validate_args(args)
    except (ValueError, TypeError, OSError) as error:
        parser.error(str(error))


def _validate_args(args):
    market_config_from_args(args, domestic_contracts=True)
    validate_environment_arguments(args)
    if not args.connect or args.simnow_environment != 'realtime':
        raise ValueError('须 --connect；Hybrid首版仅使用realtime事件时间')
    for name in ('expected_trading_day', 'expected_source_day'):
        value = getattr(args, name)
        if datetime.strptime(value, '%Y%m%d').strftime('%Y%m%d') != value:
            raise ValueError('交易日/来源日须为YYYYMMDD')
    args.product = args.product.strip().upper()
    if not args.product.isalpha():
        raise ValueError('品种须为非空字母代码')
    if not args.release_path.is_dir() or not args.directions_file.is_file():
        raise ValueError('模型发布目录或方向文件不存在')

    load_product_signal(args.signal_config, args.product)

    if not 1 <= args.min_raw_bars <= args.history_bars <= 10000:
        raise ValueError('须满足 1 <= min-raw-bars <= history-bars <= 10000')
    if args.history_minutes and not args.min_raw_bars <= args.history_minutes <= args.history_bars:
        raise ValueError('历史预热根数须介于min-raw-bars和history-bars之间')
    if bool(args.history_config or args.history_file) != bool(args.history_minutes):
        raise ValueError('历史预热须同时声明history-minutes和history-config/history-file')
    if args.history_config and not args.history_config.is_file() or args.history_file and not args.history_file.is_file():
        raise ValueError('历史配置或文件不存在')
    if args.resume and (not args.history_minutes or not args.history_config):
        raise ValueError('恢复持仓须提供history-minutes及数据库history-config重新预热模型')
    if args.mode == 'simnow':
        if not (args.enable_orders and args.confirm_simnow and args.state_file):
            raise ValueError('SimNow须 --enable-orders --confirm-simnow --state-file')
        if args.resume != args.state_file.is_file():
            raise ValueError('新会话须新状态文件；恢复须原文件并加--resume')
    elif args.enable_orders or args.confirm_simnow or args.state_file or args.resume:
        raise ValueError('Recording不接受交易授权、交易状态文件或恢复参数')
    if not args.max_notional.is_finite() or args.max_notional <= 0 or not math.isfinite(args.seconds) or args.seconds <= 0:
        raise ValueError('名义上限和运行时间须为正')
    if args.max_contracts < 1 or args.rebalance_minutes < 1 or 60 % args.rebalance_minutes:
        raise ValueError('最大手数须为正整数；调仓间隔须为 60 的正约数')
    if not 0 <= args.reference_refresh_seconds <= 30 or not 1 <= args.reference_timeout <= 120:
        raise ValueError('参考刷新须为0至30秒、读取超时须为1至120秒')
    if not 1 <= args.max_session_orders <= 10000 or args.limit_offset_ticks < 0:
        raise ValueError('报单上限或限价偏移无效')
    if not math.isfinite(args.query_timeout) or not 0 < args.query_timeout <= 120:
        raise ValueError('CTP查询超时须在0至120秒')
    validate_session_arguments(args, purpose=DataSourcePurpose.LIVE_REFERENCE)
    execution_policy_from_args(args)
    args.factor_availability = live_factor_policy(args.factor_date_basis, args.factor_availability)
    return args
