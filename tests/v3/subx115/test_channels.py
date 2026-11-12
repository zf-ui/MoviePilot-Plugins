# -*- coding: utf-8 -*-
"""SubX115 纯函数单元测试（不依赖 MoviePilot 宿主，可独立运行）。

用法：
    pytest tests/v3/subx115/test_channels.py
    或直接：python tests/v3/subx115/test_channels.py
"""

import importlib.util
import sys
from pathlib import Path

# channels.py 不依赖 MoviePilot 宿主，按文件路径独立加载（避免触发 __init__.py）
_CHANNELS = Path(__file__).resolve().parents[3] / "plugins.v3" / "subx115" / "channels.py"
_spec = importlib.util.spec_from_file_location("subx115_channels", _CHANNELS)
channels = importlib.util.module_from_spec(_spec)
sys.modules["subx115_channels"] = channels  # dataclass 解析需要模块已注册
_spec.loader.exec_module(channels)

# hdhaven.py 同样不依赖宿主（curl_cffi 为延迟导入），按文件路径独立加载
_HDHAVEN = Path(__file__).resolve().parents[3] / "plugins.v3" / "subx115" / "hdhaven.py"
_spec2 = importlib.util.spec_from_file_location("subx115_hdhaven", _HDHAVEN)
hdhaven = importlib.util.module_from_spec(_spec2)
sys.modules["subx115_hdhaven"] = hdhaven
_spec2.loader.exec_module(hdhaven)

# zhenying.py（帧影签到）同样不依赖宿主，按文件路径独立加载
_ZHENYING = Path(__file__).resolve().parents[3] / "plugins.v3" / "subx115" / "zhenying.py"
_spec3 = importlib.util.spec_from_file_location("subx115_zhenying", _ZHENYING)
zhenying = importlib.util.module_from_spec(_spec3)
sys.modules["subx115_zhenying"] = zhenying
_spec3.loader.exec_module(zhenying)

HDHavenSigner = hdhaven.HDHavenSigner
HDHavenSignError = hdhaven.HDHavenSignError
ZhenyingSigner = zhenying.ZhenyingSigner
ZhenyingSignError = zhenying.ZhenyingSignError

parse_share = channels.parse_share
parse_season_episode = channels.parse_season_episode
split_lines = channels.split_lines
to_int = channels.to_int
ShareInfo = channels.ShareInfo
match_subscribes = channels.match_subscribes
match_keywords = channels.match_keywords
RssChannel = channels.RssChannel
JsonApiChannel = channels.JsonApiChannel
HdhiveChannel = channels.HdhiveChannel
JuyingChannel = channels.JuyingChannel


# ---------------------------------------------------------------------------
# parse_share
# ---------------------------------------------------------------------------

def test_parse_share_full():
    link, code, pwd = parse_share(
        "高清资源 https://115.com/s/abc123456 提取码: 9x8y")
    assert link == "https://115.com/s/abc123456"
    assert code == "abc123456"
    assert pwd == "9x8y"


def test_parse_share_no_password():
    _, code, pwd = parse_share("新片 https://115.com/s/xyz9999")
    assert code == "xyz9999"
    assert pwd == ""


def test_parse_share_code_only():
    _, code, _ = parse_share("文本里只有 115.com/s/qwerty12 提取码 1234")
    assert code == "qwerty12"


def test_parse_share_none():
    link, code, pwd = parse_share("没有链接的普通文本")
    assert link == "" and code == "" and pwd == ""


# ---------------------------------------------------------------------------
# parse_season_episode
# ---------------------------------------------------------------------------

def test_season_episode_sxxexx():
    assert parse_season_episode("Show S01E02 2160p") == (1, 2)
    assert parse_season_episode("Show S2.E03") == (2, 3)


def test_season_episode_chinese():
    assert parse_season_episode("某剧 第2季第13集") == (2, 13)
    assert parse_season_episode("某剧 第 3 集") == (None, 3)


def test_season_episode_episode_only():
    assert parse_season_episode("番组 EP07") == (None, 7)


def test_season_episode_none():
    assert parse_season_episode("某电影 2023 4K HDR") == (None, None)
    assert parse_season_episode("") == (None, None)


