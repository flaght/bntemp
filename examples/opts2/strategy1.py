"""
OptionVega - 卖出认购期权+期货Delta对冲策略

策略逻辑：
  1. 14:00 卖出近月ITM认购期权（基于Delta/Gamma/Vega选合约和计算仓位）
  2. 10:00/11:00/13:30/14:55 期货Delta对冲
  3. 时间价值≤0时平仓期权
  4. DDB获取IV、futSettlePrice、eodUnderlyerClosePrice、Shibor
  5. 中证1000指数(SH000852)用于ITM筛选和forward_price计算

数据来源：
  BarData: 期权/期货/指数 closePrice（IM期货M1, MO期权M5, 中证1000指数M5）
  DDB(opt_config): IV_tmins1, settlePrice_tmins1, underlyerClosePrice_tmins1, Shibor, strike, lastTradingDate, futInstrumentID

适配回测（bomber_adapter）与实盘（stgWrapperExt）

_noddb 变体改动（基于_rtiv）：
  1. 完全移除 DolphinDB 依赖：
     - strike 从合约代码解析（如 MO2509-C-5200 → 5200）
     - lastTradingDate 从本地 expiry_dates.parquet 加载
     - futInstrumentID 由期权月份推导（MO2509 → IM2509）
     - Shibor 用固定常量，IV 全部实时反解（失败时用最近一次成功反解值兑底）
  2. forward_price 直接用对应期货的实时价格（替代 昨结算价×指数比例 的平移近似）
  3. 显式虚值筛选：只卖虚值认购（strike > 指数现价），与实盘口径一致

BarData 字段：
  InstStr()  → 合约代码
  timeStamp  → 纳秒时间戳
  closePrice → 收盘价
"""
import os
import math
import numpy as np
import pandas as pd

# 标准正态分布 CDF/PDF（替代 scipy.stats.norm）
_erf_vec = np.vectorize(math.erf)

def _norm_cdf(x):
    return 0.5 * (1.0 + _erf_vec(np.asarray(x, dtype=float) / np.sqrt(2.0)))

def _norm_pdf(x):
    return np.exp(-0.5 * np.asarray(x, dtype=float) ** 2) / np.sqrt(2.0 * np.pi)

