# -*- coding: utf-8 -*-
"""
115 网盘客户端（分享转存）
==========================

说明：
- 本客户端使用 115 网页端接口（webapi.115.com），属于非公开接口，官方
  可能随时调整。失效时按浏览器抓包结果校准 ENDPOINTS 与请求参数即可，
  对外方法签名保持不变。
- 若你具备 115 开放平台（proapi.115.com）的 AppID / AppSecret，建议另行
  实现 open API 版本替换本类（保持 receive_share / get_user 签名一致即可
  无缝切换），稳定性和风控表现都更好。
- 转存为写操作：请勿在测试环境使用生产 Cookie 大批量调用，避免触发风控。
"""

from __future__ import annotations

from typing import Any, Optional

from .channels import request_json


class P115Error(Exception):
    """115 客户端异常（Cookie 失效 / 接口返回异常等）。"""


class P115Client:
    """基于网页端 Cookie 的 115 分享转存客户端。"""

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    # 网页端接口地址（非公开 API，存在变动风险，失效时抓包校准）
    ENDPOINTS = {
        # 用户信息：校验 Cookie 有效性，返回 user_id
        "user_info": "https://webapi.115.com/user/get_user_info",
        # 分享详情：拿分享者 user_id / 分享标题
        "share_info": "https://webapi.115.com/share/sharepost",
        # 分享文件列表：拿待转存 file_id
        "share_files": "https://webapi.115.com/share/list",
        # 转存（接收分享到自己网盘）
        "receive": "https://webapi.115.com/share/receive",
    }

    def __init__(self, cookie: str, timeout: int = 15):
        if not cookie or not str(cookie).strip():
            raise P115Error("115 Cookie 不能为空")
        self.cookie = str(cookie).strip()
        self.timeout = timeout
        self._uid: Optional[str] = None

    # -- 内部 --------------------------------------------------------------

    def _headers(self) -> dict:
        return {"Cookie": self.cookie, "User-Agent": self.UA}

    def _post_json(self, endpoint: str, data: dict) -> dict:
        result = request_json(self.ENDPOINTS[endpoint], headers=self._headers(),
                              data=data, timeout=self.timeout)
        if not isinstance(result, dict):
            raise P115Error(f"接口 {endpoint} 返回非预期结构: {str(result)[:200]}")
        return result

    # -- 对外 --------------------------------------------------------------

    def get_user(self) -> dict:
        """校验 Cookie 并返回用户信息（顺带缓存 user_id）。"""
        result = self._post_json("user_info", {})
        if not result.get("state"):
            raise P115Error(f"Cookie 校验失败: {str(result)[:200]}")
        info = result.get("data") or {}
        self._uid = str(info.get("user_id") or result.get("user_id") or "")
        return info

    @property
    def uid(self) -> str:
        """当前登录用户 user_id（懒加载）。"""
        if not self._uid:
            self.get_user()
        return self._uid or ""

    def receive_share(self, share_code: str, password: str = "",
                      cid: str = "0") -> tuple[bool, str]:
        """
        把一个 115 分享整体转存到指定目录。

        :param share_code: 分享码（115.com/s/xxxx 中的 xxxx）
        :param password:   提取码（没有则留空）
        :param cid:        目标目录 id，"0" 表示根目录
        :return:           (是否成功, 消息)
        """
        if not share_code:
            return False, "分享码为空"

        # 1) 分享详情：拿分享者 user_id（后续接口需要）
        info = self._post_json("share_info", {
            "share_code": share_code,
            "password": password or "",
        })
        if not info.get("state"):
            return False, f"获取分享信息失败: {str(info)[:200]}"
        share_user_id = str((info.get("data") or {}).get("user_id")
                            or info.get("user_id") or "")

        # 2) 文件列表：拿待转存 file_id
        files = self._post_json("share_files", {
            "share_code": share_code,
            "user_id": share_user_id,
            "cid": "0",
            "limit": "500",
        })
        if not files.get("state"):
            return False, f"获取分享文件失败: {str(files)[:200]}"
        file_ids: list[str] = []
        for item in (files.get("data") or {}).get("list") or []:
            fid = item.get("file_id") or item.get("fid")
            if fid:
                file_ids.append(str(fid))
        if not file_ids:
            return False, "分享中没有可转存的文件"

        # 3) 转存
        result = self._post_json("receive", {
            "user_id": self.uid or share_user_id,
            "share_code": share_code,
            "file_id": ",".join(file_ids),
            "cid": str(cid),
        })
        if result.get("state"):
            return True, f"转存 {len(file_ids)} 个文件成功"
        # 常见“已转存/重复转存”也视为成功，避免反复重试
        err_text = str(result.get("error") or result)[:200]
        if "已转存" in err_text or "重复" in err_text:
            return True, "文件已存在于目标目录（重复转存）"
        return False, f"转存失败: {err_text}"
