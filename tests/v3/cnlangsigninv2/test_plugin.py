"""国语视界签到 V3 插件单元测试。

宿主接口（``app.*``）由本文件中的轻量桩模块提供，测试覆盖插件的纯逻辑、配置
生命周期与安全边界，不访问真实站点、不使用真实 Cookie。

按规范要求，插件以 ``app.plugins.<plugin_id>`` 路径导入，不把插件目录当作顶层包。
"""

import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

import pytest

PLUGIN_ID = "cnlangsigninv2"
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
    plugin = host.module.CnlangSigninV2()
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
    plugin_cls = host.module.CnlangSigninV2
    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))

    assert plugin_cls.__name__ == "CnlangSigninV2"
    assert PLUGIN_FILE.parent.name == plugin_cls.__name__.lower()
    assert "CnlangSigninV2" in index
    assert index["CnlangSigninV2"]["version"] == plugin_cls.plugin_version
    assert next(iter(index["CnlangSigninV2"]["history"])) == f"v{plugin_cls.plugin_version}"


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
    assert added["plugin_id"] == "CnlangSigninV2"
    assert added["job_id"] == host.module.JOB_SIGNIN_ONCE
    assert added["delay_seconds"] == 3
    assert plugin.saved_config["onlyonce"] is False


def test_onlyonce_falls_back_to_background_thread(host, monkeypatch):
    """宿主调度器不可用时应转入后台线程兜底，不阻塞配置保存请求。"""
    host.scheduler.available = False
    fallback = []
    monkeypatch.setattr(
        host.module.CnlangSigninV2,
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
        host.module.CnlangSigninV2,
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

    plugin = module.CnlangSigninV2()
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
    plugin = host.module.CnlangSigninV2()
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
    assert services[0]["id"] == "CnlangSigninV2.Signin"
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
    assert host.module.CnlangSigninV2._search(r"a(\d+)", "a12b") == "12"
    assert host.module.CnlangSigninV2._search(r"x(\d+)", "a12b") is None
    assert host.module.CnlangSigninV2._search(r"a(\d+)", "") is None


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

    assert host.module.CnlangSigninV2._is_cf_challenge(response) is True


def test_is_cf_challenge_falls_back_to_body_heuristic(host):
    """无官方标记时，仅当 Server 为 cloudflare 且正文含挑战特征才判定拦截。"""
    cls = host.module.CnlangSigninV2

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
    assert host.module.CnlangSigninV2._is_cf_challenge(_BareResponse()) is False


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

    monkeypatch.setattr(module.CnlangSigninV2, "_signin_by_browser", _fake_browser)

    result = plugin.signin()

    assert called == [True]
    assert result["success"] is True
    assert result["money"] == "520"


def test_execute_signin_advises_enabling_browser_mode(host):
    """关闭浏览器模式时，Cloudflare 拦截应给出可执行建议而非“未知错误”。"""
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
    assert "浏览器模式" in result["content"]
    assert "cf_clearance" in result["content"]


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
    """可编程的假页面：按需返回 HTML / 标题 / UA，并记录请求头写入。"""

    def __init__(self, html="", title="", user_agent="", fail_content=False):
        self._html = html
        self._title = title
        self._user_agent = user_agent
        self._fail_content = fail_content
        self.headers = {}
        self.goto_calls = []
        self.evaluate_calls = []

    def content(self):
        if self._fail_content:
            raise RuntimeError("page is navigating")
        return self._html

    def title(self):
        return self._title

    def evaluate(self, expression, *args):
        self.evaluate_calls.append(expression)
        if "navigator.userAgent" in expression:
            return self._user_agent
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


def test_is_cf_challenge_page_covers_title_and_body(host):
    """挑战页识别应同时覆盖标题特征与正文特征，普通页面不得误判。"""
    cls = host.module.CnlangSigninV2

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


def test_wait_cf_pass_returns_true_when_page_has_real_content(host, monkeypatch):
    """页面已有真实内容且不再是挑战页时应立即判定通过。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    page = _FakePage(html="<html><body>" + "x" * 800 + "</body></html>", title="国语视界")

    assert plugin._wait_cf_pass(page, rounds=3) is True


def test_wait_cf_pass_returns_false_on_persistent_challenge(host, monkeypatch):
    """持续停留在挑战页时应耗尽轮询并返回 False。"""
    plugin = _enabled_plugin(host)
    monkeypatch.setattr(host.module.time, "sleep", lambda _seconds: None)
    page = _FakePage(html="<html><body>" + "x" * 800 + "</body></html>", title="Just a moment...")

    assert plugin._wait_cf_pass(page, rounds=3) is False


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
    """写回 Cookie 时必须同时记录浏览器实际 UA——cf_clearance 与 UA 绑定。

    否则写回的 Cookie 配上一个不同的 UA 会立即失效，requests 快速路径永远走不通。
    """
    plugin = _enabled_plugin(host, cookie="sid=abc", user_agent="ConfiguredUA")
    context = _FakeContext(
        cookies=[{"name": "cf_clearance", "value": "fresh"}, {"name": "sid", "value": "abc"}]
    )
    page = _FakePage(user_agent="BrowserRealUA")

    plugin._refresh_cookies_from_browser(context, page)

    assert plugin._user_agent == "BrowserRealUA"
    assert "cf_clearance=fresh" in plugin._cookie
    assert plugin.saved_config["user_agent"] == "BrowserRealUA"


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
    commands = host.module.CnlangSigninV2.get_command()

    assert len(commands) == 1
    assert commands[0]["cmd"] == "/cnlang_signin"
    assert commands[0]["data"]["action"] == host.module.ACTION_SIGNIN
    assert commands[0]["event"] is host.event_type.PluginAction


def test_module_import_has_no_side_effects(host):
    """导入插件模块不应发起请求或登记调度任务。"""
    assert host.request.calls == []
    assert host.scheduler.added == []
