// 项目自有 CTP TraderApi 绑定，仅提供执行传输层使用的字段。
#include "bomber_ctp_td.hpp"
#include <cstring>
#include <stdexcept>
#ifndef _WIN32
#include <iconv.h>
#endif

namespace py = pybind11;
namespace {
template <size_t N> std::string utf8(const char (&value)[N]) {
    size_t size = 0;
    while (size < N && value[size] != '\0') ++size;
    std::string input(value, size);
    if (input.empty()) return input;
#ifndef _WIN32
    iconv_t cd = iconv_open("UTF-8", "GB18030");
    if (cd == reinterpret_cast<iconv_t>(-1)) return input;
    std::string output(input.size() * 4 + 1, '\0');
    char *in = input.data(), *out = output.data();
    size_t in_left = input.size(), out_left = output.size();
    auto status = iconv(cd, &in, &in_left, &out, &out_left);
    iconv_close(cd);
    if (status == static_cast<size_t>(-1)) return input;
    output.resize(output.size() - out_left);
    return output;
#else
    return input;
#endif
}
template <size_t N>
void in_string(const py::dict &fields, const char *key, char (&dest)[N]) {
    if (!fields.contains(key)) return;
    auto value = py::cast<std::string>(fields[py::str(key)]);
    if (value.size() >= N) throw py::value_error(std::string(key) + " exceeds CTP field length");
    std::memcpy(dest, value.c_str(), value.size() + 1);
}
void in_char(const py::dict &fields, const char *key, char &dest) {
    if (!fields.contains(key)) return;
    auto value = py::cast<std::string>(fields[py::str(key)]);
    if (value.size() != 1) throw py::value_error(std::string(key) + " must be one character");
    dest = value[0];
}
template <typename T>
void in_number(const py::dict &fields, const char *key, T &dest) {
    if (fields.contains(key)) dest = py::cast<T>(fields[py::str(key)]);
}
#define OS(n) result[#n] = utf8(value.n)
#define ON(n) result[#n] = value.n
#define OC(n) result[#n] = std::string(1, value.n)
#define IS(n) in_string(fields, #n, value.n)
#define IN(n) in_number(fields, #n, value.n)
#define IC(n) in_char(fields, #n, value.n)

py::dict as_dict(const CThostFtdcRspInfoField &value) {
    py::dict result; ON(ErrorID); OS(ErrorMsg); return result;
}
py::dict as_dict(const CThostFtdcRspAuthenticateField &value) {
    py::dict result; OS(BrokerID); OS(UserID); OS(AppID); return result;
}
py::dict as_dict(const CThostFtdcRspUserLoginField &value) {
    py::dict result; OS(TradingDay); OS(BrokerID); OS(UserID);
    OS(MaxOrderRef); ON(FrontID); ON(SessionID); return result;
}
py::dict as_dict(const CThostFtdcSettlementInfoConfirmField &value) {
    py::dict result; OS(BrokerID); OS(InvestorID); OS(ConfirmDate); OS(ConfirmTime); return result;
}
py::dict as_dict(const CThostFtdcInvestorPositionField &value) {
    py::dict result; OS(BrokerID); OS(InvestorID); OS(InstrumentID);
    OS(ExchangeID); OC(PosiDirection); ON(Position);
    OC(PositionDate); OC(HedgeFlag); ON(TodayPosition); ON(YdPosition);
    ON(PositionCost); return result;
}
py::dict as_dict(const CThostFtdcTradingAccountField &value) {
    py::dict result; OS(BrokerID); OS(AccountID); OS(CurrencyID);
    ON(Balance); ON(Available); ON(CurrMargin); return result;
}
py::dict as_dict(const CThostFtdcOrderField &value) {
    py::dict result; OS(BrokerID); OS(InvestorID); OS(InstrumentID);
    OS(ExchangeID); OS(OrderRef); OS(OrderSysID); OC(Direction);
    OC(OrderStatus); OC(OrderSubmitStatus); ON(VolumeTotalOriginal);
    ON(VolumeTraded); ON(VolumeTotal); ON(FrontID); ON(SessionID);
    OS(StatusMsg); return result;
}
py::dict as_dict(const CThostFtdcTradeField &value) {
    py::dict result; OS(BrokerID); OS(InvestorID); OS(InstrumentID);
    OS(ExchangeID); OS(OrderRef); OS(TradeID); OC(Direction); OC(OffsetFlag);
    ON(Price); ON(Volume); OS(TradingDay); OS(TradeTime); return result;
}
py::dict as_dict(const CThostFtdcInputOrderField &value) {
    py::dict result; OS(BrokerID); OS(InvestorID); OS(InstrumentID);
    OS(ExchangeID); OS(OrderRef); return result;
}
py::dict as_dict(const CThostFtdcInputOrderActionField &value) {
    py::dict result; OS(BrokerID); OS(InvestorID); OS(InstrumentID);
    OS(ExchangeID); OS(OrderRef); return result;
}
py::dict as_dict(const CThostFtdcOrderActionField &value) {
    py::dict result; OS(BrokerID); OS(InvestorID); OS(InstrumentID);
    OS(ExchangeID); OS(OrderRef); return result;
}
template <typename T> py::dict maybe_dict(const T *value) {
    return value ? as_dict(*value) : py::dict();
}
} // 匿名命名空间

