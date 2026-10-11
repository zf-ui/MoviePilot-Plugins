"""国语视界签到 V3 插件单元测试。

宿主接口（``app.*``）由本文件中的轻量桩模块提供，测试覆盖插件的纯逻辑、配置
生命周期与安全边界，不访问真实站点、不使用真实 Cookie。

按规范要求，插件以 ``app.plugins.<plugin_id>`` 路径导入，不把插件目录当作顶层包。
"""

import importlib.util
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest

PLUGIN_ID = "cnlangsigninv3"
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_FILE = REPO_ROOT / "plugins.v3" / PLUGIN_ID / "__init__.py"
INDEX_FILE = REPO_ROOT / "package.v3.json"


# ---------------------------------------------------------------------------
# 宿主桩实现
# ---------------------------------------------------------------------------


class _StubLogger:
    """记录日志调用，便于断言关键分支被走到。"""

    def __init__(self):
        self.records = []

    def _log(self, level, message):
        self.records.append((level, str(message)))

    def info(self, message):
        self._log("info", message)

    def warning(self, message):
        self._log("warning", message)

    def error(self, message):
        self._log("error", message)

    def debug(self, message):
        self._log("debug", message)


class _StubPluginBase:
    """模拟 ``app.plugins._PluginBase`` 的配置与数据接口。"""

    plugin_name = ""
    plugin_desc = ""
    plugin_icon = ""
    plugin_version = ""
    plugin_author = ""
    author_url = ""
    plugin_config_prefix = ""
    plugin_order = 9999
    auth_level = 1

    def __init__(self):
        self.saved_config = {}
        self.store = {}
        self.messages = []

    def update_config(self, config):
        self.saved_config = dict(config)
        return True

    def get_config(self):
        return dict(self.saved_config)

    def save_data(self, key, value, plugin_id=None):
        self.store[key] = value

    def get_data(self, key=None, plugin_id=None):
        return self.store.get(key)

    def del_data(self, key, plugin_id=None):
        return self.store.pop(key, None)

    def post_message(self, **kwargs):
        self.messages.append(kwargs)


class _StubEventManager:
    """模拟事件管理器：注册装饰器原样返回函数。"""

    def register(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


class _StubScheduler:
    """模拟 ``app.sdk.scheduler`` 的插件一次性任务接口。"""

    available = True
    added = []
    removed = []

    @classmethod
    def reset(cls):
        cls.available = True
        cls.added = []
        cls.removed = []

    @classmethod
    def add_plugin_once_job(
        cls, plugin_id, job_id, func, name, delay_seconds=0, func_kwargs=None
    ):
        cls.added.append(
            {
                "plugin_id": plugin_id,
                "job_id": job_id,
                "func": func,
                "name": name,
                "delay_seconds": delay_seconds,
            }
        )
        return cls.available

    @classmethod
    def remove_plugin_once_job(cls, plugin_id, job_id):
        cls.removed.append((plugin_id, job_id))


class _StubSettings:
    """模拟宿主配置对象。"""

    TZ = "Asia/Shanghai"
    PROXY = None


class _StubResponse:
    """模拟 HTTP 响应对象。"""

    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = {} if headers is None else dict(headers)


class _StubRequestUtils:
    """模拟 ``app.sdk.network.RequestUtils``，按 URL 返回预置响应。"""

    routes = {}
    calls = []
    proxy_calls = []

    def __init__(self, headers=None, proxies=None, **kwargs):
        self.headers = headers or {}
        self.proxies = proxies

    def get_res(self, url, **kwargs):
        self.calls.append(("GET", url, self.headers))
        self.proxy_calls.append(("GET", url, self.proxies))
        return self.routes.get(url)

    def post_res(self, url, data=None, **kwargs):
        self.calls.append(("POST", url, self.headers))
        self.proxy_calls.append(("POST", url, self.proxies))
        return self.routes.get(url)


class _StubEvent:
    """模拟 ``app.sdk.events.Event``。"""

    def __init__(self, event_data):
        self.event_data = event_data


class _BareResponse:
    """刻意不提供 headers 属性，用于验证检测逻辑的降级能力。"""

    def __init__(self, status_code=403, text="Forbidden"):
        self.status_code = status_code
        self.text = text


# Cloudflare 托管挑战的真实响应特征（取自 cnlang.org 实测响应）
CF_CHALLENGE_HEADERS = {"Cf-Mitigated": "challenge", "Server": "cloudflare"}
CF_CHALLENGE_BODY = (
    "<html><head><title>Just a moment...</title></head><body>"
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
    "</body></html>"
)


@pytest.fixture
def host(monkeypatch):
    """安装宿主桩模块，返回便于断言的可控对象集合。"""
    _StubScheduler.reset()
    _StubRequestUtils.routes = {}
    _StubRequestUtils.calls = []
    _StubRequestUtils.proxy_calls = []
    logger = _StubLogger()

    class _EventType(Enum):
        PluginAction = "plugin.action"

    class _MessageType(Enum):
        Plugin = "插件"

    modules = {}

    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []

    plugins_pkg = types.ModuleType("app.plugins")
    plugins_pkg.__path__ = []
    plugins_pkg._PluginBase = _StubPluginBase

    sdk_pkg = types.ModuleType("app.sdk")
    sdk_pkg.__path__ = []

    schemas_pkg = types.ModuleType("app.schemas")
    schemas_pkg.__path__ = []

    types_mod = types.ModuleType("app.schemas.types")
    types_mod.EventType = _EventType
    types_mod.MessageType = _MessageType

    config_mod = types.ModuleType("app.sdk.config")
    config_mod.settings = _StubSettings
    config_mod.global_vars = None

    events_mod = types.ModuleType("app.sdk.events")
    events_mod.Event = _StubEvent
    events_mod.eventmanager = _StubEventManager()

    logging_mod = types.ModuleType("app.sdk.logging")
    logging_mod.logger = logger

    network_mod = types.ModuleType("app.sdk.network")
    network_mod.RequestUtils = _StubRequestUtils

    scheduler_mod = types.ModuleType("app.sdk.scheduler")
    scheduler_mod.add_plugin_once_job = _StubScheduler.add_plugin_once_job
    scheduler_mod.remove_plugin_once_job = _StubScheduler.remove_plugin_once_job

    sdk_pkg.scheduler = scheduler_mod
    sdk_pkg.config = config_mod
    sdk_pkg.events = events_mod
    sdk_pkg.logging = logging_mod
    sdk_pkg.network = network_mod
    schemas_pkg.types = types_mod

    modules.update(
        {
            "app": app_pkg,
            "app.plugins": plugins_pkg,
            "app.sdk": sdk_pkg,
            "app.sdk.config": config_mod,
            "app.sdk.events": events_mod,
            "app.sdk.logging": logging_mod,
            "app.sdk.network": network_mod,
            "app.sdk.scheduler": scheduler_mod,
            "app.schemas": schemas_pkg,
            "app.schemas.types": types_mod,
        }
    )
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    module_name = f"app.plugins.{PLUGIN_ID}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    yield types.SimpleNamespace(
        module=module,
        logger=logger,
        scheduler=_StubScheduler,
        scheduler_module=scheduler_mod,
        request=_StubRequestUtils,
        event_cls=_StubEvent,
        response_cls=_StubResponse,
        event_type=_EventType,
    )

    sys.modules.pop(module_name, None)


def _enabled_plugin(host, **overrides):
    """构造一个已按给定配置初始化的插件实例。"""
    config = {"enabled": True, "cron": "0 7 * * *", "cookie": "a=1; b=2"}
    config.update(overrides)
    plugin = host.module.CnlangSigninV3()
    plugin.init_plugin(config)
    return plugin


def _record(date, success=True, money="100", content="签到成功"):
    """构造一条签到历史记录。"""
    return {
        "date": date,
        "username": "tester",
        "totalContinuousCheckIn": 1,
        "money": money,
        "content": content,
        "success": success,
    }


# ---------------------------------------------------------------------------
# 元数据与索引一致性
# ---------------------------------------------------------------------------


def test_metadata_matches_directory_and_index(host):
    """主类名、目录名与 package.v3.json 中的版本必须一致。"""
    plugin_cls = host.module.CnlangSigninV3
    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))

    assert plugin_cls.__name__ == "CnlangSigninV3"
    assert PLUGIN_FILE.parent.name == plugin_cls.__name__.lower()
    assert "CnlangSigninV3" in index
    assert index["CnlangSigninV3"]["version"] == plugin_cls.plugin_version
    assert next(iter(index["CnlangSigninV3"]["history"])) == f"v{plugin_cls.plugin_version}"


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_init_plugin_reads_config(host):
    """init_plugin 应按配置重建全部运行状态。"""
    plugin = _enabled_plugin(
        host,
        notify=True,
        notify_style="style3",
        random_delay="10-20",
        history_days="7",
        use_proxy=True,
    )

    assert plugin.get_state() is True
    assert plugin._cron == "0 7 * * *"
    assert plugin._notify is True
    assert plugin._notify_style == "style3"
    assert plugin._random_delay == "10-20"
    assert plugin._history_days == 7
    assert plugin._use_proxy is True


def test_init_plugin_tolerates_bad_history_days(host):
    """历史保留天数非法时回落到默认 30 天，不应抛异常。"""
    plugin = _enabled_plugin(host, history_days="abc")

    assert plugin._history_days == 30


def test_init_plugin_reads_anti_bot_config(host):
    """反爬相关配置（浏览器模式、自定义 UA）应被正确读入。"""
    plugin = _enabled_plugin(host, user_agent="  UA-X  ", browser_mode=False)

    assert plugin._user_agent == "UA-X"
    assert plugin._browser_mode is False


def test_browser_mode_defaults_to_enabled(host):
    """未显式配置时浏览器模式默认开启：站点常态启用 Cloudflare。"""
    plugin = _enabled_plugin(host)

    assert plugin._browser_mode is True
    assert plugin._user_agent is None


def test_save_config_persists_anti_bot_fields(host):
    """回写配置必须保留 UA 与浏览器模式，否则保存一次就会丢失。"""
    plugin = _enabled_plugin(host, user_agent="UA-Y", browser_mode=True, onlyonce=True)

    assert plugin.saved_config["user_agent"] == "UA-Y"
    assert plugin.saved_config["browser_mode"] is True


def test_init_plugin_is_repeatable(host):
    """重复初始化应重建状态且不堆积一次性任务。"""
    plugin = _enabled_plugin(host)
    plugin.init_plugin({"enabled": False, "cron": "0 8 * * *"})

    assert plugin.get_state() is False
    assert plugin._cron == "0 8 * * *"
    # 两次初始化都会先取消上一轮登记的任务
    assert len(host.scheduler.removed) == 4


def test_clear_flag_resets_itself_and_clears_history(host):
    """“清除历史记录”执行一次后应回写关闭，并清空历史与最近结果。"""
    plugin = _enabled_plugin(host)
    plugin.save_data(host.module.KEY_HISTORY, [_record("2026-09-01 07:00:00")])
    plugin.save_data(host.module.KEY_LAST_RESULT, {"success": True})

    plugin.init_plugin({"enabled": True, "cookie": "a=1", "clear": True})

    assert plugin.get_data(host.module.KEY_HISTORY) is None
    assert plugin.get_data(host.module.KEY_LAST_RESULT) is None
    assert plugin.saved_config["clear"] is False


def test_onlyonce_registers_host_job_and_resets_flag(host):
    """“立即运行一次”应登记为宿主一次性任务并复位开关。"""
    plugin = _enabled_plugin(host, onlyonce=True)

    assert len(host.scheduler.added) == 1
    added = host.scheduler.added[0]
    assert added["plugin_id"] == "CnlangSigninV3"
    assert added["job_id"] == host.module.JOB_SIGNIN_ONCE
    assert added["delay_seconds"] == 3
    assert plugin.saved_config["onlyonce"] is False


def test_onlyonce_falls_back_to_background_thread(host, monkeypatch):
    """宿主调度器不可用时应转入后台线程兜底，不阻塞配置保存请求。"""
    host.scheduler.available = False
    fallback = []
    monkeypatch.setattr(
        host.module.CnlangSigninV3,
        "_run_in_background",
        lambda self, delay_seconds=0: fallback.append(delay_seconds),
    )

    plugin = _enabled_plugin(host, onlyonce=True)

    assert fallback == [3]
    assert plugin.saved_config["onlyonce"] is False


def test_init_plugin_survives_missing_once_job_api(host, monkeypatch):
    """主程序未提供 add_plugin_once_job 时不得抛出，改用后台线程兜底。

    这正是“配置保存失败：未知错误”的直接成因：旧版本主程序的
    ``app.sdk.scheduler`` 没有一次性任务接口，直接属性访问会抛出
    ``AttributeError``；而宿主在配置保存流程中不捕获 ``init_plugin()`` 的异常，
    只把它转成 HTTP 500，前端于是只显示“未知错误”。
    """
    monkeypatch.delattr(host.scheduler_module, "add_plugin_once_job", raising=False)
    fallback = []
    monkeypatch.setattr(
        host.module.CnlangSigninV3,
        "_run_in_background",
        lambda self, delay_seconds=0: fallback.append(delay_seconds),
    )

    plugin = _enabled_plugin(host, onlyonce=True)

    assert fallback == [3]
    assert plugin.get_state() is True


def test_stop_service_survives_missing_remove_job_api(host, monkeypatch):
    """主程序未提供 remove_plugin_once_job 时 stop_service 应静默跳过。"""
    monkeypatch.delattr(host.scheduler_module, "remove_plugin_once_job", raising=False)
    plugin = _enabled_plugin(host)

    plugin.stop_service()  # 不应抛出 AttributeError


