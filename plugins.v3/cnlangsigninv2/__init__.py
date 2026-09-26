import random
import re
import threading
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils


class CnlangSigninV2(_PluginBase):
    """国语视界（cnlang.org）站点自动签到插件，MoviePilot V3 规范实现。"""

    # 插件名称
    plugin_name = "国语视界签到V2"
    # 插件描述
    plugin_desc = "美观实用的站点签到助手"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/xijin285/MoviePilot-Plugins/refs/heads/main/icons/cnlang.png"
    # 插件版本
    plugin_version = "3.0.0"
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

    # 私有属性
    _enabled = False
    _cron = "0 7 * * *"
    _cookie = None
    _onlyonce = False
    _notify = False
    _history_days = 30
    _random_delay = None
    _clear = False
    _notify_style = "style1"
    _use_proxy = False

    # 站点基础地址
    _base_url = "https://cnlang.org"
    # 签到心情默认文案（一言接口不可用时的兜底）
    _default_todaysay = "一别之后，两地相思，只道是三四月，又谁知五六年。"

    # 通知样式模板：title 标题、top/bottom 上下边框、prefix 每行前缀
    _NOTIFY_STYLES = {
        "style1": {"title": "🎬 国语视界签到", "top": "━━━━━━━━━━━━━━━━━━━━━━", "bottom": "━━━━━━━━━━━━━━━━━━━━━━", "prefix": ""},
        "style2": {"title": "🌸 国语视界签到", "top": "┏━━━━━━━━━━━━━━━━━━━━┓", "bottom": "┗━━━━━━━━━━━━━━━━━━━━┛", "prefix": "┃ "},
        "style3": {"title": "🚀 国语视界签到", "top": "━━━━━━━━━━━━━━━━━━━━━━", "bottom": "━━━━━━━━━━━━━━━━━━━━━━", "prefix": ""},
        "style4": {"title": "📊 国语视界签到", "top": "━━━━━━━━━━━━━━━━━━━━━━", "bottom": "━━━━━━━━━━━━━━━━━━━━━━", "prefix": ""},
        "style5": {"title": "✨ 国语视界签到", "top": "━━━━━━━━━━━━━━━━━━━━━━", "bottom": "━━━━━━━━━━━━━━━━━━━━━━", "prefix": ""},
    }

    def init_plugin(self, config: dict = None):
        """读取配置并建立本次运行状态；可重复调用，定时任务由 get_service() 托管给宿主调度器。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._cron = config.get("cron") or "0 7 * * *"
        self._cookie = config.get("cookie")
        self._notify = bool(config.get("notify"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._random_delay = config.get("random_delay")
        self._clear = bool(config.get("clear"))
        self._notify_style = config.get("notify_style") or "style1"
        self._use_proxy = bool(config.get("use_proxy"))
        try:
            self._history_days = int(config.get("history_days") or 30)
        except (TypeError, ValueError):
            self._history_days = 30

        # 清除历史记录（一次性开关，执行后回写配置关闭）
        if self._clear:
            self.del_data("history")
            self._clear = False
            self.__update_config()
            logger.info("签到历史记录已清除")

        # 立即运行一次：放入守护线程，避免阻塞插件初始化
        if self._onlyonce:
            self._onlyonce = False
            self.__update_config()
            logger.info("收到立即运行指令，后台执行签到...")
            threading.Thread(target=self.signin, daemon=True,
                             name="CnlangSigninV2.Once").start()

    def __update_config(self):
        """把当前内存中的配置回写到宿主配置存储。"""
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "cookie": self._cookie,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "history_days": self._history_days,
            "random_delay": self._random_delay,
            "clear": self._clear,
            "notify_style": self._notify_style,
            "use_proxy": self._use_proxy,
        })

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程命令 /cnlang_signin，通过 PluginAction 事件路由到本插件。"""
        return [{
            "cmd": "/cnlang_signin",
            "event": EventType.PluginAction,
            "desc": "国语视界签到",
            "category": "站点",
            "data": {
                "action": "cnlang_signin"
            }
        }]

    def get_api(self) -> list[dict[str, Any]]:
        """本插件不注册后端 API。"""
        return []

    def get_service(self) -> list[dict]:
        """启用且配置了 cron 时，向宿主调度器注册定时签到服务；停用即自动摘除。"""
        if not self.get_state() or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError:
            logger.error(f"Cron 表达式无效：{self._cron}，定时签到未注册")
            return []
        return [{
            "id": "CnlangSigninV2.Signin",
            "name": "国语视界定时签到",
            "trigger": trigger,
            "func": self.signin,
            "kwargs": {},
        }]

    def stop_service(self):
        """释放后台资源：定时任务由宿主调度器托管，随插件停用自动回收，无需额外清理。"""
        logger.info("国语视界签到服务已停止")

    # ------------------------------------------------------------------
    # 签到核心流程
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.PluginAction)
    def signin(self, event: Event = None):
        """执行签到：远程命令入口与定时服务共用的核心流程。"""
        if event:
            event_data = event.event_data or {}
            if event_data.get("action") != "cnlang_signin":
                return
            logger.info("收到签到命令，开始执行...")

        if not self._cookie:
            self.__notify(False, "未配置Cookie")
            return

        # 随机延迟，降低被站点风控的概率
        self.__random_sleep()

        # 步骤1：获取签到页面，解析用户名与 formhash
        logger.info("步骤1: 获取签到页面信息...")
        res = RequestUtils(headers=self.__get_headers(), proxies=self.__get_proxies()).get_res(
            url=f"{self._base_url}/dsu_paulsign-sign.html?mobile=no")
        if not res or res.status_code != 200:
            self.__notify(False, f"获取基本信息失败-status_code={res.status_code if res else '无响应'}")
            return

        user_info = res.text or ""

        user_name_match = re.search(r'title="访问我的空间">(.*?)</a>', user_info)
        if not user_name_match:
            self.__notify(False, "未获取到用户名-cookie或许已失效")
            return
        user_name = user_name_match.group(1)
        logger.info(f"登录用户名为：{user_name}")

        if re.search(r'(您今天已经签到过了或者签到时间还未开始)', user_info):
            self.__notify(True, "您今天已经签到过了或者签到时间还未开始")
            return

        formhash_match = re.search(r'<input[^>]*name="formhash"[^>]*value="([^"]*)"', user_info)
        if not formhash_match:
            self.__notify(False, "未获取到 formhash 值")
            return
        formhash_value = formhash_match.group(1)
        logger.info(f"formhash：{formhash_value}")

        month_match = re.search(r'<p>您本月已累计签到:<b>(\d+)</b>', user_info)
        total_continuous_check_in = int(month_match.group(1)) + 1 if month_match else 1
        logger.info(f"您本月已累计签到：{total_continuous_check_in}")

        # 步骤2：提交签到请求
        todaysay = self.__get_todaysay()
        logger.info(f"最终想说的话：{todaysay}")
        logger.info("步骤2: 提交签到请求...")
        res = RequestUtils(headers=self.__get_headers(), proxies=self.__get_proxies()).post_res(
            url=f"{self._base_url}/plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1",
            data={
                "formhash": formhash_value,
                "qdxq": "kx",
                "qdmode": "1",
                "todaysay": todaysay,
                "fastreply": "0",
            })
        if not res or res.status_code != 200:
            self.__notify(False, f"请求签到接口失败-status_code={res.status_code if res else '无响应'}")
            return

        content_match = re.search(r'<div class="c">(.*?)</div>', res.text or "", re.DOTALL)
        if not content_match:
            self.__notify(False, "获取签到后的响应内容失败")
            return
        content = content_match.group(1).strip()
        logger.info(content)

        # 步骤3：获取积分信息
        logger.info("步骤3: 获取积分信息...")
        money = self.__fetch_money()
        logger.info(f"当前大洋余额：{money}")

        sign_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        text = (f"签到账号：{user_name}\n"
                f"累计签到：{total_continuous_check_in} 天\n"
                f"当前大洋：{money}\n"
                f"签到时间：{sign_time}\n"
                f"{content}")
        self.__notify(True, text)

        # 保存签到历史，并按保留天数裁剪
        history = self.get_data("history") or []
        history.append({
            "date": sign_time,
            "username": user_name,
            "totalContinuousCheckIn": total_continuous_check_in,
            "money": money,
            "content": content,
        })
        deadline = time.time() - self._history_days * 24 * 60 * 60
        history = [record for record in history
                   if self.__parse_time(record.get("date"))
                   and self.__parse_time(record.get("date")).timestamp() >= deadline]
        self.save_data(key="history", value=history)

    # ------------------------------------------------------------------
    # 签到辅助方法
    # ------------------------------------------------------------------

    def __get_headers(self) -> dict:
        """构造携带 Cookie 的请求头（Host 与压缩协商由 HTTP 客户端自动处理）。"""
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            "Cache-Control": "max-age=0",
            "Upgrade-Insecure-Requests": "1",
            "Cookie": self._cookie or "",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

    def __get_proxies(self) -> Optional[dict]:
        """按配置返回系统代理；未开启或未配置系统代理时返回 None。"""
        if not self._use_proxy:
            return None
        proxy = getattr(settings, "PROXY", None)
        if not proxy:
            logger.warning("已开启使用代理，但未配置系统代理")
            return None
        logger.info("使用系统代理访问站点")
        return proxy

    def __random_sleep(self):
        """按 100-200 形式的配置随机 sleep；配置为空或格式错误时不延迟。"""
        if not self._random_delay:
            return
        try:
            start, end = map(int, str(self._random_delay).split("-"))
            seconds = random.randint(min(start, end), max(start, end))
        except (ValueError, AttributeError):
            logger.warning("随机延迟设置格式错误（应为 100-200），本次不延迟")
            return
        if seconds > 0:
            logger.info(f"随机延迟 {seconds} 秒...")
            time.sleep(seconds)

    def __get_todaysay(self) -> str:
        """从一言接口随机获取 6-50 字的签到心情，多次失败时使用默认文案。"""
        for attempt in range(1, 11):
            try:
                res = RequestUtils().get_res("https://v1.hitokoto.cn/?encode=text")
                text = (res.text or "").strip() if res else ""
                logger.info(f"尝试想说的话-{attempt}: {text}")
                if 6 <= len(text) <= 50:
                    return text
            except Exception as err:
                logger.warning(f"获取一言失败（第{attempt}次）：{err}")
        return self._default_todaysay

    def __fetch_money(self) -> str:
        """请求积分页面并解析当前大洋余额，失败时返回 0。"""
        res = RequestUtils(headers=self.__get_headers(), proxies=self.__get_proxies()).get_res(
            url=f"{self._base_url}/home.php?mod=spacecp&ac=credit&showcredit=1&inajax=1"
                f"&ajaxtarget=extcreditmenu_menu")
        if res and res.status_code == 200:
            match = re.search(r'<span id="hcredit_2">(\d+)</span>', res.text or "")
            if match:
                return match.group(1)
        return "0"

    @staticmethod
    def __parse_time(date_str: Optional[str]) -> Optional[datetime]:
        """安全解析历史记录时间，格式异常时返回 None。"""
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    def __notify(self, success: bool, text: str):
        """记录日志并按所选样式推送签到结果通知。"""
        logger.info(text)
        if not self._notify:
            return

        sign_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        is_cookie_expired = (not success) and ("cookie" in text.lower() or "未获取到用户名" in text)
        style = self._NOTIFY_STYLES.get(self._notify_style) or self._NOTIFY_STYLES["style1"]
        prefix = style["prefix"]

        if success:
            status_line = f"{prefix}✅ 签到成功"
            detail_lines = [f"{prefix}📝 {line}" for line in text.splitlines() if line.strip()]
        else:
            status_line = f"{prefix}❌ {'Cookie已失效' if is_cookie_expired else '签到失败'}"
            detail_lines = [f"{prefix}📝 失败原因：{text}"]

        lines = [style["top"], status_line, style["top"],
                 *detail_lines, f"{prefix}⏰ 执行时间：{sign_time}"]
        if is_cookie_expired:
            lines.append(f"{prefix}🔑 请更新Cookie后重试")
        lines.append(style["bottom"])

        self.post_message(
            mtype=NotificationType.Plugin,
            title=style["title"],
            text="\n".join(lines)
        )

    # ------------------------------------------------------------------
    # 配置页面
    # ------------------------------------------------------------------

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """拼装插件配置页面，返回页面配置与默认配置模型。"""
        return [
            {
                'component': 'VForm',
                'content': [
                    # 基础设置卡片
                    {
                        'component': 'VCard',
                        'props': {'title': '基础设置', 'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
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
                                                'props': {'cols': 12, 'md': 3},
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
                                                'props': {'cols': 12, 'md': 3},
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
                                                'props': {'cols': 12, 'md': 3},
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
                                                'props': {'cols': 12, 'md': 3},
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
                        'props': {'title': '运行设置', 'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
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
                                                'props': {'cols': 12, 'md': 3},
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
                                                'props': {'cols': 12, 'md': 3},
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
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'model': 'notify_style',
                                                            'label': '通知样式',
                                                            'items': [
                                                                {'title': '简约风格', 'value': 'style1'},
                                                                {'title': '清新风格', 'value': 'style2'},
                                                                {'title': '科技风格', 'value': 'style3'},
                                                                {'title': '商务风格', 'value': 'style4'},
                                                                {'title': '优雅风格', 'value': 'style5'}
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
                        'props': {'title': 'Cookie设置', 'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12},
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
                        'props': {'title': '使用说明', 'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'class': 'mb-3',
                                            'text': '一键自动签到，支持自定义周期与随机延迟；签到结果实时推送，历史记录本地安全保存。特别鸣谢 imaliang 大佬，插件签到逻辑参考自他的脚本。'
                                        }
                                    },
                                    {
                                        'component': 'div',
                                        'props': {'class': 'text-subtitle-1 font-weight-bold mb-2'},
                                        'text': '获取Cookie步骤：'
                                    },
                                    {
                                        'component': 'ol',
                                        'props': {'class': 'ml-6 mb-3'},
                                        'content': [
                                            {'component': 'li', 'props': {'class': 'mb-1'},
                                             'text': '使用浏览器（建议 Chrome 或 Edge）访问 bbs.cnlang.org 并登录账号'},
                                            {'component': 'li', 'props': {'class': 'mb-1'},
                                             'text': '按 F12 打开开发者工具，切换到“网络/Network”标签'},
                                            {'component': 'li', 'props': {'class': 'mb-1'},
                                             'text': '刷新页面，在请求列表中找到 bbs.cnlang.org 的请求'},
                                            {'component': 'li', 'props': {'class': 'mb-1'},
                                             'text': '在“请求标头/Headers”中找到 Cookie: 开头的行，复制整行值（不含 Cookie: 前缀）'},
                                            {'component': 'li', 'text': '将复制的 Cookie 粘贴到上方 Cookie 设置框中并保存'}
                                        ]
                                    },
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': 'Cookie 通常会在一段时间后失效，如遇签到失败请更新 Cookie；请勿泄露 Cookie 给他人；建议开启通知功能，及时了解签到状态。'
                                        }
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

    @staticmethod
    def __stat_col(icon: str, color: str, label: str, value: Any, md: int = 3) -> dict:
        """生成一个带图标的统计信息小卡片列。"""
        return {
            'component': 'VCol',
            'props': {'cols': 12, 'sm': 6, 'md': md},
            'content': [
                {
                    'component': 'VCard',
                    'props': {'variant': 'outlined', 'class': 'mb-2'},
                    'content': [
                        {
                            'component': 'VCardText',
                            'props': {'class': 'd-flex align-center'},
                            'content': [
                                {
                                    'component': 'VIcon',
                                    'props': {'color': color, 'class': 'me-2'},
                                    'text': icon
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'div',
                                            'props': {'class': 'text-subtitle-2'},
                                            'text': label
                                        },
                                        {
                                            'component': 'div',
                                            'props': {'class': 'text-h6'},
                                            'text': str(value)
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

    def __section_card(self, icon: str, title: str, cols: list[dict]) -> dict:
        """生成一个带标题的区块卡片，内容为若干统计列。"""
        return {
            'component': 'VCard',
            'props': {'variant': 'outlined', 'class': 'mb-4'},
            'content': [
                {
                    'component': 'VCardTitle',
                    'props': {'class': 'text-h6'},
                    'content': [
                        {
                            'component': 'VIcon',
                            'props': {'color': 'primary', 'class': 'me-2'},
                            'text': icon
                        },
                        {'component': 'span', 'text': title}
                    ]
                },
                {
                    'component': 'VCardText',
                    'content': [
                        {
                            'component': 'VRow',
                            'props': {'dense': True},
                            'content': cols
                        }
                    ]
                }
            ]
        }

    @staticmethod
    def __history_table(history: list) -> dict:
        """生成签到历史表格，按时间倒序展示。"""
        header = ['时间', '账号', '连续签到次数', '当前大洋', '响应']
        return {
            'component': 'VTable',
            'props': {'hover': True, 'density': 'compact', 'style': 'background: transparent;'},
            'content': [
                {
                    'component': 'thead',
                    'content': [
                        {
                            'component': 'tr',
                            'content': [
                                {
                                    'component': 'th',
                                    'props': {'class': 'text-caption font-weight-bold text-primary'},
                                    'text': title
                                } for title in header
                            ]
                        }
                    ]
                },
                {
                    'component': 'tbody',
                    'content': [
                        {
                            'component': 'tr',
                            'content': [
                                {
                                    'component': 'td',
                                    'props': {'class': 'text-caption text-medium-emphasis'},
                                    'text': str(h.get(key, ''))
                                } for key in ("date", "username", "totalContinuousCheckIn", "money", "content")
                            ]
                        } for h in sorted(history, key=lambda x: x.get("date", ""), reverse=True)
                    ]
                }
            ]
        }

    def get_page(self) -> list[dict]:
        """返回插件详情页：账号信息、签到状态、签到统计与历史记录。"""
        status = self.get_status_summary()
        history = self.get_data("history") or []
        stats = self.__analyze_signin_history(history)
        account = status.get("account", {})
        cookie_ok = account.get("cookie_status") == "有效"

        # 账号信息卡片
        account_card = self.__section_card("mdi-account", "账号信息", [
            self.__stat_col("mdi-account-circle", "primary", "用户名", account.get("username", "未知")),
            self.__stat_col("mdi-account-group", "info", "用户组", account.get("usergroup", "未知")),
            self.__stat_col("mdi-currency-usd", "success", "大洋余额", account.get("money", "0")),
            self.__stat_col("mdi-cookie" if cookie_ok else "mdi-cookie-off",
                            "success" if cookie_ok else "error",
                            "Cookie状态", account.get("cookie_status", "无效")),
        ])

        # 签到状态卡片
        running = status["status"] == "运行中"
        last_ok = status["last_sign_status"] == "成功"
        status_card = self.__section_card("mdi-information", "签到状态", [
            self.__stat_col("mdi-power" if running else "mdi-power-off",
                            "success" if running else "error",
                            "服务状态", status["status"], md=4),
            self.__stat_col("mdi-clock-outline", "info", "下次签到", status["next_sign_time"], md=4),
            self.__stat_col("mdi-calendar-check", "warning", "连续签到", f"{status['continuous_days']} 天", md=4),
            self.__stat_col("mdi-calendar-month", "primary", "本月签到", f"{status['month_signs']} 次", md=4),
            self.__stat_col("mdi-counter", "success", "总签到", f"{status['total_signs']} 次", md=4),
            self.__stat_col("mdi-check-circle" if last_ok else "mdi-alert-circle",
                            "success" if last_ok else "error",
                            "最后签到", status["last_sign_status"], md=4),
        ])

        # 签到统计卡片（统计列 + 历史表格）
        stats_card = self.__section_card("mdi-chart-box", "签到统计", [
            self.__stat_col("mdi-percent", "success", "签到成功率", stats["success_rate"]),
            self.__stat_col("mdi-currency-usd", "warning", "累计大洋", stats["total_money"]),
            self.__stat_col("mdi-calculator", "info", "平均大洋", stats["avg_money"]),
            self.__stat_col("mdi-clock-outline", "primary", "最佳时间", stats["best_time"]),
        ])
        # 在统计卡片末尾追加历史记录表格
        stats_card['content'][1]['content'].extend([
            {'component': 'VDivider', 'props': {'class': 'my-4'}},
            {
                'component': 'div',
                'props': {'class': 'text-subtitle-1 d-flex align-center mb-4'},
                'content': [
                    {
                        'component': 'VIcon',
                        'props': {'color': 'primary', 'class': 'me-2', 'size': 'small'},
                        'text': 'mdi-history'
                    },
                    {'component': 'span', 'text': '签到历史'}
                ]
            },
            self.__history_table(history)
        ])

        return [account_card, status_card, stats_card]

    # ------------------------------------------------------------------
    # 状态与统计
    # ------------------------------------------------------------------

    def get_status_summary(self) -> dict:
        """获取服务状态摘要：实时请求站点获取账号信息，并结合本地历史补充统计。"""
        status_data = {
            "status": "运行中" if self._enabled else "已停止",
            "next_sign_time": self.__next_run_time(),
            "last_sign_time": "无",
            "last_sign_status": "无",
            "continuous_days": 0,
            "month_signs": 0,
            "total_signs": 0,
            "account": {
                "username": "未知",
                "money": "0",
                "usergroup": "用户",
                "cookie_status": "无效"
            }
        }

        # 本地历史补充：最后签到时间与总次数
        history = self.get_data("history") or []
        sorted_history = sorted(history, key=lambda x: x.get("date", ""), reverse=True)
        if sorted_history:
            status_data["last_sign_time"] = sorted_history[0].get("date", "无")
            if "签到成功" in sorted_history[0].get("content", ""):
                status_data["last_sign_status"] = "成功"
        status_data["total_signs"] = len(sorted_history)

        if not self._cookie:
            return status_data

        try:
            # 签到页面：用户名、本月签到、连续天数、今日是否已签
            res = RequestUtils(headers=self.__get_headers(), proxies=self.__get_proxies()).get_res(
                url=f"{self._base_url}/dsu_paulsign-sign.html?mobile=no")
            if res and res.status_code == 200:
                sign_info = res.text or ""
                username_match = re.search(r'title="访问我的空间">(.*?)</a>', sign_info)
                if username_match:
                    status_data["account"]["username"] = username_match.group(1)
                    status_data["account"]["cookie_status"] = "有效"
                month_match = re.search(r'您本月已累计签到:<b>(\d+)</b>', sign_info)
                if month_match:
                    status_data["month_signs"] = int(month_match.group(1))
                continuous_match = re.search(r'您已经连续签到<b>(\d+)</b>天', sign_info)
                if continuous_match:
                    status_data["continuous_days"] = int(continuous_match.group(1))
                if re.search(r'您今天已经签到过了或者签到时间还未开始', sign_info):
                    status_data["last_sign_status"] = "成功"

            # 积分页面：大洋余额
            status_data["account"]["money"] = self.__fetch_money()

            # 用户组页面：当前用户组
            group_res = RequestUtils(headers=self.__get_headers(), proxies=self.__get_proxies()).get_res(
                url=f"{self._base_url}/home.php?mod=spacecp&ac=usergroup")
            if group_res and group_res.status_code == 200:
                group_match = re.search(r'您目前属于用户组: <strong>(.*?)</strong>', group_res.text or "")
                if group_match:
                    status_data["account"]["usergroup"] = group_match.group(1)
        except Exception as err:
            logger.error(f"获取状态信息失败：{err}")

        return status_data

    def __next_run_time(self) -> str:
        """根据 cron 表达式计算下次签到时间，失败时返回 未设置。"""
        if not (self._enabled and self._cron):
            return "未设置"
        try:
            trigger = CronTrigger.from_crontab(self._cron)
            next_fire = trigger.get_next_fire_time(None, datetime.now(tz=ZoneInfo(settings.TZ)))
            return next_fire.strftime('%Y-%m-%d %H:%M:%S') if next_fire else "未设置"
        except Exception as err:
            logger.error(f"获取下次运行时间失败：{err}")
            return "未设置"

    def __analyze_signin_history(self, history: list) -> dict:
        """分析签到历史：成功率、大洋统计、连续天数与最佳签到时段。"""
        empty = {
            "success_rate": "0%",
            "total_days": 0,
            "success_days": 0,
            "fail_days": 0,
            "total_money": 0,
            "avg_money": 0,
            "max_continuous": 0,
            "current_continuous": 0,
            "best_time": "无",
        }
        if not history:
            return empty

        total_days = len(history)
        success_records = [h for h in history if "签到成功" in h.get("content", "")]
        success_days = len(success_records)
        success_rate = f"{(success_days / total_days * 100):.1f}%"

        # 大洋统计
        total_money = sum(int(h.get("money")) for h in history if str(h.get("money", "")).isdigit())
        avg_money = f"{total_money / success_days:.1f}" if success_days > 0 else "0"

        # 连续签到：按成功签到的去重日期倒序计算
        dates = sorted({d for d in (self.__parse_time(h.get("date")) for h in success_records) if d},
                       reverse=True)
        max_continuous = current_continuous = 0
        if dates:
            streak = 1
            max_continuous = 1
            for i in range(1, len(dates)):
                if (dates[i - 1].date() - dates[i].date()).days == 1:
                    streak += 1
                    max_continuous = max(max_continuous, streak)
                else:
                    streak = 1
            current_continuous = 1
            for i in range(1, len(dates)):
                if (dates[i - 1].date() - dates[i].date()).days == 1:
                    current_continuous += 1
                else:
                    break

        # 最佳签到时段：成功记录中出现次数最多的小时
        hour_stats = {}
        for h in success_records:
            parsed = self.__parse_time(h.get("date"))
            if parsed:
                hour_stats[parsed.hour] = hour_stats.get(parsed.hour, 0) + 1
        best_hour = max(hour_stats.items(), key=lambda x: x[1])[0] if hour_stats else None
        best_time = f"{best_hour:02d}:00" if best_hour is not None else "无"

        return {
            "success_rate": success_rate,
            "total_days": total_days,
            "success_days": success_days,
            "fail_days": total_days - success_days,
            "total_money": total_money,
            "avg_money": avg_money,
            "max_continuous": max_continuous,
            "current_continuous": current_continuous,
            "best_time": best_time,
        }
