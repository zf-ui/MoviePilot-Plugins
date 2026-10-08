"""国语视界（cnlang.org）自动签到插件 —— 按 MoviePilot V3 插件开发规范重写。

主类 ``CnlangSigninV3`` 与插件 ID 一致，插件目录 ``cnlangsigninv3`` 为类名的小写形式，
主类定义在本文件（``plugins.v3/cnlangsigninv3/__init__.py``）。

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
from email.utils import parsedate_to_datetime
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
# 站点首页：浏览器模式下**必须先访问这里**换取域级 cf_clearance，再访问签到页
SITE_HOME_URL = f"https://{SITE_HOST}/"
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

# 默认 User-Agent。Cloudflare 签发的 cf_clearance 与 UA 绑定，站点也会按 UA 判断
# 浏览器新旧，因此这里取一个较新的稳定版本；用户可在配置中覆盖为与自己浏览器
# 完全一致的 UA（见配置项「浏览器UA」）。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 显式直连代理：requests 传空代理字典表示绕过环境代理
DIRECT_PROXIES: Dict[str, Any] = {"http": None, "https": None}
# Cloudflare 挑战页的标题特征（小写比较）
CF_CHALLENGE_TITLES = (
    "just a moment",
    "请稍候",
    "attention required",
    "loading",
)
# 挑战页正文里的 <title> 特征。页面内 fetch 只拿得到 HTML、没有页面标题，
# 此时必须靠正文判断；这里刻意只匹配 <title> 前缀，避免把正文里偶然出现的
# "loading" 之类字样误判为挑战页。
CF_CHALLENGE_HTML_MARKERS = (
    "<title>just a moment",
    "<title>请稍候",
    "<title>attention required",
)
# 站点签到路径上的 Cloudflare 挑战是 **interactive** 类型（Turnstile 勾选框），
# 必须由真实浏览器执行 JS 并完成交互。已实测确认无法被自动化的客户端绕过：
#   · 纯 requests / urllib        -> 403 + Cf-Mitigated: challenge
#   · curl_cffi（chrome150 指纹 + 完整 Client Hints + 论坛会话）-> 同样 403
#   · 无头 Chrome / 有头全新 profile Chrome（跑满 150s）-> 始终拿不到 cf_clearance
# 唯一能直接放行的是**有效的 cf_clearance**，因此这里给的建议只围绕「如何刷新 Cookie」。
CF_ADVICE = (
    "站点对签到路径下发了 Cloudflare 交互式人机验证（Turnstile 勾选框）。"
    "这类验证必须由真实浏览器人工完成——纯 HTTP 请求（包括模拟 Chrome 指纹的 "
    "curl_cffi）和无头/虚拟显示浏览器都过不去，唯一能直接放行的是有效的 cf_clearance。\n"
    "请按下面步骤更新配置里的 Cookie：\n"
    "1）用 Chrome/Edge 登录 cnlang.org，打开签到页并完成验证；\n"
    "2）F12 → 网络 → 刷新 → 点任意一个发往 cnlang.org 的请求 → 复制请求头里的"
    "完整 Cookie（必须包含 cf_clearance）；\n"
    "3）在同一页面执行 navigator.userAgent，把结果填进「浏览器UA」——"
    "cf_clearance 与 UA 绑定，不一致会被立即拒绝。"
    "注意：3.6.7 之前的版本可能把这里改成了浏览器模式的 UA（形如 Chrome/154），"
    "若发现该值与你的真实浏览器不符，请**覆盖掉它**，否则新 Cookie 同样会被拒绝；\n"
    "4）cf_clearance 有效期有限，失效后重复上述步骤即可。"
)
# Cloudflare 自管 Cookie（cf_clearance / __cf_bm / cf_chl_* …）。
# 早期版本以为「把旧通行证注入浏览器会让 CF 不信任会话」而把它们剔除，实测不成立：
# cf_clearance 是**域级**的，只要还在有效期内，注入后首页与签到页都能直接放行。
# 因此现在的策略是**照常注入**（见 _inject_cookies），只在需要区分分类时使用本函数
# （见 _refresh_cookies_from_browser）。
CF_COOKIE_NAMES = ("cf_clearance", "__cf_bm", "__cfduid")
CF_COOKIE_PREFIXES = ("cf_chl", "__cf")
# 通行证本体：与 UA 绑定，有效期由站点侧的 Challenge Passage 决定
CF_CLEARANCE_NAME = "cf_clearance"
# 单次挑战等待预算（秒）。挑战过程不可中断：中途 reload 会让 Turnstile 的进度归零，
# 因此这里给单次尝试一个长预算，而不是「短等待 + 反复重载」。
CF_CHALLENGE_BUDGET = 60
# 首页热身预算：首页的挑战通常能自动通过，预算不必和签到页一样长
CF_WARMUP_BUDGET = 45
# 升级到有头模式重试时的等待预算。有头浏览器本身更可信，通常很快就能过，
# 不需要和无头模式一样长的预算，也避免整轮签到耗时失控。
CF_HEADED_BUDGET = 45


def _is_cloudflare_cookie(name: str) -> bool:
    """判断一个 Cookie 名是否由 Cloudflare 自己管理。

    这类 Cookie（cf_clearance / __cf_bm / cf_chl_* …）与签发时的 UA 和 IP 绑定。
    注意：**不再**据此剔除 Cookie——cf_clearance 是域级通行证，有效期内注入后
    首页与签到页都会直接放行，剔除只会让每次执行都从零重新撞挑战。
    本函数现在只用于「把 CF Cookie 与论坛登录态 Cookie 分开处理」的分类场景。

    :param name: Cookie 名
    :return: 是否属于 Cloudflare 自管 Cookie
    """
    lowered = (name or "").strip().lower()
    return lowered in CF_COOKIE_NAMES or lowered.startswith(CF_COOKIE_PREFIXES)


# ----------------------------------------------------------------------
# curl_cffi：真实 Chrome TLS/JA3 指纹的 HTTP 客户端（可选增强）
# ----------------------------------------------------------------------
# 宿主自带的 RequestUtils 走 Python 的 OpenSSL 栈，TLS 指纹与真实 Chrome 差异明显。
# curl_cffi 能复刻 Chrome 的 TLS/JA3 + HTTP/2 指纹与请求头顺序，让快速路径尽可能
# 贴近真实浏览器。**注意**：它并不能绕过上面的交互式挑战（已实测），
# 作用是让「拿着有效 cf_clearance 的快速路径」以最接近浏览器的形态发出请求。
#
# 配置项 impersonate 的取值：
#   "auto"（默认）—— 按「浏览器UA」里的 Chrome 主版本号挑选最接近的受支持目标，
#                    让 TLS 指纹版本与 UA 版本一致；UA 不含版本号时用最新版 chrome。
#   具体值（如 "chrome131"）—— 强制使用该目标。
#   空字符串 —— 关闭 curl_cffi，完全使用宿主网络组件。
CURL_CFFI_DEFAULT_TARGET = "chrome"
# curl_cffi 的 impersonate 参数名（不同版本保持一致）
CURL_CFFI_IMPERSONATE_ARG = "impersonate"
# 请求超时（秒）
REQUEST_TIMEOUT = 30
# curl_cffi 模块的惰性导入缓存。_UNSET 表示尚未探测。
_UNSET = object()
_curl_cffi_module: Any = _UNSET


def _import_curl_cffi() -> Optional[Any]:
    """导入 curl_cffi 的 requests 模块；不可用时返回 None。

    只在首次调用时探测一次并缓存结果，避免每次请求都走一遍 import 机制。
    宿主未安装 curl_cffi 是完全正常的（它是可选的增强项）。

    :return: ``curl_cffi.requests`` 模块，或 None
    """
    global _curl_cffi_module
    if _curl_cffi_module is _UNSET:
        try:
            from curl_cffi import requests as curl_requests  # noqa: PLC0415 - 可选依赖

            _curl_cffi_module = curl_requests
        except Exception:  # noqa: BLE001 - 未安装 / 版本不兼容都按不可用处理
            _curl_cffi_module = None
    return _curl_cffi_module


def _curl_cffi_targets() -> Tuple[str, ...]:
    """返回当前 curl_cffi 版本支持的 impersonate 目标名列表。

    导入失败时返回空元组，调用方据此退回 ``CURL_CFFI_DEFAULT_TARGET``。

    :return: 受支持的目标名（如 ``("chrome131", "chrome136", …)``）
    """
    try:
        from curl_cffi.requests.impersonate import BrowserType  # noqa: PLC0415

        return tuple(item.value for item in BrowserType)
    except Exception:  # noqa: BLE001 - 取不到就当作没有清单
        return ()


def _resolve_impersonate(target: Optional[str], user_agent: Optional[str]) -> Optional[str]:
    """把配置的伪装目标解析成 curl_cffi 实际可用的目标名。

    ``auto`` 会读取 UA 里的 ``Chrome/<主版本>``，在 curl_cffi 支持的目标里挑一个
    版本号不超过它的最大值，从而让 TLS 指纹版本与 UA 版本尽量一致——两者不一致本身
    就是「非真实浏览器」的特征。

    :param target: 配置值；None/空串表示关闭 curl_cffi
    :param user_agent: 配置的浏览器 UA，可为 None
    :return: 可直接传给 curl_cffi 的目标名；None 表示不使用 curl_cffi
    """
    value = (target or "").strip()
    if not value:
        return None
    if value.lower() != "auto":
        return value

    match = re.search(r"Chrome/(\d+)", user_agent or "")
    if not match:
        return CURL_CFFI_DEFAULT_TARGET
    major = int(match.group(1))

    known = _curl_cffi_targets()
    if not known:
        return CURL_CFFI_DEFAULT_TARGET
    candidates = []
    for name in known:
        hit = re.fullmatch(r"chrome(\d+)", name)
        if hit and int(hit.group(1)) <= major:
            candidates.append((int(hit.group(1)), name))
    if not candidates:
        return CURL_CFFI_DEFAULT_TARGET
    return max(candidates)[1]


def _split_cookie_header(cookie_header: Optional[str]) -> List[Tuple[str, str]]:
    """把 Cookie 头拆成 ``(name, value)`` 列表，保持出现顺序。

    :param cookie_header: 形如 ``a=1; b=2`` 的 Cookie 头
    :return: 键值对列表，跳过没有 ``=`` 的片段
    """
    pairs: List[Tuple[str, str]] = []
    for chunk in str(cookie_header or "").split(";"):
        name, separator, value = chunk.partition("=")
        name = name.strip()
        if separator and name:
            pairs.append((name, value.strip()))
    return pairs


def _cf_clearance_values(cookie_header: Optional[str]) -> List[str]:
    """取出 Cookie 头里所有 ``cf_clearance`` 的值。

    同名 Cookie 可能因作用域不同而存在多条，这里全部返回，由调用方决定如何展示。

    :param cookie_header: Cookie 头
    :return: 值列表，可能为空
    """
    return [
        value
        for name, value in _split_cookie_header(cookie_header)
        if name == CF_CLEARANCE_NAME
    ]


def _embedded_cf_timestamp(value: str) -> Optional[int]:
    """从 ``cf_clearance`` 的值里提取内嵌的 Unix 时间戳；无法确认时返回 None。

    Cloudflare 没有公开这个值的格式，因此这里只接受「被 ``-`` 分隔、长度恰为 10 位、
    且落在 ``CF_TIMESTAMP_MIN ~ CF_TIMESTAMP_MAX`` 区间」的**整段**数字。
    宁可返回 None 让上层报「无法确定」，也不要把随机 hex 里的数字当成时间戳误导用户。

    :param value: ``cf_clearance`` 的值
    :return: Unix 秒；没有可信时间戳时返回 None
    """
    for segment in str(value or "").split("-"):
        if len(segment) != 10 or not segment.isdigit():
            continue
        stamp = int(segment)
        if CF_TIMESTAMP_MIN <= stamp <= CF_TIMESTAMP_MAX:
            return stamp
    return None


def _format_timestamp(stamp: Any) -> str:
    """把 Unix 秒格式化成本地 ``YYYY-MM-DD HH:MM``。

    :param stamp: Unix 秒
    :return: 可读时间字符串；无法转换时原样返回
    """
    try:
        return datetime.fromtimestamp(int(stamp)).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, TypeError, ValueError):
        return str(stamp)


def _set_cookie_values(headers: Any) -> List[str]:
    """尽量完整地取出响应里所有 ``Set-Cookie`` 值。

    不同 HTTP 客户端的 ``headers`` 形态不一：httpx 提供 ``get_list``，requests 与
    curl_cffi 会把多条合并成一个逗号分隔的字符串。这里逐级降级取值，任何一步失败
    都只当作「没有」，不影响主流程。

    :param headers: 响应头对象
    :return: Set-Cookie 原始值列表
    """
    if headers is None:
        return []
    for getter in ("get_list", "getlist"):
        method = getattr(headers, getter, None)
        if not callable(method):
            continue
        try:
            values = method("set-cookie")
        except Exception:  # noqa: BLE001 - 取值失败按「没有」处理
            values = None
        if values:
            return [str(item) for item in values]
    try:
        raw = headers.get("Set-Cookie") or headers.get("set-cookie")
    except Exception:  # noqa: BLE001 - 非映射型 headers
        return []
    return [str(raw)] if raw else []


def _parse_http_date(value: str) -> Optional[int]:
    """把 HTTP 日期（RFC 1123）解析成 Unix 秒；解析失败返回 None。

    :param value: ``Expires`` 字段的值
    :return: Unix 秒；无法解析时返回 None
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        return int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