TdApi::~TdApi() { shutdown(); }
void TdApi::createFtdcTraderApi(const std::string &path, bool production) {
        if (api_) throw std::runtime_error("TraderApi already created");
        api_ = CThostFtdcTraderApi::CreateFtdcTraderApi(path.c_str(), production);
        if (!api_) throw std::runtime_error("CreateFtdcTraderApi returned null");
        api_->RegisterSpi(this);
    }
void TdApi::registerFront(const std::string &front) {
        auto copy = front;
        required().RegisterFront(copy.data());
    }
void TdApi::subscribePrivateTopic(int mode) { required().SubscribePrivateTopic(resume_type(mode)); }
void TdApi::subscribePublicTopic(int mode) { required().SubscribePublicTopic(resume_type(mode)); }
void TdApi::init() {
        required();
        if (worker_.joinable()) throw std::runtime_error("TraderApi already initialized");
        worker_ = std::thread([this] { dispatch(); });
        api_->Init();
    }
void TdApi::release() { shutdown(); }
int TdApi::exit() { shutdown(); return 0; }

int TdApi::reqAuthenticate(const py::dict &fields, int id) {
        CThostFtdcReqAuthenticateField value{};
        IS(BrokerID); IS(UserID); IS(AuthCode); IS(AppID);
        return required().ReqAuthenticate(&value, id);
    }
int TdApi::reqUserLogin(const py::dict &fields, int id) {
        CThostFtdcReqUserLoginField value{};
        IS(BrokerID); IS(UserID); IS(Password);
        return required().ReqUserLogin(&value, id);
    }
int TdApi::reqSettlementInfoConfirm(const py::dict &fields, int id) {
        CThostFtdcSettlementInfoConfirmField value{};
        IS(BrokerID); IS(InvestorID);
        return required().ReqSettlementInfoConfirm(&value, id);
    }
int TdApi::reqQryInvestorPosition(const py::dict &fields, int id) {
        CThostFtdcQryInvestorPositionField value{};
        IS(BrokerID); IS(InvestorID);
        return required().ReqQryInvestorPosition(&value, id);
    }
int TdApi::reqQryTradingAccount(const py::dict &fields, int id) {
        CThostFtdcQryTradingAccountField value{};
        IS(BrokerID); IS(InvestorID);
        return required().ReqQryTradingAccount(&value, id);
    }
int TdApi::reqQryOrder(const py::dict &fields, int id) {
        CThostFtdcQryOrderField value{};
        IS(BrokerID); IS(InvestorID);
        return required().ReqQryOrder(&value, id);
    }
int TdApi::reqOrderInsert(const py::dict &fields, int id) {
        CThostFtdcInputOrderField value{};
        IS(BrokerID); IS(InvestorID); IS(UserID); IS(InstrumentID);
        IS(ExchangeID); IS(OrderRef); IC(Direction); IC(OrderPriceType);
        IS(CombOffsetFlag); IS(CombHedgeFlag); IN(LimitPrice);
        IN(VolumeTotalOriginal); IC(TimeCondition); IC(VolumeCondition);
        IN(MinVolume); IC(ContingentCondition); IC(ForceCloseReason);
        IN(IsAutoSuspend); IN(UserForceClose);
        return required().ReqOrderInsert(&value, id);
    }
int TdApi::reqOrderAction(const py::dict &fields, int id) {
        CThostFtdcInputOrderActionField value{};
        IS(BrokerID); IS(InvestorID); IS(UserID); IS(InstrumentID);
        IS(ExchangeID); IS(OrderRef); IN(FrontID); IN(SessionID); IC(ActionFlag);
        return required().ReqOrderAction(&value, id);
    }

void TdApi::OnFrontConnected() { enqueue([this] { emit("onFrontConnected"); }); }
void TdApi::OnFrontDisconnected(int reason) {
        enqueue([this, reason] { emit("onFrontDisconnected", reason); });
    }
#define RSP(method, name, type) \
    void TdApi::method(type *data, CThostFtdcRspInfoField *error, int id, bool last) { \
        auto d = copy(data); auto e = copy(error); \
        enqueue([this, d, e, id, last] { emit(name, maybe_dict(d.get()), maybe_dict(e.get()), id, last); }); \
    }
#define RTN(method, name, type) \
    void TdApi::method(type *data) { \
        auto d = copy(data); enqueue([this, d] { emit(name, maybe_dict(d.get())); }); \
    }
