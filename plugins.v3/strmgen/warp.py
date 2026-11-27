"""MediaWarp 中间件集成：下载二进制、生成配置、管理进程。

复用 DDSRem-Dev/MediaWarp 的 Go 二进制，为 Emby/Jellyfin 提供
HTTPStrm/AlistStrm 直链重定向、共同观影与客户端黑名单能力。
"""

import os
import platform
import shutil
import tarfile
import tempfile
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Dict, Generator, List, Optional

import psutil
import requests
from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter

from app.sdk.config import settings
from app.sdk.logging import logger

# MediaWarp 二进制版本，与 mediawarp 插件保持一致
MEDIAWARP_VERSION = "0.1.12"


class MediaWarpManager:
    """下载、配置并运行 MediaWarp 二进制，提供 Emby/Jellyfin 中间件能力。"""

    def __init__(self, plugin_class_name: str) -> None:
        """按插件类名规划二进制、配置与日志目录。"""
        base = settings.PLUGIN_DATA_PATH / plugin_class_name.lower()
        self._base = base
        self._binary_path = base / "MediaWarp"
        self._config_dir = base / "config"
        self._logs_dir = base / "logs"
        self._config_path = self._config_dir / "config.yaml"
        self._version_path = base / "version.txt"
        self.process: Optional[psutil.Process] = None

    # ------------------------------------------------------------------ #
    # 下载与安装
    # ------------------------------------------------------------------ #
    @staticmethod
    def _download_url() -> str:
        """按当前系统与架构构造 MediaWarp 下载地址。"""
        base_url = (
            "https://github.com/DDSRem-Dev/MediaWarp/releases/download/"
            "v{version}/MediaWarp_{version}_{os}_{arch}.tar.gz"
        )
        machine = platform.machine().lower()
        arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
        os_name = "darwin" if platform.system().lower() == "darwin" else "linux"
        return base_url.format(version=MEDIAWARP_VERSION, arch=arch, os=os_name)

    def _download_and_extract(self) -> None:
        """下载 MediaWarp 压缩包并原子落盘二进制与示例配置。"""
        url = self._download_url()
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, "MediaWarp.tar.gz")
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"【Strm Media】正在下载 MediaWarp 二进制: {url}")
            response = requests.get(
                url,
                stream=True,
                proxies=getattr(settings, "PROXY", None),
                timeout=60,
            )
            response.raise_for_status()
            with open(temp_file, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.info("【Strm Media】正在解压 MediaWarp ...")
            with tarfile.open(temp_file, "r:gz") as tar:
                binary_members = [
                    m for m in tar.getmembers() if m.name.endswith("MediaWarp")
                ]
                if not binary_members:
                    raise ValueError("下载的压缩包中缺少 MediaWarp 二进制文件")
                tar.extract(member=binary_members[0], path=temp_dir)
                extracted = Path(temp_dir) / binary_members[0].name
                extracted.chmod(0o755)
                self._atomic_copy(extracted, self._binary_path)

                if not self._config_path.exists():
                    example = [
                        m
                        for m in tar.getmembers()
                        if m.name.endswith("config.yaml.example")
                    ]
                    if example:
                        tar.extract(member=example[0], path=temp_dir)
                        self._atomic_copy(
                            Path(temp_dir) / example[0].name, self._config_path
                        )
                        logger.info(f"示例配置文件已保存到 {self._config_path}")

            with self._atomic_write(self._version_path) as f:
                f.write(MEDIAWARP_VERSION)
            logger.info(f"【Strm Media】MediaWarp 已安装到 {self._binary_path}")
        except Exception as e:
            logger.error(f"【Strm Media】MediaWarp 安装失败: {e}", exc_info=True)
        finally:
            shutil.rmtree(temp_dir)

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def ensure_binary(self) -> bool:
        """确保 MediaWarp 二进制存在且版本匹配，返回是否可用。"""
        if not self._binary_path.exists():
            logger.info("【Strm Media】尝试自动下载 MediaWarp 二进制 ...")
            self._download_and_extract()
            if not self._binary_path.exists():
                logger.error(
                    "【Strm Media】下载失败，MediaWarp 二进制不存在，无法启动中间件"
                )
                logger.info(
                    f"请将 MediaWarp 二进制放入 {self._base} 目录后重试"
                )
                return False

        if self._version_path.exists():
            version = self._version_path.read_text(encoding="utf-8").strip()
            if version != MEDIAWARP_VERSION:
                logger.info("【Strm Media】检测到 MediaWarp 版本变化，尝试更新 ...")
                self._download_and_extract()
        return self._binary_path.exists()

    def write_config(self, config: Dict[str, Any]) -> None:
        """按传入字段更新 config.yaml，保留已有注释与未涉及项。"""
        changes = {
            "Port": config.get("port") or "",
            "Logger.AccessLogger.File": True,
            "Logger.AccessLogger.Console": False,
            "MediaServer.Type": (
                "Jellyfin"
                if str(config.get("emby_type") or "").lower() == "jellyfin"
                else "Emby"
            ),
            "MediaServer.ADDR": config.get("emby_host") or "",
            "MediaServer.AUTH": config.get("emby_apikey") or "",
            # 本插件只整合直链重定向、共同观影与客户端过滤，
            # 模板里默认开启的 Web 美化与 SRT2ASS 一律关闭。
            "Web.Enable": False,
            "Web.Custom": False,
            "Web.Crx": False,
            "Web.Danmaku": False,
            "Web.ExternalPlayerUrl": False,
            "Subtitle.Enable": False,
            "Web.VideoTogether": bool(config.get("video_together")),
            "ClientFilter.Enable": bool(config.get("client_filter_enabled")),
            "ClientFilter.Mode": (
                "WhiteList"
                if str(config.get("client_filter_mode") or "").lower() == "whitelist"
                else "BlackList"
            ),
            "ClientFilter.ClientList": config.get("client_filter_list") or [],
            "HTTPStrm.Enable": bool(config.get("httpstrm_enabled")),
            "HTTPStrm.TransCode": bool(config.get("httpstrm_transcode")),
            "HTTPStrm.FinalURL": bool(config.get("httpstrm_final_url")),
            "HTTPStrm.PrefixList": config.get("httpstrm_prefix_list") or [],
            "AlistStrm.Enable": bool(config.get("aliststrm_enabled")),
            "AlistStrm.TransCode": bool(config.get("aliststrm_transcode")),
            "AlistStrm.RawURL": bool(config.get("aliststrm_raw_url")),
            "AlistStrm.List": config.get("aliststrm_list") or [],
        }
        self._modify_config(self._config_path, changes)

    # ------------------------------------------------------------------ #
    # 进程管理
    # ------------------------------------------------------------------ #
    def start(self, config: Dict[str, Any]) -> bool:
        """确保二进制、写入配置并启动 MediaWarp 进程，返回是否成功启动。"""
        if not self.ensure_binary():
            return False
        self.write_config(config)
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        self.process = psutil.Popen([str(self._binary_path)])
        if self.process.is_running():
            logger.info("【Strm Media】MediaWarp 中间件服务成功启动！")
            return True
        logger.error(
            "【Strm Media】MediaWarp 进程启动后已退出，请检查 MediaWarp 日志和配置"
        )
        return False

    def stop(self) -> None:
        """终止 MediaWarp 进程。"""
        if self.process and self.process.is_running():
            try:
                self.process.terminate()
            except Exception as e:
                logger.error(f"【Strm Media】退出 MediaWarp 进程失败：{e}")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    @contextmanager
    def _atomic_write(
        target: Path, binary: bool = False
    ) -> Generator[IO[Any], None, None]:
        """写入同目录临时文件，落盘成功后原子替换目标文件。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb" if binary else "w",
            encoding=None if binary else "utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            try:
                if target.exists():
                    shutil.copymode(target, temp_path)
                yield file
                file.flush()
                os.fsync(file.fileno())
                file.close()
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)

    def _atomic_copy(self, source: Path, target: Path) -> None:
        """原子复制文件并保留源文件的权限和时间戳。"""
        with (
            source.open("rb") as source_file,
            self._atomic_write(target, binary=True) as target_file,
        ):
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            shutil.copystat(source, target_file.name)

    def _modify_config(
        self, config_path: Path, modifications: Dict[str, Any]
    ) -> None:
        """按点分路径修改 config.yaml，保留注释；空文件按新结构重建。"""
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)

        def represent_bool(self, data):
            if data:
                return self.represent_scalar("tag:yaml.org,2002:bool", "True")
            return self.represent_scalar("tag:yaml.org,2002:bool", "False")

        RoundTripRepresenter.add_representer(bool, represent_bool)

        config = None
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as file:
                config = yaml.load(file)
        if config is None:
            logger.warning("【Strm Media】MediaWarp 配置文件为空或不存在，将按插件设置重建")
            config = {}
        if not isinstance(config, MutableMapping):
            raise TypeError("MediaWarp 配置文件根节点必须是映射")

        for key, value in modifications.items():
            keys = key.split(".")
            current = config
            for k in keys[:-1]:
                if current.get(k) is None:
                    current[k] = {}
                current = current[k]
                if not isinstance(current, MutableMapping):
                    raise TypeError(f"MediaWarp 配置项 {key} 的父节点必须是映射")
            current[keys[-1]] = value

        with self._atomic_write(config_path) as file:
            yaml.dump(config, file)
