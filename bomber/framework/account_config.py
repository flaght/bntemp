"""纯账户启动配置；读取配置不加载交易SDK或策略代码。"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
import math
from pathlib import Path
import re

FIELDS = frozenset(('version', 'trading_day', 'environment', 'execution_mode', 'position_scope',
    'existing_position_policy', 'seconds', 'account_state_file', 'report_dir', 'account_env_file', 'account'))
ACCOUNT_FIELDS = frozenset(('client_id', 'max_gross_quantity', 'max_gross_notional',
    'max_session_orders', 'limit_offset_ticks'))


def read_account_config(path):
    path = Path(path).expanduser().resolve()
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError(f'账户JSON存在重复字段: {key}')
            result[key] = value
        return result
    raw = json.loads(path.read_text(),object_pairs_hook=unique)
    if not isinstance(raw, dict) or set(raw) != FIELDS or type(raw.get('version')) is not int or raw['version'] != 1:
        raise ValueError('账户JSON仅接受账户字段；策略声明和registration须留在策略或迁移入口')
    config = deepcopy(raw)
    day = config['trading_day']
    if not isinstance(day, str) or datetime.strptime(day, '%Y%m%d').strftime('%Y%m%d') != day:
        raise ValueError('trading_day须为YYYYMMDD')
    if (config['environment'], config['execution_mode'], config['position_scope'],
            config['existing_position_policy']) != ('realtime', 'independent', 'strategy_instruments', 'preserve_external'):
        raise ValueError('独立账户服务须为realtime、independent、strategy_instruments及preserve_external')
    seconds = config['seconds']
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError('seconds须为(0,86400]内有限秒数')
    for key in ('account_state_file', 'report_dir', 'account_env_file'):
        value = config[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{key}须为非空路径')
        config[key] = str((path.parent/Path(value).expanduser()).resolve())
    account = config['account']
    if not isinstance(account, dict) or set(account) != ACCOUNT_FIELDS:
        raise ValueError('account须完整声明执行身份、预算及报单额度')
    if not isinstance(account['client_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', account['client_id']):
        raise ValueError('账户client_id无效')
    budgets = [account[key] for key in ('max_gross_quantity', 'max_gross_notional')]
    if any(value is None for value in budgets) and not all(value is None for value in budgets):
        raise ValueError('联合数量与名义预算须同时为null或有限正数')
    for value in budgets:
        if value is not None and (not Decimal(str(value)).is_finite() or Decimal(str(value)) <= 0):
            raise ValueError('账户联合预算须为有限正数')
    for key, minimum in [('max_session_orders',1), ('limit_offset_ticks',0)]:
        if type(account[key]) is not int or account[key] < minimum:
            raise ValueError('账户报单额度/跳价偏移无效')
    return config


def account_identity(config, ids, day=None):
    return dict(schema='account-execution-v1', trading_day=day or config['trading_day'],
        environment=config['environment'], account=deepcopy(config['account']),
        execution_mode=config['execution_mode'], existing_position_policy=config['existing_position_policy'],
        registered_strategies=sorted(ids))

