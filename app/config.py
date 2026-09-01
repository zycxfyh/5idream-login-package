"""服务配置。

所有配置均可通过环境变量（前缀 DREAM_）或项目根目录下的 .env 覆盖，
例如：DREAM_LOGIN_TIMEOUT_SECONDS=120 或 .env 中 LOGIN_TIMEOUT_SECONDS=120。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DREAM_",
        extra="ignore",
    )

    # ---------- 5idream 站点 ----------
    base_url: str = "https://www.5idream.net"

    # ---------- 网络与频率控制 ----------
    # 每次向 5idream 发请求前随机等待的秒数范围（避免触发风控）
    request_delay_range: tuple[float, float] = (0.5, 1.5)
    request_timeout: float = 30.0

    # ---------- 登录流程 ----------
    # 等待扫码确认的最大秒数
    login_timeout_seconds: int = 90
    # 每类数据请求的最大条数
    max_rows: int = 100

    # ---------- 缓存 ----------
    # token 个人缓存：空闲多少秒后过期（0 = 永不过期，重启进程即清空）
    token_idle_ttl: int = 3600
    # 分页查询结果的缓存秒数（按「用户+分类+页码+每页条数」缓存；0 = 不缓存）
    data_cache_ttl: int = 60
    # 登录会话（含已完成）在内存中的保留秒数
    session_ttl: int = 1800

    # ---------- 服务 ----------
    # 允许跨域的来源，多个用逗号分隔；"*" 表示全部
    cors_origins: str = "*"


settings = Settings()
