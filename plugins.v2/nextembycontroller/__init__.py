"""NE 控制器插件：通过 Agent 对话控制 NextEmby（NE）的系统监控、用户管理与会话控制能力。"""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Response
from app.utils.http import RequestUtils

from .agenttool import NextEmbyManageTool, NextEmbyStatsTool


class NextEmbyController(_PluginBase):
    """NE 控制器插件。"""

    plugin_name = "NE 控制器"
    plugin_desc = "通过 Agent 对话控制 NextEmby（NE）的系统监控、用户管理与会话控制等 API 能力。"
    plugin_icon = "nextembycontroller.png"
    plugin_version = "1.3.0"
    plugin_label = "智能体,系统工具"
    plugin_author = "local"
    plugin_config_prefix = "nextembycontroller_"
    plugin_order = 100
    auth_level = 1

    _enabled = False
    _api_url = ""
    _api_key = ""
    _admin_username = ""
    _admin_password = ""

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._enabled = False
        self._api_url = ""
        self._api_key = ""
        self._admin_username = ""
        self._admin_password = ""
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._api_url = str(config.get("api_url") or "").rstrip("/")
        self._api_key = str(config.get("api_key") or "")
        self._admin_username = str(config.get("admin_username") or "").strip()
        self._admin_password = str(config.get("admin_password") or "")

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表（供详情页按钮事件调用）。"""
        return [
            {
                "path": "/refresh",
                "endpoint": self.api_refresh,
                "methods": ["POST"],
                "summary": "刷新面板数据",
                "description": "空操作，触发前端重新加载面板",
                "auth": "bear",
            },
            {
                "path": "/kill_session",
                "endpoint": self.api_kill_session,
                "methods": ["POST"],
                "summary": "强制断开播放会话",
                "description": "强制断开指定播放会话",
                "auth": "bear",
            },
            {
                "path": "/ban_user",
                "endpoint": self.api_ban_user,
                "methods": ["POST"],
                "summary": "封禁/解封用户",
                "description": "封禁或解封指定用户",
                "auth": "bear",
            },
            {
                "path": "/delete_user",
                "endpoint": self.api_delete_user,
                "methods": ["POST"],
                "summary": "删除用户",
                "description": "批量删除指定用户",
                "auth": "bear",
            },
            {
                "path": "/set_user_filter",
                "endpoint": self.api_set_user_filter,
                "methods": ["POST"],
                "summary": "切换用户筛选",
                "description": "按全部/在线/异常/到期筛选用户列表",
                "auth": "bear",
            },
            {
                "path": "/create_user",
                "endpoint": self.api_create_user,
                "methods": ["POST"],
                "summary": "创建 NE 用户",
                "description": "使用控制室保存的待创建用户参数新增 NE 用户",
                "auth": "bear",
            },
            {
                "path": "/generate_cards",
                "endpoint": self.api_generate_cards,
                "methods": ["POST"],
                "summary": "生成 AutoCard 卡密",
                "description": "使用控制室保存的卡密参数生成新批次",
                "auth": "bear",
            },
        ]

    def api_set_user_filter(self, payload: dict) -> Response:
        """切换用户列表筛选（详情页筛选标签点击调用）。"""
        flt = (payload or {}).get("filter")
        if flt not in ("all", "online", "abnormal", "expired"):
            return Response(success=False, message=f"不支持的筛选：{flt}")
        self.save_data("user_filter", flt)
        return Response(success=True, message=f"已筛选：{flt}")

    def api_refresh(self) -> Response:
        """刷新面板空操作。"""
        return Response(success=True)

    def api_kill_session(self, payload: dict) -> Response:
        """强制断开指定播放会话。"""
        session_id = (payload or {}).get("session_id")
        if not session_id:
            return Response(success=False, message="缺少 session_id")
        result = self._request("POST", f"/session/kill/{session_id}", body={})
        if "成功" in result or result.startswith("{"):
            return Response(success=True, message="会话已强制断开")
        return Response(success=False, message=result)

    def api_ban_user(self, payload: dict) -> Response:
        """封禁或解封指定用户。"""
        username = (payload or {}).get("username")
        is_banned = bool((payload or {}).get("is_banned", True))
        if not username:
            return Response(success=False, message="缺少 username")
        result = self._request("POST", "/users/ban",
                               body={"username": username, "is_banned": is_banned})
        if "成功" in result or result.startswith("{"):
            return Response(success=True, message=f"用户 {username} 已{'封禁' if is_banned else '解封'}")
        return Response(success=False, message=result)

    def api_delete_user(self, payload: dict) -> Response:
        """删除指定用户。"""
        username = (payload or {}).get("username")
        if not username:
            return Response(success=False, message="缺少 username")
        result = self._request("POST", "/users/batch_delete",
                               body={"usernames": [username], "sync_emby": True})
        if "成功" in result or result.startswith("{"):
            return Response(success=True, message=f"用户 {username} 已删除")
        return Response(success=False, message=result)

    def api_create_user(self) -> Response:
        """按插件配置中保存的参数创建 NE 用户。"""
        config = self.get_config() or {}
        username = str(config.get("new_user_username") or "").strip()
        password = str(config.get("new_user_password") or "").strip()
        cookies = str(config.get("new_user_cookies") or "").strip()
        if not username:
            return Response(success=False, message="请先在插件配置中填写待创建用户名")
        if not password:
            return Response(success=False, message="请先在插件配置中填写初始密码")
        cache_path = str(config.get("new_user_cache_path") or "/最近接收").strip()
        if not cache_path.startswith("/"):
            cache_path = f"/{cache_path}"
        body = {
            "user": username,
            "original_user": None,
            "cookies": cookies,
            "password": password,
            "cache_path": cache_path,
            "transfer_quota": float(config.get("new_user_quota") or 5),
            "transfer_quota_enabled": bool(config.get("new_user_quota_enabled", True)),
            "remark": str(config.get("new_user_remark") or "").strip(),
            "template_user": str(config.get("new_user_template") or "").strip(),
            "auto_delete_cron": "",
            "recycle_bin_code": "",
        }
        result = self._admin_request_json("POST", "/api/users", body=body)
        if not result.get("success"):
            return Response(success=False, message=result.get("message") or "NE 用户创建失败")
        config["new_user_password"] = ""
        config["new_user_cookies"] = ""
        self.update_config(config)
        return Response(success=True, message=f"NE 用户 {username} 已创建")

    def api_generate_cards(self) -> Response:
        """按插件配置中保存的参数生成 AutoCard 卡密批次。"""
        config = self.get_config() or {}
        try:
            days = int(config.get("card_duration_days") or 30)
            count = int(config.get("card_count") or 1)
        except (TypeError, ValueError):
            return Response(success=False, message="卡密时长和数量必须为整数")
        if days < 1 or days > 36500:
            return Response(success=False, message="卡密时长需在 1 至 36500 天之间")
        if count < 1 or count > 100:
            return Response(success=False, message="单次卡密数量需在 1 至 100 张之间")
        body = {
            "duration_days": days,
            "count": count,
            "template_name": str(config.get("card_template_name") or "").strip(),
        }
        result = self._admin_generate_cards(body)
        if not result.get("success"):
            return Response(success=False, message=result.get("message") or "卡密生成失败")
        batch_id = result.get("batch_id") or "未知批次"
        codes = result.get("codes") or []
        self.save_data("last_generated_cards", {
            "batch_id": batch_id,
            "duration_days": days,
            "count": len(codes),
            "codes": codes,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return Response(success=True, message=f"已生成 {len(codes)} 张卡密，批次 {batch_id}")

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
                            "label": "NextEmby API 地址",
                            "placeholder": "https://your-server.example.com/api"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "api_key",
                            "label": "NextEmby API Key（Bearer 密钥）",
                            "type": "password"
                        }
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "AutoCard 使用 NE 管理后台会话权限。管理员账号和密码仅由插件后端用于登录，不会显示在详情页。"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "admin_username",
                            "label": "NE 管理员用户名",
                            "placeholder": "请输入管理后台用户名"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "admin_password",
                            "label": "NE 管理员密码",
                            "type": "password",
                            "placeholder": "请输入管理后台密码"
                        }
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "控制室创建用户：先保存以下参数，再到详情页 Tab3 点击“创建用户”。成功后密码和 Cookie 会自动从配置中清空。"
                        }
                    },
                    {"component": "VTextField", "props": {"model": "new_user_username", "label": "待创建用户名"}},
                    {"component": "VTextField", "props": {"model": "new_user_password", "label": "初始密码", "type": "password"}},
                    {"component": "VTextField", "props": {"model": "new_user_cookies", "label": "115 Cookie（可选）", "type": "password"}},
                    {"component": "VTextField", "props": {"model": "new_user_cache_path", "label": "缓存目录", "placeholder": "/最近接收"}},
                    {"component": "VTextField", "props": {"model": "new_user_quota", "label": "秒传额度", "type": "number"}},
                    {"component": "VSwitch", "props": {"model": "new_user_quota_enabled", "label": "启用秒传额度"}},
                    {"component": "VTextField", "props": {"model": "new_user_template", "label": "模板用户（可选）"}},
                    {"component": "VTextField", "props": {"model": "new_user_remark", "label": "用户备注（可选）"}},
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "新增卡密会立即创建真实卡密。先保存时长、数量和模板名，再到详情页 Tab4 点击“生成卡密”。"
                        }
                    },
                    {"component": "VTextField", "props": {"model": "card_duration_days", "label": "卡密时长（天）", "type": "number"}},
                    {"component": "VTextField", "props": {"model": "card_count", "label": "生成数量（1-100）", "type": "number"}},
                    {"component": "VTextField", "props": {"model": "card_template_name", "label": "卡密模板名（可选）"}}
                ]
            }
        ], {
            "enabled": False,
            "api_url": "https://your-server.example.com/api",
            "api_key": "",
            "admin_username": "",
            "admin_password": "",
            "new_user_username": "",
            "new_user_password": "",
            "new_user_cookies": "",
            "new_user_cache_path": "/最近接收",
            "new_user_quota": 5,
            "new_user_quota_enabled": True,
            "new_user_template": "",
            "new_user_remark": "",
            "card_duration_days": 30,
            "card_count": 1,
            "card_template_name": ""
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面：4 个 Tab 面板（状态/播放/用户/AutoCard），支持按钮交互与刷新。"""
        if not self._enabled:
            return None
        # 并行抓取面板数据（任一失败不影响整体渲染）
        stats, stats_error = self._fetch_stats_data()
        sys_info, sys_error = self._fetch_sys_info()
        sessions, sessions_error = self._fetch_sessions()
        users, users_error = self._fetch_users()

        api_ok = not stats_error
        fetch_time = datetime.now().strftime("%H:%M:%S")
        online = int((stats or {}).get("online_users") or 0)
        total_users = int((stats or {}).get("total_users") or 0)
        abnormal = int((stats or {}).get("abnormal_users") or 0)
        expired = int((stats or {}).get("expired_users") or 0)

        def stat_card(title: str, value: str, color: str) -> dict:
            """紧凑统计卡片（低高度，压缩顶部占用）。"""
            return {
                "component": "VCol",
                "props": {"cols": 4, "sm": 4, "md": 2, "class": "px-1"},
                "content": [
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": color, "class": "pa-1 text-center"},
                        "content": [
                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis text-opacity-70"}, "text": title},
                            {"component": "div", "props": {"class": "text-h6 font-weight-bold"}, "text": value}
                        ]
                    }
                ]
            }

        def progress_bar(percent: float, color: str) -> dict:
            """百分比进度条（div 实现，避免组件兼容问题）。"""
            return {
                "component": "div",
                "props": {"class": "rounded", "style": {"height": "8px", "background": "#eee"}},
                "content": [
                    {
                        "component": "div",
                        "props": {
                            "class": "rounded",
                            "style": {
                                "height": "8px",
                                "width": f"{max(0, min(100, percent)):.0f}%",
                                "background": color,
                                "transition": "width 0.3s"
                            }
                        }
                    }
                ]
            }

        def page_header(title: str, page_no: int, total: int) -> dict:
            """页头：Tab 名称 + 页码。"""
            return {
                "component": "div",
                "props": {"class": "d-flex justify-space-between align-center mb-2"},
                "content": [
                    {"component": "span", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": title},
                    {"component": "span", "props": {"class": "text-caption text-medium-emphasis"},
                     "text": f"第 {page_no} / {total} 页"}
                ]
            }

        def refresh_button() -> dict:
            """刷新按钮（触发前端重新加载面板）。"""
            return {
                "component": "VBtn",
                "props": {"color": "primary", "variant": "tonal", "size": "small", "prepend-icon": "mdi-refresh"},
                "text": "刷新",
                "events": {"click": {"api": "plugin/NextEmbyController/refresh", "method": "POST", "params": {}}}
            }

        # ============ Tab1 状态面板 ============
        tab1_nodes = [page_header("Tab1 · 状态面板", 1, 4)]
        if stats_error:
            tab1_nodes.append({
                "component": "VAlert", "props": {"type": "error", "text": f"获取统计数据失败：{stats_error}"}
            })
        else:
            # 成功率统计
            def rate_card(title: str, suc: int, fail: int, color: str) -> dict:
                """成功率统计卡片（计数 + 百分比进度条）。"""
                total = suc + fail
                percent = (suc / total * 100) if total else 0
                return {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "outlined", "class": "pa-3 h-100"},
                            "content": [
                                {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-1"}, "text": title},
                                {"component": "div", "props": {"class": "d-flex align-baseline ga-2 mb-1"},
                                 "content": [
                                     {"component": "span", "props": {"class": "text-h5 font-weight-bold"}, "text": f"{percent:.0f}%"},
                                     {"component": "span", "props": {"class": "text-caption text-medium-emphasis"},
                                      "text": f"成功 {suc} / 失败 {fail}"}
                                 ]},
                                progress_bar(percent, color)
                            ]
                        }
                    ]
                }
            transfer = stats.get("transfer_stats") or {}
            link = stats.get("link_stats") or {}
            validity = stats.get("user_validity") or {}
            tab1_nodes.append({
                "component": "VRow",
                "props": {"class": "mb-2"},
                "content": [
                    rate_card("秒传成功率", int(transfer.get("suc") or 0), int(transfer.get("fail") or 0), "#26c6da"),
                    rate_card("115直链获取成功率", int(link.get("suc") or 0), int(link.get("fail") or 0), "#42a5f5"),
                    rate_card("用户Cookie状态成功率", int(validity.get("suc") or 0), int(validity.get("fail") or 0), "#66bb6a"),
                ]
            })
            # 硬件监控
            if sys_error:
                tab1_nodes.append({
                    "component": "VAlert", "props": {"type": "warning", "text": f"硬件监控获取失败：{sys_error}"}
                })
            else:
                cpu = float(sys_info.get("cpu") or 0)
                ram = float(sys_info.get("ram") or 0)
                disk = float((sys_info.get("disk") or {}).get("percent") or 0)

                def hw_row(label: str, percent: float, value_text: str, color: str) -> dict:
                    """硬件监控单行进度。"""
                    return {
                        "component": "div",
                        "props": {"class": "mb-3"},
                        "content": [
                            {"component": "div", "props": {"class": "d-flex justify-space-between text-body-2 mb-1"},
                             "content": [
                                 {"component": "span", "text": label},
                                 {"component": "span", "props": {"class": "text-medium-emphasis"}, "text": value_text}
                             ]},
                            progress_bar(percent, color)
                        ]
                    }
                tab1_nodes.append({
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "pa-3 mb-2"},
                    "content": [
                        {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-2"}, "text": "系统硬件监控"},
                        hw_row("CPU 使用率", cpu, f"{cpu}%", "#ef5350"),
                        hw_row("内存使用率", ram, f"{ram}%", "#ab47bc"),
                        hw_row("磁盘使用率", disk, f"{disk}%（已用 {(sys_info.get('disk') or {}).get('used', '?')}GB）", "#ffa726"),
                    ]
                })
            # 底部连接状态
            tab1_nodes.append({
                "component": "VCard",
                "props": {"variant": "tonal", "color": "grey-lighten-2", "class": "pa-2"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-between align-center"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "d-flex align-center ga-2"},
                                "content": [
                                    {"component": "span", "props": {"class": "text-success"}, "text": "●"},
                                    {"component": "span", "text": "NE API 连接正常"}
                                ]
                            } if api_ok else {
                                "component": "div",
                                "props": {"class": "d-flex align-center ga-2"},
                                "content": [
                                    {"component": "span", "props": {"class": "text-error"}, "text": "●"},
                                    {"component": "span", "text": f"NE API 连接异常（{str(stats_error)[:40]}）"}
                                ]
                            },
                            {"component": "span", "props": {"class": "text-caption text-medium-emphasis"},
                             "text": f"数据更新：{stats.get('last_updated') or fetch_time}"}
                        ]
                    }
                ]
            })

        # ============ Tab2 播放面板 ============
        tab2_nodes = [page_header("Tab2 · 播放面板", 2, 4)]
        if sessions_error:
            tab2_nodes.append({
                "component": "VAlert", "props": {"type": "error", "text": f"获取播放会话失败：{sessions_error}"}
            })
        elif not sessions:
            tab2_nodes.append({
                "component": "div", "props": {"class": "text-center pa-6 text-medium-emphasis"}, "text": "当前无活跃播放会话"
            })
        else:
            # NE 服务端 host（海报图片地址基础，从 api_url 推导）
            ne_host = self._api_url.rsplit("/api", 1)[0] if self._api_url else ""

            def fmt_dur(ticks) -> str:
                """将 Emby ticks（100ns）格式化为 mm:ss 或 h:mm:ss。"""
                if not ticks:
                    return ""
                sec = int(ticks) // 10_000_000
                h, rem = divmod(sec, 3600)
                m, s = divmod(rem, 60)
                return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

            for session in sessions[:30]:
                item_title = session.get("series_name") or session.get("item_name") or "未知片源"
                item_ep = ""
                if session.get("season_name") and session.get("index_number") is not None:
                    item_ep = f" {session.get('season_name')}E{session.get('index_number')}"
                elif session.get("episode_title"):
                    item_ep = f" · {session.get('episode_title')}"
                client = session.get("client") or session.get("device_name") or "未知设备"
                play_method = session.get("play_method") or "未知"
                location = session.get("location") or "未知位置"
                bitrate = session.get("bitrate_mbps")
                bitrate_text = f"{bitrate} Mbps" if bitrate is not None else "—"
                percent = int(session.get("percentage") or 0)
                paused = bool(session.get("is_paused"))
                # 海报：用 poster_id（节目/电影级条目）拼 Emby 图片地址
                # 容器强制锁定 56x84，img object-fit:cover 居中裁剪，禁止撑开卡片
                poster_url = ""
                if ne_host and session.get("poster_id"):
                    poster_url = f"{ne_host}/Items/{session.get('poster_id')}/Images/Primary"
                poster_node = {
                    "component": "div",
                    "props": {
                        "class": "rounded overflow-hidden flex-shrink-0",
                        "style": {
                            "width": "56px",
                            "height": "84px",
                            "min-width": "56px",
                            "max-width": "56px",
                            "background": "#f5f5f5"
                        }
                    },
                    "content": [
                        {
                            "component": "img",
                            "props": {
                                "src": poster_url,
                                "alt": "",
                                "loading": "lazy",
                                "style": {
                                    "width": "100%",
                                    "height": "100%",
                                    "object-fit": "cover",
                                    "object-position": "center",
                                    "display": "block"
                                }
                            }
                        }
                    ]
                } if poster_url else {
                    "component": "div",
                    "props": {
                        "class": "d-flex align-center justify-center rounded bg-grey-lighten-3 text-caption text-medium-emphasis flex-shrink-0",
                        "style": {"width": "56px", "height": "84px", "min-width": "56px", "max-width": "56px"}
                    },
                    "text": "无海报"
                }
                # 播放进度（已播/总时长）
                pos_text = fmt_dur(session.get("position_ticks"))
                dur_text = fmt_dur(session.get("run_time_ticks"))
                progress_text = f"{pos_text} / {dur_text}" if pos_text else f"{percent}%"
                chips = [
                    {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": "indigo"}, "text": play_method},
                    {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": "teal"}, "text": location},
                    {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": "deep-orange"}, "text": bitrate_text},
                    {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": "purple"}, "text": f"{percent}%"},
                ]
                if paused:
                    chips.append({"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": "amber"}, "text": "已暂停"})
                tab2_nodes.append({
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mb-2"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-nowrap ga-3 pa-3"},
                            "content": [
                                poster_node,
                                {
                                    "component": "div",
                                    "props": {"class": "min-w-0 flex-grow-1"},
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {"class": "text-subtitle-2 font-weight-bold text-truncate"},
                                            "content": [
                                                {"component": "span", "text": item_title},
                                                {"component": "span", "props": {"class": "text-medium-emphasis font-weight-regular"}, "text": item_ep}
                                            ]
                                        },
                                        {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis"},
                                         "text": f"{session.get('user_name', '?')} · {client}"},
                                        {
                                            "component": "div",
                                            "props": {"class": "d-flex flex-wrap ga-1 mt-1"},
                                            "content": chips
                                        },
                                        {
                                            "component": "div",
                                            "props": {"class": "d-flex align-center ga-2 mt-2"},
                                            "content": [
                                                {
                                                    "component": "div",
                                                    "props": {"class": "flex-grow-1"},
                                                    "content": [progress_bar(percent, "#42a5f5")]
                                                },
                                                {"component": "span", "props": {"class": "text-caption text-medium-emphasis"},
                                                 "text": f"{percent}% · {progress_text}"}
                                            ]
                                        }
                                    ]
                                },
                                {
                                    "component": "VBtn",
                                    "props": {"color": "error", "variant": "tonal", "size": "small", "prepend-icon": "mdi-power"},
                                    "text": "断开",
                                    "events": {
                                        "click": {
                                            "api": "plugin/NextEmbyController/kill_session",
                                            "method": "POST",
                                            "params": {"session_id": session.get("session_id")}
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                })

        # ============ Tab3 用户管理面板 ============
        tab3_nodes = [page_header("Tab3 · 用户管理", 3, 4)]
        create_user_config = self.get_config() or {}
        pending_username = str(create_user_config.get("new_user_username") or "").strip()
        tab3_nodes.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap align-center justify-space-between ga-2 mb-2"},
            "content": [
                {
                    "component": "span",
                    "props": {"class": "text-caption text-medium-emphasis"},
                    "text": f"待创建：{pending_username or '请先在插件配置填写用户参数'}"
                },
                {
                    "component": "VBtn",
                    "props": {
                        "color": "primary", "variant": "tonal", "size": "small",
                        "prepend-icon": "mdi-account-plus", "disabled": not bool(pending_username)
                    },
                    "text": "创建用户",
                    "events": {"click": {"api": "plugin/NextEmbyController/create_user", "method": "POST"}}
                }
            ]
        })
        if users_error:
            tab3_nodes.append({
                "component": "VAlert", "props": {"type": "error", "text": f"获取用户列表失败：{users_error}"}
            })
        else:
            # 当前激活筛选（点击标签切换，持久化）
            current_filter = str(self.get_data("user_filter") or "all")
            # 在线用户集合（来自 stats.online_user_list）
            online_names = set(stats.get("online_user_list") or []) if stats else set()
            user_items = list((users or {}).items())
            today = datetime.now().date()

            def _match_filter(username, info):
                """判断用户是否匹配当前筛选。"""
                if current_filter == "all":
                    return True
                if current_filter == "online":
                    return username in online_names
                if current_filter == "abnormal":
                    return bool(info.get("abnormal") or False)
                if current_filter == "expired":
                    ed = info.get("expiry_date")
                    if not ed:
                        return False
                    try:
                        return datetime.strptime(str(ed), "%Y-%m-%d").date() < today
                    except (TypeError, ValueError):
                        return False
                return True

            # 筛选标签（选中高亮 + 主题色 + 点击切换）
            filter_defs = [
                ("all", "全部", f"全部 {total_users}", "primary"),
                ("online", "在线", f"在线 {online}", "teal"),
                ("abnormal", "异常", f"异常 {abnormal}", "orange"),
                ("expired", "到期", f"到期 {expired}", "error"),
            ]
            filter_chips = []
            for key, label, text, color in filter_defs:
                active = current_filter == key
                filter_chips.append({
                    "component": "VChip",
                    "props": {
                        "variant": "tonal" if active else "outlined",
                        "color": color,
                        "size": "small",
                        "height": "28px",
                        "rounded": "pill",
                        "class": "text-caption" + (" font-weight-bold" if active else ""),
                        "style": {"cursor": "pointer", "transition": "all 0.2s"}
                    },
                    "text": text,
                    "events": {
                        "click": {
                            "api": "plugin/NextEmbyController/set_user_filter",
                            "method": "POST",
                            "params": {"filter": key}
                        }
                    }
                })
            # 筛选栏：左筛选标签 + 右上角分页提示
            filtered = [it for it in user_items if _match_filter(it[0], it[1])]
            tab3_nodes.append({
                "component": "div",
                "props": {"class": "d-flex flex-wrap justify-space-between align-center ga-2 mb-2"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex flex-wrap align-center ga-1"},
                        "content": filter_chips
                    },
                    {
                        "component": "span",
                        "props": {"class": "text-caption text-medium-emphasis text-opacity-70"},
                        "text": f"匹配 {len(filtered)} / 共 {len(user_items)} · 显示前 30"
                    }
                ]
            })
            # 用户列表（按筛选过滤 + 前 30）
            shown = 0
            for username, info in filtered:
                if shown >= 30:
                    break
                shown += 1
                expiry_date = str(info.get("expiry_date") or "")
                is_expired = False
                if info.get("expiry_date"):
                    try:
                        is_expired = datetime.strptime(str(info["expiry_date"]), "%Y-%m-%d").date() < today
                    except (TypeError, ValueError):
                        is_expired = False
                is_abnormal = bool(info.get("abnormal") or False)
                state_text = "已到期" if is_expired else ("异常" if is_abnormal else "正常")
                state_color = "error" if is_expired else ("orange" if is_abnormal else "teal")
                # 到期时间：空显示"永不过期"并高亮
                if not info.get("expiry_date"):
                    expiry_node = {
                        "component": "span",
                        "props": {"class": "text-body-2 font-weight-medium text-teal-lighten-1"},
                        "text": "永不过期"
                    }
                else:
                    expiry_node = {
                        "component": "span",
                        "props": {"class": "text-body-2 text-medium-emphasis text-opacity-80"},
                        "text": f"到期 {expiry_date}"
                    }
                tab3_nodes.append({
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mb-1 rounded-lg"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex align-center ga-2 pa-2"},
                            "content": [
                                {"component": "div", "props": {"class": "text-body-2 font-weight-medium text-truncate", "style": {"min-width": "90px", "max-width": "150px"}}, "text": username},
                                {"component": "div", "props": {"class": "d-flex align-center", "style": {"min-width": "90px"}}, "content": [expiry_node]},
                                {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": state_color}, "text": state_text},
                                {"component": "div", "props": {"class": "flex-grow-1"}},
                                {
                                    "component": "VBtn",
                                    "props": {"color": "warning", "variant": "tonal", "size": "x-small", "ripple": True},
                                    "text": "封禁",
                                    "events": {"click": {"api": "plugin/NextEmbyController/ban_user", "method": "POST",
                                                         "params": {"username": username, "is_banned": True}}}
                                },
                                {
                                    "component": "VBtn",
                                    "props": {"color": "error", "variant": "tonal", "size": "x-small", "ripple": True},
                                    "text": "删除",
                                    "events": {"click": {"api": "plugin/NextEmbyController/delete_user", "method": "POST",
                                                         "params": {"username": username}}}
                                }
                            ]
                        }
                    ]
                })
            if not filtered:
                tab3_nodes.append({
                    "component": "div",
                    "props": {"class": "text-center pa-4 text-body-2 text-medium-emphasis text-opacity-70"},
                    "text": "当前筛选条件下无用户"
                })
            # 底部说明（缩小降透明度）
            tab3_nodes.append({
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis text-opacity-60 mt-1"},
                "text": "搜索、编辑、改期请用对话 nextemby_manage；页面上可直接封禁/删除。"
            })

        # ============ Tab4 AutoCard 开卡面板 ============
        tab4_nodes = [page_header("Tab4 · AutoCard 开卡", 4, 4)]
        admin_probe = self._probe_admin_autocard()
        probe_ok = bool(admin_probe.get("login_ok"))
        batches = admin_probe.get("batches") or []
        history = admin_probe.get("history") or []
        tab4_nodes.append({
            "component": "VAlert",
            "props": {
                "type": "success" if probe_ok else "warning",
                "variant": "tonal",
                "text": admin_probe.get("message") or "尚未配置 NE 管理员登录信息。"
            }
        })
        if probe_ok:
            card_config = self.get_config() or {}
            card_days = int(card_config.get("card_duration_days") or 30)
            card_count = int(card_config.get("card_count") or 1)
            card_template = str(card_config.get("card_template_name") or "").strip()
            tab4_nodes.append({
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center justify-space-between ga-2 mt-2"},
                "content": [
                    {
                        "component": "span",
                        "props": {"class": "text-caption text-medium-emphasis"},
                        "text": f"新增参数：{card_days} 天 × {card_count} 张" + (f" · {card_template}" if card_template else "")
                    },
                    {
                        "component": "VBtn",
                        "props": {"color": "deep-orange", "variant": "tonal", "size": "small", "prepend-icon": "mdi-card-plus"},
                        "text": "生成卡密",
                        "events": {"click": {"api": "plugin/NextEmbyController/generate_cards", "method": "POST"}}
                    }
                ]
            })
            last_generated = self.get_data("last_generated_cards") or {}
            if last_generated:
                masked_codes = "、".join(self._mask_card_code(x) for x in (last_generated.get("codes") or [])[:5])
                tab4_nodes.append({
                    "component": "VAlert",
                    "props": {
                        "type": "info", "variant": "tonal",
                        "text": f"最近生成：批次 {last_generated.get('batch_id') or '-'} · {last_generated.get('count') or 0} 张 · {last_generated.get('created_at') or '-'}" + (f" · {masked_codes}" if masked_codes else "")
                    }
                })
            total_cards = sum(int(x.get("total") or 0) for x in batches)
            remaining_cards = sum(int(x.get("remaining") or 0) for x in batches)
            tab4_nodes.append({
                "component": "VRow",
                "props": {"dense": True, "class": "mx-0 mt-1"},
                "content": [
                    stat_card("卡密批次", str(len(batches)), "deep-orange"),
                    stat_card("剩余卡密", str(remaining_cards), "success"),
                    stat_card("累计卡密", str(total_cards), "primary"),
                    stat_card("使用记录", str(len(history)), "purple"),
                ]
            })
            tab4_nodes.append({
                "component": "VList",
                "props": {"density": "compact", "class": "mt-2"},
                "content": [
                    {
                        "component": "VListItem",
                        "props": {
                            "title": f"{('永久' if int(item.get('duration_days') or 0) == 0 or int(item.get('duration_days') or 0) >= 999 else str(item.get('duration_days')) + ' 天')} · {item.get('template_name') or '默认模板'}",
                            "subtitle": f"剩余 {item.get('remaining') or 0}/{item.get('total') or 0} · 批次 {item.get('batch_id') or '-'}"
                        }
                    }
                    for item in batches[:20]
                ] or [{"component": "VListItem", "props": {"title": "暂无卡密批次"}}]
            })
            if history:
                tab4_nodes.append({
                    "component": "div",
                    "props": {"class": "text-caption font-weight-medium mt-2 mb-1"},
                    "text": "最近使用记录"
                })
                tab4_nodes.append({
                    "component": "VList",
                    "props": {"density": "compact"},
                    "content": [
                        {
                            "component": "VListItem",
                            "props": {
                                "title": f"{item.get('used_by') or '未知用户'} · {item.get('duration_days') or 0} 天",
                                "subtitle": f"{item.get('used_at') or '-'} · {self._mask_card_code(item.get('card_code'))}"
                            }
                        }
                        for item in history[:10]
                    ]
                })

        # ============ 组装 4 Tab ============
        def window_item(value: str, nodes: list) -> dict:
            """单个 Tab 页内容。"""
            return {"component": "VWindowItem", "props": {"value": value}, "content": nodes}

        return [
            {
                "component": "VRow",
                "props": {"class": "mb-1 mx-0", "dense": True},
                "content": [
                    stat_card("运行状态", "● 正常" if api_ok else "● 异常", "success" if api_ok else "error"),
                    stat_card("播放在线", f"{online}", "teal"),
                    stat_card("总用户", f"{total_users}", "primary"),
                    stat_card("异常用户", f"{abnormal}", "orange"),
                    stat_card("到期用户", f"{expired}", "error"),
                    {
                        "component": "VCol",
                        "props": {"cols": 4, "sm": 4, "md": 2, "class": "px-1"},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal", "color": "grey-lighten-3", "class": "pa-1 h-100 d-flex align-center justify-end"},
                                "content": [refresh_button()]
                            }
                        ]
                    }
                ]
            },
            {
                "component": "VWindow",
                "props": {"show-arrows": "always", "class": "mt-1"},
                "content": [
                    window_item("tab1", tab1_nodes),
                    window_item("tab2", tab2_nodes),
                    window_item("tab3", tab3_nodes),
                    window_item("tab4", tab4_nodes),
                ]
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        return None

    def get_agent_tools(self) -> List[type]:
        """返回插件提供的 Agent 工具列表。"""
        return [
            NextEmbyStatsTool,
            NextEmbyManageTool,
        ]

    # ==================== NE API 调用方法 ====================

    def _admin_host(self) -> str:
        """从开放 API 地址推导 NE 管理后台主机地址。"""
        return self._api_url.rsplit("/api", 1)[0].rstrip("/") if self._api_url else ""

    @staticmethod
    def _mask_card_code(card_code: Any) -> str:
        """遮盖卡密，仅保留首尾少量字符供用户辨认。"""
        code = str(card_code or "")
        if len(code) <= 8:
            return "****"
        return f"{code[:4]}****{code[-4:]}"

    def _admin_login_session(self) -> Tuple[Optional[requests.Session], str]:
        """登录 NE 管理后台并返回带会话 Cookie 的请求会话。"""
        if not self._admin_username or not self._admin_password:
            return None, "请先配置 NE 管理员用户名和密码"
        host = self._admin_host()
        if not host:
            return None, "无法从 API 地址推导 NE 管理后台地址"
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.post(
                f"{host}/api/admin/login",
                json={"username": self._admin_username, "password": self._admin_password},
                timeout=15,
            )
            if response.status_code == 200:
                return session, ""
            try:
                detail = str((response.json() or {}).get("detail") or "")
            except (ValueError, TypeError):
                detail = response.text[:120]
            session.close()
            return None, f"NE 管理后台登录失败：HTTP {response.status_code} {detail}".strip()
        except requests.RequestException as err:
            session.close()
            return None, f"NE 管理后台连接失败：{err}"

    def _admin_request_json(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        """使用 NE 管理会话调用 JSON 接口。"""
        session, error = self._admin_login_session()
        if not session:
            return {"success": False, "message": error}
        try:
            response = session.request(method, f"{self._admin_host()}{path}", json=body, timeout=30)
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.ok:
                return {"success": True, "data": data}
            message = data.get("detail") or data.get("message") if isinstance(data, dict) else ""
            return {"success": False, "message": str(message or f"HTTP {response.status_code}")}
        except requests.RequestException as err:
            return {"success": False, "message": f"NE 管理请求失败：{err}"}
        finally:
            session.close()

    def _admin_generate_cards(self, body: dict) -> Dict[str, Any]:
        """调用 NE 流式接口生成卡密并解析批次及卡密列表。"""
        session, error = self._admin_login_session()
        if not session:
            return {"success": False, "message": error}
        try:
            response = session.post(
                f"{self._admin_host()}/api/admin/cards/generate",
                json=body,
                timeout=60,
                stream=True,
            )
            if not response.ok:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                return {"success": False, "message": str(data.get("detail") or data.get("message") or f"HTTP {response.status_code}")}
            batch_id = ""
            codes = []
            for raw_line in response.iter_lines(decode_unicode=True):
                line = str(raw_line or "").strip()
                if not line:
                    continue
                if line.startswith("BATCH_ID:"):
                    batch_id = line[9:].strip()
                else:
                    codes.append(line)
            if not batch_id or not codes:
                return {"success": False, "message": "NE 未返回完整的卡密批次数据"}
            return {"success": True, "batch_id": batch_id, "codes": codes}
        except requests.RequestException as err:
            return {"success": False, "message": f"NE 卡密生成请求失败：{err}"}
        finally:
            session.close()

    def _probe_admin_autocard(self) -> Dict[str, Any]:
        """登录 NE 管理后台并读取 AutoCard 批次及使用历史。"""
        empty = {"login_ok": False, "batches": [], "history": []}
        if not self._admin_username or not self._admin_password:
            return {**empty, "message": "请先在插件配置中填写 NE 管理员用户名和密码。"}
        host = self._admin_host()
        if not host:
            return {**empty, "message": "无法从 API 地址推导 NE 管理后台地址。"}
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.post(
                f"{host}/api/admin/login",
                json={"username": self._admin_username, "password": self._admin_password},
                timeout=15,
            )
            if response.status_code != 200:
                detail = ""
                try:
                    detail = str((response.json() or {}).get("detail") or "")
                except Exception:
                    detail = response.text[:120]
                return {
                    **empty,
                    "message": f"NE 管理后台登录失败：HTTP {response.status_code} {detail}".strip(),
                }
            batches_res = session.get(f"{host}/api/admin/cards/batches", timeout=15)
            history_res = session.get(f"{host}/api/admin/cards/history", timeout=15)
            batches_json = batches_res.json() if batches_res.status_code == 200 else {}
            history_json = history_res.json() if history_res.status_code == 200 else {}
            batches = batches_json.get("data") if isinstance(batches_json, dict) else []
            history = history_json.get("data") if isinstance(history_json, dict) else []
            batches = batches if isinstance(batches, list) else []
            history = history if isinstance(history, list) else []
            if batches_res.status_code != 200 or history_res.status_code != 200:
                return {
                    "login_ok": True,
                    "batches": batches,
                    "history": history,
                    "message": f"NE 管理后台登录成功，但 AutoCard 数据读取不完整：批次 HTTP {batches_res.status_code}，历史 HTTP {history_res.status_code}。",
                }
            return {
                "login_ok": True,
                "batches": batches,
                "history": history,
                "message": f"NE 管理后台登录成功，AutoCard 管理权限可用；已读取 {len(batches)} 个批次、{len(history)} 条使用记录。",
            }
        except (requests.RequestException, ValueError) as err:
            return {**empty, "message": f"NE AutoCard 管理请求失败：{err}"}
        finally:
            session.close()

    def _request(self, method: str, path: str, params: dict = None, body: dict = None) -> str:
        """发起 NE API 请求并返回格式化结果。"""
        if not self._api_url or not self._api_key:
            return "NE 控制器未配置 API 地址或 API Key"
        url = f"{self._api_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if method.upper() == "GET":
                res = RequestUtils(headers=headers).get_res(url, params=params)
            elif method.upper() == "POST":
                res = RequestUtils(headers=headers).post_res(url, json=body)
            else:
                return f"不支持的请求方法：{method}"
            if not res:
                return "NE API 请求失败（无响应）"
            if res.status_code != 200:
                return f"NE API 请求失败：HTTP {res.status_code} {res.text[:300]}"
            try:
                data = res.json()
            except Exception:
                return f"NE API 返回非 JSON：{res.text[:500]}"
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception as err:
            logger.error(f"NE API 请求异常：{err}")
            return f"NE API 请求异常：{err}"

    def _request_json(self, method: str, path: str, params: dict = None,
                      body: dict = None) -> Tuple[Optional[dict], str]:
        """发起 NE API 请求并返回解析后的 JSON（供详情页使用）。"""
        if not self._api_url or not self._api_key:
            return None, "未配置 API 地址或 API Key"
        url = f"{self._api_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if method.upper() == "GET":
                res = RequestUtils(headers=headers, timeout=10).get_res(url, params=params)
            else:
                res = RequestUtils(headers=headers, timeout=10).post_res(url, json=body)
            if not res:
                return None, "NE API 请求失败（无响应）"
            if res.status_code != 200:
                return None, f"NE API 请求失败：HTTP {res.status_code} {res.text[:150]}"
            try:
                return res.json(), ""
            except Exception:
                return None, f"NE API 返回非 JSON：{res.text[:200]}"
        except Exception as err:
            return None, f"NE API 请求异常：{err}"

    def _fetch_stats_data(self) -> Tuple[dict, str]:
        """获取 NE 综合统计（用户数/成功率/历史）。"""
        data, error = self._request_json("GET", "/stats")
        if error or not isinstance(data, dict):
            return {}, error or "统计接口返回异常"
        return data, ""

    def _fetch_sys_info(self) -> Tuple[dict, str]:
        """获取 NE 系统硬件监控状态。"""
        data, error = self._request_json("GET", "/sys_stats")
        if error or not isinstance(data, dict):
            return {}, error or "系统状态返回异常"
        return data, ""

    def _fetch_sessions(self) -> Tuple[List[dict], str]:
        """获取 NE 实时活跃播放会话。"""
        data, error = self._request_json("GET", "/active_sessions")
        if error:
            return [], error
        if isinstance(data, list):
            return data, ""
        return [], "活跃会话返回格式异常"

    def _fetch_users(self) -> Tuple[dict, str]:
        """获取 NE 用户列表（返回 data 字典 {username: info}）。"""
        data, error = self._request_json("GET", "/users/list")
        if error:
            return {}, error
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"], ""
        return {}, "用户列表返回格式异常"

    async def tool_stats(self, action: str = "stats") -> str:
        """NE 系统监控：硬件状态、活跃会话、缓存列表、用户列表。"""
        action_map = {
            "stats": "/sys_stats",
            "sessions": "/active_sessions",
            "caches": "/cache/list",
            "users": "/users/list",
        }
        path = action_map.get(action)
        if not path:
            return f"不支持的查询类型：{action}（可选：{', '.join(action_map.keys())}）"
        return self._request("GET", path)

    async def tool_manage(self, action: str, **kwargs) -> str:
        """NE 管理操作：用户管理、会话控制、系统配置。"""
        # 用户管理
        if action == "ban":
            body = {"username": kwargs.get("username"), "is_banned": kwargs.get("is_banned", True)}
            return self._request("POST", "/users/ban", body=body)
        if action == "batch_delete":
            body = {
                "usernames": kwargs.get("usernames") or [],
                "sync_emby": kwargs.get("sync_emby", True),
            }
            return self._request("POST", "/users/batch_delete", body=body)
        if action == "batch_approve":
            users = kwargs.get("users")
            if not users:
                return "批量审批需要 users 参数（[{user, cookies}, ...]）"
            if isinstance(users, str):
                try:
                    users = json.loads(users)
                except Exception:
                    return f"users 参数不是合法 JSON：{users}"
            return self._request("POST", "/users/batch_approve", body={"users": users})
        if action == "batch_expiry":
            users = kwargs.get("users")
            if not users:
                return "批量修改期限需要 users 参数（[{username, expiry_date}, ...]）"
            if isinstance(users, str):
                try:
                    users = json.loads(users)
                except Exception:
                    return f"users 参数不是合法 JSON：{users}"
            return self._request("POST", "/users/batch_expiry", body={"users": users})
        if action == "update_settings":
            body = {}
            if kwargs.get("cookies"):
                body["cookies"] = kwargs["cookies"]
            if kwargs.get("cache_path"):
                body["cache_path"] = kwargs["cache_path"]
            return self._request("POST", "/user/update_settings", body=body)
        if action == "edit_info":
            body = {"username": kwargs.get("username")}
            if kwargs.get("password"):
                body["password"] = kwargs["password"]
            return self._request("POST", "/user/edit_info", body=body)
        if action == "reset_password":
            body = {
                "username": kwargs.get("username"),
                "old_cookies": kwargs.get("old_cookies") or "",
                "password": kwargs.get("password"),
            }
            return self._request("POST", "/admin/reset_password", body=body)
        # 网盘与系统配置
        if action == "update_master_cookie":
            return self._request("POST", "/config/update_master_cookie",
                                 body={"cookie": kwargs.get("cookie")})
        if action == "check_drive":
            return self._request("POST", "/system/check_admin_drive",
                                 body={"force": kwargs.get("force", True)})
        if action == "update_user_group":
            body = {
                "overclock_mode": kwargs.get("overclock_mode", False),
                "expire_days": kwargs.get("expire_days") or 0,
            }
            return self._request("POST", "/system/update_user_group", body=body)
        # 系统控制
        if action == "kill_session":
            session_id = kwargs.get("session_id")
            if not session_id:
                return "踢出会话需要 session_id 参数"
            return self._request("POST", f"/session/kill/{session_id}", body={})
        if action == "notify_test":
            body = {}
            if kwargs.get("proxy_url"):
                body["proxy_url"] = kwargs["proxy_url"]
            return self._request("POST", "/notification/test", body=body)
        if action == "system_settings":
            body = {"system": {"enable_ip_lock": kwargs.get("enable_ip_lock", True)}}
            return self._request("POST", "/system/settings", body=body)
        if action == "restart":
            return self._request("POST", "/restart", body={})
        return (
            f"不支持的 NE 操作：{action}（可选：ban/batch_delete/batch_approve/batch_expiry/"
            f"update_settings/edit_info/reset_password/update_master_cookie/check_drive/"
            f"update_user_group/kill_session/notify_test/system_settings/restart）"
        )