def test_init_plugin_never_raises_when_internal_steps_fail(host, monkeypatch):
    """init_plugin 内部的任何失败都必须就地兜底，不能冒泡成 HTTP 500。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(
        type(plugin), "stop_service", lambda self: (_ for _ in ()).throw(RuntimeError("停用失败"))
    )
    monkeypatch.setattr(
        type(plugin), "update_config", lambda self, config: (_ for _ in ()).throw(RuntimeError("回写失败"))
    )
    monkeypatch.setattr(
        type(plugin), "del_data", lambda self, key, plugin_id=None: (_ for _ in ()).throw(RuntimeError("清理失败"))
    )

    plugin.init_plugin({"enabled": True, "cookie": "a=1", "clear": True, "onlyonce": True})

    assert plugin.get_state() is True


def test_resolve_timezone_degrades_instead_of_raising(host, monkeypatch):
    """时区解析失败时返回 None，交由调度器使用自身默认时区。"""
    module = host.module
    monkeypatch.setattr(module, "pytz", None)
    monkeypatch.setitem(sys.modules, "zoneinfo", None)

    assert module._resolve_timezone() is None

    plugin = module.CnlangSigninV3()
    plugin._cron = "0 7 * * *"
    # 表达式合法时仍应构建出触发器，只是不再指定时区
    assert plugin._build_trigger() is not None


def test_stop_service_is_idempotent(host):
    """stop_service 可重复调用，且会取消本插件登记的全部一次性任务。"""
    plugin = _enabled_plugin(host)
    host.scheduler.removed.clear()

    plugin.stop_service()
    plugin.stop_service()

    removed_ids = [job_id for _, job_id in host.scheduler.removed]
    assert removed_ids.count(host.module.JOB_SIGNIN_ONCE) == 2
    assert removed_ids.count(host.module.JOB_SIGNIN_DELAYED) == 2


# ---------------------------------------------------------------------------
# 定时服务注册
# ---------------------------------------------------------------------------


def test_get_service_empty_when_disabled(host):
    """插件停用时不注册定时服务。"""
    plugin = host.module.CnlangSigninV3()
    plugin.init_plugin({"enabled": False, "cron": "0 7 * * *"})

    assert plugin.get_service() == []


def test_get_service_rejects_invalid_cron(host):
    """cron 表达式非法时不注册服务，也不抛异常。"""
    plugin = _enabled_plugin(host, cron="not a cron")

    assert plugin.get_service() == []


def test_get_service_registers_cron_trigger(host):
    """启用且 cron 合法时应返回一个稳定的宿主调度服务声明。"""
    plugin = _enabled_plugin(host)
    services = plugin.get_service()

    assert len(services) == 1
    assert services[0]["id"] == "CnlangSigninV3.Signin"
    assert services[0]["func"] == plugin._scheduled_signin
    assert services[0]["trigger"] is not None


def test_scheduled_signin_uses_host_once_job_for_delay(host):
    """配置了随机延迟时，定时签到应转成宿主一次性任务而非阻塞等待。"""
    plugin = _enabled_plugin(host, random_delay="30-30")
    plugin._scheduled_signin()

    assert len(host.scheduler.added) == 1
    assert host.scheduler.added[0]["job_id"] == host.module.JOB_SIGNIN_DELAYED
    assert host.scheduler.added[0]["delay_seconds"] == 30


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 0), ("", 0), ("abc", 0), ("10-5", 0), ("-3-5", 0), ("7-7", 7)],
)
def test_random_delay_seconds_parsing(host, raw, expected):
    """随机延迟解析：非法配置一律按不延迟处理。"""
    plugin = _enabled_plugin(host, random_delay=raw)

    assert plugin._random_delay_seconds() == expected


def test_random_delay_seconds_stays_in_range(host):
    """随机延迟结果应落在配置区间内。"""
    plugin = _enabled_plugin(host, random_delay="100-200")

    for _ in range(50):
        assert 100 <= plugin._random_delay_seconds() <= 200


def test_analyze_history_on_empty_input(host):
    """空历史返回全零统计，不应抛异常。"""
    plugin = _enabled_plugin(host)
    stats = plugin._analyze_history([])

    assert stats["success_rate"] == "0%"
    assert stats["total_days"] == 0
    assert stats["best_time"] == "无"


def test_analyze_history_success_rate_and_money(host):
    """成功率与大洋统计应按成功记录计算。"""
    plugin = _enabled_plugin(host)
    history = [
        _record("2026-09-01 07:00:00", money="100"),
        _record("2026-09-02 08:00:00", money="200"),
        _record("2026-09-03 09:00:00", success=False, money="0", content="未获取到用户名"),
    ]

    stats = plugin._analyze_history(history)

    assert stats["total_days"] == 3
    assert stats["success_days"] == 2
    assert stats["fail_days"] == 1
    assert stats["success_rate"] == "66.7%"
    assert stats["total_money"] == 300
    assert stats["avg_money"] == "150.0"
    assert stats["best_time"] == "07:00"


def test_analyze_history_tracks_continuous_streak(host):
    """连续签到统计应取历史最长连续天数，并给出当前连续天数。"""
    plugin = _enabled_plugin(host)
    history = [
        _record("2026-09-01 07:00:00"),
        _record("2026-09-02 07:00:00"),
        _record("2026-09-03 07:00:00"),
        # 中断一天
        _record("2026-09-05 07:00:00"),
        _record("2026-09-06 07:00:00"),
    ]

    stats = plugin._analyze_history(history)

    assert stats["max_continuous"] == 3
    assert stats["current_continuous"] == 2


def test_is_success_falls_back_to_legacy_content(host):
    """缺少 success 字段的旧数据应按响应文本判断。"""
    plugin = _enabled_plugin(host)

    assert plugin._is_success({"content": "恭喜，签到成功"}) is True
    assert plugin._is_success({"content": "未获取到用户名"}) is False
    assert plugin._is_success({"content": "签到成功", "success": False}) is False


def test_prune_history_drops_expired_and_invalid_records(host):
    """超期记录与时间非法记录都应在裁剪时丢弃。"""
    plugin = _enabled_plugin(host, history_days=5)
    now = datetime.now()
    history = [
        _record(now.strftime("%Y-%m-%d %H:%M:%S")),
        _record((now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")),
        {"date": "not-a-date"},
    ]

    pruned = plugin._prune_history(history)

    assert len(pruned) == 1


def test_build_headers_contains_cookie_and_fixed_accept_encoding(host):
    """请求头应携带配置的 Cookie，且 Accept-Encoding 拼写正确。"""
    plugin = _enabled_plugin(host, cookie="sid=abc")
    headers = plugin._build_headers()

    assert headers["Cookie"] == "sid=abc"
    assert headers["Accept-Encoding"] == "gzip, deflate, br"
    assert "Accept - Encoding" not in headers
    assert headers["Host"] == host.module.SITE_HOST


def test_search_returns_first_group_or_none(host):
    """正则提取未命中时返回 None。"""
    assert host.module.CnlangSigninV3._search(r"a(\d+)", "a12b") == "12"
    assert host.module.CnlangSigninV3._search(r"x(\d+)", "a12b") is None
    assert host.module.CnlangSigninV3._search(r"a(\d+)", "") is None


# ---------------------------------------------------------------------------
# 网络与签到流程
# ---------------------------------------------------------------------------


def test_fetch_returns_none_on_non_200(host):
    """状态码非 200 时 _fetch 返回 None，不抛异常。"""
    plugin = _enabled_plugin(host)
    url = "https://example.invalid/x"
    host.request.routes = {url: host.response_cls(500, "boom")}

    assert plugin._fetch(url) is None


def test_fetch_returns_none_on_exception(host, monkeypatch):
    """请求抛异常时 _fetch 返回 None，由调用方按业务处理。"""
    plugin = _enabled_plugin(host)
    url = "https://example.invalid/y"

    def _raise(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(host.request, "get_res", _raise)

    assert plugin._fetch(url) is None


# --- Cloudflare 拦截识别 ---------------------------------------------------


def test_is_cf_challenge_detects_official_marker(host):
    """Cloudflare 官方标记 Cf-Mitigated: challenge 应立即判定为挑战页。"""
    response = host.response_cls(403, "", headers={"Cf-Mitigated": "challenge"})

    assert host.module.CnlangSigninV3._is_cf_challenge(response) is True


def test_is_cf_challenge_falls_back_to_body_heuristic(host):
    """无官方标记时，仅当 Server 为 cloudflare 且正文含挑战特征才判定拦截。"""
    cls = host.module.CnlangSigninV3

    assert (
        cls._is_cf_challenge(
            host.response_cls(403, CF_CHALLENGE_BODY, headers={"Server": "cloudflare"})
        )
        is True
    )
    # 非 Cloudflare 站点即使正文含同名字样也不误判
    assert (
        cls._is_cf_challenge(
            host.response_cls(403, CF_CHALLENGE_BODY, headers={"Server": "nginx"})
        )
        is False
    )
    # Cloudflare 返回的普通 403 不应被误判为挑战页
    assert (
        cls._is_cf_challenge(
            host.response_cls(403, "Forbidden", headers={"Server": "cloudflare"})
        )
        is False
    )


def test_is_cf_challenge_tolerates_missing_headers(host):
    """响应对象没有 headers 属性时不得抛异常，按未拦截处理。"""
    assert host.module.CnlangSigninV3._is_cf_challenge(_BareResponse()) is False


def test_request_flags_cloudflare_challenge_with_status(host):
    """被拦截时 _request 返回 (None, 状态码, True)，供上层切换浏览器模式。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            403, CF_CHALLENGE_BODY, headers=CF_CHALLENGE_HEADERS
        )
    }

    text, status, cf_blocked = plugin._request(
        module.SIGN_PAGE_URL, headers=plugin._build_headers()
    )

    assert text is None
    assert status == 403
    assert cf_blocked is True


def test_request_falls_back_to_direct_when_proxy_unreachable(host, monkeypatch):
    """代理无响应时应自动回退直连重试一轮，避免代理失效导致整轮签到报废。"""
    module = host.module
    plugin = _enabled_plugin(host, use_proxy=True)
    monkeypatch.setattr(module.settings, "PROXY", "http://proxy.invalid:8080")

    responses = [None, host.response_cls(200, "ok")]
    seen_proxies = []

    def _get_res(self, url, **kwargs):
        seen_proxies.append(self.proxies)
        return responses.pop(0)

    monkeypatch.setattr(host.request, "get_res", _get_res)

    text, status, cf_blocked = plugin._request("https://example.invalid/z")

    assert (text, status, cf_blocked) == ("ok", 200, False)
    assert seen_proxies[0] == "http://proxy.invalid:8080"
    assert seen_proxies[1] == module.DIRECT_PROXIES


# --- 请求头 UA --------------------------------------------------------------


def test_build_headers_sends_configured_user_agent(host):
    """自定义 UA 必须原样发送：cf_clearance 与 UA 绑定，不一致会立即失效。"""
    plugin = _enabled_plugin(host, user_agent="Mozilla/5.0 CustomUA")

    assert plugin._build_headers()["User-Agent"] == "Mozilla/5.0 CustomUA"


def test_build_headers_defaults_to_chrome_user_agent(host):
    """未配置 UA 时使用内置 Chrome UA，避免被 python-requests 标识识别为爬虫。"""
    plugin = _enabled_plugin(host, user_agent="")

    assert plugin._build_headers()["User-Agent"] == host.module.DEFAULT_USER_AGENT
    assert "Chrome/" in host.module.DEFAULT_USER_AGENT


# --- Cloudflare 拦截后的分支 -------------------------------------------------


def test_execute_signin_switches_to_browser_mode_on_cloudflare(host, monkeypatch):
    """requests 被 Cloudflare 拦截时应自动切换到浏览器模式完成签到。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            403, CF_CHALLENGE_BODY, headers=CF_CHALLENGE_HEADERS
        )
    }
    called = []

    def _fake_browser(self):
        called.append(True)
        self.save_data(
            module.KEY_LAST_RESULT,
            {
                "time": "2026-09-29 20:00:00",
                "success": True,
                "username": "tester",
                "money": "520",
                "content": "签到成功（浏览器模式）",
                "total_signs": 3,
            },
        )
        return True

    monkeypatch.setattr(module.CnlangSigninV3, "_signin_by_browser", _fake_browser)

    result = plugin.signin()

    assert called == [True]
    assert result["success"] is True
    assert result["money"] == "520"


def test_execute_signin_advises_enabling_browser_mode(host):
    """关闭浏览器模式时，Cloudflare 拦截应给出可执行建议而非“未知错误”。

    站点签到路径的挑战是交互式的，自动化客户端都过不去，因此建议文案的重点是
    「如何刷新 Cookie」，而不是「打开浏览器模式」。
    """
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=False, notify=True)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            403, CF_CHALLENGE_BODY, headers=CF_CHALLENGE_HEADERS
        )
    }

    result = plugin.signin()

    assert result["success"] is False
    assert result["content"] == module.CF_ADVICE
    assert "cf_clearance" in result["content"]
    assert "Cookie" in result["content"]
    assert "navigator.userAgent" in result["content"]


def test_cloudflare_block_reports_browser_unavailable(host):
    """浏览器模式开启但宿主未提供 app.sdk.browser 时，应给出明确的依赖提示。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            403, CF_CHALLENGE_BODY, headers=CF_CHALLENGE_HEADERS
        )
    }

    result = plugin.signin()

    assert result["success"] is False
    assert "浏览器模式不可用" in result["content"]


def test_signin_by_browser_without_host_support_returns_false(host):
    """宿主未提供 app.sdk.browser 时 _signin_by_browser 返回 False 而非抛异常。"""
    plugin = _enabled_plugin(host, browser_mode=True)

    assert plugin._signin_by_browser() is False


def test_execute_signin_reports_http_status_in_failure(host):
    """普通失败应把状态码写进原因，避免只报“失败”而无法定位。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {module.SIGN_PAGE_URL: host.response_cls(500, "server error")}

    result = plugin.signin()

    assert result["success"] is False
    assert "500" in result["content"]


# --- 浏览器模式内部机制 -------------------------------------------------------


class _FakePage:
    """可编程的假页面：按需返回 HTML / 标题 / UA，并记录页面内请求。"""

    def __init__(self, html="", title="", user_agent="", fail_content=False):
        self._html = html
        self._title = title
        self._user_agent = user_agent
        self._fail_content = fail_content
        self.headers = {}
        self.goto_calls = []
        self.reload_calls = []
        self.evaluate_calls = []
        # 页面内 fetch 的返回值：{url: 响应正文}
        self.fetch_map = {}

    def content(self):
        if self._fail_content:
            raise RuntimeError("page is navigating")
        return self._html

    def title(self):
        return self._title

    def goto(self, url, *args, **kwargs):
        self.goto_calls.append(url)
        return None

    def reload(self, *args, **kwargs):
        self.reload_calls.append(True)
        return None

    def screenshot(self, *args, **kwargs):
        return b""

    def evaluate(self, expression, *args):
        self.evaluate_calls.append(expression)
        if "navigator.userAgent" in expression:
            return self._user_agent
        if "fetch" in expression:
            target = args[0][0] if args and args[0] else None
            return self.fetch_map.get(target, "")
        return ""

    def set_extra_http_headers(self, headers):
        self.headers.update(headers)

    def set_default_timeout(self, timeout):
        self.headers["__timeout"] = timeout


class _FakeContext:
    """可编程的假浏览器上下文。"""

    def __init__(self, cookies=None, add_cookies=None):
        self._cookies = cookies or []
        self._add_cookies = add_cookies
        self.jar = []

    def cookies(self):
        if isinstance(self._cookies, Exception):
            raise self._cookies
        return self._cookies

    def add_cookies(self, jar):
        if isinstance(self._add_cookies, Exception):
            raise self._add_cookies
        if self._add_cookies is None:
            raise AttributeError("add_cookies")
        self.jar.extend(jar)


class _FakeBrowserContext:
    """假浏览器上下文，按脚本弹出页面并记录 Cookie / 关闭动作。"""

    def __init__(self, pages=None, cookies=None):
        self._pages = list(pages or [])
        self._cookies = cookies or []
        self.jar = []
        self.closed = False

    def new_page(self):
        return self._pages.pop(0) if self._pages else _FakePage()

    def cookies(self):
        return self._cookies

    def add_cookies(self, jar):
        self.jar.extend(jar)

    def close(self):
        self.closed = True


def _install_browser_stub(monkeypatch, context):
    """把 ``app.sdk.browser`` 装成桩模块，返回记录启动参数的列表。"""
    calls = []
    module = types.ModuleType("app.sdk.browser")

    def launch_browser_context(**kwargs):
        calls.append(kwargs)
        return context

    module.launch_browser_context = launch_browser_context
    monkeypatch.setitem(sys.modules, "app.sdk.browser", module)
    return calls


def test_is_cf_challenge_page_covers_title_and_body(host):
    """挑战页识别应同时覆盖标题特征与正文特征，普通页面不得误判。"""
    cls = host.module.CnlangSigninV3

    assert cls._is_cf_challenge_page("<html></html>", "Just a moment...") is True
    assert cls._is_cf_challenge_page("<html></html>", "请稍候...") is True
    assert (
        cls._is_cf_challenge_page(
            '<script src="https://challenges.cloudflare.com/turnstile"></script>'
            '<div id="cf-chl-widget-abc">',
            "国语视界",
        )
        is True
    )
    assert cls._is_cf_challenge_page("<html><body>hello</body></html>", "国语视界") is False


def test_solve_cloudflare_returns_true_when_page_has_real_content(host, monkeypatch):
    """页面已有真实内容且不再是挑战页时应立即判定通过。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    page = _FakePage(html="<html><body>" + "x" * 800 + "</body></html>", title="国语视界")

    assert plugin._solve_cloudflare(page, budget_seconds=6, max_rounds=3) is True


