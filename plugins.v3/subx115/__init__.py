# -*- coding: utf-8 -*-
"""
SubX115 — 网盘聚合签到（subx 的 MoviePilot 原生实现）
==========================================================

把 subx（115 订阅工具，Docker 镜像 lmhhj/subx）的渠道配置原生搬进 MoviePilot。
配置页只有 subx 原版的 8 个渠道页：聚影 / 栖影 / 剧迷 / 癫影 / 观影 /
帧影 / 蜗牛4K / 权益宝（VTabs 垂直侧栏 + VWindow 面板，参照 subx 侧栏
布局）；插件运行必需的全局参数（启用 / 通知 / 轮询周期 / 115 Cookie /
默认转存目录）收敛为配置页顶部一条紧凑的全局设置栏，不占渠道页签。

另整合了 JinxJie/MoviePilot-Plugins 的 hdhavensign：栖影（HDHaven）
自动签到（预检 -> 查状态 -> 提交），配置放在「栖影配置」页内。

运行流程：
  定时轮询（cron） -> 各启用渠道拉取最新 115 分享
  -> 与 MoviePilot 订阅（片名+季）匹配 -> 去重 -> 转存到 115 -> 消息通知
  栖影签到（cron） -> 登录预检 -> 查询今日状态 -> 未签到才提交 -> 通知
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.schemas.types import EventType

# ---- 宿主兼容导入：优先 V3 SDK（见 docs/Plugin_Development.md），旧宿主兜底 ----
try:
    from app.sdk.plugin import _PluginBase
except ImportError:  # pragma: no cover
    from app.plugins import _PluginBase

try:
    from app.sdk.events import Event, eventmanager
except ImportError:  # pragma: no cover
    from app.core.event import Event, eventmanager

try:
    from app.sdk import scheduler as scheduler_sdk
except ImportError:  # pragma: no cover
    scheduler_sdk = None

try:
    from app.sdk.config import settings
except ImportError:  # pragma: no cover
    try:
        from app.core.config import settings
    except ImportError:
        settings = None

try:
    from app.sdk.logging import logger
except ImportError:  # pragma: no cover
    try:
        from app.log import logger
    except ImportError:
        import logging
        logger = logging.getLogger("subx115")

try:
    from app.schemas.types import NotificationType
    _NT_PLUGIN = getattr(NotificationType, "Plugin", None) \
        or getattr(NotificationType, "Manual", None)
except Exception:  # pragma: no cover
    _NT_PLUGIN = None

from .channels import ADAPTER_CLASSES, ShareInfo, match_subscribes
from .hdhaven import HDHavenSigner, HDHavenSignError, curl_cffi_available
from .p115 import P115Client, P115Error

# 历史记录上限（超过后截断旧记录）
_HISTORY_MAX = 200


class Subx115(_PluginBase):
    """网盘聚合签到：subx 原版 8 个渠道配置页 + 栖影签到 + 自动转存 115。"""

    # ---- 插件元信息（与 package.v3.json 保持一致） ----
    plugin_name = "网盘聚合签到"
    plugin_desc = ("聚影/栖影/剧迷/癫影/观影/帧影/蜗牛4K/权益宝八渠道配置页 + "
                   "栖影（HDHaven）签到，匹配 MoviePilot 订阅并自动转存 115。")
    plugin_icon = "Moviepilot_A.png"        # 占位图标，可替换市场已有图标
    plugin_version = "1.2.1"
    plugin_author = "zf-ui"
    author_url = "https://github.com/zf-ui"
    plugin_config_prefix = "subx115_"
    plugin_order = 50
    auth_level = 1

    # ---- 渠道定义（对应 subx 侧栏的 8 个配置页） ----
    CHANNEL_DEFS = [
        {"key": "juying", "name": "聚影配置", "icon": "mdi-movie-open",
         "tip": "聚影资源站：填写 RSS / 导出接口地址；无 115 分享的消息自动忽略。"},
        {"key": "hdhive", "name": "栖影配置", "icon": "mdi-earth",
         "tip": "栖影（HDHaven）：填写 JSON 接口地址，需要账号时填 Cookie。"},
        {"key": "jumi", "name": "剧迷配置", "icon": "mdi-movie-filter",
         "tip": "剧迷渠道：填写 RSS / 接口地址（一行一个可配多个来源）。"},
        {"key": "dianying", "name": "癫影配置", "icon": "mdi-filmstrip",
         "tip": "癫影渠道：填写 RSS / 接口地址。"},
        {"key": "guanying", "name": "观影配置", "icon": "mdi-cinema",
         "tip": "观影渠道：填写 RSS / 接口地址。"},
        {"key": "zhenying", "name": "帧影配置", "icon": "mdi-image-multiple",
         "tip": "帧影渠道：填写 RSS / 接口地址。"},
        {"key": "woniu", "name": "蜗牛4K", "icon": "mdi-turtle",
         "tip": "蜗牛4K：填写 JSON 接口地址（优先 4K 高画质资源）。"},
        {"key": "quanyi", "name": "权益宝", "icon": "mdi-gift",
         "tip": "权益宝：填写 JSON 接口地址。"},
    ]

    # ---- 运行状态 ----
    _config: dict = {}
    _enabled = False
    _notify = True
    _cron = "0 */2 * * *"
    _p115_cookie = ""
    _p115_cid = "0"

    # ---- 栖影（HDHaven）签到状态 ----
    _sign_enabled = False
    _sign_cookie = ""
    _sign_cron = "30 9 * * *"
    _sign_notify = True
    _sign_use_system_proxy = True
    _sign_proxy = ""
    _sign_lock: threading.Lock = threading.Lock()   # 防并发签到
    _sign_timer: threading.Timer | None = None      # 兜底一次性签到定时器

    # 固定运行参数（不再暴露配置页，需要调整时改这里后重载插件）
    _max_transfer = 10      # 单轮最大转存数（防风控）
    _p115_timeout = 15      # 115 请求超时（秒）

    # =====================================================================
    # 生命周期
    # =====================================================================

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并建立运行状态；要求可重复调用。"""
        config = config or {}
        self._config = config

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._cron = str(config.get("cron") or "0 */2 * * *").strip()
        self._p115_cookie = str(config.get("p115_cookie") or "").strip()
        self._p115_cid = str(config.get("p115_cid") or "0").strip() or "0"

        # 栖影（HDHaven）签到配置
        self._sign_enabled = bool(config.get("hdhive_sign_enabled"))
        self._sign_cookie = str(config.get("hdhive_sign_cookie") or "").strip()
        self._sign_cron = str(
            config.get("hdhive_sign_cron") or "30 9 * * *").strip()
        self._sign_notify = bool(config.get("hdhive_sign_notify", True))
        self._sign_use_system_proxy = bool(
            config.get("hdhive_sign_use_system_proxy", True))
        self._sign_proxy = str(
            config.get("hdhive_sign_proxy") or "").strip()
        if self._sign_enabled and not curl_cffi_available():
            logger.warning("SubX115：宿主缺少 curl_cffi 依赖，栖影签到不可用"
                           "（渠道订阅功能不受影响）")

        # 勾选「保存后立即签到一次」：先清除一次性开关再执行，避免宿主
        # 重启后再次触发
        if config.get("hdhive_sign_onlyonce"):
            config["hdhive_sign_onlyonce"] = False
            try:
                self.update_config(config)
            except Exception:
                logger.warning("SubX115：签到一次性开关持久化失败，未执行签到")
            else:
                self._schedule_sign_once()

        # 启用时轻量校验 115 Cookie（只读接口，失败不阻断加载）
        if self._enabled and self._p115_cookie:
            try:
                client = P115Client(self._p115_cookie, timeout=self._p115_timeout)
                user = client.get_user()
                logger.info(f"SubX115：115 Cookie 有效，用户 {user.get('user_name')}")
            except Exception as exc:
                logger.warning(f"SubX115：115 Cookie 校验未通过：{exc}")

    def get_state(self) -> bool:
        """插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """释放后台资源；要求可重复、安全执行。"""
        if scheduler_sdk is not None:
            for job_id in ("poll_once", "sign_once"):
                try:
                    scheduler_sdk.remove_plugin_once_job(
                        self.__class__.__name__, job_id)
                except Exception:
                    pass
        if self._sign_timer is not None:
            self._sign_timer.cancel()
            self._sign_timer = None
        self._enabled = False

    # =====================================================================
    # 定时服务 / 远程命令 / 插件 API
    # =====================================================================

    def get_service(self) -> list[dict]:
        """启用时注册周期轮询任务与栖影签到任务。"""
        if not self.get_state():
            return []
        services: list[dict] = [{
            "id": "Subx115.Poll",
            "name": "SubX115 渠道订阅轮询",
            "trigger": self._cron_trigger(self._cron, "0 */2 * * *"),
            "func": self._poll,
            "kwargs": {},
        }]
        if self._sign_enabled and self._sign_cookie:
            services.append({
                "id": "Subx115.HdhavenSign",
                "name": "SubX115 栖影（HDHaven）自动签到",
                "trigger": self._cron_trigger(self._sign_cron, "30 9 * * *"),
                "func": self._hdhaven_sign,
                "kwargs": {},
            })
        return services

    @staticmethod
    def _cron_trigger(expr: str, fallback: str) -> CronTrigger:
        """解析 cron 表达式，非法时回退默认并告警。"""
        try:
            return CronTrigger.from_crontab(expr)
        except ValueError:
            logger.warning(f"SubX115：cron 表达式非法（{expr}），回退 {fallback}")
            return CronTrigger.from_crontab(fallback)

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程命令：/subx_run、/subx_status、/subx_sign。"""
        return [
            {
                "cmd": "/subx_run",
                "event": EventType.PluginAction,
                "desc": "立即执行115渠道订阅轮询",
                "category": "插件命令",
                "data": {"action": "subx115_run"},
            },
            {
                "cmd": "/subx_status",
                "event": EventType.PluginAction,
                "desc": "查看115渠道订阅状态",
                "category": "插件命令",
                "data": {"action": "subx115_status"},
            },
            {
                "cmd": "/subx_sign",
                "event": EventType.PluginAction,
                "desc": "立即执行栖影（HDHaven）签到",
                "category": "插件命令",
                "data": {"action": "subx115_sign"},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def run_command(self, event: Event) -> None:
        """处理远程命令事件（只处理本插件 action）。"""
        event_data = event.event_data if event else None
        event_data = event_data or {}
        action = event_data.get("action")
        if action == "subx115_run":
            self._poll()
        elif action == "subx115_status":
            self._send_message("【SubX115】当前状态", self._status_text())
        elif action == "subx115_sign":
            result = self._hdhaven_sign(manual=True)
            self._send_message("【SubX115】栖影签到", str(result.get("message")))

    def get_api(self) -> list[dict[str, Any]]:
        """对外注册只读 API（外部系统用 apikey 鉴权）。"""
        return [
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询 SubX115 转存历史",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询 SubX115 运行状态",
            },
        ]

    # =====================================================================
    # 配置页面（Vuetify：VTabs 垂直侧栏，参照胖虎媒体中心布局）
    # =====================================================================

    @classmethod
    def _channel_fields(cls, ch: dict) -> list[dict]:
        """生成单个渠道配置页的字段。"""
        key = ch["key"]

        def _text(model: str, label: str, **props) -> dict:
            return {"component": "VTextField",
                    "props": {"model": model, "label": label, **props}}

        def _area(model: str, label: str, rows: int = 3) -> dict:
            return {"component": "VTextarea",
                    "props": {"model": model, "label": label, "rows": rows}}

        fields = [
            {
                "component": "VSwitch",
                "props": {"model": f"{key}_enabled", "label": f"启用{ch['name']}",
                          "color": "deep-purple"},
            },
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "density": "compact",
                          "text": ch["tip"]},
            },
            _text(f"{key}_api", "接口地址（API / RSS）",
                  placeholder="https://...  留空则该渠道不参与轮询"),
            _text(f"{key}_cookie", "凭证（Cookie / Token，可选）",
                  type="password", hint="渠道需要登录时填写"),
            _area(f"{key}_channels", "订阅来源（一行一个，可选；与接口地址并用）"),
            _text(f"{key}_target", "转存目录（cid 或目录名，留空用全局）"),
            _area(f"{key}_include", "包含关键词（正则，一行一个，可选）", rows=2),
            _area(f"{key}_exclude", "排除关键词（正则，一行一个，可选）", rows=2),
            _text(f"{key}_limit", "单次处理条数", placeholder="20"),
        ]
        # 栖影（HDHaven）页额外包含签到配置（整合自 hdhavensign 插件）
        if key == "hdhive":
            fields.extend(cls._hdhaven_sign_fields())
        return fields

    @staticmethod
    def _hdhaven_sign_fields() -> list[dict]:
        """栖影（HDHaven）自动签到字段（置于栖影配置页内）。"""
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info", "variant": "tonal", "density": "compact",
                    "text": "栖影签到（hdhaven.com）：登录与人机验证需在"
                            "浏览器完成；每次先查询站点签到状态，仅明确未签到"
                            "才提交请求。需要 curl_cffi 依赖。"},
            },
            {
                "component": "VSwitch",
                "props": {"model": "hdhive_sign_enabled", "label": "启用自动签到",
                          "color": "deep-purple"},
            },
            {
                "component": "VSwitch",
                "props": {"model": "hdhive_sign_onlyonce",
                          "label": "保存后立即签到一次", "color": "deep-purple"},
            },
            {
                "component": "VSwitch",
                "props": {"model": "hdhive_sign_notify", "label": "签到结果通知",
                          "color": "deep-purple"},
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "hdhive_sign_cookie", "label": "栖影登录会话 Cookie",
                    "type": "password",
                    "hint": "浏览器登录 hdhaven.com 后复制的完整 Cookie"
                            "（必须含 hdh_session）；失效后需手动更新"},
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "hdhive_sign_cron",
                    "label": "签到 Cron（默认每天 09:30）",
                    "placeholder": "30 9 * * *"},
            },
            {
                "component": "VSwitch",
                "props": {"model": "hdhive_sign_use_system_proxy",
                          "label": "签到使用系统代理（默认开启）"},
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "hdhive_sign_proxy",
                    "label": "自定义代理（关闭系统代理时使用）"},
            },
        ]

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面 JSON 与默认配置模型（仅 subx 原版的 8 个渠道页）。"""
        tabs: list[dict] = []
        windows: list[dict] = []
        defaults: dict[str, Any] = {
            "enabled": False,
            "notify": True,
            "cron": "0 */2 * * *",
            "p115_cookie": "",
            "p115_cid": "0",
            # 栖影（HDHaven）签到
            "hdhive_sign_enabled": False,
            "hdhive_sign_onlyonce": False,
            "hdhive_sign_notify": True,
            "hdhive_sign_cookie": "",
            "hdhive_sign_cron": "30 9 * * *",
            "hdhive_sign_use_system_proxy": True,
            "hdhive_sign_proxy": "",
        }

        # 8 个渠道页（对应 subx 侧栏，仅此 8 个页签）
        for ch in self.CHANNEL_DEFS:
            tabs.append({
                "component": "VTab",
                "props": {"value": ch["key"], "prepend-icon": ch["icon"],
                          "text": ch["name"]},
            })
            windows.append({
                "component": "VWindowItem",
                "props": {"value": ch["key"]},
                "content": self._channel_fields(ch),
            })
            defaults.update({
                f"{ch['key']}_enabled": False,
                f"{ch['key']}_api": "",
                f"{ch['key']}_cookie": "",
                f"{ch['key']}_channels": "",
                f"{ch['key']}_target": "",
                f"{ch['key']}_include": "",
                f"{ch['key']}_exclude": "",
                f"{ch['key']}_limit": "20",
            })

        defaults["tab"] = "juying"

        form = [{
            "component": "VForm",
            "content": [
                # 顶部全局设置栏（不占渠道页签，渠道页与 subx 原版一致）
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VSwitch", "props": {
                                "model": "enabled", "label": "启用插件",
                                "color": "deep-purple", "density": "compact"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VSwitch", "props": {
                                "model": "notify", "label": "消息通知",
                                "color": "deep-purple", "density": "compact"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VTextField", "props": {
                                "model": "cron", "label": "轮询周期（cron）",
                                "placeholder": "0 */2 * * *",
                                "density": "compact"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VTextField", "props": {
                                "model": "p115_cid", "label": "默认转存目录 cid",
                                "placeholder": "0（根目录）",
                                "density": "compact"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12},
                            "content": [{"component": "VTextField", "props": {
                                "model": "p115_cookie", "label": "115 Cookie",
                                "type": "password", "density": "compact",
                                "hint": "浏览器登录 115 后复制的完整 Cookie，"
                                        "仅用于转存；渠道留空转存目录时落到上面的 cid"}}],
                        },
                    ],
                },
                # 8 个渠道页（VTabs 垂直侧栏，参照 subx 侧栏布局）
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 3},
                            "content": [{
                                "component": "VTabs",
                                "props": {
                                    "model": "tab",
                                    "direction": "vertical",
                                    "color": "deep-purple",
                                    "density": "comfortable",
                                    "align-tabs": "start",
                                },
                                "content": tabs,
                            }],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 9},
                            "content": [{
                                "component": "VWindow",
                                "props": {"model": "tab"},
                                "content": windows,
                            }],
                        },
                    ],
                },
            ],
        }]
        return form, defaults

    # =====================================================================
    # 详情页（统计 + 最近转存）
    # =====================================================================

    def get_page(self) -> list[dict]:
        """插件详情页：运行统计与最近转存记录。"""
        history = self.get_data("history") or []
        total = len(history)
        ok_count = sum(1 for h in history if h.get("ok"))
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = sum(1 for h in history
                          if str(h.get("time", "")).startswith(today))

        summary = (f"累计转存 {total} 次（成功 {ok_count}），今日 {today_count} 次。"
                   f"启用渠道：{self._enabled_channels_text()}")

        page: list[dict] = [{
            "component": "VAlert",
            "props": {"type": "info", "variant": "tonal", "text": summary},
        }]
        # 栖影签到状态
        sign_record = self.get_data("sign_last") or {}
        if self._sign_enabled or sign_record:
            state = {"success": "签到成功", "pending": "结果待核对"}.get(
                str(sign_record.get("state") or ""), "尚无记录")
            page.append({
                "component": "VAlert",
                "props": {
                    "type": "success" if sign_record.get("state") == "success"
                            else "warning",
                    "variant": "tonal", "density": "compact",
                    "text": f"栖影（HDHaven）签到：{state}；"
                            f"日期：{sign_record.get('date', '-')}"},
            })
        if not history:
            page.append({
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal",
                          "text": "暂无转存记录：配置渠道与 115 Cookie 后，"
                                  "等待轮询或发送 /subx_run 立即执行。"},
            })
            return page
        items = []
        for h in reversed(history[-10:]):
            items.append({
                "component": "VListItem",
                "props": {
                    "title": h.get("title", ""),
                    "subtitle": (f"{h.get('time', '')} · 渠道 {h.get('channel', '')}"
                                 f" · {'成功' if h.get('ok') else '失败'}"
                                 f"{(' · ' + h.get('message', '')) if h.get('message') else ''}"),
                    "prepend-icon": "mdi-check-circle" if h.get("ok")
                                    else "mdi-close-circle",
                },
            })
        page.append({"component": "VList", "props": {"lines": "two"},
                     "content": items})
        return page

    # ---- 插件 API 端点 ----------------------------------------------------

    def api_history(self) -> dict:
        """GET /api/v1/plugin/Subx115/history。"""
        return {"code": 0, "data": self.get_data("history") or []}

    def api_status(self) -> dict:
        """GET /api/v1/plugin/Subx115/status。"""
        return {"code": 0, "data": {
            "enabled": self.get_state(),
            "channels": self._enabled_channels_text(),
            "cron": self._cron,
            "hdhaven_sign": {
                "enabled": self._sign_enabled,
                "cron": self._sign_cron,
                "last": self.get_data("sign_last") or {},
            },
            "stats": self._status_text(),
        }}

    # =====================================================================
    # 核心业务
    # =====================================================================

    def _poll(self) -> None:
        """轮询主流程：拉取 -> 匹配 -> 去重 -> 转存 -> 通知。"""
        if not self.get_state():
            return
        logger.info("SubX115：开始渠道轮询")

        # 1) 收集可用渠道适配器
        adapters = []
        for key, adapter_cls in ADAPTER_CLASSES.items():
            adapter = adapter_cls(self._config)
            if adapter.available():
                adapters.append(adapter)
            elif bool(self._config.get(f"{key}_enabled")):
                logger.warning(f"SubX115：渠道 {adapter.name} 已启用但未配置"
                               f"接口地址/来源，跳过")
        if not adapters:
            logger.info("SubX115：没有可用渠道，结束本轮轮询")
            return

        # 2) 拉取最新资源
        shares: list[ShareInfo] = []
        for adapter in adapters:
            try:
                items = adapter.fetch()
                logger.info(f"SubX115：{adapter.name} 拉取到 {len(items)} 条资源")
                shares.extend(items)
            except Exception as exc:
                logger.error(f"SubX115：渠道 {adapter.name} 拉取失败：{exc}")
        if not shares:
            logger.info("SubX115：本轮各渠道均无新资源")
            return

        # 3) 匹配目标（MoviePilot 订阅：片名 + 季）
        subscribes = self._get_subscribes()
        if not subscribes:
            logger.warning("SubX115：未能读取 MoviePilot 订阅列表，本轮跳过匹配")
            return
        history = self.get_data("history") or []
        done_keys = {h.get("key") for h in history}
        matched: list[tuple[ShareInfo, Any]] = []
        for share in shares:
            if share.key in done_keys:
                continue
            hits = match_subscribes(share, subscribes)
            if hits:
                matched.append((share, hits[0]))
        if not matched:
            logger.info("SubX115：本轮没有匹配到订阅的新资源")
            return

        # 4) 转存（限流防风控）
        ok_count, fail_count = 0, 0
        for share, _sub in matched:
            if ok_count >= self._max_transfer:
                logger.info("SubX115：达到单轮最大转存数，剩余顺延下轮")
                break
            ok, message = self._transfer(share)
            if ok:
                ok_count += 1
                history = self._append_history(history, share, True, message)
                if self._notify:
                    self._send_message(
                        "【SubX115】转存成功",
                        f"《{share.title}》\n来源渠道：{share.channel}\n{message}")
            else:
                fail_count += 1
                logger.error(f"SubX115：转存失败 {share.title}：{message}")
        self.save_data("history", history)
        logger.info(f"SubX115：本轮转存成功 {ok_count}，失败 {fail_count}")

    def _transfer(self, share: ShareInfo) -> tuple[bool, str]:
        """把一条资源转存到 115（渠道独立目录 > 全局目录）。"""
        if not self._p115_cookie:
            return False, "未配置 115 Cookie"
        adapter_cls = ADAPTER_CLASSES.get(share.channel)
        target = ""
        if adapter_cls:
            target = str(self._config.get(f"{share.channel}_target") or "").strip()
        cid = target or self._p115_cid or "0"
        try:
            client = P115Client(self._p115_cookie, timeout=self._p115_timeout)
            return client.receive_share(share_code=share.share_code,
                                        password=share.password, cid=cid)
        except P115Error as exc:
            return False, f"115 客户端异常：{exc}"
        except Exception as exc:
            return False, f"转存异常：{exc}"

    # =====================================================================
    # 栖影（HDHaven）签到（移植自 hdhavensign）
    # =====================================================================

    @staticmethod
    def _proxy_from_settings() -> str:
        """读取宿主系统代理配置（兼容 dict / 对象两种形态）。"""
        if settings is None:
            return ""
        raw = getattr(settings, "PROXY", None)
        if isinstance(raw, dict):
            value = raw.get("https") or raw.get("http")
        else:
            value = getattr(raw, "host", None)
        return str(value or os.environ.get("PROXY_HOST")
                   or os.environ.get("https_proxy")
                   or os.environ.get("HTTPS_PROXY") or "").strip()

    def _schedule_sign_once(self) -> None:
        """延迟 3 秒执行一次签到（一次性任务，宿主 SDK 优先，本地定时器兜底）。"""
        if scheduler_sdk is not None:
            try:
                scheduler_sdk.add_plugin_once_job(
                    self.__class__.__name__, "sign_once",
                    lambda: self._hdhaven_sign(manual=True),
                    "SubX115 栖影签到一次", delay_seconds=3)
                return
            except Exception as exc:
                logger.warning(f"SubX115：注册一次性签到任务失败，"
                               f"改用本地定时器：{exc}")
        self._sign_timer = threading.Timer(
            3.0, self._hdhaven_sign, kwargs={"manual": True})
        self._sign_timer.daemon = True
        self._sign_timer.start()

    def _hdhaven_sign(self, manual: bool = False) -> dict:
        """
        签到主流程（线程锁防并发；预检 -> 查状态 -> 提交 -> 保存 -> 通知）。

        安全设计沿用原插件：
        - 提交前写 pending 标记并读回校验（防请求已送达但未记录时重复扣账）；
        - 只有站点明确返回成功才记成功；
        - 网络异常不外抛原文（可能含 Cookie / 代理地址）。
        """
        if not manual and not self._sign_enabled:
            return {"success": False, "message": "栖影签到未启用"}
        if not self._sign_lock.acquire(blocking=False):
            return {"success": False, "message": "签到正在运行"}
        try:
            if not self._sign_cookie:
                raise HDHavenSignError("未配置栖影登录会话 Cookie")
            if not curl_cffi_available():
                raise HDHavenSignError("宿主缺少 curl_cffi 依赖，无法签到")
            today = datetime.now().date().isoformat()
            proxy = (self._proxy_from_settings()
                     if self._sign_use_system_proxy else self._sign_proxy)
            if self._sign_use_system_proxy and not proxy:
                logger.info("SubX115：栖影签到已开启系统代理但未读到代理配置，"
                            "将直连")
            signer = HDHavenSigner(self._sign_cookie, proxy=proxy, timeout=20)

            def _mark_pending() -> None:
                self.save_data("sign_last", {"date": today, "state": "pending"})
                marker = self.get_data("sign_last") or {}
                if (marker.get("date") != today
                        or marker.get("state") != "pending"):
                    raise HDHavenSignError("待核对标记保存失败，未发送签到请求")

            result = signer.signin(mark_pending=_mark_pending)
            if result.get("already"):
                self.save_data("sign_last",
                               {"date": today, "state": "success", "already": True})
                self._sign_notice("【SubX115】栖影签到",
                                  "站点确认今日已签到，不重复提交")
                return {"success": True, "message": "站点确认今日已签到"}
            self.save_data("sign_last", {"date": today, **result})
            saved = self.get_data("sign_last") or {}
            if (saved.get("date") != today
                    or saved.get("state") != "success"):
                raise HDHavenSignError("签到已返回成功，但记录保存失败，请人工核对")
            self._sign_notice(
                "【SubX115】栖影签到成功",
                f"获得积分：{result['earned']}；当前积分：{result['points']}；"
                f"连续签到：{result['streak']}")
            return {"success": True, "message": "签到成功", "data": result}
        except HDHavenSignError as exc:
            logger.warning(f"SubX115：栖影签到未完成：{exc}")
            self._sign_notice("【SubX115】栖影签到待核对", str(exc))
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            # 不输出网络异常原文：可能含 Cookie / 代理地址等敏感字段
            logger.warning(f"SubX115：栖影签到异常：{type(exc).__name__}")
            message = "请求或存储异常，结果可能已提交；请人工核对，勿重试"
            self._sign_notice("【SubX115】栖影签到待核对", message)
            return {"success": False, "message": message}
        finally:
            self._sign_lock.release()

    def _sign_notice(self, title: str, text: str) -> None:
        """签到结果通知（受签到通知开关控制）。"""
        if self._sign_notify:
            self._send_message(title, text)

    # =====================================================================
    # 宿主集成（订阅读取、通知）
    # =====================================================================

    def _get_subscribes(self) -> list:
        """读取 MoviePilot 订阅列表（V3 SDK 优先，失败返回空列表降级）。"""
        try:
            from app.db.oper.subscribe import SubscribeOper  # type: ignore
            return list(SubscribeOper().list() or [])
        except Exception as exc:
            logger.debug(f"SubX115：SubscribeOper 不可用：{exc}")
        try:
            from app.chain.subscribe import SubscribeChain  # type: ignore
            return list(SubscribeChain().list() or [])
        except Exception as exc:
            logger.debug(f"SubX115：SubscribeChain 不可用：{exc}")
        return []

    def _send_message(self, title: str, text: str) -> None:
        """发送消息通知（兼容不同宿主签名）。"""
        try:
            self.post_message(channel=None, mtype=_NT_PLUGIN,
                              title=title, text=text, image=None)
        except TypeError:
            try:
                self.post_message(title=title, text=text)
            except Exception as exc:
                logger.warning(f"SubX115：发送通知失败：{exc}")
        except Exception as exc:
            logger.warning(f"SubX115：发送通知失败：{exc}")

    # =====================================================================
    # 状态与历史
    # =====================================================================

    def _enabled_channels_text(self) -> str:
        names = []
        for key, adapter_cls in ADAPTER_CLASSES.items():
            adapter = adapter_cls(self._config)
            if adapter.available():
                names.append(adapter.name)
        return "、".join(names) if names else "无"

    def _status_text(self) -> str:
        history = self.get_data("history") or []
        ok_count = sum(1 for h in history if h.get("ok"))
        lines = [
            f"插件状态：{'运行中' if self.get_state() else '已停用'}",
            f"轮询周期：{self._cron}",
            f"启用渠道：{self._enabled_channels_text()}",
            f"累计转存：{len(history)} 次（成功 {ok_count}）",
        ]
        if self._sign_enabled:
            record = self.get_data("sign_last") or {}
            state = {"success": "签到成功", "pending": "结果待核对"}.get(
                str(record.get("state") or ""), "尚无记录")
            lines.append(
                f"栖影签到：{self._sign_cron}，{state}（{record.get('date', '-')}）")
        lines.append("发送 /subx_run 立即轮询，/subx_sign 立即签到")
        return "\n".join(lines)

    @staticmethod
    def _append_history(history: list, share: ShareInfo, ok: bool,
                        message: str) -> list:
        """追加成功记录并截断到上限（失败不记，下轮自动重试）。"""
        history.append({
            "key": share.key,
            "channel": share.channel,
            "title": share.title,
            "link": share.link,
            "ok": ok,
            "message": message,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return history[-_HISTORY_MAX:]
