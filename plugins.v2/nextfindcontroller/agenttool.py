"""NextFind 控制器 Agent 工具定义。"""

from typing import Optional, Type

from pydantic import BaseModel

from app.agent.tools.base import MoviePilotTool
from app.core.plugin import PluginManager

from .schemas import (
    NextFindCreateDirectoryToolInput,
    NextFindDeleteEpisodeToolInput,
    NextFindDeleteMovieToolInput,
    NextFindDeleteSeasonToolInput,
    NextFindDirectoriesToolInput,
    NextFindFillMissingToolInput,
    NextFindHdhiveUnlockToolInput,
    NextFindHistoryToolInput,
    NextFindIgnoredEpisodesToolInput,
    NextFindLocalLibraryToolInput,
    NextFindLogsToolInput,
    NextFindPreviewToolInput,
    NextFindQuotaToolInput,
    NextFindResourcesSearchToolInput,
    NextFindSearchToolInput,
    NextFindShieldSearchToolInput,
    NextFindSubscribeAddToolInput,
    NextFindSubscribeRemoveToolInput,
    NextFindSubscriptionsToolInput,
    NextFindTransferToolInput,
)


def _get_plugin():
    """获取 NextFindController 插件实例。"""
    return PluginManager().running_plugins.get("NextFindController")