def test_solve_cloudflare_returns_false_on_persistent_challenge(host, monkeypatch):
    """持续停留在挑战页时应耗尽轮询并返回 False。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    page = _FakePage(html="<html><body>" + "x" * 800 + "</body></html>", title="Just a moment...")

    assert plugin._solve_cloudflare(page, budget_seconds=6, max_rounds=3) is False


def test_inject_cookies_prefers_browser_cookie_jar(host):
    """上下文支持 add_cookies 时应写入 Cookie 罐，而不是覆盖请求头。"""
    plugin = _enabled_plugin(host, cookie="sid=abc")
    context = _FakeContext(add_cookies=True)
    page = _FakePage()

    plugin._inject_cookies(context, page)

    assert context.jar[0]["name"] == "sid"
    assert context.jar[0]["value"] == "abc"
    assert context.jar[0]["domain"] == f".{host.module.SITE_HOST}"
    assert page.headers == {}


def test_inject_cookies_falls_back_to_request_header(host):
    """上下文不提供 add_cookies 时应回退到请求头方式。"""
    plugin = _enabled_plugin(host, cookie="sid=abc; t=1")
    page = _FakePage()

    plugin._inject_cookies(_FakeContext(add_cookies=None), page)

    assert page.headers == {"cookie": "sid=abc; t=1"}


def test_inject_cookies_falls_back_when_jar_write_fails(host):
    """写入 Cookie 罐失败时应回退到请求头方式，不抛异常。"""
    plugin = _enabled_plugin(host, cookie="sid=abc")
    page = _FakePage()

    plugin._inject_cookies(_FakeContext(add_cookies=RuntimeError("boom")), page)

    assert page.headers == {"cookie": "sid=abc"}


def test_inject_cookies_skips_when_no_cookie(host):
    """未配置 Cookie 时不应写入任何内容。"""
    plugin = _enabled_plugin(host, cookie="")
    context = _FakeContext(add_cookies=True)
    page = _FakePage()

    plugin._inject_cookies(context, page)

    assert context.jar == []
    assert page.headers == {}


def test_refresh_cookies_records_browser_user_agent(host):
    """浏览器 UA 存进插件数据，且**绝不覆盖用户配置的 UA**。

    配置里的 UA 是用户为「自己的浏览器 + 自己的 Cookie」设定的，而 cf_clearance 与 UA
    绑定。一旦被浏览器模式的 UA 覆盖，用户再粘贴一份新鲜 Cookie 也会因 UA 不匹配立即
    失效——等于把唯一可用的方案弄坏（3.6.6 及更早版本实际踩到的坑）。
    """
    module = host.module
    plugin = _enabled_plugin(host, cookie="sid=abc", user_agent="ConfiguredUA")
    context = _FakeContext(
        cookies=[{"name": "cf_clearance", "value": "fresh"}, {"name": "sid", "value": "abc"}]
    )
    page = _FakePage(user_agent="BrowserRealUA")

    plugin._refresh_cookies_from_browser(context, page)

    # 浏览器 UA 进插件数据
    assert plugin._browser_user_agent == "BrowserRealUA"
    assert plugin.store[module.KEY_BROWSER_UA] == "BrowserRealUA"
    # 用户配置的 UA 原样保留
    assert plugin._user_agent == "ConfiguredUA"
    # 整个流程不得回写用户配置
    assert plugin.saved_config == {}
    # Cloudflare 通行证单独缓存（带 domain/path），不污染用户配置的 Cookie 字段
    assert [item["name"] for item in plugin._cf_cookies] == ["cf_clearance"]
    assert plugin._cf_cookies[0]["value"] == "fresh"
    assert plugin.store[module.KEY_CF_COOKIES] == plugin._cf_cookies
    assert "cf_clearance" not in (plugin._cookie or "")
    assert plugin._cookie == "sid=abc"


def test_refresh_cookies_never_rewrites_config_cookie(host):
    """浏览器会话里的论坛 Cookie 不得合并进用户配置的 Cookie 字段。

    早期版本会把浏览器会话 Cookie 合并回去，实测一次执行里从 16 条涨到 26 条，
    把用户的登录 Cookie 淹没。
    """
    plugin = _enabled_plugin(host, cookie="sid=abc", user_agent="ConfiguredUA")
    context = _FakeContext(
        cookies=[
            {"name": "cf_clearance", "value": "fresh"},
            {"name": "3rir_2132_saltkey", "value": "browser-salt"},
            {"name": "3rir_2132_sid", "value": "browser-sid"},
            {"name": "3rir_2132_auth", "value": "browser-auth"},
        ]
    )

    plugin._refresh_cookies_from_browser(context, _FakePage())

    assert plugin._cookie == "sid=abc"
    assert plugin.saved_config == {}


def test_refresh_cookies_dedupes_repeated_scope(host):
    """同一 (name, domain, path) 的重复通行证只缓存一条，避免缓存逐轮膨胀。"""
    module = host.module
    plugin = _enabled_plugin(host, cookie="sid=abc")
    context = _FakeContext(
        cookies=[
            {"name": "cf_clearance", "value": "v1", "domain": ".cnlang.org", "path": "/"},
            {"name": "cf_clearance", "value": "v1", "domain": ".cnlang.org", "path": "/"},
            {"name": "cf_clearance", "value": "v2", "domain": "cnlang.org", "path": "/"},
        ]
    )

    plugin._refresh_cookies_from_browser(context, _FakePage())

    cached = plugin.store[module.KEY_CF_COOKIES]
    assert len(cached) == 2
    assert [item["domain"] for item in cached] == [".cnlang.org", "cnlang.org"]


def test_effective_user_agent_prefers_configured_over_browser(host):
    """用户配置的 UA 永远优先于浏览器记录的 UA。"""
    plugin = _enabled_plugin(host, user_agent="ConfiguredUA")
    plugin._browser_user_agent = "BrowserRealUA"

    assert plugin._effective_user_agent() == "ConfiguredUA"


def test_effective_user_agent_falls_back_to_browser_ua(host):
    """用户没配 UA 时才用浏览器记录的 UA 兜底。"""
    module = host.module
    plugin = _enabled_plugin(host, user_agent="")
    plugin.store[module.KEY_BROWSER_UA] = "BrowserRealUA"
    plugin._browser_user_agent = None

    assert plugin._effective_user_agent() == "BrowserRealUA"


def test_effective_user_agent_without_any_record(host):
    """都没有时返回 None，由调用方回落内置默认 UA。"""
    plugin = _enabled_plugin(host, user_agent="")

    assert plugin._effective_user_agent() is None


def test_refresh_cookies_keeps_configured_cookie_when_browser_has_none(host):
    """浏览器没给出任何非 CF Cookie 时，用户配置的 Cookie 不能被清空。"""
    plugin = _enabled_plugin(host, cookie="sid=abc; saltkey=xyz", user_agent="ConfiguredUA")

    plugin._refresh_cookies_from_browser(
        _FakeContext(cookies=[{"name": "cf_clearance", "value": "fresh"}]), _FakePage()
    )

    assert "sid=abc" in plugin._cookie
    assert "saltkey=xyz" in plugin._cookie
    assert "cf_clearance" not in plugin._cookie


def test_refresh_cookies_survives_broken_browser_api(host):
    """浏览器 Cookie / UA 读取失败时保留原配置，不得抛异常。"""
    plugin = _enabled_plugin(host, cookie="sid=abc", user_agent="ConfiguredUA")

    plugin._refresh_cookies_from_browser(
        _FakeContext(cookies=RuntimeError("no cookies api")), _FakePage()
    )

    assert plugin._cookie == "sid=abc"
    assert plugin._user_agent == "ConfiguredUA"


def test_browser_proxy_url_respects_use_proxy_flag(host, monkeypatch):
    """未开启代理时返回 None；开启时从宿主配置取出代理地址。"""
    monkeypatch.setattr(host.module.settings, "PROXY", "http://proxy.invalid:8080")

    plugin = _enabled_plugin(host, use_proxy=False)
    assert plugin._browser_proxy_url() is None

    plugin = _enabled_plugin(host, use_proxy=True)
    assert plugin._browser_proxy_url() == "http://proxy.invalid:8080"


def test_browser_proxy_url_supports_dict_form(host, monkeypatch):
    """宿主以字典形式提供代理时应取出 https/http 之一。"""
    monkeypatch.setattr(
        host.module.settings,
        "PROXY",
        {"http": "http://p:1", "https": "https://p:2"},
    )
    plugin = _enabled_plugin(host, use_proxy=True)

    assert plugin._browser_proxy_url() == "https://p:2"


# --- Cloudflare 自管 Cookie 的剔除 -------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("cf_clearance", True),
        ("CF_CLEARANCE", True),
        ("__cf_bm", True),
        ("__cfduid", True),
        ("cf_chl_2", True),
        ("cf_chl_opt", True),
        # 论坛登录态与普通 Cookie 不得被误剔
        ("_auth", False),
        ("saltkey", False),
        ("3rir_2132_sid", False),
        ("cfx", False),
        ("", False),
    ],
)
def test_is_cloudflare_cookie(host, name, expected):
    """只识别 Cloudflare 自管 Cookie，不能误伤论坛登录态。"""
    assert host.module._is_cloudflare_cookie(name) is expected


def test_inject_cookies_keeps_configured_cloudflare_cookies(host):
    """配置里的 cf_clearance 必须照常注入，不能剔除。

    cf_clearance 是域级通行证：只要还在有效期内，注入后首页与签到页都会直接放行，
    是成本最低的热启动路径。剔除它等于每次执行都从零过一次挑战。
    """
    plugin = _enabled_plugin(
        host, cookie="_auth=abc; cf_clearance=stale; __cf_bm=x; saltkey=sk"
    )
    context = _FakeContext(add_cookies=True)

    plugin._inject_cookies(context, _FakePage())

    assert [c["name"] for c in context.jar] == [
        "_auth",
        "cf_clearance",
        "__cf_bm",
        "saltkey",
    ]


def test_inject_cookies_header_fallback_keeps_cloudflare_cookies(host):
    """回退到请求头方式时同样保留全部 Cookie。"""
    plugin = _enabled_plugin(host, cookie="_auth=abc; cf_clearance=stale")
    page = _FakePage()

    plugin._inject_cookies(_FakeContext(add_cookies=None), page)

    assert page.headers == {"cookie": "_auth=abc; cf_clearance=stale"}


def test_inject_cookies_appends_cached_cloudflare_cookies_last(host):
    """缓存的通行证带完整作用域，且必须在配置之后注入以覆盖旧值。"""
    module = host.module
    plugin = _enabled_plugin(host, cookie="_auth=abc", browser_mode=True)
    plugin.store[module.KEY_CF_COOKIES] = [
        {
            "name": "cf_clearance",
            "value": "fresh",
            "domain": ".cnlang.org",
            "path": "/",
        },
        {
            "name": "cf_clearance",
            "value": "scoped",
            "domain": "cnlang.org",
            "path": "/dsu_paulsign-sign.html",
        },
    ]
    context = _FakeContext(add_cookies=True)

    plugin._inject_cookies(context, _FakePage())

    assert [c["name"] for c in context.jar] == ["_auth", "cf_clearance", "cf_clearance"]
    assert context.jar[1]["value"] == "fresh"
    assert context.jar[2]["path"] == "/dsu_paulsign-sign.html"


def test_stored_cf_cookies_reads_legacy_string_cache(host):
    """兼容早期版本写入的字符串缓存。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    plugin.store[module.KEY_CF_COOKIES] = "cf_clearance=legacy; __cf_bm=bm"

    items = plugin._stored_cf_cookies()

    assert [item["name"] for item in items] == ["cf_clearance", "__cf_bm"]
    assert all(item["domain"] == ".cnlang.org" for item in items)


# --- 挑战等待策略 ------------------------------------------------------------


def test_solve_cloudflare_does_not_reload_challenge_page(host, monkeypatch):
    """挑战页不应被反复重载——重载会让 Turnstile 的进度归零。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    page = _FakePage(html="<html><body>" + "x" * 800 + "</body></html>", title="Just a moment...")

    assert plugin._solve_cloudflare(page, budget_seconds=10, max_rounds=5) is False
    assert page.reload_calls == []


def test_solve_cloudflare_reloads_when_page_stays_blank(host, monkeypatch):
    """页面长时间空白（连接被挂起）时应重载一次争取拿到响应。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    page = _FakePage(html="", title="")

    assert plugin._solve_cloudflare(page, budget_seconds=30, max_rounds=8) is False
    assert page.reload_calls == [True]


def test_solve_cloudflare_limits_checkbox_clicks(host, monkeypatch):
    """补点人机验证框的次数要有上限，高频点击反而会被判为机器人。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    clicks = []
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_try_click_cf_checkbox", staticmethod(lambda page: clicks.append(1) or True)
    )
    page = _FakePage(html="<html><body>" + "x" * 800 + "</body></html>", title="Just a moment...")

    plugin._solve_cloudflare(page, budget_seconds=200, max_rounds=100)

    assert len(clicks) == 3


# --- 浏览器启动参数 ----------------------------------------------------------


def test_browser_mode_does_not_override_user_agent_by_default(host, monkeypatch):
    """未配置 UA 时不应把内置 Chrome/131 强加给浏览器。

    UA 与 Client Hints 不一致会被 Cloudflare 判为「非真实浏览器」，从而持续下发
    交互式挑战——这正是签到页卡住的原因之一。
    """
    plugin = _enabled_plugin(host, user_agent="", browser_mode=True)
    page = _FakePage(html="", title="Just a moment...")
    calls = _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page]))
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: False
    )
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_sign_page_via_fetch", lambda self, context, page: ""
    )

    plugin._signin_by_browser()

    assert calls and calls[0]["headless"] is True
    assert "user_agent" not in calls[0]


def test_browser_mode_passes_explicitly_configured_user_agent(host, monkeypatch):
    """用户显式配置了 UA 时必须原样传给浏览器。"""
    plugin = _enabled_plugin(host, user_agent="MyUA/1.0", browser_mode=True)
    page = _FakePage(html="", title="Just a moment...")
    calls = _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page]))
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: False
    )
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_sign_page_via_fetch", lambda self, context, page: ""
    )

    plugin._signin_by_browser()

    assert calls[0]["user_agent"] == "MyUA/1.0"


# --- 页面内 fetch 兜底 -------------------------------------------------------


def test_sign_page_via_fetch_rejects_challenge_response(host, monkeypatch):
    """页面内 fetch 若仍返回挑战页，应判定失败，而不是把挑战页当业务页解析。"""
    module = host.module
    plugin = _enabled_plugin(host)
    page = _FakePage()
    page.fetch_map = {module.SIGN_PAGE_URL: CF_CHALLENGE_BODY}
    monkeypatch.setattr(
        module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: True
    )

    assert plugin._sign_page_via_fetch(_FakeBrowserContext(pages=[page]), page) == ""


def test_browser_mode_falls_back_to_in_page_fetch(host, monkeypatch):
    """签到页文档导航被挑战时应改走页面内 fetch，并在页面上下文内完成签到。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    page = _FakePage(user_agent="RealUA")
    page.fetch_map = {
        module.SIGN_PAGE_URL: (
            '<input name="formhash" value="ff00" />'
            '<a title="访问我的空间">tester</a>'
        ),
        module.SIGN_SUBMIT_URL: '<div class="c">恭喜，签到成功</div>',
        module.CREDIT_URL: '<span id="hcredit_2">777</span>',
    }
    _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page]))
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    # 文档导航（签到页）始终被挑战，但首页正常
    monkeypatch.setattr(
        module.CnlangSigninV3,
        "_solve_cloudflare",
        lambda self, page, **kwargs: "首页" in (kwargs.get("label") or ""),
    )

    result = plugin._signin_by_browser()

    assert result is True
    last = plugin.get_data(module.KEY_LAST_RESULT)
    assert last["success"] is True
    assert last["username"] == "tester"
    assert last["money"] == "777"
    # 浏览器实际 UA 记录进插件数据，且不覆盖用户配置字段
    assert plugin.store[module.KEY_BROWSER_UA] == "RealUA"
    assert "user_agent" not in plugin.saved_config


