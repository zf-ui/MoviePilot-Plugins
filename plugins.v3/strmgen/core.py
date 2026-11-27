"""115 网盘目录扫描与 STRM 文件生成核心逻辑。"""

import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from app.sdk.logging import logger

from p115client import P115Client, check_response
from p115client.tool import iter_files_skim

PICKCODE_IN_STRM_PATTERN = re.compile(r"pickcode=([0-9A-Za-z]{17})")
DEFAULT_VIDEO_EXTENSIONS = (
    "mp4,mkv,ts,iso,avi,wmv,flv,rmvb,m2ts,mov,webm,mpg,mpeg,3gp,vob"
)


class StrmGenError(Exception):
    """STRM 生成过程中的可预期错误。"""


class StrmCore:
    """115 网盘目录扫描与 STRM 文件生成核心。"""

    def __init__(
        self,
        cookie: str,
        base_url: str,
        dest_strm: str,
        video_extensions: str,
        delete_strm: bool,
    ) -> None:
        """初始化核心组件，配置项为空时延迟到扫描时才报错。"""
        self._cookie = (cookie or "").strip()
        self._base_url = (base_url or "").strip().rstrip("/")
        self._dest = Path((dest_strm or "").strip())
        exts = (video_extensions or "").strip().lower()
        self._exts = {
            e.strip().lstrip(".").lower()
            for e in exts.split(",")
            if e.strip()
        } or {
            e.strip()
            for e in DEFAULT_VIDEO_EXTENSIONS.split(",")
        }
        self._delete_strm = bool(delete_strm)
        self._client: Optional[P115Client] = None

    @property
    def redirect_base(self) -> str:
        """获取本插件 302 跳转接口的 URL 前缀。"""
        return f"{self._base_url}/api/v1/plugin/StrmGen/redirect_url"

    def get_client(self) -> P115Client:
        """获取（并缓存）115 客户端实例。"""
        if self._client is None:
            if not self._cookie:
                raise StrmGenError("未配置 115 Cookie，无法访问网盘")
            try:
                self._client = P115Client(self._cookie)
            except Exception as e:
                raise StrmGenError(f"初始化 115 客户端失败: {e}") from e
        return self._client

    def get_cid(self, path: str) -> int:
        """通过网盘路径获取目录 ID，根目录返回 0。

        :param path: 115 网盘目录路径，如 /影视库/电视剧
        :return: 目录 ID，-1 表示获取失败
        """
        path = str(path).strip()
        path = "/" + path.strip("/")
        if path == "/":
            return 0
        resp = self.get_client().fs_dir_getid(path)
        check_response(resp)
        pid = resp.get("id", -1)
        return int(pid) if pid is not None else -1

    def iter_pan_files(self, source: str) -> Iterator[Dict[str, Any]]:
        """递归遍历网盘目录，逐个产出标准化文件属性。"""
        cid = self.get_cid(source)
        if cid < 0:
            raise StrmGenError(f"网盘目录不存在或无法访问: {source}")
        yield from iter_files_skim(self.get_client(), cid, with_path=True)

    @staticmethod
    def normalize_source(source: str) -> str:
        """规范监控目录路径：保证以 / 开头、不以 / 结尾。"""
        path = "/" + str(source).strip().strip("/")
        return "" if path == "/" else path

    def strm_path_for(self, source: str, cloud_path: str, file_name: str) -> Optional[Path]:
        """计算云端文件对应的本地 STRM 输出路径。"""
        source = self.normalize_source(source)
        cloud_path = str(cloud_path or file_name)
        rel = cloud_path
        if cloud_path.startswith(source + "/"):
            rel = cloud_path[len(source) + 1:]
        elif cloud_path.startswith("/"):
            return None
        if not rel or rel.startswith("/"):
            return None
        stem = Path(file_name).stem or file_name
        return self._dest / Path(rel).parent / (stem + ".strm")

    def scan(self, sources: List[str]) -> Dict[str, Any]:
        """扫描所有监控目录，生成缺失的 STRM 并返回统计。"""
        if not self._dest or str(self._dest) == ".":
            raise StrmGenError("未配置本地 STRM 输出目录")
        if not self._base_url:
            raise StrmGenError("未配置 STRM 链接前缀（MoviePilot 地址）")

        stats: Dict[str, Any] = {
            "created": 0,
            "skipped": 0,
            "cloud_files": 0,
            "deleted": 0,
        }
        cloud_pickcodes: Set[str] = set()
        local_pickcodes: Set[str] = self._load_local_pickcodes()
        per_source: List[Dict[str, Any]] = []

        for source in sources:
            source = self.normalize_source(source)
            if not source:
                continue
            created = skipped = cloud_files = 0
            try:
                for attr in self.iter_pan_files(source):
                    name = str(attr.get("name") or "")
                    if not name or attr.get("is_dir"):
                        continue
                    suffix = name.rpartition(".")[-1].lower()
                    if suffix not in self._exts:
                        continue
                    pickcode = str(attr.get("pickcode") or attr.get("pc") or "")
                    if not (len(pickcode) == 17 and pickcode.isalnum()):
                        continue
                    cloud_files += 1
                    cloud_pickcodes.add(pickcode)
                    # 本地任意路径已有该 pickcode 的 STRM（含其他插件生成的），跳过
                    if pickcode in local_pickcodes:
                        skipped += 1
                        continue
                    strm_path = self.strm_path_for(source, attr.get("path"), name)
                    if strm_path is None:
                        continue
                    if strm_path.exists():
                        skipped += 1
                        local_pickcodes.add(pickcode)
                        continue
                    strm_path.parent.mkdir(parents=True, exist_ok=True)
                    strm_path.write_text(
                        f"{self.redirect_base}?pickcode={pickcode}",
                        encoding="utf-8",
                    )
                    created += 1
                    local_pickcodes.add(pickcode)
            except StrmGenError:
                raise
            except Exception as e:
                logger.error(f"【Strm Media】扫描 {source} 失败: {e}", exc_info=True)
                per_source.append(
                    {"source": source, "created": created, "skipped": skipped,
                     "cloud_files": cloud_files, "error": str(e)}
                )
                continue
            per_source.append(
                {"source": source, "created": created, "skipped": skipped,
                 "cloud_files": cloud_files}
            )
            logger.info(
                f"【Strm Media】{source}: 云端视频 {cloud_files}，新增 {created}，已存在 {skipped}"
            )

        if self._delete_strm and self._dest.exists():
            stats["deleted"] = self._cleanup_stale_strm(cloud_pickcodes)

        stats["created"] = sum(p["created"] for p in per_source)
        stats["skipped"] = sum(p["skipped"] for p in per_source)
        stats["cloud_files"] = sum(p["cloud_files"] for p in per_source)
        stats["sources"] = per_source
        return stats

    def _load_local_pickcodes(self) -> Set[str]:
        """扫描本地输出目录，收集所有已存在 STRM 中记录的 pickcode。

        覆盖任意插件生成的 STRM（通过 URL 中的 pickcode 参数识别），
        用于在生成前判断本地是否已存在同一云端文件。
        """
        codes: Set[str] = set()
        if not self._dest.exists():
            return codes
        for strm_path in self._dest.rglob("*.strm"):
            try:
                content = strm_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = PICKCODE_IN_STRM_PATTERN.search(content)
            if match:
                codes.add(match.group(1))
        return codes

    def _cleanup_stale_strm(self, cloud_pickcodes: Set[str]) -> int:
        """删除云端已不存在的 STRM 文件，返回删除数量。"""
        deleted = 0
        for strm_path in self._dest.rglob("*.strm"):
            try:
                content = strm_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = PICKCODE_IN_STRM_PATTERN.search(content)
            if match and match.group(1) not in cloud_pickcodes:
                try:
                    strm_path.unlink()
                    deleted += 1
                    logger.info(f"【Strm Media】云端已不存在，删除 STRM: {strm_path}")
                except OSError as e:
                    logger.error(f"【Strm Media】删除 STRM 失败 {strm_path}: {e}")
        return deleted

    def get_download_url(self, pickcode: str, user_agent: str) -> Tuple[str, str]:
        """获取 115 下载直链，返回 (url, file_name)。"""
        if not (len(pickcode) == 17 and pickcode.isalnum()):
            raise StrmGenError(f"无效 pickcode: {pickcode}")
        client = self.get_client()
        url = client.download_url(pickcode.lower(), user_agent=user_agent or None)
        url_str = str(url)
        from urllib.parse import unquote, urlsplit
        file_name = unquote(urlsplit(url_str).path.rpartition("/")[-1])
        return url_str, file_name
