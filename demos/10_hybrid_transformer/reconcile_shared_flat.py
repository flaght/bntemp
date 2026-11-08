"""共享Hybrid空仓维护；旧命令默认归档旧未决平仓单，可显式选择普通对齐。"""
import argparse
from pathlib import Path

from bomber.framework.trader.runtime.live.channels.ctp_maintenance import reconcile_shared_flat
from .flat_state import align_flat_strategy, align_shared_signal


def reconcile(config, *, strategy_id, transport, archive_unknown_close=True):
    if not any(r['strategy_id'] == strategy_id and r['kind'] == 'hybrid_transformer' for r in config['strategies']):
        raise ValueError('须指定Hybrid策略')
    return reconcile_shared_flat(config, strategy_id=strategy_id, transport=transport,
        align_signal=align_shared_signal, archive_unknown_close=archive_unknown_close)


def main(argv=None):
    from dotenv import load_dotenv
    from scripts.integration.shared_account.config import read_config
    from bomber.framework.trader.execution.ctp import CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--strategy-id', required=True)
    parser.add_argument('--confirm-broker-flat', action='store_true')
    parser.add_argument('--manual-flat', action='store_true', help='普通对齐：要求所有旧委托已终结')
    args = parser.parse_args(argv)
    if not args.confirm_broker_flat:
        parser.error('须明确confirm-broker-flat')
    config = read_config(args.config)
    transport = build_simnow_transport(CtpTdApiTransport, client_id=config['account']['client_id'],
        flow_path='/tmp/bomber-shared-flat', timeout_seconds=30)
    return reconcile(config, strategy_id=args.strategy_id, transport=transport,
        archive_unknown_close=not args.manual_flat)


if __name__ == '__main__':
    main()
