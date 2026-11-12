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

HDHavenSigner = hdhaven.HDHavenSigner
HDHavenSignError = hdhaven.HDHavenSignError

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
