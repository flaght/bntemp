"""单策略进程；复用demo算法和Runner，通过本机账户服务执行。"""

import argparse
from pathlib import Path
from datetime import datetime
import time
import logging

from .config import read_config, load_environment


def session_reference_is_current(session):
    """沿用共享宿主的固定参考检查，兼容没有专属检查方法的Runner。"""
    current = getattr(session.runner, "reference_is_current", None)
    return current() if callable(current) else session.references.spec == session.runner.fixed_spec


def capture_session_failure(session):
    """停机清空Feed之前保存原健康结果及本策略账户连接状态。"""
    from bomber.framework.trader.runtime.live.health import readiness_failure_context
    context = readiness_failure_context(session.runner)
    port = session.client
    context["execution_port"] = {"started": port._started, "stopping": port._stopping,
        "reconciled": port.is_reconciled, "account_reconciled": port.state.get("reconciled"),
        "observation_age_seconds": time.monotonic() - port._observed}
    session.runner.failure_readiness = context
    return context


def require_session_health(session):
    runner = session.runner
    if runner.failure:
        raise RuntimeError(runner.failure)
    reason = None
    if not runner.session_ready():
        reason = "策略行情或账户未就绪"
    elif not session_reference_is_current(session):
        reason = "策略参考合约或条款变化"
    if reason:
        context = capture_session_failure(session)
        raise RuntimeError(f"{reason}: {context}")


def poll_strategy_session(session, save):
    # 原参考缓存只在发布版本时持提交锁；数据库查询允许行情分派继续推进。
    with session.runner._submit_lock:
        if session.runner.failure:
            raise RuntimeError(session.runner.failure)
    session.references.refresh()
    with session.runner._submit_lock:
        if session.runner.failure:
            raise RuntimeError(session.runner.failure)
        session.client.drain_events()
        poll = getattr(session.runner, "poll_live_inputs", None)
        if callable(poll):
            poll()
        require_session_health(session)
        save()


def _main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument('--log-level', choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'), default='INFO')
    parser.add_argument('--env-file', type=Path, help='本策略env文件；不补读根目录.env')
    parser.add_argument("--activate-at", type=datetime.fromisoformat, help=argparse.SUPPRESS)
    parser.add_argument("--stop-at", type=datetime.fromisoformat,
        help="本策略窗口终点（含时区）；正常保存、撤单并保留归属仓位")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    if args.activate_at is not None and args.activate_at.tzinfo is None:
        parser.error("开盘边界须携带时区")
    if args.stop_at is not None and (args.stop_at.tzinfo is None or
            args.activate_at is None or args.stop_at <= args.activate_at):
        parser.error("stop-at须晚于activate-at且携带时区")
    config = read_config(args.config)
    rows = [r for r in config["strategies"] if r["strategy_id"] == args.strategy_id]
    if len(rows) != 1 or config.get("execution_mode") != "independent":
        parser.error("须声明独立模式及配置中的单策略ID")
    row = rows[0]
    if args.connect and not (args.enable_orders and args.confirm_simnow):
        parser.error("策略连接须显式双报单授权")
    if not args.connect:
        if args.enable_orders or args.confirm_simnow or args.resume:
            parser.error("离线检查不接受授权或恢复")
        print(f"策略进程配置通过：{args.strategy_id}；未连接")
        return
    from bomber.framework.trader.runtime.live.continuous import StopSignal
    with StopSignal() as stop_event:
        return _run(args, config, stop_event)


def _run(args, config, stop_event):
    from .native import make_runtime
    load_environment(getattr(args, 'env_file', None) or config.get('strategy_env_files', {}).get(args.strategy_id))
    row = next(row for row in config['strategies'] if row['strategy_id'] == args.strategy_id)
    runtime = make_runtime(config, row, args.credentials, resume=args.resume,
        activate_at=getattr(args, 'activate_at', None), stop_at=getattr(args, 'stop_at', None))
    runtime.poll_session = poll_strategy_session
    return runtime.run(stop_event)


def main(argv=None):
    return _main(argv)


if __name__ == "__main__":
    main()