def _norm_cdf_s(x):
    """标量版标准正态CDF（供反解循环使用）"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _implied_iv(F, K, T, r, price):
    """Black-76认购隐含波动率反解（二分法），无解返回None"""
    if F <= 0 or K <= 0 or T <= 0 or price <= 0:
        return None
    disc = math.exp(-r * T)
    # 无套利边界： disc*max(F-K,0) <= price <= disc*F
    if price < disc * max(F - K, 0.0) or price >= disc * F:
        return None
    lo, hi = 1e-4, 5.0
    sqrt_t = math.sqrt(T)
    log_fk = math.log(F / K)
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        d1 = (log_fk + 0.5 * mid * mid * T) / (mid * sqrt_t)
        c = disc * (F * _norm_cdf_s(d1) - K * _norm_cdf_s(d1 - mid * sqrt_t))
        if c > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)

try:
    import bomber_adapter as rp
except ImportError:
    try:
        from stgWrapperExt import rp
    except ImportError:
        import stgWrapper as rp


class OptionVegaStrategy(rp.IPyStrategy):
    """卖出认购期权+期货Delta对冲策略（回测/实盘共用）"""

    # 触发时间
    OPTION_TRADE_SLOT = "13:58"
    HEDGE_SLOTS = ["10:00", "11:00", "13:30", "14:00", "14:55"]

    # 策略参数
    FUT_PRODUCT = "IM"
    OPT_PRODUCT = "MO"
    INDEX_CODE = "SH000852"  # 中证1000指数
    NAV = 40000000.0
    DELTA_TARGETS = [0.2, 0.25, 0.3, 0.35]
    VEGA_BUDGET = 1.25
    FUT_MULTIPLIER = 200
    OPT_MULTIPLIER = 100
    MIN_REMAINING_DAYS = 5
    MIN_TIME_VALUE = 5.0     # 时间价值<5点时不反解IV（深度实值反解病态）
    IV_DEV_LIMIT = 0.10      # 保留（无DDB后不再有昨日IV回退）
    SHIBOR_CONST = 0.02      # 固定无风险利率（替代DDB Shibor）

    def __init__(self, insts=None, nav=None, calendar_path=None, expiry_path=None):
        rp.IPyStrategy.__init__(self)
        self._nav = nav or self.NAV
        self._all_insts = list(insts) if insts else []
        self._calendar_path = calendar_path
        self._expiry_path = expiry_path

        # 行情缓存
        self._bar_cache = {}

        # 持仓跟踪（目标仓位，手）
        self._opt_positions = {}
        self._fut_positions = {}

        # 本地静态数据缓存（替代DDB）
        self._local_data_cache = None
        self._expiry_map = {}
        self._ddb_date = None
        self._iv_cache = {}   # {inst: 最近一次成功反解的IV}，反解失败时兑底

        # 防重
        self._sent_option_date = None
        self._hedge_sent = set()
        self._cleanup_date = None
        self._current_bar_date = None
        self._pending_slot = None  # (bar_date, slot, ts, bar_ts) 延迟执行

        # 合约分组
        self._futures = {}
        self._options = {}
        self._fut_by_month = {}
        self._calls_by_month = {}
        self._common_months = set()
        self._parse_insts()

        # 交易日历
        self._trading_days = None
        self._load_calendar()
        self._load_expiry()

    # ---- 交易日历 ----

    def _load_calendar(self):
        """加载交易日历"""
        if self._calendar_path and os.path.exists(self._calendar_path):
            df = pd.read_csv(self._calendar_path)
            df['date'] = pd.to_datetime(df['date'])
            self._trading_days = df[df['is_trading_day'] == True]['date'].sort_values().tolist()
            print(f"[OptionVega] 交易日历加载: {len(self._trading_days)}个交易日")
        else:
            print(f"[OptionVega] 交易日历未找到: {self._calendar_path}")

    def _trading_days_between(self, start_date, end_date):
        """计算两个日期间的交易日数（不含start，含end）"""
        if self._trading_days is None:
            return None
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        return sum(1 for d in self._trading_days if start < d <= end)

    # ---- 本地静态数据（替代DDB） ----

    def _load_expiry(self):
        """从本地 expiry_dates.parquet 加载最后交易日"""
        if self._expiry_path and os.path.exists(self._expiry_path):
            df = pd.read_parquet(self._expiry_path)
            self._expiry_map = dict(zip(
                df['code'].astype(str).str.split('.').str[0],
                pd.to_datetime(df['expiry_date'])))
            print(f"[OptionVega] 到期日数据加载: {len(self._expiry_map)}个合约")
        else:
            print(f"[OptionVega] 到期日数据未找到: {self._expiry_path}")

    def _local_data(self, bar_date):
        """构建当日候选期权静态表（替代DDB查询）
        字段与原DDB表对齐：Code/optionType/strike/lastTradingDate/futInstrumentID/Shibor
        """
        if self._local_data_cache is not None:
            return self._local_data_cache
        rows = []
        for ym, calls in self._calls_by_month.items():
            fut_inst = self.FUT_PRODUCT + f"{ym[0] % 100:02d}{ym[1]:02d}"
            for inst, strike in calls.items():
                ltd = self._expiry_map.get(inst)
                if ltd is None or strike <= 0:
                    continue
                rows.append({'Code': inst, 'optionType': '认购', 'strike': strike,
                             'lastTradingDate': ltd, 'futInstrumentID': fut_inst,
                             'Shibor': self.SHIBOR_CONST})
        self._local_data_cache = pd.DataFrame(rows)
        print(f"[OptionVega] 本地静态数据: {len(self._local_data_cache)}个认购合约")
        return self._local_data_cache

    # ---- 合约解析 ----

    @staticmethod
    def _parse_future(inst):
        """IM2509 → ('IM', 2025, 9)"""
        inst = inst.split('.')[0]
        if len(inst) < 4:
            return None
        root, code = inst[:2], inst[2:]
        if len(code) == 4 and code.isdigit():
            return root, 2000 + int(code[:2]), int(code[2:])
        return None

    @staticmethod
    def _parse_option(inst):
        """MO2509-C-5200 → ('MO', 2025, 9, 5200.0, 'CALL')"""
        inst = inst.split('.')[0]
        if "-" not in inst:
            return None
        parts = inst.split("-")
        if len(parts) < 3:
            return None
        product = parts[0]
        cp = parts[1]
        if len(cp) > 1 and cp[1:].isdigit():
            direction, strike_str = cp[0], cp[1:]
        else:
            direction, strike_str = cp, parts[2]
        if len(product) < 4 or not product[-4:].isdigit():
            return None
        yy, mm = int(product[-4:-2]), int(product[-2:])
        root, year, month = product[:-4], 2000 + yy, mm
        strike = float(strike_str) if strike_str.replace(".", "").isdigit() else 0.0
        opt_type = "CALL" if direction.upper() in ("C", "CALL") else "PUT"
        return root, year, month, strike, opt_type

    def _parse_insts(self):
        """解析合约 → 按月份分组 → 确定共有月份"""
        for inst in self._all_insts:
            f = self._parse_future(inst)
            if f and f[0] == self.FUT_PRODUCT:
                self._futures[inst] = f
                self._fut_by_month[(f[1], f[2])] = inst
                continue
            o = self._parse_option(inst)
            if o and o[0] == self.OPT_PRODUCT:
                self._options[inst] = o
                if o[4] == "CALL":
                    key = (o[1], o[2])
                    if key not in self._calls_by_month:
                        self._calls_by_month[key] = {}
                    self._calls_by_month[key][inst] = o[3]

        fut_months = set(self._fut_by_month.keys())
        opt_months = set(self._calls_by_month.keys())
        self._common_months = fut_months & opt_months

        print(f"[OptionVega] 期货{len(self._futures)} 期权{len(self._options)} "
              f"共有月份{len(self._common_months)}个")

    def _current_near_month(self, bar_ym):
        """近月：共有的最早到期月 >= 当前月"""
        future = [ym for ym in self._common_months if ym >= bar_ym]
        return min(future) if future else None

    # ---- 框架接口 ----

    def get_insts(self) -> list:
        """返回所有需要订阅的合约（含中证1000指数）"""
        result = [self.INDEX_CODE]
        for ym in self._common_months:
            if ym in self._fut_by_month:
                result.append(self._fut_by_month[ym])
            if ym in self._calls_by_month:
                result.extend(self._calls_by_month[ym].keys())
        return result

    def initialize(self, name: str, env):
        super().initialize(name, env)
        for inst in self.get_insts():
            if inst == self.INDEX_CODE or self.is_option(inst):
                self.subBar(inst, "M5")
            else:
                self.subBar(inst, "M1")

    def set_contracts(self, contract, contracts_df=None):
        pass

    # ---- 核心逻辑 ----

    def onBar(self, BarData: rp.BarData):
        """仅缓存行情，不立即交易。交易延迟到下一个slot的bar到达时执行（确保所有合约bar已到齐）"""
        inst = BarData.InstStr().split('.')[0]
        ts = BarData.timeStamp
        close = BarData.closePrice
        if close is None:
            return

        # 时间转换
        try:
            bar_ts = pd.Timestamp(BarData.cst_time)
        except (AttributeError, TypeError):
            bar_ts = pd.Timestamp(ts, unit='ns', tz='UTC').tz_convert('Asia/Shanghai')

        slot = f"{bar_ts.hour:02d}:{bar_ts.minute:02d}"
        bar_date = f"{bar_ts.year:04d}-{bar_ts.month:02d}-{bar_ts.day:02d}"

        # 如果slot或日期变了，先执行上一个pending（用旧缓存，此时上一个slot的所有bar已到齐）
        if self._pending_slot is not None:
            p_date, p_slot, p_ts, p_bar_ts = self._pending_slot
            if slot != p_slot or bar_date != p_date:
                self._pending_slot = None
                self._try_trade(p_date, p_slot, p_ts, p_bar_ts)

        # 新的一天：清空行情缓存（过期期权当天无bar，不会进入缓存）
        if self._current_bar_date != bar_date:
            self._bar_cache = {}
            self._current_bar_date = bar_date

        self._bar_cache[inst] = close

        # 设置当前slot为pending（如果是交易时点且尚未执行）
        if self._is_trade_slot(slot) and not self._is_slot_done(bar_date, slot):
            self._pending_slot = (bar_date, slot, ts, bar_ts)

    def _is_trade_slot(self, slot):
        """判断是否为交易时点"""
        return slot == self.OPTION_TRADE_SLOT or slot in self.HEDGE_SLOTS

    def _is_slot_done(self, bar_date, slot):
        """判断该时点当日是否已执行"""
        if slot == self.OPTION_TRADE_SLOT:
            return self._sent_option_date == bar_date
        if slot in self.HEDGE_SLOTS:
            return f"{bar_date}_{slot}" in self._hedge_sent
        return True

    def _try_trade(self, bar_date, slot, ts, bar_ts):
        """统一交易入口：先标记防重，再执行计算，最后统一发送所有持仓"""
        # 期权交易（14:00）：计算新期权仓位，不重算期货
        if slot == self.OPTION_TRADE_SLOT and self._sent_option_date != bar_date:
            self._sent_option_date = bar_date  # 先标记
            self._option_trade(bar_date, bar_ts, ts)
            self._send_all_positions(ts)

        # 期货对冲：重算期货仓位
        if slot in self.HEDGE_SLOTS:
            hedge_key = f"{bar_date}_{slot}"
            if hedge_key not in self._hedge_sent:
                self._hedge_sent.add(hedge_key)  # 先标记，防止重入
                self._future_hedge(bar_date, bar_ts, ts)
                self._send_all_positions(ts)

    def _send_all_positions(self, ts):
        """统一发送所有非零持仓的目标仓位（期权+期货），仅最后一笔isLast=True"""
        # 先清理已过期期权（避免发送过期合约的targetVol）
        if self._current_bar_date:
            self._cleanup_expired_options(self._current_bar_date, self._local_data(self._current_bar_date))

        items = []
        for inst, pos in self._opt_positions.items():
            if pos != 0 and inst in self._bar_cache:
                items.append((inst, float(pos)))
        for inst, pos in self._fut_positions.items():
            if pos != 0 and inst in self._bar_cache:
                items.append((inst, float(pos)))

        if not items:
            return

        for i, (inst, pos) in enumerate(items):
            is_last = (i == len(items) - 1)
            self.sendTargetVol(inst, pos, ts, is_last)

        print(f"[OptionVega] 发送{len(items)}个合约目标仓位:")
        for inst, pos in items:
            print(f"  {inst}: {pos}")

    def _cleanup_expired_options(self, bar_date, ddb_data=None):
        """清理已过期期权：用lastTradingDate精确判断"""
        today = pd.Timestamp(bar_date)
        expired = []

        if self._expiry_map:
            expired = [inst for inst in self._opt_positions
                       if self._expiry_map.get(inst, pd.Timestamp.max) < today]
        else:
            # 月份fallback
            bar_ym = (int(bar_date[:4]), int(bar_date[5:7]))
            expired = [inst for inst in list(self._opt_positions.keys())
                       if (o := self._parse_option(inst)) and (o[1], o[2]) < bar_ym]

        for inst in expired:
            del self._opt_positions[inst]
        if expired:
            print(f"[OptionVega] {bar_date} 清理过期期权: {len(expired)}个 {expired}")

    def _option_trade(self, bar_date, bar_ts, ts):
        """期权开仓：计算新仓位并更新_opt_positions（不发单，由_send_all_positions统一发）"""
        local_data = self._local_data(bar_date)
        if local_data is None or local_data.empty:
            return

        self._cleanup_expired_options(bar_date, local_data)

        # 1. 筛选目标期权 + 计算新仓位
        target = self._select_target_options(local_data, bar_date)
        if target is not None and not target.empty:
            new_pos = self._calc_option_positions(target, bar_date)
            for inst, pos in new_pos.items():
                self._opt_positions[inst] = self._opt_positions.get(inst, 0) + pos

        # 2. 评估平仓（时间价值≤0）
        close_pos = self._evaluate_close(local_data)
        for inst, pos in close_pos.items():
            self._opt_positions[inst] = self._opt_positions.get(inst, 0) + pos

        active = {k: v for k, v in self._opt_positions.items() if v != 0}
        print(f"[OptionVega] {bar_date} 14:00 期权交易 持仓: {active}")

    def _select_target_options(self, local_data, bar_date):
        """筛选目标期权：虚值认购，剩余交易日≥5，最近到期月"""
        df = local_data.copy()
        # 只选认购
        df = df[df['optionType'] == '认购']
        if df.empty:
            return df

        # 标准化Code
        df['Code'] = df['Code'].astype(str).str.split('.').str[0]

        # 筛选有行情的合约
        df = df[df['Code'].isin(self._bar_cache.keys())]
        if df.empty:
            return df

        # 解析数值列（去掉逗号）
        df['strike'] = pd.to_numeric(
            df['strike'].astype(str).str.replace(',', ''), errors='coerce')
        df = df.dropna(subset=['strike'])

        # 获取标的期货价格（用于后续forward_price和对冲）
        df['futInst'] = df['futInstrumentID'].astype(str).str.split('.').str[0]
        df['futClosePrice'] = df['futInst'].map(self._bar_cache)
        df = df.dropna(subset=['futClosePrice'])

        # 获取中证1000指数价格（用于ITM筛选和forward_price）
        index_close = self._bar_cache.get(self.INDEX_CODE, 0)
        if index_close <= 0:
            return df.iloc[0:0]
        df['underlyerClosePrice'] = index_close

        # 虚值筛选：认购要求 strike > 指数现价（与实盘口径一致）
        df['closePrice'] = df['Code'].map(self._bar_cache)
        df = df[df['strike'] > df['underlyerClosePrice']]
        if df.empty:
            return df

        # 计算剩余交易日（用交易日历）
        trading_date = pd.Timestamp(bar_date)
        df['lastTradingDate'] = pd.to_datetime(df['lastTradingDate'])
        df['剩余交易日'] = df['lastTradingDate'].apply(
            lambda d: self._trading_days_between(trading_date, d))
        df = df.dropna(subset=['剩余交易日'])
        df = df[df['剩余交易日'] >= self.MIN_REMAINING_DAYS]
        if df.empty:
            return df

        # 最近到期月
        min_days = df['剩余交易日'].min()
        df = df[df['剩余交易日'] == min_days]

        return df

    def _calc_option_positions(self, df, bar_date):
        """计算期权仓位（布莱克—斯科尔斯模型风险指标 + Vega预算）

        _noddb：forward用实时期货价，IV从实时期权价反解（反解失败则剔除）
        """
        valid = df.copy()
        # Shibor（无风险利率，反解与vega折现都需要）
        valid['Shibor'] = pd.to_numeric(valid['Shibor'], errors='coerce')
        valid = valid.dropna(subset=['Shibor'])
        if valid.empty:
            return {}

        # 剩余自然日
        trading_date = pd.Timestamp(bar_date)
        valid['剩余自然日'] = valid['lastTradingDate'].apply(
            lambda d: (d - trading_date).days)

        # forward_price 直接取对应期货实时价（替代昨结算平移近似）
        valid['forward_price'] = valid['futClosePrice']

        # 实时反解IV，失败则剔除该合约
        iv_list = []
        for _, row in valid.iterrows():
            tau = row['剩余交易日'] / 252.0
            iv = None
            time_value = row['closePrice'] - max(row['forward_price'] - row['strike'], 0.0)
            if tau > 0 and time_value >= self.MIN_TIME_VALUE:
                iv = _implied_iv(row['forward_price'], row['strike'], tau,
                                 row['Shibor'], row['closePrice'])
            if iv is None:
                iv = self._iv_cache.get(row['Code'])  # 兑底：最近一次成功反解值
            if iv is not None and iv > 0:
                self._iv_cache[row['Code']] = iv
            iv_list.append(iv)
        valid['IV'] = iv_list
        valid = valid.dropna(subset=['IV'])
        valid = valid[valid['IV'] > 0]
        if valid.empty:
            return {}
        print(f"[OptionVega] {bar_date} IV实时反解成功: {len(valid)}张 失败剔除: {len(iv_list)-len(valid)}张")

        # 布莱克—斯科尔斯模型风险指标
        T = valid['剩余交易日'] / 252
        d1 = (np.log(valid['forward_price'] / valid['strike'])
              + (valid['IV'] ** 2) / 2 * T) / (valid['IV'] * np.sqrt(T))
        valid['delta'] = _norm_cdf(d1)
        valid['gamma'] = _norm_pdf(d1) / (
            valid['forward_price'] * valid['IV'] * np.sqrt(T))
        valid['vega'] = _norm_pdf(d1) * valid['forward_price'] * np.sqrt(T) * np.exp(
            -valid['Shibor'] * (valid['剩余自然日'] / 365))

        # 选择最接近delta目标的合约
        deltas = valid['delta'].to_numpy()
        diffs = np.abs(deltas[:, np.newaxis] - np.array(self.DELTA_TARGETS))
        closest_indices = diffs.argmin(axis=0)
        target = valid.iloc[closest_indices].copy()

        # 仓位计算
        target['pos_new'] = -(1 / target['gamma']) / (
            target['vega'] / target['gamma']).sum() * self.VEGA_BUDGET * 10 * self._nav / 1000
        # 先按合约汇总，再四舍五入到100张，转为手
        result = target.groupby('Code')['pos_new'].sum()
        result = np.round(result / 100, 0) * 100 / 100
        return dict(result)

    def _evaluate_close(self, ddb_data):
        """评估持仓平仓：时间价值≤0则平仓"""
        if not self._opt_positions:
            return {}

        # 标准化DDB Code
        ddb = ddb_data.copy()
        ddb['Code'] = ddb['Code'].astype(str).str.split('.').str[0]
        ddb['futInst'] = ddb['futInstrumentID'].astype(str).str.split('.').str[0]

        close_pos = {}
        for inst, pos in self._opt_positions.items():
            if pos == 0 or inst not in self._bar_cache:
                continue

            row = ddb[ddb['Code'] == inst]
            if row.empty:
                continue

            strike = pd.to_numeric(
                str(row.iloc[0]['strike']).replace(',', ''), errors='coerce')
            if pd.isna(strike):
                continue

            fut_inst = row.iloc[0]['futInst']
            fut_close = self._bar_cache.get(fut_inst, 0)
            opt_close = self._bar_cache.get(inst, 0)
            if fut_close <= 0 or opt_close <= 0:
                continue

            # 时间价值 = 期权价格 - 内在价值
            time_value = opt_close - (fut_close - strike)
            if time_value <= 0:
                close_pos[inst] = -pos  # 平仓

        return close_pos

    def _future_hedge(self, bar_date, bar_ts, ts):
        """期货Delta对冲"""
        if not self._opt_positions:
            return

        ddb_data = self._local_data(bar_date)
        if ddb_data is None or ddb_data.empty:
            return

        # 标准化DDB
        ddb = ddb_data.copy()
        ddb['Code'] = ddb['Code'].astype(str).str.split('.').str[0]
        ddb['futInst'] = ddb['futInstrumentID'].astype(str).str.split('.').str[0]

        # 当日剩余交易分钟
        remaining_minutes = self._calc_remaining_minutes(bar_ts)

        # 按futInstrumentID分组计算delta
        fut_delta = {}
        trading_date = pd.Timestamp(bar_date)
        for inst, pos in self._opt_positions.items():
            if pos == 0 or inst not in self._bar_cache:
                continue

            row = ddb[ddb['Code'] == inst]
            if row.empty:
                continue

            strike = pd.to_numeric(
                str(row.iloc[0]['strike']).replace(',', ''), errors='coerce')
            if pd.isna(strike) or strike <= 0:
                continue

            fut_inst = row.iloc[0]['futInst']
            fut_close = self._bar_cache.get(fut_inst, 0)
            if fut_close <= 0:
                continue

            last_date = pd.to_datetime(row.iloc[0]['lastTradingDate'])
            remaining_days = self._trading_days_between(trading_date, last_date)
            if remaining_days is None:
                continue
            tao = remaining_days / 252 + remaining_minutes / (240 * 252)
            if tao <= 0:
                continue

            # _noddb：实时反解IV（用实时期权价+实时期货价），失败则不对冲该合约
            opt_close = self._bar_cache.get(inst, 0)
            shibor = pd.to_numeric(row.iloc[0].get('Shibor'), errors='coerce')
            r = shibor if pd.notna(shibor) else self.SHIBOR_CONST
            iv = None
            if opt_close > 0:
                time_value = opt_close - max(fut_close - strike, 0.0)
                if time_value >= self.MIN_TIME_VALUE:
                    iv = _implied_iv(fut_close, strike, tao, r, opt_close)
            if iv is None:
                iv = self._iv_cache.get(inst)  # 兑底：最近一次成功反解值，避免对冲缺口
            if iv is not None and iv > 0:
                self._iv_cache[inst] = iv
            else:
                continue

            d1 = (np.log(fut_close / strike) + (iv ** 2) / 2 * tao) / (iv * np.sqrt(tao))
            delta = _norm_cdf(d1)

            # pos in 手, * OPT_MULTIPLIER = 张
            fut_delta[fut_inst] = fut_delta.get(fut_inst, 0) + pos * self.OPT_MULTIPLIER * delta

        # 计算期货对冲仓位（更新_fut_positions，不发单）
        for fut_inst, total_delta in fut_delta.items():
            hedge_pos = -total_delta / self.FUT_MULTIPLIER  # 张 → 手
            hedge_pos = float(np.round(hedge_pos))
            self._fut_positions[fut_inst] = hedge_pos

        # 清理无期权敞口的旧月份期货仓位（期权到期/平仓后对冲仓归零）
        for fut_inst in list(self._fut_positions.keys()):
            if fut_inst not in fut_delta:
                self._fut_positions[fut_inst] = 0.0

        active = {k: v for k, v in self._fut_positions.items() if v != 0}
        print(f"[OptionVega] {bar_date} 期货对冲 持仓: {active}")

    # ---- 工具方法 ----

    @staticmethod
    def _calc_remaining_minutes(bar_ts):
        """计算当日剩余交易分钟（240=4h×60min）"""
        h, m = bar_ts.hour, bar_ts.minute
        total = h * 60 + m
        if total < 570:       # 9:30前
            return 240
        elif total < 690:     # 9:30-11:30
            return 240 - (total - 570)
        elif total < 780:     # 11:30-13:00 午休
            return 120
        elif total < 900:     # 13:00-15:00
            return 120 - (total - 780)
        else:
            return 0

    # ---- 其他回调 ----

    def onBatchBar(self, inst, ts, barData, isLast):
        """批量K线回调：isLast=True时所有合约bar已到齐，立即执行交易"""
        inst = inst.split('.')[0]
        close = barData.closePrice
        if close is None:
            return

        self._bar_cache[inst] = close

        if not isLast:
            return

        # isLast=True：所有bar到齐，清除pending（避免onBar的延迟执行重复触发）
        self._pending_slot = None

        try:
            bar_ts = pd.Timestamp(barData.cst_time)
        except (AttributeError, TypeError):
            bar_ts = pd.Timestamp(barData.timeStamp, unit='ns', tz='UTC').tz_convert('Asia/Shanghai')

        slot = f"{bar_ts.hour:02d}:{bar_ts.minute:02d}"
        bar_date = f"{bar_ts.year:04d}-{bar_ts.month:02d}-{bar_ts.day:02d}"
        self._try_trade(bar_date, slot, barData.timeStamp, bar_ts)

    def onDailyOpen(self, date: str):
        # 先执行上一交易日的pending（用旧缓存），再清空
        if self._pending_slot is not None:
            p_date, p_slot, p_ts, p_bar_ts = self._pending_slot
            self._pending_slot = None
            self._try_trade(p_date, p_slot, p_ts, p_bar_ts)
        self._bar_cache = {}

    def onDailyClose(self, date: str):
        # 日末执行未完成的pending trade（14:55等最后一个slot可能没有后续bar触发）
        if self._pending_slot is not None:
            p_date, p_slot, p_ts, p_bar_ts = self._pending_slot
            self._pending_slot = None
            self._try_trade(p_date, p_slot, p_ts, p_bar_ts)