# ---------------------------------------------------------------------------
# split_lines / to_int
# ---------------------------------------------------------------------------

def test_split_lines():
    assert split_lines(" a\n\nb \nc") == ["a", "b", "c"]
    assert split_lines("") == []
    assert split_lines(["x", " y ", None]) == ["x", "y"]


def test_to_int():
    assert to_int("10", 5) == 10
    assert to_int(None, 7) == 7
    assert to_int("abc", 9) == 9


# ---------------------------------------------------------------------------
# match_subscribes / match_keywords
# ---------------------------------------------------------------------------

class _FakeSub:
    def __init__(self, name, season=None):
        self.name = name
        self.season = season


def test_match_subscribes_hit():
    share = ShareInfo(channel="hdhive", title="流浪地球2 2160p S01E01")
    subs = [_FakeSub("流浪地球2"), _FakeSub("别的片")]
    hits = match_subscribes(share, subs)
    assert len(hits) == 1 and hits[0].name == "流浪地球2"


def test_match_subscribes_season_mismatch():
    share = ShareInfo(channel="hdhive", title="某剧 S02E01")
    subs = [_FakeSub("某剧", season=1)]
    assert match_subscribes(share, subs) == []


def test_match_subscribes_dict():
    share = ShareInfo(channel="juying", title="繁花 第 1 集")
    hits = match_subscribes(share, [{"name": "繁花", "season": 1}])
    assert len(hits) == 1


def test_match_keywords():
    share = ShareInfo(channel="woniu", title="海贼王 4K 2160p")
    assert match_keywords(share, ["海贼王", "不存在"]) == ["海贼王"]


# ---------------------------------------------------------------------------
# 适配器行为（不发真实请求，只测配置解析与过滤）
# ---------------------------------------------------------------------------

def test_channel_adapter_config():
    cfg = {
        "hdhive_enabled": True,
        "hdhive_api": " https://example.com/api ",
        "hdhive_channels": "https://example.com/a\n\nhttps://example.com/b",
        "hdhive_limit": "5",
        "hdhive_include": "",
        "hdhive_exclude": "预告",
    }
    adapter = HdhiveChannel(cfg)
    assert adapter.available() is True
    assert adapter.api == "https://example.com/api"
    assert adapter.sources == ["https://example.com/a", "https://example.com/b"]
    assert adapter.limit == 5
    shares = [
        ShareInfo(channel="hdhive", title="正片资源"),
        ShareInfo(channel="hdhive", title="预告片资源"),
        ShareInfo(channel="hdhive", title="另一正片"),
    ]
    filtered = adapter.apply_filters(shares)
    assert [s.title for s in filtered] == ["正片资源", "另一正片"]


def test_channel_adapter_not_available():
    adapter = JuyingChannel({"juying_enabled": False, "juying_api": "https://x"})
    assert adapter.available() is False
    adapter = JuyingChannel({"juying_enabled": True})
    assert adapter.available() is False  # 无地址无来源


def test_rss_parse():
    xml = """
    <rss><channel>
    <item>
      <title><![CDATA[新片A 2160p]]></title>
      <link>https://115.com/s/aaaa1111</link>
      <description>提取码: 1a2b</description>
    </item>
    <item>
      <title><![CDATA[无链接条目]]></title>
      <link>https://example.com</link>
    </item>
    </channel></rss>
    """
    items = RssChannel._ITEM_RE.findall(xml)
    assert len(items) == 2
    title = RssChannel._clean(RssChannel._TITLE_RE.search(items[0]).group(1))
    assert title == "新片A 2160p"


# ---------------------------------------------------------------------------
# 栖影（HDHaven）签到 Cookie 校验
# ---------------------------------------------------------------------------

def test_hdhaven_parse_cookie_ok():
    value = "hdh_session=abc123; theme=dark"
    assert HDHavenSigner.parse_cookie(value) == value


def test_hdhaven_parse_cookie_with_prefix():
    assert HDHavenSigner.parse_cookie(
        "Cookie: hdh_session=xyz") == "hdh_session=xyz"


def test_hdhaven_parse_cookie_missing_session():
    try:
        HDHavenSigner.parse_cookie("theme=dark; uid=1")
        assert False, "缺少 hdh_session 应抛异常"
    except HDHavenSignError:
        pass