# 远程命令动作标识
ACTION_SIGNIN = "cnlang_signin"

# 插件结构化数据键
KEY_HISTORY = "history"
KEY_LAST_RESULT = "last_result"
# 上一轮由浏览器自己签发的 Cloudflare 通行证 Cookie。存放进插件数据而非配置，
# 是因为配置在「保存插件配置」时会被前端表单整体覆盖，未在表单里声明的内部字段
# 会被静默清空。
KEY_CF_COOKIES = "cf_cookies"
# 浏览器实际使用的 UA。**必须存插件数据，绝不能写进配置字段**：配置里的「浏览器UA」
# 是用户为「自己的浏览器 + 自己的 Cookie」设定的，被浏览器模式的 UA 覆盖后，
# 用户再粘贴一份新鲜 Cookie 也会因 UA 不匹配而立刻失效——等于把唯一可用的方案弄坏。
KEY_BROWSER_UA = "browser_user_agent"
# 观测到的 cf_clearance 到期信息。Cloudflare 的 Challenge Passage 是**站点侧**配置，
# 外部读不到（官方文档只给了「默认 30 分钟」），所以只能在站点自己下发 Set-Cookie、
# 或浏览器上下文里带 expires 时把它记下来，再回报给用户。
KEY_CF_EXPIRY = "cf_clearance_expiry"
# 上一次签到成功的时间戳（Unix 秒）。Cookie 失效时用它算出「这份 Cookie 活了多久」，
# 这是用户唯一能拿到的、关于本站通行证寿命的实测数据。
KEY_LAST_SUCCESS = "last_success_at"

# cf_clearance 的值里可能内嵌一个 Unix 时间戳。Cloudflare 没有公开格式，因此这里
# 只接受「被 '-' 分隔、且落在 2017-07 ~ 2039-09 区间」的 10 位数字段，
# 避免把随机 hex 片段里的数字误判成时间戳。
CF_TIMESTAMP_MIN = 1_500_000_000
CF_TIMESTAMP_MAX = 2_200_000_000
# Cloudflare 文档给出的 Challenge Passage 默认值，仅用于「到期时间未知」时给用户一个参照。
CF_DEFAULT_TTL_MINUTES = 30

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