def test_browser_mode_reports_failure_when_both_paths_challenged(host, monkeypatch):
    """文档导航与页面内 fetch 都被挑战时应给出明确失败原因，并已尝试有头模式。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    page = _FakePage(html="", title="Just a moment...")
    calls = _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page, _FakePage()]))
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: False
    )
    monkeypatch.setattr(
        module.CnlangSigninV3, "_sign_page_via_fetch", lambda self, context, page: ""
    )

    assert plugin._signin_by_browser() is True
    last = plugin.get_data(module.KEY_LAST_RESULT)
    assert last["success"] is False
    assert "Cloudflare" in last["content"]
    # 失败信息必须带上「怎么办」，否则用户只看到一串技术细节
    assert module.CF_ADVICE in last["content"]
    assert "cf_clearance" in last["content"]
    assert "navigator.userAgent" in last["content"]
    # 必须点明「旧版本可能污染过这个字段」，否则被改坏 UA 的用户照做仍然失败
    assert "Chrome/154" in last["content"]
    # 无头失败后必须升级到有头模式再试一次
    assert [call["headless"] for call in calls] == [True, False]


# --- 浏览器拟人化与启动参数 ---------------------------------------------------


def test_browser_launch_passes_host_humanize_settings(host, monkeypatch):
    """必须跟随宿主配置开启拟人化——这是浏览器能否通过托管挑战的关键。

    宿主自身启动浏览器时固定传 ``humanize`` / ``human_preset``；插件若省略，
    cloakbrowser 会以脚本化的固定行为运行，被 Cloudflare 判为机器人。
    """
    plugin = _enabled_plugin(host, browser_mode=True)
    page = _FakePage(html="<html>ok</html>", title="国语视界")
    calls = _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page]))
    monkeypatch.setattr(host.module.settings, "CLOAKBROWSER_HUMANIZE", True, raising=False)
    monkeypatch.setattr(host.module.settings, "CLOAKBROWSER_HUMAN_PRESET", "careful", raising=False)
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: True
    )
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_finish_browser_signin", lambda self, page, html: True
    )

    plugin._signin_by_browser()

    assert calls[0]["humanize"] is True
    assert calls[0]["human_preset"] == "careful"


def test_browser_launch_kwargs_follow_host_humanize_off(host, monkeypatch):
    """宿主关闭拟人化时不得擅自开启，避免与宿主行为不一致。"""
    plugin = _enabled_plugin(host, browser_mode=True)
    monkeypatch.setattr(host.module.settings, "CLOAKBROWSER_HUMANIZE", False, raising=False)

    assert plugin._browser_launch_kwargs(headless=True, humanize=False) == {"headless": True}


def test_browser_launch_kwargs_normalizes_string_humanize(host):
    """宿主配置可能是字符串形式的布尔值，需要归一化后再决定是否传参。"""
    plugin = _enabled_plugin(host)

    assert plugin._as_bool("false", True) is False
    assert plugin._as_bool("0", True) is False
    assert plugin._as_bool("true", False) is True
    assert plugin._as_bool(None, True) is True


def test_browser_launch_downgrades_when_humanize_unsupported(host, monkeypatch):
    """旧版浏览器实现不认拟人化参数时应自动降级重试，而不是直接失败。"""
    plugin = _enabled_plugin(host, browser_mode=True)
    seen = []

    def launcher(**kwargs):
        seen.append(kwargs)
        if "humanize" in kwargs:
            raise TypeError("launch_context() got an unexpected keyword argument 'humanize'")
        return _FakeBrowserContext()

    context = plugin._launch_browser_context(launcher, {"headless": True, "humanize": True})

    assert isinstance(context, _FakeBrowserContext)
    assert seen[0]["humanize"] is True
    assert seen[1] == {"headless": True}


def test_browser_launch_propagates_unrelated_type_error(host):
    """与拟人化无关的 TypeError 不应被吞掉，必须原样抛出。"""
    plugin = _enabled_plugin(host, browser_mode=True)

    def launcher(**kwargs):
        raise TypeError("launch_context() missing 1 required positional argument")

    with pytest.raises(TypeError):
        plugin._launch_browser_context(launcher, {"headless": True})


def test_browser_attempt_plan_escalates_headless_then_headed(host):
    """尝试顺序应为「无头拟人化 → 有头拟人化」，且预算逐级收敛。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)

    assert plugin._browser_attempt_plan() == [
        (True, True, module.CF_CHALLENGE_BUDGET),
        (False, True, module.CF_HEADED_BUDGET),
    ]


def test_browser_flow_warms_up_homepage_before_sign_page(host, monkeypatch):
    """必须先访问站点首页拿到域级 cf_clearance，再访问签到页。

    直接访问签到页等于在没有任何通行证的状态下撞上最严的那条规则，签到页会下发
    自动化浏览器过不了的交互式挑战——这是 v3.6.2~v3.6.4 一直失败的真正结构原因。
    """
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    page = _FakePage(html="<html>ok</html>", title="国语视界")
    _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page]))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: True
    )
    monkeypatch.setattr(
        module.CnlangSigninV3, "_finish_browser_signin", lambda self, page, html: True
    )

    plugin._signin_by_browser()

    assert page.goto_calls == [module.SITE_HOME_URL, module.SIGN_PAGE_URL]


def test_browser_flow_persists_cf_cookies_right_after_homepage(host, monkeypatch):
    """首页通过后必须立刻写回 Cookie，签到页与下次执行都依赖它。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    page = _FakePage(html="<html>ok</html>", title="国语视界")
    context = _FakeBrowserContext(
        pages=[page],
        cookies=[{"name": "cf_clearance", "value": "home", "domain": ".cnlang.org", "path": "/"}],
    )
    _install_browser_stub(monkeypatch, context)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: True
    )
    monkeypatch.setattr(
        module.CnlangSigninV3, "_finish_browser_signin", lambda self, page, html: True
    )

    plugin._signin_by_browser()

    assert plugin.store[module.KEY_CF_COOKIES][0]["value"] == "home"


def test_browser_flow_continues_when_homepage_warmup_fails(host, monkeypatch):
    """首页热身失败不应直接放弃，仍要尝试签到页。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    page = _FakePage(html="", title="Just a moment...")
    _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page]))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: False
    )
    monkeypatch.setattr(
        module.CnlangSigninV3, "_sign_page_via_fetch", lambda self, context, page: ""
    )

    plugin._signin_by_browser()

    assert page.goto_calls == [module.SITE_HOME_URL, module.SIGN_PAGE_URL]


def test_browser_mode_retries_headed_after_headless_challenge(host, monkeypatch):
    """无头模式被挑战、有头模式通过时应正常签到成功。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    headless_page = _FakePage(html="", title="Just a moment...")
    headed_page = _FakePage(
        html=(
            '<input name="formhash" value="ff00" />'
            '<a title="访问我的空间">tester</a>'
            "<p>您本月已累计签到:<b>3</b>"
        ),
        title="国语视界",
        user_agent="HeadedUA",
    )
    headed_page.fetch_map = {
        module.SIGN_SUBMIT_URL: '<div class="c">恭喜，签到成功</div>',
        module.CREDIT_URL: '<span id="hcredit_2">888</span>',
    }
    context = _FakeBrowserContext(pages=[headless_page, headed_page])
    calls = _install_browser_stub(monkeypatch, context)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    # 只让无头那次卡住：有头页面视为已通过
    monkeypatch.setattr(
        module.CnlangSigninV3,
        "_solve_cloudflare",
        lambda self, page, **kwargs: page is headed_page,
    )
    monkeypatch.setattr(
        module.CnlangSigninV3, "_sign_page_via_fetch", lambda self, context, page: ""
    )

    assert plugin._signin_by_browser() is True

    assert [call["headless"] for call in calls] == [True, False]
    assert calls[0]["humanize"] is True
    assert calls[1]["humanize"] is True
    last = plugin.get_data(module.KEY_LAST_RESULT)
    assert last["success"] is True
    assert last["money"] == "888"


def test_browser_mode_survives_headed_launch_failure(host, monkeypatch):
    """有头模式启动失败（如宿主无虚拟显示资源）不得吞掉无头模式的失败结论。"""
    module = host.module
    plugin = _enabled_plugin(host, browser_mode=True)
    page = _FakePage(html="", title="Just a moment...")
    module_stub = types.ModuleType("app.sdk.browser")
    calls = []

    def launcher(**kwargs):
        calls.append(kwargs)
        if kwargs.get("headless") is False:
            raise RuntimeError("host.display unavailable")
        return _FakeBrowserContext(pages=[page])

    module_stub.launch_browser_context = launcher
    monkeypatch.setitem(sys.modules, "app.sdk.browser", module_stub)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: False
    )
    monkeypatch.setattr(
        module.CnlangSigninV3, "_sign_page_via_fetch", lambda self, context, page: ""
    )

    assert plugin._signin_by_browser() is True

    last = plugin.get_data(module.KEY_LAST_RESULT)
    assert last["success"] is False
    assert "Cloudflare" in last["content"]
    assert "host.display unavailable" in last["content"]
    assert [call["headless"] for call in calls] == [True, False]


def test_inject_cookies_reuses_cached_cloudflare_cookies(host):
    """上一轮由本浏览器签发的通行证应被注入，配置里的旧值仍须剔除。"""
    module = host.module
    plugin = _enabled_plugin(
        host, cookie="sid=abc; cf_clearance=stale", browser_mode=True
    )
    plugin.store[module.KEY_CF_COOKIES] = "cf_clearance=fresh; __cf_bm=bm1"
    context = _FakeContext(add_cookies=True)
    page = _FakePage()

    plugin._inject_cookies(context, page)

    names = {item["name"]: item["value"] for item in context.jar}
    assert names["sid"] == "abc"
    assert names["cf_clearance"] == "fresh"
    assert names["__cf_bm"] == "bm1"


def test_refresh_cookies_caches_duplicate_cloudflare_names_separately(host):
    """同名不同作用域的 CF Cookie 必须各存一条，不能压成一条。"""
    module = host.module
    plugin = _enabled_plugin(host, cookie="sid=abc", browser_mode=True)
    context = _FakeContext(
        cookies=[
            {"name": "cf_clearance", "value": "root", "domain": ".cnlang.org", "path": "/"},
            {
                "name": "cf_clearance",
                "value": "scoped",
                "domain": "cnlang.org",
                "path": "/dsu_paulsign-sign.html",
            },
            {"name": "cf_chl_rc_ni", "value": "1", "domain": ".cnlang.org", "path": "/"},
        ]
    )

    plugin._refresh_cookies_from_browser(context, _FakePage())

    cached = plugin.store[module.KEY_CF_COOKIES]
    assert len(cached) == 3
    assert [item["value"] for item in cached[:2]] == ["root", "scoped"]
    assert cached[1]["path"] == "/dsu_paulsign-sign.html"


def test_finish_browser_signin_reports_challenged_submit(host):
    """签到提交返回挑战页时应给出明确原因，而不是「未获取到响应内容」。"""
    module = host.module
    plugin = _enabled_plugin(host)
    page = _FakePage()
    page.fetch_map = {module.SIGN_SUBMIT_URL: CF_CHALLENGE_BODY}
    html = '<input name="formhash" value="ff00" /><a title="访问我的空间">tester</a>'

    plugin._finish_browser_signin(page, html)

    last = plugin.get_data(module.KEY_LAST_RESULT)
    assert last["success"] is False
    assert "Cloudflare" in last["content"]


def test_finish_browser_signin_records_success(host):
    """页面上下文内取到响应内容时应正常落库成功结果。"""
    module = host.module
    plugin = _enabled_plugin(host)
    page = _FakePage()
    page.fetch_map = {
        module.SIGN_SUBMIT_URL: '<div class="c">恭喜，签到成功</div>',
        module.CREDIT_URL: '<span id="hcredit_2">321</span>',
    }
    html = (
        '<input name="formhash" value="ff00" />'
        '<a title="访问我的空间">tester</a>'
        "<p>您本月已累计签到:<b>4</b>"
    )

    plugin._finish_browser_signin(page, html)

    last = plugin.get_data(module.KEY_LAST_RESULT)
    assert last["success"] is True
    assert last["money"] == "321"
    assert last["total_signs"] == 5


def test_execute_signin_without_cookie_records_failure(host):
    """未配置 Cookie 时应直接记录失败结果，不发起任何请求。"""
    plugin = _enabled_plugin(host, cookie="", notify=True)
    result = plugin.signin()

    assert result["success"] is False
    assert "Cookie" in result["content"]
    assert plugin.get_data(host.module.KEY_LAST_RESULT)["success"] is False
    assert host.request.calls == []
    # 失败通知应带上 Cookie 更新建议
    assert "更新Cookie" in plugin.messages[0]["text"]


def test_execute_signin_success_flow(host):
    """完整签到流程应解析用户名、提交签到、读取大洋并写入历史。"""
    module = host.module
    plugin = _enabled_plugin(host, notify=True)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            200,
            '<input name="formhash" value="ff00" />'
            '<a title="访问我的空间">tester</a>'
            "<p>您本月已累计签到:<b>5</b>",
        ),
        module.SIGN_SUBMIT_URL: host.response_cls(
            200, '<div class="c">恭喜，签到成功</div>'
        ),
        module.CREDIT_URL: host.response_cls(
            200, '<span id="hcredit_2">1234</span>'
        ),
        module.HITOKOTO_URL: host.response_cls(200, "今天也要加油鸭"),
    }

    result = plugin.signin()

    assert result["success"] is True
    assert result["username"] == "tester"
    assert result["money"] == "1234"
    assert result["total_signs"] == 6

    history = plugin.get_data(module.KEY_HISTORY)
    assert len(history) == 1
    assert history[0]["money"] == "1234"
    assert history[0]["success"] is True
    assert plugin.messages[0]["mtype"] is not None


def test_execute_signin_skips_submit_when_already_signed(host):
    """站点提示今日已签到时，不应重复提交，也不应写入历史。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            200,
            '<a title="访问我的空间">tester</a>'
            "您今天已经签到过了或者签到时间还未开始",
        ),
    }

    result = plugin.signin()

    assert result["success"] is True
    assert plugin.get_data(module.KEY_HISTORY) is None
    posted = [url for method, url, _ in host.request.calls if method == "POST"]
    assert posted == []


def test_execute_signin_reports_expired_cookie(host):
    """页面无用户名时应判定 Cookie 失效。"""
    module = host.module
    plugin = _enabled_plugin(host, notify=True, notify_style="style3")
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(200, "<html>login page</html>"),
    }

    result = plugin.signin()

    assert result["success"] is False
    assert "Cookie" in result["content"]
    assert "Cookie验证失败" in plugin.messages[0]["text"]