def test_hdhaven_parse_cookie_bad_format():
    for bad in ("", "不是cookie", "hdh_session=abc\nuid=2"):
        try:
            HDHavenSigner.parse_cookie(bad)
            assert False, f"非法 Cookie 应抛异常：{bad!r}"
        except HDHavenSignError:
            pass


# ---------------------------------------------------------------------------
# 帧影（FrameHDR）签到 Cookie 校验
# ---------------------------------------------------------------------------

def test_zhenying_parse_cookie_ok():
    value = "wangpan_session=ch0m3tgh3a4b6grhjeaf7dh84g"
    assert ZhenyingSigner.parse_cookie(value) == value


def test_zhenying_parse_cookie_with_security_token():
    result = ZhenyingSigner.parse_cookie(
        "_security_token=abc123; wangpan_session=xyz")
    assert "wangpan_session=xyz" in result
    assert "_security_token=abc123" in result


def test_zhenying_parse_cookie_with_prefix():
    assert ZhenyingSigner.parse_cookie(
        "Cookie: wangpan_session=xyz") == "wangpan_session=xyz"


def test_zhenying_parse_cookie_missing_session():
    try:
        ZhenyingSigner.parse_cookie("_security_token=abc; uid=1")
        assert False, "缺少 wangpan_session 应抛异常"
    except ZhenyingSignError:
        pass


def test_zhenying_parse_cookie_bad_format():
    for bad in ("", "不是cookie", "wangpan_session=abc\nuid=2"):
        try:
            ZhenyingSigner.parse_cookie(bad)
            assert False, f"非法 Cookie 应抛异常：{bad!r}"
        except ZhenyingSignError:
            pass


def test_zhenying_parse_cookie_tolerates_orphan_fragment():
    # 复制/粘贴时 _security_token= 前缀被截断、只剩孤立 token 尾巴的场景：
    # 应宽容忽略无「=」的片段，只要 wangpan_session 完整即可
    result = ZhenyingSigner.parse_cookie(
        "ZmEzNDk2MWFIMzAwODJkY2E5YzMxY2Jm; wangpan_session=ch0m3tgh3a4b6grhjeaf7dh84g")
    assert result == "wangpan_session=ch0m3tgh3a4b6grhjeaf7dh84g"


def test_zhenying_parse_cookie_tolerates_empty_fragments():
    # 首尾多余分号 + 空片段 + 合法键值对共存
    result = ZhenyingSigner.parse_cookie(
        "; ; wangpan_session=xyz ; _security_token=abc;;")
    assert "wangpan_session=xyz" in result
    assert "_security_token=abc" in result


# ---------------------------------------------------------------------------
# 签到流程（_init_state / check_login / signin）：用假 session 模拟 HTTP 响应
# ---------------------------------------------------------------------------