class NextFindSearchTool(MoviePilotTool):
    """全局搜索工具。"""
    name: str = "nextfind_search"
    description: str = "在 NextFind 中按名称全局搜索影片或剧集，返回候选列表（含 TMDB ID、类型、年份、评分）。"
    args_schema: Type[BaseModel] = NextFindSearchToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        query = kwargs.get("query", "")
        return f"正在通过 NextFind 搜索：{query}"

    async def run(self, query: str, media_type: str = None, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_search(query=query, media_type=media_type)


class NextFindSubscriptionsTool(MoviePilotTool):
    """获取订阅列表工具。"""
    name: str = "nextfind_subscriptions"
    description: str = "获取 NextFind 当前活跃订阅列表。"
    args_schema: Type[BaseModel] = NextFindSubscriptionsToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在获取 NextFind 订阅列表"

    async def run(self, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_subscriptions()


class NextFindSubscribeAddTool(MoviePilotTool):
    """添加订阅工具。"""
    name: str = "nextfind_subscribe_add"
    description: str = "在 NextFind 中添加订阅（追更）。"
    args_schema: Type[BaseModel] = NextFindSubscribeAddToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        title = kwargs.get("title", "")
        return f"正在通过 NextFind 添加订阅：{title}"

    async def run(self, tmdb_id: str, media_type: str, title: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_subscribe_add(tmdb_id=tmdb_id, media_type=media_type, title=title)


class NextFindSubscribeRemoveTool(MoviePilotTool):
    """取消订阅工具。"""
    name: str = "nextfind_subscribe_remove"
    description: str = "取消 NextFind 中的订阅。"
    args_schema: Type[BaseModel] = NextFindSubscribeRemoveToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 取消订阅：tmdb_id={kwargs.get('tmdb_id', '')}"

    async def run(self, tmdb_id: str, media_type: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_subscribe_remove(tmdb_id=tmdb_id, media_type=media_type)


class NextFindQuotaTool(MoviePilotTool):
    """查询额度工具。"""
    name: str = "nextfind_quota"
    description: str = "查询 NextFind 当前各频道剩余积分、API 次数等额度状态。"
    args_schema: Type[BaseModel] = NextFindQuotaToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在查询 NextFind 额度与积分"

    async def run(self, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_quota()


class NextFindHistoryTool(MoviePilotTool):
    """查询转存历史工具。"""
    name: str = "nextfind_history"
    description: str = "查询 NextFind 的转存/入库历史记录。"
    args_schema: Type[BaseModel] = NextFindHistoryToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在查询 NextFind 转存历史"

    async def run(self, page: int = 1, page_size: int = 20, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_history(page=page, page_size=page_size)


class NextFindResourcesSearchTool(MoviePilotTool):
    """搜索资源工具。"""
    name: str = "nextfind_resources_search"
    description: str = "在 NextFind 中按 TMDB ID 搜索网盘与种子资源，返回资源列表及标签、洗版权重等属性。"
    args_schema: Type[BaseModel] = NextFindResourcesSearchToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 搜索资源：tmdb_id={kwargs.get('tmdb_id', '')}"

    async def run(self, tmdb_id: str, media_type: str, season: int = None, episode: int = None, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_resources_search(
            tmdb_id=tmdb_id, media_type=media_type, season=season, episode=episode
        )


class NextFindTransferTool(MoviePilotTool):
    """转存到网盘工具。"""
    name: str = "nextfind_transfer"
    description: str = "将 NextFind 资源一键转存到网盘指定目录。"
    args_schema: Type[BaseModel] = NextFindTransferToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 转存资源：{kwargs.get('slug', '')}"

    async def run(self, slug: str, target_folder: str = None, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_transfer(slug=slug, target_folder=target_folder)


class NextFindDirectoriesTool(MoviePilotTool):
    """查询网盘目录工具。"""
    name: str = "nextfind_directories"
    description: str = "查询 NextFind 网盘指定父目录下的子文件夹列表。"
    args_schema: Type[BaseModel] = NextFindDirectoriesToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在查询 NextFind 网盘目录：cid={kwargs.get('cid', '0')}"

    async def run(self, cid: str = "0", **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_directories(cid=cid)


class NextFindLocalLibraryTool(MoviePilotTool):
    """查询本地库状态工具。"""
    name: str = "nextfind_local_library"
    description: str = "查询 NextFind 本地库状态，可按状态过滤（missing/error/duplicate）。"
    args_schema: Type[BaseModel] = NextFindLocalLibraryToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在查询 NextFind 本地库状态"

    async def run(self, status_filter: str = None, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_local_library(status_filter=status_filter)


class NextFindLogsTool(MoviePilotTool):
    """获取系统日志工具。"""
    name: str = "nextfind_logs"
    description: str = "获取 NextFind 系统日志。"
    args_schema: Type[BaseModel] = NextFindLogsToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在获取 NextFind 系统日志"

    async def run(self, lines: int = 50, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_logs(lines=lines)


class NextFindFillMissingTool(MoviePilotTool):
    """触发补缺集搜索工具。"""
    name: str = "nextfind_fill_missing"
    description: str = "触发 NextFind 补缺集搜索，推入高优搜索队列。"
    args_schema: Type[BaseModel] = NextFindFillMissingToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 补缺集：{kwargs.get('title', '')}"

    async def run(self, tmdb_id: str, media_type: str, title: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_fill_missing(tmdb_id=tmdb_id, media_type=media_type, title=title)


class NextFindIgnoredEpisodesTool(MoviePilotTool):
    """切换忽略季状态工具。"""
    name: str = "nextfind_ignored_episodes"
    description: str = "切换 NextFind 指定季的忽略状态（给不想下的季打忽略钢印）。"
    args_schema: Type[BaseModel] = NextFindIgnoredEpisodesToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在切换 NextFind 忽略季：tmdb_id={kwargs.get('tmdb_id', '')} S{kwargs.get('season', '')}"

    async def run(self, tmdb_id: str, season: int, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_ignored_episodes(tmdb_id=tmdb_id, season=season)


class NextFindShieldSearchTool(MoviePilotTool):
    """神盾模式查询工具。"""
    name: str = "nextfind_shield_search"
    description: str = "在 NextFind 中按 sha1 或 mediasource_id 或 tmdb_id 查询神盾分享码等信息。"
    args_schema: Type[BaseModel] = NextFindShieldSearchToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在通过 NextFind 神盾模式查询"

    async def run(self, sha1: str = None, mediasource_id: str = None, tmdb_id: str = None, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_shield_search(sha1=sha1, mediasource_id=mediasource_id, tmdb_id=tmdb_id)


class NextFindPreviewTool(MoviePilotTool):
    """触发探针解包工具。"""
    name: str = "nextfind_preview"
    description: str = "触发 NextFind 探针解包，提取隐藏属性、探针缓存和文件树。"
    args_schema: Type[BaseModel] = NextFindPreviewToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 触发探针解包：{kwargs.get('slug', '')}"

    async def run(self, slug: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_preview(slug=slug)


class NextFindHdhiveUnlockTool(MoviePilotTool):
    """HDHive 积分解锁工具。"""
    name: str = "nextfind_hdhive_unlock"
    description: str = "消耗积分解锁 NextFind HDHive 资源真实下载链接。"
    args_schema: Type[BaseModel] = NextFindHdhiveUnlockToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 解锁 HDHive 资源：{kwargs.get('id', '')}"

    async def run(self, id: str, media_type: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_hdhive_unlock(id=id, media_type=media_type)


class NextFindCreateDirectoryTool(MoviePilotTool):
    """创建网盘目录工具。"""
    name: str = "nextfind_create_directory"
    description: str = "在 NextFind 网盘指定位置新建文件夹。"
    args_schema: Type[BaseModel] = NextFindCreateDirectoryToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 创建目录：{kwargs.get('name', '')}"

    async def run(self, parent_cid: str, name: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_create_directory(parent_cid=parent_cid, name=name)


class NextFindDeleteEpisodeTool(MoviePilotTool):
    """静默删除指定集工具。"""
    name: str = "nextfind_delete_episode"
    description: str = "静默删除 NextFind 中指定的集，同时清理本地记录与文件。"
    args_schema: Type[BaseModel] = NextFindDeleteEpisodeToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 删除集：tmdb_id={kwargs.get('tmdb_id', '')} S{kwargs.get('season', '')}E{kwargs.get('episode', '')}"

    async def run(self, tmdb_id: str, season: int, episode: int, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_delete_episode(tmdb_id=tmdb_id, season=season, episode=episode)


class NextFindDeleteSeasonTool(MoviePilotTool):
    """静默删除整季工具。"""
    name: str = "nextfind_delete_season"
    description: str = "静默删除 NextFind 中整季，同时清理本地记录与文件。"
    args_schema: Type[BaseModel] = NextFindDeleteSeasonToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 删除整季：tmdb_id={kwargs.get('tmdb_id', '')} S{kwargs.get('season', '')}"

    async def run(self, tmdb_id: str, season: int, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_delete_season(tmdb_id=tmdb_id, season=season)


class NextFindDeleteMovieTool(MoviePilotTool):
    """静默删除电影工具。"""
    name: str = "nextfind_delete_movie"
    description: str = "静默删除 NextFind 中电影，同时清理本地记录与文件。"
    args_schema: Type[BaseModel] = NextFindDeleteMovieToolInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return f"正在通过 NextFind 删除电影：tmdb_id={kwargs.get('tmdb_id', '')}"

    async def run(self, tmdb_id: str, **kwargs) -> str:
        plugin = _get_plugin()
        if not plugin:
            return "NextFind 控制器插件未运行"
        return await plugin.tool_delete_movie(tmdb_id=tmdb_id)
