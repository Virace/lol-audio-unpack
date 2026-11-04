"""在共享上下文后台构建中生成目录快照，不在 GUI 线程访问游戏文件。"""

from threading import Lock

from loguru import logger

from lol_audio_unpack.app.context import create_app_context
from lol_audio_unpack.app.lobby import repair_lobby
from lol_audio_unpack.app.source_info import SourceInfo, log_source_info, read_source_info
from lol_audio_unpack.app.types import AppContext
from lol_audio_unpack.manager.source_inventory import scan_inventory
from lol_audio_unpack.model.progress import OperationProgress
from lol_audio_unpack.utils.logging import log_paths


class SourceContextBuilder:
    """为一个 GUI 会话保存来源诊断，各轮资源发现仍独立预检。"""

    def __init__(self) -> None:
        """创建窗口专属的来源状态。"""
        self._source: SourceInfo | None = None
        self._language = ""
        self._lock = Lock()

    def __call__(self, *, settings: dict, progress_callback=None) -> AppContext:
        """在后台构建上下文，只有切换目录时才重新读取安装诊断。"""
        ctx = create_app_context(settings=settings, allow_empty_language=True, progress_callback=progress_callback)
        root = ctx.game_path.resolve()
        # 旧 generation 的后台构建可能尚未退出，来源读取和播报须作为同一步完成。
        with self._lock:
            log_paths(root, ctx.config.output_path)
            new_source = self._source is None or self._source.layout.root != root
            if new_source:
                self._source = read_source_info(root)
                self._language = ""
                log_source_info(self._source)
            language = "en_US" if ctx.game_region.casefold() == "default" else ctx.game_region
            if language and language != self._language:
                logger.info("资源语言：{}", language)
            self._language = language
        if progress_callback:
            progress_callback(OperationProgress("startup", "source_inventory", "started"))
        if new_source:
            logger.info("正在扫描本地资源……")
        inventory = scan_inventory(ctx.config.game_path)
        ctx.runtime_cache["source_inventory"] = inventory
        if new_source:
            logger.info("本地资源扫描完成：检测到 {} 种资源语言", len(inventory.languages))
        if ctx.game_region and inventory.get_language(ctx.game_region) is not None:
            repair_lobby(ctx)
        return ctx
