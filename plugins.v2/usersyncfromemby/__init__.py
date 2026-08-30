"""Emby 用户同步插件（NE 数据源）：NextEmby（NE）用户自动同步到 MoviePilot。

权限策略：同步用户仅授权订阅能力（permissions.search=False），禁止搜索 PT 站点资源。
搜索权限由 MoviePilot 搜索接口权限补丁强制（无 search 权限的非超管用户搜索返回 403）。
用户资料（头像、有效期）直接使用 NE 资料：头像下载为 base64 存 MP 用户，有效期存 settings。
"""

import base64
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager
from app.core.security import get_password_hash
from app.db.models.user import User
from app.db.user_oper import UserOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Event, Response
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class UserSyncFromEmby(_PluginBase):
    """Emby 用户自动同步到 MoviePilot（NE 资料源）。"""

    plugin_name = "Emby 用户同步"
    plugin_desc = "NextEmby（NE）用户自动同步到 MoviePilot：头像/有效期取自 NE 资料；同步用户仅授权订阅，禁止搜索 PT 站点资源。"
    plugin_icon = "usersyncfromemby.png"
    plugin_version = "1.0.0"
    plugin_label = "用户管理,媒体服务器"
    plugin_author = "local"
    plugin_config_prefix = "usersyncfromemby_"
    plugin_order = 100
    auth_level = 1

    _enabled = False
    _api_url = ""
    _api_key = ""
    _interval = 60
    _default_password = ""
    _skip_admin = True

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。"""
        self.stop_service()
        self._sync_lock = threading.Lock()
        self._enabled = False
        # 用户列表排序模式：name=用户名 / expiry=到期时间 / created=注册时间
        self._sort_mode = str(self.get_data("sort_mode") or "name")
        self._api_url = ""
        self._api_key = ""
        self._interval = 60
        self._default_password = ""
        self._skip_admin = True
        if not config:
            return
        self._enabled = bool(config.get("enabled"))
        self._api_url = str(config.get("api_url") or "").rstrip("/")
        self._api_key = str(config.get("api_key") or "")
        try:
            self._interval = max(5, int(config.get("interval") or 60))
        except (TypeError, ValueError):
            self._interval = 60
        self._default_password = str(config.get("default_password") or "")
        self._skip_admin = bool(config.get("skip_admin", True))

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return [
            {
                "cmd": "/usersync",
                "event": EventType.PluginAction,
                "desc": "手动触发 Emby 用户同步",
                "category": "用户",
                "data": {"action": "sync"}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。"""
        return [
            {
                "path": "/sync",
                "endpoint": self.api_sync,
                "methods": ["POST"],
                "summary": "执行用户同步",
                "description": "立即同步 NE 用户到 MoviePilot",
                "auth": "bear",
            },
            {
                "path": "/reverse_sync",
                "endpoint": self.api_reverse_sync,
                "methods": ["POST"],
                "summary": "执行反向同步",
                "description": "删除 MoviePilot 中 NE 已不存在的同步用户",
                "auth": "bear",
            },
            {
                "path": "/sort",
                "endpoint": self.api_sort,
                "methods": ["POST"],
                "summary": "切换用户排序",
                "description": "按到期时间/注册时间/用户名排序用户列表",
                "auth": "bear",
            },
            {
                "path": "/set_ai_access",
                "endpoint": self.api_set_ai_access,
                "methods": ["POST"],
                "summary": "授权/取消智能体",
                "description": "授予或取消指定用户使用智能体的权限",
                "auth": "bear",
            }
        ]

    def api_set_ai_access(self, payload: dict) -> Response:
        """授予/取消用户智能体使用权限（详情页「授权智能体」按钮调用）。"""
        username = (payload or {}).get("username")
        if not username:
            return Response(success=False, message="缺少 username")
        granted = bool((payload or {}).get("granted", True))
        user = UserOper().get_by_name(username)
        if not user:
            return Response(success=False, message=f"用户不存在：{username}")
        perms = dict(user.permissions or {})
        if granted:
            perms["ai_agent"] = True
        else:
            perms.pop("ai_agent", None)
        try:
            user.update(UserOper()._db, {"permissions": perms})
            logger.info(f"{'授权' if granted else '取消'}用户智能体权限：{username}")
            return Response(success=True, message=f"{'已授权' if granted else '已取消'}「{username}」使用智能体")
        except Exception as err:
            logger.error(f"更新用户权限失败：{username} - {err}")
            return Response(success=False, message=f"更新失败：{err}")

    def api_sort(self, payload: dict) -> Response:
        """切换用户列表排序模式（详情页排序按钮调用）。"""
        mode = (payload or {}).get("mode")
        if mode not in ("name", "expiry", "created"):
            return Response(success=False, message=f"不支持的排序方式：{mode}")
        self._sort_mode = mode
        self.save_data("sort_mode", mode)
        return Response(success=True, message=f"已按{'到期时间' if mode == 'expiry' else '注册时间' if mode == 'created' else '用户名'}排序")

    def api_sync(self) -> Response:
        """异步触发用户同步（详情页「执行同步」按钮调用）。"""
        if not self._sync_lock.acquire(blocking=False):
            return Response(success=False, message="同步正在运行中，请稍候")
        try:
            def _run():
                try:
                    self.sync_users()
                except Exception as err:
                    logger.error(f"后台用户同步异常：{err}")
                finally:
                    self._sync_lock.release()
            threading.Thread(target=_run, daemon=True).start()
            return Response(success=True, message="已开始同步 NE 用户，稍后刷新查看")
        except Exception as err:
            self._sync_lock.release()
            logger.error(f"启动用户同步失败：{err}")
            return Response(success=False, message=f"启动失败：{err}")

    def api_reverse_sync(self) -> Response:
        """异步触发反向同步（详情页「反向同步」按钮调用）。"""
        if not self._sync_lock.acquire(blocking=False):
            return Response(success=False, message="同步正在运行中，请稍候")
        try:
            def _run():
                try:
                    self.reverse_sync()
                except Exception as err:
                    logger.error(f"后台反向同步异常：{err}")
                finally:
                    self._sync_lock.release()
            threading.Thread(target=_run, daemon=True).start()
            return Response(success=True, message="已开始反向同步，稍后刷新查看")
        except Exception as err:
            self._sync_lock.release()
            logger.error(f"启动反向同步失败：{err}")
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
                            "label": "NextEmby API 地址",
                            "placeholder": "https://your-server.example.com/api"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "api_key",
                            "label": "NextEmby API Key",
                            "type": "password"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "interval",
                            "label": "同步检查间隔（分钟，最小5）",
                            "placeholder": "60"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "default_password",
                            "label": "新用户默认密码",
                            "type": "password",
                            "hint": "Emby 用户首次在 MoviePilot 登录时使用，可后续自行修改"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "skip_admin",
                            "label": "跳过 NE 管理员账号",
                            "hint": "NE 的 is_admin 用户不同步到 MoviePilot"
                        }
                    }
                ]
            }
        ], {
            "enabled": False,
            "api_url": "https://your-server.example.com/api",
            "api_key": "",
            "interval": 60,
            "default_password": "",
            "skip_admin": True
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面：状态 / 操作 / 统计 / 用户列表 四模块（暗色主题适配）。"""
        if not self._enabled:
            return None
        users = UserOper().list() or []
        sync_users = [
            u for u in users
            if (u.settings or {}).get("emby_sync") and u.name
        ]
        total = len(sync_users)
        expired = sum(
            1 for u in sync_users
            if (u.settings or {}).get("expiry_date")
            and self._is_expired((u.settings or {}).get("expiry_date"))
        )
        pwd_state = "已配置" if self._default_password else "未配置（新用户无法登录）"
        admin_state = "跳过管理员" if self._skip_admin else "包含管理员"
        # 高亮青蓝色值
        highlight = "text-primary"

        def section_card(title: str, icon: str, content: list, extra_class: str = "") -> dict:
            """模块容器卡片：半透明磨砂背景 + 充足内边距。"""
            return {
                "component": "VCard",
                "props": {
                    "variant": "tonal",
                    "color": "surface-variant",
                    "class": f"mb-5 rounded-lg {extra_class}".strip()
                },
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center ga-2 px-5 pt-4"},
                        "content": [
                            {"component": "span", "props": {"class": "text-primary text-body-1"}, "text": icon},
                            {"component": "span", "props": {"class": "text-subtitle-1 font-weight-bold"}, "text": title}
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "pa-5 pt-3"},
                        "content": content
                    }
                ]
            }

        # 状态行：CSS 网格两列（左标签固定宽居左，右值居右对齐）
        def info_row(label: str, value: str, value_color: str = "", full: bool = False) -> dict:
            """状态行；full=True 跨两列占满整行。"""
            if full:
                return {
                    "component": "div",
                    "props": {"class": "py-2"},
                    "content": [
                        {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis text-opacity-70 mb-1"}, "text": label},
                        {"component": "div", "props": {
                            "class": f"text-body-2 {value_color}".strip(),
                            "style": {"line-height": "1.6"}
                        }, "text": value}
                    ]
                }
            return {
                "component": "div",
                "props": {
                    "class": "d-flex align-center justify-space-between py-2",
                    "style": {
                        "display": "grid",
                        "grid-template-columns": "96px 1fr",
                        "gap": "16px"
                    }
                },
                "content": [
                    {"component": "span", "props": {"class": "text-body-2 text-medium-emphasis text-opacity-70"}, "text": label},
                    {"component": "span", "props": {
                        "class": f"text-body-2 font-weight-medium {value_color}".strip(),
                        "style": {"text-align": "right"}
                    }, "text": value}
                ]
            }

        nodes = []

        # ① 运行状态（网格两列）
        status_content = [
            info_row("同步状态", "已启用", highlight),
            info_row("同步周期", f"每 {self._interval} 分钟自动检查"),
            info_row("权限策略", "同步用户仅授权订阅，禁止搜索 PT 站点资源", full=True),
            info_row("资料来源", "头像 / 有效期取自 NextEmby 资料"),
            info_row("默认密码", pwd_state, "text-error" if not self._default_password else highlight),
            info_row("管理员账号", admin_state, highlight),
        ]
        nodes.append(section_card("运行状态", "●", status_content))

        # ② 操作模块：统一按钮 + 浅红半透明警告条
        action_content = [
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap ga-5 align-center"},
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "tonal",
                            "prepend-icon": "mdi-sync",
                            "size": "default",
                            "min-width": "120px",
                            "height": "40px",
                            "rounded": "lg",
                            "class": "flex-grow-0"
                        },
                        "text": "执行同步",
                        "events": {
                            "click": {
                                "api": "plugin/UserSyncFromEmby/sync",
                                "method": "POST",
                                "params": {}
                            }
                        }
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "error",
                            "variant": "tonal",
                            "prepend-icon": "mdi-sync-alert",
                            "size": "default",
                            "min-width": "120px",
                            "height": "40px",
                            "rounded": "lg",
                            "class": "flex-grow-0"
                        },
                        "text": "反向同步",
                        "events": {
                            "click": {
                                "api": "plugin/UserSyncFromEmby/reverse_sync",
                                "method": "POST",
                                "params": {}
                            }
                        }
                    }
                ]
            },
            # 浅红半透明提示条（高度自适应，不实色大块）
            {
                "component": "div",
                "props": {
                    "class": "d-flex align-start ga-2 mt-4 px-4 py-3 rounded-lg",
                    "style": {
                        "background": "rgba(var(--v-theme-error), 0.12)",
                        "border": "1px solid rgba(var(--v-theme-error), 0.35)"
                    }
                },
                "content": [
                    {"component": "span", "props": {"class": "text-body-1 flex-shrink-0 text-error"}, "text": "⚠️"},
                    {"component": "span", "props": {"class": "text-body-2 text-error", "style": {"line-height": "1.6"}},
                     "text": "高危操作：反向同步仅删除本插件同步生成的用户，手动创建用户不受影响，请确认后执行"}
                ]
            }
        ]
        nodes.append(section_card("操作", "⌘", action_content))

        # ③ 智能体权限独立小节（集中授权管理）
        ai_granted = sum(1 for u in users if (u.permissions or {}).get("ai_agent"))
        ai_rows = []
        for u in sorted([x for x in users if x.name], key=lambda x: x.name):
            granted = bool((u.permissions or {}).get("ai_agent"))
            ai_rows.append({
                "component": "VCard",
                "props": {"variant": "flat", "class": "mb-1 rounded-lg bg-surface"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center ga-2 pa-2"},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "flex-grow-1 min-w-0 text-body-2 font-weight-medium text-truncate"},
                                "text": u.name
                            },
                            {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": "teal" if granted else "grey", "height": "22px", "rounded": "pill"}, "text": "已授权" if granted else "未授权"},
                            (
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "teal",
                                        "variant": "tonal",
                                        "size": "x-small",
                                        "ripple": True,
                                        "height": "28px",
                                        "class": "flex-shrink-0"
                                    },
                                    "text": "取消授权",
                                    "events": {
                                        "click": {
                                            "api": "plugin/UserSyncFromEmby/set_ai_access",
                                            "method": "POST",
                                            "params": {"username": u.name, "granted": False}
                                        }
                                    }
                                }
                                if granted
                                else {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "primary",
                                        "variant": "tonal",
                                        "size": "x-small",
                                        "ripple": True,
                                        "height": "28px",
                                        "class": "flex-shrink-0"
                                    },
                                    "text": "授权",
                                    "events": {
                                        "click": {
                                            "api": "plugin/UserSyncFromEmby/set_ai_access",
                                            "method": "POST",
                                            "params": {"username": u.name, "granted": True}
                                        }
                                    }
                                }
                            )
                        ]
                    }
                ]
            })
        ai_content = [
            {
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis text-opacity-70 mb-3"},
                "text": f"默认所有人禁用智能体；已授权 {ai_granted} 人，仅授权用户可使用（需重启后生效）。"
            },
            {
                "component": "div",
                "props": {"style": {"max-height": "360px", "overflow-y": "auto"}},
                "content": ai_rows if ai_rows else [
                    {"component": "div", "props": {"class": "text-center pa-4 text-body-2 text-medium-emphasis text-opacity-70"}, "text": "暂无用户"}
                ]
            }
        ]
        nodes.append(section_card("智能体权限", "🤖", ai_content))

        # ④ 统计胶囊：半透明磨砂，统一高度圆角
        nodes.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap ga-3 mb-4"},
            "content": [
                {
                    "component": "VChip",
                    "props": {
                        "variant": "outlined",
                        "color": "primary",
                        "size": "default",
                        "height": "32px",
                        "rounded": "xl",
                        "class": "text-body-2"
                    },
                    "text": f"已同步 {total}"
                },
                {
                    "component": "VChip",
                    "props": {
                        "variant": "outlined",
                        "color": "error",
                        "size": "default",
                        "height": "32px",
                        "rounded": "xl",
                        "class": "text-body-2"
                    },
                    "text": f"已到期 {expired}"
                },
                {
                    "component": "VChip",
                    "props": {
                        "variant": "tonal",
                        "color": "grey",
                        "size": "default",
                        "height": "32px",
                        "rounded": "xl",
                        "class": "text-body-2"
                    },
                    "text": "仅订阅权限"
                },
            ]
        })

        # ④ 用户列表
        if not sync_users:
            nodes.append({
                "component": "VCard",
                "props": {"variant": "tonal", "color": "surface-variant", "class": "mb-5 rounded-lg"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "text-center pa-8 text-body-2 text-medium-emphasis text-opacity-70"},
                        "text": "暂无同步用户（配置默认密码后执行同步）"
                    }
                ]
            })
            return nodes

        # 排序
        sort_desc = {
            "expiry": "到期时间",
            "created": "注册时间",
            "name": "用户名",
        }

        def _sort_key(u):
            """按当前排序模式生成排序键。"""
            settings = u.settings or {}
            if self._sort_mode == "expiry":
                return settings.get("expiry_date") or "9999-12-31"
            if self._sort_mode == "created":
                return settings.get("add_date") or ""
            return u.name or ""

        sorted_users = sorted(sync_users, key=_sort_key)
        if self._sort_mode == "created":
            # 注册时间同按名称次级排序
            sorted_users = sorted(sorted_users, key=lambda x: (x.settings or {}).get("add_date") or "",)
        sort_label = sort_desc.get(self._sort_mode, "用户名")

        # 排序按钮行
        sort_buttons = []
        for mode, label in (("expiry", "到期时间"), ("created", "注册时间"), ("name", "用户名")):
            sort_buttons.append({
                "component": "VChip",
                "props": {
                    "variant": "tonal" if self._sort_mode == mode else "outlined",
                    "color": "primary" if self._sort_mode == mode else "grey",
                    "size": "small",
                    "height": "28px",
                    "rounded": "pill",
                    "class": "text-caption",
                    "style": {"cursor": "pointer"}
                },
                "text": f"按{label}排序",
                "events": {
                    "click": {
                        "api": "plugin/UserSyncFromEmby/sort",
                        "method": "POST",
                        "params": {"mode": mode}
                    }
                }
            })
        nodes.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap align-center ga-2 mb-3"},
            "content": [
                {"component": "span", "props": {"class": "text-caption text-medium-emphasis text-opacity-70"}, "text": "排序："},
                *sort_buttons
            ]
        })

        user_items = []
        for u in sorted_users:
            settings = u.settings or {}
            expiry = str(settings.get("expiry_date") or "—")
            is_expired = self._is_expired(expiry)
            state_text = "已到期" if is_expired else "正常"
            state_color = "error" if is_expired else "teal"
            avatar = u.avatar or ""
            avatar_node = {
                "component": "img",
                "props": {
                    "src": avatar,
                    "style": {
                        "width": "40px",
                        "height": "40px",
                        "border-radius": "50%",
                        "object-fit": "cover",
                        "flex-shrink": "0",
                        "display": "block"
                    }
                }
            } if avatar and avatar.startswith("data:") else {
                "component": "div",
                "props": {
                    "class": "d-flex align-center justify-center rounded-circle bg-grey-lighten-3 text-body-2 text-medium-emphasis flex-shrink-0",
                    "style": {"width": "40px", "height": "40px"}
                },
                "text": u.name[:1]
            }
            user_items.append({
                "component": "VCard",
                "props": {"variant": "flat", "class": "mb-2 rounded-lg bg-surface"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center ga-3 pa-4"},
                        "content": [
                            avatar_node,
                            {
                                "component": "div",
                                "props": {"class": "flex-grow-1 min-w-0"},
                                "content": [
                                    {"component": "div", "props": {"class": "text-body-1 font-weight-medium text-truncate"}, "text": u.name},
                                    {"component": "div", "props": {"class": "text-caption text-medium-emphasis text-opacity-60 mt-1"},
                                     "text": f"有效期 {expiry}"}
                                ]
                            },
                            {"component": "VChip", "props": {"size": "x-small", "variant": "tonal", "color": state_color, "height": "22px", "rounded": "pill"}, "text": state_text}
                        ]
                    }
                ]
            })
        nodes.append(section_card(f"同步用户（{total}）· 按{sort_label}排序", "👤", user_items, extra_class="mt-2"))
        return nodes

    @staticmethod
    def _is_expired(expiry_date: str) -> bool:
        """判断有效期是否已过。"""
        if not expiry_date or expiry_date == "—":
            return False
        try:
            return datetime.strptime(str(expiry_date), "%Y-%m-%d").date() < datetime.now().date()
        except (TypeError, ValueError):
            return False

    def get_service(self) -> List[Dict[str, Any]]:
        """返回插件定时服务。"""
        return [
            {
                "id": "UserSyncFromEmbyCheck",
                "name": "Emby 用户同步检查",
                "trigger": "interval",
                "func": self.sync_users,
                # 显式传 seconds，避免 APScheduler 0 间隔退化为每秒触发
                "kwargs": {"seconds": self._interval * 60}
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
            if not event_data or event_data.get("action") != "sync":
                return
            logger.info("收到命令，开始 Emby 用户同步 ...")
            self.sync_users()

    def _fetch_ne_users(self) -> Optional[Dict[str, dict]]:
        """从 NE 分页获取全部用户资料 {username: info}；失败返回 None。"""
        if not self._api_url or not self._api_key:
            logger.warn("未配置 NE API 地址或 Key")
            return None
        headers = {"Authorization": f"Bearer {self._api_key}"}
        all_users = {}
        page = 1
        total = None
        try:
            while True:
                url = f"{self._api_url}/users/list"
                res = RequestUtils(headers=headers, timeout=15).get_res(
                    url, params={"page": page}
                )
                if not res:
                    logger.error(f"NE 用户列表请求失败（无响应，page={page}）")
                    return None
                if res.status_code != 200:
                    logger.error(f"NE 用户列表请求失败：HTTP {res.status_code}（page={page}）")
                    return None
                data = res.json()
                if data.get("status") != "success":
                    logger.error(f"NE 用户列表返回异常：{str(data)[:150]}")
                    return None
                payload = data.get("data")
                if isinstance(payload, dict):
                    all_users.update(payload)
                if total is None:
                    total = int(data.get("total") or 0)
                # 已拉完则退出
                if len(all_users) >= total or not payload:
                    break
                page += 1
            logger.info(f"NE 用户全量获取完成：{len(all_users)} 条（total {total}）")
            return all_users
        except Exception as err:
            logger.error(f"获取 NE 用户列表异常：{err}")
            return None

    def _fetch_avatar_base64(self, uid) -> Optional[str]:
        """下载 NE 用户头像并压缩为 64px JPEG base64 data URL（防用户列表响应过大）；失败返回 None。"""
        if not uid:
            return None
        url = f"{self._api_url}/user/avatar/{uid}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            res = RequestUtils(headers=headers, timeout=15).get_res(url)
            if not res or res.status_code != 200:
                logger.warn(f"获取 NE 头像失败：uid={uid}")
                return None
            try:
                from io import BytesIO
                from PIL import Image
                img = Image.open(BytesIO(res.content))
                img = img.convert("RGB")
                img = img.resize((64, 64), Image.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=75)
                return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
            except Exception as err:
                # PIL 不可用时回退原图（限 128KB 防数据爆炸）
                logger.warn(f"头像压缩失败，回退原图：uid={uid} - {err}")
                content = res.content[:131072]
                return f"data:{res.headers.get('Content-Type') or 'image/png'};base64,{base64.b64encode(content).decode()}"
        except Exception as err:
            logger.warn(f"下载 NE 头像异常：uid={uid} - {err}")
            return None

    def sync_users(self) -> None:
        """同步 NE 用户到 MoviePilot：新用户自动创建，仅订阅权限；头像/有效期取自 NE。"""
        if not self._enabled:
            return
        if not self._default_password:
            logger.warn("未配置默认密码，跳过 Emby 用户同步")
            return
        ne_users = self._fetch_ne_users()
        if ne_users is None:
            logger.error("获取 NE 用户列表失败")
            return
        existing_map = {u.name: u for u in (UserOper().list() or [])}
        created = 0
        updated = 0
        skipped = 0
        for name, info in ne_users.items():
            if not name:
                continue
            # 跳过 NE 管理员
            if self._skip_admin and info.get("is_admin"):
                skipped += 1
                continue
            expiry = str(info.get("expiry_date") or "")
            uid = info.get("uid")
            add_date = str(info.get("add_date") or "")
            avatar = None
            if uid:
                avatar = self._fetch_avatar_base64(uid)
            settings = {
                "emby_sync": True,
                "emby_uid": str(uid or ""),
                "expiry_date": expiry,
                "add_date": add_date,
            }
            existing = existing_map.get(name)
            if existing:
                # 已存在：更新头像/有效期（幂等）
                merged_settings = {**(existing.settings or {}), **settings}
                update_fields = {"settings": merged_settings}
                if avatar and existing.avatar != avatar:
                    update_fields["avatar"] = avatar
                try:
                    user = UserOper().get_by_name(name)
                    if user:
                        user.update(UserOper()._db, update_fields)
                    updated += 1
                    logger.info(f"已更新 NE 用户资料：{name}（有效期 {expiry or '未知'}）")
                except Exception as err:
                    logger.error(f"更新用户失败：{name} - {err}")
                continue
            # 创建新用户
            try:
                UserOper().add(
                    name=name,
                    hashed_password=get_password_hash(self._default_password),
                    is_active=True,
                    is_superuser=False,
                    permissions={"search": False},
                    avatar=avatar or "",
                    settings=settings,
                )
                created += 1
                logger.info(f"已同步 NE 用户到 MoviePilot：{name}（仅订阅权限，有效期 {expiry or '未知'}）")
            except Exception as err:
                logger.error(f"创建用户失败：{name} - {err}")
        logger.info(f"Emby 用户同步完成：新增 {created}，更新 {updated}，跳过 {skipped}")

    def reverse_sync(self) -> None:
        """反向同步：删除 MoviePilot 中 NE 已不存在的同步用户（保留超级用户与手工创建用户）。"""
        if not self._enabled:
            return
        ne_users = self._fetch_ne_users()
        if ne_users is None:
            logger.error("获取 NE 用户列表失败，反向同步终止")
            return
        ne_names = set(ne_users.keys())
        deleted = 0
        skipped = 0
        for u in (UserOper().list() or []):
            # 只处理本插件同步的用户
            if not (u.settings or {}).get("emby_sync"):
                skipped += 1
                continue
            # 超级用户不删除
            if u.is_superuser:
                skipped += 1
                continue
            if u.name in ne_names:
                continue
            # NE 已不存在：删除 MP 用户
            try:
                User.delete(UserOper()._db, u.id)
                deleted += 1
                logger.info(f"反向同步删除 MP 用户：{u.name}（NE 已不存在）")
            except Exception as err:
                logger.error(f"删除用户失败：{u.name} - {err}")
        logger.info(f"反向同步完成：删除 {deleted}，跳过 {skipped}")
