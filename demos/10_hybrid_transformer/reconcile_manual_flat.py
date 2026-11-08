"""单策略手工平仓后的本地基线对齐；不发送订单、不修改手数配置。"""
import argparse
from pathlib import Path

from bomber.framework.trader.runtime.live.channels.ctp_maintenance import reconcile_flat_checkpoint
from .flat_state import aligned_payload, managed_instruments, prepare_native_payload


def reconcile(*, state_file, transport):
    return reconcile_flat_checkpoint(state_file=state_file, transport=transport,
        prepare=lambda payload, day: prepare_native_payload(payload, trading_day=day))


def main(argv=None):
    from dotenv import load_dotenv
    from bomber.framework.trader.execution.ctp import CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--confirm-manual-flat', action='store_true')
    args = parser.parse_args(argv)
    if not args.confirm_manual_flat:
        parser.error('须明确confirm-manual-flat')
    transport = build_simnow_transport(CtpTdApiTransport, client_id='hybrid-transformer-live',
        flow_path='/tmp/bomber-hybrid-manual-flat', timeout_seconds=30)
    return reconcile(state_file=args.state_file, transport=transport)


if __name__ == '__main__':
    main()
