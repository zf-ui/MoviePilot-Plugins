# -*- coding: utf-8 -*-
"""
聚影（JYing, jying.top）签到客户端
=====================================

聚影是 Django + Vue 前后端分离站，登录态采用 **token**（存浏览器
localStorage），**不是 Cookie**。请求通过自定义头 `X-App-User-Token` 携带
token；`csrftoken` 是 CSRF 防护值，需与 token 一起携带（`X-CSRFToken` 头 +
`csrftoken` cookie + `X-Requested-With` 头），否则服务端会判「登录已失效」。

接口约定（已通过逆向前端 JS + 真实 token 抓包实测确认）：
- 登录：`POST /api/app/login/`（JSON `{username, password}`），成功返回
  `{status:"success", token:...}`；失败 `{status:"error", message:...}`。
- 认证头：`X-App-User-Token: <token>` + `X-CSRFToken: <csrftoken>` +
  `X-Requested-With: XMLHttpRequest` + `csrftoken` cookie。
- 签到状态：`GET /api/app/checkin/stats/`
  返回 `checked_today / can_checkin / checkin_mode(normal|luck) /
  my_total_days / reward_points / reward_exp`。
- 普通签到：`POST /api/app/checkin/do/`
- 幸运签到状态：`GET /api/app/checkin/luck/`（含 `can_draw`、
  `user_points`、`checkin_mode`）
- 幸运签到：`POST /api/app/checkin/luck/draw/`
- 奖池：`GET /api/app/checkin/bonus-pool/`、`POST /api/app/checkin/bonus-pool/draw/`

本模块不依赖 MoviePilot 宿主，可独立导入与测试。HTTP 走 httpx 优先、
requests 兜底，与 zhenying.py 保持一致。
"""

from __future__ import annotations

from typing import Callable, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class JuyingSignError(RuntimeError):
    """签到/登录流程异常（消息已脱敏，可直接用于通知）。"""