def test_signin_ignores_other_plugin_actions(host):
    """事件动作不属于本插件时不应执行签到。"""
    plugin = _enabled_plugin(host)
    event = host.event_cls({"action": "other_plugin"})

    assert plugin.signin(event) == {}
    assert plugin.get_data(host.module.KEY_LAST_RESULT) is None


def test_signin_handles_own_action(host):
    """收到本插件动作时应执行签到。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            200,
            '<input name="formhash" value="ff00" />'
            '<a title="访问我的空间">tester</a>',
        ),
        module.SIGN_SUBMIT_URL: host.response_cls(200, '<div class="c">签到成功</div>'),
        module.CREDIT_URL: host.response_cls(200, '<span id="hcredit_2">1</span>'),
        module.HITOKOTO_URL: host.response_cls(200, "今天也要加油鸭"),
    }

    result = plugin.signin(host.event_cls({"action": module.ACTION_SIGNIN}))

    assert result["success"] is True


def test_get_status_summary_reads_money_and_usergroup(host):
    """状态摘要应正确解析大洋余额与用户组（旧实现此处存在缺陷）。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {
        module.SIGN_PAGE_URL: host.response_cls(
            200,
            '<a title="访问我的空间">tester</a>'
            "<p>您本月已累计签到:<b>9</b>"
            "您已经连续签到<b>4</b>天",
        ),
        module.CREDIT_URL: host.response_cls(200, '<span id="hcredit_2">888</span>'),
        module.USERGROUP_URL: host.response_cls(
            200, "您目前属于用户组: <strong>VIP会员</strong>"
        ),
    }

    status = plugin.get_status_summary()

    assert status["status"] == "运行中"
    assert status["account"]["username"] == "tester"
    assert status["account"]["cookie_status"] == "有效"
    assert status["account"]["money"] == "888"
    assert status["account"]["usergroup"] == "VIP会员"
    assert status["month_signs"] == 9
    assert status["continuous_days"] == 4
    assert status["next_sign_time"] != "未设置"


def test_get_status_summary_without_cookie_makes_no_request(host):
    """未配置 Cookie 时状态摘要只返回本地数据，不发起请求。"""
    plugin = _enabled_plugin(host, cookie="")

    status = plugin.get_status_summary()

    assert status["account"]["cookie_status"] == "无效"
    assert host.request.calls == []


# ---------------------------------------------------------------------------
# API 与页面
# ---------------------------------------------------------------------------


def test_get_api_declares_expected_routes(host):
    """get_api 应声明 4 个带 bear 鉴权的接口。"""
    plugin = _enabled_plugin(host)
    routes = plugin.get_api()

    assert {route["path"] for route in routes} == {
        "/status",
        "/history",
        "/signin",
        "/history/clear",
    }
    for route in routes:
        assert route["auth"] == "bear"
        assert route["methods"]
        assert callable(route["endpoint"])


def test_api_history_returns_stats(host):
    """历史接口应返回按时间倒序的明细与统计结果。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin.save_data(
        module.KEY_HISTORY,
        [_record("2026-09-01 07:00:00"), _record("2026-09-02 07:00:00")],
    )

    payload = plugin.api_history()

    assert payload["history"][0]["date"] == "2026-09-02 07:00:00"
    assert payload["stats"]["total_days"] == 2


def test_api_clear_history(host):
    """清空历史接口应同时清除历史与最近结果。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin.save_data(module.KEY_HISTORY, [_record("2026-09-01 07:00:00")])
    plugin.save_data(module.KEY_LAST_RESULT, {"success": True})

    payload = plugin.api_clear_history()

    assert payload["success"] is True
    assert plugin.get_data(module.KEY_HISTORY) is None
    assert plugin.get_data(module.KEY_LAST_RESULT) is None


def test_get_page_returns_three_cards(host):
    """详情页应返回账号信息、签到状态、统计三张卡片。"""
    plugin = _enabled_plugin(host, cookie="")

    page = plugin.get_page()

    assert len(page) == 3
    assert all(card["component"] == "VCard" for card in page)


def test_get_form_returns_defaults(host):
    """配置页应返回页面配置与完整默认模型。"""
    plugin = _enabled_plugin(host)
    form, defaults = plugin.get_form()

    assert form[0]["component"] == "VForm"
    assert defaults["cron"] == "0 7 * * *"
    assert defaults["notify_style"] == "style1"
    assert defaults["enabled"] is False


def test_get_form_exposes_anti_bot_settings(host):
    """配置页应包含浏览器模式开关与 UA 输入框，并带出可用的默认值。"""
    plugin = _enabled_plugin(host)
    form, defaults = plugin.get_form()

    serialized = json.dumps(form, ensure_ascii=False)
    assert "browser_mode" in serialized
    assert "user_agent" in serialized
    assert defaults["browser_mode"] is True
    assert defaults["user_agent"] == ""


def test_get_command_registers_remote_action(host):
    """远程命令应注册为 PluginAction 并携带本插件动作标识。"""
    commands = host.module.CnlangSigninV3.get_command()

    assert len(commands) == 1
    assert commands[0]["cmd"] == "/cnlang_signin"
    assert commands[0]["data"]["action"] == host.module.ACTION_SIGNIN
    assert commands[0]["event"] is host.event_type.PluginAction


def test_module_import_has_no_side_effects(host):
    """导入插件模块不应发起请求或登记调度任务。"""
    assert host.request.calls == []
    assert host.scheduler.added == []


# ---------------------------------------------------------------------------
# curl_cffi 快速路径
# ---------------------------------------------------------------------------


class _StubCurlResponse:
    """模拟 curl_cffi 响应对象（status_code / text / headers 与 requests 一致）。"""

    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = {} if headers is None else dict(headers)


class _StubCurlRequests:
    """模拟 ``curl_cffi.requests`` 模块，记录每次调用的参数。"""

    calls = []
    routes = {}
    error = None

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.routes = {}
        cls.error = None

    @classmethod
    def _call(cls, method, url, **kwargs):
        cls.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if cls.error is not None:
            raise cls.error
        return cls.routes.get(url)

    @classmethod
    def get(cls, url, **kwargs):
        return cls._call("GET", url, **kwargs)

    @classmethod
    def post(cls, url, **kwargs):
        return cls._call("POST", url, **kwargs)


def _install_curl_cffi(monkeypatch, *, available=True, targets=None):
    """把 curl_cffi 桩模块装入 ``sys.modules``，返回记录调用的桩类。

    :param available: False 表示模拟「宿主未安装 curl_cffi」
    :param targets: 模拟 curl_cffi 支持的 impersonate 目标列表
    :return: ``_StubCurlRequests``
    """
    _StubCurlRequests.reset()
    if not available:
        # sys.modules 中值为 None 时，import 会直接抛 ImportError
        monkeypatch.setitem(sys.modules, "curl_cffi", None)
        return _StubCurlRequests

    class _Item:
        def __init__(self, value):
            self.value = value

    imp_mod = types.ModuleType("curl_cffi.requests.impersonate")
    imp_mod.BrowserType = [_Item(v) for v in (targets or ["chrome131", "chrome136"])]

    req_mod = types.ModuleType("curl_cffi.requests")
    req_mod.get = _StubCurlRequests.get
    req_mod.post = _StubCurlRequests.post
    req_mod.impersonate = imp_mod

    pkg = types.ModuleType("curl_cffi")
    pkg.requests = req_mod

    monkeypatch.setitem(sys.modules, "curl_cffi", pkg)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", req_mod)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests.impersonate", imp_mod)
    return _StubCurlRequests


CHROME_131_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def test_resolve_impersonate_auto_matches_ua_chrome_version(host, monkeypatch):
    """auto 模式应挑一个版本号不超过 UA 里 Chrome 主版本的受支持目标。"""
    _install_curl_cffi(monkeypatch, targets=["chrome124", "chrome131", "chrome136"])
    resolve = host.module._resolve_impersonate

    assert resolve("auto", CHROME_131_UA) == "chrome131"
    # 140 没有精确匹配，应退到不超过它的最大值
    assert resolve("auto", CHROME_131_UA.replace("/131.", "/140.")) == "chrome136"
    # 比所有已知目标都旧时退回默认目标
    assert resolve("auto", CHROME_131_UA.replace("/131.", "/120.")) == "chrome"


def test_resolve_impersonate_explicit_and_disabled(host, monkeypatch):
    """显式目标原样返回；空串/None 表示关闭 curl_cffi。"""
    _install_curl_cffi(monkeypatch)
    resolve = host.module._resolve_impersonate

    assert resolve("chrome136", CHROME_131_UA) == "chrome136"
    assert resolve("", CHROME_131_UA) is None
    assert resolve(None, CHROME_131_UA) is None
    # UA 里没有 Chrome 版本号时退回默认目标
    assert resolve("auto", "Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0") == "chrome"


def test_request_uses_curl_cffi_when_available(host, monkeypatch):
    """安装了 curl_cffi 时应优先用它发请求，并带上 impersonate 与请求头。"""
    stub = _install_curl_cffi(monkeypatch, targets=["chrome131"])
    plugin = _enabled_plugin(host, user_agent=CHROME_131_UA)
    stub.routes[host.module.SIGN_PAGE_URL] = _StubCurlResponse(200, "<html>ok</html>")

    text, status, blocked = plugin._request(
        host.module.SIGN_PAGE_URL, headers={"Cookie": "a=1"}
    )

    assert (text, status, blocked) == ("<html>ok</html>", 200, False)
    assert len(stub.calls) == 1
    assert stub.calls[0]["method"] == "GET"
    assert stub.calls[0]["kwargs"]["impersonate"] == "chrome131"
    assert stub.calls[0]["kwargs"]["headers"] == {"Cookie": "a=1"}
    assert host.request.calls == []  # 不应回退到宿主网络组件


def test_request_falls_back_when_curl_cffi_raises(host, monkeypatch):
    """curl_cffi 抛异常时必须静默回退宿主 RequestUtils。"""
    stub = _install_curl_cffi(monkeypatch, targets=["chrome131"])
    stub.error = RuntimeError("boom")
    plugin = _enabled_plugin(host)
    host.request.routes[host.module.SIGN_PAGE_URL] = host.response_cls(200, "<html>ok</html>")

    text, status, blocked = plugin._request(host.module.SIGN_PAGE_URL)

    assert (text, status, blocked) == ("<html>ok</html>", 200, False)
    assert host.request.calls  # 回退确实发生了


def test_request_skips_curl_cffi_when_disabled(host, monkeypatch):
    """关闭 use_curl_cffi 后不得调用 curl_cffi。"""
    stub = _install_curl_cffi(monkeypatch, targets=["chrome131"])
    plugin = _enabled_plugin(host, use_curl_cffi=False)
    host.request.routes[host.module.SIGN_PAGE_URL] = host.response_cls(200, "ok")

    plugin._request(host.module.SIGN_PAGE_URL)

    assert stub.calls == []
    assert host.request.calls


def test_request_skips_curl_cffi_when_impersonate_blank(host, monkeypatch):
    """impersonate 留空等同于关闭 curl_cffi。"""
    stub = _install_curl_cffi(monkeypatch, targets=["chrome131"])
    plugin = _enabled_plugin(host, impersonate="")
    host.request.routes[host.module.SIGN_PAGE_URL] = host.response_cls(200, "ok")

    plugin._request(host.module.SIGN_PAGE_URL)

    assert stub.calls == []
    assert host.request.calls


def test_request_works_without_curl_cffi_installed(host, monkeypatch):
    """宿主未安装 curl_cffi 时快速路径照常可用。"""
    _install_curl_cffi(monkeypatch, available=False)
    plugin = _enabled_plugin(host)
    host.request.routes[host.module.SIGN_PAGE_URL] = host.response_cls(200, "ok")

    text, status, _blocked = plugin._request(host.module.SIGN_PAGE_URL)

    assert (text, status) == ("ok", 200)


def test_request_via_curl_cffi_still_detects_challenge(host, monkeypatch):
    """经 curl_cffi 拿到的挑战页同样要能被识别并触发浏览器模式切换。"""
    stub = _install_curl_cffi(monkeypatch, targets=["chrome131"])
    plugin = _enabled_plugin(host)
    stub.routes[host.module.SIGN_PAGE_URL] = _StubCurlResponse(
        403, CF_CHALLENGE_BODY, headers=CF_CHALLENGE_HEADERS
    )

    text, status, blocked = plugin._request(host.module.SIGN_PAGE_URL)

    assert (text, status, blocked) == (None, 403, True)


def test_request_via_curl_cffi_posts_form_data(host, monkeypatch):
    """curl_cffi 路径下的签到提交必须是 POST 且携带表单数据。"""
    stub = _install_curl_cffi(monkeypatch, targets=["chrome131"])
    plugin = _enabled_plugin(host)
    stub.routes[host.module.SIGN_SUBMIT_URL] = _StubCurlResponse(200, "ok")

    text, status, _blocked = plugin._request(
        host.module.SIGN_SUBMIT_URL, data={"formhash": "abc"}
    )

    assert (text, status) == ("ok", 200)
    assert stub.calls[0]["method"] == "POST"
    assert stub.calls[0]["kwargs"]["data"] == {"formhash": "abc"}


# ---------------------------------------------------------------------------
# 强制浏览器模式
# ---------------------------------------------------------------------------


def test_force_browser_skips_fast_path(host, monkeypatch):
    """开启「强制浏览器模式」时不应发出任何 HTTP 请求。"""
    plugin = _enabled_plugin(host, force_browser=True, browser_mode=True)
    page = _FakePage(html="", title="Just a moment...")
    calls = _install_browser_stub(monkeypatch, _FakeBrowserContext(pages=[page]))
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_solve_cloudflare", lambda self, page, **kwargs: False
    )
    monkeypatch.setattr(
        host.module.CnlangSigninV3, "_sign_page_via_fetch", lambda self, context, page: ""
    )

    plugin.signin()

    assert calls, "应当直接进入浏览器模式"
    assert host.request.calls == [], "强制浏览器模式不得走 HTTP 快速路径"


def test_force_browser_without_browser_mode_still_advises(host):
    """同时关闭浏览器模式时，强制开关也不应让流程崩溃。"""
    plugin = _enabled_plugin(host, force_browser=True, browser_mode=False)
    host.request.routes[host.module.SIGN_PAGE_URL] = host.response_cls(
        403, CF_CHALLENGE_BODY, headers=CF_CHALLENGE_HEADERS
    )

    result = plugin.signin()

    assert result["success"] is False
    assert result["content"] == host.module.CF_ADVICE


def test_force_browser_still_requires_cookie(host):
    """未配置 Cookie 时应优先报「未配置Cookie」，而不是去开浏览器。"""
    plugin = _enabled_plugin(host, force_browser=True, cookie="")

    result = plugin.signin()

    assert result["success"] is False
    assert result["content"] == "未配置Cookie"


# ---------------------------------------------------------------------------
# 新增配置项的生命周期
# ---------------------------------------------------------------------------


def test_save_config_preserves_new_anti_bot_fields(host):
    """新增配置项必须参与回写，否则保存配置时会被静默清空。"""
    plugin = _enabled_plugin(
        host, force_browser=True, use_curl_cffi=False, impersonate="chrome136"
    )

    plugin._save_config()
    saved = plugin.get_config()

    assert saved["force_browser"] is True
    assert saved["use_curl_cffi"] is False
    assert saved["impersonate"] == "chrome136"


def test_get_form_exposes_curl_cffi_settings(host):
    """配置页应包含 curl_cffi 开关、伪装目标与强制浏览器模式开关。"""
    plugin = _enabled_plugin(host)
    form, defaults = plugin.get_form()
    serialized = json.dumps(form, ensure_ascii=False)

    assert "use_curl_cffi" in serialized
    assert "impersonate" in serialized
    assert "force_browser" in serialized
    assert defaults["use_curl_cffi"] is True
    assert defaults["impersonate"] == "auto"
    assert defaults["force_browser"] is False


