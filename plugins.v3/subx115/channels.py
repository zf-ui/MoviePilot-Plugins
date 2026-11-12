# -*- coding: utf-8 -*-
"""
SubX115 渠道适配层
==================

职责：
1. 定义统一的资源信息结构 ShareInfo（标题 + 115 分享链接 + 提取码）。
2. 提供 8 个渠道（聚影/栖影/剧迷/癫影/观影/帧影/蜗牛4K/权益宝）的适配器，
   统一走「HTTP 拉取 -> 解析出 115 分享」的模式，分两种：
   - RssChannel    ：RSS / HTML 文本源，正则解析 <item> 与 115 链接；
   - JsonApiChannel：JSON 接口源，兼容常见的返回结构（data/list/items 等）。
3. 提供标题解析（季/集）、订阅匹配、关键词过滤等纯函数，便于单元测试。

注意：
- 本文件不依赖 MoviePilot 宿主，可独立导入与测试。
- 各渠道的真实接口地址 / 鉴权方式在插件配置页填写；若某渠道的实际
  协议与下方默认适配模式不符，只需为该渠道继承基类重写 fetch() 即可，
  无需改动主插件逻辑。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

# ---------------------------------------------------------------------------
# 轻量 HTTP 封装：httpx 优先（MoviePilot 宿主自带），requests 兜底
# ---------------------------------------------------------------------------

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class NetworkError(Exception):
    """统一网络请求异常（语义上对应宿主 SDK 的 httpx2.RequestError）"""


def _request(method: str, url: str, *, headers: dict | None = None,
             data: dict | None = None, timeout: int = 15,
             proxies: dict | None = None) -> str:
    """发起 HTTP 请求并返回文本；网络层异常统一转为 NetworkError。"""
    if httpx is not None:
        try:
            if proxies:
                with httpx.Client(proxies=proxies, timeout=timeout,
                                  follow_redirects=True) as client:
                    resp = client.request(method, url, headers=headers, data=data)
            else:
                resp = httpx.request(method, url, headers=headers, data=data,
                                     timeout=timeout, follow_redirects=True)
        except Exception as exc:
            raise NetworkError(f"httpx 请求失败: {exc}") from exc
        resp.raise_for_status()
        return resp.text
    if requests is not None:
        try:
            resp = requests.request(method, url, headers=headers, data=data,
                                     timeout=timeout, allow_redirects=True,
                                     proxies=proxies or None)
        except Exception as exc:
            raise NetworkError(f"requests 请求失败: {exc}") from exc
        resp.raise_for_status()
        return resp.text
    raise NetworkError("宿主环境缺少 httpx / requests，无法发起 HTTP 请求")


def request_text(url: str, headers: dict | None = None, data: dict | None = None,
                 timeout: int = 15, proxies: dict | None = None) -> str:
    """GET（无 data）或 POST（有 data）请求，返回响应文本。"""
    return _request("POST" if data else "GET", url,
                    headers=headers, data=data, timeout=timeout, proxies=proxies)


def request_json(url: str, headers: dict | None = None, data: dict | None = None,
                 timeout: int = 15, proxies: dict | None = None) -> Any:
    """请求并解析 JSON；非 JSON 响应抛 NetworkError。"""
    text = request_text(url, headers=headers, data=data, timeout=timeout,
                        proxies=proxies)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise NetworkError(f"响应不是合法 JSON: {text[:200]}") from exc


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def split_lines(value: Any) -> list[str]:
    """把多行文本 / 列表拆成非空行列表。"""
    if not value:
        return []
    if isinstance(value, str):
        return [ln.strip() for ln in value.splitlines() if ln.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    return [str(value).strip()]


def to_int(value: Any, default: int = 0) -> int:
    """容错地把配置值转 int。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 解析：115 分享链接 / 季集
# ---------------------------------------------------------------------------

_SHARE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?115\.com/s/(?P<code>[A-Za-z0-9]{4,20})", re.IGNORECASE)
_PWD_RE = re.compile(
    r"(?:提取码|访问码|密码|passcode|password|pwd)\s*[:：为]?\s*(?P<pwd>[0-9A-Za-z]{4})",
    re.IGNORECASE)


