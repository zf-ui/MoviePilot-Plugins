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
    plugin_name = "国语视界签到V3"
    # 插件描述
    plugin_desc = "美观实用的站点签到助手"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/xijin285/MoviePilot-Plugins/refs/heads/main/icons/cnlang.png"
    # 插件版本
    plugin_version = "3.4.1"
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
    _user_agent = None
    _use_browser = False
    _username = None
    _password = None

    # 站点基础地址
    _base_url = "https://cnlang.org"
    # 显式直连：requests 默认会读取 HTTP(S)_PROXY 环境变量，传 None 值字典可强制绕过
    _DIRECT_PROXIES = {"http": None, "https": None}
    # 默认 UA；Cloudflare 的 cf_clearance 与 UA 绑定，建议配置为与浏览器一致
    _default_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
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
        self._user_agent = config.get("user_agent")
        self._use_browser = bool(config.get("use_browser"))
        self._username = config.get("username")
        self._password = config.get("password")
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
            "user_agent": self._user_agent,
            "use_browser": self._use_browser,
            "username": self._username,
            "password": self._password,
        })

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程命令 /cnlang_qiandao，通过 PluginAction 事件路由到本插件。

        Telegram BotCommand 仅允许 a-z、0-9、下划线且不超过32字符，命令不能含中文；
        中文说明放在 desc 中展示。
        """
        return [{
            "cmd": "/cnlang_qiandao",
            "event": EventType.PluginAction,
            "desc": "新的一天打卡签到（国语视界）",
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
        sign_page_url = f"{self._base_url}/dsu_paulsign-sign.html?mobile=no"
        res = self.__get_res(sign_page_url)
        # 被 Cloudflare 拦截且开启了浏览器模式时，由浏览器完成整个签到流程
        if self.__is_cf_blocked(res) and self._use_browser:
            logger.info("疑似被Cloudflare拦截，启动浏览器模式...")
            if self.__signin_by_browser():
                return  # 浏览器模式已完成本次签到流程（含成功与明确的失败通知）
            logger.info("浏览器模式不可用，重试直接请求...")
            res = self.__get_res(sign_page_url)
        if not res or res.status_code != 200:
            reason = (f"status_code={res.status_code}" if res
                      else "无响应（网络不通或被Cloudflare拦截：请确认Cookie包含cf_clearance且UA与浏览器一致）")
            self.__notify(False, f"获取基本信息失败-{reason}")
            return

        user_info = res.text or ""

        user_name_match = re.search(r'title="访问我的空间">(.*?)</a>', user_info)
        if not user_name_match:
            # 登录态失效：配置了账号密码且开启浏览器模式时，自动登录并接管签到
            if self._use_browser and self._username and self._password:
                logger.info("登录态失效，启动浏览器模式自动登录并签到...")
                if self.__signin_by_browser():
                    return
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
        res = self.__post_res(
            url=f"{self._base_url}/plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1",
            data={
                "formhash": formhash_value,
                "qdxq": "kx",
                "qdmode": "1",
                "todaysay": todaysay,
                "fastreply": "0",
            })
        if not res or res.status_code != 200:
            reason = f"status_code={res.status_code}" if res else "无响应（请检查网络或代理设置）"
            self.__notify(False, f"请求签到接口失败-{reason}")
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
        self.__save_history(user_name, total_continuous_check_in, money, content, sign_time)

    def __save_history(self, user_name: str, total_check_in: int, money: str,
                       content: str, sign_time: str):
        """保存一条签到历史，并按保留天数裁剪旧记录。"""
        history = self.get_data("history") or []
        history.append({
            "date": sign_time,
            "username": user_name,
            "totalContinuousCheckIn": total_check_in,
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
            "User-Agent": self._user_agent or self._default_ua,
        }

    def __get_proxies(self) -> dict:
        """按配置返回代理：开启时使用系统代理，关闭时显式直连（绕过环境变量代理）。"""
        if not self._use_proxy:
            return self._DIRECT_PROXIES
        proxy = getattr(settings, "PROXY", None)
        if not proxy:
            logger.warning("已开启使用代理，但未配置系统代理，本次直连")
            return self._DIRECT_PROXIES
        logger.info("使用系统代理访问站点")
        return proxy

    @staticmethod
    def __is_cf_blocked(res) -> bool:
        """判断响应是否疑似被 Cloudflare 拦截（无响应超时，或返回 403/503 挑战页）。"""
        return res is None or res.status_code in (403, 503)

    def __refresh_cookies_from_browser(self, ctx, page):
        """合并浏览器新签发的Cookie与原配置Cookie（浏览器值优先），并记录浏览器实际UA。"""
        merged = {}
        for pair in (self._cookie or "").split(";"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                merged[key.strip()] = value.strip()
        for c in ctx.cookies() or []:
            if c.get("name"):
                merged[c["name"]] = c.get("value", "")
        self._cookie = "; ".join(f"{k}={v}" for k, v in merged.items())
        try:
            browser_ua = page.evaluate("navigator.userAgent")
            if browser_ua:
                self._user_agent = browser_ua
        except Exception:
            pass
        self.__update_config()
        logger.info("浏览器模式：Cookie已刷新并保存")

    @staticmethod
    def __is_cf_challenge_page(html: str, title: str) -> bool:
        """判断页面是否为 Cloudflare 挑战页（挑战页标题固定为 Just a moment/请稍候）。"""
        title = title or ""
        if "Just a moment" in title or "请稍候" in title or "Attention Required" in title:
            return True
        return "challenges.cloudflare.com" in (html or "") and "cf-chl" in html

    def __ocr_login_captcha(self, page, dialog: str) -> tuple[Optional[str], Optional[str]]:
        """识别登录验证码，返回 (seccodehash, 识别文本)；识别不可用时返回 (None, None)。"""
        try:
            import base64 as b64
            import ddddocr
        except ImportError:
            logger.error("浏览器模式：缺少 ddddocr 组件，无法识别验证码；"
                         "可在容器内执行 pip install ddddocr 后重试")
            return None, None
        try:
            hash_match = (re.search(r"updateseccode\('(\w+)'", dialog)
                          or re.search(r"idhash=(\w+)", dialog)
                          or re.search(r"seccode_(\w+)", dialog))
            if not hash_match:
                logger.error("浏览器模式：未找到验证码标识")
                return None, None
            seccodehash = hash_match.group(1)

            # 在浏览器页面上下文内取验证码图片（保持会话一致），转 base64 交给 OCR
            img_b64 = page.evaluate(
                """async (url) => {
                    const resp = await fetch(url);
                    const buf = new Uint8Array(await resp.arrayBuffer());
                    let bin = '';
                    for (let i = 0; i < buf.length; i++) bin += String.fromCharCode(buf[i]);
                    return btoa(bin);
                }""",
                f"/misc.php?mod=seccode&update={random.randint(10000, 99999)}&idhash={seccodehash}")
            if not img_b64:
                logger.error("浏览器模式：验证码图片获取失败")
                return None, None
            ocr = ddddocr.DdddOcr(show_ad=False)
            code = ocr.classification(b64.b64decode(img_b64))
            logger.info(f"浏览器模式：验证码识别结果 [{code}]")
            return seccodehash, code
        except Exception as err:
            logger.error(f"浏览器模式：验证码识别异常 - {err}")
            return None, None

    def __login_by_browser(self, page) -> bool:
        """在浏览器页面上下文内用账号密码提交登录，支持验证码自动识别与重试。"""
        if not (self._username and self._password):
            logger.error("浏览器模式：未配置登录账号密码，无法自动登录")
            return False

        # Discuz 默认每15分钟允许5次登录失败，最多尝试4次预留余量
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"浏览器模式：正在使用账号密码自动登录（第{attempt}次）...")
                # 拉取登录浮窗：含本次会话的 formhash，需要验证码时也含 seccodehash
                dialog = page.evaluate(
                    """async () => {
                        const resp = await fetch('/member.php?mod=logging&action=login&infloat=yes&handlekey=login&inajax=1&ajaxtarget=fwin_content_login');
                        return await resp.text();
                    }""") or ""
                formhash_match = re.search(r'name="formhash"[^>]*value="([^"]*)"', dialog)
                if not formhash_match:
                    logger.error("浏览器模式：未获取到登录 formhash")
                    return False

                payload = {
                    "loginfield": "username",
                    "username": self._username,
                    "password": self._password,
                    "questionid": "0",
                    "answer": "",
                    "formhash": formhash_match.group(1),
                }

                # 登录浮窗包含验证码时，自动识别并携带
                if "seccode" in dialog:
                    seccodehash, code = self.__ocr_login_captcha(page, dialog)
                    if not code:
                        return False
                    payload["seccodehash"] = seccodehash
                    payload["seccodeverify"] = code

                login_resp = page.evaluate(
                    """async (data) => {
                        const resp = await fetch('/member.php?mod=logging&action=login&loginsubmit=yes&inajax=1', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                            body: new URLSearchParams(data).toString()
                        });
                        return await resp.text();
                    }""",
                    payload) or ""

                if "succeedhandle" in login_resp or "欢迎您回来" in login_resp:
                    logger.info("浏览器模式：账号密码登录成功")
                    return True

                # Discuz 错误回调形如 errorhandle_xxx('错误信息')，下划线后缀可能为空
                err_match = re.search(r"errorhandle_\w*\('(.*?)'", login_resp)
                err_text = err_match.group(1) if err_match else re.sub(r"<[^>]+>", "", login_resp)[:150]
                logger.error(f"浏览器模式：登录失败 - {err_text}")

                if "验证码" not in err_text:
                    # 账号密码错误等，重试无意义
                    return False
                if attempt < max_attempts:
                    logger.info("浏览器模式：验证码识别错误，换一个新验证码重试...")
                    continue
                logger.error("浏览器模式：验证码多次识别错误，请稍后重试或手动登录一次后复制Cookie")
                return False
            except Exception as err:
                logger.error(f"浏览器模式：自动登录异常 - {err}")
                return False
        return False

    def __signin_by_browser(self) -> bool:
        """CF拦截时的完整浏览器签到：过验证、读页面、浏览器内提交签到、刷新Cookie。

        cf_clearance 与 TLS 指纹绑定，requests 无法复用浏览器拿到的通行证，
        因此签到请求必须在浏览器页面上下文内通过 fetch 完成。
        返回 True 表示已完整处理（含成功与明确的失败通知），False 表示浏览器不可用。
        """
        try:
            from app.sdk.browser import launch_browser_context
        except ImportError:
            logger.error("当前宿主不支持浏览器自动化，无法使用浏览器模式")
            return False

        ctx = None
        page = None
        try:
            launch_kwargs = {"headless": True}
            # 浏览器使用与插件一致的 UA，保证 cf_clearance 对 requests 流程同样有效
            if self._user_agent:
                launch_kwargs["user_agent"] = self._user_agent
            # 开启代理时浏览器同样走系统代理
            proxy_url = None
            if self._use_proxy:
                proxy = getattr(settings, "PROXY", None)
                if proxy and proxy.get("https"):
                    proxy_url = proxy["https"]

            # 诊断：确认配置Cookie中是否包含论坛登录态（auth）
            has_auth = "_auth=" in (self._cookie or "")
            cookie_keys = [p.split("=", 1)[0].strip() for p in (self._cookie or "").split(";") if "=" in p]
            logger.info(f"浏览器模式：配置Cookie包含 {len(cookie_keys)} 个字段，"
                        f"论坛登录态(auth)：{'有' if has_auth else '【无】'}")

            # 网络模式：配置了代理先走代理，失败后自动切直连重试一轮（代理失效不至于整轮报废）
            network_modes = ["proxy", "direct"] if proxy_url else ["direct"]
            passed = False
            for mode in network_modes:
                if ctx:
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    ctx, page = None, None
                if mode == "proxy":
                    launch_kwargs["proxy"] = {"server": proxy_url}
                    logger.info("浏览器模式：按系统代理访问站点...")
                else:
                    launch_kwargs.pop("proxy", None)
                    if len(network_modes) > 1:
                        logger.info("浏览器模式：代理模式失败，改为直连重试...")

                try:
                    ctx = launch_browser_context(**launch_kwargs)
                    page = ctx.new_page()
                    page.set_default_timeout(60000)
                    if self._cookie:
                        page.set_extra_http_headers({"cookie": self._cookie})

                    # 先访问站点首页：CF 验证对全站生效，首页更容易触发并完成挑战
                    logger.info("浏览器模式：正在访问站点，等待Cloudflare验证...")
                    goto_failed = False
                    try:
                        page.goto(f"{self._base_url}/", wait_until="domcontentloaded", timeout=45000)
                    except Exception as goto_err:
                        # goto 超时不一定是死局：页面可能已部分加载，交给下面的轮询判断
                        goto_failed = True
                        logger.warning(f"浏览器模式：首页加载超时（{goto_err.__class__.__name__}），检查已加载内容...")
                except Exception as launch_err:
                    logger.error(f"浏览器模式：浏览器启动/访问异常 - {launch_err}")
                    continue

                # 轮询等待页面加载出真实内容（cf_clearance 未过期时 CF 不会重新签发，
                # 因此以"不再是挑战页"为通过标准，而不是等待新 cf_clearance 出现）
                for i in range(30):
                    try:
                        html_now = page.content() or ""
                        title_now = page.title() or ""
                    except Exception:
                        html_now, title_now = "", ""
                    # 连接被挂起时页面停留在空白页（about:blank），识别出来避免傻等
                    no_content = len(html_now) < 500 and not title_now
                    if not no_content and not self.__is_cf_challenge_page(html_now, title_now):
                        passed = True
                        break
                    if goto_failed and no_content and i >= 2:
                        logger.error("浏览器模式：服务器未返回页面内容（连接可能被挂起）")
                        break
                    # 每10秒输出一次页面标题，便于诊断卡在哪个环节
                    if i % 5 == 0:
                        logger.info(f"浏览器模式：等待验证中... 当前页面标题：{title_now}")
                    time.sleep(2)

                if passed:
                    break

            if not passed:
                logger.error("浏览器模式：等待超时，Cloudflare验证未通过（可能需要人工完成交互验证）")
                if page:
                    try:
                        shot_path = self.get_data_path() / "cf_challenge_failed.png"
                        shot_path.write_bytes(page.screenshot())
                        logger.error(f"浏览器模式：失败页面截图已保存到 {shot_path}")
                    except Exception:
                        pass
                self.__notify(False, "Cloudflare验证未通过（浏览器模式等待超时）")
                return True

            # CF 已通过，合并刷新 Cookie（保留原论坛登录态）
            self.__refresh_cookies_from_browser(ctx, page)

            # 访问签到页，解析用户名与 formhash
            page.goto(f"{self._base_url}/dsu_paulsign-sign.html?mobile=no",
                      wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            html = page.content() or ""

            user_name_match = re.search(r'title="访问我的空间">(.*?)</a>', html)
            if not user_name_match and self._username and self._password:
                # 登录态失效，尝试账号密码自动登录后重试
                if self.__login_by_browser(page):
                    self.__refresh_cookies_from_browser(ctx, page)
                    page.goto(f"{self._base_url}/dsu_paulsign-sign.html?mobile=no",
                              wait_until="domcontentloaded", timeout=60000)
                    time.sleep(2)
                    html = page.content() or ""
                    user_name_match = re.search(r'title="访问我的空间">(.*?)</a>', html)
            if not user_name_match:
                try:
                    logger.error(f"浏览器模式：签到页标题：{page.title()}")
                    shot_path = self.get_data_path() / "signin_page_failed.png"
                    shot_path.write_bytes(page.screenshot())
                    logger.error(f"浏览器模式：签到页截图已保存到 {shot_path}")
                except Exception:
                    pass
                self.__notify(False, "论坛登录态已失效，请重新复制完整Cookie（cf_clearance已自动刷新）")
                return True
            user_name = user_name_match.group(1)
            logger.info(f"登录用户名为：{user_name}")

            if re.search(r'(您今天已经签到过了或者签到时间还未开始)', html):
                self.__notify(True, "您今天已经签到过了或者签到时间还未开始")
                return True

            formhash_match = re.search(r'<input[^>]*name="formhash"[^>]*value="([^"]*)"', html)
            if not formhash_match:
                self.__notify(False, "未获取到 formhash 值")
                return True
            formhash_value = formhash_match.group(1)

            month_match = re.search(r'<p>您本月已累计签到:<b>(\d+)</b>', html)
            total_continuous_check_in = int(month_match.group(1)) + 1 if month_match else 1

            todaysay = self.__get_todaysay()
            logger.info(f"最终想说的话：{todaysay}")

            # 在浏览器页面上下文内提交签到（共享浏览器 Cookie 与 TLS 指纹）
            logger.info("浏览器模式：提交签到请求...")
            sign_resp = page.evaluate(
                """async (data) => {
                    const resp = await fetch('/plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                        body: new URLSearchParams(data).toString()
                    });
                    return await resp.text();
                }""",
                {"formhash": formhash_value, "qdxq": "kx", "qdmode": "1",
                 "todaysay": todaysay, "fastreply": "0"})

            content_match = re.search(r'<div class="c">(.*?)</div>', sign_resp or "", re.DOTALL)
            if not content_match:
                self.__notify(False, "获取签到后的响应内容失败")
                return True
            content = content_match.group(1).strip()
            logger.info(content)

            # 浏览器内获取积分信息
            credit_html = page.evaluate(
                """async () => {
                    const resp = await fetch('/home.php?mod=spacecp&ac=credit&showcredit=1&inajax=1&ajaxtarget=extcreditmenu_menu');
                    return await resp.text();
                }""")
            money_match = re.search(r'<span id="hcredit_2">(\d+)</span>', credit_html or "")
            money = money_match.group(1) if money_match else "0"
            logger.info(f"当前大洋余额：{money}")

            sign_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            text = (f"签到账号：{user_name}\n"
                    f"累计签到：{total_continuous_check_in} 天\n"
                    f"当前大洋：{money}\n"
                    f"签到时间：{sign_time}\n"
                    f"{content}")
            self.__notify(True, text)
            self.__save_history(user_name, total_continuous_check_in, money, content, sign_time)
            return True
        except Exception as err:
            logger.error(f"浏览器模式执行失败：{err}")
            return False
        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass

    def __get_res(self, url: str):
        """GET 请求站点：按配置走代理，代理无响应时自动回退显式直连一次。"""
        res = RequestUtils(headers=self.__get_headers(), proxies=self.__get_proxies()).get_res(url=url)
        if res is None and self._use_proxy:
            logger.warning("代理请求无响应，自动回退直连重试...")
            res = RequestUtils(headers=self.__get_headers(), proxies=self._DIRECT_PROXIES).get_res(url=url)
        return res

    def __post_res(self, url: str, data: dict):
        """POST 请求站点：按配置走代理，代理无响应时自动回退显式直连一次。"""
        res = RequestUtils(headers=self.__get_headers(), proxies=self.__get_proxies()).post_res(url=url, data=data)
        if res is None and self._use_proxy:
            logger.warning("代理请求无响应，自动回退直连重试...")
            res = RequestUtils(headers=self.__get_headers(), proxies=self._DIRECT_PROXIES).post_res(url=url, data=data)
        return res

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
                res = RequestUtils(proxies=self._DIRECT_PROXIES).get_res(
                    "https://v1.hitokoto.cn/?encode=text")
                text = (res.text or "").strip() if res else ""
                logger.info(f"尝试想说的话-{attempt}: {text}")
                if 6 <= len(text) <= 50:
                    return text
            except Exception as err:
                logger.warning(f"获取一言失败（第{attempt}次）：{err}")
        return self._default_todaysay

    def __fetch_money(self) -> str:
        """请求积分页面并解析当前大洋余额，失败时返回 0。"""
        res = self.__get_res(
            f"{self._base_url}/home.php?mod=spacecp&ac=credit&showcredit=1&inajax=1"
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
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 3},
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'use_browser',
                                                            'label': '浏览器模式',
                                                            'color': 'success',
                                                            'prepend-icon': 'mdi-robot',
                                                            'hint': '被Cloudflare拦截时自动用浏览器完成签到并刷新Cookie',
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
                                                            'placeholder': '请填写完整Cookie，需包含 cf_clearance',
                                                            'prepend-inner-icon': 'mdi-cookie',
                                                            'hint': '从浏览器开发者工具复制完整Cookie，必须包含 cf_clearance（Cloudflare验证）'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'user_agent',
                                                            'label': '浏览器UA（User-Agent）',
                                                            'placeholder': '留空使用默认UA',
                                                            'prepend-inner-icon': 'mdi-web',
                                                            'hint': 'cf_clearance与UA绑定，请填写与浏览器完全一致的UA（开发者工具-网络-请求标头中的User-Agent）'
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
                    # 账号密码卡片（自动登录）
                    {
                        'component': 'VCard',
                        'props': {'title': '账号密码（自动登录，可选）', 'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {
                                'component': 'VCardText',
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'username',
                                                            'label': '论坛账号',
                                                            'placeholder': '登录用户名',
                                                            'prepend-inner-icon': 'mdi-account'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {'cols': 12, 'md': 6},
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'password',
                                                            'label': '论坛密码',
                                                            'type': 'password',
                                                            'placeholder': '登录密码',
                                                            'prepend-inner-icon': 'mdi-lock',
                                                            'hint': '配置后，登录态失效时浏览器模式自动登录获取Cookie',
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
            "use_proxy": False,
            "user_agent": "",
            "use_browser": False,
            "username": "",
            "password": ""
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
            res = self.__get_res(f"{self._base_url}/dsu_paulsign-sign.html?mobile=no")
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
            group_res = self.__get_res(f"{self._base_url}/home.php?mod=spacecp&ac=usergroup")
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
