"""08独立策略进程：复用公共IPC worker，不创建CTP交易连接。"""
from pathlib import Path
import sys


def main(argv=None):
    import argparse
    from scripts.integration.shared_account.config import read_config
    from scripts.integration.shared_account.worker import main as run
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--strategy-id', default='option-vega-live')
    args, rest = parser.parse_known_args(argv)
    config = read_config(args.config)
    rows = [row for row in config['strategies'] if row['strategy_id'] == args.strategy_id]
    if len(rows) != 1 or rows[0]['kind'] != 'option_vega':
        parser.error('08 worker须使用option_vega策略声明')
    return run(['--config', str(args.config), '--strategy-id', args.strategy_id, *rest])


if __name__ == '__main__':
    main(sys.argv[1:])
