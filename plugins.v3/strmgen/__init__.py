"""strm生成 插件主入口。"""

from typing import Any, Dict, List, Optional

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

from .api import Api
from .core import DEFAULT_VIDEO_EXTENSIONS, StrmCore, StrmGenError
from .version import VERSION


class StrmGen(_PluginBase):
    """监控指定 115 网盘目录，定时扫描并自动生成 STRM 到本地目录。"""

    plugin_name = "strm生成"
    plugin_desc = (
        "监控指定 115 网盘目录，定时递归扫描并自动生成 STRM 文件到本地输出目录，"
        "播放时通过本插件的 302 接口实时获取 115 直链。"
    )
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png"
    plugin_version = VERSION
    plugin_author = "zf-ui"
    plugin_label = "网盘工具"
    plugin_order = 99
    auth_level = 1

    _enabled = False
    _cookies = ""
    _source_paths = ""
    _dest_strm = ""
    _moviepilot_address = ""
    _cron = ""
    _video_extensions = DEFAULT_VIDEO_EXTENSIONS
    _delete_strm = False

    def init_plugin(self, config: dict = None) -> None:
        """初始化插件，读取并规范化配置。"""
        if config:
            self._enabled = bool(config.get("enabled"))
            self._cookies = str(config.get("cookies") or "").strip()
            self._source_paths = str(config.get("source_paths") or "").strip()
            self._dest_strm = str(config.get("dest_strm") or "").strip()
            self._moviepilot_address = str(config.get("moviepilot_address") or "").strip()
            self._cron = str(config.get("cron") or "").strip()
            self._video_extensions = str(config.get("video_extensions") or "").strip()
            self._delete_strm = bool(config.get("delete_strm"))
        self._moviepilot_address = self._moviepilot_address or str(
                settings.APP_DOMAIN or ""
            ).strip().rstrip("/")
        # 同步最新配置到 API 模块，供 302 端点使用
        Api.instance = self
        Api.core = StrmCore(
            cookie=self._cookies,
            base_url=self._moviepilot_address,
            dest_strm=self._dest_strm,
            video_extensions=self._video_extensions,
            delete_strm=self._delete_strm,
        )
        # 移除可能残留的一次性手动扫描任务
        self.remove_manual_scan_job()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """插件不注册聊天命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API 端点。"""
        return [
            {
                "path": "/redirect_url",
                "endpoint": Api.redirect_url_get,
                "methods": ["GET"],
                "summary": "115网盘302跳转",
                "description": "按 pickcode 实时获取 115 下载直链并 302 跳转",
                "allow_anonymous": True,
            },
            {
                "path": "/redirect_url",
                "endpoint": Api.redirect_url_post,
                "methods": ["POST"],
                "summary": "115网盘302跳转",
                "description": "按 pickcode 实时获取 115 下载直链并 302 跳转",
                "allow_anonymous": True,
            },
            {
                "path": "/status",
                "endpoint": Api.status_api,
                "methods": ["GET"],
                "summary": "查询状态",
                "description": "查询配置摘要与最近一次扫描统计",
                "auth": "bear",
            },
            {
                "path": "/manual_scan",
                "endpoint": Api.manual_scan_api,
                "methods": ["POST"],
                "summary": "手动扫描",
                "description": "立即触发一次后台扫描任务",
                "auth": "bear",
            },
        ]

    def get_service(self) -> List[Dict[str, str | Dict[Any, Any] | Any]] | None:
        """注册定时扫描服务。"""
        if (
            not self._enabled
            or not self._cookies
            or not self._cron
            or not self._source_paths
            or not self._dest_strm
        ):
            return None
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError as e:
            logger.error(f"【strm生成】定时表达式无效: {self._cron} ({e})")
            return None
        return [
            {
                "id": "StrmGen_scan",
                "name": "定时扫描115网盘生成STRM",
                "trigger": trigger,
                "func": self.scan,
                "kwargs": {},
            }
        ]

    @staticmethod
    def get_form() -> tuple[List[dict], Dict[str, Any]]:
        """Vue 联邦 UI 模式，返回空表单与默认配置。"""
        return [], StrmGen.get_default_config()

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        """返回默认配置。"""
        return {
            "enabled": False,
            "cookies": "",
            "source_paths": "/影视库/电视剧",
            "dest_strm": "/data/strm",
            "moviepilot_address": "",
            "cron": "0 4 * * *",
            "video_extensions": DEFAULT_VIDEO_EXTENSIONS,
            "delete_strm": False,
        }

    @staticmethod
    def get_page() -> List[dict]:
        """页面由 Vue 联邦组件渲染。"""
        return []

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        """返回 Vue 联邦前端渲染模式与 remoteEntry 目录。"""
        return "vue", "dist/assets"

    def get_config_snapshot(self) -> Dict[str, Any]:
        """返回脱敏配置摘要，供状态接口使用。"""
        cookies = self._cookies
        masked = (cookies[:8] + "..." + cookies[-6:]) if len(cookies) > 20 else (
            "已配置" if cookies else "未配置"
        )
        return {
            "enabled": self._enabled,
            "cookies": masked,
            "source_paths": self._source_paths,
            "dest_strm": self._dest_strm,
            "moviepilot_address": self._moviepilot_address,
            "cron": self._cron,
            "video_extensions": self._video_extensions,
            "delete_strm": self._delete_strm,
        }

    def _source_list(self) -> List[str]:
        """解析监控目录列表（按行或逗号分隔）。"""
        raw = (self._source_paths or "").replace(",", "\n")
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def schedule_manual_scan(self) -> Dict[str, Any]:
        """通过宿主调度器安排一次性手动扫描。"""
        ok = add_plugin_once_job(
            self.__class__.__name__,
            "StrmGen_manual_scan",
            self.scan,
            "strm生成-手动扫描",
            delay_seconds=1,
        )
        if ok:
            logger.info("【strm生成】已提交手动扫描任务")
            return {"code": 0, "msg": "手动扫描任务已提交，请稍后查看状态"}
        return {"code": 500, "msg": "手动扫描任务提交失败，调度器不可用"}

    def remove_manual_scan_job(self) -> None:
        """清理一次性手动扫描任务。"""
        remove_plugin_once_job(self.__class__.__name__, "StrmGen_manual_scan")

    def stop_service(self) -> None:
        """停止插件服务。"""
        self.remove_manual_scan_job()

    def scan(self) -> None:
        """执行一次扫描并持久化统计。"""
        if not Api.core:
            logger.error("【strm生成】核心组件未初始化，跳过扫描")
            return
        sources = self._source_list()
        if not sources:
            logger.warning("【strm生成】未配置监控目录，跳过扫描")
            return
        try:
            stats = Api.core.scan(sources)
        except StrmGenError as e:
            logger.error(f"【strm生成】扫描中止: {e}")
            stats = {"created": 0, "skipped": 0, "cloud_files": 0, "deleted": 0,
                     "error": str(e)}
        except Exception as e:
            logger.error(f"【strm生成】扫描异常: {e}", exc_info=True)
            stats = {"created": 0, "skipped": 0, "cloud_files": 0, "deleted": 0,
                     "error": str(e)}
        from datetime import datetime
        stats["time"] = datetime.now().isoformat(timespec="seconds")
        self.save_data("last_scan", stats)
        logger.info(
            f"【strm生成】扫描完成: 云端 {stats.get('cloud_files')}，"
            f"新增 {stats.get('created')}，已存在 {stats.get('skipped')}，"
            f"删除 {stats.get('deleted')}"
        )
        self._notify_scan_result(stats)

    def _notify_scan_result(self, stats: Dict[str, Any]) -> None:
        """扫描完成后发送系统通知。"""
        from app.schemas.types import MessageType

        created = int(stats.get("created") or 0)
        skipped = int(stats.get("skipped") or 0)
        deleted = int(stats.get("deleted") or 0)
        errors = [
            p for p in (stats.get("sources") or [])
            if p.get("error")
        ]
        error_text = stats.get("error") or ""
        failed = len(errors) + (1 if error_text else 0)
        text = (
            f"📄 生成STRM文件 {created} 个\n"
            f"⏭️ 跳过已存在 {skipped} 个\n"
            f"🧹 删除失效 {deleted} 个\n"
            f"❌ 扫描失败 {failed} 个"
        )
        if failed:
            detail = "; ".join(p.get("source", "?") for p in errors) or error_text
            text += f"\n⚠️ 失败详情: {detail}"
        try:
            self.post_message(
                mtype=MessageType.Plugin,
                title=f"✅【strm生成插件】扫描生成 STRM 文件完成",
                text=text,
            )
        except Exception as e:
            logger.error(f"【strm生成】发送扫描结果通知失败: {e}")