# ---------------------------------------------------------------------------
# Cloudflare 通行证有效期观测
# ---------------------------------------------------------------------------


def test_split_cookie_header_keeps_order_and_skips_noise(host):
    """Cookie 头解析应保持顺序，并跳过没有 '=' 的片段。"""
    module = host.module
    assert module._split_cookie_header("  a=1 ; ; b=2; c=3 ") == [
        ("a", "1"),
        ("b", "2"),
        ("c", "3"),
    ]
    assert module._split_cookie_header(None) == []
    assert module._split_cookie_header("noequals") == []


def test_cf_clearance_values_returns_every_scope(host):
    """同名 cf_clearance 可能有多条（作用域不同），必须全部取出。"""
    module = host.module
    header = "cf_clearance=one; 3rir_2132_sid=x; cf_clearance=two"
    assert module._cf_clearance_values(header) == ["one", "two"]
    assert module._cf_clearance_values("a=1") == []


@pytest.mark.parametrize(
    "value, expected",
    [
        ("abc-1759900000-1-deadbeef", 1759900000),
        ("1759900000", 1759900000),
        ("abc-1-deadbeef", None),  # 没有 10 位段
        ("abc-12345678901234-x", None),  # 位数不对
        ("abc-9999999999-x", None),  # 超出上界
        ("abc-1000000000-x", None),  # 低于下界
        ("deadbeef", None),
        ("", None),
    ],
)
def test_embedded_cf_timestamp_only_accepts_plausible_stamps(host, value, expected):
    """只接受「整段 10 位数字且落在合理区间」的时间戳，避免误判随机 hex。"""
    assert host.module._embedded_cf_timestamp(value) == expected


def test_humanize_seconds_formats_units(host):
    """秒数应格式化为可读文本，且不出现 '0 分钟' 这类冗余单位。"""
    humanize = host.module.CnlangSigninV3._humanize_seconds
    assert humanize(45) == "45 秒"
    assert humanize(90) == "1 分钟"
    assert humanize(7200) == "2 小时"
    assert humanize(3720) == "1 小时 2 分钟"
    assert humanize(86400 * 2 + 3600 * 3) == "2 天 3 小时"
    assert humanize(86400 * 2) == "2 天"
    assert humanize(-5) == "0 秒"  # 负数被夹到 0，不显示「-5 秒」
    assert humanize("bad") == "bad"


def test_set_cookie_values_prefers_get_list(host):
    """优先使用 get_list（httpx 形态），否则回退到 get（requests 形态）。"""
    module = host.module

    class _HeadersWithList:
        def get_list(self, name):
            assert name == "set-cookie"
            return ["a=1", "b=2"]

        def get(self, name):  # pragma: no cover - 不应被调用
            raise AssertionError("get_list 可用时不应回退到 get")

    assert module._set_cookie_values(_HeadersWithList()) == ["a=1", "b=2"]
    assert module._set_cookie_values({"Set-Cookie": "cf_clearance=x"}) == ["cf_clearance=x"]
    assert module._set_cookie_values(None) == []
    assert module._set_cookie_values({}) == []


def test_parse_http_date_handles_rfc1123(host):
    """HTTP 日期应按 UTC 解析为 Unix 秒。"""
    module = host.module
    expected = int(datetime(2026, 10, 21, 7, 28, tzinfo=timezone.utc).timestamp())
    assert module._parse_http_date("Wed, 21 Oct 2026 07:28:00 GMT") == expected
    assert module._parse_http_date("not a date") is None
    assert module._parse_http_date("") is None


def test_observe_cf_expiry_records_max_age(host):
    """站点下发 Set-Cookie 时应记录 Max-Age 推出的到期时间。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin._observe_cf_expiry({"Set-Cookie": "cf_clearance=abc; Path=/; Max-Age=1800"})

    state = plugin.store[module.KEY_CF_EXPIRY]
    assert state["source"] == "response"
    assert state["expires_at"] - state["observed_at"] == 1800


def test_observe_cf_expiry_parses_expires_attribute(host):
    """没有 Max-Age 时应回退解析 Expires。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin._observe_cf_expiry(
        {"Set-Cookie": "cf_clearance=abc; Expires=Wed, 21 Oct 2026 07:28:00 GMT"}
    )
    expected = int(datetime(2026, 10, 21, 7, 28, tzinfo=timezone.utc).timestamp())
    assert plugin.store[module.KEY_CF_EXPIRY]["expires_at"] == expected


def test_observe_cf_expiry_ignores_other_cookies(host):
    """非 cf_clearance 的 Cookie 不应被记录。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin._observe_cf_expiry({"Set-Cookie": "3rir_2132_sid=abc; Max-Age=3600"})
    assert module.KEY_CF_EXPIRY not in plugin.store


def test_observe_cf_expiry_skips_cookie_without_expiry(host):
    """会话级 Cookie（没有 Max-Age / Expires）不应编造到期时间。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin._observe_cf_expiry({"Set-Cookie": "cf_clearance=abc; Path=/"})
    assert module.KEY_CF_EXPIRY not in plugin.store


def test_observe_browser_cf_expiry_records_zone_ttl(host):
    """浏览器签发的通行证应记录为 browser 来源。"""
    module = host.module
    plugin = _enabled_plugin(host)
    now = int(time.time())
    plugin._observe_browser_cf_expiry(now + 1800)

    state = plugin.store[module.KEY_CF_EXPIRY]
    assert state["source"] == "browser"
    assert state["expires_at"] == now + 1800


def test_observe_browser_cf_expiry_ignores_session_cookie(host):
    """expires <= 0（会话级）或缺失时不应记录。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin._observe_browser_cf_expiry(0)
    plugin._observe_browser_cf_expiry(-1)
    plugin._observe_browser_cf_expiry(None)
    assert module.KEY_CF_EXPIRY not in plugin.store


def test_cf_status_reports_observed_expiry(host):
    """站点下发过到期时间时，应报告精确的剩余时长。"""
    module = host.module
    plugin = _enabled_plugin(host)
    now = int(time.time())
    plugin._cf_expiry = {"expires_at": now + 1800, "observed_at": now, "source": "response"}

    text = plugin._cf_clearance_status()
    assert "有效期至" in text
    assert "30 分钟" in text


def test_cf_status_reports_expired_observation(host):
    """已过期的观测应提示重新复制 Cookie。"""
    module = host.module
    plugin = _enabled_plugin(host)
    now = int(time.time())
    plugin._cf_expiry = {"expires_at": now - 600, "observed_at": now - 2400, "source": "response"}

    text = plugin._cf_clearance_status()
    assert "过期" in text
    assert "重新复制 Cookie" in text


def test_cf_status_uses_embedded_timestamp_as_expiry(host):
    """值内嵌的时间戳在未来时，应判定为到期时间。"""
    module = host.module
    now = int(time.time())
    plugin = _enabled_plugin(host, cookie=f"cf_clearance=abc-{now + 3600}-1-x; b=2")
    plugin._cf_expiry = {}

    text = plugin._cf_clearance_status()
    assert "内嵌时间戳" in text
    assert "到期" in text
    assert "1 小时" in text


def test_cf_status_uses_embedded_timestamp_as_issue_time(host):
    """值内嵌的时间戳在过去时（签到仍成功），应判定为签发时间。"""
    module = host.module
    now = int(time.time())
    plugin = _enabled_plugin(host, cookie=f"cf_clearance=abc-{now - 3600}-1-x")
    plugin._cf_expiry = {}

    text = plugin._cf_clearance_status()
    assert "签发" in text


def test_cf_status_is_honest_when_nothing_available(host):
    """既没有站点下发、值里也没有时间戳时，必须明说无法确定而不是编造日期。"""
    module = host.module
    plugin = _enabled_plugin(host, cookie="cf_clearance=opaquevalue; b=2")
    plugin._cf_expiry = {}

    text = plugin._cf_clearance_status()
    assert "无法确定" in text
    assert str(module.CF_DEFAULT_TTL_MINUTES) in text


def test_cf_status_reports_missing_cookie(host):
    """Cookie 里没有 cf_clearance 时应明确指出。"""
    module = host.module
    plugin = _enabled_plugin(host, cookie="b=2")
    plugin._cf_expiry = {}

    assert "未在配置的 Cookie 中找到" in plugin._cf_clearance_status()


def test_cf_status_keeps_browser_ttl_separate(host):
    """浏览器观测到的是站点级 TTL，不能冒充这份 Cookie 的到期时间。"""
    module = host.module
    now = int(time.time())
    plugin = _enabled_plugin(host, cookie="cf_clearance=opaquevalue")
    plugin._cf_expiry = {"expires_at": now + 1800, "observed_at": now, "source": "browser"}

    text = plugin._cf_clearance_status()
    assert "无法确定" in text
    assert "站点通行证有效期实测约 30 分钟" in text


def test_cf_survival_note_uses_last_success(host):
    """存活时长应基于上一次成功签到的时间。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin.save_data(module.KEY_LAST_SUCCESS, int(time.time()) - 7200)

    note = plugin._cf_survival_note()
    assert "上次成功签到" in note
    assert "2 小时" in note


def test_cf_survival_note_empty_without_record(host):
    """没有成功记录时不显示这一行。"""
    plugin = _enabled_plugin(host)
    assert plugin._cf_survival_note() == ""


def test_request_observes_cf_expiry_from_response(host):
    """请求路径应把响应里的 Set-Cookie 交给观测逻辑。"""
    module = host.module
    plugin = _enabled_plugin(host)
    _StubRequestUtils.routes = {
        module.SIGN_PAGE_URL: _StubResponse(
            status_code=200,
            text="ok",
            headers={"Set-Cookie": "cf_clearance=abc; Max-Age=1800"},
        )
    }

    body, status, blocked = plugin._request(
        module.SIGN_PAGE_URL, headers={"Cookie": "a=1"}
    )
    assert (body, status, blocked) == ("ok", 200, False)
    assert plugin.store[module.KEY_CF_EXPIRY]["source"] == "response"


def test_record_success_includes_cf_status_and_marks_last_success(host):
    """签到成功的通知里要带上通行证有效期，并记下成功时间。"""
    module = host.module
    plugin = _enabled_plugin(host)
    now = int(time.time())
    plugin._cf_expiry = {"expires_at": now + 1800, "observed_at": now, "source": "response"}

    result = plugin._record_success(
        username="tester", total_signs=3, money="120", content="签到成功"
    )

    assert "Cloudflare 通行证" in result["message"]
    assert "有效期至" in result["message"]
    assert module.KEY_LAST_SUCCESS in plugin.store


def test_record_failure_appends_survival_note_for_cloudflare(host):
    """被 Cloudflare 拦截时，失败信息要带上「上次成功 + 存活时长」。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin.save_data(module.KEY_LAST_SUCCESS, int(time.time()) - 3600)

    result = plugin._record_failure("浏览器模式未通过 Cloudflare 人机验证，未能完成签到")

    assert "上次成功签到" in result["content"]
    assert "1 小时" in result["content"]


def test_record_failure_leaves_other_reasons_untouched(host):
    """非 Cloudflare 的失败原因不应被追加存活时长。"""
    module = host.module
    plugin = _enabled_plugin(host)
    plugin.save_data(module.KEY_LAST_SUCCESS, int(time.time()) - 3600)

    result = plugin._record_failure("未配置Cookie")

    assert result["content"] == "未配置Cookie"


# ---------------------------------------------------------------------------
# 自动登录：Discuz 登录页解析与 Cookie 合并
# ---------------------------------------------------------------------------

# 取自 cnlang.org 登录页的真实结构（已按测试需要精简）
LOGIN_PAGE_SNIPPET = (
    '<form method="post" autocomplete="off" name="login" id="loginform_LDr3Z" class="cl" '
    "onsubmit=\"pwmd5('password3_LDr3Z');pwdclear = 1;\" "
    'action="member.php?mod=logging&amp;action=login&amp;loginsubmit=yes&amp;loginhash=LDr3Z">'
    '<input type="hidden" name="formhash" value="1cdb3aa6" />'
    '<input type="hidden" name="referer" value="https://cnlang.org/./" />'
    '<input type="text" name="username" id="username_LDr3Z" />'
    '<input type="password" id="password3_LDr3Z" name="password" />'
    '<span id="seccode_cSB1R7X1"></span>'
    '<script type="text/javascript" reload="1">'
    "updateseccode('cSB1R7X1', '<div class=\"rfm\">...</div>');</script>"
    "</form>"
)

# misc.php?mod=seccode&action=update 返回的脚本片段（真实结构）
SECCODE_UPDATE_SNIPPET = (
    "if($('seccode_cSDb5rzV')) { var sectpl = seccheck_tpl['cSDb5rzV']; "
    "var string = '<input name=\"seccodehash\" type=\"hidden\" value=\"cSDb5rzV\" />' + "
    "'<img id=\"seccodeimg\" src=\"misc.php?mod=seccode&update=72232&idhash=cSDb5rzV\" />'; }"
)


class _FakeCookies:
    """模拟会话 Cookie 容器（提供 get_dict）。"""

    def __init__(self, data=None):
        self._data = dict(data or {})

    def get_dict(self):
        return dict(self._data)

    def update(self, data):
        self._data.update(data)


class _FakeHttpResponse:
    """模拟会话请求返回的响应对象（带 text / content）。"""

    def __init__(self, text="", content=b"", status_code=200):
        self.text = text
        self.content = content
        self.status_code = status_code
        self.headers = {}


class _FakeSession:
    """按 URL 关键字返回预置响应的假会话，并记录每次调用。"""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.cookies = _FakeCookies()
        self.closed = False

    def _dispatch(self, method, url, kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        for (want_method, key), value in self.routes.items():
            if want_method == method and key in url:
                return value(url, kwargs) if callable(value) else value
        return None

    def get(self, url, **kwargs):
        return self._dispatch("get", url, kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("post", url, kwargs)

    def close(self):
        self.closed = True


def test_md5_hex_matches_discuz_pwmd5(host):
    """Discuz 用 pwmd5() 提交密码，插件必须算出同样的摘要。"""
    module = host.module

    assert module._md5_hex("secret") == "5ebe2294ecd0e0f08eab7690d2a6ee69"
    assert module._md5_hex("") == "d41d8cd98f00b204e9800998ecf8427e"


def test_parse_login_form_extracts_hashes(host):
    """从登录页提取 formhash / loginhash / 验证码 idhash。"""
    module = host.module

    parsed = module._parse_login_form(LOGIN_PAGE_SNIPPET)

    assert parsed == {
        "formhash": "1cdb3aa6",
        "loginhash": "LDr3Z",
        "seccodehash": "cSB1R7X1",
    }


@pytest.mark.parametrize(
    "html",
    ["", "<html>没有登录表单</html>", '<form id="loginform_X"></form>'],
)
def test_parse_login_form_returns_none_when_structure_missing(host, html):
    """页面结构不符合预期时必须返回 None，而不是抛出或返回半个结果。"""
    assert host.module._parse_login_form(html) is None


def test_parse_seccode_image_url_extracts_src(host):
    """验证码图片地址藏在 action=update 返回的脚本里。"""
    module = host.module

    url = module._parse_seccode_image_url(SECCODE_UPDATE_SNIPPET)

    assert url == "misc.php?mod=seccode&update=72232&idhash=cSDb5rzV"


def test_parse_seccode_image_url_returns_none_for_empty(host):
    """空脚本不应崩溃。"""
    assert host.module._parse_seccode_image_url("") is None


def test_login_error_message_extracts_discuz_error(host):
    """Discuz 用 errorhandle_ 回传失败文案。"""
    module = host.module

    body = "<root><![CDATA[抱歉，验证码填写错误<script>errorhandle_('抱歉，验证码填写错误', {})</script>]]></root>"

    assert module._login_error_message(body) == "抱歉，验证码填写错误"


def test_login_error_message_is_none_on_success(host):
    """成功响应里没有 errorhandle_。"""
    module = host.module

    assert module._login_error_message("<root><![CDATA[succeedhandle_login('...')]]></root>") is None
    assert module._login_error_message("") is None


def test_cookies_to_dict_prefers_get_dict(host):
    """会话 Cookie 优先用 get_dict() 读取。"""
    module = host.module

    assert module._cookies_to_dict(_FakeCookies({"a": "1", "b": "2"})) == {"a": "1", "b": "2"}


def test_cookies_to_dict_falls_back_to_jar(host):
    """没有 get_dict 时退回遍历 jar，兼容不同客户端。"""
    module = host.module

    class _Item:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class _JarOnly:
        jar = [_Item("sid", "abc"), _Item("auth", "xyz")]

    assert module._cookies_to_dict(_JarOnly()) == {"sid": "abc", "auth": "xyz"}
    assert module._cookies_to_dict(None) == {}


def test_is_logged_in_cookies_detects_auth_suffix(host):
    """Discuz 的登录凭证是 <表前缀>_auth，前缀随站点变化，因此只认后缀。"""
    module = host.module

    assert module._is_logged_in_cookies({"3rir_2132_auth": "x"})
    assert not module._is_logged_in_cookies({"3rir_2132_sid": "x"})
    assert not module._is_logged_in_cookies({})


def test_merge_cookie_header_preserves_cf_clearance(host):
    """合并新 Cookie 时必须保住 cf_clearance——登录接口不会重新下发它。

    丢掉它等于把已经通过的 Cloudflare 验证一并丢弃，是这版最容易踩的坑。
    """
    module = host.module
    existing = "cf_clearance=ABC123; 3rir_2132_sid=old; 3rir_2132_saltkey=oldkey"

    merged = module._merge_cookie_header(existing, {"3rir_2132_auth": "newauth"})

    assert "cf_clearance=ABC123" in merged
    assert "3rir_2132_auth=newauth" in merged
    assert "3rir_2132_sid=old" in merged


def test_merge_cookie_header_overrides_same_name(host):
    """同名项以新登录结果为准。"""
    module = host.module

    merged = module._merge_cookie_header(
        "3rir_2132_sid=old; other=keep", {"3rir_2132_sid": "new"}
    )

    assert "3rir_2132_sid=new" in merged
    assert "3rir_2132_sid=old" not in merged
    assert "other=keep" in merged


def test_merge_cookie_header_handles_empty_existing(host):
    """没有旧 Cookie 时也能正常工作。"""
    module = host.module

    assert module._merge_cookie_header(None, {"a": "1"}) == "a=1"
    assert module._merge_cookie_header("", {}) == ""


# ---------------------------------------------------------------------------
# 自动登录：验证码识别
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("eBG4", "eBG4"),
        (" eBG4 ", "eBG4"),
        ("eB-G4", "eBG4"),
        ("eBG4\n", "eBG4"),
    ],
)
def test_normalize_captcha_accepts_four_chars(host, raw, expected):
    """识别结果去掉噪声后仍是 4 位即可用。"""
    assert host.module.CnlangSigninV3._normalize_captcha(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "abcde", "------", None])
def test_normalize_captcha_rejects_wrong_length(host, raw):
    """长度不对宁可判失败重试，也不要把明显错误的答案提交上去。"""
    assert host.module.CnlangSigninV3._normalize_captcha(raw) is None


def test_solve_captcha_auto_prefers_local_then_service(host, monkeypatch):
    """auto 模式：本地优先，本地失败才用在线服务。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(plugin, "_solve_captcha_local", lambda image: None)
    monkeypatch.setattr(plugin, "_solve_captcha_service", lambda image: "SVC1")

    assert plugin._solve_captcha(b"img") == "SVC1"