class _FakeResp:
    """模拟 httpx/requests 响应对象。"""
    def __init__(self, payload: dict, status_code: int = 200, url: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.url = url

    def json(self):
        return self._payload


class _FakeSession:
    """记录请求并返回预设响应的假 session。"""
    def __init__(self, init_resp: dict, post_resp: dict | None = None,
                 cookies=None):
        self.init_resp = init_resp
        self.post_resp = post_resp
        self.cookies = cookies or []
        self.gets: list = []
        self.posts: list = []

    def get(self, url, params=None, **kwargs):
        self.gets.append((url, params))
        return _FakeResp(self.init_resp)

    def post(self, url, params=None, **kwargs):
        self.posts.append((url, params, kwargs))
        return _FakeResp(self.post_resp or self.init_resp)

    def close(self):
        pass


def _signer_with_session(init_resp, post_resp=None):
    signer = ZhenyingSigner("wangpan_session=xyz")
    fake = _FakeSession(init_resp, post_resp)
    signer._session = lambda cookie="": fake  # 注入假 session
    return signer, fake


def test_zhenying_init_state_parses_csrf_and_status():
    signer, fake = _signer_with_session({
        "success": True,
        "data": {"csrf_token": "tok123", "points": 193, "total_days": 33,
                 "continuous_days": 1, "has_checked_today": True},
    })
    state = signer._init_state(fake)
    assert state["csrf_token"] == "tok123"
    assert state["has_checked_today"] is True
    assert state["points"] == 193
    # 应调用 GET ?action=init 且带 year/month
    url, params = fake.gets[0]
    assert url.endswith("/api/checkin.php")
    assert params["action"] == "init"
    assert "year" in params and "month" in params


def test_zhenying_signin_already_checked():
    signer, fake = _signer_with_session({
        "success": True,
        "data": {"csrf_token": "tok", "points": 193, "continuous_days": 1,
                 "has_checked_today": True},
    })
    result = signer.signin()
    assert result["state"] == "success"
    assert result["already"] is True
    assert result["points"] == 193
    assert result["streak"] == 1
    assert fake.posts == []  # 已签到不应发起 POST


def test_zhenying_signin_submits_with_csrf():
    init = {"success": True,
            "data": {"csrf_token": "tokABC", "points": 100,
                     "continuous_days": 2, "has_checked_today": False}}
    post = {"success": True, "data": {"points": 5}}
    signer, fake = _signer_with_session(init, post)
    result = signer.signin()
    assert result["state"] == "success"
    assert result["earned"] == 5
    # 应发起 POST ?action=checkin，且带 csrf_token
    assert len(fake.posts) == 1
    url, params, kwargs = fake.posts[0]
    assert params["action"] == "checkin"
    # csrf_token 应通过 files= 以 multipart 提交
    assert "files" in kwargs and kwargs["files"]["csrf_token"][1] == "tokABC"


def test_zhenying_signin_site_error():
    init = {"success": True,
            "data": {"csrf_token": "tok", "has_checked_today": False}}
    post = {"success": False, "message": "无效的操作"}
    signer, fake = _signer_with_session(init, post)
    try:
        signer.signin()
        assert False, "站点返回失败应抛异常"
    except ZhenyingSignError as exc:
        assert "无效的操作" in str(exc)


# ---------------------------------------------------------------------------
# 账号密码登录兜底（login / _extract_session_cookie）
# ---------------------------------------------------------------------------

class _FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


def test_zhenying_login_returns_new_cookie():
    # 登录成功后站点下发新的 wangpan_session + _security_token
    signer = ZhenyingSigner("wangpan_session=old", username="u@x.com",
                            password="secret")
    login_session = _FakeSession(
        {"success": True, "data": {"csrf_token": "t", "points": 0}},
        cookies=[_FakeCookie("wangpan_session", "new_session"),
                 _FakeCookie("_security_token", "new_token")])
    # 预检会话返回成功（login 会用新 cookie 再做一次预检）
    precheck_session = _FakeSession(
        {"success": True, "data": {"csrf_token": "t2", "points": 0}})
    sessions = [login_session, precheck_session]

    def fake_session(cookie=""):
        return sessions.pop(0)

    signer._session = fake_session
    new_cookie = signer.login()
    assert "wangpan_session=new_session" in new_cookie
    assert "_security_token=new_token" in new_cookie


def test_zhenying_login_requires_credentials():
    signer = ZhenyingSigner("wangpan_session=old")  # 未配账号密码
    try:
        signer.login()
        assert False, "未配账号密码应抛异常"
    except ZhenyingSignError as exc:
        assert "账号密码" in str(exc)


def test_zhenying_login_no_session_cookie_fails():
    # 登录响应未下发 wangpan_session → 视为登录失败
    signer = ZhenyingSigner("", username="u@x.com", password="secret")
    login_session = _FakeSession(
        {"success": False, "message": "用户名或密码错误"},
        cookies=[_FakeCookie("_security_token", "only_token")])
    signer._session = lambda cookie="": login_session
    try:
        signer.login()
        assert False, "无 wangpan_session 应抛异常"
    except ZhenyingSignError as exc:
        assert "会话 Cookie" in str(exc)


def test_zhenying_extract_session_cookie_requires_wangpan():
    signer = ZhenyingSigner("wangpan_session=old")
    session = _FakeSession({}, cookies=[_FakeCookie("_security_token", "abc")])
    # 只有 _security_token 没有 wangpan_session → 返回空串
    assert signer._extract_session_cookie(session) == ""


def _main():
    """无 pytest 时直接运行。"""
    failed = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