def parse_share(text: str) -> tuple[str, str, str]:
    """从任意文本中解析 115 分享：返回 (链接, 分享码, 提取码)。"""
    text = text or ""
    link, code, pwd = "", "", ""
    m = _SHARE_URL_RE.search(text)
    if m:
        code = m.group("code")
        link = m.group(0)
        if not link.lower().startswith("http"):
            link = f"https://115.com/s/{code}"
    m = _PWD_RE.search(text)
    if m:
        pwd = m.group("pwd")
    return link, code, pwd


_SEASON_EP_PATTERNS = [
    # S01E02 / S1.E3 / s01-e04
    re.compile(r"\b[Ss](\d{1,2})[\s.\-]*[Ee](\d{1,4})\b"),
    # 第 1 季第 2 集 / 第2季第13话
    re.compile(r"第\s*(\d{1,2})\s*季\s*第\s*(\d{1,4})\s*[集话話]"),
    # Season 1 Episode 2 / Season 1 Ep2
    re.compile(r"[Ss]eason\s*(\d{1,2})\s*[\s\-]*[Ee]p(?:isode)?\s*(\d{1,4})"),
]
_EP_ONLY_PATTERNS = [
    # 第 3 集 / 第3话
    re.compile(r"第\s*(\d{1,4})\s*[集话]"),
    # EP03 / E03
    re.compile(r"\b[Ee][Pp]?(\d{1,4})\b(?![\d])"),
]


def parse_season_episode(title: str) -> tuple[Optional[int], Optional[int]]:
    """从标题解析（季, 集）；解析不到返回 (None, None)。"""
    if not title:
        return None, None
    for pat in _SEASON_EP_PATTERNS:
        m = pat.search(title)
        if m:
            return int(m.group(1)), int(m.group(2))
    for pat in _EP_ONLY_PATTERNS:
        m = pat.search(title)
        if m:
            return None, int(m.group(1))
    return None, None


@dataclass
class ShareInfo:
    """一条渠道资源（115 分享）。"""
    channel: str
    title: str = ""
    link: str = ""
    share_code: str = ""
    password: str = ""
    published: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """去重键：渠道 + 分享码。"""
        return f"{self.channel}:{self.share_code or self.link or self.title}"

    @property
    def season_episode(self) -> tuple[Optional[int], Optional[int]]:
        return parse_season_episode(self.title)


# ---------------------------------------------------------------------------
# 渠道适配器
# ---------------------------------------------------------------------------

class ChannelAdapter:
    """
    渠道适配器基类。

    约定：
    - 配置键 = f"{key}_xxx"（enabled/api/cookie/channels/target/include/exclude/limit），
      与插件 get_form() 生成的配置模型一一对应；
    - fetch() 返回 ShareInfo 列表，并已应用 include/exclude/limit 过滤；
    - 拉取失败抛异常（NetworkError 或其它），由主插件记录日志。
    """

    key: str = "base"
    name: str = "基础渠道"
    timeout: int = 15

    def __init__(self, config: dict | None):
        config = config or {}
        self.config = config
        self.enabled = bool(config.get(f"{self.key}_enabled"))
        self.api = str(config.get(f"{self.key}_api") or "").strip()
        self.cookie = str(config.get(f"{self.key}_cookie") or "").strip()
        self.sources = split_lines(config.get(f"{self.key}_channels"))
        self.limit = to_int(config.get(f"{self.key}_limit"), 20)
        self.include = split_lines(config.get(f"{self.key}_include"))
        self.exclude = split_lines(config.get(f"{self.key}_exclude"))
        self.target = str(config.get(f"{self.key}_target") or "").strip()
        # 渠道级代理（配置页开关 + 地址），未启用时为 None
        self.proxies: dict | None = None
        proxy_addr = str(config.get(f"{self.key}_proxy") or "").strip()
        if bool(config.get(f"{self.key}_proxy_enabled")) and proxy_addr:
            self.proxies = {"http://": proxy_addr, "https://": proxy_addr}

    # -- 基础判断 ----------------------------------------------------------

    def available(self) -> bool:
        """启用且至少配置了接口地址或来源列表。"""
        return self.enabled and bool(self.api or self.sources)

    def _urls(self) -> list[str]:
        urls = [u for u in ([self.api] + self.sources) if u]
        # 去重保序
        return list(dict.fromkeys(urls))

    def _headers(self) -> dict:
        ua = str(self.config.get(f"{self.key}_useragent")
                 or "").strip() or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "MoviePilot-SubX115/1.0")
        headers = {"User-Agent": ua}
        if self.cookie:
            headers["Cookie"] = self.cookie
            headers["Authorization"] = f"Bearer {self.cookie}"
        return headers

    # -- 过滤 --------------------------------------------------------------

    @staticmethod
    def _safe_search(pattern: str, title: str):
        try:
            return re.search(pattern, title, re.IGNORECASE)
        except re.error:
            return None

    def apply_filters(self, shares: list[ShareInfo]) -> list[ShareInfo]:
        """应用包含/排除关键词（正则）与条数限制。"""
        result = shares
        if self.include:
            result = [s for s in result
                      if any(self._safe_search(p, s.title) for p in self.include)]
        if self.exclude:
            result = [s for s in result
                      if not any(self._safe_search(p, s.title) for p in self.exclude)]
        if self.limit and self.limit > 0:
            result = result[: self.limit]
        return result

    # -- 拉取（子类实现） ----------------------------------------------------

    def fetch(self, keyword: str = "") -> list[ShareInfo]:
        """拉取该渠道最新资源；具体协议由子类实现。"""
        raise NotImplementedError


