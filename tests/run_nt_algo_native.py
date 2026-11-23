"""远程完整Bomber SDK的原生NT时钟/派生单冒烟测试；仅使用离线柜台。"""
import time


def main():
    # 必须先在真实SDK命名空间建立原生宿主，再安装测试夹具的框架类型替身。
    from bomber.framework.trader.execution.nt_algo import NativeNTHost
    from tests.test_nt_algo_ctp import NTAlgoCtpTests

    real_ns = time.time_ns
    host = NativeNTHost()
    case = NTAlgoCtpTests()
    case.setUp()
    try:
        account, service, transport, (one, _), runtime = case.make_account(
            host_factory=lambda: host, wait_seconds=0.1)
        case.submit(one, 2)
        first = transport.sent[-1]['OrderRef']
        transport.fill(first, 1)
        end = time.monotonic() + 5
        while len(transport.sent) < 2 and time.monotonic() < end:
            case.now = real_ns()
            service.prices.update(case.instrument, 3007, case.now)
            service.poll()
            time.sleep(0.005)
        assert len(transport.sent) == 2, '原生NT定时器未产生撤单及重报闭环'
        assert transport.canceled == [first]
        assert transport.sent[-1]['VolumeTotalOriginal'] == 1
        ref = transport.sent[-1]['OrderRef']
        intent = account.driver._orders[ref][1]
        assert intent.metadata['nt_algo_child_id'].endswith('-E2')
        transport.fill(ref)
        assert service.position_manager.position('one', 'main') == 2
        assert not runtime.snapshot_state()['tasks']
        account._validate_account()
        print('通过：原生NT ExecAlgorithm、LiveClock、spawn_limit及CTP账本离线闭环。未连接真实柜台。')
    finally:
        case.doCleanups()
        host.close()


if __name__ == '__main__':
    main()
