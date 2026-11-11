"""进程内CTP生命周期；账户上的其他策略可以同时正常交易。"""
from contextlib import contextmanager
from decimal import Decimal
import fcntl
import hashlib
import time

from .ctp_shared import CtpSharedAccountLifecycle
from .ctp_owned import external_partition
from .ctp import account_lock
from bomber.framework.trader.execution.ctp.independent import StrategyBuckets, YesterdayFirstPlanner


class IndependentCtpLifecycle(CtpSharedAccountLifecycle):
    @staticmethod
    def acquire(resources, transport, *, environment, strategy_ids):
        # 与旧入口的排他锁互斥；新入口之间共享连接权限，按策略ID防重复实例。
        for namespace in ('bomber-ctp', 'bomber-main-ema'):
            resources.enter_context(shared_account_guard(transport.account_id, namespace))
        for sid in sorted(strategy_ids):
            resources.enter_context(account_lock(transport.account_id+':'+sid, namespace='bomber-direct-strategy'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.service.event_journal is None:
            from bomber.framework.trader.execution.ipc import ExecutionEventJournal
            self.service.event_journal = ExecutionEventJournal(self.service._ports)
        self.driver.book.day = self.expected_day
        self.driver.buckets = StrategyBuckets(self.expected_day)
        self.client.planner = YesterdayFirstPlanner(self.client.planner.planner,
            self.execution.positions, self.driver.buckets)
        self.manager.state_components.update(ctp_order_owners=self.driver.book, strategy_buckets=self.driver.buckets)
        self.import_transform = None
        self.initial_payload = None
        self._importing = False

    def _validate_external(self, gross, buckets):
        self.external_gross, self.external_buckets = external_partition(
            self.ledger.state(), gross, buckets, self.generic_close_venues)

    def _validate_account_snapshot(self, gross, buckets, active):
        self.service.validate_owned_state()
        self._validate_external(gross, buckets)
        attribution = self.execution.positions.attribution.state()['owned']
        for instrument in self.service.instrument_limits:
            state = self.ledger.snapshot(instrument)
            owned = [self.driver.buckets.quantities(sid, instrument) for sid in self.service._ports]
            if tuple(sum((q[i] for q in owned), Decimal(0)) for i in range(4)) != (
                    state.long_today, state.long_yesterday, state.short_today, state.short_yesterday):
                raise RuntimeError('策略今昨归属与CTP本地成交账本不一致')
            if self.execution.positions.account_position(self.service.client_id, instrument) != state.net_position:
                raise RuntimeError('归属净仓与CTP本地成交账本不一致')
            for sid in self.service._ports:
                q = sum((Decimal(row['quantity']) for row in attribution
                    if row['strategy_id'] == sid and row['instrument_id'] == str(instrument)), Decimal(0))
                a,b,c,d = self.driver.buckets.quantities(sid,instrument)
                if (a+b,c+d) != (max(q,Decimal(0)),max(-q,Decimal(0))):
                    raise RuntimeError('本策略今昨桶与本策略真实合约归属不一致')
        known = {row.client_order_id for row in self.client.order_state_machine.states() if not row.status.is_terminal}
        if {row.client_order_id for row in active} != known:
            raise RuntimeError('柜台活动订单与本进程持久化归属不一致')
        if set(self.driver.buckets.frozen) != known:
            raise RuntimeError('活动订单今昨冻结与订单状态不一致')
        for row in active:
            local = self.client.order_state_machine.state(row.client_order_id)
            if (row.order_quantity, row.cumulative_filled, row.remaining_quantity) != (
                    local.order_quantity, local.filled_quantity, local.remaining_quantity):
                raise RuntimeError('柜台活动单成交数量与本地不一致，须查询真实成交补记')
            frozen = self.driver.buckets.frozen[row.client_order_id]
            ref = row.client_order_id.rsplit('-',1)[-1]
            record = self.driver.book.records[ref]
            intent = self.driver._orders[ref][1]
            if (record['terminal'] or frozen['strategy_id'] != record['strategy_id']
                    or frozen['strategy_id'] != intent.strategy_id or frozen['instrument_id'] != str(intent.instrument_id)
                    or sum(map(Decimal,frozen['buckets'])) != (Decimal(0) if intent.position_effect.value == 'OPEN' else local.remaining_quantity)):
                raise RuntimeError('委托、策略归属和今昨冻结不一致')

    def start(self, *, resume, payload_transform=None, commit_restored=True):
        if self._started:
            return
        if resume:
            transform = payload_transform or self.import_transform or (lambda payload: payload)
            repository = self.manager.repository
            if self.initial_payload is not None and repository.load() is None:
                from types import SimpleNamespace
                from bomber.framework.trader.persistence import PersistedState
                self.manager.repository = SimpleNamespace(load=lambda: PersistedState(0, 0, self.initial_payload))
                self._importing = True
            try:
                if self.manager.restore(payload_transform=transform) is None:
                    raise RuntimeError('进程策略账本不存在')
            finally:
                self.manager.repository = repository
            self._restored = True
            self.service.validate_owned_state()
        elif self.manager.repository.load() is not None:
            raise RuntimeError('新策略不能覆盖已有账本')
        self.driver.terminal_query_recovery = True
        # 先补齐真实漏成交，再让受控客户端记录known集并完成标准核验。
        self.client.backend.start()
        self.driver.reconcile_active_orders()
        self.client.start()
        self._started = True
        if self.driver.trading_day != self.expected_day or self.ledger.state().trading_day != self.expected_day:
            raise RuntimeError('直连CTP交易日不一致')
        self._validate_account()
        self.service._pending = self.service._failed = False
        self._ready = True
        self._next_check = time.monotonic()+self.reconcile_seconds
        if resume and commit_restored:
            self._commit()

    def _commit(self):
        if self._importing:
            self.manager._restore_pending = False
            try:
                self.manager.save()
            except BaseException:
                self.manager._restore_pending = True
                raise
            self._importing = False
        else:
            self.manager.commit_restore()

    def authorize(self):
        if not self._ready:
            raise RuntimeError('CTP恢复尚未通过核验')
        self._validate_account()
        self.manager.validate_components()
        self._commit()
        self.client.arm_demo(self.client.DEMO_CONFIRMATION)

    def poll(self):
        if (not self._ready or not self.driver.is_simnow_authenticated
                or not self.client.is_reconciled or self.client.report_errors
                or self.execution.positions.is_recovery_required(self.service.client_id)):
            raise RuntimeError('直连CTP断开或执行状态不明，策略闭闸')
        if time.monotonic() >= self._next_check:
            self.client.refresh_account_state()
            self._validate_account()
            self._next_check = time.monotonic()+self.reconcile_seconds


@contextmanager
def shared_account_guard(account, namespace):
    key = hashlib.sha256(account.encode()).hexdigest()[:16]
    with open(f'/tmp/{namespace}-{key}.lock', 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('旧账户服务或旧独占CTP入口仍在运行，请先正常停止') from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
