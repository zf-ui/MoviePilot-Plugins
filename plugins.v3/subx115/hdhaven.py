# -*- coding: utf-8 -*-
"""
栖影（HDHaven, hdhaven.com）签到客户端
========================================

移植自 JinxJie/MoviePilot-Plugins plugins.v3/hdhavensign（v3.0.1），并入
SubX115 插件：使用浏览器登录后的站点会话 Cookie，不处理人机验证。

依赖：curl_cffi（impersonate chrome 指纹，规避基础 TLS 指纹校验）。
宿主未安装 curl_cffi 时签到功能自动禁用（渠道订阅功能不受影响）。

安全设计（沿用原插件）：
- 每次签到前先只读查询站点签到状态，确认今日未签到才提交；
- 提交前由调用方写入 pending 标记（mark_pending 回调），防止请求已送达
  但本地未记录时重复扣账 / 重复签到；
- 只有站点明确返回成功（HTTP 201 + success + mode=normal）才记成功；
- 异常消息统一脱敏（不携带 Cookie / 代理地址原文）。
"""

from __future__ import annotations

from datetime import datetime
from http.cookies import SimpleCookie
from typing import Callable, Optional

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None


class HDHavenSignError(RuntimeError):
    """签到流程异常（消息已脱敏，可直接用于通知）。"""


def curl_cffi_available() -> bool:
    """宿主是否安装 curl_cffi。"""
    return curl_requests is not None


class HDHavenSigner:
    """栖影（HDHaven）签到器：预检 -> 查状态 -> 提交签到。"""

    BASE = "https://hdhaven.com"

    def __init__(self, cookie: str, proxy: str = "", timeout: int = 20):
        self.cookie = (cookie or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout = timeout

    # -- Cookie 校验 ---------------------------------------------------------

    @staticmethod
    def parse_cookie(value: str) -> str:
        """校验并规范化浏览器会话 Cookie；非法抛 HDHavenSignError。"""
        raw = (value or "").strip()
        if raw.lower().startswith("cookie:"):
            raw = raw[7:].strip()
        if not raw or any(c in raw for c in "\r\n"):
            raise HDHavenSignError("请填写站点登录会话 Cookie")
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            raise HDHavenSignError("Cookie 格式错误") from None
        if not jar or any(not part.strip() or "=" not in part
                          for part in raw.split(";")):
            raise HDHavenSignError("Cookie 格式错误")
        if "hdh_session" not in jar or not jar["hdh_session"].value:
            raise HDHavenSignError("缺少 hdh_session 登录会话 Cookie")
        return "; ".join(f"{name}={item.value}" for name, item in jar.items())

    # -- 会话 ------------------------------------------------------------------

    def _session(self):
        if curl_requests is None:
            raise HDHavenSignError("宿主缺少 curl_cffi 依赖，无法签到")
        cookie = self.parse_cookie(self.cookie)
        session = curl_requests.Session(impersonate="chrome110")
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        session.headers.update({
            "Cookie": cookie,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.BASE + "/me",
            "Origin": self.BASE,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
        return session

    # -- 响应解析 ---------------------------------------------------------

    def _decode(self, response) -> dict:
        if "json" not in (response.headers.get("content-type") or "").lower():
            raise HDHavenSignError("站点返回非 JSON，可能遭遇人机验证；请人工核对")
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise HDHavenSignError("站点 JSON 响应无法解析；请人工核对") from None
        if not isinstance(data, dict):
            raise HDHavenSignError("站点响应结构异常；请人工核对")
        return data

    def _interpret(self, response) -> dict:
        """只有明确确认成功的响应才记成功。"""
        data = self._decode(response)
        if response.status_code == 201 and data.get("success") is True:
            result = data.get("data")
            if (not isinstance(result, dict)
                    or result.get("mode") != "normal"):
                raise HDHavenSignError("签到成功响应结构异常；请人工核对")
            if any(not isinstance(result.get(k), int)
                   for k in ("earned", "points", "checkin_streak")):
                raise HDHavenSignError("签到结果数字字段缺失；请人工核对")
            return {"state": "success", "earned": result["earned"],
                    "points": result["points"],
                    "streak": result["checkin_streak"]}
        raise HDHavenSignError(
            "签到响应未确认，请在站点核对；再次运行会先查询今日签到状态")

    # -- 主流程 ------------------------------------------------------------

    def check_login(self) -> dict:
        """
        仅做登录预检（只读请求 /api/auth/me），用于「测试登录」按钮。

        :return: {"ok": True, "user": {...}} 或 {"ok": False, "message": ...}
        :raises HDHavenSignError: 请求/解析异常（不泄露 Cookie）
        """
        session = self._session()
        try:
            me = session.get(self.BASE + "/api/auth/me", timeout=self.timeout)
            account = self._decode(me)
            data = account.get("data") if isinstance(account, dict) else None
            has_account = isinstance(data, dict) and bool(data.get("id"))
            if me.status_code != 200 or account.get("success") is not True \
                    or not has_account:
                return {"ok": False,
                        "message": "会话未通过登录预检，请重新登录并更新 Cookie"}
            return {"ok": True, "user": {
                "id": data.get("id"),
                "name": data.get("username") or data.get("name"),
                "checkin_today": data.get("checkin_today"),
            }}
        finally:
            try:
                session.close()
            except Exception:
                pass

    def signin(self, mark_pending: Optional[Callable[[], None]] = None) -> dict:
        """
        执行签到。

        :param mark_pending: 提交签到前由调用方持久化 pending 标记并校验
                             （校验失败应抛异常，此时不会发送签到请求）
        :return: {"state": "success", "already": True}（站点确认今日已签到）
                 或 {"state": "success", "earned", "points", "streak"}
        :raises HDHavenSignError: 任一阶段失败
        """
        session = self._session()
        try:
            today = datetime.now().date().isoformat()

            # 1) 登录预检（只读请求）：确认会话有效
            me = session.get(self.BASE + "/api/auth/me", timeout=self.timeout)
            account = self._decode(me)
            has_account = (isinstance(account.get("data"), dict)
                           and bool(account["data"].get("id")))
            if (me.status_code != 200
                    or account.get("success") is not True
                    or not has_account):
                raise HDHavenSignError("会话未通过登录预检，请重新登录并更新 Cookie")

            # 2) 查询今日签到状态：仅明确未签到才提交
            status = account["data"].get("checkin_today")
            last_checkin = account["data"].get("last_checkin")
            if type(status) is not bool or (last_checkin is not None and (
                    not isinstance(last_checkin, str)
                    or (status and last_checkin != today)
                    or (not status and last_checkin == today))):
                raise HDHavenSignError("站点今日签到状态无法确认，未发送签到请求")
            if status:
                return {"state": "success", "already": True}

            # 3) 提交前写 pending 标记（防重复扣账）
            if mark_pending is not None:
                mark_pending()

            # 4) 提交签到：浏览器行为是 POST 空请求体（不要传 json={}）
            response = session.post(self.BASE + "/api/account/checkin",
                                    timeout=self.timeout)
            return self._interpret(response)
        finally:
            try:
                session.close()
            except Exception:
                pass
