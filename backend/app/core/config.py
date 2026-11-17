"""应用配置。

所有配置来自环境变量；开发阶段提供可用默认值，生产必须显式覆盖。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置。

    环境变量名与字段名大小写不敏感，例如 ``APP_ENV`` 映射到 ``app_env``。
    """

    model_config = SettingsConfigDict(extra="ignore")

    app_env: Literal["dev", "staging", "prod"] = "dev"
    app_version: str = "0.1.0"
    log_level: str = "info"
    # 反向代理对外服务源；未设置时使用请求源，生产 HTTPS 部署建议显式配置。
    app_origin: str | None = None

    # 开发阶段可先用 local；接入目录服务后改为 ldap
    auth_mode: Literal["local", "ldap"] = "local"
    ldap_server_url: str | None = None
    ldap_bind_dn: str | None = None
    ldap_bind_password: str | None = None
    ldap_user_base_dn: str | None = None
    ldap_user_dn_template: str | None = None
    ldap_user_filter: str = "(uid={username})"
    ldap_group_base_dn: str | None = None
    ldap_role_groups: dict[str, list[str]] = Field(default_factory=dict)
    ldap_connect_timeout_s: int = 5
    ldap_start_tls: bool = True
    ldap_allow_insecure: bool = False
    ldap_ca_cert_file: Path | None = None
    ldap_tls_valid_names: list[str] = Field(default_factory=list)
    auth_break_glass: bool = False
    local_admin_user: str | None = None
    local_admin_hash: str | None = None

    # 本地开发默认连 compose 里的 db；生产通过环境变量覆盖
    database_url: str = "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval"

    # 仅开发默认值；生产必须通过 secrets 注入
    jwt_key: str = "dev-only-change-me-32-bytes-minimum"
    jwt_issuer: str = "asr-eval"
    jwt_audience: str = "asr-eval-web"
    access_ttl_seconds: int = 1800
    refresh_ttl_days: int = 7

    # 本地对象目录（raw response / 报告）
    data_dir: Path = Path("./data")
    # M9 §3.2 凭据密钥默认与对象数据同目录管理，文件由运行用户创建并限制为 0600。
    credentials_key_file: Path | None = None

    # 导入根目录白名单（M1 §8 E_PATH_ESCAPE）；空 = 本机开发不限制，生产必须配置
    import_allowed_roots: list[str] = []
    # 可选只读 NFS 根目录（M9 §2.1）；系统状态页仅检查可读，不遍历内容。
    nfs_root: Path | None = None

    # 走 HTTPS 时置 true；HTTP 下必须为 false，否则浏览器丢弃 refresh cookie
    cookie_secure: bool = False

    # M5 评分队列：并发 worker 数固定，队列只保存 Evaluation ID。
    scoring_workers: int = 2
    scoring_queue_capacity: int = 100
    scoring_page_size: int = 500

    # M4 Run 执行队列：数据库中的 queued Run 是权威队列，进程内队列只传 Run ID。
    runner_workers: int = 2
    runner_queue_capacity: int = 100
    runner_poll_interval_s: float = 1.0

    @property
    def refresh_cookie_name(self) -> str:
        """HTTP 用普通名字；HTTPS 用 __Host- 前缀（要求 Secure）。"""
        return "__Host-refresh" if self.cookie_secure else "asr_refresh"


@lru_cache
def get_settings() -> Settings:
    """返回进程内单例配置。"""
    return Settings()
