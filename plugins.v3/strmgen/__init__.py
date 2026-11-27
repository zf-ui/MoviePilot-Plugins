"""Strm Media 插件主入口。"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.helper.mediaserver import MediaServerHelper
from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job

from .api import Api
from .core import DEFAULT_VIDEO_EXTENSIONS, StrmCore, StrmGenError
from .version import VERSION
from .warp import MediaWarpManager


class StrmGen(_PluginBase):
    """115 网盘 STRM 媒体桥：定时生成 STRM，并经内置 MediaWarp 中间件 302 直链播放。"""

    plugin_name = "Strm Media"
    plugin_desc = (
        "115 网盘 STRM 媒体桥：定时扫描 115 目录生成 STRM 到本地；内置 MediaWarp 中间件，"
        "让 Emby/Jellyfin 播放 STRM 时 302 直链到 115，支持 HTTPStrm/AlistStrm、"
        "FinalURL、共同观影与客户端名单。"
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
    # MediaWarp 中间件
    _warp_enabled = False
    _warp_port = "9096"
    _warp_mediaserver = ""
    _media_strm_path = ""
    _httpstrm_enabled = True
    _httpstrm_final_url = True
    _aliststrm_enabled = False
    _alist_list = ""
    _video_together = False
    _client_filter_enabled = False
    _client_filter_mode = "BlackList"
    _client_filter_list = ""
    _warp_manager = None
    _warp_scheduler = None
    _emby_type = ""
    _emby_host = ""
    _emby_apikey = ""

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
            # MediaWarp 中间件
            self._warp_enabled = bool(config.get("warp_enabled", False))
            self._warp_port = str(config.get("warp_port") or "9096").strip()
            self._warp_mediaserver = str(config.get("warp_mediaserver") or "").strip()
            self._media_strm_path = str(config.get("media_strm_path") or "").strip()
            self._httpstrm_enabled = bool(config.get("httpstrm_enabled", True))
            self._httpstrm_final_url = bool(config.get("httpstrm_final_url", True))
            self._aliststrm_enabled = bool(config.get("aliststrm_enabled", False))
            self._alist_list = str(config.get("alist_list") or "").strip()
            self._video_together = bool(config.get("video_together", False))
            self._client_filter_enabled = bool(config.get("client_filter_enabled", False))
            self._client_filter_mode = str(config.get("client_filter_mode") or "BlackList").strip()
            self._client_filter_list = str(config.get("client_filter_list") or "").strip()
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
        # 启动或停止 MediaWarp 中间件
        self._setup_mediawarp()

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
                # HEAD 供 MediaWarp 的 FinalURL 探测使用（它用 HEAD 跟踪重定向链）
                "methods": ["GET", "HEAD"],
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
            logger.error(f"【Strm Media】定时表达式无效: {self._cron} ({e})")
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
            "warp_enabled": False,
            "warp_port": "9096",
            "warp_mediaserver": "",
            "media_strm_path": "/data",
            "httpstrm_enabled": True,
            "httpstrm_final_url": True,
            "aliststrm_enabled": False,
            "alist_list": "",
            "video_together": False,
            "client_filter_enabled": False,
            "client_filter_mode": "BlackList",
            "client_filter_list": "Fileball\nInfuse",
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
            "warp_enabled": self._warp_enabled,
            "warp_port": self._warp_port,
            "warp_mediaserver": self._warp_mediaserver,
            "video_together": self._video_together,
            "client_filter_enabled": self._client_filter_enabled,
            "client_filter_mode": self._client_filter_mode,
            "httpstrm_enabled": self._httpstrm_enabled,
            "aliststrm_enabled": self._aliststrm_enabled,
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
            "Strm Media-手动扫描",
            delay_seconds=1,
        )
        if ok:
            logger.info("【Strm Media】已提交手动扫描任务")
            return {"code": 0, "msg": "手动扫描任务已提交，请稍后查看状态"}
        return {"code": 500, "msg": "手动扫描任务提交失败，调度器不可用"}

    def remove_manual_scan_job(self) -> None:
        """清理一次性手动扫描任务。"""
        remove_plugin_once_job(self.__class__.__name__, "StrmGen_manual_scan")

    def stop_service(self) -> None:
        """停止插件服务。"""
        self.remove_manual_scan_job()
        self._stop_mediawarp()

    def _setup_mediawarp(self) -> None:
        """按配置启动或停止 MediaWarp 中间件。"""
        self._stop_mediawarp()
        self._resolve_mediawarp_server()
        if not (self._warp_enabled and self._warp_mediaserver):
            return
        self._warp_manager = MediaWarpManager(self.__class__.__name__)
        try:
            self._warp_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._warp_scheduler.add_job(
                func=self._start_mediawarp,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                + timedelta(seconds=2),
                name="StrmGen启动MediaWarp",
            )
            self._warp_scheduler.start()
        except Exception as e:
            logger.error(f"【Strm Media】MediaWarp 调度器启动失败: {e}", exc_info=True)

    def _resolve_mediawarp_server(self) -> None:
        """从 MoviePilot 媒体服务器配置解析 Emby/Jellyfin 连接信息。"""
        self._emby_type = ""
        self._emby_host = ""
        self._emby_apikey = ""
        if not self._warp_mediaserver:
            return
        try:
            helper = MediaServerHelper()
            services = helper.get_services(name_filters=[self._warp_mediaserver])
            for _, server in services.items():
                self._emby_type = server.type
                self._emby_apikey = server.config.config.get("apikey") or ""
                self._emby_host = server.config.config.get("host") or ""
                self._emby_host = self._emby_host.rstrip("/")
                if not self._emby_host.startswith("http"):
                    self._emby_host = "http://" + self._emby_host
                break
        except Exception as e:
            logger.error(f"【Strm Media】解析媒体服务器失败: {e}", exc_info=True)

    def _start_mediawarp(self) -> None:
        """启动 MediaWarp 进程，捕获启动异常避免影响调度器。"""
        try:
            self._warp_manager.start(self._mediawarp_config())
        except Exception:
            logger.error(
                "【Strm Media】MediaWarp 中间件启动失败，请检查配置与磁盘空间",
                exc_info=True,
            )

    def _stop_mediawarp(self) -> None:
        """停止 MediaWarp 调度器与进程。"""
        if self._warp_scheduler:
            try:
                self._warp_scheduler.remove_all_jobs()
                if self._warp_scheduler.running:
                    self._warp_scheduler.shutdown()
            except Exception as e:
                logger.error(f"【Strm Media】关闭 MediaWarp 调度器失败: {e}")
            self._warp_scheduler = None
        if self._warp_manager:
            self._warp_manager.stop()
            self._warp_manager = None

    def _mediawarp_config(self) -> Dict[str, Any]:
        """组装传给 MediaWarpManager 的中间件配置。"""
        return {
            "port": self._warp_port,
            "emby_type": self._emby_type,
            "emby_host": self._emby_host,
            "emby_apikey": self._emby_apikey,
            "video_together": self._video_together,
            "client_filter_enabled": self._client_filter_enabled,
            "client_filter_mode": self._client_filter_mode,
            "client_filter_list": self._client_filter_list.splitlines(),
            "httpstrm_enabled": self._httpstrm_enabled,
            "httpstrm_transcode": False,
            "httpstrm_final_url": self._httpstrm_final_url,
            "httpstrm_prefix_list": self._media_strm_path.splitlines(),
            "aliststrm_enabled": self._aliststrm_enabled,
            "aliststrm_transcode": True,
            "aliststrm_raw_url": False,
            "aliststrm_list": self._parse_alist_list(),
        }

    def _parse_alist_list(self) -> List[Dict[str, Any]]:
        """把用户填写的 Alist 列表 YAML 文本解析为列表。"""
        if not self._alist_list:
            return []
        try:
            from ruamel.yaml import YAML

            data = YAML().load(self._alist_list)
            if isinstance(data, list):
                return data
            logger.warning("【Strm Media】Alist 配置不是列表格式，已忽略")
            return []
        except Exception as e:
            logger.error(f"【Strm Media】解析 Alist 配置失败: {e}")
            return []

    def scan(self) -> None:
        """执行一次扫描并持久化统计。"""
        if not Api.core:
            logger.error("【Strm Media】核心组件未初始化，跳过扫描")
            return
        sources = self._source_list()
        if not sources:
            logger.warning("【Strm Media】未配置监控目录，跳过扫描")
            return
        try:
            stats = Api.core.scan(sources)
        except StrmGenError as e:
            logger.error(f"【Strm Media】扫描中止: {e}")
            stats = {"created": 0, "skipped": 0, "cloud_files": 0, "deleted": 0,
                     "error": str(e)}
        except Exception as e:
            logger.error(f"【Strm Media】扫描异常: {e}", exc_info=True)
            stats = {"created": 0, "skipped": 0, "cloud_files": 0, "deleted": 0,
                     "error": str(e)}
        from datetime import datetime
        stats["time"] = datetime.now().isoformat(timespec="seconds")
        self.save_data("last_scan", stats)
        logger.info(
            f"【Strm Media】扫描完成: 云端 {stats.get('cloud_files')}，"
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
                title=f"✅【Strm Media插件】扫描生成 STRM 文件完成",
                text=text,
            )
        except Exception as e:
            logger.error(f"【Strm Media】发送扫描结果通知失败: {e}")
