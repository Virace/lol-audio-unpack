"""提供 GUI 共享目录与轻量实体数据的后台扫描线程。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import Qt, QThread, Signal, Slot

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext

from lol_audio_unpack.gui.service.data_loader import EntityDataLoader, build_scan_failure_result
from lol_audio_unpack.gui.shared_data import SharedDataProblemCode
from lol_audio_unpack.manager.errors import is_shared_data_not_ready


class SharedDataScanWorker(QThread):
    """在单一 generation 中生成完整类型化共享目录快照。"""

    result_ready = Signal(object)
    progress = Signal(object)
    error = Signal(object)

    def __init__(self, app_context: AppContext, generation: int, *, require_resources: bool = False):
        """初始化完整目录扫描线程。

        Args:
            app_context: 当前有效应用上下文。
            generation: 结果所属的上下文代数。
            require_resources: 是否同时要求资源绑定和事件缓存完整。
        """
        super().__init__()
        self.app_context = app_context
        self.generation = generation
        self.require_resources = require_resources
        self._result = None
        self._problem = None
        # 控制器收到结果后会释放线程引用，必须等原生 finished 后才交付。
        self.finished.connect(self._publish_result, Qt.ConnectionType.QueuedConnection)

    def run(self) -> None:
        """保存扫描结果或问题，待线程退出后通过对应信号交付。"""
        logger.debug(f"SharedDataScanWorker 线程启动: generation={self.generation}")
        try:
            loader = EntityDataLoader(self.app_context)
            result = loader.scan_catalog(
                self.generation,
                progress=self.progress.emit,
                require_resources=self.require_resources,
            )
        except Exception as exc:  # noqa: BLE001
            result = build_scan_failure_result(self.generation, exc)
            problem = result.problems[0]
            if problem.code is SharedDataProblemCode.UNEXPECTED:
                logger.opt(exception=exc).error(f"共享实体目录扫描发生未预期失败: generation={self.generation}")
                self._problem = problem
                return
            if problem.code is SharedDataProblemCode.DATASET_MISSING:
                logger.debug("基础实体数据尚未生成：generation={}", self.generation)
            else:
                logger.opt(exception=exc).warning("无法读取实体目录：{}", problem.message)
        self._result = result

    @Slot()
    def _publish_result(self) -> None:
        """线程结束后在 GUI 线程交付结果，允许接收方安全释放 worker。"""
        logger.debug(f"SharedDataScanWorker 线程已结束: generation={self.generation}")
        if self._problem is not None:
            self.error.emit(self._problem)
        elif self._result is not None:
            self.result_ready.emit(self._result)


class DataLoadWorker(QThread):
    """异步数据加载线程"""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, app_context: AppContext, entity_type: str):
        super().__init__()
        self.app_context = app_context
        self.entity_type = entity_type

    def run(self):
        logger.debug(f"DataLoadWorker 线程启动: {self.entity_type}")
        try:
            loader = EntityDataLoader(self.app_context)
            logger.debug("EntityDataLoader 初始化成功")
            data = (
                loader.load_champion_catalog()
                if self.entity_type == "champion_catalog"
                else loader.load_entities(self.entity_type)
            )
            item_count = sum(len(rows) for rows in data.values()) if isinstance(data, dict) else len(data)
            logger.debug(f"{self.entity_type} 实体状态扫描完成，整理出 {item_count} 个列表项")
            self.finished.emit(data)
            logger.debug(f"finished 信号已发送: {self.entity_type}")
        except Exception as e:
            if is_shared_data_not_ready(e):
                logger.info(f"{self.entity_type} 共享实体数据暂不可用，交由后续流程决定是否自动准备: {e}")
            else:
                logger.opt(exception=True).error(f"{self.entity_type} 实体扫描失败: {e}")
            self.error.emit(str(e))
