"""strm生成 插件对外 API 端点。"""

from asyncio import to_thread
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

from app.sdk.logging import logger

from .core import StrmCore


class Api:
    """strm生成 插件 API 集合。"""

    # 当前插件实例与核心组件引用，由 __init__.init_plugin 维护
    instance: Optional[Any] = None
    core: Optional[StrmCore] = None

    @staticmethod
    def _error(message: str, code: int = status.HTTP_500_INTERNAL_SERVER_ERROR) -> JSONResponse:
        """构造统一错误响应。"""
        return JSONResponse({"code": code, "msg": message}, status_code=code)

    @staticmethod
    async def _redirect_url_impl(
        request: Request,
        pickcode: str = "",
        file_name: str = "",
    ) -> Response:
        """115 网盘 302 跳转实现：按 pickcode 实时获取下载直链。"""
        user_agent = request.headers.get("User-Agent") or ""
        if not pickcode:
            return Api._error("Missing pickcode parameter", status.HTTP_400_BAD_REQUEST)
        if not (len(pickcode) == 17 and pickcode.isalnum()):
            return Api._error(
                f"Bad pickcode: {pickcode}", status.HTTP_400_BAD_REQUEST
            )
        core = Api.core
        if core is None:
            return Api._error("插件未完成初始化", status.HTTP_503_SERVICE_UNAVAILABLE)
        try:
            url, name = await to_thread(core.get_download_url, pickcode, user_agent)
        except Exception as e:
            logger.error(f"【strm生成】获取 115 下载地址失败 {pickcode}: {e}", exc_info=True)
            return Api._error(f"获取 115 下载地址失败: {e}")

        name = name or file_name
        try:
            name.encode("ascii")
            content_disposition = f'attachment; filename="{name}"'
        except UnicodeEncodeError:
            content_disposition = (
                f"attachment; filename*=UTF-8''{quote(name, safe='')}"
            )
        return Response(
            status_code=status.HTTP_302_FOUND,
            headers={
                "Location": quote(url, safe=":/?&=%#$+,@"),
                "Content-Disposition": content_disposition,
            },
        )

    @staticmethod
    async def redirect_url_get(
        request: Request,
        pickcode: str = "",
        file_name: str = "",
    ) -> Response:
        """115 网盘 302 跳转 (GET)。"""
        return await Api._redirect_url_impl(request, pickcode, file_name)

    @staticmethod
    async def redirect_url_post(
        request: Request,
        pickcode: str = "",
        file_name: str = "",
    ) -> Response:
        """115 网盘 302 跳转 (POST)。"""
        return await Api._redirect_url_impl(request, pickcode, file_name)

    @staticmethod
    def status_api() -> Dict[str, Any]:
        """查询插件配置摘要与最近一次扫描统计。"""
        inst = Api.instance
        if inst is None:
            return {"code": 500, "msg": "插件未完成初始化"}
        last_scan = inst.get_data("last_scan") or {}
        config = inst.get_config_snapshot()
        return {"code": 0, "config": config, "last_scan": last_scan}

    @staticmethod
    def manual_scan_api() -> Dict[str, Any]:
        """手动触发一次扫描（异步执行，立即返回）。"""
        inst = Api.instance
        if inst is None:
            return {"code": 500, "msg": "插件未完成初始化"}
        return inst.schedule_manual_scan()