def test_solve_captcha_auto_uses_local_when_it_works(host, monkeypatch):
    """auto 模式：本地成功就不该再花钱调在线服务。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(plugin, "_solve_captcha_local", lambda image: "LOC1")
    called = []
    monkeypatch.setattr(
        plugin, "_solve_captcha_service", lambda image: called.append(1) or "SVC1"
    )

    assert plugin._solve_captcha(b"img") == "LOC1"
    assert called == []


def test_solve_captcha_local_only_never_calls_service(host, monkeypatch):
    """local 模式：本地失败直接放弃，不碰在线服务。"""
    plugin = _enabled_plugin(host, captcha_mode="local")
    monkeypatch.setattr(plugin, "_solve_captcha_local", lambda image: None)
    called = []
    monkeypatch.setattr(plugin, "_solve_captcha_service", lambda image: called.append(1))

    assert plugin._solve_captcha(b"img") is None
    assert called == []


def test_solve_captcha_service_only_skips_local(host, monkeypatch):
    """service 模式：不加载本地引擎。"""
    plugin = _enabled_plugin(host, captcha_mode="service")
    called = []
    monkeypatch.setattr(plugin, "_solve_captcha_local", lambda image: called.append(1))
    monkeypatch.setattr(plugin, "_solve_captcha_service", lambda image: "SVC1")

    assert plugin._solve_captcha(b"img") == "SVC1"
    assert called == []


def test_solve_captcha_returns_none_for_empty_image(host):
    """没有图片就没有识别。"""
    assert _enabled_plugin(host)._solve_captcha(b"") is None


def test_solve_captcha_auto_escalates_to_service_after_rejection(host, monkeypatch):
    """auto 模式：本地答案被站点拒过之后，在线服务应当被优先尝试。

    本地模型可能给出「格式合法但内容错误」的答案，这一层看不出来；只有站点会说错。
    若不升级，重试只是在重复同一个错误答案，付费兜底永远走不到。
    """
    plugin = _enabled_plugin(host)
    plugin._captcha_escalated = True
    order = []
    monkeypatch.setattr(
        plugin, "_solve_captcha_local", lambda image: order.append("local") or "LOC1"
    )
    monkeypatch.setattr(
        plugin, "_solve_captcha_service", lambda image: order.append("service") or "SVC1"
    )

    assert plugin._solve_captcha(b"img") == "SVC1"
    assert order == ["service"]


def test_solve_captcha_auto_escalated_falls_back_to_local_without_key(host, monkeypatch):
    """升级后若在线服务不可用（无密钥），仍要回到本地，不能直接放弃。"""
    plugin = _enabled_plugin(host)
    plugin._captcha_escalated = True
    order = []
    monkeypatch.setattr(
        plugin, "_solve_captcha_local", lambda image: order.append("local") or "LOC1"
    )
    monkeypatch.setattr(
        plugin, "_solve_captcha_service", lambda image: order.append("service") or None
    )

    assert plugin._solve_captcha(b"img") == "LOC1"
    assert order == ["service", "local"]


def test_solve_captcha_local_mode_ignores_escalation(host, monkeypatch):
    """local 模式是用户明确「只用本地」，升级标记不该让它去调在线服务。"""
    plugin = _enabled_plugin(host, captcha_mode="local")
    plugin._captcha_escalated = True
    called = []
    monkeypatch.setattr(plugin, "_solve_captcha_local", lambda image: "LOC1")
    monkeypatch.setattr(
        plugin, "_solve_captcha_service", lambda image: called.append(1) or "SVC1"
    )

    assert plugin._solve_captcha(b"img") == "LOC1"
    assert called == []


@pytest.mark.parametrize(
    "message,expected",
    [
        ("抱歉，验证码填写错误", True),
        ("验证码错误", True),
        ("seccode invalid", True),
        ("抱歉，密码错误", False),
        ("您所在的用户组不允许登录", False),
        ("", False),
    ],
)
def test_is_captcha_error_classification(host, message, expected):
    """只有验证码类错误才值得换识别方式重试，账号密码错重试没有意义。"""
    assert host.module._is_captcha_error(message) is expected


def test_solve_captcha_service_requires_key(host):
    """未配置密钥时不应发起在线请求。"""
    plugin = _enabled_plugin(host, captcha_mode="service", captcha_key="")

    assert plugin._solve_captcha_service(b"img") is None


def test_solve_captcha_service_returns_solution_text(host, monkeypatch):
    """在线打码：createTask -> getTaskResult(status=ready) -> solution.text。"""
    plugin = _enabled_plugin(
        host, captcha_mode="service", captcha_key="KEY", captcha_service="yescaptcha"
    )
    responses = [
        {"errorId": 0, "taskId": 12345},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready", "solution": {"text": "eBG4"}},
    ]
    seen = []

    def fake_post(url, payload):
        seen.append((url, payload))
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(plugin, "_captcha_post", fake_post)
    monkeypatch.setattr(host.module.time, "sleep", lambda _s: None)

    assert plugin._solve_captcha_service(b"img") == "eBG4"
    assert seen[0][0].endswith("/createTask")
    assert seen[0][1]["task"]["type"] == "ImageToTextTask"
    assert seen[0][1]["softID"] == host.module.CAPTCHA_SOFT_ID
    assert seen[1][0].endswith("/getTaskResult")


def test_solve_captcha_service_reports_error_code(host, monkeypatch):
    """打码服务返回 errorId 时直接放弃，不无限轮询。"""
    plugin = _enabled_plugin(host, captcha_mode="service", captcha_key="KEY")
    monkeypatch.setattr(plugin, "_captcha_post", lambda url, payload: {"errorId": 12, "errorCode": "ERROR_ZERO_BALANCE"})

    assert plugin._solve_captcha_service(b"img") is None


def test_solve_captcha_service_omits_soft_id_for_2captcha(host, monkeypatch):
    """2Captcha 不接受 softID 字段，必须不带。"""
    plugin = _enabled_plugin(
        host, captcha_mode="service", captcha_key="KEY", captcha_service="2captcha"
    )
    seen = []

    def fake_post(url, payload):
        seen.append((url, payload))
        return {"errorId": 0, "taskId": 1}

    monkeypatch.setattr(plugin, "_captcha_post", fake_post)
    monkeypatch.setattr(host.module.time, "sleep", lambda _s: None)

    plugin._solve_captcha_service(b"img")

    assert seen[0][0].startswith("https://api.2captcha.com")
    assert "softID" not in seen[0][1]


# ---------------------------------------------------------------------------
# 自动登录：登录流程
# ---------------------------------------------------------------------------


def _login_routes(login_body='<root><![CDATA[succeedhandle_login("ok")]]></root>'):
    """构造一套覆盖登录流程的假路由。"""

    def on_post(url, kwargs):
        return _FakeHttpResponse(text=login_body)

    return {
        ("get", "member.php?mod=logging&action=login"): _FakeHttpResponse(
            text=LOGIN_PAGE_SNIPPET
        ),
        ("get", "action=update"): _FakeHttpResponse(text=SECCODE_UPDATE_SNIPPET),
        ("get", "update=72232"): _FakeHttpResponse(content=b"\x89PNG-fake-image"),
        ("post", "loginsubmit=yes"): on_post,
    }


def _session_with_cookies(routes, cookie):
    """构造假会话，并在 POST 登录后写入登录凭证 Cookie。"""
    session = _FakeSession(routes)
    original_post = session.post

    def post(url, **kwargs):
        response = original_post(url, **kwargs)
        session.cookies.update(cookie)
        return response

    session.post = post
    return session


def test_perform_login_sends_md5_password_and_30day_cookietime(host, monkeypatch):
    """登录请求必须带 md5(密码) 与 cookietime=30 天——后者是长期有效的关键。"""
    module = host.module
    plugin = _enabled_plugin(host, account="luobop", password="p@ss word")
    session = _session_with_cookies(
        _login_routes(), {"3rir_2132_auth": "tok", "3rir_2132_sid": "sid"}
    )
    monkeypatch.setattr(plugin, "_solve_captcha", lambda image: "eBG4")

    cookies = plugin._perform_login(session)

    assert cookies == {"3rir_2132_auth": "tok", "3rir_2132_sid": "sid"}
    posted = [c for c in session.calls if c["method"] == "post"][0]
    data = posted["kwargs"]["data"]
    assert data["username"] == "luobop"
    assert data["password"] == module._md5_hex("p@ss word")
    assert data["password"] != "p@ss word"
    assert data["cookietime"] == module.LOGIN_COOKIE_LIFETIME
    assert data["seccodehash"] == "cSB1R7X1"
    assert data["seccodemodid"] == module.SECCODE_MODID
    assert data["seccodeverify"] == "eBG4"
    assert data["formhash"] == "1cdb3aa6"
    assert "loginhash=LDr3Z" in posted["url"]


def test_perform_login_retries_after_wrong_captcha(host, monkeypatch):
    """验证码识别错时应当重新拉登录页并重试，而不是直接失败。"""
    module = host.module
    plugin = _enabled_plugin(host, account="u", password="p")
    attempts = []

    def on_post(url, kwargs):
        attempts.append(kwargs["data"]["seccodeverify"])
        if len(attempts) == 1:
            return _FakeHttpResponse(
                text="<root><![CDATA[errorhandle_('抱歉，验证码填写错误', {})]]></root>"
            )
        return _FakeHttpResponse(text="<root><![CDATA[succeedhandle_login('ok')]]></root>")

    routes = _login_routes()
    routes[("post", "loginsubmit=yes")] = on_post
    session = _session_with_cookies(routes, {"3rir_2132_auth": "tok"})
    monkeypatch.setattr(plugin, "_solve_captcha", lambda image: "eBG4")

    cookies = plugin._perform_login(session)

    assert len(attempts) == 2
    assert cookies["3rir_2132_auth"] == "tok"
    assert module.LOGIN_MAX_ATTEMPTS == 3


def test_perform_login_gives_up_after_max_attempts(host, monkeypatch):
    """连续失败达到上限后返回 None，不无限重试。"""
    module = host.module
    plugin = _enabled_plugin(host, account="u", password="p")
    routes = _login_routes(
        login_body="<root><![CDATA[errorhandle_('抱歉，验证码填写错误', {})]]></root>"
    )
    session = _session_with_cookies(routes, {"3rir_2132_auth": "tok"})
    monkeypatch.setattr(plugin, "_solve_captcha", lambda image: "eBG4")

    assert plugin._perform_login(session) is None
    posts = [c for c in session.calls if c["method"] == "post"]
    assert len(posts) == module.LOGIN_MAX_ATTEMPTS


def test_perform_login_stops_when_captcha_unreadable(host, monkeypatch):
    """验证码识别不出来时不提交登录请求，避免白白消耗一次失败计数。"""
    plugin = _enabled_plugin(host, account="u", password="p")
    session = _FakeSession(_login_routes())
    monkeypatch.setattr(plugin, "_solve_captcha", lambda image: None)

    assert plugin._perform_login(session) is None
    assert not [c for c in session.calls if c["method"] == "post"]


def test_perform_login_escalates_to_service_after_captcha_rejection(host, monkeypatch):
    """第一轮被站点以「验证码填写错误」拒绝后，第二轮必须换成在线打码。

    否则本地模型一旦对不准这个站点，三次重试都只是在重复同一个错误答案。
    """
    plugin = _enabled_plugin(host, account="u", password="p")
    seen_modes = []

    def fake_solve(image):
        seen_modes.append(plugin._captcha_escalated)
        return "eBG4"

    monkeypatch.setattr(plugin, "_solve_captcha", fake_solve)

    attempts = []

    def on_post(url, kwargs):
        attempts.append(kwargs["data"]["seccodeverify"])
        if len(attempts) == 1:
            return _FakeHttpResponse(
                text="<root><![CDATA[errorhandle_('抱歉，验证码填写错误', {})]]></root>"
            )
        return _FakeHttpResponse(text="<root><![CDATA[succeedhandle_login('ok')]]></root>")

    routes = _login_routes()
    routes[("post", "loginsubmit=yes")] = on_post
    session = _session_with_cookies(routes, {"3rir_2132_auth": "tok"})

    cookies = plugin._perform_login(session)

    assert cookies["3rir_2132_auth"] == "tok"
    # 第一轮未升级，第二轮已升级
    assert seen_modes == [False, True]


def test_perform_login_does_not_escalate_on_password_error(host, monkeypatch):
    """账号密码错属于「再试也没用」，不该升级到在线打码，避免无谓消耗。"""
    plugin = _enabled_plugin(host, account="u", password="p")
    seen_modes = []
    monkeypatch.setattr(
        plugin, "_solve_captcha", lambda image: seen_modes.append(plugin._captcha_escalated) or "eBG4"
    )
    routes = _login_routes(
        login_body="<root><![CDATA[errorhandle_('抱歉，密码错误', {})]]></root>"
    )
    session = _session_with_cookies(routes, {"3rir_2132_auth": "tok"})

    assert plugin._perform_login(session) is None
    assert seen_modes == [False] * host.module.LOGIN_MAX_ATTEMPTS


def test_perform_login_resets_escalation_between_calls(host, monkeypatch):
    """升级标记是「本轮登录」的状态，下一轮必须重置，不能污染后续登录。"""
    plugin = _enabled_plugin(host, account="u", password="p")
    plugin._captcha_escalated = True
    seen = []
    monkeypatch.setattr(
        plugin, "_solve_captcha", lambda image: seen.append(plugin._captcha_escalated) or "eBG4"
    )
    session = _session_with_cookies(_login_routes(), {"3rir_2132_auth": "tok"})

    plugin._perform_login(session)

    assert seen == [False]


def test_fetch_captcha_returns_empty_when_page_has_no_seccode(host, monkeypatch):
    """登录页没有验证码时返回空串，表示「无需验证码」。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(plugin, "_solve_captcha", lambda image: "XXXX")

    assert plugin._fetch_captcha(_FakeSession(), "") == ""


