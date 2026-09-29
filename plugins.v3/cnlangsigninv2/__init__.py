"""国语视界（cnlang.org）自动签到插件 —— 按 MoviePilot V3 插件开发规范重写。

主类 ``CnlangSigninV2`` 与插件 ID 一致，插件目录 ``cnlangsigninv2`` 为类名的小写形式，
主类定义在本文件（``plugins.v3/cnlangsigninv2/__init__.py``）。

相对旧版 V2 实现，本次重写遵守的 V3 约定：

1. 只依赖稳定 SDK（``app.plugins``、``app.schemas``、``app.sdk.*``），不再使用
   ``app.core.*``、``app.utils.*``、``app.log`` 等兼容桥接路径。
2. 不再自建 ``BackgroundScheduler``。周期签到由 ``get_service()`` 注册到宿主调度器，
   “立即运行一次”与“随机延迟若干秒后执行”交给
   ``app.sdk.scheduler.add_plugin_once_job()``。
3. 模块导入期与类定义期不发起网络请求、不访问数据库、不创建线程。
4. 配置、结构化数据、历史记录全部经基类接口读写；插件 ID 统一使用
   ``self.__class__.__name__``，插件可被安全地创建虚拟分身。
5. ``init_plugin()`` 可重复调用，``stop_service()`` 可重复且安全地释放资源。
6. ``get_api()`` 返回真实的后端 API 声明，不再返回 ``None``。
"""

import random
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import EventType, MessageType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

try:  # 宿主 V3 依赖 pytz，优先使用与宿主一致的时区实现
    import pytz
except ImportError:  # pragma: no cover - 仅在宿主未提供 pytz 时回退到标准库
    pytz = None  # type: ignore[assignment]


def _resolve_timezone() -> Any:
    """解析宿主配置的时区，无法解析时返回 None 交给调度器使用自身默认时区。

    MoviePilot V3 依赖 ``pytz`` 但不保证容器内存在系统时区库，标准库 ``zoneinfo``
    在精简镜像中会抛 ``ZoneInfoNotFoundError``。这里按 pytz -> zoneinfo -> 调度器
    默认时区的顺序逐级降级，任何一步失败都只记录日志，绝不向上抛出。
    """
    tz_name = getattr(settings, "TZ", None) or "Asia/Shanghai"
    if pytz is not None:
        try:
            return pytz.timezone(tz_name)
        except Exception as err:  # noqa: BLE001 - 时区解析失败必须降级而非中断
            logger.warning(f"pytz 无法解析时区 {tz_name}：{err}")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception as err:  # noqa: BLE001 - 缺少 tzdata 时退回调度器默认时区
        logger.warning(f"无法解析时区 {tz_name}：{err}，改用调度器默认时区")
        return None


# ---------------------------------------------------------------------------
# 站点地址与流程常量
# ---------------------------------------------------------------------------

# 国语视界站点域名（Discuz 论坛）
SITE_HOST = "cnlang.org"
# 签到页面：同时用于探测登录态、提取 formhash 与本月累计签到
SIGN_PAGE_URL = f"https://{SITE_HOST}/dsu_paulsign-sign.html?mobile=no"
# 签到提交接口
SIGN_SUBMIT_URL = (
    f"https://{SITE_HOST}/plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1"
)
# 积分（大洋）接口
CREDIT_URL = (
    f"https://{SITE_HOST}/home.php?mod=spacecp&ac=credit"
    "&showcredit=1&inajax=1&ajaxtarget=extcreditmenu_menu"
)
# 用户组接口
USERGROUP_URL = f"https://{SITE_HOST}/home.php?mod=spacecp&ac=usergroup"
# 签到寄语来源（一言）
HITOKOTO_URL = "https://v1.hitokoto.cn/?encode=text"

# 站点对“想说的话”的长度要求为 6~50 字
SAY_MIN_LEN = 6
SAY_MAX_LEN = 50
SAY_MAX_ATTEMPTS = 10
SAY_FALLBACK = "一别之后，两地相思，只道是三四月，又谁知五六年。"
# 签到心情固定使用“开心”
SIGN_MOOD = "kx"

# 远程命令动作标识
ACTION_SIGNIN = "cnlang_signin"

# 插件结构化数据键
KEY_HISTORY = "history"
KEY_LAST_RESULT = "last_result"

# 宿主调度器中的一次性任务 ID：同 ID 重复登记只保留最后一次
JOB_SIGNIN_ONCE = "signin_once"
JOB_SIGNIN_DELAYED = "signin_delayed"

# 合法通知样式标识，非法值回落到 style1
NOTIFY_STYLES = ("style1", "style2", "style3", "style4", "style5")

# 通知样式模板。占位符：{detail} 结果详情、{time} 执行时间、
# {headline} 失败标题、{advice} Cookie 失效时的处理建议。
NOTIFY_TEMPLATES: Dict[str, Dict[str, str]] = {
    "style1": {
        "title": "🎬 国语视界签到",
        "success": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ 签到成功\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 详细信息：\n"
            "{detail}\n"
            "⏰ 执行时间：{time}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "failure": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "❌ {headline}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 失败原因：{detail}\n"
            "⏰ 执行时间：{time}\n"
            "{advice}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "headline": "签到失败",
        "cookie_headline": "Cookie已失效",
        "advice": "🔑 请更新Cookie后重试",
    },
    "style2": {
        "title": "🌸 国语视界签到",
        "success": (
            "┏━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃ ✅ 签到成功\n"
            "┃ 📝 {detail}\n"
            "┃ ⏰ {time}\n"
            "┗━━━━━━━━━━━━━━━━━━━━┛"
        ),
        "failure": (
            "┏━━━━━━━━━━━━━━━━━━━━┓\n"
            "┃ ❌ {headline}\n"
            "┃ 📝 {detail}\n"
            "┃ ⏰ {time}\n"
            "{advice}\n"
            "┗━━━━━━━━━━━━━━━━━━━━┛"
        ),
        "headline": "签到失败",
        "cookie_headline": "Cookie已失效",
        "advice": "┃ 🔑 请更新Cookie后重试",
    },
    "style3": {
        "title": "🚀 国语视界签到",
        "success": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ 任务执行成功\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 详细信息：\n"
            "{detail}\n"
            "⏱️ 执行时间：{time}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "failure": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ {headline}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 错误信息：{detail}\n"
            "⏱️ 执行时间：{time}\n"
            "{advice}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "headline": "任务执行失败",
        "cookie_headline": "Cookie验证失败",
        "advice": "🔑 请更新Cookie后重试",
    },
    "style4": {
        "title": "📊 国语视界签到",
        "success": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 签到状态：成功\n"
            "📋 详细信息：\n"
            "{detail}\n"
            "🕒 执行时间：{time}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "failure": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 签到状态：{headline}\n"
            "📋 错误详情：{detail}\n"
            "🕒 执行时间：{time}\n"
            "{advice}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "headline": "失败",
        "cookie_headline": "Cookie已失效",
        "advice": "🔑 操作建议：请更新Cookie后重试",
    },
    "style5": {
        "title": "✨ 国语视界签到",
        "success": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💫 签到任务执行成功\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 详细信息：\n"
            "{detail}\n"
            "🕰️ 执行时间：{time}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "failure": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💫 {headline}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 失败原因：{detail}\n"
            "🕰️ 执行时间：{time}\n"
            "{advice}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "headline": "签到任务执行失败",
        "cookie_headline": "Cookie验证失败",
        "advice": "🔑 请更新Cookie后重试",
    },
}