class RssChannel(ChannelAdapter):
    """RSS / HTML 文本源适配器：解析 <item> 中的标题/链接/描述里的 115 分享。"""

    _ITEM_RE = re.compile(r"<item[^>]*>(.*?)</item>", re.S | re.I)
    _TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
    _LINK_RE = re.compile(r"<link[^>]*>(.*?)</link>", re.S | re.I)
    _DESC_RE = re.compile(r"<description[^>]*>(.*?)</description>", re.S | re.I)

    @staticmethod
    def _clean(value: str) -> str:
        """去掉 CDATA / HTML 标签并折叠空白。"""
        text = (value or "").strip()
        text = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @classmethod
    def _tag(cls, chunk: str, tag: str) -> str:
        pat = {"title": cls._TITLE_RE, "link": cls._LINK_RE,
               "description": cls._DESC_RE}[tag]
        m = pat.search(chunk)
        return cls._clean(m.group(1)) if m else ""

    def fetch(self, keyword: str = "") -> list[ShareInfo]:
        urls = self._urls()
        if not urls:
            raise RuntimeError("未配置接口地址或订阅来源")
        result: list[ShareInfo] = []
        for url in urls:
            text = request_text(url, headers=self._headers(), timeout=self.timeout,
                                proxies=self.proxies)
            for chunk in self._ITEM_RE.findall(text):
                title = self._tag(chunk, "title")
                link = self._tag(chunk, "link")
                desc = self._tag(chunk, "description")
                if keyword and keyword.lower() not in f"{title} {desc}".lower():
                    continue
                s_link, s_code, s_pwd = parse_share(f"{link} {title} {desc}")
                if not s_code:
                    continue  # 无 115 分享，跳过
                result.append(ShareInfo(
                    channel=self.key, title=title or s_link,
                    link=s_link, share_code=s_code, password=s_pwd))
        return self.apply_filters(result)


class JsonApiChannel(ChannelAdapter):
    """JSON 接口源适配器：兼容 data/list/items/result 等常见返回结构。"""

    @staticmethod
    def _pick(item: Any, *names: str) -> Any:
        """从 dict 或对象中按候选字段名取第一个非空值。"""
        for name in names:
            if isinstance(item, dict):
                value = item.get(name)
            else:
                value = getattr(item, name, None)
            if value not in (None, "", 0):
                return value
        return None

    @staticmethod
    def _iter_items(data: Any):
        """从常见 JSON 结构中提取条目列表。"""
        if isinstance(data, list):
            yield from data
            return
        if isinstance(data, dict):
            for path in ("data", "result", "list", "items"):
                value = data.get(path)
                if isinstance(value, list):
                    yield from value
                    return
                if isinstance(value, dict):
                    for sub in ("list", "items", "data", "records"):
                        if isinstance(value.get(sub), list):
                            yield from value[sub]
                            return

    def fetch(self, keyword: str = "") -> list[ShareInfo]:
        urls = self._urls()
        if not urls:
            raise RuntimeError("未配置接口地址或订阅来源")
        result: list[ShareInfo] = []
        for url in urls:
            real_url = url
            if keyword:
                sep = "&" if "?" in url else "?"
                real_url = f"{url}{sep}keyword={quote(keyword)}"
            data = request_json(real_url, headers=self._headers(),
                                timeout=self.timeout, proxies=self.proxies)
            for item in self._iter_items(data) or []:
                title = str(self._pick(item, "title", "name", "subject") or "")
                link = str(self._pick(item, "link", "url", "share_url",
                                      "pan_url", "share") or "")
                pwd = str(self._pick(item, "password", "pwd", "code") or "")
                if keyword and keyword.lower() not in f"{title} {link}".lower():
                    continue
                s_link, s_code, s_pwd = parse_share(f"{link} {title}")
                if not s_code:
                    continue
                result.append(ShareInfo(
                    channel=self.key, title=title or s_link,
                    link=s_link, share_code=s_code, password=s_pwd or pwd))
        return self.apply_filters(result)


