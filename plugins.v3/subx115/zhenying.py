# -*- coding: utf-8 -*-
"""
帧影（FrameHDR, framehdr.com）签到客户端
============================================

帧影是一个纯 PHP 站（非 JSON API 框架），登录态通过 `wangpan_session`
会话 Cookie 维护。签到接口为 `api/checkin.php`，未登录时返回 302 跳转到
`/api/login.php`，登录后返回 JSON `{"success": ..., "data": {...}}`。

本模块不依赖 MoviePilot 宿主，可独立导入与测试。HTTP 走 httpx（宿主自带）
优先、requests 兜底，与 channels.py 的封装保持一致，因此**不依赖 curl_cffi**
（帧影没有 HDHaven 那种 TLS 指纹校验，普通 HTTPS 即可）。

安全设计（与 HDHaven 对齐）：
- 每次签到前先做登录预检（api/login_status.php），确认会话有效；
- 只有站点明确返回成功（HTTP 200 + JSON success=true）才记成功；
- 异常消息统一脱敏（不携带 Cookie / 代理地址原文）。
"""

from __future__ import annotations

from datetime import datetime
from http.cookies import SimpleCookie
from typing import Callable, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class ZhenyingSignError(RuntimeError):
    """签到流程异常（消息已脱敏，可直接用于通知）。"""


def _http_client(timeout: int, proxies: dict | None):
    """构造可复用 HTTP 客户端（httpx 优先，requests 兜底）。"""
    if httpx is not None:
        return httpx.Client(timeout=timeout, proxies=proxies,
                            follow_redirects=True)
    if requests is not None:
        return requests.Session()
    raise ZhenyingSignError("宿主缺少 httpx / requests 依赖，无法签到")


