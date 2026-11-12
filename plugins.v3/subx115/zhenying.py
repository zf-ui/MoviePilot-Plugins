# -*- coding: utf-8 -*-
"""
帧影（FrameHDR, framehdr.com）签到客户端
============================================

帧影是一个纯 PHP 站（非 JSON API 框架），登录态通过 `wangpan_session`
会话 Cookie 维护。签到接口为 `api/checkin.php`，未登录时返回 302 跳转到
`/api/login.php`，登录后返回 JSON `{"success": ..., "data": {...}}`。

签到接口约定（已通过抓包站点前端 JS + 真实 Cookie 实测确认）：
- `GET /api/checkin.php?action=init&year=Y&month=M`
  返回签到状态与本次会话的 `csrf_token`（data 内含 points/total_days/
  continuous_days/has_checked_today/records/csrf_token 等）；
- `POST /api/checkin.php?action=checkin`（multipart/form-data，body 带
  `csrf_token`）执行签到，成功返回 `{success:true, data:{points, rewards[]}}`。
- 注意：`login_status.php` 是登录防暴力破解的状态查询（返回 is_locked 等），
  **不是**登录预检，不能用于判断会话是否有效。

本模块不依赖 MoviePilot 宿主，可独立导入与测试。HTTP 走 httpx（宿主自带）
优先、requests 兜底，与 channels.py 的封装保持一致，因此**不依赖 curl_cffi**
（帧影没有 HDHaven 那种 TLS 指纹校验，普通 HTTPS 即可）。

安全设计（与 HDHaven 对齐）：
- 每次签到前先 GET ?action=init 做状态预检（顺带拿 csrf_token），确认会话有效；
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

    def __init__(self, cookie: str, proxy: str = "", timeout: int = 20,
                 username: str = "", password: str = ""):
        self.cookie = (cookie or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout = timeout
        self.username = (username or "").strip()
        self.password = (password or "").strip()

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

    def _session(self, cookie: str = ""):
        """构造会话。cookie 为空时用于登录（此时无 wangpan_session）。"""
        if cookie:
            cookie = self.parse_cookie(cookie)
        if httpx is not None:
            client = httpx.Client(timeout=self.timeout,
                                  follow_redirects=True)
            if self.proxy:
                client.proxies = {"http://": self.proxy,
                                  "https://": self.proxy}
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0 Safari/537.36"),
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.BASE + "/",
            }
            if cookie:
                headers["Cookie"] = cookie
            client.headers.update(headers)
            return client
        # requests 兜底
        session = requests.Session()
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.BASE + "/",
        }
        if cookie:
            headers["Cookie"] = cookie
        session.headers.update(headers)
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

    # -- 状态查询 / 登录预检 --------------------------------------------------

    def _init_state(self, session) -> dict:
        """GET api/checkin.php?action=init 获取签到状态与 csrf_token。

        返回站点的 data 字典（含 csrf_token、points、total_days、
        continuous_days、has_checked_today、records 等）；未登录或异常抛
        ZhenyingSignError。
        """
        now = datetime.now()
        resp = session.get(
            self.BASE + "/api/checkin.php",
            params={"action": "init",
                    "year": str(now.year), "month": str(now.month)},
        )
        if resp.status_code == 302 or (hasattr(resp, "url")
                                       and str(resp.url).endswith("login.php")):
            raise ZhenyingSignError("会话未通过登录预检，请重新登录并更新 Cookie")
        data = self._decode(resp)
        if data.get("success") is not True:
            raise ZhenyingSignError("会话未通过登录预检，请重新登录并更新 Cookie")
        inner = data.get("data")
        if not isinstance(inner, dict):
            raise ZhenyingSignError("站点签到状态结构异常；请人工核对")
        return inner

    def check_login(self) -> dict:
        """登录预检（GET ?action=init），供「测试登录」按钮。

        :return: {"ok": True, "user": {...}} 或 {"ok": False, "message": ...}
        :raises ZhenyingSignError: 请求/解析异常（不泄露 Cookie）
        """
        session = self._session(self.cookie)
        try:
            inner = self._init_state(session)
            return {"ok": True, "user": {
                "points": inner.get("points"),
                "total_days": inner.get("total_days"),
                "continuous_days": inner.get("continuous_days"),
                "checkin_today": inner.get("has_checked_today"),
            }}
        except ZhenyingSignError as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            self._close(session)

    # -- 账号密码登录（Cookie 失效时兜底） -----------------------------------

    def login(self) -> str:
        """用账号密码登录帧影，返回新的 wangpan_session Cookie。

        帧影登录接口为 `POST /login.php`（form 字段 login_mode=password、
        username、password、remember_me），成功后响应 `Set-Cookie` 下发新的
        `wangpan_session` 与 `_security_token`。登录成功与否通过「随后用新
        Cookie 预检 `api/checkin.php?action=init` 能否通过」来判定，比判断
        302 Location 更可靠。

        :return: 新会话 Cookie 字符串（含 wangpan_session）
        :raises ZhenyingSignError: 未配置账号密码 / 登录失败 / 预检失败
        """
        if not self.username or not self.password:
            raise ZhenyingSignError("未配置帧影账号密码，无法自动重新登录")
        session = self._session()  # 不带 Cookie，让站点下发新会话
        try:
            resp = session.post(
                self.BASE + "/login.php",
                data={"login_mode": "password",
                      "username": self.username,
                      "password": self.password,
                      "remember_me": "1"},
            )
            # 登录成功后站点会通过 Set-Cookie 下发新的 wangpan_session。
            # 优先从响应头提取；httpx 会话对象已把 Cookie 存入 cookies jar。
            new_cookie = self._extract_session_cookie(session)
            if not new_cookie:
                raise ZhenyingSignError("登录失败：站点未返回会话 Cookie"
                                        "（账号密码可能有误）")
            # 用新 Cookie 预检，确认会话已生效
            self.cookie = new_cookie
            check = self._session(new_cookie)
            try:
                self._init_state(check)
            finally:
                self._close(check)
            return new_cookie
        except ZhenyingSignError:
            raise
        except Exception:
            raise ZhenyingSignError("登录请求异常，请检查网络与账号密码") from None
        finally:
            self._close(session)

    def _extract_session_cookie(self, session) -> str:
        """从登录响应提取新的会话 Cookie（wangpan_session + _security_token）。"""
        pairs: list[str] = []

        def _collect(cookies) -> None:
            for c in cookies:
                name = getattr(c, "name", None) or getattr(c, "key", None)
                value = getattr(c, "value", None)
                if name in ("wangpan_session", "_security_token") and value:
                    pairs.append(f"{name}={value}")

        # httpx：session.cookies（Cookies 对象，可迭代 Cookie）
        jar = getattr(session, "cookies", None)
        if jar is not None:
            _collect(jar)
        # requests：session.cookies 同结构，上面已处理；兜底再读响应历史
        if not pairs:
            history = getattr(session, "history", None) or []
            for resp in history:
                _collect(getattr(resp, "cookies", None) or [])
        if not any(p.startswith("wangpan_session=") for p in pairs):
            return ""
        return "; ".join(pairs)

    # -- 签到 ----------------------------------------------------------------

    def signin(self, mark_pending: Optional[Callable[[], None]] = None) -> dict:
        """执行签到。

        :return: {"state": "success", "already": True}（站点确认今日已签到）
                 或 {"state": "success", "earned", "points", "streak"}
        :raises ZhenyingSignError: 任一阶段失败
        """
        session = self._session(self.cookie)
        try:
            today = datetime.now().date().isoformat()

            # 1) 查询签到状态 + csrf_token（只读）
            state = self._init_state(session)

            # 2) 今日已签到 → 直接返回
            if state.get("has_checked_today") is True:
                return {"state": "success", "already": True,
                        "points": state.get("points"),
                        "streak": state.get("continuous_days")}

            # 3) 提交前写 pending 标记（防重复）
            if mark_pending is not None:
                mark_pending()

            # 4) 提交签到：POST ?action=checkin，multipart 表单带 csrf_token
            #    （前端用 FormData 即 multipart/form-data，实测 urlencoded 会被
            #    站点判「操作过于频繁」，必须用 files= 发 multipart 才正确）
            csrf = state.get("csrf_token") or ""
            if httpx is not None:
                resp2 = session.post(
                    self.BASE + "/api/checkin.php",
                    params={"action": "checkin"},
                    files={"csrf_token": (None, csrf)},
                )
            else:
                resp2 = session.post(
                    self.BASE + "/api/checkin.php",
                    params={"action": "checkin"},
                    files={"csrf_token": (None, csrf)},
                )
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
                    or result.get("reward")
                points = result.get("total_points") or result.get("points")
                streak = result.get("continuous_days") or result.get("streak") \
                    or result.get("days")
            else:
                earned = points = streak = None

            # 5) 签到成功后再拉一次状态，回读准确的总积分/连续天数
            try:
                after = self._init_state(session)
                if points is None:
                    points = after.get("points")
                if streak is None:
                    streak = after.get("continuous_days")
            except ZhenyingSignError:
                pass  # 回读失败不阻断，保留已确认的签到成功

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