# ---------------------------------------------------------------------------
# 8 个渠道的适配器注册（协议模式可按实际情况在配置页填地址后微调）
# ---------------------------------------------------------------------------

class JuyingChannel(RssChannel):
    """聚影：站内榜单/搜索，若提供 RSS 或导出接口可直接使用。"""
    key, name = "juying", "聚影"


class HdhiveChannel(JsonApiChannel):
    """栖影（HDHaven）：资源社区，通常提供 JSON 接口，账号 Cookie 鉴权。"""
    key, name = "hdhive", "栖影"


class JumiChannel(RssChannel):
    """剧迷：影视资源渠道。"""
    key, name = "jumi", "剧迷"


class DianyingChannel(RssChannel):
    """癫影：影视资源渠道。"""
    key, name = "dianying", "癫影"


class GuanyingChannel(RssChannel):
    """观影：影视资源渠道。"""
    key, name = "guanying", "观影"


class ZhenyingChannel(RssChannel):
    """帧影：影视资源渠道。"""
    key, name = "zhenying", "帧影"


class WoniuChannel(JsonApiChannel):
    """蜗牛4K：4K 高画质资源渠道。"""
    key, name = "woniu", "蜗牛4K"


class QuanyiChannel(JsonApiChannel):
    """权益宝：115 权益/资源渠道。"""
    key, name = "quanyi", "权益宝"


ADAPTER_CLASSES: dict[str, type[ChannelAdapter]] = {
    "juying": JuyingChannel,
    "hdhive": HdhiveChannel,
    "jumi": JumiChannel,
    "dianying": DianyingChannel,
    "guanying": GuanyingChannel,
    "zhenying": ZhenyingChannel,
    "woniu": WoniuChannel,
    "quanyi": QuanyiChannel,
}


# ---------------------------------------------------------------------------
# 匹配：MoviePilot 订阅 / 自定义关键词
# ---------------------------------------------------------------------------

def _field(sub: Any, *names: str, default: Any = None) -> Any:
    """从订阅对象（ORM 对象或 dict）中按候选字段名取第一个有效值。"""
    for name in names:
        if isinstance(sub, dict):
            value = sub.get(name)
        else:
            value = getattr(sub, name, None)
        if value not in (None, "", 0):
            return value
    return default


def match_subscribes(share: ShareInfo, subscribes: list) -> list:
    """
    按片名 + 季做粗匹配，返回命中的订阅对象列表。

    规则：
    - 片名（不区分大小写）需出现在资源标题中；
    - 订阅指定了季、且资源标题解析出季时，两者必须一致；
    - 集数不在这里过滤（由缺集判断/渠道内容决定），保证追更不漏单集。
    """
    hits = []
    share_season, _ = share.season_episode
    for sub in subscribes or []:
        name = str(_field(sub, "name", "title") or "")
        if not name:
            continue
        if name.lower() not in share.title.lower():
            continue
        sub_season = _field(sub, "season", default=None)
        if sub_season is not None:
            try:
                sub_season = int(sub_season)
            except (TypeError, ValueError):
                sub_season = None
        if sub_season and share_season is not None and sub_season != share_season:
            continue
        hits.append(sub)
    return hits


def match_keywords(share: ShareInfo, keywords: list) -> list:
    """按自定义关键词（包含匹配）过滤，返回命中的关键词。"""
    return [k for k in (keywords or []) if k.lower() in share.title.lower()]
