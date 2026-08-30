"""NF 订阅管理插件（缺集监测 + 兜底订阅，2026-08-28 合并）。

职责（合并 NfFallbackSubscribe 后唯一 NF 订阅插件）：
1. 缺集监测：扫描全部 MP 订阅缺集，按播出日历过滤已播出集，NF 有资源则拆包转存补全（ed2k/115）；NF 没有就算了，不触发 PT。
2. 洗版：对 best_version=1 的订阅对比库中版本与 NF 资源质量，更高则转存（多版本共存）。
3. 兜底订阅：轮询 NextFind 订阅列表，对 NF 侧失败次数高或缺集的订阅由 MoviePilot 创建订阅（可先搜 NF 转存，无资源才 PT）。
"""

import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain, build_subscribe_meta
from app.core.event import eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Event, Response
from app.schemas.types import EventType, MediaType
from app.utils.http import RequestUtils


class NfMissingMonitor(_PluginBase):
    """NF 订阅管理：缺集监测（NF 补全）+ 洗版 + NF 订阅列表兜底（PT）。"""

    plugin_name = "缺集监测"
    plugin_desc = "定时监测 MP 订阅缺集，用 NextFind 搜索补全（支持 ed2k/115 拆包转存）并可选洗版；同时兜底 NextFind 订阅列表，NF 找不到的资源由 MoviePilot 创建订阅搜索下载。"
    plugin_icon = "nfmissingmonitor.png"
    plugin_version = "1.0.0"
    plugin_label = "订阅,资源管理,智能体"
    plugin_author = "local"
    plugin_config_prefix = "nfmissingmonitor_"
    plugin_order = 100
    auth_level = 1

    _enabled = False
    _api_url = ""
    _api_key = ""
    _interval = 120
    _pt_after_days = 3
    # 日历过滤：只处理已到播出时间的缺集（未播出集不搜 NF，避免提前白查）
    _calendar_filter = True
    # 兜底订阅配置（合并自 NfFallbackSubscribe）
    _fail_threshold = 3
    _filter_groups_tv = ""
    _filter_groups_movie = ""
    _search_now = True
    _search_nf_first = True
    # 兜底检查间隔（分钟），interval 触发器显式传 seconds，避免 APScheduler 0 间隔退化为每秒触发
    _check_interval = 30
    # 内存去重缓存（已转存 key 集合），init 时从 plugindata 加载
    _nf_done = set()
    # NF 无资源订阅缓存：{tmdb_id: timestamp}，记录首次确认无资源的时间（确认期内不重复查询）
    _no_resource_cache = {}
    # 洗版规则组 6 级打分（与 MP 洗版规则一致，从高到低）
    _wash_levels = [
        {"name": "REMUX 4K", "score": 600, "match": lambda q: q.get("remux") and q.get("reso") == 2160},
        {"name": "高码杜比/HDR 4K", "score": 500, "match": lambda q: q.get("hdr") and q.get("reso") == 2160},
        {"name": "杜比/HDR 4K", "score": 400, "match": lambda q: q.get("hdr") and q.get("reso") == 2160},
        {"name": "高码 4K", "score": 300, "match": lambda q: q.get("hq") and q.get("reso") == 2160},
        {"name": "4K", "score": 200, "match": lambda q: q.get("reso") == 2160},
        {"name": "1080P", "score": 100, "match": lambda q: q.get("reso") == 1080},
    ]

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        # 手动搜索缺集防重入锁
        self._check_lock = threading.Lock()
        self._enabled = False
        self._api_url = ""
        self._api_key = ""
        self._interval = 120
        self._pt_after_days = 3
        self._calendar_filter = True
        self._fail_threshold = 3
        self._filter_groups_tv = ""
        self._filter_groups_movie = ""
        self._search_now = True
        self._search_nf_first = True
        self._check_interval = 30
        self._wash_enabled = False
        self._wash_rule_group = "洗版规则"
        # 迁移旧 NfFallbackSubscribe 插件数据（去重记录 + 订阅历史）
        self._migrate_legacy_data()
        self._nf_done = set((self.get_data("nf_transferred") or {}).keys())
        self._no_resource_cache = self.get_data("nf_no_resource") or {}
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._api_url = str(config.get("api_url") or "").rstrip("/")
        self._api_key = str(config.get("api_key") or "")
        try:
            self._interval = max(5, int(config.get("interval") or 120))
        except (TypeError, ValueError):
            self._interval = 120
        try:
            self._pt_after_days = max(1, int(config.get("pt_after_days") or 3))
        except (TypeError, ValueError):
            self._pt_after_days = 3
        self._calendar_filter = bool(config.get("calendar_filter", True))
        try:
            self._fail_threshold = int(config.get("fail_threshold") or 3)
        except (TypeError, ValueError):
            self._fail_threshold = 3
        self._filter_groups_tv = str(config.get("filter_groups_tv") or "").strip()
        self._filter_groups_movie = str(config.get("filter_groups_movie") or "").strip()
        self._search_now = bool(config.get("search_now", True))
        self._search_nf_first = bool(config.get("search_nf_first", True))
        try:
            self._check_interval = max(5, int(config.get("check_interval") or 30))
        except (TypeError, ValueError):
            self._check_interval = 30
        self._wash_enabled = bool(config.get("wash_enabled"))
        self._wash_rule_group = str(config.get("wash_rule_group") or "洗版规则").strip()

    def _migrate_legacy_data(self) -> None:
        """迁移旧 NfFallbackSubscribe 插件数据：去重记录（nf_transferred）与订阅历史（subscribe_history）。"""
        try:
            legacy_done = self.get_data("nf_transferred", plugin_id="NfFallbackSubscribe") or {}
            cur_done = self.get_data("nf_transferred") or {}
            if legacy_done:
                merged = {**legacy_done, **cur_done}
                self.save_data("nf_transferred", merged)
                logger.info(f"已迁移旧插件去重记录 {len(merged)} 条（含存量 {len(legacy_done)} 条）")
            legacy_hist = self.get_data("subscribe_history", plugin_id="NfFallbackSubscribe") or {}
            if legacy_hist and not self.get_data("subscribe_history"):
                self.save_data("subscribe_history", legacy_hist)
                logger.info(f"已迁移旧插件订阅历史 {len(legacy_hist)} 条")
        except Exception as err:
            logger.error(f"迁移旧插件数据失败：{err}")

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return [
            {
                "cmd": "/nfmissing",
                "event": EventType.PluginAction,
                "desc": "手动触发缺集监测检查",
                "category": "订阅",
                "data": {"action": "check"}
            },
            {
                "cmd": "/nffallback",
                "event": EventType.PluginAction,
                "desc": "手动触发 NF 兜底订阅检查",
                "category": "订阅",
                "data": {"action": "fallback"}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return [
            {
                "path": "/check_missing",
                "endpoint": self.api_check_missing,
                "methods": ["POST"],
                "summary": "手动搜索缺集",
                "description": "异步触发缺集监测检查：扫描订阅缺集并搜索 NF 转存",
                "auth": "bear",
            },
            {
                "path": "/search_sub",
                "endpoint": self.api_search_sub,
                "methods": ["POST"],
                "summary": "搜索单个订阅缺集",
                "description": "对指定订阅单独执行缺集 NF 搜索补全",
                "auth": "bear",
            }
        ]

    def api_check_missing(self) -> Response:
        """异步触发缺集监测检查（详情页「搜索缺集」按钮调用）。"""
        if not self._check_lock.acquire(blocking=False):
            return Response(success=False, message="缺集监测正在运行中，请稍候")
        try:
            def _run():
                try:
                    self.check_missing()
                except Exception as err:
                    logger.error(f"后台缺集监测异常：{err}")
                finally:
                    self._check_lock.release()
            threading.Thread(target=_run, daemon=True).start()
            return Response(success=True, message="已开始搜索缺集，稍后刷新页面查看结果")
        except Exception as err:
            self._check_lock.release()
            logger.error(f"启动缺集监测失败：{err}")
            return Response(success=False, message=f"启动失败：{err}")

    def api_search_sub(self, payload: dict) -> Response:
        """异步对单个订阅执行缺集 NF 搜索补全（详情页卡片「搜索」按钮调用）。"""
        sid = (payload or {}).get("sid")
        if not sid:
            return Response(success=False, message="缺少订阅 ID")
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return Response(success=False, message=f"无效的订阅 ID：{sid}")
        sub = SubscribeOper().get(sid)
        if not sub:
            return Response(success=False, message=f"订阅不存在：{sid}")
        if not self._check_lock.acquire(blocking=False):
            return Response(success=False, message="已有搜索任务运行中，请稍候")
        try:
            def _run():
                try:
                    logger.info(f"开始单独搜索订阅缺集：{sub.name} (sid={sid})")
                    result = self._nf_complete_for_subscribe(sub)
                    logger.info(f"单订阅搜索完成：{sub.name} (sid={sid}) 结果={result}")
                except Exception as err:
                    logger.error(f"单订阅搜索异常：{sub.name} - {err}")
                finally:
                    self._check_lock.release()
            threading.Thread(target=_run, daemon=True).start()
            return Response(success=True, message=f"已开始搜索「{sub.name}」缺集")
        except Exception as err:
            self._check_lock.release()
            logger.error(f"启动单订阅搜索失败：{err}")
            return Response(success=False, message=f"启动失败：{err}")

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "api_url",
                            "label": "NextFind API 地址",
                            "placeholder": "https://your-server.example.com/api/openapi"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "api_key",
                            "label": "NextFind API Key",
                            "type": "password"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "interval",
                            "label": "缺集监测间隔（分钟，最小5）",
                            "placeholder": "120"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "pt_after_days",
                            "label": "NF 无资源确认天数（满 N 天后转 PT 下载）",
                            "placeholder": "3"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "calendar_filter",
                            "label": "日历过滤（只搜已到播出时间的缺集）",
                            "hint": "开启后，按 TMDB 播出日历判断，未播出集不去 NF 搜索；无日历数据的剧回退为全部缺集"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "wash_enabled",
                            "label": "启用 NF 洗版（替换库中低版本）"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "wash_rule_group",
                            "label": "洗版规则组名称",
                            "placeholder": "洗版规则"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "fail_threshold",
                            "label": "NF 订阅失败次数阈值（兜底判定）",
                            "placeholder": "3"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "filter_groups_tv",
                            "label": "电视剧过滤规则组（兜底建订阅用，留空用系统默认）",
                            "placeholder": "如：电视剧下载优先级"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "filter_groups_movie",
                            "label": "电影过滤规则组（兜底建订阅用，留空用系统默认）",
                            "placeholder": "如：电影下载优先级"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "search_now",
                            "label": "兜底建订阅后立即搜索下载"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "search_nf_first",
                            "label": "兜底前先搜 NF 资源（TG频道/网盘）再启 PT",
                            "hint": "开启后，兜底前先搜索 NextFind 网盘/TG 资源，有则自动转存，确认无资源才走 PT"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "check_interval",
                            "label": "兜底检查间隔（分钟）",
                            "placeholder": "30"
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "api_url": "https://your-server.example.com/api/openapi",
            "api_key": "",
            "interval": 120,
            "pt_after_days": 3,
            "calendar_filter": True,
            "wash_enabled": False,
            "wash_rule_group": "洗版规则",
            "fail_threshold": 3,
            "filter_groups_tv": "",
            "filter_groups_movie": "",
            "search_now": True,
            "search_nf_first": True,
            "check_interval": 30
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面：缺集监测/兜底状态 + NF 订阅海报卡片（含订阅时间与连载进度）。"""
        if not self._enabled:
            return None
        # 补录存量订阅（filter_groups 匹配）
        self._backfill_history()
        history = self.get_data("subscribe_history") or {}
        # 顶部状态说明 + 搜索缺集按钮
        status_nodes = [
            {
                "component": "div",
                "props": {"class": "d-flex justify-space-between align-center ga-2 mb-2"},
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "class": "flex-grow-1",
                            "text": (
                                f"缺集监测：{self._interval} 分钟/次"
                                + ("（日历过滤，只搜已播出缺集）" if self._calendar_filter else "")
                                + f"；NF 无资源满 {self._pt_after_days} 天转 PT"
                                + ("；洗版已启用" if self._wash_enabled else "")
                                + f"；兜底检查：{self._check_interval} 分钟/次（失败≥{self._fail_threshold} 或缺集）"
                            )
                        }
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "tonal",
                            "prepend-icon": "mdi-magnify",
                            "size": "small",
                            "class": "flex-shrink-0"
                        },
                        "text": "搜索缺集",
                        "events": {
                            "click": {
                                "api": "plugin/NfMissingMonitor/check_missing",
                                "method": "POST",
                                "params": {}
                            }
                        }
                    }
                ]
            }
        ]
        if not history:
            return status_nodes + [
                {
                    "component": "div",
                    "text": "暂无 NF 兜底订阅记录",
                    "props": {
                        "class": "text-center pa-6 text-medium-emphasis"
                    }
                }
            ]
        # 订阅表实时数据（一次取全量，按 id 建索引）
        subscribe_map = {}
        try:
            subscribe_map = {str(sub.id): sub for sub in (SubscribeOper().list() or [])}
        except Exception as err:
            logger.error(f"查询订阅列表失败：{err}")
        # 媒体库实际入库状态（实时查 Emby，避免订阅表 lack_episode 陈旧）
        lib_state = self._build_library_state(list(subscribe_map.values()))

        def safe_text(value, default: str = "—") -> str:
            """统一处理空值，避免页面直接显示 None。"""
            if value is None:
                return default
            text = str(value).strip()
            if not text or text.lower() == "none":
                return default
            return text

        def tmdb_href(media_type: str, tmdb_id) -> str:
            """根据媒体类型生成 TMDB 链接，缺少 ID 时返回空串。"""
            clean_id = safe_text(tmdb_id, "")
            if not clean_id:
                return ""
            if media_type == "tv":
                return f"https://www.themoviedb.org/tv/{clean_id}"
            return f"https://www.themoviedb.org/movie/{clean_id}"

        def progress_text(media_type: str, state: str, total, lack, season) -> str:
            """生成连载进度文本：电视剧按已下载集数，电影按订阅状态。"""
            if state == "REMOVED":
                return "已完结" if media_type == "tv" else "已完成"
            if media_type == "movie":
                if int(total or 0) > 0 and int(lack or 0) <= 0:
                    return "已入库"
                return "已订阅"
            total_i = int(total or 0)
            lack_i = int(lack or 0)
            if total_i <= 0:
                return "连载中"
            done = total_i - lack_i
            if done >= total_i:
                return f"已全 {total_i} 集"
            if done <= 0:
                return f"尚未下载 · 全 {total_i} 集"
            if season:
                return f"连载至 S{int(season):02d}E{done:02d} · 全 {total_i} 集"
            return f"连载至 {done}/{total_i} 集"

        state_map = {
            "R": "订阅中",
            "S": "已暂停",
            "P": "待定",
            "REMOVED": "已完成"
        }
        state_color = {
            "R": "teal",
            "S": "grey",
            "P": "orange",
            "REMOVED": "deep-purple"
        }

        # 组装条目（按订阅时间降序）
        items = []
        for sid, info in sorted(
            history.items(), key=lambda kv: kv[1].get("time") or "", reverse=True
        ):
            sub = subscribe_map.get(str(sid))
            media_type = info.get("media_type") or (
                "tv" if sub and sub.type == "电视剧" else "movie"
            )
            state = sub.state if sub else "REMOVED"
            total = sub.total_episode if sub else 0
            lack = sub.lack_episode if sub else 0
            season = info.get("season") if info.get("season") is not None else (sub.season if sub else None)
            poster = safe_text(sub.poster, "") if sub else ""
            # 媒体库实际入库状态（优先实时，未命中回退订阅表字段）
            lib_tmdb = str(info.get("tmdbid") or (sub.tmdbid if sub else ""))
            lib_info = lib_state.get(lib_tmdb)
            if lib_info is not None:
                if media_type == "movie":
                    if lib_info.get("movie"):
                        progress = "已入库"
                    else:
                        progress = "未入库"
                else:
                    season_key = int(season) if season else 1
                    eps_in_lib = (lib_info.get("tv") or {}).get(season_key, set())
                    done = len(eps_in_lib)
                    total_i = int(total or 0)
                    if total_i and done >= total_i:
                        progress = f"已全 {total_i} 集"
                    elif done:
                        if total_i:
                            progress = f"已更新 {done}/{total_i} 集"
                        else:
                            progress = f"已下载 {done} 集"
                    else:
                        # 订阅季无集：检查该剧其他季（库中季号可能与订阅季不一致）
                        all_tv = lib_info.get("tv") or {}
                        other_eps = {
                            s: len(e) for s, e in all_tv.items()
                            if s != season_key and e
                        }
                        if other_eps:
                            desc = "、".join(
                                f"S{s}({n}集)" for s, n in sorted(other_eps.items())
                            )
                            progress = f"订阅季无 · 库中 {desc}"
                        else:
                            progress = f"尚未下载 · 全 {total_i} 集" if total_i else "连载中"
            else:
                progress = progress_text(media_type, state, total, lack, season)
            items.append({
                "sid": sid,
                "title": safe_text(info.get("title") or (sub.name if sub else ""), "未命名"),
                "media_type": "电影" if media_type == "movie" else "电视剧",
                "state": state,
                "state_text": state_map.get(state, "未知"),
                "state_color": state_color.get(state, "grey"),
                "season": season,
                "progress": progress,
                "time": safe_text(info.get("time") or (sub.date if sub else ""), "未知时间"),
                "poster": poster,
                "tmdb_href": tmdb_href(media_type, info.get("tmdbid") or (sub.tmdbid if sub else None)),
            })

        movie_count = sum(1 for item in items if item.get("media_type") == "电影")
        tv_count = sum(1 for item in items if item.get("media_type") == "电视剧")
        missing_count = sum(
            1 for item in items
            if item.get("state") != "REMOVED"
            and "已全" not in item.get("progress")
            and "已入库" not in item.get("progress")
            and "已订阅" not in item.get("progress")
            and "连载中" != item.get("progress")
        )
        full_count = sum(1 for item in items if "已全" in item.get("progress", ""))
        movie_missing = sum(
            1 for item in items
            if item.get("media_type") == "电影" and "未入库" in item.get("progress", "")
        )
        # 资源状态速查栏
        status_bar = [
            {
                "component": "VChipGroup",
                "props": {"class": "mb-2"},
                "content": [
                    {"component": "VChip", "props": {"variant": "tonal", "color": "primary"}, "text": f"全部 {len(items)}"},
                    {"component": "VChip", "props": {"variant": "tonal", "color": "indigo"}, "text": f"电视剧 {tv_count}"},
                    {"component": "VChip", "props": {"variant": "tonal", "color": "deep-orange"}, "text": f"电影 {movie_count}"},
                    {"component": "VChip", "props": {"variant": "tonal", "color": "orange"}, "text": f"缺集中 {missing_count}"},
                    {"component": "VChip", "props": {"variant": "tonal", "color": "teal"}, "text": f"已全 {full_count}"},
                    {"component": "VChip", "props": {"variant": "tonal", "color": "error"}, "text": f"电影未入库 {movie_missing}"},
                ]
            }
        ]

        def stat_card(title: str, value: str, color: str) -> dict:
            """顶部统计卡片，帮助快速掌握订阅规模。"""
            return {
                "component": "VCol",
                "props": {
                    "cols": 12,
                    "md": 3
                },
                "content": [
                    {
                        "component": "VCard",
                        "props": {
                            "variant": "tonal",
                            "color": color,
                            "class": "pa-2"
                        },
                        "content": [
                            {
                                "component": "VCardSubtitle",
                                "props": {
                                    "class": "pb-0"
                                },
                                "text": title
                            },
                            {
                                "component": "VCardTitle",
                                "props": {
                                    "class": "text-h6 pt-1"
                                },
                                "text": value
                            }
                        ]
                    }
                ]
            }

        def subscribe_card(item: dict) -> dict:
            """订阅卡片：海报 + 标题 + 状态 + 连载进度 + 订阅时间。"""
            title_node = {
                "component": "a" if item.get("tmdb_href") else "span",
                "props": {
                    "href": item.get("tmdb_href"),
                    "target": "_blank",
                    "class": "text-decoration-none"
                } if item.get("tmdb_href") else {},
                "text": item.get("title")
            }
            poster_node = {
                "component": "VImg",
                "props": {
                    "src": item.get("poster"),
                    "height": 132,
                    "width": 88,
                    "aspect-ratio": "2/3",
                    "class": "rounded flex-shrink-0",
                    "cover": True
                }
            } if item.get("poster") else {
                "component": "div",
                "props": {
                    "class": "d-flex align-center justify-center rounded bg-grey-lighten-3 text-caption text-medium-emphasis flex-shrink-0",
                    "style": {
                        "width": "88px",
                        "height": "132px"
                    }
                },
                "text": "无海报"
            }
            return {
                "component": "VCard",
                "props": {
                    "variant": "outlined",
                    "class": "h-100"
                },
                "content": [
                    {
                        "component": "div",
                        "props": {
                            "class": "d-flex flex-nowrap ga-3 pa-3"
                        },
                        "content": [
                            poster_node,
                            {
                                "component": "div",
                                "props": {
                                    "class": "min-w-0 flex-grow-1"
                                },
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-subtitle-2 font-weight-bold text-truncate mb-2"
                                        },
                                        "content": [title_node]
                                    },
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "d-flex flex-wrap ga-1 mb-2"
                                        },
                                        "content": [
                                            {
                                                "component": "VChip",
                                                "props": {
                                                    "size": "x-small",
                                                    "variant": "tonal",
                                                    "color": "indigo"
                                                },
                                                "text": item.get("media_type")
                                            },
                                            {
                                                "component": "VChip",
                                                "props": {
                                                    "size": "x-small",
                                                    "variant": "tonal",
                                                    "color": item.get("state_color")
                                                },
                                                "text": item.get("state_text")
                                            }
                                        ]
                                    },
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-body-2 mb-1"
                                        },
                                        "text": item.get("progress")
                                    },
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-caption text-medium-emphasis"
                                        },
                                        "text": f"订阅时间：{item.get('time')}"
                                    }
                                ]
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-magnify",
                                    "class": "align-self-center flex-shrink-0"
                                },
                                "text": "搜索",
                                "events": {
                                    "click": {
                                        "api": "plugin/NfMissingMonitor/search_sub",
                                        "method": "POST",
                                        "params": {"sid": item.get("sid")}
                                    }
                                }
                            }
                        ]
                    }
                ]
            }

        page_size = 8
        pages = [items[index:index + page_size] for index in range(0, len(items), page_size)]
        page_items = []
        for page_index, page_records in enumerate(pages, start=1):
            page_items.append({
                "component": "VWindowItem",
                "content": [
                    {
                        "component": "div",
                        "props": {
                            "class": "d-flex justify-space-between align-center mb-2 text-caption text-medium-emphasis"
                        },
                        "content": [
                            {
                                "component": "span",
                                "text": f"第 {page_index} / {len(pages)} 页"
                            },
                            {
                                "component": "span",
                                "text": f"本页 {len(page_records)} 条"
                            }
                        ]
                    },
                    {
                        "component": "div",
                        "props": {
                            "class": "grid gap-3 grid-info-card"
                        },
                        "content": [subscribe_card(item) for item in page_records]
                    }
                ]
            })

        return status_nodes + status_bar + [
            {
                "component": "VRow",
                "props": {
                    "class": "mb-2"
                },
                "content": [
                    stat_card("NF 订阅", f"{len(items)} 条", "primary"),
                    stat_card("电影", f"{movie_count} 条", "deep-orange"),
                    stat_card("电视剧", f"{tv_count} 条", "indigo"),
                    stat_card("连载中/缺集中", f"{missing_count} 条", "teal"),
                ]
            },
            {
                "component": "VWindow",
                "props": {
                    "show-arrows": "hover"
                },
                "content": page_items
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """返回插件定时服务。"""
        return [
            {
                "id": "NfMissingMonitorCheck",
                "name": "缺集监测检查",
                "trigger": "interval",
                "func": self.check_missing,
                # 显式传 seconds，避免 APScheduler 0 间隔退化为每秒触发（一直连跑）
                "kwargs": {"seconds": self._interval * 60}
            },
            {
                "id": "NfFallbackSubscribeCheck",
                "name": "NF 兜底订阅检查",
                "trigger": "interval",
                "func": self.check_fallback,
                "kwargs": {"seconds": self._check_interval * 60}
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None

    # ==================== 核心逻辑 ====================

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event = None) -> None:
        """处理插件命令。"""
        if event:
            event_data = event.event_data
            if not event_data:
                return
            action = event_data.get("action")
            if action == "check":
                logger.info("收到命令，开始缺集监测检查 ...")
                self.check_missing()
            elif action == "fallback":
                logger.info("收到命令，开始 NF 兜底订阅检查 ...")
                self.check_fallback()

    @eventmanager.register(EventType.SubscribeAdded)
    def handle_subscribe_added(self, event: Event = None) -> None:
        """新订阅默认置为暂停，等待 NF 确认期，防止系统订阅搜索抢跑 PT。

        策略：所有订阅先 NF 搜索，NF 无资源满 N 天后才恢复启用转 PT 下载。
        监听添加订阅事件，将新订阅（如猫眼榜单）立即置为 S，由本插件统一调度。
        """
        if not self._enabled:
            return
        if not event or not event.event_data:
            return
        try:
            subscribe_id = None
            subscribe_info = event.event_data.get("subscribe_info") or {}
            subscribe_id = subscribe_info.get("id") or event.event_data.get("subscribe_id")
            if not subscribe_id:
                # 兜底：从 event_data 各字段查找 id
                for key in ("id", "sid"):
                    if event.event_data.get(key):
                        subscribe_id = event.event_data.get(key)
                        break
            if not subscribe_id:
                logger.debug("订阅添加事件缺少订阅 ID，跳过暂停处理")
                return
            # 置为暂停状态，由缺集监测统一走 NF 优先流程
            SubscribeOper().update(int(subscribe_id), {"state": "S"})
            name = subscribe_info.get("name") or subscribe_id
            logger.info(f"新订阅已置为暂停等待 NF 确认：{name} (sid={subscribe_id})")
        except Exception as err:
            logger.error(f"处理订阅添加事件异常：{err}")

    @eventmanager.register(EventType.SubscribeComplete)
    def handle_subscribe_complete(self, event: Event = None) -> None:
        """MP 订阅完成后，自动取消 NF 对应订阅，避免重复。"""
        if not self._enabled:
            return
        if not event or not event.event_data:
            return
        try:
            event_data = event.event_data
            # 优先从 mediainfo 提取
            mediainfo = event_data.get("mediainfo") or {}
            tmdb_id = mediainfo.get("tmdb_id")
            media_type = mediainfo.get("type")
            # 兜底从 subscribe_info 提取
            if not tmdb_id:
                subscribe_info = event_data.get("subscribe_info") or {}
                tmdb_id = subscribe_info.get("tmdbid") or subscribe_info.get("tmdb_id")
            if not media_type:
                subscribe_info = event_data.get("subscribe_info") or {}
                media_type = subscribe_info.get("media_type")
            if not tmdb_id:
                logger.debug("订阅完成事件缺少 tmdb_id，跳过 NF 取消订阅")
                return
            # 转换 media_type：中文 -> tv/movie
            if media_type in ("电视剧", "tv", "TV"):
                nf_type = "tv"
            elif media_type in ("电影", "movie", "MOVIE"):
                nf_type = "movie"
            else:
                nf_type = None
            if not nf_type:
                logger.debug(f"订阅完成事件无法识别媒体类型：{media_type}，跳过 NF 取消订阅")
                return
            # 取消 NF 订阅
            self._cancel_nf_subscribe(tmdb_id=str(tmdb_id), media_type=nf_type)
        except Exception as err:
            logger.error(f"处理订阅完成事件异常：{err}")

    # ==================== 缺集监测 ====================

    def check_missing(self) -> None:
        """监测全部 MP 订阅缺集，NF 有资源则转存补全，无资源跳过（不触发 PT）。"""
        if not self._enabled:
            return
        if not self._api_url or not self._api_key:
            logger.warn("缺集监测未配置 API 地址或 API Key")
            return
        logger.info("开始缺集监测检查 ...")
        try:
            # 扫描全部订阅（含暂停 S 状态），暂停订阅不参与系统 PT 搜索，仅由本插件 NF 补全
            subscribes = SubscribeOper().list() or []
            logger.info(f"订阅总数：{len(subscribes)}")
            transferred = 0
            no_nf = 0
            complete = 0
            for sub in subscribes:
                if sub.type not in (MediaType.TV.value, MediaType.MOVIE.value):
                    continue
                try:
                    result = self._nf_complete_for_subscribe(sub)
                    if result == "transferred":
                        transferred += 1
                    elif result == "no_nf_resource":
                        no_nf += 1
                    elif result == "complete":
                        complete += 1
                except Exception as err:
                    logger.error(f"订阅缺集监测异常：{sub.name} - {err}")
            logger.info(
                f"缺集监测完成：NF转存补齐 {transferred}，NF无资源跳过 {no_nf}，无缺集 {complete}"
            )
            # 洗版检查（仅对已开启 best_version 的订阅）
            if self._wash_enabled:
                self.check_wash(subscribes)
        except Exception as err:
            logger.error(f"缺集监测异常：{err}")

    def _nf_complete_for_subscribe(self, sub) -> str:
        """对单个订阅：缺集 →（日历过滤已播出集）→ NF 搜索 → 有资源拆包转存补全；无资源满 N 天后转 PT。"""
        tmdb_id = sub.tmdbid
        if not tmdb_id:
            return "no_nf_resource"
        missing = self._resolve_subscribe_missing(sub)
        if not missing:
            return "complete"
        media_type = "tv" if sub.type == MediaType.TV.value else "movie"
        # 日历过滤：只处理已到播出时间的缺集（未播出集不去搜 NF）
        if self._calendar_filter and media_type == "tv" and sub.season:
            aired = self._get_aired_episodes(int(tmdb_id), int(sub.season))
            if aired is not None and aired:
                filtered = {}
                for sea, eps in missing.items():
                    if sea != sub.season:
                        continue
                    need = [ep for ep in eps if ep in aired]
                    if need:
                        filtered[sea] = need
                missing = filtered
                if not missing:
                    logger.info(f"日历过滤后无待搜集（未到播出时间）：{sub.name} (tmdb={tmdb_id})")
                    return "complete"
        # NF 无资源确认：首次记录时间，满 pt_after_days 天后恢复订阅启用（转 PT 下载）
        now = self._now()
        no_res_since = self._no_resource_cache.get(str(tmdb_id))
        if no_res_since and (now - float(no_res_since)) >= self._pt_after_days * 24 * 3600:
            # 已连续 N 天无资源：恢复订阅为 R，由系统订阅刷新走 PT 搜索下载
            if sub.state in ("S", "N"):
                try:
                    SubscribeOper().update(sub.id, {"state": "R"})
                    logger.info(
                        f"NF 已连续 {self._pt_after_days} 天无资源，恢复订阅启用转 PT 下载："
                        f"{sub.name} (tmdb={tmdb_id})"
                    )
                except Exception as err:
                    logger.error(f"恢复订阅状态失败：{sub.name} - {err}")
            return "no_nf_resource"
        # 未到确认期，查询 NF（有缓存且未满确认期则跳过查询）
        if no_res_since:
            logger.info(
                f"NF 无资源确认中（{int((now - float(no_res_since)) / 3600)} 小时/"
                f"{self._pt_after_days * 24} 小时）：{sub.name} (tmdb={tmdb_id})"
            )
            return "no_nf_resource"
        resources = self._check_nf_resource(int(tmdb_id), media_type)
        if resources is None:
            # 查询失败（网络/接口异常），不写缓存，下轮重试
            logger.warn(f"NF 资源查询失败，本轮跳过不缓存：{sub.name} (tmdb={tmdb_id})")
            return "no_nf_resource"
        if not resources:
            # NF 无资源：记录首次确认时间
            self._no_resource_cache[str(tmdb_id)] = now
            self.save_data("nf_no_resource", self._no_resource_cache)
            logger.info(
                f"NF 无资源，开始 {self._pt_after_days} 天确认期：{sub.name} (tmdb={tmdb_id})"
            )
            return "no_nf_resource"
        if str(tmdb_id) in self._no_resource_cache:
            del self._no_resource_cache[str(tmdb_id)]
            self.save_data("nf_no_resource", self._no_resource_cache)
        # NF 有资源：拆包转存缺集
        if media_type == "movie":
            best = self._pick_best_resource(resources)
            if best and self._transfer_nf_resource(best):
                self._mark_transferred(tmdb_id, "movie", 0, 0)
                logger.info(f"电影 NF 转存补齐：{sub.name} - {best.get('slug')}")
                return "transferred"
            return "no_nf_resource"
        done = False
        hdhive_failed = False
        for resource in resources:
            slug = resource.get("slug") or ""
            if slug.startswith("hdhive://") and hdhive_failed:
                continue
            preview_files = self._preview_nf_resource(slug)
            if preview_files is None:
                if slug.startswith("hdhive://"):
                    hdhive_failed = True
                continue
            for season, episodes in list(missing.items()):
                for ep in list(episodes):
                    key = self._transferred_key(tmdb_id, "tv", season, ep)
                    if key in self._nf_done:
                        continue
                    item = self._find_episode_item(preview_files, season, ep)
                    if not item:
                        continue
                    if self._transfer_single_item(item, media_type):
                        self._mark_transferred(tmdb_id, "tv", season, ep)
                        done = True
                        logger.info(f"NF 拆包转存：{sub.name} S{season:02d}E{ep:02d} - {item.get('name')}")
                        episodes.remove(ep)
                        if not episodes:
                            missing.pop(season, None)
        if done:
            return "transferred"
        # 拆包未命中：非 hdhive 资源整包转存回退
        best = self._pick_best_resource(resources)
        if best and not (best.get("slug") or "").startswith("hdhive://"):
            preview_files = self._preview_nf_resource(best.get("slug") or "")
            if preview_files and self._transfer_nf_resource(best):
                for season, episodes in missing.items():
                    for ep in episodes:
                        self._mark_transferred(tmdb_id, "tv", season, ep)
                logger.info(f"电视剧 NF 整包转存：{sub.name} - {best.get('slug')}")
                return "transferred"
        return "no_nf_resource"

    # ==================== 洗版 ====================

    def check_wash(self, subscribes: List[Any]) -> None:
        """洗版检查：对 best_version=1 的订阅，对比库中版本与 NF 资源质量，更高则转存替换。"""
        logger.info("开始洗版检查 ...")
        washed = 0
        skipped = 0
        failed = 0
        for sub in subscribes:
            if sub.type not in (MediaType.TV.value, MediaType.MOVIE.value):
                continue
            if not getattr(sub, "best_version", 0):
                continue
            try:
                result = self._wash_for_subscribe(sub)
                if result == "washed":
                    washed += 1
                elif result == "no_upgrade":
                    skipped += 1
                else:
                    failed += 1
            except Exception as err:
                logger.error(f"订阅洗版异常：{sub.name} - {err}")
        logger.info(f"洗版检查完成：已洗版 {washed}，无需升级 {skipped}，失败 {failed}")

    def _wash_for_subscribe(self, sub) -> str:
        """对单个订阅执行洗版：库中版本质量 < NF 最优资源质量 → 转存新版本（保留旧版多版本）。"""
        tmdb_id = sub.tmdbid
        if not tmdb_id:
            return "no_upgrade"
        media_type = "tv" if sub.type == MediaType.TV.value else "movie"
        lib_quality = self._get_library_quality(int(tmdb_id), media_type, getattr(sub, "season", None))
        if not lib_quality:
            return "no_upgrade"
        resources = self._check_nf_resource(int(tmdb_id), media_type)
        if not resources:
            return "no_upgrade"
        best = self._pick_best_resource(resources)
        if not best:
            return "no_upgrade"
        if (best.get("slug") or "").startswith("hdhive://"):
            logger.debug(f"洗版最优资源为 hdhive，跳过：{sub.name} (tmdb={tmdb_id})")
            return "no_upgrade"
        lib_score = self._score_quality(lib_quality)
        nf_score = self._score_quality(self._parse_resource_quality(best))
        if nf_score <= lib_score:
            logger.info(f"库中版本已是最优，无需洗版：{sub.name} (tmdb={tmdb_id}) lib={lib_score} nf={nf_score}")
            return "no_upgrade"
        if not self._transfer_nf_resource(best, media_type=media_type):
            logger.error(f"洗版转存失败：{sub.name} (tmdb={tmdb_id})")
            return "failed"
        self._mark_transferred(tmdb_id, media_type, getattr(sub, "season", None) or 0, 0)
        logger.info(f"洗版转存完成（保留多版本）：{sub.name} (tmdb={tmdb_id}) lib={lib_score} nf={nf_score}")
        return "washed"

    def _get_library_quality(self, tmdb_id: int, media_type: str, season: Optional[int] = None) -> Optional[dict]:
        """查询媒体库中已有版本的质量（分辨率/编码/REMUX/HDR）。"""
        try:
            from app.modules.emby import EmbyModule
            em = EmbyModule()
            em.init_module()
            insts = em.get_instances()
            if not insts:
                return None
            target = str(tmdb_id)
            for inst in insts.values():
                host = inst._host
                apikey = inst._apikey
                if not host or not apikey:
                    continue
                for item_type in ("Movie", "Series", "Episode"):
                    url = f"{host}emby/Items"
                    params = {
                        "Recursive": "true",
                        "IncludeItemTypes": item_type,
                        "Fields": "MediaSources,MediaStreams,Path,ProviderIds",
                        "Limit": 50,
                        "api_key": apikey,
                    }
                    res = inst.get_data(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()))
                    if not res or res.status_code != 200:
                        continue
                    items = res.json().get("Items") or []
                    for it in items:
                        tm = (it.get("ProviderIds") or {}).get("Tmdb")
                        item_path = it.get("Path") or ""
                        path_tmdb = None
                        m = re.search(r"\{tmdb(?:id)?[-=](\d+)\}", item_path)
                        if m:
                            path_tmdb = m.group(1)
                        if str(tm) != target and path_tmdb != target:
                            continue
                        ms = (it.get("MediaSources") or [{}])[0]
                        streams = ms.get("MediaStreams") or []
                        vstreams = [s for s in streams if s.get("Type") == "Video"]
                        height = 0
                        codec = ""
                        if vstreams:
                            height = int(vstreams[0].get("Height") or 0)
                            codec = str(vstreams[0].get("Codec") or "")
                        reso = 2160 if height >= 2000 else (1080 if height >= 1000 else 0)
                        if not reso:
                            reso = self._detect_resolution_from_path(item_path)
                        up_path = item_path.upper()
                        up_codec = codec.upper()
                        quality = {
                            "path": item_path,
                            "reso": reso,
                            "remux": "REMUX" in up_path,
                            "hdr": any(k in up_path or k in up_codec for k in ("HDR", "DOLBY", "DOVI", "DV")),
                            "hq": "HEVC" in up_codec or "H265" in up_path or "X265" in up_path,
                        }
                        logger.info(f"库中版本质量：tmdb={tmdb_id} type={item_type} path={item_path} reso={reso} hdr={quality['hdr']} remux={quality['remux']}")
                        return quality
            return None
        except Exception as err:
            logger.error(f"查询库中版本质量失败：tmdb={tmdb_id} - {err}")
            return None

    def _detect_resolution_from_path(self, path: str) -> int:
        """从文件路径中检测分辨率（2160/1080/其他）。"""
        up = str(path or "").upper()
        if any(k in up for k in ("2160", "4K", "UHD")):
            return 2160
        if "1080" in up or "BLURAY" in up:
            return 1080
        return 0

    def _parse_resource_quality(self, resource: dict) -> dict:
        """解析 NF 资源质量为统一结构。"""
        reso_str = str(resource.get("video_resolution") or resource.get("resolution") or "")
        reso = 2160 if any(k in reso_str.upper() for k in ("4K", "2160", "UHD")) else (1080 if "1080" in reso_str.upper() else 0)
        src = " ".join(resource.get("source") or [])
        src = f"{src} {resource.get('source_type') or ''}".upper()
        return {
            "reso": reso,
            "remux": "REMUX" in src,
            "hdr": any(k in src for k in ("DOLBY", "HDR")),
            "hq": True,
        }

    def _score_quality(self, quality: dict) -> int:
        """按洗版规则组 6 级打分（从高到低）。"""
        if not quality:
            return 0
        for level in self._wash_levels:
            try:
                if level["match"](quality):
                    return level["score"]
            except Exception:
                continue
        return 0

    # ==================== 兜底订阅（NF 订阅列表） ====================

    def check_fallback(self) -> None:
        """执行 NF 兜底订阅检查：NF 侧失败次数高或缺集的订阅，由 MP 创建订阅搜索下载兜底。"""
        if not self._enabled:
            return
        if not self._api_url or not self._api_key:
            logger.warn("NF 兜底订阅未配置 API 地址或 API Key")
            return
        logger.info("开始执行 NF 兜底订阅检查 ...")
        try:
            # 获取 NF 订阅列表
            subscriptions = self._get_nf_subscriptions()
            if subscriptions is None:
                logger.error("获取 NF 订阅列表失败")
                return
            logger.info(f"NF 订阅总数：{len(subscriptions)}")
            # 筛选需要兜底的订阅
            fallback_items = self._filter_fallback_items(subscriptions)
            logger.info(f"需要 MP 兜底的订阅：{len(fallback_items)} 个")
            # 逐个创建 MP 订阅
            added = 0
            skipped = 0
            failed = 0
            nf_resource = 0
            for item in fallback_items:
                result = self._create_mp_subscribe(item)
                if result == "added":
                    added += 1
                elif result == "nf_resource":
                    nf_resource += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    failed += 1
            logger.info(
                f"NF 兜底订阅检查完成：新增 {added}，跳过 {skipped}，失败 {failed}，NF资源命中跳过 {nf_resource}"
            )
        except Exception as err:
            logger.error(f"NF 兜底订阅检查异常：{err}")

    def _get_nf_subscriptions(self) -> Optional[List[dict]]:
        """获取 NF 订阅列表。"""
        url = f"{self._api_url}/subscriptions"
        headers = {"X-API-Key": self._api_key}
        try:
            res = RequestUtils(headers=headers).get_res(url)
            if not res:
                logger.error("NF 订阅列表请求失败（无响应）")
                return None
            if res.status_code != 200:
                logger.error(f"NF 订阅列表请求失败：HTTP {res.status_code}")
                return None
            data = res.json()
            if data.get("status") != "success":
                logger.error(f"NF 订阅列表返回异常：{data}")
                return None
            return data.get("data") or []
        except Exception as err:
            logger.error(f"获取 NF 订阅列表异常：{err}")
            return None

    def _filter_fallback_items(self, items: List[dict]) -> List[dict]:
        """筛选需要 MP 兜底的订阅。"""
        result = []
        for item in items:
            # 只处理订阅中的
            if item.get("status") != "subscribing":
                continue
            # 判定：fail_count >= 阈值 或 缺集
            fail_count = int(item.get("fail_count") or 0)
            local_episodes = int(item.get("local_episodes") or 0)
            total_episodes = int(item.get("total_episodes") or 0)
            media_type = item.get("media_type")
            is_missing = (
                media_type == "tv"
                and total_episodes > 0
                and local_episodes < total_episodes
            )
            if fail_count >= self._fail_threshold or is_missing:
                result.append(item)
        return result

    def _create_mp_subscribe(self, item: dict) -> str:
        """为单个 NF 订阅创建 MP 订阅。"""
        tmdb_id = item.get("tmdb_id")
        title = item.get("title")
        media_type = item.get("media_type")
        year = item.get("year") or ""
        if not tmdb_id or not title:
            return "failed"
        try:
            tmdb_id_int = int(tmdb_id)
        except (TypeError, ValueError):
            logger.warn(f"无效的 TMDB ID：{tmdb_id}")
            return "failed"

        # 幂等检查：MP 是否已有对应订阅
        existing = SubscribeOper().list_by_tmdbid(tmdbid=tmdb_id_int)
        if existing:
            logger.info(f"MP 已有订阅，跳过：{title} (tmdb={tmdb_id})")
            return "skipped"

        # 幂等检查：MP 订阅历史
        if SubscribeOper().exist_history(tmdbid=tmdb_id_int):
            logger.info(f"MP 订阅历史已存在，跳过：{title} (tmdb={tmdb_id})")
            return "skipped"

        # NF 资源预检：先搜 TG 频道/网盘资源，确认无资源才走 PT 兜底
        if self._search_nf_first:
            nf_type = "tv" if media_type == "tv" else "movie"
            resources = self._check_nf_resource(tmdb_id_int, nf_type)
            if resources:
                logger.info(f"NF 资源搜索命中（TG频道/网盘有资源），跳过 PT 兜底：{title} (tmdb={tmdb_id})")
                # 自动转存最优资源到 115 转存文件夹（由 CloudLinkMonitor 入库）
                best = self._pick_best_resource(resources)
                if best:
                    transferred = self._transfer_nf_resource(best, media_type=nf_type)
                    if transferred:
                        logger.info(f"已自动转存最优资源：{title} - {best.get('slug')}")
                else:
                    logger.warn(f"NF 资源无法选择最优项，跳过转存：{title}")
                return "nf_resource"

        # 创建 MP 订阅
        mtype = MediaType.TV if media_type == "tv" else MediaType.MOVIE
        kwargs = {}
        if mtype == MediaType.TV and self._filter_groups_tv:
            kwargs["filter_groups"] = [self._filter_groups_tv]
        elif mtype == MediaType.MOVIE and self._filter_groups_movie:
            kwargs["filter_groups"] = [self._filter_groups_movie]
        try:
            sid, err_msg = SubscribeChain().add(
                title=title,
                year=year,
                mtype=mtype,
                tmdbid=tmdb_id_int,
                message=False,
                exist_ok=False,
                **kwargs,
            )
        except Exception as err:
            logger.error(f"创建 MP 订阅失败：{title} - {err}")
            return "failed"
        if not sid:
            logger.warn(f"创建 MP 订阅失败：{title} - {err_msg}")
            return "failed"
        logger.info(f"已创建 MP 兜底订阅：{title} (tmdb={tmdb_id}, sid={sid})")
        # 记录到订阅历史（详情页展示）
        self._record_subscribe(
            sid=sid,
            tmdbid=tmdb_id_int,
            title=title,
            media_type="tv" if media_type == "tv" else "movie",
            season=item.get("season"),
            year=year,
        )

        # 立即搜索下载
        if self._search_now:
            try:
                SubscribeChain().search(sid=sid, manual=True)
                logger.info(f"已触发 MP 订阅搜索：{title} (sid={sid})")
            except Exception as err:
                logger.error(f"触发 MP 订阅搜索失败：{title} - {err}")
        return "added"

    def _cancel_nf_subscribe(self, tmdb_id: str, media_type: str) -> bool:
        """调用 NF API 取消订阅。"""
        if not self._api_url or not self._api_key:
            logger.warn("NF 订阅管理未配置 API 地址或 API Key，无法取消订阅")
            return False
        url = f"{self._api_url}/subscriptions/remove"
        headers = {"X-API-Key": self._api_key}
        body = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        }
        try:
            res = RequestUtils(headers=headers).post_res(url=url, json=body)
            if not res:
                logger.error(f"取消 NF 订阅失败（无响应）：tmdb={tmdb_id} type={media_type}")
                return False
            if res.status_code != 200:
                logger.error(f"取消 NF 订阅失败：HTTP {res.status_code} tmdb={tmdb_id} type={media_type}")
                return False
            data = res.json()
            if data.get("status") != "success":
                logger.error(f"取消 NF 订阅返回异常：{data} tmdb={tmdb_id} type={media_type}")
                return False
            logger.info(f"已取消 NF 订阅：tmdb={tmdb_id} type={media_type}")
            return True
        except Exception as err:
            logger.error(f"取消 NF 订阅异常：{err} tmdb={tmdb_id} type={media_type}")
            return False

    def _record_subscribe(self, sid: int, tmdbid: int, title: str,
                          media_type: str, season=None, year: str = "") -> None:
        """记录插件创建的 MP 订阅，供详情页展示（海报/订阅时间/连载进度）。"""
        try:
            history = self.get_data("subscribe_history") or {}
            if not isinstance(history, dict):
                history = {}
            key = str(sid)
            if key not in history:
                history[key] = {
                    "tmdbid": tmdbid,
                    "title": title,
                    "media_type": media_type,
                    "season": season,
                    "year": year or "",
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.save_data("subscribe_history", history)
        except Exception as err:
            logger.error(f"记录订阅历史失败：{title} - {err}")

    def _backfill_history(self) -> None:
        """补录存量订阅：带电视剧/电影下载优先级规则组的订阅视为兜底创建。"""
        try:
            history = self.get_data("subscribe_history") or {}
            if not isinstance(history, dict):
                history = {}
            changed = False
            subs = SubscribeOper().list() or []
            for sub in subs:
                fg = sub.filter_groups or []
                if not any(g in ("电视剧下载优先级", "电影下载优先级") for g in fg):
                    continue
                key = str(sub.id)
                if key in history:
                    continue
                history[key] = {
                    "tmdbid": sub.tmdbid,
                    "title": sub.name,
                    "media_type": "tv" if sub.type == "电视剧" else "movie",
                    "season": sub.season,
                    "year": sub.year or "",
                    "time": sub.date or "",
                }
                changed = True
            if changed:
                self.save_data("subscribe_history", history)
        except Exception as err:
            logger.error(f"补录订阅历史失败：{err}")

    # ==================== 缺集计算 ====================

    def _build_library_state(self, subscribes: List[Any]) -> dict:
        """并发查询媒体库实际入库状态，返回 {tmdb_str: {"movie": bool, "tv": {season: set(eps)}}}。

        针对订阅涉及的剧/电影定向查询（Emby 库 23 万集，不能全量拉取）：
        1. Series 全量一次（<1 万条）建 tmdb -> series_id 映射；
        2. 电视剧订阅并发查 /Shows/{series_id}/Episodes 拿各季已入库集；
        3. 电影订阅并发按 AnyProviderIdEquals=tmdb 判定是否入库。
        查询失败/未命中返回空（详情页回退订阅表 lack_episode）。
        """
        state = {}
        try:
            from concurrent.futures import ThreadPoolExecutor
            from app.modules.emby import EmbyModule
            em = EmbyModule()
            em.init_module()
            insts = em.get_instances()
            if not insts:
                return state
            inst = list(insts.values())[0]
            host = inst._host
            apikey = inst._apikey
            if not host or not apikey:
                return state
            # 1. Series 全量：tmdb -> series_id 列表（ProviderIds.Tmdb 或路径 {tmdbid=}/{tmdb=} 双匹配；
            #    同一剧可能有重复 Series 条目（空条目/有集条目并存），全部保留，查询时合并取有集的）
            series_map = {}
            url = f"{host}emby/Items"
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "Series",
                "Fields": "ProviderIds,Path",
                "Limit": 10000,
                "api_key": apikey,
            }
            res = inst.get_data(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()))
            if res and res.status_code == 200:
                for it in (res.json().get("Items") or []):
                    tm = (it.get("ProviderIds") or {}).get("Tmdb")
                    if not tm:
                        m = re.search(r"\{tmdb(?:id)?[-=](\d+)\}", it.get("Path") or "")
                        if m:
                            tm = m.group(1)
                    if tm:
                        series_map.setdefault(str(tm), []).append(it.get("Id"))
            tv_subs = [s for s in subscribes if s.type == MediaType.TV.value and s.tmdbid]
            movie_subs = [s for s in subscribes if s.type == MediaType.MOVIE.value and s.tmdbid]

            def _fetch_tv(tmdb_id) -> tuple:
                """查询单部电视剧各季已入库集（合并该剧全部 Series 条目的集）。"""
                tm = str(tmdb_id)
                ids = series_map.get(tm) or []
                eps = {}
                if not ids:
                    return tm, {}
                for series_id in ids:
                    u2 = f"{host}emby/Shows/{series_id}/Episodes"
                    p2 = {
                        "Fields": "ParentIndexNumber,IndexNumber",
                        "Limit": 1000,
                        "api_key": apikey,
                    }
                    try:
                        r = inst.get_data(u2 + "?" + "&".join(f"{k}={v}" for k, v in p2.items()))
                        if r and r.status_code == 200:
                            for it in (r.json().get("Items") or []):
                                sea = it.get("ParentIndexNumber")
                                ep = it.get("IndexNumber")
                                if sea is None or ep is None:
                                    continue
                                eps.setdefault(int(sea), set()).add(int(ep))
                    except Exception as err:
                        logger.warn(f"查询剧集入库状态失败：tmdb={tm} series={series_id} - {err}")
                return tm, eps

            def _fetch_movie(tmdb_id) -> tuple:
                """查询电影是否已入库。"""
                tm = str(tmdb_id)
                u3 = f"{host}emby/Items"
                p3 = {
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie",
                    "AnyProviderIdEquals": f"tmdb|{tmdb_id}",
                    "Limit": 5,
                    "api_key": apikey,
                }
                try:
                    r = inst.get_data(u3 + "?" + "&".join(f"{k}={v}" for k, v in p3.items()))
                    return tm, bool(r and r.status_code == 200 and (r.json().get("Items") or []))
                except Exception as err:
                    logger.warn(f"查询电影入库状态失败：tmdb={tm} - {err}")
                    return tm, False

            # 2/3. 并发查询
            with ThreadPoolExecutor(max_workers=8) as ex:
                for tm, eps in ex.map(_fetch_tv, [s.tmdbid for s in tv_subs]):
                    state[tm] = {"movie": False, "tv": eps}
                for tm, ok in ex.map(_fetch_movie, [s.tmdbid for s in movie_subs]):
                    state.setdefault(tm, {"movie": False, "tv": {}})["movie"] = ok
        except Exception as err:
            logger.error(f"构建媒体库入库状态失败：{err}")
        return state

    def _get_aired_episodes(self, tmdb_id: int, season: int) -> Optional[set]:
        """获取已播出的集号集合（按 TMDB 播出日历）；查询失败返回 None，无日历数据返回空集合。"""
        try:
            from app.chain.tmdb import TmdbChain
            episodes = TmdbChain().tmdb_episodes(tmdbid=tmdb_id, season=season) or []
        except Exception as err:
            logger.warn(f"获取 TMDB 播出日历失败：tmdb={tmdb_id} S{season} - {err}")
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        aired = set()
        for ep in episodes:
            air_date = getattr(ep, "air_date", None) or ""
            ep_num = getattr(ep, "episode_number", None)
            if not air_date or not ep_num:
                continue
            if air_date <= today:
                aired.add(int(ep_num))
        return aired

    def _resolve_subscribe_missing(self, sub) -> Dict[int, List[int]]:
        """计算订阅缺集列表 {season: [episodes]}。"""
        try:
            meta = build_subscribe_meta(sub)
        except Exception as err:
            logger.error(f"构建订阅元数据失败：{sub.name} - {err}")
            return {}
        try:
            from app.chain.subscribe import _subscribe_recognize_kwargs
            mediainfo = MediaChain().recognize_media(
                meta=meta,
                mtype=meta.type,
                **_subscribe_recognize_kwargs(sub),
                cache=False,
            )
        except Exception as err:
            logger.error(f"识别订阅媒体失败：{sub.name} - {err}")
            return {}
        if not mediainfo:
            return {}
        totals = {}
        if sub.type == MediaType.TV.value and sub.season and sub.total_episode:
            totals = {sub.season: sub.total_episode}
        try:
            exist_flag, no_exists = DownloadChain().get_no_exists_info(
                meta=meta,
                mediainfo=mediainfo,
                totals=totals,
            )
        except Exception as err:
            logger.error(f"查询订阅缺集失败：{sub.name} - {err}")
            return {}
        if exist_flag:
            return {}
        missing = {}
        for season, info in (no_exists or {}).items():
            if isinstance(info, dict):
                for sea, notexist in info.items():
                    if getattr(notexist, "episodes", None):
                        missing[sea] = notexist.episodes
            elif getattr(info, "season", None) is not None:
                sea = info.season
                if getattr(info, "episodes", None):
                    missing[sea] = info.episodes
        return missing

    # ==================== NF API ====================

    @staticmethod
    def _now() -> float:
        """获取当前时间戳。"""
        import time as _t
        return _t.time()

    def _check_nf_resource(self, tmdb_id: int, media_type: str) -> Optional[List[dict]]:
        """搜索 NextFind 网盘/TG 频道资源；查询失败返回 None（不缓存），真无资源返回空列表。"""
        url = f"{self._api_url}/resources/search"
        headers = {"X-API-Key": self._api_key}
        params = {
            "tmdb_id": str(tmdb_id),
            "media_type": "tv" if media_type == "tv" else "movie",
        }
        try:
            res = RequestUtils(headers=headers, timeout=15).get_res(url, params=params)
            if not res:
                logger.warn(f"NF 资源搜索失败（无响应）：tmdb={tmdb_id}")
                return None
            if res.status_code != 200:
                logger.warn(f"NF 资源搜索失败：HTTP {res.status_code}：tmdb={tmdb_id}")
                return None
            data = res.json()
            if data.get("status") != "success":
                logger.warn(f"NF 资源搜索返回异常：{str(data)[:150]}：tmdb={tmdb_id}")
                return None
            payload = data.get("data")
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for key in ("items", "list", "resources", "results"):
                    val = payload.get(key)
                    if isinstance(val, list) and val:
                        return val
            return []
        except Exception as err:
            logger.warn(f"NF 资源搜索异常：{err}：tmdb={tmdb_id}")
            return None

    def _preview_nf_resource(self, slug: str) -> Optional[List[dict]]:
        """调用 NF 探针解包，返回文件树列表；失败返回 None。"""
        if not slug:
            return None
        url = f"{self._api_url}/preview"
        headers = {"X-API-Key": self._api_key}
        try:
            res = RequestUtils(headers=headers, timeout=10).post_res(url=url, json={"slug": slug})
            if not res or res.status_code != 200:
                logger.warn(f"NF 解包失败（HTTP {res.status_code if res else '无响应'}）：{slug[:60]}")
                return None
            data = res.json()
            if data.get("status") != "success":
                logger.warn(f"NF 解包返回异常：{str(data)[:150]}")
                return None
            files = data.get("data") or []
            if not isinstance(files, list):
                return None
            return [f for f in files if isinstance(f, dict) and f.get("id")]
        except Exception as err:
            logger.warn(f"NF 解包异常：{err}")
            return None

    def _pick_best_resource(self, resources: List[dict]) -> Optional[dict]:
        """从资源列表中挑选最优资源（4K/REMUX 优先）。"""
        if not resources:
            return None

        def _score(res: dict) -> int:
            score = 0
            reso = str(res.get("video_resolution") or res.get("resolution") or "")
            if any(k in reso.upper() for k in ("4K", "2160", "UHD")):
                score += 100
            elif "1080" in reso.upper():
                score += 50
            src = " ".join(res.get("source") or [])
            src = f"{src} {res.get('source_type') or ''}".upper()
            if "REMUX" in src:
                score += 60
            elif "蓝光" in src or "BLURAY" in src or "原盘" in src:
                score += 30
            score += min(int(res.get("unlocked_users_count") or 0), 100)
            return score

        return max(resources, key=_score)

    def _transfer_nf_resource(self, resource: dict, media_type: Optional[str] = None) -> bool:
        """整包转存资源到 115 转存文件夹。

        :param resource: NF 资源对象（含 slug）
        :param media_type: 媒体类型（tv/movie），显式传入优先于资源对象字段，避免缺字段误判目标目录
        """
        slug = resource.get("slug")
        if not slug:
            logger.warn("NF 资源缺少 slug，无法转存")
            return False
        if not media_type:
            media_type = resource.get("media_type")
        target_folder = "转存文件夹/电视剧" if media_type == "tv" else "转存文件夹/电影"
        url = f"{self._api_url}/transfer"
        headers = {"X-API-Key": self._api_key}
        body = {"slug": slug, "target_folder": target_folder}
        try:
            res = RequestUtils(headers=headers, timeout=30).post_res(url=url, json=body)
            if not res:
                logger.error(f"NF 转存失败（无响应）：{slug[:60]}")
                return False
            if res.status_code != 200:
                logger.error(f"NF 转存失败：HTTP {res.status_code} {res.text[:150]}")
                return False
            data = res.json()
            if data.get("status") != "success":
                logger.error(f"NF 转存返回异常：{str(data)[:150]}")
                return False
            logger.info(f"NF 转存成功：{slug[:60]} -> {target_folder}")
            return True
        except Exception as err:
            logger.error(f"NF 转存异常：{err}")
            return False

    def _transfer_single_item(self, item: dict, media_type: str) -> bool:
        """转存单个拆包文件（ed2k 链接或 115 文件 id）。"""
        item_id = item.get("id")
        if not item_id:
            return False
        target_folder = "转存文件夹/电视剧" if media_type == "tv" else "转存文件夹/电影"
        url = f"{self._api_url}/transfer"
        headers = {"X-API-Key": self._api_key}
        body = {"slug": item_id, "target_folder": target_folder}
        try:
            res = RequestUtils(headers=headers, timeout=30).post_res(url=url, json=body)
            if not res:
                logger.error(f"NF 单文件转存失败（无响应）：{str(item_id)[:60]}")
                return False
            if res.status_code != 200:
                logger.error(f"NF 单文件转存失败：HTTP {res.status_code} {res.text[:150]}")
                return False
            data = res.json()
            if data.get("status") != "success":
                logger.error(f"NF 单文件转存返回异常：{str(data)[:150]}")
                return False
            logger.info(f"NF 单文件转存成功：{str(item_id)[:60]} -> {target_folder}")
            return True
        except Exception as err:
            logger.error(f"NF 单文件转存异常：{err}")
            return False

    # ==================== 工具 ====================

    @staticmethod
    def _find_episode_item(files: List[dict], season: int, episode: int) -> Optional[dict]:
        """在文件树中查找指定季集的单集文件（支持 SxxExx 与 第N集 格式）。"""
        s_str = f"S{season:02d}E{episode:02d}"
        for f in files:
            name = str(f.get("name") or "")
            if s_str in name.upper():
                return f
        for f in files:
            name = str(f.get("name") or "")
            if re.search(rf"S{season}E{episode}\b", name, re.I):
                return f
            if re.search(rf"第\s*{episode}\s*集", name):
                return f
        return None

    @staticmethod
    def _transferred_key(tmdb_id, media_type: str, season: int, episode: int) -> str:
        return f"{tmdb_id}:{media_type}:{season}:{episode}"

    def _mark_transferred(self, tmdb_id, media_type: str, season: int, episode: int) -> None:
        key = self._transferred_key(tmdb_id, media_type, season, episode)
        self._nf_done.add(key)
        data = self.get_data("nf_transferred") or {}
        data[key] = 1
        self.save_data("nf_transferred", data)