class JuyingSigner:
    """聚影（JYing）签到器：登录换 token -> 查询状态 -> 签到/抽奖。"""

    BASE = "https://jying.top"

    def __init__(self, username: str = "", password: str = "",
                 token: str = "", proxy: str = "", timeout: int = 20):
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.token = (token or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout = timeout

    # -- 会话 / 请求 ---------------------------------------------------------

    def _session(self):
        """构造会话并预取 csrftoken（访问首页触发下发，供后续请求用）。"""
        if httpx is not None:
            client = httpx.Client(timeout=self.timeout, follow_redirects=True)
            if self.proxy:
                client.proxies = {"http://": self.proxy,
                                  "https://": self.proxy}
            client.headers.update(self._headers())
            # 对齐前端：csrf 从 /api/csrf/ 接口获取（而非首页 /）
            client.get(self.BASE + "/api/csrf/")
            return client
        session = requests.Session()
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        session.headers.update(self._headers())
        # 对齐前端：csrf 从 /api/csrf/ 接口获取（而非首页 /）
        session.get(self.BASE + "/api/csrf/")
        return session

    def _headers(self) -> dict:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/156.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.BASE + "/",
            "Origin": self.BASE,
        }
        if self.token:
            headers["X-App-User-Token"] = self.token
        return headers

    @staticmethod
    def _csrf_from(session) -> str:
        """从会话 cookie 提取 csrftoken 值。"""
        jar = getattr(session, "cookies", None)
        if jar is None:
            return ""
        try:
            c = jar.get("csrftoken")
        except Exception:
            c = None
        if c is None:
            return ""
        return getattr(c, "value", None) or str(c) or ""

    def _decode(self, response) -> dict:
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise JuyingSignError("站点返回非 JSON，可能遭遇验证；请人工核对")
        if not isinstance(data, dict):
            raise JuyingSignError("站点响应结构异常；请人工核对")
        return data

    def _api(self, session, method: str, path: str,
             data: dict | None = None) -> dict:
        """统一请求；非 2xx 或 status!=success 抛 JuyingSignError。"""
        url = self.BASE + "/api/app/" + path
        headers = {"X-CSRFToken": self._csrf_from(session)}
        fn = getattr(session, method.lower())
        kwargs = {"headers": headers}
        if data is not None:
            kwargs["json"] = data
        resp = fn(url, **kwargs)
        body = self._decode(resp)
        if resp.status_code >= 400:
            msg = body.get("message") or body.get("msg") or "请求失败"
            raise JuyingSignError(str(msg))
        return body

    # -- 登录 / token --------------------------------------------------------

    def login(self) -> str:
        """用账号密码登录，返回 token。

        :raises JuyingSignError: 未配置账号密码 / 登录失败
        """
        if not self.username or not self.password:
            raise JuyingSignError("未配置聚影账号密码")
        session = self._session()
        try:
            body = self._api(session, "POST", "login/",
                             {"username": self.username,
                              "password": self.password})
            token = body.get("token")
            if not token:
                raise JuyingSignError(
                    body.get("message") or "登录失败：未返回 token")
            self.token = token
            return token
        finally:
            self._close(session)

    # -- 签到状态 / 签到 -----------------------------------------------------

    def _authed_session(self):
        """带 token 的会话（token 为空则先尝试登录）。"""
        if not self.token:
            self.login()
        return self._session()

    def check_login(self) -> dict:
        """登录预检（GET checkin/stats/），供「测试登录」按钮。

        :return: {"ok": True, "user": {...}} 或 {"ok": False, "message": ...}
        """
        session = self._authed_session()
        try:
            body = self._api(session, "GET", "checkin/stats/")
            return {"ok": True, "user": {
                "points": body.get("reward_points"),
                "checked_today": body.get("checked_today"),
                "total_days": body.get("my_total_days"),
                "checkin_mode": body.get("checkin_mode"),
                "reward_exp": body.get("reward_exp"),
            }}
        except JuyingSignError as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            self._close(session)

    def _get_stats(self, session) -> dict:
        """查签到状态，返回完整响应体（含 data 无关的顶层字段）。"""
        return self._api(session, "GET", "checkin/stats/")

    def signin(self, mode: str = "normal",
               mark_pending: Optional[Callable[[], None]] = None) -> dict:
        """执行签到（mode: normal=普通签到，lucky=幸运签到）。

        :return: {"state": "success", "already": True, ...} 或
                 {"state": "success", "points", "earned", "streak", ...}
        :raises JuyingSignError: 任一阶段失败
        """
        session = self._authed_session()
        try:
            stats = self._get_stats(session)
            if stats.get("checked_today"):
                return {"state": "success", "already": True,
                        "points": stats.get("reward_points"),
                        "streak": stats.get("my_total_days")}

            if mark_pending is not None:
                mark_pending()

            if mode == "lucky":
                # 幸运签到：先查 luck 状态确认可抽，再 draw
                luck = self._api(session, "GET", "checkin/luck/")
                if luck.get("checked_today") or luck.get("daily_checked_today"):
                    return {"state": "success", "already": True,
                            "points": luck.get("user_points")}
                body = self._api(session, "POST", "checkin/luck/draw/", {})
                draw = body.get("draw") if isinstance(
                    body.get("draw"), dict) else {}
                earned = draw.get("delta")
                points = draw.get("balance_after")
                streak = body.get("my_total_days")
            else:
                body = self._api(session, "POST", "checkin/do/", {})
                earned = body.get("reward_points")
                points = body.get("user_points") or body.get("reward_points")
                streak = body.get("my_total_days")

            return {"state": "success",
                    "message": body.get("message"),
                    "points": points, "earned": earned, "streak": streak}
        finally:
            self._close(session)

    def draw_bonus(self, mark_pending: Optional[Callable[[], None]] = None) -> dict:
        """奖池抽奖（签到后自动抽奖）。"""
        session = self._authed_session()
        try:
            pool = self._api(session, "GET", "checkin/bonus-pool/")
            if pool.get("my_draw"):
                return {"state": "success", "already": True}
            if mark_pending is not None:
                mark_pending()
            body = self._api(session, "POST", "checkin/bonus-pool/draw/", {})
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            return {"state": "success",
                    "reward": data.get("reward_points") or data.get("points"),
                    "message": body.get("message")}
        finally:
            self._close(session)

    @staticmethod
    def _close(session) -> None:
        try:
            session.close()
        except Exception:
            pass
