"""NE 控制器 Agent 工具定义。"""

from typing import Optional, Type

from pydantic import BaseModel

from app.agent.tools.base import MoviePilotTool
from app.core.plugin import PluginManager

from .schemas import NextEmbyManageToolInput, NextEmbyStatsToolInput


def _get_plugin():
    """获取 NextEmbyController 插件实例。"""
    return PluginManager().running_plugins.get("NextEmbyController")


class NextEmbyStatsTool(MoviePilotTool):
    """NE 系统监控工具。"""
    name: str = "nextemby_stats"
    description: str = (
        "获取 NextEmby（NE）系统监控：硬件状态（CPU/内存/磁盘/网络）、实时活跃播放会话、"
        "系统缓存列表、注册用户列表。"
    )
    args_schema: Type[BaseModel] = NextEmbyStatsToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        action = kwargs.get("action", "stats")
        return f"正在获取 NE 系统监控（{action}）"

    async def run(self, action: str = "stats", **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NE 控制器插件未运行"
        return await plugin.tool_stats(action=action)


class NextEmbyManageTool(MoviePilotTool):
    """NE 管理操作工具。"""
    name: str = "nextemby_manage"
    description: str = (
        "执行 NextEmby（NE）管理操作：用户管理（封禁/解封、批量删除、批量审批、批量改期限、"
        "改 Cookie/缓存、改密码、重置管理员密码）、网盘配置（更新主账号 Cookie、检查网盘挂载、"
        "修改秒传配额策略）、系统控制（踢出播放会话、系统通知测试、开关系统防护、重启系统代理服务）。"
    )
    args_schema: Type[BaseModel] = NextEmbyManageToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        action = kwargs.get("action", "")
        return f"正在执行 NE 管理操作：{action}"

    async def run(self, action: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NE 控制器插件未运行"
        return await plugin.tool_manage(action=action, **kwargs)
