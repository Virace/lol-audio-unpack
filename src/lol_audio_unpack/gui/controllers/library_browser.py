"""管理总览本地版本发现与独立目录加载，不改变任务使用的客户端。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, QThreadPool, Signal

from lol_audio_unpack.app.game_version import get_game_version
from lol_audio_unpack.app.local_library import LibraryVersion, discover_versions
from lol_audio_unpack.gui.service.library_loader import LibraryDataLoader
from lol_audio_unpack.gui.workers import TaskWorker


class LibraryBrowser(QObject):
    """持有当前浏览选择，并隔离版本切换前的异步结果。"""

    options_changed = Signal()
    selection_changed = Signal()
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None):
        """初始化只读浏览状态与单线程读取队列。"""
        super().__init__(parent)
        self.root: Path | None = None
        self.source: LibraryVersion | None = None
        self.selection: LibraryVersion | None = None
        self.versions: tuple[LibraryVersion, ...] = ()
        self.loader: LibraryDataLoader | None = None
        self.loading = False
        self._generation = 0
        self._scan_generation = 0
        self._workers = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def configure(self, root: Path) -> None:
        """配置输出库；目录变化时使旧预览和发现请求失效。"""
        root = Path(root).resolve()
        changed = self.root != root
        self.root = root
        if changed:
            self._generation += 1
            self.loader = None
            self.loading = False
            self.versions = ()
            if self.source is not None:
                self.selection = None
            self.selection_changed.emit()
        self.refresh()

    def set_source(self, context) -> None:
        """记录当前客户端身份；更换客户端时默认回到其完整目录。"""
        previous = self.source
        cache = getattr(context, "runtime_cache", {})
        version = cache.get("resolved_runtime_version", "")
        region = getattr(context, "game_region", "")
        if context is not None and region and not version:
            # 新建上下文尚未填充运行时版本；当前客户端选项不能依赖已有解包产物或后续扫描。
            try:
                version = get_game_version(context.game_path)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                logger.warning("无法读取当前客户端版本，暂仅提供本地音频浏览: {}", exc)
        self.source = LibraryVersion(version, region) if version and region else None
        self.options_changed.emit()
        if self.source is not None and previous != self.source:
            self._apply_selection(None)
        if self.root is not None:
            self.refresh()

    def refresh(self) -> None:
        """在后台发现有音频的版本，保留本次会话的有效浏览选择。"""
        if self.root is None:
            return
        self._scan_generation += 1
        token = self._scan_generation
        root = self.root
        logger.debug("开始发现本地音频版本")
        worker = TaskWorker(lambda: discover_versions(root))
        key = ("scan", token)
        self._workers[key] = worker

        def finish(versions):
            self._workers.pop(key, None)
            if token != self._scan_generation:
                return
            self.versions = versions
            self.options_changed.emit()
            logger.debug("本地音频版本发现完成: {} 个版本/语言", len(versions))
            if self.selection is not None and self.selection in versions:
                if self.loader is None and not self.loading:
                    self._apply_selection(self.selection)
            elif self.source is not None:
                if self.selection is not None:
                    self._apply_selection(None)
            elif versions:
                if self.selection != versions[0] or (self.loader is None and not self.loading):
                    self._apply_selection(versions[0])
            elif self.selection is not None:
                self._apply_selection(None)

        def fail(message):
            self._workers.pop(key, None)
            if token == self._scan_generation:
                logger.warning("本地音频版本发现失败: {}", message)
                self.failed.emit(f"无法读取本地版本：{message}")

        worker.signals.finished.connect(finish)
        worker.signals.failed.connect(fail)
        self._pool.start(worker)

    def select(self, selection: LibraryVersion | None) -> None:
        """切换会话内的浏览版本；同版同语言始终回到当前客户端。"""
        if selection == self.source:
            selection = None
        if selection != self.selection:
            self._apply_selection(selection)

    def _apply_selection(self, selection: LibraryVersion | None) -> None:
        """清空旧版本快照，并按加载代次接收新的本地目录。"""
        self._generation += 1
        token = self._generation
        self.selection = selection
        self.loader = None
        self.loading = selection is not None
        self.selection_changed.emit()
        if selection is None or self.root is None:
            self.loading = False
            return
        root = self.root
        logger.debug("开始读取本地音频目录: {} / {}", selection.version, selection.region)

        def load():
            loader = LibraryDataLoader(root, selection)
            rows = loader.load_champion_catalog()
            rows["maps"] = loader.load_entities("maps")
            return loader, rows

        worker = TaskWorker(load)
        key = ("load", token)
        self._workers[key] = worker

        def finish(result):
            self._workers.pop(key, None)
            if token != self._generation:
                return
            self.loader = result[0]
            self.loading = False
            logger.debug("本地音频目录读取完成: {} / {}", selection.version, selection.region)
            self.loaded.emit(result[1])

        def fail(message):
            self._workers.pop(key, None)
            if token != self._generation:
                return
            self.loading = False
            logger.warning("本地音频目录读取失败: {} / {}: {}", selection.version, selection.region, message)
            self.failed.emit(f"无法读取 {selection.version} 的本地数据：{message}")

        worker.signals.finished.connect(finish)
        worker.signals.failed.connect(fail)
        self._pool.start(worker)