class CnlangSigninV2(_PluginBase):
    """国语视界自动签到插件。

    职责：按配置的 cron 定时登录国语视界完成签到，按配置的样式发送通知，并把签到
    结果写入插件历史数据，供详情页统计展示。插件本身不持有任何后台线程或调度器，
    全部后台能力都交由宿主调度器承担。
    """

    # 插件名称
    plugin_name = "国语视界签到V3"
    # 插件描述
    plugin_desc = (
        "国语视界（cnlang.org）自动签到助手：支持定时签到、随机延迟、系统代理、"
        "多种通知样式与签到历史统计。"
    )
    # 插件图标
    plugin_icon = (
        "https://raw.githubusercontent.com/xijin285/MoviePilot-Plugins"
        "/refs/heads/main/icons/cnlang.png"
    )
    # 插件版本，必须与 package.v3.json 中的 version 保持一致
    plugin_version = "3.6.1"
    # 插件作者
    plugin_author = "xijin285"
    # 作者主页
    author_url = "https://github.com/xijin285"
    # 插件配置项ID前缀
    plugin_config_prefix = "cnlangsignin_v2_"
    # 加载顺序
    plugin_order = 2
    # 可使用的用户级别
    auth_level = 1

    # ---- 运行状态：全部在 init_plugin() 中按配置重建 ----
    # 是否启用插件
    _enabled: bool = False
    # 签到周期 cron 表达式
    _cron: Optional[str] = None
    # 站点 Cookie
    _cookie: Optional[str] = None
    # 是否发送通知
    _notify: bool = False
    # 历史记录保留天数
    _history_days: int = 30
    # 随机延迟区间，形如 "100-200"（秒）
    _random_delay: Optional[str] = None
    # 通知样式
    _notify_style: str = "style1"
    # 是否使用宿主系统代理
    _use_proxy: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置并重建本次运行状态；允许被宿主重复调用。

        宿主在“保存插件配置”流程里直接调用本方法，且只捕获
        ``PluginMutationRejectedError``：这里抛出的任何异常都会变成 HTTP 500，
        前端只能显示“未知错误”。因此除字段赋值外的副作用操作全部就地兜底，
        失败只记日志，不向上抛出。

        :param config: 插件配置字典，None 表示按空配置初始化
        """
        # 先取消上一轮登记的一次性任务，保证重复初始化不会堆积待执行任务。
        try:
            self.stop_service()
        except Exception as err:  # noqa: BLE001 - 停用失败不得阻断配置保存
            logger.warning(f"停止上一轮签到服务时出错，已忽略：{err}")

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._cron = (config.get("cron") or "").strip() or None
        self._cookie = (config.get("cookie") or "").strip() or None
        self._notify = bool(config.get("notify"))
        self._notify_style = config.get("notify_style") or "style1"
        self._random_delay = config.get("random_delay")
        self._use_proxy = bool(config.get("use_proxy", False))
        try:
            self._history_days = max(int(config.get("history_days") or 30), 1)
        except (TypeError, ValueError):
            logger.warning(f"历史保留天数配置无效：{config.get('history_days')}，按 30 天处理")
            self._history_days = 30

        # “清除历史记录”是开关式操作：执行一次后立即回写关闭，
        # 避免宿主重复初始化时反复清空用户数据。
        if config.get("clear"):
            try:
                self.del_data(KEY_HISTORY)
                self.del_data(KEY_LAST_RESULT)
                logger.info("国语视界签到历史记录已清除")
            except Exception as err:  # noqa: BLE001 - 清理失败不得阻断配置保存
                logger.error(f"清除国语视界签到历史记录失败：{err}")
            self._save_config(clear=False)

        # “立即运行一次”登记为宿主调度器的一次性任务，不再自建调度器。
        if config.get("onlyonce"):
            self._save_config(onlyonce=False)
            self._run_once(delay_seconds=3)

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """取消本插件登记的一次性任务；可被重复调用。

        宿主版本未提供 ``remove_plugin_once_job`` 时直接跳过，而不是抛出
        ``AttributeError``：本方法会在 ``init_plugin()`` 开头被调用，一旦抛出
        就会让配置保存接口返回 500。
        """
        plugin_id = self.__class__.__name__
        remove_once_job = getattr(scheduler_sdk, "remove_plugin_once_job", None)
        if remove_once_job is not None:
            for job_id in (JOB_SIGNIN_ONCE, JOB_SIGNIN_DELAYED):
                try:
                    remove_once_job(plugin_id, job_id)
                except Exception as err:  # noqa: BLE001 - 调度器未启动或版本较旧时忽略
                    logger.debug(f"取消一次性任务 {job_id} 失败：{err}")
        logger.info("国语视界签到服务已停止")

    # ------------------------------------------------------------------
    # 扩展点注册
    # ------------------------------------------------------------------

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程控制命令。"""
        return [
            {
                "cmd": "/cnlang_signin",
                "event": EventType.PluginAction,
                "desc": "国语视界签到",
                "category": "站点",
                "data": {"action": ACTION_SIGNIN},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件后端 API，最终路径为 ``/api/v1/plugin/<PluginID>/<path>``。"""
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询国语视界账号与签到状态",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询签到历史与统计",
            },
            {
                "path": "/signin",
                "endpoint": self.api_signin,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行一次签到",
            },
            {
                "path": "/history/clear",
                "endpoint": self.api_clear_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空签到历史",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """插件启用且 cron 合法时，把定时签到注册到宿主调度器。"""
        if not self._enabled or not self._cron:
            return []
        trigger = self._build_trigger()
        if trigger is None:
            return []
        return [
            {
                "id": f"{self.__class__.__name__}.Signin",
                "name": "国语视界定时签到",
                "trigger": trigger,
                "func": self._scheduled_signin,
                "kwargs": {},
            }
        ]

    def _build_trigger(self) -> Optional[CronTrigger]:
        """按当前 cron 配置构建触发器；表达式非法时返回 None。

        统一捕获 ``Exception``：``ZoneInfoNotFoundError``、宿主缺少 ``TZ`` 等
        都会在时区解析阶段抛出，只捕获 ``ValueError``/``TypeError`` 会漏掉它们。
        """
        if not self._cron:
            return None
        try:
            return CronTrigger.from_crontab(self._cron, timezone=_resolve_timezone())
        except Exception as err:  # noqa: BLE001 - 非法表达式不应阻断插件加载
            logger.error(f"签到周期表达式无效：{self._cron}（{err}）")
            return None

    # ------------------------------------------------------------------
    # 插件 API 实现
    # ------------------------------------------------------------------

    def api_status(self) -> Dict[str, Any]:
        """API：返回账号与签到状态摘要（含一次实时站点探测）。"""
        return self.get_status_summary()

    def api_history(self) -> Dict[str, Any]:
        """API：返回签到历史明细与统计结果。"""
        history = self.get_data(KEY_HISTORY) or []
        return {
            "history": sorted(
                history, key=lambda item: item.get("date", ""), reverse=True
            ),
            "stats": self._analyze_history(history),
            "last_result": self.get_data(KEY_LAST_RESULT) or {},
        }

    def api_signin(self) -> Dict[str, Any]:
        """API：立即执行一次签到并返回本次结果。"""
        return self.signin() or {}

    def api_clear_history(self) -> Dict[str, Any]:
        """API：清空签到历史与最近一次结果。"""
        self.del_data(KEY_HISTORY)
        self.del_data(KEY_LAST_RESULT)
        return {"success": True, "message": "签到历史已清空"}

    # ------------------------------------------------------------------
    # 签到主流程
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.PluginAction)
    def signin(self, event: Optional[Event] = None) -> Dict[str, Any]:
        """执行一次签到。

        同时承担两个角色：``/cnlang_signin`` 远程命令的事件处理器（``event`` 非空），
        以及宿主调度器与插件 API 直接调用的入口（``event`` 为空）。

        :param event: 事件对象，仅在响应远程命令时传入
        :return: 本次签到结果字典
        """
        if event is not None:
            event_data = event.event_data or {}
            if event_data.get("action") != ACTION_SIGNIN:
                return {}
            logger.info("收到远程命令，开始执行国语视界签到")
        return self._execute_signin()

    def _scheduled_signin(self) -> None:
        """定时入口：按随机延迟配置把签到转成宿主调度器的一次性任务。"""
        delay = self._random_delay_seconds()
        if delay <= 0:
            self.signin()
            return
        logger.info(f"国语视界签到将随机延迟 {delay} 秒后执行")
        if not self._add_once_job(JOB_SIGNIN_DELAYED, "国语视界延迟签到", delay):
            logger.warning("宿主调度器不可用，改为在后台线程中延迟执行签到")
            self._run_in_background(delay)

    def _execute_signin(self) -> Dict[str, Any]:
        """签到主流程：探测登录态 -> 提交签到 -> 汇总结果 -> 落库并通知。"""
        if not self._cookie:
            return self._record_failure("未配置Cookie")

        headers = self._build_headers()
        proxy_hint = "（使用代理）" if self._use_proxy else ""

        # 步骤 1：读取签到页面，确认登录态并提取 formhash
        logger.info(f"步骤1：获取签到页面信息{proxy_hint}")
        page = self._fetch(SIGN_PAGE_URL, headers=headers)
        if page is None:
            return self._record_failure("获取签到页面失败，请检查网络或代理设置")

        user_name = self._search(r'title="访问我的空间">(.*?)</a>', page)
        if not user_name:
            return self._record_failure("未获取到用户名，Cookie 可能已失效")
        logger.info(f"登录用户名：{user_name}")

        if re.search(r"您今天已经签到过了或者签到时间还未开始", page):
            logger.info("今日已完成签到，跳过提交")
            return self._record_already_signed(user_name)

        formhash = self._search(r'<input[^>]*name="formhash"[^>]*value="([^"]*)"', page)
        if not formhash:
            return self._record_failure("未获取到 formhash，站点页面结构可能已变化")
        logger.info(f"formhash：{formhash}")

        month_signs = self._search(r"<p>您本月已累计签到:<b>(.*?)</b>", page)
        total_signs = int(month_signs) + 1 if month_signs and month_signs.isdigit() else 1
        logger.info(f"本月累计签到（含本次）：{total_signs}")

        # 步骤 2：提交签到
        logger.info(f"步骤2：提交签到请求{proxy_hint}")
        response = self._fetch(
            SIGN_SUBMIT_URL,
            headers=headers,
            data={
                "formhash": formhash,
                "qdxq": SIGN_MOOD,
                "qdmode": "1",
                "todaysay": self._build_say(),
                "fastreply": "0",
            },
        )
        if response is None:
            return self._record_failure("提交签到请求失败，请检查网络或代理设置")

        content = self._search(r'<div class="c">(.*?)</div>', response, flags=re.DOTALL)
        if not content:
            return self._record_failure("未获取到签到响应内容，签到结果未知")
        content = content.strip()
        logger.info(f"签到响应：{content}")

        # 步骤 3：读取积分（大洋）余额
        logger.info(f"步骤3：获取积分信息{proxy_hint}")
        credit_page = self._fetch(CREDIT_URL, headers=headers) or ""
        money = self._search(r'<span id="hcredit_2">(\d+)</span>', credit_page) or "未知"
        logger.info(f"当前大洋余额：{money}")

        return self._record_success(
            username=user_name,
            total_signs=total_signs,
            money=money,
            content=content,
        )

    # ------------------------------------------------------------------
    # 结果记录与通知
    # ------------------------------------------------------------------

    def _record_success(
        self, *, username: str, total_signs: int, money: str, content: str
    ) -> Dict[str, Any]:
        """记录一次成功签到：发送通知、追加历史、保存最近结果。"""
        sign_time = self._now()
        detail = (
            f"签到账号：{username}\n"
            f"本月累计签到：{total_signs} 天\n"
            f"当前大洋：{money}\n"
            f"签到时间：{sign_time}\n"
            f"{content}"
        )
        self._notify_result(success=True, detail=detail)

        history = self.get_data(KEY_HISTORY) or []
        history.append(
            {
                "date": sign_time,
                "username": username,
                "totalContinuousCheckIn": total_signs,
                "money": money,
                "content": content,
                "success": True,
            }
        )
        self.save_data(KEY_HISTORY, self._prune_history(history))

        result = {
            "time": sign_time,
            "success": True,
            "username": username,
            "money": money,
            "content": content,
            "total_signs": total_signs,
            "message": detail,
        }
        self.save_data(KEY_LAST_RESULT, result)
        return result

    def _record_already_signed(self, username: str) -> Dict[str, Any]:
        """记录“今日已签到”：发送通知但不重复追加历史，避免统计虚高。"""
        sign_time = self._now()
        detail = f"签到账号：{username}\n今日已完成签到，无需重复提交\n检查时间：{sign_time}"
        self._notify_result(success=True, detail=detail)
        result = {
            "time": sign_time,
            "success": True,
            "username": username,
            "money": self._last_known_money(),
            "content": "您今天已经签到过了或者签到时间还未开始",
            "total_signs": 0,
            "message": detail,
        }
        self.save_data(KEY_LAST_RESULT, result)
        return result

    def _record_failure(self, reason: str) -> Dict[str, Any]:
        """记录一次失败：发送失败通知并保存最近结果。"""
        sign_time = self._now()
        self._notify_result(success=False, detail=reason)
        result = {
            "time": sign_time,
            "success": False,
            "username": "",
            "money": self._last_known_money(),
            "content": reason,
            "total_signs": 0,
            "message": reason,
        }
        self.save_data(KEY_LAST_RESULT, result)
        return result

    def _notify_result(self, *, success: bool, detail: str) -> None:
        """按配置的通知样式发送签到结果通知。"""
        logger.info(detail)
        if not self._notify:
            return

        style = self._notify_style if self._notify_style in NOTIFY_STYLES else "style1"
        template = NOTIFY_TEMPLATES[style]
        expired = self._looks_like_cookie_expired(detail)
        advice = template["advice"] if (expired and not success) else ""
        text = (template["success"] if success else template["failure"]).format(
            detail=detail,
            time=self._now(),
            headline=template["cookie_headline"] if expired else template["headline"],
            advice=advice,
        )
        # 未命中 Cookie 失效分支时会残留空行，这里统一清理
        text = "\n".join(line for line in text.split("\n") if line.strip())
        self.post_message(mtype=MessageType.Plugin, title=template["title"], text=text)

    @staticmethod
    def _looks_like_cookie_expired(detail: str) -> bool:
        """判断失败原因是否属于 Cookie 失效，用于选择更明确的通知文案。"""
        return "cookie" in detail.lower() or "未获取到用户名" in detail

    # ------------------------------------------------------------------
    # 配置与工具方法
    # ------------------------------------------------------------------

    def _save_config(self, **overrides: Any) -> bool:
        """回写插件配置；失败只记录日志，不向上抛出。

        本方法在 ``init_plugin()`` 内被调用，而宿主把 ``init_plugin()`` 的异常
        直接转成配置保存接口的 HTTP 500，因此这里必须自行兜底。

        :param overrides: 需要覆盖的字段，用于关闭“立即运行一次”“清除历史”等一次性开关
        :return: 是否成功写入
        """
        config: Dict[str, Any] = {
            "enabled": self._enabled,
            "cron": self._cron,
            "cookie": self._cookie,
            "notify": self._notify,
            "notify_style": self._notify_style,
            "random_delay": self._random_delay,
            "history_days": self._history_days,
            "use_proxy": self._use_proxy,
            "onlyonce": False,
            "clear": False,
        }
        config.update(overrides)
        try:
            self.update_config(config)
        except Exception as err:  # noqa: BLE001 - 回写失败不得阻断配置保存
            logger.error(f"回写国语视界签到配置失败：{err}")
            return False
        return True

    def _add_once_job(self, job_id: str, name: str, delay_seconds: float) -> bool:
        """向宿主调度器登记一次性签到任务。

        官方开发指南要求兼容尚未提供该接口的主程序，因此先用 ``getattr`` 探测
        ``add_plugin_once_job``：缺失时返回 False 交由调用方兜底，而不是抛出
        ``AttributeError``。

        :param job_id: 插件内唯一的一次性任务 ID
        :param name: 仪表盘显示的任务名称
        :param delay_seconds: 延迟秒数
        :return: 是否成功登记到宿主调度器
        """
        add_once_job = getattr(scheduler_sdk, "add_plugin_once_job", None)
        if add_once_job is None:
            logger.warning("当前主程序未提供 add_plugin_once_job，改用后台线程执行签到")
            return False
        try:
            return bool(
                add_once_job(
                    self.__class__.__name__,
                    job_id,
                    self.signin,
                    name,
                    delay_seconds=delay_seconds,
                )
            )
        except Exception as err:  # noqa: BLE001 - 登记失败按不可用处理，由调用方兜底
            logger.warning(f"登记一次性签到任务失败：{err}")
            return False

    def _run_in_background(self, delay_seconds: float = 0) -> None:
        """兜底执行：在守护线程中延迟运行一次签到。

        宿主调度器不可用（或主程序版本较旧）时使用，避免在配置保存等
        同步请求线程中直接发起网络请求。

        :param delay_seconds: 延迟秒数
        """

        def _worker() -> None:
            """等待指定延迟后执行一次签到。"""
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            try:
                self.signin()
            except Exception as err:  # noqa: BLE001 - 后台线程异常只记录日志
                logger.error(f"后台执行国语视界签到失败：{err}")

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"{self.__class__.__name__}.Once",
        ).start()

    def _run_once(self, delay_seconds: float = 0) -> None:
        """把一次签到登记到宿主调度器；调度器不可用时用后台线程兜底。

        :param delay_seconds: 延迟秒数
        """
        if self._add_once_job(JOB_SIGNIN_ONCE, "国语视界签到立即运行一次", delay_seconds):
            logger.info(f"国语视界签到已登记为宿主一次性任务，{delay_seconds} 秒后执行")
            return
        logger.warning("宿主调度器不可用，改为在后台线程中执行一次签到")
        self._run_in_background(delay_seconds)

    def _random_delay_seconds(self) -> int:
        """把 ``100-200`` 形式的随机延迟配置解析为秒数，非法配置按不延迟处理。"""
        raw = str(self._random_delay or "").strip()
        if not raw:
            return 0
        try:
            start_text, end_text = raw.split("-", 1)
            start, end = int(start_text.strip()), int(end_text.strip())
        except ValueError:
            logger.warning(f"随机延迟配置格式错误：{raw}，本次不延迟")
            return 0
        if start < 0 or end < start:
            logger.warning(f"随机延迟区间无效：{raw}，本次不延迟")
            return 0
        return random.randint(start, end)

    def _build_say(self) -> str:
        """获取一段符合站点长度要求的签到寄语，取不到时使用兜底文案。"""
        say = ""
        for attempt in range(1, SAY_MAX_ATTEMPTS + 1):
            text = self._fetch(HITOKOTO_URL)
            if text:
                say = text.strip()
            logger.info(f"尝试想说的话-{attempt}：{say}")
            if SAY_MIN_LEN <= len(say) <= SAY_MAX_LEN:
                return say
        logger.warning("未获取到符合长度要求的签到寄语，使用默认文案")
        return SAY_FALLBACK

    def _build_headers(self) -> Dict[str, str]:
        """构造站点请求头，Cookie 取自插件配置。"""
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": (
                "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2"
            ),
            "cache-control": "max-age=0",
            "Upgrade-Insecure-Requests": "1",
            "Host": SITE_HOST,
            "Cookie": self._cookie or "",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36 Edg/97.0.1072.62"
            ),
        }

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        """按配置返回宿主系统代理；未开启或宿主未配置代理时返回 None。"""
        if not self._use_proxy:
            return None
        proxy = settings.PROXY
        if not proxy:
            logger.warning("已开启使用代理，但宿主未配置系统代理")
            return None
        logger.info(f"使用系统代理：{proxy}")
        return proxy

    def _fetch(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """发起一次请求并返回响应正文。

        :param url: 目标地址
        :param headers: 请求头，None 时使用 RequestUtils 默认头
        :param data: 非空时使用 POST，否则使用 GET
        :return: 响应正文；请求异常或状态码非 200 时返回 None
        """
        try:
            client = RequestUtils(headers=headers, proxies=self._get_proxies())
            if data is not None:
                response = client.post_res(url, data=data)
            else:
                response = client.get_res(url)
        except Exception as err:
            logger.error(f"请求 {url} 异常：{err}")
            return None
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "无响应"
            logger.error(f"请求 {url} 失败，状态码：{status}")
            return None
        return response.text

    @staticmethod
    def _search(pattern: str, text: str, flags: int = 0) -> Optional[str]:
        """在站点响应中按正则提取第一个分组，未命中返回 None。"""
        if not text:
            return None
        match = re.search(pattern, text, flags)
        return match.group(1) if match else None

    @staticmethod
    def _now() -> str:
        """返回当前本地时间字符串，统一历史记录与通知的时间格式。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _parse_date(value: Any) -> Optional[datetime]:
        """把历史记录中的时间字符串解析为 datetime，解析失败返回 None。"""
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_success(record: Dict[str, Any]) -> bool:
        """判断一条历史记录是否签到成功。

        新数据直接读取 ``success`` 字段；旧版本只以响应文本判断，这里保留回退逻辑。
        """
        if "success" in record:
            return bool(record["success"])
        return "签到成功" in (record.get("content") or "")

    def _prune_history(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按保留天数裁剪历史记录，时间无法解析的记录直接丢弃。"""
        deadline = time.time() - self._history_days * 24 * 60 * 60
        pruned: List[Dict[str, Any]] = []
        for record in history:
            record_time = self._parse_date(record.get("date"))
            if record_time is not None and record_time.timestamp() >= deadline:
                pruned.append(record)
        return pruned

    def _last_known_money(self) -> str:
        """取最近一次已知的大洋余额，取不到时返回 "0"。"""
        last = self.get_data(KEY_LAST_RESULT) or {}
        if last.get("money"):
            return str(last["money"])
        history = self.get_data(KEY_HISTORY) or []
        if history:
            newest = max(history, key=lambda item: item.get("date", ""))
            return str(newest.get("money") or "0")
        return "0"

    def _next_sign_time(self) -> str:
        """按 cron 表达式推算下次签到时间，未启用或表达式非法时返回“未设置”。"""
        if not (self._enabled and self._cron):
            return "未设置"
        try:
            trigger = self._build_trigger()
            if trigger is None:
                return "未设置"
            now = datetime.now(tz=trigger.timezone) if trigger.timezone else datetime.now()
            next_run = trigger.get_next_fire_time(None, now)
        except Exception as err:  # noqa: BLE001 - 推算失败只影响展示，不影响运行
            logger.error(f"推算下次签到时间失败：{err}")
            return "未设置"
        return next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else "未设置"

    # ------------------------------------------------------------------
    # 详情页数据
    # ------------------------------------------------------------------

    def get_status_summary(self) -> Dict[str, Any]:
        """汇总详情页所需的运行状态，并实时探测站点账号信息。"""
        status_data: Dict[str, Any] = {
            "status": "运行中" if self._enabled else "已停止",
            "next_sign_time": self._next_sign_time(),
            "last_sign_time": "无",
            "last_sign_status": "无",
            "continuous_days": 0,
            "month_signs": 0,
            "total_signs": 0,
            "account": {
                "username": "未知",
                "money": "0",
                "usergroup": "用户",
                "cookie_status": "无效",
            },
        }

        # 历史数据不依赖网络，先填充，保证站点不可用时详情页仍有内容
        history = self.get_data(KEY_HISTORY) or []
        if history:
            newest = max(history, key=lambda item: item.get("date", ""))
            status_data["last_sign_time"] = newest.get("date", "无")
            status_data["last_sign_status"] = "成功" if self._is_success(newest) else "失败"
            status_data["total_signs"] = len(history)

        if not self._cookie:
            return status_data

        # 实时探测：签到页 -> 积分 -> 用户组，任一失败只影响对应字段
        headers = self._build_headers()
        page = self._fetch(SIGN_PAGE_URL, headers=headers)
        if page:
            username = self._search(r'title="访问我的空间">(.*?)</a>', page)
            if username:
                status_data["account"]["username"] = username
                status_data["account"]["cookie_status"] = "有效"

            month_signs = self._search(r"您本月已累计签到:<b>(\d+)</b>", page)
            if month_signs and month_signs.isdigit():
                status_data["month_signs"] = int(month_signs)

            continuous_days = self._search(r"您已经连续签到<b>(\d+)</b>天", page)
            if continuous_days and continuous_days.isdigit():
                status_data["continuous_days"] = int(continuous_days)

            if re.search(r"您今天已经签到过了或者签到时间还未开始", page):
                status_data["last_sign_status"] = "成功"
                if status_data["last_sign_time"] == "无":
                    status_data["last_sign_time"] = self._now()

        credit_page = self._fetch(CREDIT_URL, headers=headers)
        money = self._search(r'<span id="hcredit_2">(\d+)</span>', credit_page or "")
        if money:
            status_data["account"]["money"] = money

        usergroup_page = self._fetch(USERGROUP_URL, headers=headers)
        group_name = self._search(
            r"您目前属于用户组: <strong>(.*?)</strong>", usergroup_page or ""
        )
        if group_name:
            status_data["account"]["usergroup"] = group_name

        return status_data

    def _analyze_history(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        """统计签到历史：成功率、大洋收益、连续签到天数与最佳签到时段。"""
        if not history:
            return {
                "success_rate": "0%",
                "total_days": 0,
                "success_days": 0,
                "fail_days": 0,
                "total_money": 0,
                "avg_money": 0,
                "max_continuous": 0,
                "current_continuous": 0,
                "best_time": "无",
                "month_stats": {},
            }

        total_days = len(history)
        success_records = [record for record in history if self._is_success(record)]
        success_days = len(success_records)
        success_rate = f"{success_days / total_days * 100:.1f}%"

        money_values = [
            int(record["money"])
            for record in history
            if str(record.get("money", "")).isdigit()
        ]
        total_money = sum(money_values)
        avg_money = f"{total_money / success_days:.1f}" if success_days else 0

        # 成功签到的日期集合（同一天多次签到只算一天），按时间倒序
        success_day_list = sorted(
            {
                record_date.date()
                for record_date in (
                    self._parse_date(item.get("date")) for item in success_records
                )
                if record_date is not None
            },
            reverse=True,
        )

        # 历史最长连续签到
        max_continuous = 0
        streak = 0
        previous_day = None
        for day in success_day_list:
            if previous_day is not None and (previous_day - day).days == 1:
                streak += 1
            else:
                streak = 1
            previous_day = day
            max_continuous = max(max_continuous, streak)

        # 当前连续签到：从最新一条成功记录起逐日回溯，遇到断档即停止
        current_continuous = 0
        expected_day = None
        for day in success_day_list:
            if expected_day is None:
                current_continuous = 1
            elif day == expected_day:
                current_continuous += 1
            else:
                break
            expected_day = day - timedelta(days=1)

        # 统计签到成功次数最多的时段
        hour_stats: Dict[int, int] = {}
        for record in success_records:
            record_time = self._parse_date(record.get("date"))
            if record_time is None:
                continue
            hour_stats[record_time.hour] = hour_stats.get(record_time.hour, 0) + 1
        best_hour = (
            max(hour_stats.items(), key=lambda item: item[1])[0] if hour_stats else None
        )

        return {
            "success_rate": success_rate,
            "total_days": total_days,
            "success_days": success_days,
            "fail_days": total_days - success_days,
            "total_money": total_money,
            "avg_money": avg_money,
            "max_continuous": max_continuous,
            "current_continuous": current_continuous,
            "best_time": f"{best_hour:02d}:00" if best_hour is not None else "无",
            "month_stats": {},
        }

    # ------------------------------------------------------------------
    # 配置页面
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装配置页面。

        :return: 1、Vuetify 页面配置；2、默认配置数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    # 基础设置卡片
                    {
                        'component': 'VCard',
                        'props': {
                            'title': '基础设置',
                            'variant': 'outlined',
                            'class': 'mb-4'
                        },
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'enabled',
                                                            'label': '启用插件',
                                                            'color': 'primary',
                                                            'prepend-icon': 'mdi-power'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'notify',
                                                            'label': '开启通知',
                                                            'color': 'info',
                                                            'prepend-icon': 'mdi-bell'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'onlyonce',
                                                            'label': '立即运行一次',
                                                            'color': 'success',
                                                            'prepend-icon': 'mdi-play'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'clear',
                                                            'label': '清除历史记录',
                                                            'color': 'warning',
                                                            'prepend-icon': 'mdi-delete'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'use_proxy',
                                                            'label': '使用代理',
                                                            'color': 'primary',
                                                            'prepend-icon': 'mdi-proxy'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 运行设置卡片
                    {
                        'component': 'VCard',
                        'props': {
                            'title': '运行设置',
                            'variant': 'outlined',
                            'class': 'mb-4'
                        },
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VCronField',
                                                        'props': {
                                                            'model': 'cron',
                                                            'label': '签到周期',
                                                            'placeholder': '0 7 * * *',
                                                            'hint': 'Cron表达式，默认每天7点执行',
                                                            'prepend-inner-icon': 'mdi-clock-outline'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'random_delay',
                                                            'label': '随机延迟(秒)',
                                                            'placeholder': '100-200 随机延迟100-200秒',
                                                            'prepend-inner-icon': 'mdi-timer-outline',
                                                            'hint': '设置随机延迟范围，防止被风控'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'history_days',
                                                            'label': '保留历史天数',
                                                            'type': 'number',
                                                            'prepend-inner-icon': 'mdi-calendar-clock',
                                                            'hint': '设置历史记录保留天数'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 3
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'model': 'notify_style',
                                                            'label': '通知样式',
                                                            'items': [
                                                                {'title': '简约风格', 'value': 'style1', 'prepend-icon': 'mdi-view-dashboard'},
                                                                {'title': '清新风格', 'value': 'style2', 'prepend-icon': 'mdi-flower'},
                                                                {'title': '科技风格', 'value': 'style3', 'prepend-icon': 'mdi-rocket'},
                                                                {'title': '商务风格', 'value': 'style4', 'prepend-icon': 'mdi-briefcase'},
                                                                {'title': '优雅风格', 'value': 'style5', 'prepend-icon': 'mdi-star'}
                                                            ],
                                                            'prepend-inner-icon': 'mdi-palette',
                                                            'hint': '选择通知消息的显示样式',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # Cookie设置卡片
                    {
                        'component': 'VCard',
                        'props': {
                            'title': 'Cookie设置',
                            'variant': 'outlined',
                            'class': 'mb-4'
                        },
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextarea',
                                                        'props': {
                                                            'model': 'cookie',
                                                            'label': 'Cnlang Cookie',
                                                            'rows': 5,
                                                            'placeholder': '请填写您的Cookie信息',
                                                            'prepend-inner-icon': 'mdi-cookie',
                                                            'hint': '从浏览器中获取的Cookie信息'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 使用说明卡片
                    {
                        'component': 'VCard',
                        'props': {
                            'variant': 'outlined',
                            'class': 'mb-4'
                        },
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {
                                    'class': 'text-h6'
                                },
                                'content': [
                                    {
                                        'component': 'VIcon',
                                        'props': {
                                            'color': 'info',
                                            'class': 'me-2'
                                        },
                                        'text': 'mdi-help-circle'
                                    },
                                    {
                                        'component': 'span',
                                        'props': {
                                            'class': 'font-weight-bold'
                                        },
                                        'text': '使用说明'
                                    }
                                ]
                            },
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'mb-4'
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'd-flex align-center mb-2'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VIcon',
                                                        'props': {
                                                            'color': 'amber',
                                                            'class': 'me-2'
                                                        },
                                                        'text': 'mdi-star'
                                                    },
                                                    {'component': 'span', 'text': '特别鸣谢 imaliang 大佬，插件源码来自于他的脚本。'}
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'd-flex align-center mb-2'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VIcon',
                                                        'props': {
                                                            'color': 'success',
                                                            'class': 'me-2'
                                                        },
                                                        'text': 'mdi-rocket'
                                                    },
                                                    {'component': 'span', 'text': '一键自动签到，省心省力。'}
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'd-flex align-center mb-2'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VIcon',
                                                        'props': {
                                                            'color': 'info',
                                                            'class': 'me-2'
                                                        },
                                                        'text': 'mdi-clock-outline'
                                                    },
                                                    {'component': 'span', 'text': '灵活定时，支持自定义周期与随机延迟。'}
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'd-flex align-center mb-2'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VIcon',
                                                        'props': {
                                                            'color': 'warning',
                                                            'class': 'me-2'
                                                        },
                                                        'text': 'mdi-bell'
                                                    },
                                                    {'component': 'span', 'text': '多样通知，签到结果实时推送。'}
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'd-flex align-center mb-2'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VIcon',
                                                        'props': {
                                                            'color': 'primary',
                                                            'class': 'me-2'
                                                        },
                                                        'text': 'mdi-calendar'
                                                    },
                                                    {'component': 'span', 'text': '历史记录清晰可查，数据本地安全保存。'}
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VDivider',
                                        'props': {
                                            'class': 'my-4'
                                        }
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'text-subtitle-1 font-weight-bold mb-3'
                                        },
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {
                                                    'color': 'primary',
                                                    'class': 'me-2'
                                                },
                                                'text': 'mdi-cookie'
                                            },
                                            {
                                                'component': 'span',
                                                'text': '获取Cookie步骤：'
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'ml-6'
                                        },
                                        'content': [
                                            {
                                                'component': 'ol',
                                                'props': {
                                                    'class': 'mb-4'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'li',
                                                        'props': {
                                                            'class': 'mb-2'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'text': '使用浏览器（建议使用Chrome或Edge）访问 '
                                                            },
                                                            {
                                                                'component': 'a',
                                                                'props': {
                                                                    'href': 'https://bbs.cnlang.org/',
                                                                    'target': '_blank',
                                                                    'class': 'text-decoration-underline text-primary',
                                                                    'style': 'transition: all 0.3s ease; text-decoration-thickness: 1px; text-underline-offset: 2px;'
                                                                },
                                                                'text': 'bbs.cnlang.org'
                                                            },
                                                            {
                                                                'component': 'span',
                                                                'text': ' 并登录您的账号'
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        'component': 'li',
                                                        'props': {
                                                            'class': 'mb-2'
                                                        },
                                                        'text': '按F12打开开发者工具（或右键点击页面，选择"检查"）'
                                                    },
                                                    {
                                                        'component': 'li',
                                                        'props': {
                                                            'class': 'mb-2'
                                                        },
                                                        'text': '在开发者工具中，切换到"网络/Network"标签'
                                                    },
                                                    {
                                                        'component': 'li',
                                                        'props': {
                                                            'class': 'mb-2'
                                                        },
                                                        'text': '刷新页面，在网络请求列表中找到 bbs.cnlang.org'
                                                    },
                                                    {
                                                        'component': 'li',
                                                        'props': {
                                                            'class': 'mb-2'
                                                        },
                                                        'text': '点击该请求，在右侧详情中找到"请求标头/Headers"部分'
                                                    },
                                                    {
                                                        'component': 'li',
                                                        'props': {
                                                            'class': 'mb-2'
                                                        },
                                                        'text': '找到"Cookie:"开头的行，复制整行Cookie值（不包含"Cookie:"前缀）'
                                                    },
                                                    {
                                                        'component': 'li',
                                                        'text': '将复制的Cookie值粘贴到插件的Cookie设置框中'
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'mt-3 pa-4',
                                            'style': 'background-color: rgba(var(--v-theme-warning), 0.1); border-radius: 8px;'
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'd-flex align-center mb-3'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VIcon',
                                                        'props': {
                                                            'color': 'warning',
                                                            'class': 'me-2'
                                                        },
                                                        'text': 'mdi-alert'
                                                    },
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-subtitle-1 font-weight-bold'
                                                        },
                                                        'text': '注意事项：'
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'ml-8'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'ul',
                                                        'props': {
                                                            'class': 'mb-0'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'li',
                                                                'props': {
                                                                    'class': 'mb-2'
                                                                },
                                                                'text': 'Cookie通常会在一段时间后失效，如遇签到失败请更新Cookie'
                                                            },
                                                            {
                                                                'component': 'li',
                                                                'props': {
                                                                    'class': 'mb-2'
                                                                },
                                                                'text': '请勿泄露您的Cookie给他人，以免账号被盗用'
                                                            },
                                                            {
                                                                'component': 'li',
                                                                'text': '建议开启通知功能，及时了解签到状态'
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "clear": False,
            "cookie": "",
            "random_delay": "",
            "history_days": 30,
            "cron": "0 7 * * *",
            "notify_style": "style1",
            "use_proxy": False
        }

    # ------------------------------------------------------------------
    # 详情页面
    # ------------------------------------------------------------------

    def get_page(self) -> List[dict]:
        """拼装详情页面：账号信息、签到状态与历史统计三张卡片。"""
        status = self.get_status_summary()
        history = self.get_data(KEY_HISTORY) or []
        stats = self._analyze_history(history)

        # 账号信息卡片
        account_card = {
            'component': 'VCard',
            'props': {
                'variant': 'outlined',
                'class': 'mb-4'
            },
            'content': [
                {
                    'component': 'VCardTitle',
                    'props': {
                        'class': 'text-h6'
                    },
                    'content': [
                        {
                            'component': 'VIcon',
                            'props': {
                                'color': 'primary',
                                'class': 'me-2'
                            },
                            'text': 'mdi-account'
                        },
                        {
                            'component': 'span',
                            'text': '账号信息'
                        }
                    ]
                },
                {
                    'component': 'VCardText',
                    'content': [
                        {
                            'component': 'VRow',
                            'props': {
                                'dense': True
                            },
                            'content': [
                                # 用户名
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'primary',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-account-circle'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '用户名'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': status.get("account", {}).get("username", "未知")
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 用户组
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'info',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-account-group'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '用户组'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': status.get("account", {}).get("usergroup", "未知")
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 大洋余额
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'success',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-currency-usd'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '大洋余额'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': status.get("account", {}).get("money", "0")
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # Cookie状态
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'success' if status.get("account", {}).get("cookie_status") == "有效" else 'error',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-cookie' if status.get("account", {}).get("cookie_status") == "有效" else 'mdi-cookie-off'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': 'Cookie状态'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': status.get("account", {}).get("cookie_status", "无效")
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        # 状态展示卡片
        status_card = {
            'component': 'VCard',
            'props': {
                'variant': 'outlined',
                'class': 'mb-4'
            },
            'content': [
                {
                    'component': 'VCardTitle',
                    'props': {
                        'class': 'text-h6'
                    },
                    'content': [
                        {
                            'component': 'VIcon',
                            'props': {
                                'color': 'primary',
                                'class': 'me-2'
                            },
                            'text': 'mdi-information'
                        },
                        {
                            'component': 'span',
                            'text': '签到状态'
                        }
                    ]
                },
                {
                    'component': 'VCardText',
                    'content': [
                        {
                            'component': 'VRow',
                            'props': {
                                'dense': True
                            },
                            'content': [
                                # 服务状态
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 4
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'success' if status["status"] == "运行中" else 'error',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-power' if status["status"] == "运行中" else 'mdi-power-off'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '服务状态'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': status["status"]
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 下次签到时间
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 4
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'info',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-clock-outline'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '下次签到'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': status["next_sign_time"]
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 连续签到
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 4
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'warning',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-calendar-check'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '连续签到'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': f"{status['continuous_days']} 天"
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 本月签到
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 4
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'primary',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-calendar-month'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '本月签到'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': f"{status['month_signs']} 次"
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 总签到次数
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 4
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'success',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-counter'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '总签到'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': f"{status['total_signs']} 次"
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 最后签到状态
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 4
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'success' if status["last_sign_status"] == "成功" else 'error',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-check-circle' if status["last_sign_status"] == "成功" else 'mdi-alert-circle'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '最后签到'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': status["last_sign_status"]
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        # 统计分析卡片
        stats_card = {
            'component': 'VCard',
            'props': {
                'variant': 'outlined',
                'class': 'mb-4'
            },
            'content': [
                {
                    'component': 'VCardTitle',
                    'props': {
                        'class': 'text-h6'
                    },
                    'content': [
                        {
                            'component': 'VIcon',
                            'props': {
                                'color': 'primary',
                                'class': 'me-2'
                            },
                            'text': 'mdi-chart-box'
                        },
                        {
                            'component': 'span',
                            'text': '签到统计'
                        }
                    ]
                },
                {
                    'component': 'VCardText',
                    'content': [
                        # 统计数据行
                        {
                            'component': 'VRow',
                            'props': {
                                'dense': True
                            },
                            'content': [
                                # 签到成功率
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'success',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-percent'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '签到成功率'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': stats["success_rate"]
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 累计获得大洋
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'warning',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-currency-usd'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '累计大洋'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': str(stats["total_money"])
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 平均每次大洋
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'info',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-calculator'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '平均大洋'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': str(stats["avg_money"])
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                # 最佳签到时间
                                {
                                    'component': 'VCol',
                                    'props': {
                                        'cols': 12,
                                        'sm': 6,
                                        'md': 3
                                    },
                                    'content': [
                                        {
                                            'component': 'VCard',
                                            'props': {
                                                'variant': 'outlined',
                                                'class': 'mb-2'
                                            },
                                            'content': [
                                                {
                                                    'component': 'VCardText',
                                                    'props': {
                                                        'class': 'd-flex align-center'
                                                    },
                                                    'content': [
                                                        {
                                                            'component': 'VIcon',
                                                            'props': {
                                                                'color': 'primary',
                                                                'class': 'me-2'
                                                            },
                                                            'text': 'mdi-clock-outline'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'content': [
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-subtitle-2'
                                                                    },
                                                                    'text': '最佳时间'
                                                                },
                                                                {
                                                                    'component': 'div',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': stats["best_time"]
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                        # 历史记录表格
                        {
                            'component': 'VDivider',
                            'props': {
                                'class': 'my-4'
                            }
                        },
                        {
                            'component': 'div',
                            'props': {
                                'class': 'text-subtitle-1 d-flex align-center mb-4'
                            },
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {
                                        'color': 'primary',
                                        'class': 'me-2',
                                        'size': 'small'
                                    },
                                    'text': 'mdi-history'
                                },
                                {
                                    'component': 'span',
                                    'text': '签到历史'
                                }
                            ]
                        },
                        {
                            'component': 'VTable',
                            'props': {
                                'hover': True,
                                'density': 'compact',
                                'class': 'sign-history-table',
                                'style': 'background: transparent;'
                            },
                            'content': [
                                {
                                    'component': 'thead',
                                    'content': [
                                        {
                                            'component': 'tr',
                                            'props': {
                                                'style': 'background: rgba(var(--v-theme-surface-variant), 0.1);'
                                            },
                                            'content': [
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-caption font-weight-bold text-primary'
                                                    },
                                                    'text': '时间'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-caption font-weight-bold text-primary'
                                                    },
                                                    'text': '账号'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-caption font-weight-bold text-primary'
                                                    },
                                                    'text': '连续签到次数'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-caption font-weight-bold text-primary'
                                                    },
                                                    'text': '当前大洋'
                                                },
                                                {
                                                    'component': 'th',
                                                    'props': {
                                                        'class': 'text-caption font-weight-bold text-primary'
                                                    },
                                                    'text': '响应'
                                                }
                                            ]
                                        }
                                    ]
                                },
                                {
                                    'component': 'tbody',
                                    'content': [
                                        {
                                            'component': 'tr',
                                            'props': {
                                                'style': 'background: rgba(var(--v-theme-surface), 0.02);'
                                            },
                                            'content': [
                                                {
                                                    'component': 'td',
                                                    'props': {
                                                        'class': 'text-caption text-medium-emphasis'
                                                    },
                                                    'text': h.get("date")
                                                },
                                                {
                                                    'component': 'td',
                                                    'props': {
                                                        'class': 'text-caption text-medium-emphasis'
                                                    },
                                                    'text': h.get("username")
                                                },
                                                {
                                                    'component': 'td',
                                                    'props': {
                                                        'class': 'text-caption text-medium-emphasis'
                                                    },
                                                    'text': str(h.get("totalContinuousCheckIn"))
                                                },
                                                {
                                                    'component': 'td',
                                                    'props': {
                                                        'class': 'text-caption text-medium-emphasis'
                                                    },
                                                    'text': str(h.get("money"))
                                                },
                                                {
                                                    'component': 'td',
                                                    'props': {
                                                        'class': 'text-caption text-medium-emphasis'
                                                    },
                                                    'text': h.get("content")
                                                }
                                            ]
                                        } for h in sorted(history, key=lambda x: x.get("date", ""), reverse=True)
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        return [account_card, status_card, stats_card]