def test_fetch_captcha_follows_update_script_to_image(host, monkeypatch):
    """验证码分两步：先 action=update 拿脚本，再从脚本里取图片地址。"""
    plugin = _enabled_plugin(host)
    session = _FakeSession(
        {
            ("get", "action=update"): _FakeHttpResponse(text=SECCODE_UPDATE_SNIPPET),
            ("get", "update=72232"): _FakeHttpResponse(content=b"png-bytes"),
        }
    )
    seen = []
    monkeypatch.setattr(
        plugin, "_solve_captcha", lambda image: seen.append(image) or "eBG4"
    )

    assert plugin._fetch_captcha(session, "cSDb5rzV") == "eBG4"
    assert seen == [b"png-bytes"]


def test_fetch_captcha_returns_none_when_image_missing(host):
    """取不到图片地址时返回 None，由调用方按失败处理。"""
    plugin = _enabled_plugin(host)
    session = _FakeSession({("get", "action=update"): _FakeHttpResponse(text="<html></html>")})

    assert plugin._fetch_captcha(session, "cSDb5rzV") is None


# ---------------------------------------------------------------------------
# 自动登录：整体编排
# ---------------------------------------------------------------------------


def test_auto_login_ready_requires_credentials(host):
    """开关打开但缺账号或缺密码时都不算就绪。"""
    module = host.module
    plugin = _enabled_plugin(host)
    assert not plugin._auto_login_ready()

    plugin = _enabled_plugin(host, auto_login=True, account="u")
    assert not plugin._auto_login_ready()

    plugin = _enabled_plugin(host, auto_login=True, account="u", password="p")
    assert plugin._auto_login_ready()

    plugin = _enabled_plugin(host, auto_login=False, account="u", password="p")
    assert not plugin._auto_login_ready()
    assert module.CAPTCHA_MODE_AUTO in module.CAPTCHA_MODES


def test_auto_login_now_skips_without_credentials(host):
    """没配账号密码时不应创建会话，更不该发请求。"""
    plugin = _enabled_plugin(host)
    called = []
    plugin._new_session = lambda: called.append(1) or (None, None)

    assert plugin._auto_login_now("测试") is False
    assert called == []


def test_auto_login_now_saves_cookie_and_writes_config(host, monkeypatch):
    """登录成功且校验通过后：Cookie 写入插件数据，并按配置回写配置字段。"""
    module = host.module
    plugin = _enabled_plugin(
        host,
        cookie="cf_clearance=KEEP; 3rir_2132_sid=old",
        auto_login=True,
        account="u",
        password="p",
        save_login_cookie=True,
    )
    session = _FakeSession()
    monkeypatch.setattr(plugin, "_new_session", lambda: (session, "fake"))
    monkeypatch.setattr(plugin, "_perform_login", lambda s: {"3rir_2132_auth": "new"})
    monkeypatch.setattr(plugin, "_verify_cookie", lambda cookie: "luobop")

    assert plugin._auto_login_now("Cookie 已失效") is True
    assert session.closed is True
    # cf_clearance 必须被保住
    assert "cf_clearance=KEEP" in plugin._cookie
    assert "3rir_2132_auth=new" in plugin._cookie
    assert plugin.store[module.KEY_LOGIN_COOKIE] == plugin._cookie
    assert plugin.saved_config["cookie"] == plugin._cookie


def test_auto_login_now_keeps_config_when_writeback_disabled(host, monkeypatch):
    """关掉回写时只写插件数据，配置字段保持用户原值。"""
    module = host.module
    plugin = _enabled_plugin(
        host,
        cookie="cf_clearance=KEEP",
        auto_login=True,
        account="u",
        password="p",
        save_login_cookie=False,
    )
    monkeypatch.setattr(plugin, "_new_session", lambda: (_FakeSession(), "fake"))
    monkeypatch.setattr(plugin, "_perform_login", lambda s: {"3rir_2132_auth": "new"})
    monkeypatch.setattr(plugin, "_verify_cookie", lambda cookie: "luobop")

    assert plugin._auto_login_now("Cookie 已失效") is True
    assert plugin.store[module.KEY_LOGIN_COOKIE] == plugin._cookie
    assert "cookie" not in plugin.saved_config


def test_auto_login_now_rejects_cookie_that_fails_verification(host, monkeypatch):
    """登录看似成功但新 Cookie 打不开签到页时，不能污染现有配置。"""
    module = host.module
    plugin = _enabled_plugin(
        host,
        cookie="cf_clearance=KEEP",
        auto_login=True,
        account="u",
        password="p",
    )
    monkeypatch.setattr(plugin, "_new_session", lambda: (_FakeSession(), "fake"))
    monkeypatch.setattr(plugin, "_perform_login", lambda s: {"3rir_2132_auth": "new"})
    monkeypatch.setattr(plugin, "_verify_cookie", lambda cookie: None)

    assert plugin._auto_login_now("Cookie 已失效") is False
    assert plugin._cookie == "cf_clearance=KEEP"
    assert module.KEY_LOGIN_COOKIE not in plugin.store


def test_auto_login_now_survives_login_exception(host, monkeypatch):
    """登录过程抛异常时不得中断签到主流程。"""
    plugin = _enabled_plugin(host, auto_login=True, account="u", password="p")
    session = _FakeSession()
    monkeypatch.setattr(plugin, "_new_session", lambda: (session, "fake"))

    def boom(_session):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(plugin, "_perform_login", boom)

    assert plugin._auto_login_now("Cookie 已失效") is False
    assert session.closed is True


def test_verify_cookie_reads_username_from_sign_page(host):
    """校验新 Cookie 的方式是直接访问签到页并读用户名。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {
        module.SIGN_PAGE_URL: _StubResponse(
            text='<a href="home.php?mod=space&uid=1" title="访问我的空间">luobop</a>'
        )
    }

    assert plugin._verify_cookie("a=1") == "luobop"


def test_verify_cookie_returns_none_when_not_logged_in(host):
    """签到页显示「请先登录」时应判定校验失败。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {
        module.SIGN_PAGE_URL: _StubResponse(
            text="<p>您需要先登录才能继续本操作</p>"
        )
    }

    assert plugin._verify_cookie("a=1") is None


def test_verify_cookie_returns_none_on_cloudflare(host):
    """被 Cloudflare 拦截时不能把候选 Cookie 判为可用。"""
    module = host.module
    plugin = _enabled_plugin(host)
    host.request.routes = {
        module.SIGN_PAGE_URL: _StubResponse(
            status_code=403, text=CF_CHALLENGE_BODY, headers=CF_CHALLENGE_HEADERS
        )
    }

    assert plugin._verify_cookie("a=1") is None


# ---------------------------------------------------------------------------
# 签到主流程：Cookie 失效时自动登录并重试
# ---------------------------------------------------------------------------


def test_execute_signin_retries_after_auto_login(host, monkeypatch):
    """Cookie 失效 -> 自动登录成功 -> 重跑一轮并返回成功结果。"""
    plugin = _enabled_plugin(host, auto_login=True, account="u", password="p")
    calls = {"signin": 0, "login": 0}

    def fake_signin_once():
        calls["signin"] += 1
        if calls["signin"] == 1:
            return None, "Cookie 已失效（签到页未显示用户名）"
        return {"success": True, "message": "ok"}, None

    def fake_login(reason):
        calls["login"] += 1
        return True

    monkeypatch.setattr(plugin, "_signin_once", fake_signin_once)
    monkeypatch.setattr(plugin, "_auto_login_now", fake_login)

    result = plugin._execute_signin()

    assert result == {"success": True, "message": "ok"}
    assert calls == {"signin": 2, "login": 1}


def test_execute_signin_reports_failure_when_relogin_fails(host, monkeypatch):
    """自动登录失败时仍要如实报告失败，并保留原始原因。"""
    plugin = _enabled_plugin(host, auto_login=True, account="u", password="p")
    monkeypatch.setattr(
        plugin, "_signin_once", lambda: (None, "Cookie 已失效（签到页未显示用户名）")
    )
    monkeypatch.setattr(plugin, "_auto_login_now", lambda reason: False)

    result = plugin._execute_signin()

    assert result["success"] is False
    assert "Cookie 已失效" in result["content"]


def test_execute_signin_does_not_relogin_without_credentials(host, monkeypatch):
    """未配置账号密码时不应尝试自动登录。"""
    plugin = _enabled_plugin(host, cookie="a=1")
    monkeypatch.setattr(
        plugin, "_signin_once", lambda: (None, "Cookie 已失效（签到页未显示用户名）")
    )
    called = []
    monkeypatch.setattr(plugin, "_auto_login_now", lambda reason: called.append(1))

    result = plugin._execute_signin()

    assert result["success"] is False
    assert called == []


def test_execute_signin_does_not_relogin_on_cloudflare(host, monkeypatch):
    """Cloudflare 拦截不是重新登录能修的，不应触发自动登录。"""
    plugin = _enabled_plugin(host, auto_login=True, account="u", password="p")
    monkeypatch.setattr(plugin, "_signin_once", lambda: ({"success": False}, None))
    called = []
    monkeypatch.setattr(plugin, "_auto_login_now", lambda reason: called.append(1))

    plugin._execute_signin()

    assert called == []


def test_execute_signin_allows_empty_cookie_when_auto_login_configured(host, monkeypatch):
    """只要配了自动登录，没填 Cookie 也能开跑（由自动登录取回）。"""
    plugin = _enabled_plugin(host, cookie="", auto_login=True, account="u", password="p")
    monkeypatch.setattr(plugin, "_signin_once", lambda: ({"success": True}, None))

    assert plugin._execute_signin() == {"success": True}


def test_execute_signin_requires_cookie_without_auto_login(host):
    """既没 Cookie 也没自动登录时，直接报「未配置Cookie」。"""
    plugin = _enabled_plugin(host, cookie="")

    result = plugin._execute_signin()

    assert result["success"] is False
    assert result["content"] == "未配置Cookie"


# ---------------------------------------------------------------------------
# 自动登录：配置三处同步
# ---------------------------------------------------------------------------


def test_init_plugin_reads_auto_login_config(host):
    """配置应被完整读入运行状态。"""
    plugin = _enabled_plugin(
        host,
        auto_login=True,
        account=" luobop ",
        password="pw",
        question_id="3",
        answer="北京",
        captcha_mode="service",
        captcha_service="2captcha",
        captcha_key="KEY",
        captcha_base_url="https://my.node/",
        save_login_cookie=False,
    )

    assert plugin._auto_login is True
    assert plugin._account == "luobop"
    assert plugin._password == "pw"
    assert plugin._question_id == "3"
    assert plugin._answer == "北京"
    assert plugin._captcha_mode == "service"
    assert plugin._captcha_service == "2captcha"
    assert plugin._captcha_key == "KEY"
    assert plugin._captcha_base_url == "https://my.node/"
    assert plugin._save_login_cookie is False


@pytest.mark.parametrize(
    "overrides,expected_mode,expected_service",
    [
        ({"captcha_mode": "bogus"}, "auto", "yescaptcha"),
        ({"captcha_service": "bogus"}, "auto", "yescaptcha"),
        ({"captcha_mode": ""}, "auto", "yescaptcha"),
    ],
)
def test_init_plugin_falls_back_on_invalid_captcha_options(
    host, overrides, expected_mode, expected_service
):
    """非法的验证码选项要回落到默认值，不能让自动登录整体不可用。"""
    plugin = _enabled_plugin(host, **overrides)

    assert plugin._captcha_mode == expected_mode
    assert plugin._captcha_service == expected_service


def test_save_config_persists_auto_login_fields(host):
    """保存配置时必须把新字段一起写回，否则下次保存会被前端表单静默清空。"""
    plugin = _enabled_plugin(
        host,
        auto_login=True,
        account="luobop",
        password="pw",
        question_id="2",
        answer="x",
        captcha_mode="service",
        captcha_service="2captcha",
        captcha_key="KEY",
        captcha_base_url="https://node/",
        save_login_cookie=False,
    )

    plugin._save_config()

    saved = plugin.saved_config
    assert saved["auto_login"] is True
    assert saved["account"] == "luobop"
    assert saved["password"] == "pw"
    assert saved["question_id"] == "2"
    assert saved["answer"] == "x"
    assert saved["captcha_mode"] == "service"
    assert saved["captcha_service"] == "2captcha"
    assert saved["captcha_key"] == "KEY"
    assert saved["captcha_base_url"] == "https://node/"
    assert saved["save_login_cookie"] is False


def test_get_form_declares_every_auto_login_field(host):
    """表单必须声明全部新字段——没声明的字段会在保存时被静默清空。"""
    plugin = _enabled_plugin(host)

    form, defaults = plugin.get_form()
    rendered = json.dumps(form, ensure_ascii=False)

    for field in (
        "auto_login",
        "account",
        "password",
        "question_id",
        "answer",
        "captcha_mode",
        "captcha_service",
        "captcha_key",
        "captcha_base_url",
        "save_login_cookie",
    ):
        assert f'"{field}"' in rendered, f"表单缺少字段 {field}"
        assert field in defaults, f"默认值缺少字段 {field}"

    assert defaults["captcha_mode"] == "auto"
    assert defaults["save_login_cookie"] is True
    assert defaults["auto_login"] is False