#define ERR(method, name, type) \
    void TdApi::method(type *data, CThostFtdcRspInfoField *error) { \
        auto d = copy(data); auto e = copy(error); \
        enqueue([this, d, e] { emit(name, maybe_dict(d.get()), maybe_dict(e.get())); }); \
    }
    RSP(OnRspAuthenticate, "onRspAuthenticate", CThostFtdcRspAuthenticateField)
    RSP(OnRspUserLogin, "onRspUserLogin", CThostFtdcRspUserLoginField)
    RSP(OnRspSettlementInfoConfirm, "onRspSettlementInfoConfirm", CThostFtdcSettlementInfoConfirmField)
    RSP(OnRspQryInvestorPosition, "onRspQryInvestorPosition", CThostFtdcInvestorPositionField)
    RSP(OnRspQryTradingAccount, "onRspQryTradingAccount", CThostFtdcTradingAccountField)
    RSP(OnRspQryOrder, "onRspQryOrder", CThostFtdcOrderField)
    RSP(OnRspOrderInsert, "onRspOrderInsert", CThostFtdcInputOrderField)
    RSP(OnRspOrderAction, "onRspOrderAction", CThostFtdcInputOrderActionField)
    RTN(OnRtnOrder, "onRtnOrder", CThostFtdcOrderField)
    RTN(OnRtnTrade, "onRtnTrade", CThostFtdcTradeField)
    ERR(OnErrRtnOrderInsert, "onErrRtnOrderInsert", CThostFtdcInputOrderField)
    ERR(OnErrRtnOrderAction, "onErrRtnOrderAction", CThostFtdcOrderActionField)
void TdApi::OnRspError(CThostFtdcRspInfoField *error, int id, bool last) {
        auto e = copy(error);
        enqueue([this, e, id, last] { emit("onRspError", maybe_dict(e.get()), id, last); });
    }
#undef RSP
#undef RTN
#undef ERR
template <typename T> std::shared_ptr<T> TdApi::copy(const T *value) {
        return value ? std::make_shared<T>(*value) : nullptr;
    }
THOST_TE_RESUME_TYPE TdApi::resume_type(int mode) {
        if (mode < 0 || mode > 3) throw py::value_error("invalid CTP resume mode");
        return static_cast<THOST_TE_RESUME_TYPE>(mode);
    }
CThostFtdcTraderApi &TdApi::required() {
        if (!api_) throw std::runtime_error("TraderApi not created");
        return *api_;
    }
template <typename... Args> void TdApi::emit(const char *name, Args&&... args) {
        if (auto method = py::get_override(this, name)) method(std::forward<Args>(args)...);
    }
void TdApi::enqueue(std::function<void()> action) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping_ || overflowed_) return;
        if (queue_.size() >= 65536) {
            overflowed_ = true;
            queue_.clear();
            queue_.push_back([this] { emit("onFrontDisconnected", -1); });
            cv_.notify_one();
            return;
        }
        queue_.push_back(std::move(action));
        cv_.notify_one();
    }
void TdApi::dispatch() {
        for (;;) {
            std::function<void()> action;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
                if (stopping_) return;
                action = std::move(queue_.front());
                queue_.pop_front();
            }
            py::gil_scoped_acquire gil;
            try { action(); }
            catch (py::error_already_set &error) {
                error.discard_as_unraisable("bomber_ctp_td callback");
                try { emit("onFrontDisconnected", -1); }
                catch (py::error_already_set &nested) {
                    nested.discard_as_unraisable("bomber_ctp_td disconnect callback");
                }
            }
            catch (const std::exception &error) {
                PyErr_SetString(PyExc_RuntimeError, error.what());
                PyErr_WriteUnraisable(Py_None);
                try { emit("onFrontDisconnected", -1); }
                catch (py::error_already_set &nested) {
                    nested.discard_as_unraisable("bomber_ctp_td disconnect callback");
                }
            }
        }
    }
void TdApi::shutdown() {
        if (api_) {
            api_->RegisterSpi(nullptr);
            { py::gil_scoped_release gil; api_->Release(); }
            api_ = nullptr;
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
            queue_.clear();
        }
        cv_.notify_all();
        if (worker_.joinable()) {
            py::gil_scoped_release gil;
            worker_.join();
        }
    }

PYBIND11_MODULE(bomber_ctp_td, module) {
    py::class_<TdApi>(module, "TdApi", py::dynamic_attr())
        .def(py::init<>())
        .def("createFtdcTraderApi", &TdApi::createFtdcTraderApi,
             py::arg("flow_path") = "", py::arg("production_mode") = true)
        .def("registerFront", &TdApi::registerFront)
        .def("subscribePrivateTopic", &TdApi::subscribePrivateTopic)
        .def("subscribePublicTopic", &TdApi::subscribePublicTopic)
        .def("init", &TdApi::init)
        .def("release", &TdApi::release)
        .def("exit", &TdApi::exit)
        .def("reqAuthenticate", &TdApi::reqAuthenticate)
        .def("reqUserLogin", &TdApi::reqUserLogin)
        .def("reqSettlementInfoConfirm", &TdApi::reqSettlementInfoConfirm)
        .def("reqQryInvestorPosition", &TdApi::reqQryInvestorPosition)
        .def("reqQryTradingAccount", &TdApi::reqQryTradingAccount)
        .def("reqQryOrder", &TdApi::reqQryOrder)
        .def("reqOrderInsert", &TdApi::reqOrderInsert)
        .def("reqOrderAction", &TdApi::reqOrderAction);
}
