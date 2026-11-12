# -*- coding: utf-8 -*-
"""
SubX115 — 网盘聚合签到（subx 的 MoviePilot 原生实现）
==========================================================

把 subx（115 订阅工具，Docker 镜像 lmhhj/subx）的渠道配置原生搬进 MoviePilot。
配置页侧栏：资源搜索 / 115设置 / 8 个渠道页（聚影 / 栖影 / 剧迷 / 癫影 /
观影 / 帧影 / 蜗牛4K / 权益宝），渠道页版式与 subx 原版一致
（站点地址 / 账号 / 密码 / Cookie / 搜索开关），VTabs 垂直侧栏实现。

运行流程：
  定时轮询（cron） -> 各启用渠道拉取最新 115 分享
  -> 与 MoviePilot 订阅（片名+季）/ 资源搜索关键词匹配 -> 去重
  -> 转存到 115 -> 消息通知
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

from .channels import (ADAPTER_CLASSES, ShareInfo, match_keywords,
                       match_subscribes, regex_match_any, split_lines)
from .hdhaven import HDHavenSigner, HDHavenSignError, curl_cffi_available
from .zhenying import ZhenyingSigner, ZhenyingSignError
from .p115 import P115Client, P115Error

# 历史记录上限（超过后截断旧记录）
_HISTORY_MAX = 200


class Subx115(_PluginBase):
    """网盘聚合签到：资源搜索 + subx 原版 8 渠道配置页 + 栖影签到 + 自动转存。"""

    # ---- 插件元信息（与 package.v3.json 保持一致） ----
    plugin_name = "网盘聚合签到"
    plugin_desc = ("资源搜索 + 聚影/栖影/剧迷/癫影/观影/帧影/蜗牛4K/权益宝"
                   "八渠道配置页 + 栖影（HDHaven）签到，按订阅/关键词"
                   "自动转存 115。")
    plugin_icon = "Moviepilot_A.png"        # 占位图标，可替换市场已有图标
    plugin_version = "1.7.9"
    plugin_author = "z0812"
    author_url = "https://github.com/z0812"
    plugin_config_prefix = "subx115_"
    plugin_order = 50
    auth_level = 1

    # ---- 渠道定义（对应 subx 侧栏的 8 个配置页） ----
    CHANNEL_DEFS = [
        {"key": "juying", "name": "聚影配置", "short": "聚影", "en": "JYing",
         "color": "teal", "icon": "mdi-movie-open", "site": "https://jying.top",
         "tip": "聚影资源站：填写站点地址与账号即可参与搜索。\n"
                "无 115 分享的消息自动忽略。"},
        {"key": "hdhive", "name": "栖影配置", "short": "栖影", "en": "HDHive",
         "color": "green", "icon": "mdi-earth", "site": "https://hdhaven.com",
         "tip": "栖影（HDHaven）：填写站点地址与登录会话 Cookie。\n"
                "（需含 hdh_session）"},
        {"key": "jumi", "name": "剧迷配置", "short": "剧迷", "en": "Gimy",
         "color": "orange", "icon": "mdi-movie-filter", "site": "",
         "tip": "剧迷渠道：填写站点地址与账号。\n"
                "登录需验证码时建议使用浏览器 Cookie。"},
        {"key": "dianying", "name": "癫影配置", "short": "癫影", "en": "Dian115",
         "color": "purple", "icon": "mdi-filmstrip", "site": "https://m.dian115.com",
         "tip": "OpenAPI Key 优先用于癫影搜索、解锁和签到；没有 Key 时可填写"
                "浏览器 Cookie 作为普通网页模式兜底，遇到站点防护会提示重新登录。\n"
                "普通 Cookie 模式只提取网页已返回的 115、磁力、ed2k 明文链接。"},
        {"key": "guanying", "name": "观影配置", "short": "观影", "en": "GuanYing",
         "color": "blue", "icon": "mdi-cinema", "site": "",
         "tip": "观影渠道：填写站点地址与账号。\n"
                "后台会自动选择可用镜像并刷新登录态。"},
        {"key": "zhenying", "name": "帧影配置", "short": "帧影", "en": "FrameHDR",
         "color": "indigo", "icon": "mdi-image-multiple", "site": "https://framehdr.com",
         "tip": "帧影渠道：填写站点地址与账号。\n"
                "搜索读取详情页的 115、磁力和 ed2k 链接。"},
        {"key": "woniu", "name": "蜗牛4K", "short": "蜗牛4K", "en": "WN4K",
         "color": "green", "icon": "mdi-turtle", "site": "",
         "tip": "蜗牛4K：填写站点地址与账号。\n"
                "优先收录 4K 高画质资源。"},
        {"key": "quanyi", "name": "权益宝", "short": "权益宝", "en": "QuanYiBao",
         "color": "orange", "icon": "mdi-gift", "site": "",
         "tip": "权益宝：填写站点地址与账号。\n"
                "先匹配 TMDb 再检索站内资源。"},
    ]

    # ---- 运行状态 ----
    _config: dict = {}
    _enabled = False
    _notify = True
    _cron = "0 */2 * * *"
    _p115_cookie = ""
    _p115_cid = "0"
    _keywords: list[str] = []          # 资源搜索页配置的关键词
    _search_include: list[str] = []    # 资源搜索页：包含关键词（正则）
    _search_exclude: list[str] = []    # 资源搜索页：排除关键词（正则）

    # ---- 栖影（HDHaven）签到状态 ----
    _sign_enabled = False
    _sign_cookie = ""
    _sign_cron = "30 9 * * *"
    _sign_notify = True
    _sign_use_system_proxy = True
    _sign_proxy = ""
    _sign_lock: threading.Lock = threading.Lock()   # 防并发签到
    _sign_timer: threading.Timer | None = None      # 兜底一次性签到定时器

    # ---- 帧影（FrameHDR）签到状态 ----
    _zy_sign_enabled = False
    _zy_sign_cookie = ""
    _zy_sign_cron = "18 9 * * *"
    _zy_sign_notify = True
    _zy_username = ""
    _zy_password = ""
    _zy_sign_lock: threading.Lock = threading.Lock()   # 防并发签到
    _zy_sign_timer: threading.Timer | None = None      # 兜底一次性签到定时器

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
        self._keywords = split_lines(config.get("search_keywords"))
        self._search_include = split_lines(config.get("search_include"))
        self._search_exclude = split_lines(config.get("search_exclude"))

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

        # 帧影（FrameHDR）签到配置
        self._zy_sign_enabled = bool(config.get("zhenying_sign_enabled"))
        self._zy_sign_cookie = str(
            config.get("zhenying_sign_cookie") or "").strip()
        self._zy_sign_cron = str(
            config.get("zhenying_sign_cron") or "18 9 * * *").strip()
        self._zy_sign_notify = bool(config.get("zhenying_sign_notify", True))
        # 账号密码兜底登录（Cookie 失效时自动重新登录）
        self._zy_username = str(config.get("zhenying_username") or "").strip()
        self._zy_password = str(config.get("zhenying_password") or "").strip()

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

        # 帧影「保存后立即签到一次」
        if config.get("zhenying_sign_onlyonce"):
            config["zhenying_sign_onlyonce"] = False
            try:
                self.update_config(config)
            except Exception:
                logger.warning("SubX115：帧影签到一次性开关持久化失败，未执行签到")
            else:
                self._schedule_zy_sign_once()

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
            for job_id in ("poll_once", "sign_once", "zy_sign_once"):
                try:
                    scheduler_sdk.remove_plugin_once_job(
                        self.__class__.__name__, job_id)
                except Exception:
                    pass
        if self._sign_timer is not None:
            self._sign_timer.cancel()
            self._sign_timer = None
        if self._zy_sign_timer is not None:
            self._zy_sign_timer.cancel()
            self._zy_sign_timer = None
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
        if self._zy_sign_enabled and self._zy_sign_cookie:
            services.append({
                "id": "Subx115.ZhenyingSign",
                "name": "SubX115 帧影（FrameHDR）自动签到",
                "trigger": self._cron_trigger(self._zy_sign_cron, "18 9 * * *"),
                "func": self._zhenying_sign,
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
        """对外注册 API（配置页按钮 / 外部系统调用）。"""
        return [
            {
                "path": "/sign",
                "endpoint": self.api_sign,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "立即执行栖影（HDHaven）签到",
            },
            {
                "path": "/test_login",
                "endpoint": self.api_test_login,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "测试栖影（HDHaven）登录态",
            },
            {
                "path": "/zhenying_sign",
                "endpoint": self.api_zhenying_sign,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "立即执行帧影（FrameHDR）签到",
            },
            {
                "path": "/zhenying_test_login",
                "endpoint": self.api_zhenying_test_login,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "测试帧影（FrameHDR）登录态",
            },
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
    def _action_btn(cls, text: str, api: str, color: str = "deep-purple",
                    variant: str = "tonal", extra_class: str = "",
                    icon: str = "") -> dict:
        """生成一个点击后调插件 API 的操作按钮（onclick 调 window.MoviePilotAPI）。

        配置页（get_form）的 VBtn 无法像详情页那样通过 events 调后端 API，
        但可通过 onclick（FormRender 会把 on 开头的字符串转成函数执行）调用
        前端全局注入的 window.MoviePilotAPI（保留完整响应的 pluginApi，自动
        带 Bearer token）。

        点击后结果以内联提示条显示在按钮下方（绿色=成功、红色=失败），
        而非浏览器 alert 弹窗。
        """
        plugin = cls.__name__
        props = {
            "color": color, "variant": variant,
            "size": "small", "class": extra_class,
            "onclick": (
                "(e) => { window.MoviePilotAPI.get('plugin/" + plugin
                + "/" + api + "').then(function(r){ "
                "var ok = r && (r.success === true || r.ok === true);"
                "var msg = (r && r.message) || (ok ? '操作成功' : '操作失败');"
                "var old = document.querySelector('.subx-inline-status');"
                "if (old) { old.parentNode.removeChild(old); }"
                "var d = document.createElement('div');"
                "d.className = 'subx-inline-status';"
                "d.style.cssText = 'border-radius:6px;padding:8px 14px;"
                "margin:0 12px 8px 0;font-size:13px;line-height:1.5;"
                "word-break:break-all;' + (ok ? "
                "'background:#e8f5e9;color:#1b5e20;border:1px solid #a5d6a7;' : "
                "'background:#fdecea;color:#b71c1c;border:1px solid #f5c6cb;');"
                "d.textContent = msg;"
                "var row = e.target;"
                "while (row && row.parentNode && "
                "row.className && row.className.indexOf && "
                "row.className.indexOf('v-row') === -1 && "
                "row.parentNode !== document.body) { row = row.parentNode; }"
                "if (row && row.parentNode) { "
                "row.parentNode.insertBefore(d, row.nextSibling); }"
                "else { document.body.appendChild(d); }"
                "}).catch(function(){ }); }"
            ),
        }
        if icon:
            props["prepend-icon"] = icon
        return {"component": "VBtn", "props": props, "text": text}

    @classmethod
    def _site_action_btn_list(cls, key: str, has_sign: bool) -> list[dict]:
        """渠道页操作按钮列表（立即签到 + 测试登录）。

        帧影（zhenying）走独立的 zhenying_sign / zhenying_test_login 端点，
        其余渠道沿用默认 sign / test_login（当前仅栖影真正实现签到）。
        """
        buttons = []
        if key == "zhenying":
            if has_sign:
                buttons.append(cls._action_btn("立即签到", "zhenying_sign"))
            buttons.append(cls._action_btn("测试登录", "zhenying_test_login",
                                           "grey", "outlined",
                                           "ml-2" if has_sign else ""))
            return buttons
        if has_sign:
            buttons.append(cls._action_btn("立即签到", "sign"))
        buttons.append(cls._action_btn("测试登录", "test_login", "grey",
                                       "outlined", "ml-2" if has_sign else ""))
        return buttons

    @classmethod
    def _channel_header(cls, ch: dict) -> list[dict]:
        """渠道页顶部：操作按钮在上（右对齐），提示条在下，间距拉大。"""
        key = ch["key"]
        # 聚影页：右上角单个「测试聚影账号」按钮，无顶部提示条（subx 原版）
        if key == "juying":
            return [{
                "component": "VRow",
                "props": {"justify": "end", "align": "center",
                          "class": "pt-6 mb-4 pr-3"},
                "content": [cls._action_btn(
                    "测试聚影账号", "test_login", "green", "outlined",
                    icon="mdi-account-check")],
            }]
        # 仅「观影」无签到功能，只显示「测试登录」
        has_sign = key != "guanying"
        # 按钮行在上（右对齐），用 pt-6 内边距把按钮从面板顶边推下来，避免贴顶被裁切
        header = [{
            "component": "VRow",
            "props": {"justify": "end", "align": "center",
                      "class": "pt-6 mb-3 pr-3"},
            "content": cls._site_action_btn_list(key, has_sign),
        }]
        # 帧影不显示顶部 info 提示条（用户要求移除）；其余渠道显示单行提示
        if key != "zhenying":
            tip = ch.get("tip", "").replace("\n", " ")
            header.append({
                "component": "VAlert",
                "props": {
                    "type": "info", "variant": "tonal", "density": "compact",
                    "text": tip, "class": "mb-4",
                },
            })
        return header

    @classmethod
    def _channel_fields(cls, ch: dict) -> list[dict]:
        """按渠道分发到各自专属字段方法（版式对应 subx 原版各渠道页）。

        所有渠道页顶部统一加「操作按钮 + 提示」header（见 _channel_header）。
        """
        key = ch["key"]
        builder = {
            "juying": cls._juying_fields,
            "jumi": cls._jumi_fields,
            "guanying": cls._guanying_fields,
            "zhenying": cls._zhenying_fields,
            "woniu": cls._woniu_fields,
            "quanyi": cls._quanyi_fields,
        }.get(key)
        if builder is not None:
            body = builder(ch)
        else:
            body = cls._default_channel_fields(ch)
        return cls._channel_header(ch) + body

    # ---- 表单构造 helper ---------------------------------------------------

    @staticmethod
    def _f_text(model: str, label: str, **props) -> dict:
        return {"component": "VTextField",
                "props": {"model": model, "label": label,
                          "class": "mb-2", **props}}

    @staticmethod
    def _f_area(model: str, label: str, rows: int = 3, **props) -> dict:
        return {"component": "VTextarea",
                "props": {"model": model, "label": label, "rows": rows,
                          "class": "mb-2", **props}}

    @staticmethod
    def _f_col(cols: int, md: int, node: dict) -> dict:
        return {"component": "VCol", "props": {"cols": cols, "md": md},
                "content": [node]}

    @staticmethod
    def _f_header(text: str) -> dict:
        # 分区标题：顶层 text 渲染为插槽内容（FormRender 通用机制）
        # pt-1 缩小标题与上方内容的距离，pb-3 拉开标题与下方输入框的间距
        return {"component": "VCardTitle",
                "props": {"class": "px-0 pt-1 pb-3 mt-1"}, "text": text}

    @staticmethod
    def _f_switch(model: str, label: str, color: str = "deep-purple") -> dict:
        return {"component": "VSwitch",
                "props": {"model": model, "label": label, "color": color,
                          "class": "mb-2"}}

    @classmethod
    def _default_channel_fields(cls, ch: dict) -> list[dict]:
        """未单独定制的渠道（栖影/癫影）使用的通用版式。"""
        key = ch["key"]
        short = ch.get("short", ch["name"])

        def _text(model: str, label: str, **props) -> dict:
            return cls._f_text(model, label, **props)

        def _area(model: str, label: str, rows: int = 3) -> dict:
            return cls._f_area(model, label, rows)

        def _col(cols: int, md: int, node: dict) -> dict:
            return cls._f_col(cols, md, node)

        fields = [
            # 站点地址（顶部 header 已含操作按钮与提示，此处只留站点地址）
            {
                "component": "VRow",
                "content": [
                    _col(12, 12, _text(f"{key}_site", "站点地址",
                                      placeholder="https://...")),
                ],
            },
            cls._f_header("账号认证"),
            {
                "component": "VRow",
                "content": [
                    _col(12, 6, _text(f"{key}_username", f"{short}账号 / 用户名",
                                      placeholder=f"{short}账号")),
                    _col(12, 6, _text(f"{key}_password", f"{short}密码",
                                      type="password", placeholder=f"{short}密码")),
                ],
            },
            *([] if key == "dianying" else
              [_area(f"{key}_cookie",
                     f"{short} Cookie（可选，浏览器登录后复制）", rows=2)]),
            cls._f_header("搜索与设置"),
            {
                "component": "VRow",
                "content": [
                    _col(12, 6, cls._f_switch(f"{key}_enabled",
                                              f"启用{short}搜索",
                                              ch.get("color", "deep-purple"))),
                    _col(12, 6, _text(f"{key}_limit", "单次处理条数",
                                      placeholder="20")),
                ],
            },
        ]
        # 栖影（HDHaven）页额外包含签到配置（整合自 hdhavensign 插件）
        if key == "hdhive":
            fields.extend(cls._hdhaven_sign_fields())
        # 癫影页：subx 原版字段（OpenAPI Key / UA / 保活 / 签到 / 代理）
        if key == "dianying":
            fields.extend(cls._dianying_fields())
        return fields

    # ---- 各渠道专属字段（版式对应 subx 原版各渠道配置页） -------------------

    @classmethod
    def _juying_fields(cls, ch: dict) -> list[dict]:
        """聚影：App Key / 账号密码 / 签到（普通|幸运）+ 抽奖（对齐 subx 原版）。"""
        c = ch["color"]
        return [
            cls._f_text("juying_apikey", "聚影 App Key",
                        placeholder="填入你的聚影 App Key"),
            # 账号 + 密码 左右并排
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_text(
                        "juying_username", "聚影账号", placeholder="用户名")),
                    cls._f_col(12, 6, cls._f_text(
                        "juying_password", "聚影密码", type="password",
                        placeholder="密码")),
                ],
            },
            # 启用搜索 + 启用每日签到 左右并排
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_switch(
                        "juying_enabled", "启用聚影搜索", c)),
                    cls._f_col(12, 6, cls._f_switch(
                        "juying_sign_enabled", "启用每日自动签到", c)),
                ],
            },
            cls._f_text("juying_sign_cron", "签到 Cron 表达式",
                        placeholder="12 8 * * *",
                        hint="默认每天 08:12 执行一次，按北京时间"
                             "（Asia/Shanghai）解释。"),
            {"component": "VDivider", "props": {"class": "my-3"}},
            # ---- 签到设置 ----
            cls._f_header("签到设置"),
            {
                "component": "VRow",
                "props": {"align": "center"},
                "content": [
                    cls._f_col(12, 4, {
                        "component": "div",
                        "props": {"class": "text-body-2 text-medium-emphasis"},
                        "text": "签到方式",
                    }),
                    cls._f_col(12, 8, {
                        "component": "VBtnToggle",
                        "props": {
                            "model": "juying_sign_mode",
                            "mandatory": True,
                            "variant": "outlined",
                            "color": c,
                            "density": "comfortable",
                        },
                        "content": [
                            {"component": "VBtn", "props": {"value": "normal"},
                             "text": "普通签到"},
                            {"component": "VBtn", "props": {"value": "lucky"},
                             "text": "幸运签到"},
                        ],
                    }),
                ],
            },
            cls._f_switch("juying_sign_draw", "签到后自动抽奖", c),
            {
                "component": "VRow",
                "props": {"class": "mt-2"},
                "content": [
                    cls._action_btn("立即签到", "sign", "deep-purple",
                                    "outlined", "mr-2", "mdi-check"),
                    cls._action_btn("奖池抽奖", "sign", "deep-purple",
                                    "outlined", "mr-2", "mdi-gift"),
                    cls._action_btn("签到与抽奖记录", "history", "grey",
                                    "text", "", "mdi-history"),
                ],
            },
            {
                "component": "VAlert",
                "props": {"type": "error", "variant": "tonal",
                          "density": "compact", "class": "mt-2",
                          "text": "抽奖：聚影账号密码未配置"},
            },
            {
                "component": "VBtn",
                "props": {"color": "deep-purple", "block": True,
                          "class": "mt-2",
                          "prepend-icon": "mdi-content-save"},
                "text": "保存聚影配置",
            },
        ]

    @classmethod
    def _jumi_fields(cls, ch: dict) -> list[dict]:
        """剧迷：站点地址 / 用户名密码 / Cookie / 显示标签 + 签到。"""
        c = ch["color"]
        return [
            {
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal",
                          "density": "compact", "class": "mb-3",
                          "text": "剧迷登录需要验证码，自动签到建议使用浏览器登录后"
                                  "的 Cookie；若后端或 Docker 网络被 Cloudflare"
                                  "拦截，搜索和签到会返回防护提示。"},
            },
            cls._f_header("账号认证"),
            cls._f_text("jumi_site", "站点地址",
                        placeholder="https://www.gimy115.top/"),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_text(
                        "jumi_username", "用户名", placeholder="剧迷账号")),
                    cls._f_col(12, 6, cls._f_text(
                        "jumi_password", "密码", type="password",
                        placeholder="剧迷密码")),
                ],
            },
            cls._f_area("jumi_cookie", "Cookie", rows=3,
                        placeholder="PHPSESSID=...; ...",
                        hint="在浏览器登录剧迷后，从请求头复制 Cookie；账号密码"
                             "因验证码限制通常不能直接自动登录。"),
            cls._f_header("搜索与签到"),
            cls._f_switch("jumi_show_tag", "在影视资源站中显示“剧迷”标签", c),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_switch(
                        "jumi_sign_enabled", "启用每日自动签到", c)),
                    cls._f_col(12, 6, cls._f_text(
                        "jumi_sign_cron", "签到 Cron 表达式",
                        placeholder="15 9 * * *",
                        hint="默认每天 09:15 执行一次，按北京时间"
                             "（Asia/Shanghai）解释。")),
                ],
            },
        ]

    @classmethod
    def _guanying_fields(cls, ch: dict) -> list[dict]:
        """观影：用户名密码 / Cookie / 显示标签（镜像自动切换 + 保活）。"""
        c = ch["color"]
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal",
                          "density": "compact", "class": "mb-3",
                          "text": "观影会自动选择可用镜像并在后台刷新登录态；搜索会优先"
                                  "使用上次成功的镜像，失败时再尝试备用地址。"},
            },
            cls._f_header("账号认证"),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_text(
                        "guanying_username", "用户名", placeholder="观影账号")),
                    cls._f_col(12, 6, cls._f_text(
                        "guanying_password", "密码", type="password",
                        placeholder="观影密码")),
                ],
            },
            cls._f_area("guanying_cookie", "Cookie", rows=3,
                        placeholder="浏览器登录观影后，从请求头复制 Cookie；"
                                    "遇到验证码或风控时可使用",
                        hint="账号密码登录遇到验证码时，可手动填 Cookie；保存后"
                             "搜索和后台保活会优先使用 Cookie。"),
            cls._f_header("搜索与保活"),
            cls._f_switch("guanying_show_tag", "在影视资源站中显示“观影”标签", c),
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal",
                          "density": "compact",
                          "text": "测试登录态成功后，系统会保存后台 Cookie；定时保活"
                                  "会继续刷新 Cookie。验证码场景可用上方 Cookie"
                                  "兜底。"},
            },
        ]

    @classmethod
    def _zhenying_fields(cls, ch: dict) -> list[dict]:
        """帧影：站点地址 / 邮箱密码 / Cookie / 显示标签 + 签到。"""
        c = ch["color"]
        return [
            cls._f_header("账号认证"),
            cls._f_text("zhenying_site", "站点地址",
                        placeholder="https://framehdr.com/"),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_text(
                        "zhenying_username", "用户名 / 邮箱",
                        placeholder="帧影账号邮箱",
                        hint="Cookie 失效时自动重新登录兜底用")),
                    cls._f_col(12, 6, cls._f_text(
                        "zhenying_password", "密码", type="password",
                        placeholder="帧影密码",
                        hint="Cookie 失效时自动重新登录兜底用")),
                ],
            },
            cls._f_header("搜索与签到"),
            cls._f_switch("zhenying_show_tag", "在影视资源站中显示“帧影”标签", c),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_switch(
                        "zhenying_sign_enabled", "启用每日自动签到", c)),
                    cls._f_col(12, 6, cls._f_text(
                        "zhenying_sign_cron", "签到 Cron 表达式",
                        placeholder="18 9 * * *",
                        hint="默认每天 09:18 执行一次，按北京时间"
                             "（Asia/Shanghai）解释。")),
                ],
            },
            # ---- 帧影签到设置 ----
            {
                "component": "VDivider", "props": {"class": "my-2"},
            },
            cls._f_header("帧影签到设置"),
            cls._f_text("zhenying_sign_cookie", "帧影登录会话 Cookie",
                        type="password",
                        placeholder="_security_token=...;",
                        hint="浏览器登录 framehdr.com 后复制的完整 Cookie；"
                             "Cookie 失效时自动使用账号密码重新登录获取。"),
            cls._f_switch("zhenying_sign_notify", "签到结果通知", c),
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal",
                          "density": "compact", "class": "mt-3",
                          "text": "优先使用 Cookie；Cookie 失效时自动使用账号密码"
                                  "重新登录获取。"},
            },
        ]

    @classmethod
    def _woniu_fields(cls, ch: dict) -> list[dict]:
        """蜗牛4K：账号认证 | 搜索与签到（两分区卡片）。"""
        c = ch["color"]
        return [
            cls._f_header("账号认证"),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_text("woniu_username", "用户名")),
                    cls._f_col(12, 6, cls._f_text(
                        "woniu_password", "密码", type="password",
                        placeholder="填写登录密码")),
                ],
            },
            cls._f_area("woniu_cookie", "Cookie（可选）", rows=3,
                        placeholder="浏览器 Cookie"),
            cls._f_header("搜索与签到"),
            cls._f_switch("woniu_show_tag", "在影视资源站中显示", c),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_switch(
                        "woniu_sign_enabled", "自动签到", c)),
                    cls._f_col(12, 6, cls._f_text(
                        "woniu_sign_cron", "签到 Cron",
                        placeholder="20 8 * * *")),
                ],
            },
        ]

    @classmethod
    def _quanyi_fields(cls, ch: dict) -> list[dict]:
        """权益宝：账号认证 | 搜索与签到（邮箱用户名 / 密码 / Cookie）。"""
        c = ch["color"]
        return [
            cls._f_header("账号认证"),
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_text("quanyi_username", "邮箱 / 用户名")),
                    cls._f_col(12, 6, cls._f_text(
                        "quanyi_password", "密码", type="password",
                        placeholder="填写登录密码")),
                ],
            },
            cls._f_area("quanyi_cookie", "Cookie（可选）", rows=3,
                        placeholder="可补充浏览器登录后的完整 Cookie",
                        hint="先保存并测试账号。如遇网站登录验证，可在浏览器完成"
                             "验证后补充 Cookie。密码和 Cookie 保存后不会回填。"),
            cls._f_header("搜索与签到"),
            cls._f_switch("quanyi_show_tag", "在影视资源站中显示", c),
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal",
                          "density": "compact",
                          "text": "先匹配 TMDb 影视信息，再检索站内资源；只展示 115"
                                  "分享、115 秒传、磁力和 ED2K 链接。115 分享、"
                                  "磁力和 ED2K 可转存；秒传链接请复制后使用秒传"
                                  "工具。需积分的资源会先显示价格，由你确认解锁。"
                                  "也可从「资源搜索 → 资源站聚合」选择权益宝。"},
            },
            {
                "component": "VRow",
                "content": [
                    cls._f_col(12, 6, cls._f_switch(
                        "quanyi_sign_enabled", "自动签到", c)),
                    cls._f_col(12, 6, cls._f_text(
                        "quanyi_sign_cron", "签到 Cron",
                        placeholder="25 8 * * *",
                        hint="默认每天 08:25，按 SubX 配置的时区执行。")),
                ],
            },
        ]

    @staticmethod
    def _dianying_fields() -> list[dict]:
        """癫影（Dian115）专属字段，版式对应 subx 原版癫影配置页。"""
        return [
            {
                "component": "VTextarea",
                "props": {"model": "dianying_apikey", "label": "OpenAPI Key",
                          "rows": 2, "class": "mb-4",
                          "placeholder": "dys_xxxxxxxxxxxxxxxx",
                          "hint": "VIP 或开发者账号建议使用 OpenAPI Key；"
                                  "普通账号可使用 Cookie 模式进行搜索、解锁和签到"},
            },
            {
                "component": "VTextarea",
                "props": {"model": "dianying_cookie", "label": "Cookie",
                          "rows": 3, "class": "mb-4",
                          "placeholder": "浏览器正常登录癫影后，从请求头复制"
                                         " Cookie；不会自动绕过站点防护",
                          "hint": "普通 Cookie 模式只提取网页已返回的 115、"
                                  "磁力、ed2k 明文链接；如果触发验证，需要先在"
                                  "浏览器重新登录再复制 Cookie"},
            },
            {
                "component": "VTextField",
                "props": {"model": "dianying_useragent",
                          "label": "User-Agent（可选）", "class": "mb-4",
                          "placeholder": "默认（Windows Chrome 124）",
                          "hint": "与复制 Cookie 的浏览器 UA 保持一致可提高通过率"
                                  "（站点防护 Cookie 通常绑定 UA）；不确定时保持"
                                  "默认即可"},
            },
            {
                "component": "VRow",
                "props": {"class": "mb-4"},
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 6},
                        "content": [{
                            "component": "VSwitch",
                            "props": {"model": "dianying_show_tag",
                                      "label": "在影视资源站中显示“癫影”标签",
                                      "color": "purple"},
                        }],
                    },
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 6},
                        "content": [{
                            "component": "VSwitch",
                            "props": {"model": "dianying_keepalive",
                                      "label": "启用 Cookie 保活刷新（每 6 小时）",
                                      "color": "purple"},
                        }],
                    },
                ],
            },
            {
                "component": "VSwitch",
                "props": {"model": "dianying_sign_enabled",
                          "label": "启用每日自动签到", "color": "purple",
                          "class": "mb-2"},
            },
            {
                "component": "VTextField",
                "props": {"model": "dianying_sign_cron",
                          "label": "签到 Cron 表达式", "class": "mb-4",
                          "placeholder": "10 8 * * *",
                          "hint": "默认每天 08:10 执行一次，按北京时间"
                                  "（Asia/Shanghai）解释"},
            },
            {
                "component": "VSelect",
                "props": {"model": "dianying_sign_mode",
                          "label": "签到模式", "class": "mb-4",
                          "items": [
                              {"title": "普通签到", "value": "normal"},
                              {"title": "运气签到", "value": "lucky"},
                          ],
                          "hint": "普通签到固定奖励；运气签到按癫影接口返回"
                                  "随机倍数"},
            },
            # ---- 代理 ----
            {"component": "VCardTitle", "props": {"class": "px-0 py-2"},
             "text": "代理"},
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 4},
                        "content": [{
                            "component": "VSwitch",
                            "props": {"model": "dianying_proxy_enabled",
                                      "label": "启用代理",
                                      "color": "purple"},
                        }],
                    },
                    {
                        "component": "VCol", "props": {"cols": 12, "md": 8},
                        "content": [{
                            "component": "VTextField",
                            "props": {"model": "dianying_proxy",
                                      "label": "代理地址",
                                      "placeholder": "http://127.0.0.1:7890",
                                      "hint": "启用后癫影渠道的搜索请求"
                                              "走该代理"},
                        }],
                    },
                ],
            },
        ]

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
                          "color": "green", "class": "mb-2"},
            },
            {
                "component": "VSwitch",
                "props": {"model": "hdhive_sign_onlyonce",
                          "label": "保存后立即签到一次", "color": "green",
                          "class": "mb-2"},
            },
            {
                "component": "VSwitch",
                "props": {"model": "hdhive_sign_notify", "label": "签到结果通知",
                          "color": "green", "class": "mb-4"},
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "hdhive_sign_cookie", "label": "栖影登录会话 Cookie",
                    "type": "password", "class": "mb-4",
                    "hint": "浏览器登录 hdhaven.com 后复制的完整 Cookie"
                            "（必须含 hdh_session）；失效后需手动更新"},
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "hdhive_sign_cron",
                    "label": "签到 Cron（默认每天 09:30）",
                    "class": "mb-4",
                    "placeholder": "30 9 * * *"},
            },
            {
                "component": "VSwitch",
                "props": {"model": "hdhive_sign_use_system_proxy",
                          "label": "签到使用系统代理（默认开启）",
                          "color": "green", "class": "mb-4"},
            },
            {
                "component": "VTextField",
                "props": {
                    "model": "hdhive_sign_proxy",
                    "class": "mb-4",
                    "label": "自定义代理（关闭系统代理时使用）"},
            },
        ]

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面 JSON 与默认配置模型（资源搜索 + 115设置 + 8 渠道页）。"""
        tabs: list[dict] = []
        windows: list[dict] = []
        defaults: dict[str, Any] = {
            "enabled": False,
            "notify": True,
            "cron": "0 */2 * * *",
            "p115_cookie": "",
            "p115_cid": "0",
            "search_keywords": "",
            "search_include": "",
            "search_exclude": "",
            # 栖影（HDHaven）签到
            "hdhive_sign_enabled": False,
            "hdhive_sign_onlyonce": False,
            "hdhive_sign_notify": True,
            "hdhive_sign_cookie": "",
            "hdhive_sign_cron": "30 9 * * *",
            "hdhive_sign_use_system_proxy": True,
            "hdhive_sign_proxy": "",
        }

        # ---- 页签 1：资源搜索（侧栏最上面） ----
        # 注意：MoviePilot 渲染器把节点顶层 text 渲染为插槽内容（见
        # FormRender.vue 的 {{ formItem.text }}），VTab 文字必须用顶层
        # text 而不是 props.text，否则在部分宿主上不显示。
        tabs.append({
            "component": "VTab",
            "props": {"value": "search", "prepend-icon": "mdi-magnify",
                      "style": {"text-transform": "none"}},
            "text": "资源搜索",
        })
        site_checks: list[dict] = []
        for ch in self.CHANNEL_DEFS:
            defaults[f"search_{ch['key']}"] = True
            site_checks.append({
                "component": "VCol",
                "props": {"cols": 6, "md": 3},
                "content": [{
                    "component": "VCheckbox",
                    "props": {"model": f"search_{ch['key']}",
                              "label": f"{ch['short']} {ch['en']}",
                              "color": ch["color"], "density": "compact"},
                }],
            })
        windows.append({
            "component": "VWindowItem",
            "props": {"value": "search"},
            "content": [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal",
                              "density": "compact",
                              "text": "填写订阅关键词（一行一个）并勾选搜索站点后，"
                                      "定时轮询会在勾选的站点中按关键词匹配资源"
                                      "并自动转存 115；与 MoviePilot 订阅监控"
                                      "并用，命中任一即转存。"},
                },
                {
                    "component": "VTextarea",
                    "props": {"model": "search_keywords", "rows": 4,
                              "label": "搜索关键词（一行一个，留空则仅按订阅匹配）",
                              "placeholder": "流浪地球 3\n三体 第二季"},
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextarea",
                                "props": {"model": "search_include", "rows": 3,
                                          "label": "包含关键词（正则，一行一个，可选）",
                                          "placeholder": "4K\nHDR"},
                            }],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextarea",
                                "props": {"model": "search_exclude", "rows": 3,
                                          "label": "排除关键词（正则，一行一个，可选）",
                                          "placeholder": "预告\n花絮"},
                            }],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol", "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {"type": "success", "variant": "tonal",
                                          "density": "compact",
                                          "text": "搜索站点 8/8：勾选参与"
                                                  "关键词搜索的站点（默认全部）"},
                            }],
                        },
                        *site_checks,
                    ],
                },
            ],
        })

        # ---- 页签 2：115设置（全局参数，对应 subx「基础与账号」） ----
        tabs.append({
            "component": "VTab",
            "props": {"value": "p115", "prepend-icon": "mdi-cloud-outline",
                      "style": {"text-transform": "none"}},
            "text": "115设置",
        })
        windows.append({
            "component": "VWindowItem",
            "props": {"value": "p115"},
            "content": [
                {
                    "component": "VAlert",
                    "props": {"type": "warning", "variant": "tonal",
                              "density": "compact",
                              "text": "115 Cookie 属账号登录凭证，切勿外发；"
                                      "失效后需在浏览器重新登录并复制。"},
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VSwitch", "props": {
                                "model": "enabled", "label": "启用插件",
                                "color": "deep-purple"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VSwitch", "props": {
                                "model": "notify", "label": "消息通知",
                                "color": "deep-purple"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VTextField", "props": {
                                "model": "cron", "label": "轮询周期（cron）",
                                "placeholder": "0 */2 * * *"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12, "md": 3},
                            "content": [{"component": "VTextField", "props": {
                                "model": "p115_cid", "label": "转存目录 cid",
                                "placeholder": "0（根目录）"}}],
                        },
                        {
                            "component": "VCol", "props": {"cols": 12},
                            "content": [{"component": "VTextField", "props": {
                                "model": "p115_cookie", "label": "115 Cookie",
                                "type": "password",
                                "hint": "浏览器登录 115 后复制的完整 Cookie，"
                                        "仅用于转存；渠道留空转存目录时落到上面的 cid"}}],
                        },
                    ],
                },
            ],
        })

        # ---- 页签 3+：8 个渠道页（对应 subx 侧栏「资源网站」） ----
        for ch in self.CHANNEL_DEFS:
            tabs.append({
                "component": "VTab",
                "props": {"value": ch["key"], "prepend-icon": ch["icon"],
                          "color": ch["color"],
                          "style": {"text-transform": "none"}},
                "text": ch["name"],
            })
            windows.append({
                "component": "VWindowItem",
                "props": {"value": ch["key"]},
                "content": self._channel_fields(ch),
            })
            defaults.update({
                f"{ch['key']}_enabled": False,
                f"{ch['key']}_site": ch.get("site", ""),
                f"{ch['key']}_username": "",
                f"{ch['key']}_password": "",
                f"{ch['key']}_cookie": "",
                f"{ch['key']}_limit": "20",
            })

        # 癫影（Dian115）专属默认值（对应 subx 原版癫影配置页）
        defaults.update({
            "dianying_site": "https://m.dian115.com",
            "dianying_apikey": "",
            "dianying_useragent": "",
            "dianying_show_tag": False,
            "dianying_keepalive": True,
            "dianying_sign_enabled": False,
            "dianying_sign_cron": "10 8 * * *",
            "dianying_sign_mode": "normal",
            "dianying_proxy_enabled": False,
            "dianying_proxy": "",
        })

        # 其余 6 个渠道专属默认值（版式对应 subx 原版各渠道页）
        defaults.update({
            # 聚影
            "juying_apikey": "",
            "juying_username": "",
            "juying_password": "",
            "juying_sign_enabled": False,
            "juying_sign_draw": False,
            "juying_sign_mode": "normal",
            "juying_sign_cron": "12 8 * * *",
            # 剧迷
            "jumi_site": "https://www.gimy115.top/",
            "jumi_show_tag": False,
            "jumi_sign_enabled": False,
            "jumi_sign_cron": "15 9 * * *",
            # 观影
            "guanying_show_tag": False,
            # 帧影
            "zhenying_site": "https://framehdr.com/",
            "zhenying_show_tag": False,
            "zhenying_sign_enabled": False,
            "zhenying_sign_cron": "18 9 * * *",
            "zhenying_sign_cookie": "",
            "zhenying_sign_notify": True,
            "zhenying_sign_onlyonce": False,
            # 蜗牛4K
            "woniu_show_tag": False,
            "woniu_sign_enabled": False,
            "woniu_sign_cron": "20 8 * * *",
            # 权益宝
            "quanyi_show_tag": False,
            "quanyi_sign_enabled": False,
            "quanyi_sign_cron": "25 8 * * *",
        })

        defaults["tab"] = "search"

        form = [{
            "component": "VForm",
            "content": [
                # 左侧导航 + 右侧面板（参照 subx 侧栏布局）
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
                                    "style": {"width": "100%"},
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

        page: list[dict] = [
            # 右上角操作按钮：详情页 events 才能调用插件后端 API（配置页不支持）
            {
                "component": "VRow",
                "props": {"justify": "end"},
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "deep-purple", "variant": "tonal",
                            "prepend-icon": "mdi-check", "size": "small",
                        },
                        "text": "立即签到",
                        "events": {
                            "click": {
                                "api": f"plugin/{self.__class__.__name__}/sign",
                                "method": "get",
                            },
                        },
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "grey", "variant": "outlined",
                            "prepend-icon": "mdi-check", "size": "small",
                            "class": "ml-2",
                        },
                        "text": "测试登录态",
                        "events": {
                            "click": {
                                "api": f"plugin/{self.__class__.__name__}/test_login",
                                "method": "get",
                            },
                        },
                    },
                ],
            },
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": summary},
            },
        ]
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
        # 帧影签到状态
        zy_sign_record = self.get_data("zy_sign_last") or {}
        if self._zy_sign_enabled or zy_sign_record:
            state = {"success": "签到成功", "pending": "结果待核对"}.get(
                str(zy_sign_record.get("state") or ""), "尚无记录")
            page.append({
                "component": "VAlert",
                "props": {
                    "type": "success" if zy_sign_record.get("state") == "success"
                            else "warning",
                    "variant": "tonal", "density": "compact",
                    "text": f"帧影（FrameHDR）签到：{state}；"
                            f"日期：{zy_sign_record.get('date', '-')}"},
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

    def api_sign(self) -> dict:
        """GET/POST /api/v1/plugin/Subx115/sign（配置页「立即签到」按钮）。"""
        result = self._hdhaven_sign(manual=True)
        return {"code": 0 if result.get("success") else 1,
                "message": result.get("message", "")}

    def api_test_login(self) -> dict:
        """GET/POST /api/v1/plugin/Subx115/test_login（配置页「测试登录」按钮）。"""
        result = self._hdhaven_test_login()
        return {"code": 0 if result.get("ok") else 1,
                "message": result.get("message", ""),
                "data": result.get("user")}

    def api_zhenying_sign(self) -> dict:
        """GET/POST /api/v1/plugin/Subx115/zhenying_sign（帧影「立即签到」按钮）。"""
        result = self._zhenying_sign(manual=True)
        return {"code": 0 if result.get("success") else 1,
                "message": result.get("message", "")}

    def api_zhenying_test_login(self) -> dict:
        """GET/POST /api/v1/plugin/Subx115/zhenying_test_login（帧影「测试登录」按钮）。"""
        result = self._zhenying_test_login()
        return {"code": 0 if result.get("ok") else 1,
                "message": result.get("message", ""),
                "data": result.get("user")}

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
            "zhenying_sign": {
                "enabled": self._zy_sign_enabled,
                "cron": self._zy_sign_cron,
                "last": self.get_data("zy_sign_last") or {},
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

        # 1) 收集可用渠道适配器（渠道启用开关 + 资源搜索页站点勾选同时生效）
        adapters = []
        for key, adapter_cls in ADAPTER_CLASSES.items():
            adapter = adapter_cls(self._config)
            if not bool(self._config.get(f"{key}_enabled")):
                continue
            if not bool(self._config.get(f"search_{key}", True)):
                continue
            if adapter.available():
                adapters.append(adapter)
            else:
                logger.warning(f"SubX115：渠道 {adapter.name} 已启用但未配置"
                               f"站点地址，跳过")
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

        # 3) 匹配目标（MoviePilot 订阅：片名 + 季；资源搜索页关键词并用）
        # 资源搜索页的全局包含/排除关键词（正则）先行过滤
        if self._search_include or self._search_exclude:
            before = len(shares)
            shares = [
                s for s in shares
                if (not self._search_include
                    or regex_match_any(s.title, self._search_include))
                and not regex_match_any(s.title, self._search_exclude)
            ]
            dropped = before - len(shares)
            if dropped:
                logger.info(f"SubX115：全局包含/排除关键词过滤掉 {dropped} 条资源")
        subscribes = self._get_subscribes()
        if not subscribes and not self._keywords:
            logger.warning("SubX115：未能读取 MoviePilot 订阅列表，且资源搜索"
                           "未配置关键词，本轮跳过匹配")
            return
        history = self.get_data("history") or []
        done_keys = {h.get("key") for h in history}
        matched: list[tuple[ShareInfo, Any]] = []
        for share in shares:
            if share.key in done_keys:
                continue
            hits = match_subscribes(share, subscribes) if subscribes else []
            if hits:
                matched.append((share, hits[0]))
            elif self._keywords and match_keywords(share, self._keywords):
                matched.append((share, None))
        if not matched:
            logger.info("SubX115：本轮没有匹配到订阅/关键词的新资源")
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

    def _hdhaven_test_login(self) -> dict:
        """测试栖影登录态（只读预检，供「测试登录」按钮）。"""
        if not self._sign_cookie:
            return {"ok": False, "message": "未配置栖影登录会话 Cookie"}
        if not curl_cffi_available():
            return {"ok": False, "message": "宿主缺少 curl_cffi 依赖，无法测试"}
        proxy = (self._proxy_from_settings()
                 if self._sign_use_system_proxy else self._sign_proxy)
        try:
            signer = HDHavenSigner(self._sign_cookie, proxy=proxy, timeout=20)
            result = signer.check_login()
        except HDHavenSignError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            logger.warning("SubX115：栖影登录测试异常："
                           f"{type(exc).__name__}")
            return {"ok": False, "message": "请求异常，请检查网络与代理配置"}
        return result

    # =====================================================================
    # 帧影（FrameHDR）签到
    # =====================================================================

    def _schedule_zy_sign_once(self) -> None:
        """延迟 3 秒执行一次帧影签到（一次性任务，宿主 SDK 优先，本地定时器兜底）。"""
        if scheduler_sdk is not None:
            try:
                scheduler_sdk.add_plugin_once_job(
                    self.__class__.__name__, "zy_sign_once",
                    lambda: self._zhenying_sign(manual=True),
                    "SubX115 帧影签到一次", delay_seconds=3)
                return
            except Exception as exc:
                logger.warning(f"SubX115：注册一次性帧影签到任务失败，"
                               f"改用本地定时器：{exc}")
        self._zy_sign_timer = threading.Timer(
            3.0, self._zhenying_sign, kwargs={"manual": True})
        self._zy_sign_timer.daemon = True
        self._zy_sign_timer.start()

    def _zy_resolve_signer(self) -> "ZhenyingSigner":
        """构造帧影 signer；Cookie 失效时尝试账号密码自动重新登录兜底。

        :raises ZhenyingSignError: Cookie 与账号密码均不可用
        """
        proxy = self._proxy_from_settings()
        if not proxy:
            logger.info("SubX115：帧影签到未读到代理配置，将直连")
        signer = ZhenyingSigner(self._zy_sign_cookie, proxy=proxy,
                                timeout=20, username=self._zy_username,
                                password=self._zy_password)
        # 先尝试 Cookie 预检；失败且已配账号密码则自动重新登录
        try:
            check = signer.check_login()
        except ZhenyingSignError as exc:
            raise exc
        if check.get("ok"):
            return signer
        if not self._zy_username or not self._zy_password:
            raise ZhenyingSignError(check.get("message") or "登录会话已失效")
        logger.info("SubX115：帧影 Cookie 已失效，尝试账号密码自动重新登录")
        new_cookie = signer.login()
        self._zy_sign_cookie = new_cookie
        self._update_zy_cookie(new_cookie)
        return ZhenyingSigner(new_cookie, proxy=proxy, timeout=20,
                              username=self._zy_username,
                              password=self._zy_password)

    def _update_zy_cookie(self, cookie: str) -> None:
        """把自动登录得到的新 Cookie 回写到插件配置（下次重载仍有效）。"""
        try:
            cfg = dict(self.get_config() or {})
            cfg["zhenying_sign_cookie"] = cookie
            self.update_config(cfg)
        except Exception as exc:
            logger.warning(f"SubX115：帧影新 Cookie 回写配置失败：{exc}")

    def _zhenying_sign(self, manual: bool = False) -> dict:
        """帧影签到主流程（线程锁防并发；预检 -> 提交 -> 保存 -> 通知）。"""
        if not manual and not self._zy_sign_enabled:
            return {"success": False, "message": "帧影签到未启用"}
        if not self._zy_sign_lock.acquire(blocking=False):
            return {"success": False, "message": "签到正在运行"}
        try:
            if not self._zy_sign_cookie and not self._zy_username:
                raise ZhenyingSignError("未配置帧影登录会话 Cookie 或账号密码")
            today = datetime.now().date().isoformat()
            signer = self._zy_resolve_signer()

            def _mark_pending() -> None:
                self.save_data("zy_sign_last",
                               {"date": today, "state": "pending"})
                marker = self.get_data("zy_sign_last") or {}
                if (marker.get("date") != today
                        or marker.get("state") != "pending"):
                    raise ZhenyingSignError("待核对标记保存失败，未发送签到请求")

            result = signer.signin(mark_pending=_mark_pending)
            if result.get("already"):
                self.save_data("zy_sign_last",
                               {"date": today, "state": "success",
                                "already": True})
                self._zy_sign_notice("【SubX115】帧影签到",
                                     "站点确认今日已签到，不重复提交")
                return {"success": True, "message": "站点确认今日已签到"}
            self.save_data("zy_sign_last", {"date": today, **result})
            saved = self.get_data("zy_sign_last") or {}
            if (saved.get("date") != today
                    or saved.get("state") != "success"):
                raise ZhenyingSignError("签到已返回成功，但记录保存失败，请人工核对")
            earned = result.get("earned")
            points = result.get("points")
            streak = result.get("streak")
            detail = []
            if earned is not None:
                detail.append(f"获得积分：{earned}")
            if points is not None:
                detail.append(f"当前积分：{points}")
            if streak is not None:
                detail.append(f"连续签到：{streak}")
            self._zy_sign_notice(
                "【SubX115】帧影签到成功",
                "；".join(detail) if detail else "签到成功")
            return {"success": True,
                    "message": "签到成功" + ("：" + "；".join(detail)
                                            if detail else ""),
                    "data": result}
        except ZhenyingSignError as exc:
            logger.warning(f"SubX115：帧影签到未完成：{exc}")
            self._zy_sign_notice("【SubX115】帧影签到待核对", str(exc))
            return {"success": False, "message": str(exc)}
        except Exception as exc:
            logger.warning(f"SubX115：帧影签到异常：{type(exc).__name__}")
            message = "请求或存储异常，结果可能已提交；请人工核对，勿重试"
            self._zy_sign_notice("【SubX115】帧影签到待核对", message)
            return {"success": False, "message": message}
        finally:
            self._zy_sign_lock.release()

    def _zy_sign_notice(self, title: str, text: str) -> None:
        """帧影签到结果通知（受通知开关控制）。"""
        if self._zy_sign_notify:
            self._send_message(title, text)

    def _zhenying_test_login(self) -> dict:
        """测试帧影登录态（只读预检，供「测试登录」按钮）。

        Cookie 失效时同样尝试账号密码自动登录兜底。
        """
        if not self._zy_sign_cookie and not self._zy_username:
            return {"ok": False, "message": "未配置帧影登录会话 Cookie 或账号密码"}
        try:
            signer = self._zy_resolve_signer()
            result = signer.check_login()
        except ZhenyingSignError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            logger.warning("SubX115：帧影登录测试异常："
                           f"{type(exc).__name__}")
            return {"ok": False, "message": "请求异常，请检查网络与代理配置"}
        if result.get("ok"):
            user = result.get("user") or {}
            parts = ["帧影登录态有效"]
            if user.get("points") is not None:
                parts.append(f"积分 {user['points']}")
            if user.get("total_days") is not None:
                parts.append(f"累计签到 {user['total_days']} 天")
            if user.get("continuous_days") is not None:
                parts.append(f"连续 {user['continuous_days']} 天")
            if user.get("checkin_today"):
                parts.append("今日已签到")
            result["message"] = "；".join(parts)
        return result

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
        if self._zy_sign_enabled:
            record = self.get_data("zy_sign_last") or {}
            state = {"success": "签到成功", "pending": "结果待核对"}.get(
                str(record.get("state") or ""), "尚无记录")
            lines.append(
                f"帧影签到：{self._zy_sign_cron}，{state}（{record.get('date', '-')}）")
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
