"""NE 控制器 Agent 工具输入模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class NextEmbyStatsToolInput(BaseModel):
    """NE 系统监控输入参数。"""
    action: Optional[str] = Field(
        default="stats",
        description="查询类型：stats=硬件监控（默认）、sessions=活跃播放会话、caches=缓存列表、users=用户列表",
    )


class NextEmbyManageToolInput(BaseModel):
    """NE 管理操作输入参数。"""
    action: str = Field(
        ...,
        description="操作类型：ban=封禁/解封用户、batch_delete=批量删除用户、batch_approve=批量审批注册、"
                    "batch_expiry=批量修改用户期限、update_settings=修改用户Cookie/缓存、edit_info=修改用户密码、"
                    "reset_password=重置管理员密码、update_master_cookie=更新主账号Cookie、check_drive=检查网盘挂载、"
                    "update_user_group=修改秒传配额策略、kill_session=踢出播放会话、notify_test=系统通知测试、"
                    "system_settings=开关系统防护、restart=重启系统代理服务",
    )
    username: Optional[str] = Field(default=None, description="目标用户名（ban/edit_info/reset_password 使用）")
    password: Optional[str] = Field(default=None, description="新密码（edit_info/reset_password 使用）")
    cookies: Optional[str] = Field(default=None, description="115 Cookie（update_settings/update_master_cookie 使用）")
    cache_path: Optional[str] = Field(default=None, description="缓存路径（update_settings 使用）")
    old_cookies: Optional[str] = Field(default=None, description="安全验证 Cookie（reset_password 使用）")
    is_banned: Optional[bool] = Field(default=True, description="是否封禁（ban 使用，true=封禁 false=解封）")
    usernames: Optional[str] = Field(default=None, description="用户名列表 JSON，如 [\"u1\",\"u2\"]（batch_delete 使用）")
    users: Optional[str] = Field(default=None, description="用户列表 JSON（batch_approve/batch_expiry 使用）")
    sync_emby: Optional[bool] = Field(default=True, description="是否同步 Emby 删除（batch_delete 使用）")
    session_id: Optional[str] = Field(default=None, description="播放会话 ID（kill_session 使用）")
    proxy_url: Optional[str] = Field(default=None, description="代理地址（notify_test 使用）")
    enable_ip_lock: Optional[bool] = Field(default=True, description="是否开启 IP 锁（system_settings 使用）")
    force: Optional[bool] = Field(default=True, description="是否强制检测（check_drive 使用）")
    overclock_mode: Optional[bool] = Field(default=False, description="秒传超频模式（update_user_group 使用）")
    expire_days: Optional[int] = Field(default=None, description="用户组到期天数（update_user_group 使用）")
    cookie: Optional[str] = Field(default=None, description="主账号 Cookie（update_master_cookie 使用）")
