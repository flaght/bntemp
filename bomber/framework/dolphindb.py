"""按用途读取 DolphinDB 连接；不加载 SDK、不连接服务器。"""
from dataclasses import dataclass, field
import os


def connection_env(purpose, name, default=None):
    """用途专属变量优先；仅未设置时回退旧 DDB_*，不跨用途回退。"""
    if purpose not in {"market", "reference"}:
        raise ValueError("DolphinDB连接用途须为market或reference")
    return os.getenv(f"DDB_{purpose.upper()}_{name}", os.getenv(f"DDB_{name}", default))


@dataclass(frozen=True)
class DolphinDbConnectionConfig:
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    read_timeout_seconds: int = 15
    enable_ssl: bool = False

    def __post_init__(self):
        if not self.host.strip() or not self.username or not self.password:
            raise ValueError("DolphinDB 连接配置不完整")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("DolphinDB 端口无效")
        if type(self.read_timeout_seconds) is not int or not 1 <= self.read_timeout_seconds <= 120:
            raise ValueError("数据库读取超时须为1至120秒整数")

    @classmethod
    def from_env(cls, *, purpose, read_timeout_seconds=15):
        def required(name):
            value = connection_env(purpose, name, "").strip()
            if not value:
                raise ValueError(f"缺少环境变量: DDB_{purpose.upper()}_{name}（兼容DDB_{name}）")
            return value

        return cls(host=required("HOST"), port=int(connection_env(purpose, "PORT", "8848")),
            username=required("USERNAME"), password=required("PASSWORD"),
            read_timeout_seconds=read_timeout_seconds,
            enable_ssl=connection_env(purpose, "ENABLE_SSL", "false").lower() in {"1", "true", "yes"})
