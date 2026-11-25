"""z01账号直连入口；只提供独立启动配置，策略实现复用原08。"""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from demos.direct_ctp import main as direct_main


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not any(arg.split('=', 1)[0] == '--config-file' for arg in args):
        startup = Path(__file__).resolve().parent.parent / 'z01_shared_account/startup.yaml'
        args = ['--config-file', str(startup), '--config-id', 'option_z01', *args]
    return direct_main('option_vega', args)

if __name__ == '__main__':
    main()