class ZhenyingSigner:
    """帧影（FrameHDR）签到器：登录预检 -> 签到。"""

    BASE = "https://framehdr.com"

    def __init__(self, cookie: str, proxy: str = "", timeout: int = 20):
        self.cookie = (cookie or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout = timeout

    # -- Cookie 校验 ---------------------------------------------------------

    @staticmethod
    def parse_cookie(value: str) -> str:
        """校验并规范化浏览器会话 Cookie；非法抛 ZhenyingSignError。

        帧影登录态的关键字段是 `wangpan_session`（站点自己下发的会话 Cookie），
        常见的 `_security_token` 可作为附加凭据一并携带。
        """
        raw = (value or "").strip()
        if raw.lower().startswith("cookie:"):
            raw = raw[7:].strip()
        if not raw or any(c in raw for c in "\r\n"):
            raise ZhenyingSignError("请填写站点登录会话 Cookie")
        # 宽容解析：先过滤掉无法识别为「键=值」的孤立片段（如复制/粘贴时
        # 被截断、缺少「键=」前缀的 token 尾巴），只保留合法键值对，避免
        # SimpleCookie 因单个坏片段而整体失败
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        kv_parts = [p for p in parts if "=" in p]
        jar = SimpleCookie()
        try:
            for p in kv_parts:
                jar.load(p)
        except Exception:
            raise ZhenyingSignError("Cookie 格式错误") from None
        # 只要求关键的 wangpan_session 存在且非空即可
        if "wangpan_session" not in jar or not jar["wangpan_session"].value:
            raise ZhenyingSignError("缺少 wangpan_session 登录会话 Cookie")
        return "; ".join(f"{name}={item.value}" for name, item in jar.items())

    # -- 会话 / 请求 ---------------------------------------------------------

    def _session(self):
        cookie = self.parse_cookie(self.cookie)
        if httpx is not None:
            client = httpx.Client(timeout=self.timeout,
                                  follow_redirects=True)
            if self.proxy:
                client.proxies = {"http://": self.proxy,
                                  "https://": self.proxy}
            client.headers.update({
                "Cookie": cookie,
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0 Safari/537.36"),
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.BASE + "/",
            })
            return client
        # requests 兜底
        session = requests.Session()
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        session.headers.update({
            "Cookie": cookie,
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.BASE + "/",
        })
        return session

    def _decode(self, response) -> dict:
        """解析 JSON 响应；非 JSON 或结构异常抛 ZhenyingSignError。"""
        try:
            if httpx is not None:
                data = response.json()
            else:
                data = response.json()
        except (ValueError, TypeError):
            raise ZhenyingSignError("站点返回非 JSON，可能遭遇验证；请人工核对")
        if not isinstance(data, dict):
            raise ZhenyingSignError("站点响应结构异常；请人工核对")
        return data

    # -- 登录预检 ------------------------------------------------------------

    def check_login(self) -> dict:
        """仅做登录预检（只读请求 api/login_status.php），供「测试登录」按钮。

        :return: {"ok": True, "user": {...}} 或 {"ok": False, "message": ...}
        :raises ZhenyingSignError: 请求/解析异常（不泄露 Cookie）
        """
        session = self._session()
        try:
            resp = session.get(
                self.BASE + "/api/login_status.php",
                params={"action": "login_status"},
            )
            # 未登录时站点会 302 到 /api/login.php；已登录返回 200 JSON
            if resp.status_code == 302 or (hasattr(resp, "url")
                                           and str(resp.url).endswith("login.php")):
                return {"ok": False,
                        "message": "会话未通过登录预检，请重新登录并更新 Cookie"}
            data = self._decode(resp)
            if data.get("success") is not True:
                return {"ok": False,
                        "message": "会话未通过登录预检，请重新登录并更新 Cookie"}
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            return {"ok": True, "user": {
                "username": inner.get("username") or inner.get("name"),
                "checkin_today": inner.get("checkin_today")
                                 or inner.get("checkin")
                                 or inner.get("is_checked_in"),
            }}
        finally:
            self._close(session)

    # -- 签到 ----------------------------------------------------------------

    def signin(self, mark_pending: Optional[Callable[[], None]] = None) -> dict:
        """执行签到。

        :return: {"state": "success", "already": True}（站点确认今日已签到）
                 或 {"state": "success", "earned", "points", "streak"}
        :raises ZhenyingSignError: 任一阶段失败
        """
        session = self._session()
        try:
            today = datetime.now().date().isoformat()

            # 1) 登录预检（只读）
            resp = session.get(
                self.BASE + "/api/login_status.php",
                params={"action": "login_status"},
            )
            if resp.status_code == 302 or (hasattr(resp, "url")
                                           and str(resp.url).endswith("login.php")):
                raise ZhenyingSignError("会话未通过登录预检，请重新登录并更新 Cookie")
            login = self._decode(resp)
            if login.get("success") is not True:
                raise ZhenyingSignError("会话未通过登录预检，请重新登录并更新 Cookie")

            # 2) 查询今日签到状态（若 login_status 带 checkin 字段）
            inner = login.get("data") if isinstance(login.get("data"), dict) else {}
            checked = inner.get("checkin_today") or inner.get("checkin") \
                or inner.get("is_checked_in")
            if checked is True or checked == today or str(checked) == "1":
                return {"state": "success", "already": True}

            # 3) 提交前写 pending 标记（防重复）
            if mark_pending is not None:
                mark_pending()

            # 4) 提交签到：帧影签到为 POST api/checkin.php（浏览器为表单提交）
            if httpx is not None:
                resp2 = session.post(self.BASE + "/api/checkin.php")
            else:
                resp2 = session.post(self.BASE + "/api/checkin.php")
            if resp2.status_code == 302 or (hasattr(resp2, "url")
                                            and str(resp2.url).endswith("login.php")):
                raise ZhenyingSignError("签到请求被重定向到登录页，请更新 Cookie")
            data = self._decode(resp2)
            if data.get("success") is not True:
                msg = data.get("message") or data.get("msg") or "签到未确认"
                raise ZhenyingSignError(f"站点返回：{msg}")
            result = data.get("data")
            if isinstance(result, dict):
                earned = result.get("points") or result.get("earned") \
                    or result.get("reward") or result.get("score")
                points = result.get("total_points") or result.get("points") \
                    or result.get("score")
                streak = result.get("streak") or result.get("continuous_days") \
                    or result.get("days")
            else:
                earned = points = streak = None
            return {"state": "success",
                    "earned": earned, "points": points, "streak": streak}
        finally:
            self._close(session)

    @staticmethod
    def _close(session) -> None:
        try:
            session.close()
        except Exception:
            pass
