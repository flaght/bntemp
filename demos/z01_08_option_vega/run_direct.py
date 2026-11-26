"""z01账号直连入口；使用本目录的strategy.yaml，策略实现复用原08。"""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from demos.direct_ctp import main as direct_main


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not any(arg.split('=', 1)[0] in {'--config-file', '--config_file'} for arg in args):
        config = Path(__file__).resolve().parent / 'config/strategy.yaml'
        args = ['--config-file', str(config), '--config-id', 'direct_nt_algo', *args]
    return direct_main('option_vega', args)

if __name__ == '__main__':
    main()