class CnlangSigninV3(_PluginBase):
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
    plugin_version = "3.7.0"
    # 插件作者
    plugin_author = "xijin285"
    # 作者主页
    author_url = "https://github.com/xijin285"
    # 插件配置项ID前缀
    plugin_config_prefix = "cnlangsignin_v3_"
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
    # 自定义 User-Agent，None 表示使用 DEFAULT_USER_AGENT
    _user_agent: Optional[str] = None
    # 被 Cloudflare 拦截时是否自动切换浏览器模式完成签到
    _browser_mode: bool = True
    # 是否跳过快速路径，直接走浏览器模式（用于主动验证浏览器模式是否可用）
    _force_browser: bool = False
    # 是否使用 curl_cffi（真实 Chrome TLS 指纹）作为快速路径的 HTTP 客户端
    _use_curl_cffi: bool = True
    # curl_cffi 的 impersonate 目标；"auto" 表示按 UA 自动匹配，空串表示关闭
    _impersonate: str = "auto"
    # 上一轮由浏览器自己签发的 Cloudflare 通行证 Cookie（懒加载，见 _stored_cf_cookies）
    _cf_cookies: Optional[List[Dict[str, str]]] = None
    # 浏览器实际使用的 UA（懒加载，见 _stored_browser_user_agent）。
    # 与 _user_agent 严格区分：_user_agent 是用户配置，本字段只用于「用户没配时兜底」。
    _browser_user_agent: Optional[str] = None
    # 观测到的 cf_clearance 到期信息（懒加载，见 _stored_cf_expiry）。
    # None 表示「尚未读取」，空字典表示「读过了但没有记录」。
    _cf_expiry: Optional[Dict[str, Any]] = None

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
        self._user_agent = (config.get("user_agent") or "").strip() or None
        # 默认开启：站点常态启用 Cloudflare，关闭后 requests 被拦截时会直接失败
        self._browser_mode = bool(config.get("browser_mode", True))
        # 默认关闭：只有用户主动想验证浏览器模式时才跳过快速路径
        self._force_browser = bool(config.get("force_browser", False))
        # 默认开启：curl_cffi 未安装时会自动回退宿主网络组件，不影响可用性
        self._use_curl_cffi = bool(config.get("use_curl_cffi", True))
        # "auto" 按 UA 自动匹配 Chrome 版本；空串表示关闭 curl_cffi。
        # 注意：缺失（None）与显式留空（""）语义不同——前者取默认 auto，后者是关闭。
        raw_impersonate = config.get("impersonate")
        self._impersonate = (
            "auto" if raw_impersonate is None else str(raw_impersonate).strip()
        )
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
        """签到主流程：探测登录态 -> 提交签到 -> 汇总结果 -> 落库并通知。

        默认先走 HTTP 快速路径（可用时经 curl_cffi 以真实 Chrome 指纹发出）；一旦被
        Cloudflare 人机验证拦截，按配置切换到浏览器模式完成整轮签到。开启「强制浏览器
        模式」时跳过快速路径，直接走浏览器模式——这是主动验证浏览器模式是否可用的手段。
        """
        if not self._cookie:
            return self._record_failure("未配置Cookie")

        if self._force_browser:
            logger.info("已开启「强制浏览器模式」，跳过 HTTP 快速路径")
            return self._handle_cloudflare_block()

        headers = self._build_headers()
        proxy_hint = "（使用代理）" if self._use_proxy else ""

        # 步骤 1：读取签到页面，确认登录态并提取 formhash
        logger.info(f"步骤1：获取签到页面信息{proxy_hint}")
        page, status, cf_blocked = self._request(SIGN_PAGE_URL, headers=headers)
        if cf_blocked:
            return self._handle_cloudflare_block()
        if page is None:
            detail = (
                f"获取签到页面失败，状态码：{status}"
                if status is not None
                else "获取签到页面失败，请检查网络或代理设置"
            )
            return self._record_failure(detail)

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
        response, status, cf_blocked = self._request(
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
        if cf_blocked:
            return self._handle_cloudflare_block()
        if response is None:
            detail = (
                f"提交签到请求失败，状态码：{status}"
                if status is not None
                else "提交签到请求失败，请检查网络或代理设置"
            )
            return self._record_failure(detail)

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
    # Cloudflare 拦截与浏览器模式
    # ------------------------------------------------------------------

    def _handle_cloudflare_block(self) -> Dict[str, Any]:
        """处理 Cloudflare 人机验证拦截：按配置切浏览器模式，否则给出可执行建议。"""
        if not self._browser_mode:
            logger.error("未开启浏览器模式，无法通过 Cloudflare 人机验证")
            return self._record_failure(CF_ADVICE)
        logger.warning("检测到 Cloudflare 人机验证，切换到浏览器模式执行签到")
        if not self._signin_by_browser():
            return self._record_failure(
                "浏览器模式不可用，请确认宿主机已安装浏览器依赖（cloakbrowser / playwright）"
            )
        result = self.get_data(KEY_LAST_RESULT) or {}
        if not result:
            return self._record_failure("浏览器模式未产生签到结果")
        return result

    def _signin_by_browser(self) -> bool:
        """在真实浏览器中完成整轮签到。

        **实测结论（重要）**：站点签到路径上的挑战是 Cloudflare 的 **interactive**
        类型（Turnstile 勾选框），必须由真实浏览器人工完成。实测已确认以下客户端
        全部无法通过：纯 requests、curl_cffi（chrome150 指纹 + 完整 Client Hints）、
        无头 Chrome、以及全新 profile 的有头 Chrome（跑满 150 秒仍未取得
        ``cf_clearance``）。因此本方法的定位是**尽力而为的兜底**：宿主环境恰好具备
        真实显示、且浏览器可信度足够时才有机会通过；容器内的 Xvfb 虚拟显示基本无效。

        尝试顺序（按可信度从高到低），任一尝试拿到签到页即停止：

        1. 无头 + 拟人化：宿主默认配置，与宿主自身启动浏览器的方式完全一致；
        2. 有头 + 拟人化：无头 Chromium 是 Cloudflare 下发交互式挑战的常见诱因，
           有头模式依赖宿主提供的虚拟显示资源（``host.display``），资源缺失时该次
           尝试会直接失败并保留第 1 次的结论。

        :return: True 表示已完整处理（结果已由 ``_record_*`` 落库并通知）；
                 False 表示宿主未提供浏览器能力，调用方需要自行给出失败结论
        """
        try:
            from app.sdk.browser import launch_browser_context
        except ImportError:
            logger.error("当前宿主未提供 app.sdk.browser，无法使用浏览器模式")
            return False

        plan = self._browser_attempt_plan()
        reasons: List[str] = []
        for index, (headless, humanize, budget) in enumerate(plan, start=1):
            mode = self._describe_browser_mode(headless=headless, humanize=humanize)
            logger.info(f"浏览器模式：第 {index}/{len(plan)} 次尝试（{mode}）")
            try:
                done = self._browser_attempt(
                    launch_browser_context,
                    headless=headless,
                    humanize=humanize,
                    budget=budget,
                    reasons=reasons,
                )
            except Exception as err:  # noqa: BLE001 - 单次尝试异常不应中断后续尝试
                detail = f"{mode}启动失败（{err.__class__.__name__}: {err}）"
                logger.warning(f"浏览器模式：{detail}")
                reasons.append(detail)
                continue
            if done:
                return True
        # 浏览器模式失败时，必须把「怎么办」一并给出。否则用户只看到一串技术细节
        # （如「未取得签到页（标题：…）」），完全不知道下一步该做什么。
        logger.warning(
            "浏览器模式未能通过 Cloudflare 挑战。站点签到路径的挑战是 interactive 类型"
            "（Turnstile 勾选框），必须真实浏览器人工完成，自动化浏览器过不去。"
            "若你的宿主跑在 Docker 容器里（Xvfb 虚拟显示），浏览器模式基本无效，"
            "建议关闭「浏览器模式」以免每次白等约 2.5 分钟。"
        )
        self._record_failure(
            "浏览器模式未通过 Cloudflare 人机验证，未能完成签到："
            + "；".join(reasons)
            + "\n\n"
            + CF_ADVICE
        )
        return True

    def _browser_attempt_plan(self) -> List[Tuple[bool, bool, int]]:
        """返回浏览器启动参数的尝试顺序。

        宿主把「是否拟人化」做成系统配置（``CLOAKBROWSER_HUMANIZE``，默认开启），
        并固定传给浏览器实现。插件必须跟随该配置，否则会以脚本化的固定行为启动，
        被 Cloudflare 判为机器人。

        :return: ``(是否无头, 是否拟人化, 等待预算秒数)`` 的尝试列表
        """
        humanize = self._as_bool(getattr(settings, "CLOAKBROWSER_HUMANIZE", None), True)
        return [
            (True, humanize, CF_CHALLENGE_BUDGET),
            (False, humanize, CF_HEADED_BUDGET),
        ]

    @staticmethod
    def _describe_browser_mode(*, headless: bool, humanize: bool) -> str:
        """生成用于日志的启动模式描述。"""
        return f"{'无头模式' if headless else '有头模式'} + {'拟人化' if humanize else '非拟人化'}"

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        """把宿主配置里的布尔值（可能是 bool，也可能是 "true"/"0" 之类的字符串）归一化。"""
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)

    def _browser_launch_kwargs(self, *, headless: bool, humanize: bool) -> Dict[str, Any]:
        """构造浏览器启动参数。

        必须把宿主的拟人化配置一并传入：宿主自身启动浏览器时会固定传
        ``humanize`` / ``human_preset``（见 ``app/adapters/network/browser.py``）。
        插件若省略这两个参数，cloakbrowser 会退回到脚本化的固定行为——鼠标瞬移、
        输入零延迟，Cloudflare 的托管挑战会把这类会话判为机器人并持续下发无法自动
        完成的交互式验证，签到页因此永远停在 ``Just a moment...``。

        :param headless: 是否使用无头模式
        :param humanize: 是否启用拟人化输入
        :return: 传给 ``launch_browser_context`` 的关键字参数
        """
        kwargs: Dict[str, Any] = {"headless": headless}
        if self._user_agent:
            # 只在用户显式配置了 UA 时才覆盖。把内置的 Chrome/131 硬套到浏览器实际的
            # Chromium 版本上，会造成 UA 与 Client Hints 不一致——这是 Cloudflare 判定
            # 「非真实浏览器」的典型特征。未配置时交给浏览器使用自己的原生 UA。
            kwargs["user_agent"] = self._user_agent
        proxy_url = self._browser_proxy_url()
        if proxy_url:
            kwargs["proxy"] = {"server": proxy_url}
            logger.info(f"浏览器模式：使用代理 {proxy_url}")
        if humanize:
            kwargs["humanize"] = True
            preset = getattr(settings, "CLOAKBROWSER_HUMAN_PRESET", None)
            if preset:
                kwargs["human_preset"] = preset
        return kwargs

    def _launch_browser_context(self, launcher: Any, kwargs: Dict[str, Any]) -> Any:
        """启动浏览器上下文；浏览器实现不认拟人化参数时自动降级重试。

        :param launcher: ``app.sdk.browser.launch_browser_context``
        :param kwargs: 启动参数
        :return: 浏览器上下文
        """
        try:
            return launcher(**kwargs)
        except TypeError as err:
            optional = [key for key in ("humanize", "human_preset") if key in kwargs]
            if not optional:
                raise
            logger.warning(f"浏览器模式：当前浏览器实现不支持拟人化参数（{err}），降级后重试")
            for key in optional:
                kwargs.pop(key, None)
            return launcher(**kwargs)

    def _browser_attempt(
        self,
        launcher: Any,
        *,
        headless: bool,
        humanize: bool,
        budget: int,
        reasons: List[str],
    ) -> bool:
        """执行一次完整的浏览器签到尝试。

        :param launcher: ``app.sdk.browser.launch_browser_context``
        :param headless: 是否无头
        :param humanize: 是否拟人化
        :param budget: 等待 Cloudflare 的秒数预算
        :param reasons: 失败原因收集列表，供最终失败信息汇总
        :return: True 表示已产生签到结果（无需再试）；False 表示本次未取得签到页
        """
        kwargs = self._browser_launch_kwargs(headless=headless, humanize=humanize)
        # 启动失败（依赖缺失 / 无虚拟显示资源）向上抛出，由调用方决定是否继续尝试
        context = self._launch_browser_context(launcher, kwargs)
        try:
            return self._run_browser_flow(context, budget=budget, reasons=reasons)
        finally:
            try:
                context.close()
            except Exception as err:  # noqa: BLE001 - 关闭失败只记录
                logger.debug(f"关闭浏览器上下文失败：{err}")

    def _run_browser_flow(self, context: Any, *, budget: int, reasons: List[str]) -> bool:
        """在已启动的浏览器上下文内完成签到流程。

        **顺序至关重要**：先访问站点首页拿到 Cloudflare 通行证，再访问签到页。

        ``cf_clearance`` 是**域级**通行证：一旦在首页通过挑战，后续访问同域下受保护的
        路径（签到页）时 Cloudflare 会直接放行。反过来「一上来就访问签到页」等于在
        完全没有通行证的状态下撞上最严的那条规则——签到页会下发交互式挑战，而自动化
        浏览器恰恰过不了它。这就是 v3.6.2 ~ v3.6.4 一直卡在 ``Just a moment...`` 的
        结构性原因（v3.5.2 是「首页优先」，所以当时能跑通）。

        :param context: 浏览器上下文
        :param budget: 签到页等待 Cloudflare 的秒数预算
        :param reasons: 失败原因收集列表
        :return: True 表示已产生签到结果；False 表示未取得签到页，可换参数重试
        """
        try:
            page = context.new_page()
            page.set_default_timeout(60000)
            self._inject_cookies(context, page)
            self._log_browser_identity(page)

            # 第一步：在首页完成挑战，换取域级 cf_clearance。
            logger.info("浏览器模式：先访问站点首页，获取 Cloudflare 通行证...")
            self._goto(page, SITE_HOME_URL)
            if self._solve_cloudflare(
                page, label="站点首页", budget_seconds=CF_WARMUP_BUDGET
            ):
                # 立刻写回：这一步拿到的 cf_clearance 正是下一步访问签到页的关键，
                # 同时也是下一次执行的「热启动」素材。
                self._refresh_cookies_from_browser(context, page)
            else:
                logger.warning("浏览器模式：站点首页未加载出真实内容，仍继续尝试签到页")

            # 第二步：带着通行证访问签到页（文档导航）。
            logger.info("浏览器模式：正在访问签到页，等待 Cloudflare 验证...")
            self._goto(page, SIGN_PAGE_URL)
            html = ""
            if self._solve_cloudflare(page, label="签到页", budget_seconds=budget):
                html = self._page_content(page)
            if not html:
                # 文档导航仍被挑战：改从首页用页面内 fetch 取签到页。同源请求复用已
                # 建立的 TLS 会话与 Cookie，作为最后一道兜底。
                html = self._sign_page_via_fetch(context, page)
            if not html:
                title = self._page_title(page)
                logger.error(f"浏览器模式：无法取得签到页，标题：{title}")
                self._save_failure_screenshot(page, "signin_page_failed.png")
                reasons.append(f"未取得签到页（标题：{title or '空'}）")
                return False

            # 挑战通过后把浏览器新签发的 Cookie 写回，供下次执行复用
            self._refresh_cookies_from_browser(context, page)
            return self._finish_browser_signin(page, html)
        except Exception as err:  # noqa: BLE001 - 浏览器异常不得冒泡到宿主调度器
            logger.error(f"浏览器模式执行失败：{err}")
            reasons.append(f"执行异常（{err.__class__.__name__}: {err}）")
            return False

    def _browser_proxy_url(self) -> Optional[str]:
        """返回浏览器模式使用的代理地址；未开启代理或宿主未配置时返回 None。"""
        if not self._use_proxy:
            return None
        proxy = getattr(settings, "PROXY", None)
        if isinstance(proxy, dict):
            return proxy.get("https") or proxy.get("http") or None
        return proxy or None

    def _stored_cf_cookies(self) -> List[Dict[str, str]]:
        """读取上一轮由浏览器自己签发的 Cloudflare 通行证 Cookie。

        以「Cookie 字典列表」形式缓存而不是拼成 ``k=v; k=v`` 字符串：同一个名字可能
        同时存在多条（不同 domain / path 的 ``cf_clearance``），拼成字符串会把它们
        压成一条，注入时丢失作用域。实测日志里就出现过 ``cf_clearance`` 重复两条。

        :return: Cookie 字典列表；无缓存或读取失败时返回空列表
        """
        if self._cf_cookies is not None:
            return self._cf_cookies
        try:
            value = self.get_data(KEY_CF_COOKIES)
        except Exception as err:  # noqa: BLE001 - 读取失败按无缓存处理
            logger.debug(f"浏览器模式：读取 Cloudflare Cookie 缓存失败：{err}")
            return []
        items: List[Dict[str, str]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("name"):
                    items.append(
                        {
                            "name": str(item.get("name")),
                            "value": str(item.get("value", "")),
                            "domain": str(item.get("domain") or f".{SITE_HOST}"),
                            "path": str(item.get("path") or "/"),
                        }
                    )
        elif isinstance(value, str):  # 兼容早期版本的字符串缓存
            for pair in value.split(";"):
                if "=" not in pair:
                    continue
                name, _, val = pair.partition("=")
                if name.strip():
                    items.append(
                        {
                            "name": name.strip(),
                            "value": val.strip(),
                            "domain": f".{SITE_HOST}",
                            "path": "/",
                        }
                    )
        self._cf_cookies = items
        return items

    def _stored_browser_user_agent(self) -> Optional[str]:
        """读取上一轮浏览器实际使用的 UA（存插件数据，不占配置字段）。

        :return: 浏览器 UA；无缓存或读取失败时返回 None
        """
        if self._browser_user_agent is not None:
            return self._browser_user_agent
        try:
            value = self.get_data(KEY_BROWSER_UA)
        except Exception as err:  # noqa: BLE001 - 读取失败按无缓存处理
            logger.debug(f"浏览器模式：读取浏览器 UA 缓存失败：{err}")
            return None
        self._browser_user_agent = str(value).strip() if value else None
        return self._browser_user_agent

    def _effective_user_agent(self) -> Optional[str]:
        """返回快速路径实际要发送的 UA。

        优先级：**用户显式配置的 UA** > 上一轮浏览器记录的 UA > 内置默认 UA。

        用户配置永远优先：配置里的 UA 是用户为「自己的浏览器 + 自己的 Cookie」设定的，
        而 ``cf_clearance`` 与 UA 绑定。浏览器模式只有在用户没配 UA 时才允许兜底，
        **绝不能反过来覆盖用户的配置**——否则用户粘贴一份新鲜 Cookie 也会因 UA 不匹配
        而立刻失效（这正是 3.6.6 及更早版本实际踩到的坑）。

        :return: UA 字符串；三者都没有时返回 None（调用方回落 DEFAULT_USER_AGENT）
        """
        return self._user_agent or self._stored_browser_user_agent()

    # ------------------------------------------------------------------
    # Cloudflare 通行证有效期观测
    # ------------------------------------------------------------------

    def _stored_cf_expiry(self) -> Optional[Dict[str, Any]]:
        """读取插件数据里记录的 cf_clearance 到期信息（懒加载）。

        :return: 形如 ``{"expires_at": int, "observed_at": int, "source": str}``；
                 没有记录时返回 None
        """
        if self._cf_expiry is None:
            try:
                value = self.get_data(KEY_CF_EXPIRY)
            except Exception as err:  # noqa: BLE001 - 读不到按「没有记录」处理
                logger.debug(f"读取 cf_clearance 到期信息失败：{err}")
                value = None
            self._cf_expiry = value if isinstance(value, dict) else {}
        return self._cf_expiry or None

    def _observe_cf_expiry(self, headers: Any) -> None:
        """从响应头里捕获 Cloudflare 重新签发的 ``cf_clearance`` 及其有效期。

        Cloudflare 的 Challenge Passage 是**站点侧**配置、外部读不到，所以只要站点在
        响应里下发 ``Set-Cookie: cf_clearance=...``，就把它的 ``Max-Age`` / ``Expires``
        记进插件数据，后续在签到结果里回报给用户。拿不到就什么都不做——绝不用默认值
        冒充真实到期时间。

        :param headers: 响应头对象
        """
        for raw in _set_cookie_values(headers):
            if CF_CLEARANCE_NAME not in raw.lower():
                continue
            expires_at = self._extract_cookie_expiry(raw)
            if expires_at is None:
                continue
            state: Dict[str, Any] = {
                "expires_at": expires_at,
                "observed_at": int(time.time()),
                "source": "response",
            }
            if state == self._cf_expiry:
                return
            self._cf_expiry = state
            try:
                self.save_data(KEY_CF_EXPIRY, state)
            except Exception as err:  # noqa: BLE001 - 记录失败不影响签到结果
                logger.debug(f"保存 cf_clearance 到期信息失败：{err}")
            logger.info(
                f"Cloudflare 重新签发了 cf_clearance，有效期至 {_format_timestamp(expires_at)}"
            )
            return

    @staticmethod
    def _extract_cookie_expiry(raw_set_cookie: str) -> Optional[int]:
        """从一条 ``Set-Cookie`` 里解析出到期时间（Unix 秒）。

        优先 ``Max-Age``（相对时间，无时区歧义），再回退 ``Expires``（HTTP 日期）。

        :param raw_set_cookie: 单条 Set-Cookie 原始值
        :return: Unix 秒；解析不出来时返回 None
        """
        match = re.search(r"[Mm]ax-[Aa]ge\s*=\s*(-?\d+)", raw_set_cookie)
        if match:
            try:
                return int(time.time()) + int(match.group(1))
            except (TypeError, ValueError):
                return None
        match = re.search(r"[Ee]xpires\s*=\s*([^;]+)", raw_set_cookie)
        if match:
            return _parse_http_date(match.group(1))
        return None

    def _observe_browser_cf_expiry(self, expires: Any) -> None:
        """记录浏览器上下文里 ``cf_clearance`` 的 ``expires``。

        浏览器签发的通行证与用户手动复制的同源，其寿命由**站点级 Challenge Passage**
        决定，因此这个 TTL 对「我粘贴的 Cookie 大概能活多久」有直接参考价值。
        注意它只代表站点配置，不代表用户那份 Cookie 的确切到期时间，回报时会分开陈述。

        :param expires: 浏览器返回的 expires 字段（Unix 秒）；<=0 表示会话级 Cookie
        """
        try:
            expires_at = int(expires)
        except (TypeError, ValueError):
            return
        if expires_at <= 0:
            return
        now = int(time.time())
        state: Dict[str, Any] = {
            "expires_at": expires_at,
            "observed_at": now,
            "source": "browser",
        }
        if state == self._cf_expiry:
            return
        self._cf_expiry = state
        try:
            self.save_data(KEY_CF_EXPIRY, state)
        except Exception as err:  # noqa: BLE001 - 记录失败不影响签到结果
            logger.debug(f"保存浏览器观测到的 cf_clearance 到期时间失败：{err}")
        if expires_at > now:
            logger.info(
                "浏览器签发的 cf_clearance 有效期至 "
                f"{_format_timestamp(expires_at)}"
                f"（站点通行证有效期约 {self._humanize_seconds(expires_at - now)}）"
            )

    def _cf_clearance_status(self) -> str:
        """返回一行「cf_clearance 什么时候失效」的说明，用于签到结果。

        来源优先级：

        1. **站点在响应里亲自下发的到期时间**（``Set-Cookie``）——唯一能代表「你这份
           Cookie 什么时候死」的权威来源；
        2. 配置 Cookie 里 ``cf_clearance`` 值内嵌的时间戳——按它与当前时间的大小关系
           判断是「到期时间」还是「签发时间」（签到成功说明它此刻仍然有效，
           因此过去的戳只可能是签发时间）；
        3. 都没有时**明确说明无法确定**，并给出 Cloudflare 的默认值作参照。

        浏览器观测到的到期时间只代表**站点级 Challenge Passage**（来自首页那次自动
        通过的挑战），并不等于这份 Cookie 的到期时间，因此单独陈述、不冒充精确值。

        :return: 单行说明文本
        """
        observed = self._stored_cf_expiry() or {}
        expires_at = observed.get("expires_at")
        source = observed.get("source")

        if expires_at and source == "response":
            remaining = int(expires_at) - int(time.time())
            if remaining > 0:
                return (
                    f"Cloudflare 通行证：有效期至 {_format_timestamp(expires_at)}"
                    f"（剩余约 {self._humanize_seconds(remaining)}）"
                )
            return (
                f"Cloudflare 通行证：已于 {_format_timestamp(expires_at)} 过期"
                f"（{self._humanize_seconds(-remaining)}前），需重新复制 Cookie"
            )

        ttl_hint = ""
        if expires_at and source == "browser":
            ttl = int(expires_at) - int(observed.get("observed_at") or 0)
            if ttl > 0:
                ttl_hint = (
                    f"；站点通行证有效期实测约 {self._humanize_seconds(ttl)}"
                    f"（浏览器观测于 {_format_timestamp(observed.get('observed_at'))}）"
                )

        values = _cf_clearance_values(self._cookie)
        if not values:
            return "Cloudflare 通行证：未在配置的 Cookie 中找到 cf_clearance" + ttl_hint

        now = int(time.time())
        parts: List[str] = []
        for value in values:
            stamp = _embedded_cf_timestamp(value)
            if stamp is None:
                continue
            if stamp > now:
                parts.append(
                    f"{_format_timestamp(stamp)} 到期"
                    f"（剩余约 {self._humanize_seconds(stamp - now)}）"
                )
            else:
                parts.append(
                    f"{_format_timestamp(stamp)} 签发"
                    f"（{self._humanize_seconds(now - stamp)}前）"
                )
        if parts:
            return "Cloudflare 通行证内嵌时间戳：" + "；".join(parts) + ttl_hint
        return (
            "Cloudflare 通行证：到期时间无法确定（站点未下发，Cookie 值里也没有"
            f"可解析的时间戳；Cloudflare 默认 {CF_DEFAULT_TTL_MINUTES} 分钟）"
            + ttl_hint
        )

    def _cf_survival_note(self) -> str:
        """Cookie 失效时报告「上次成功签到」与「这份 Cookie 活了多久」。

        这是用户唯一能拿到的、关于本站通行证真实寿命的实测数据。

        :return: 单行说明；没有成功记录时返回空串
        """
        try:
            last = self.get_data(KEY_LAST_SUCCESS)
        except Exception as err:  # noqa: BLE001 - 读不到就不显示这一行
            logger.debug(f"读取上次成功签到时间失败：{err}")
            return ""
        if not last:
            return ""
        try:
            elapsed = max(int(time.time()) - int(last), 0)
        except (TypeError, ValueError):
            return ""
        return (
            f"上次成功签到：{_format_timestamp(last)}"
            f"（{self._humanize_seconds(elapsed)}前）"
        )

    @staticmethod
    def _humanize_seconds(seconds: Any) -> str:
        """把秒数格式化成「X 天 Y 小时」「Y 小时 Z 分钟」这类可读文本。

        :param seconds: 秒数
        :return: 可读文本
        """
        try:
            total = max(int(seconds), 0)
        except (TypeError, ValueError):
            return str(seconds)
        days, rest = divmod(total, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
        if hours:
            return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
        if minutes:
            return f"{minutes} 分钟"
        return f"{total} 秒"

    def _inject_cookies(self, context: Any, page: Any) -> None:
        """把配置 Cookie 与缓存的 Cloudflare 通行证写入浏览器会话。

        两个要点：

        1. 优先写入 Cookie 罐而不是请求头：请求头覆盖会把浏览器新拿到的
           cf_clearance 顶回旧值。宿主不支持 ``add_cookies`` 时回退请求头方式。
        2. **配置里的 Cloudflare Cookie 一律照常注入，不再剔除。** 早期版本出于
           「旧通行证与签发记录不符会让 CF 不信任会话」的猜测把它们丢掉了，但
           ``cf_clearance`` 是**域级**的：只要它还在有效期内，注入后首页与签到页
           都能直接放行，是成本最低的热启动路径；即便已经失效，Cloudflare 也只是
           重新下发一次挑战，并不会因此「永久不信任」该会话。剔除反而让每一次执行
           都必须从零开始过一次挑战——这正是 v3.6.3 / v3.6.4 仍然失败的原因之一。
        3. 插件上一轮由本浏览器自己签发的通行证（``KEY_CF_COOKIES``）带完整
           domain / path，最后注入以确保覆盖配置里的旧值。

        :param context: 浏览器上下文
        :param page: 浏览器页面
        """
        jar: List[Dict[str, str]] = []
        kept_pairs: List[str] = []
        for pair in (self._cookie or "").split(";"):
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name:
                continue
            jar.append({"name": name, "value": value, "domain": f".{SITE_HOST}", "path": "/"})
            kept_pairs.append(f"{name}={value}")

        cached = self._stored_cf_cookies()
        if cached:
            jar.extend(cached)
            kept_pairs.extend(f"{item['name']}={item['value']}" for item in cached)
            logger.info(
                f"浏览器模式：复用上一轮缓存的 {len(cached)} 个 Cloudflare Cookie"
                f"（{', '.join(item['name'] for item in cached)}）"
            )

        if not jar:
            return
        add_cookies = getattr(context, "add_cookies", None)
        if add_cookies:
            try:
                add_cookies(jar)
                logger.info(f"浏览器模式：{len(jar)} 个 Cookie 字段已写入浏览器会话")
                return
            except Exception as err:  # noqa: BLE001 - 回退到请求头方式
                logger.warning(f"浏览器模式：Cookie 写入浏览器失败（{err}），改用请求头方式")
        try:
            page.set_extra_http_headers({"cookie": "; ".join(kept_pairs)})
        except Exception as err:  # noqa: BLE001 - 写入失败由外层兜底
            logger.warning(f"浏览器模式：Cookie 写入请求头失败（{err}）")

    def _refresh_cookies_from_browser(self, context: Any, page: Any) -> None:
        """把浏览器新签发的 Cookie 收进插件数据缓存，**不改动用户的配置字段**。

        这是 3.6.7 的重要修正。早期版本会把浏览器会话 Cookie 合并回配置的 Cookie
        字段、并把浏览器 UA 写进配置的 UA 字段，带来两个真实危害：

        1. **覆盖用户配置的 UA**。用户的「浏览器UA」是为「他自己的浏览器 + 他自己的
           Cookie」设定的，而 cf_clearance 与 UA 绑定。被浏览器模式的 UA（如 Chrome/154）
           覆盖后，用户再粘贴一份新鲜 Cookie 也会因 UA 不匹配立即失效——**等于把唯一
           可用的方案弄坏**。
        2. **Cookie 字段被越滚越大**。每轮都合并浏览器会话 Cookie，实测一次执行里
           从 16 条涨到 26 条，用户的登录 Cookie 被淹没。

        现在的做法：

        - Cloudflare 通行证（cf_clearance / __cf_bm / cf_chl_* …）存进插件数据
          （``KEY_CF_COOKIES``，带 domain / path），下次启动浏览器时原样注入回去；
        - 浏览器 UA 存进插件数据（``KEY_BROWSER_UA``），仅在用户**没有**配置 UA 时
          作为快速路径的兜底（见 ``_effective_user_agent``）；
        - 用户配置的 Cookie / UA 字段**一个字节都不动**。

        :param context: 浏览器上下文
        :param page: 浏览器页面
        """
        try:
            cookies = context.cookies() or []
        except Exception as err:  # noqa: BLE001 - 取不到 Cookie 时保留原值
            logger.debug(f"浏览器模式：读取浏览器 Cookie 失败：{err}")
            cookies = []

        cf_items: List[Dict[str, str]] = []
        seen: set = set()
        for item in cookies:
            name = item.get("name")
            if not name or not _is_cloudflare_cookie(name):
                continue
            if name == CF_CLEARANCE_NAME:
                # 浏览器签发的通行证带 expires，顺手记下站点级 Challenge Passage 的长度
                self._observe_browser_cf_expiry(item.get("expires"))
            # 连 domain / path 一起存：同名 Cookie 可能有多条（不同作用域），
            # 只留 name=value 会把它们压成一条，注入时丢失作用域。
            entry = {
                "name": str(name),
                "value": str(item.get("value", "")),
                "domain": str(item.get("domain") or f".{SITE_HOST}"),
                "path": str(item.get("path") or "/"),
            }
            # 按 (name, domain, path) 去重：注入的缓存 + 浏览器新签发的会同时出现，
            # 不去重的话缓存会一轮比一轮长（实测 2 -> 5 -> 4 -> 7 条地涨）。
            fingerprint = (entry["name"], entry["domain"], entry["path"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            cf_items.append(entry)

        if cf_items:
            self._cf_cookies = cf_items
            try:
                self.save_data(KEY_CF_COOKIES, cf_items)
                logger.info(
                    f"浏览器模式：已缓存 {len(cf_items)} 条 Cloudflare 通行证 Cookie"
                    f"（{', '.join(item['name'] for item in cf_items)}），"
                    "下次执行可直接复用"
                )
            except Exception as err:  # noqa: BLE001 - 缓存失败不影响本次签到结果
                logger.debug(f"浏览器模式：缓存 Cloudflare Cookie 失败：{err}")

        try:
            browser_ua = page.evaluate("navigator.userAgent")
        except Exception as err:  # noqa: BLE001 - 取不到 UA 时保留配置值
            logger.debug(f"浏览器模式：读取浏览器 UA 失败：{err}")
            browser_ua = None
        if browser_ua:
            self._browser_user_agent = str(browser_ua)
            try:
                self.save_data(KEY_BROWSER_UA, self._browser_user_agent)
            except Exception as err:  # noqa: BLE001 - 缓存失败不影响本次签到结果
                logger.debug(f"浏览器模式：缓存浏览器 UA 失败：{err}")
            if self._user_agent:
                logger.info(
                    f"浏览器模式：浏览器实际 UA 为 {browser_ua}，"
                    "但用户已配置 UA，配置值优先，不作覆盖"
                )
            else:
                logger.info(
                    f"浏览器模式：已记录浏览器实际 UA（{browser_ua}），"
                    "仅在未配置「浏览器UA」时用于快速路径"
                )

    def _solve_cloudflare(
        self,
        page: Any,
        *,
        label: str = "",
        budget_seconds: int = CF_CHALLENGE_BUDGET,
        click_checkbox: bool = True,
        max_rounds: Optional[int] = None,
    ) -> bool:
        """在同一个页面上持续等待 Cloudflare 挑战通过。

        通过标准是「页面已有真实内容且不再是挑战页」：cf_clearance 未过期时
        Cloudflare 不会重新签发，因此不能以「出现新 Cookie」为通过标准。

        与旧实现的关键区别：**等待期间不重载页面**。Turnstile 的挑战进度保存在
        当前文档里，中途 reload 会让进度归零；旧实现「每轮只等 20 秒 + 重载重试」
        等于每次都从零开始，签到页因此永远等不到通过。这里给单次尝试一个长预算。

        :param page: 浏览器页面
        :param label: 日志用的场景名（如「签到页」）
        :param budget_seconds: 单次等待预算（秒）
        :param click_checkbox: 是否周期性尝试点击 Turnstile 复选框
        :param max_rounds: 轮询次数上限，默认按预算推算（每轮 2 秒）
        :return: 是否已通过验证
        """
        if max_rounds is None:
            max_rounds = max(int(budget_seconds / 2), 1)
        started = time.time()
        deadline = started + budget_seconds
        blank_rounds = 0
        clicks = 0
        # 给 Turnstile 留出自动通过的时间；点击过多、过快反而会被判为机器人行为，
        # 因此整轮最多补 3 次点击，且首次点击前先静待约 14 秒。
        max_clicks = 3

        for index in range(max_rounds):
            if time.time() >= deadline:
                break
            html, title = "", ""
            try:
                html = page.content() or ""
                title = page.title() or ""
            except Exception:  # noqa: BLE001 - 页面跳转中读取失败属正常
                pass
            no_content = len(html) < 500 and not title
            if not no_content and not self._is_cf_challenge_page(html, title):
                logger.info(
                    f"浏览器模式：{label}验证已通过（耗时约 {int(time.time() - started)} 秒）"
                )
                return True
            if no_content:
                blank_rounds += 1
                if blank_rounds == 5:
                    # 连接被挂起时页面会一直停在空白页，重载一次争取拿到响应
                    logger.warning(f"浏览器模式：{label}页面长时间空白，重载页面重试")
                    self._reload(page)
            else:
                blank_rounds = 0
                if click_checkbox and clicks < max_clicks and index % 8 == 7:
                    if self._try_click_cf_checkbox(page):
                        clicks += 1
            if index % 5 == 0:
                logger.info(f"浏览器模式：等待验证中... {label}当前页面标题：{title}")
            time.sleep(2)
        return False

    def _sign_page_via_fetch(self, context: Any, page: Any) -> str:
        """文档导航持续被挑战时的兜底：改从首页用页面内 fetch 取签到页。

        同源 XHR 复用浏览器已建立的 TLS 会话与全部 Cookie，且不触发只针对文档
        导航下发的托管挑战，因此常常能拿到真实页面。取到后，后续的签到提交与
        积分查询本来就走页面内 fetch，整条链路保持一致。

        :param context: 浏览器上下文
        :param page: 浏览器页面
        :return: 签到页 HTML；仍被挑战或取不到时返回空串
        """
        logger.info("浏览器模式：签到页文档导航被挑战，改从站点首页用页面内 fetch 获取")
        self._goto(page, SITE_HOME_URL)
        if not self._solve_cloudflare(page, label="站点首页", budget_seconds=CF_WARMUP_BUDGET):
            logger.warning("浏览器模式：首页未能加载出真实内容")
            return ""
        # 首页若签发了新 Cookie（含 cf_clearance）立即写回，再发起同源 fetch
        self._refresh_cookies_from_browser(context, page)
        html = self._browser_fetch(page, SIGN_PAGE_URL) or ""
        if not html:
            return ""
        if self._is_cf_challenge_page(html, ""):
            logger.warning("浏览器模式：页面内 fetch 仍返回 Cloudflare 挑战页")
            return ""
        logger.info(f"浏览器模式：页面内 fetch 已取得签到页（{len(html)} 字节）")
        return html

    def _finish_browser_signin(self, page: Any, html: str) -> bool:
        """在浏览器页面上下文内完成解析、提交签到与积分查询。

        :param page: 浏览器页面
        :param html: 签到页 HTML（来自文档导航或页面内 fetch）
        :return: 恒为 True——结果已由 ``_record_*`` 落库并通知
        """
        user_name = self._search(r'title="访问我的空间">(.*?)</a>', html)
        if not user_name:
            self._save_failure_screenshot(page, "signin_page_failed.png")
            self._record_failure("未获取到用户名，Cookie 可能已失效")
            return True
        logger.info(f"登录用户名：{user_name}")

        if re.search(r"您今天已经签到过了或者签到时间还未开始", html):
            logger.info("今日已完成签到，跳过提交")
            self._record_already_signed(user_name)
            return True

        formhash = self._search(r'<input[^>]*name="formhash"[^>]*value="([^"]*)"', html)
        if not formhash:
            self._record_failure("未获取到 formhash，站点页面结构可能已变化")
            return True

        month_signs = self._search(r"<p>您本月已累计签到:<b>(.*?)</b>", html)
        total_signs = int(month_signs) + 1 if month_signs and month_signs.isdigit() else 1

        logger.info("浏览器模式：在页面上下文内提交签到")
        submitted = self._browser_fetch(
            page,
            SIGN_SUBMIT_URL,
            data={
                "formhash": formhash,
                "qdxq": SIGN_MOOD,
                "qdmode": "1",
                "todaysay": self._build_say(),
                "fastreply": "0",
            },
        )
        if submitted and self._is_cf_challenge_page(submitted, ""):
            self._record_failure("签到提交被 Cloudflare 人机验证拦截")
            return True
        content = self._search(r'<div class="c">(.*?)</div>', submitted or "", flags=re.DOTALL)
        if not content:
            self._record_failure("获取签到后的响应内容失败")
            return True
        content = content.strip()
        logger.info(f"签到响应：{content}")

        credit_html = self._browser_fetch(page, CREDIT_URL) or ""
        money = self._search(r'<span id="hcredit_2">(\d+)</span>', credit_html) or "未知"
        logger.info(f"当前大洋余额：{money}")

        self._record_success(
            username=user_name,
            total_signs=total_signs,
            money=money,
            content=content,
        )
        return True

    @staticmethod
    def _page_title(page: Any) -> str:
        """安全读取页面标题，失败返回空串。"""
        try:
            return (page.title() or "").strip()
        except Exception:  # noqa: BLE001 - 页面跳转中读取失败属正常
            return ""

    @staticmethod
    def _log_browser_identity(page: Any) -> None:
        """记录浏览器实际 UA，便于诊断 UA 与 Cloudflare 通行证是否自洽。"""
        try:
            ua = page.evaluate("navigator.userAgent")
        except Exception as err:  # noqa: BLE001 - 取不到不影响主流程
            logger.debug(f"浏览器模式：读取浏览器 UA 失败：{err}")
            return
        if ua:
            logger.info(f"浏览器模式：浏览器实际 UA：{ua}")

    @staticmethod
    def _is_cf_challenge_page(html: str, title: str) -> bool:
        """判断页面（或响应正文）是否为 Cloudflare 挑战页。

        :param html: 页面或响应正文
        :param title: 页面标题；页面内 fetch 只有正文时传空串
        :return: 是否为挑战页
        """
        lowered_title = (title or "").strip().lower()
        if any(marker in lowered_title for marker in CF_CHALLENGE_TITLES):
            return True
        lowered_html = (html or "").lower()
        # 页面内 fetch 拿不到页面标题，只能看正文里的 <title>
        if any(marker in lowered_html for marker in CF_CHALLENGE_HTML_MARKERS):
            return True
        return "challenges.cloudflare.com" in lowered_html and "cf-chl" in lowered_html

    @staticmethod
    def _try_click_cf_checkbox(page: Any) -> bool:
        """尽力点击 Cloudflare Turnstile 复选框。

        交互式挑战不会自动通过，点击只是提高通过率；失败静默处理，不影响主流程。

        :param page: 浏览器页面
        :return: 是否真的发出了点击
        """
        try:
            frames = getattr(page, "frames", None) or []
            mouse = getattr(page, "mouse", None)
            if not mouse:
                return False
            for frame in frames:
                try:
                    if "challenges.cloudflare.com" not in (frame.url or ""):
                        continue
                    element = frame.frame_element()
                    box = element.bounding_box() if element else None
                    if box:
                        mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                        logger.info("浏览器模式：检测到交互式验证，已尝试点击人机验证框")
                        return True
                except Exception:  # noqa: BLE001 - 单个 frame 失败继续尝试下一个
                    continue
        except Exception as err:  # noqa: BLE001 - 点击失败不影响主流程
            logger.debug(f"浏览器模式：尝试点击人机验证框失败：{err}")
        return False

    @staticmethod
    def _browser_fetch(
        page: Any, url: str, data: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """在浏览器页面上下文内发起请求，复用浏览器的 Cookie 与 TLS 指纹。"""
        script = """async ([target, body]) => {
            const options = { method: body ? 'POST' : 'GET', credentials: 'include' };
            if (body) {
                options.headers = {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'};
                options.body = new URLSearchParams(body).toString();
            }
            const resp = await fetch(target, options);
            return await resp.text();
        }"""
        try:
            return page.evaluate(script, [url, data or None])
        except Exception as err:  # noqa: BLE001 - 浏览器内请求失败按无响应处理
            logger.error(f"浏览器模式：页面内请求 {url} 失败：{err}")
            return None

    @staticmethod
    def _goto(page: Any, url: str, timeout: int = 20000) -> bool:
        """导航到目标地址；加载事件超时不视为致命（页面可能已部分加载）。

        超时给得比较短：Cloudflare 挑战页的脚本会长时间占住 DOMContentLoaded，等它
        白等 45 秒没有意义。真正的等待交给 ``_solve_cloudflare`` 轮询页面内容，
        它同时能容忍挑战通过后的自动跳转。
        """
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception as err:  # noqa: BLE001 - 交给外层轮询判断实际内容
            logger.warning(f"浏览器模式：{url} 加载事件超时（{err.__class__.__name__}），检查已加载内容")
            return False

    @staticmethod
    def _reload(page: Any, timeout: int = 45000) -> None:
        """重载当前页面；宿主浏览器未提供 reload 时忽略。"""
        reload_page = getattr(page, "reload", None)
        if reload_page is None:
            return
        try:
            reload_page(wait_until="domcontentloaded", timeout=timeout)
        except Exception as err:  # noqa: BLE001 - 重载失败由外层轮询兜底
            logger.debug(f"浏览器模式：页面重载失败（{err}）")

    @staticmethod
    def _page_content(page: Any) -> str:
        """读取页面 HTML；页面跳转过程中 content() 可能失败，重试若干次。"""
        for _ in range(5):
            try:
                html = page.content()
                if html:
                    return html
            except Exception:  # noqa: BLE001 - 跳转中读取失败属正常
                pass
            time.sleep(1)
        return ""

    def _save_failure_screenshot(self, page: Any, filename: str) -> None:
        """把失败页面截图保存到插件数据目录，便于排查挑战卡在哪一步。"""
        try:
            shot = page.screenshot()
            if not shot:
                return
            path = self.get_data_path() / filename
            path.write_bytes(shot)
            logger.error(f"浏览器模式：失败页面截图已保存到 {path}")
        except Exception as err:  # noqa: BLE001 - 截图仅用于诊断
            logger.debug(f"浏览器模式：保存失败页面截图失败：{err}")

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
            f"{content}\n"
            f"{self._cf_clearance_status()}"
        )
        self._notify_result(success=True, detail=detail)
        try:
            self.save_data(KEY_LAST_SUCCESS, int(time.time()))
        except Exception as err:  # noqa: BLE001 - 记录失败不影响签到结果
            logger.debug(f"记录上次成功签到时间失败：{err}")

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
        detail = (
            f"签到账号：{username}\n"
            f"今日已完成签到，无需重复提交\n"
            f"检查时间：{sign_time}\n"
            f"{self._cf_clearance_status()}"
        )
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
        # Cloudflare 拦截通常意味着通行证已失效：顺手报出「上次成功签到」与存活时长。
        # 这是用户判断本站通行证到底能活多久的唯一实测依据。
        if "cloudflare" in reason.lower():
            note = self._cf_survival_note()
            if note and note not in reason:
                reason = f"{reason}\n\n{note}"
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
            "user_agent": self._user_agent or "",
            "browser_mode": self._browser_mode,
            "force_browser": self._force_browser,
            "use_curl_cffi": self._use_curl_cffi,
            "impersonate": self._impersonate,
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
            # Cloudflare 签发的 cf_clearance 与 UA 绑定，配置了自定义 UA 时必须原样发送
            "User-Agent": self._effective_user_agent() or DEFAULT_USER_AGENT,
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

    def _request(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], Optional[int], bool]:
        """发起一次请求并返回 ``(正文, 状态码, 是否被 Cloudflare 拦截)``。

        正文仅在状态码为 200 时返回；请求异常时状态码为 None。
        被 Cloudflare 拦截时单独标记，调用方据此决定是否切换浏览器模式。

        :param url: 目标地址
        :param headers: 请求头，None 时使用 RequestUtils 默认头
        :param data: 非空时使用 POST，否则使用 GET
        :return: 三元组，见上文
        """
        proxies = self._get_proxies()
        response = self._send(url, headers=headers, data=data, proxies=proxies)

        if response is None and proxies:
            # 代理不可用时回退直连重试一轮，避免代理失效导致整轮签到报废
            logger.warning("代理请求无响应，自动回退直连重试...")
            response = self._send(url, headers=headers, data=data, proxies=DIRECT_PROXIES)

        if response is None:
            logger.error(f"请求 {url} 失败，无响应")
            return None, None, False
        # 站点若在响应里重新签发 cf_clearance，顺手记下它的有效期
        self._observe_cf_expiry(getattr(response, "headers", None))
        if self._is_cf_challenge(response):
            logger.error(f"请求 {url} 被 Cloudflare 人机验证拦截，状态码：{response.status_code}")
            return None, response.status_code, True
        if response.status_code != 200:
            logger.error(f"请求 {url} 失败，状态码：{response.status_code}")
            return None, response.status_code, False
        return response.text, response.status_code, False

    def _send(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]],
        data: Optional[Dict[str, Any]],
        proxies: Optional[Dict[str, str]],
    ) -> Any:
        """发送一次请求：优先 curl_cffi，失败时回退宿主 RequestUtils。

        curl_cffi 复刻 Chrome 的 TLS/JA3 + HTTP/2 指纹与请求头顺序，让「带着有效
        cf_clearance 的快速路径」以最接近真实浏览器的形态发出请求。它**不能**绕过
        站点的交互式挑战（已实测），作用是降低被重新挑战的概率；未安装 curl_cffi
        或请求出错时静默回退，完全不影响可用性。

        :param url: 目标地址
        :param headers: 请求头
        :param data: 非空时使用 POST
        :param proxies: 代理映射，None 或全空表示直连
        :return: 响应对象（具备 status_code / text / headers）；失败返回 None
        """
        curl_requests = _import_curl_cffi() if self._use_curl_cffi else None
        if curl_requests is not None:
            target = _resolve_impersonate(self._impersonate, self._effective_user_agent())
            if target:
                try:
                    return self._send_via_curl_cffi(
                        curl_requests,
                        url,
                        target=target,
                        headers=headers,
                        data=data,
                        proxies=proxies,
                    )
                except Exception as err:  # noqa: BLE001 - 增强路径失败必须回退而非中断
                    logger.warning(f"curl_cffi 请求失败，回退宿主网络组件：{err}")
        return self._send_via_request_utils(url, headers=headers, data=data, proxies=proxies)

    def _send_via_curl_cffi(
        self,
        curl_requests: Any,
        url: str,
        *,
        target: str,
        headers: Optional[Dict[str, str]],
        data: Optional[Dict[str, Any]],
        proxies: Optional[Dict[str, str]],
    ) -> Any:
        """用 curl_cffi 发起请求（真实 Chrome TLS 指纹）。

        :param curl_requests: ``curl_cffi.requests`` 模块
        :param url: 目标地址
        :param target: impersonate 目标名
        :param headers: 请求头
        :param data: 非空时使用 POST
        :param proxies: 代理映射
        :return: curl_cffi 响应对象
        """
        kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": REQUEST_TIMEOUT,
            "allow_redirects": True,
            CURL_CFFI_IMPERSONATE_ARG: target,
        }
        # curl_cffi 不接受值为 None 的代理项（DIRECT_PROXIES 就是这种形态）
        proxy_map = {key: value for key, value in (proxies or {}).items() if value}
        if proxy_map:
            kwargs["proxies"] = proxy_map
        logger.info(f"使用 curl_cffi 请求 {url}（impersonate={target}）")
        if data is not None:
            return curl_requests.post(url, data=data, **kwargs)
        return curl_requests.get(url, **kwargs)

    def _send_via_request_utils(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]],
        data: Optional[Dict[str, Any]],
        proxies: Optional[Dict[str, str]],
    ) -> Any:
        """用宿主 RequestUtils 发起请求；异常时返回 None。

        :param url: 目标地址
        :param headers: 请求头
        :param data: 非空时使用 POST
        :param proxies: 代理映射
        :return: requests 响应对象；异常时返回 None
        """
        try:
            client = RequestUtils(headers=headers, proxies=proxies)
            if data is not None:
                return client.post_res(url, data=data)
            return client.get_res(url)
        except Exception as err:  # noqa: BLE001 - 网络异常统一按无响应处理
            logger.error(f"请求 {url} 异常：{err}")
            return None

    @staticmethod
    def _is_cf_challenge(response: Any) -> bool:
        """判断响应是否为 Cloudflare 人机验证页。

        依据 ``Cf-Mitigated: challenge`` 响应头（Cloudflare 官方标记），并在
        Server 为 cloudflare 时回退检查挑战页正文特征。
        """
        headers = getattr(response, "headers", None) or {}
        try:
            mitigated = str(headers.get("Cf-Mitigated", "") or "").strip().lower()
        except Exception:  # noqa: BLE001 - 非映射型 headers 直接跳过
            mitigated = ""
        if mitigated == "challenge":
            return True
        try:
            server = str(headers.get("Server", "") or "").strip().lower()
        except Exception:  # noqa: BLE001 - 非映射型 headers 直接跳过
            server = ""
        if server != "cloudflare":
            return False
        body = (getattr(response, "text", "") or "")[:4000].lower()
        return "just a moment" in body or "challenges.cloudflare.com" in body

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
        :return: 响应正文；请求异常、被 Cloudflare 拦截或状态码非 200 时返回 None
        """
        text, _status, _cf_blocked = self._request(url, headers=headers, data=data)
        return text

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
                                                            'hint': '从浏览器开发者工具复制完整Cookie；站点启用 Cloudflare 时必须包含 cf_clearance'
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
                    # 反爬设置卡片
                    {
                        'component': 'VCard',
                        'props': {
                            'title': '反爬设置',
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
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'browser_mode',
                                                            'label': '浏览器模式',
                                                            'color': 'warning',
                                                            'prepend-icon': 'mdi-web'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'force_browser',
                                                            'label': '强制浏览器模式',
                                                            'color': 'deep-orange',
                                                            'prepend-icon': 'mdi-flask-outline'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'use_curl_cffi',
                                                            'label': 'curl_cffi 指纹',
                                                            'color': 'info',
                                                            'prepend-icon': 'mdi-fingerprint'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 6
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'user_agent',
                                                            'label': '浏览器UA（User-Agent）',
                                                            'placeholder': '留空使用内置默认 UA',
                                                            'prepend-inner-icon': 'mdi-account-search',
                                                            'hint': 'cf_clearance 与 UA 绑定：请填写与浏览器完全一致的 UA，否则会被立即拒绝'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 6
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'impersonate',
                                                            'label': '指纹伪装目标',
                                                            'placeholder': 'auto',
                                                            'prepend-inner-icon': 'mdi-incognito',
                                                            'hint': 'auto=按 UA 里的 Chrome 版本自动匹配；也可填 chrome131；留空则关闭 curl_cffi'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
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
                                                        'component': 'VAlert',
                                                        'props': {
                                                            'type': 'info',
                                                            'variant': 'tonal',
                                                            'text': '站点对签到路径下发了 Cloudflare 交互式人机验证（Turnstile 勾选框），必须由真实浏览器人工完成。实测确认：纯 requests、curl_cffi 指纹伪装、无头/虚拟显示浏览器均无法通过；唯一能直接放行的是有效的 cf_clearance。因此请以「更新 Cookie」为主要手段，浏览器模式仅作兜底。'
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
            "use_proxy": False,
            "browser_mode": True,
            "force_browser": False,
            "use_curl_cffi": True,
            "impersonate": "auto",
            "user_agent": ""
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
