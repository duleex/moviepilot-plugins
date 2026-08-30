"""NextFind 控制器 Agent 工具输入模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class NextFindSearchToolInput(BaseModel):
    """全局搜索输入参数。"""
    query: str = Field(..., description="要搜索的影片或剧集名称")
    media_type: Optional[str] = Field(default=None, description="媒体类型，可选：剧集 / 电影")


class NextFindSubscriptionsToolInput(BaseModel):
    """获取订阅列表输入参数。"""
    pass


class NextFindSubscribeAddToolInput(BaseModel):
    """添加订阅输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
    media_type: str = Field(..., description="媒体类型，tv 或 movie")
    title: str = Field(..., description="媒体标题")


class NextFindSubscribeRemoveToolInput(BaseModel):
    """取消订阅输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
    media_type: str = Field(..., description="媒体类型，tv 或 movie")


class NextFindQuotaToolInput(BaseModel):
    """查询额度与积分输入参数。"""
    pass


class NextFindHistoryToolInput(BaseModel):
    """查询转存历史输入参数。"""
    page: Optional[int] = Field(default=1, description="页码，默认 1")
    page_size: Optional[int] = Field(default=20, description="每页数量，默认 20")


class NextFindResourcesSearchToolInput(BaseModel):
    """搜索网盘与种子资源输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
    media_type: str = Field(..., description="媒体类型，tv 或 movie")
    season: Optional[int] = Field(default=None, description="季数")
    episode: Optional[int] = Field(default=None, description="集数")


class NextFindTransferToolInput(BaseModel):
    """一键转存到网盘输入参数。"""
    slug: str = Field(..., description="资源标识，格式 nextfind://...")
    target_folder: Optional[str] = Field(default=None, description="目标目录，如 /自动分类/美剧")


class NextFindDirectoriesToolInput(BaseModel):
    """查询网盘目录输入参数。"""
    cid: Optional[str] = Field(default="0", description="父目录 ID，默认 0")


class NextFindLocalLibraryToolInput(BaseModel):
    """查询本地库状态输入参数。"""
    status_filter: Optional[str] = Field(default=None, description="状态过滤：missing / error / duplicate")


class NextFindLogsToolInput(BaseModel):
    """获取系统日志输入参数。"""
    lines: Optional[int] = Field(default=50, description="日志行数，默认 50")


class NextFindFillMissingToolInput(BaseModel):
    """触发补缺集搜索输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
    media_type: str = Field(..., description="媒体类型，tv 或 movie")
    title: str = Field(..., description="媒体标题")


class NextFindIgnoredEpisodesToolInput(BaseModel):
    """切换忽略季状态输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
    season: int = Field(..., description="季数")


class NextFindShieldSearchToolInput(BaseModel):
    """神盾模式查询输入参数。"""
    sha1: Optional[str] = Field(default=None, description="SHA1 值")
    mediasource_id: Optional[str] = Field(default=None, description="媒体源 ID")
    tmdb_id: Optional[str] = Field(default=None, description="TMDB ID")


class NextFindPreviewToolInput(BaseModel):
    """触发探针解包输入参数。"""
    slug: str = Field(..., description="资源标识，格式 nextfind://...")


class NextFindHdhiveUnlockToolInput(BaseModel):
    """HDHive 积分解锁输入参数。"""
    id: str = Field(..., description="资源 ID")
    media_type: str = Field(..., description="媒体类型，movie 或 tv")


class NextFindCreateDirectoryToolInput(BaseModel):
    """创建网盘目录输入参数。"""
    parent_cid: str = Field(..., description="父目录 ID")
    name: str = Field(..., description="新文件夹名称")


class NextFindDeleteEpisodeToolInput(BaseModel):
    """静默删除指定集输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
    season: int = Field(..., description="季数")
    episode: int = Field(..., description="集数")


class NextFindDeleteSeasonToolInput(BaseModel):
    """静默删除整季输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
    season: int = Field(..., description="季数")


class NextFindDeleteMovieToolInput(BaseModel):
    """静默删除电影输入参数。"""
    tmdb_id: str = Field(..., description="TMDB ID")
