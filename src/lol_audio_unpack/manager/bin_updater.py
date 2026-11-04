"""本地与预处理游戏资源的 BIN 更新辅助逻辑。

该模块负责协调 BIN 来源选择、资源提取，以及更新流程中的
元数据刷新。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from lol_audio_unpack.app.game_version import resolve_game_version
from lol_audio_unpack.app.targets import should_hide_champion_by_default, with_common_map
from lol_audio_unpack.manager.bin_source import BinSource
from lol_audio_unpack.manager.champion_bin_processor import ChampionBinProcessor
from lol_audio_unpack.manager.files import read_data
from lol_audio_unpack.manager.map_bin_processor import MapBinProcessor
from lol_audio_unpack.manager.update_result import UpdateEntityResult, UpdateStatus
from lol_audio_unpack.model.progress import OperationProgress
from lol_audio_unpack.utils.logging import performance_monitor

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext


class BinUpdater:
    """
    负责从BIN文件提取音频数据并更新到数据文件中

    支持可选的事件处理：设置 process_events=False 可显著提升处理速度，但不会生成事件数据
    """

    def __init__(
        self,
        force_update: bool = False,
        process_events: bool = True,
        *,
        ctx: AppContext,
        progress_callback: Callable[[OperationProgress], None] | None = None,
    ):
        """
        初始化BIN音频更新器

        :param force_update: 是否强制更新，忽略版本检查
        :param process_events: 是否处理事件数据（默认True，设置为False可大幅提升处理速度）
        :param ctx: 运行时上下文。
        """
        self.ctx = ctx
        self.game_path = Path(self.ctx.config.game_path)
        self.manifest_path = Path(self.ctx.paths.manifest_path)

        if not self.game_path or not self.manifest_path:
            raise ValueError("GAME_PATH 和 MANIFEST_PATH 必须在配置中设置")

        self.force_update = force_update
        self.process_events = process_events
        self.version: str = resolve_game_version(self.ctx)
        self.version_manifest_path: Path = self.ctx.version_path("manifest", self.version)
        self.data_file_base: Path = self.version_manifest_path / "data"
        self.champion_banks_dir: Path = self.version_manifest_path / "banks" / "champions"
        self.map_banks_dir: Path = self.version_manifest_path / "banks" / "maps"
        self.champion_events_dir: Path = self.version_manifest_path / "events" / "champions"
        self.map_events_dir: Path = self.version_manifest_path / "events" / "maps"
        self.languages: list[str] = []  # 在update()中初始化

        # 拆分后的协作对象：BIN 来源 + 英雄/地图处理器
        self.bin_source = BinSource(
            ctx=self.ctx,
            game_path=self.game_path,
            version=self.version,
            languages=self.languages,
        )
        self._champion_processor = ChampionBinProcessor(
            self.bin_source,
            ctx=self.ctx,
            version=self.version,
            force_update=self.force_update,
            process_events=self.process_events,
            game_path=self.game_path,
            champion_banks_dir=self.champion_banks_dir,
            champion_events_dir=self.champion_events_dir,
            progress_callback=progress_callback,
        )
        self._map_processor = MapBinProcessor(
            self.bin_source,
            ctx=self.ctx,
            version=self.version,
            force_update=self.force_update,
            process_events=self.process_events,
            map_banks_dir=self.map_banks_dir,
            map_events_dir=self.map_events_dir,
            languages=self.languages,
            progress_callback=progress_callback,
        )

    def _is_dev_mode(self) -> bool:
        """返回当前运行是否为开发模式。"""
        return bool(self.ctx.config.dev_mode)

    def _filter_default_champions(self, data: dict) -> dict:
        """过滤默认批量更新中应隐藏的特殊英雄。

        Args:
            data: 完整的聚合元数据。

        Returns:
            保留原元数据结构、仅替换英雄集合的新字典。
        """
        champions = data.get("champions", {})
        visible = {
            champion_id: champion
            for champion_id, champion in champions.items()
            if not should_hide_champion_by_default(champion)
        }
        return {**data, "champions": visible}

    @staticmethod
    def _log_completion(
        results: list[UpdateEntityResult],
        *,
        mode: str,
        champion_count: int,
        map_count: int,
    ) -> None:
        """按真实逐实体结果记录更新摘要，避免部分失败被标记为成功。"""
        failed_count = sum(result.status is UpdateStatus.FAILED for result in results)
        partial_count = sum(result.status is UpdateStatus.PARTIAL for result in results)
        summary = f"英雄 {champion_count} 个，地图 {map_count} 个"
        if failed_count or partial_count:
            logger.warning(
                f"BinUpdater 更新完成（{mode}）：{summary}，部分成功 {partial_count} 个，失败 {failed_count} 个"
            )
            return

    @performance_monitor(level="TRACE")
    def update(
        self,
        target: str = "all",
        champion_ids: list[str] | None = None,
        map_ids: list[str] | None = None,
    ) -> tuple[UpdateEntityResult, ...]:
        """处理 BIN 文件并返回逐实体结果。

        Args:
            target: 批量处理目标；显式 ID 存在时忽略。
            champion_ids: 指定英雄 ID；``None`` 表示未限定。
            map_ids: 指定地图 ID；显式范围会自动包含 Map 0。

        Returns:
            按英雄、地图处理顺序排列的逐实体结果。
        """
        data = read_data(self.data_file_base, dev_mode=self._is_dev_mode())
        if not data:
            logger.error(f"数据文件不存在，请先运行DataUpdater: {self.data_file_base}")
            raise FileNotFoundError(f"数据文件不存在: {self.data_file_base}")

        self.languages = data.get("metadata", {}).get("languages", [])
        # 将运行期解析出的语言列表同步给协作对象
        self.bin_source.languages = self.languages
        self._map_processor.languages = self.languages
        results: list[UpdateEntityResult] = []
        normalized_map_ids = with_common_map(map_ids, "0")
        map_ids = list(normalized_map_ids) if normalized_map_ids is not None else None

        # 根据传入的IDs构建筛选后的数据
        if champion_ids is not None or map_ids is not None:
            # 精确模式：根据具体ID筛选数据
            filtered_data = self._filter_data_by_ids(data, champion_ids, map_ids)
            champion_count = len(filtered_data.get("champions", {}))
            map_count = len(filtered_data.get("maps", {}))
            if champion_ids and filtered_data.get("champions"):
                results.extend(self._champion_processor._update_champions(filtered_data))
            if map_ids and filtered_data.get("maps"):
                results.extend(self._map_processor._update_maps(filtered_data))
            self._log_completion(
                results,
                mode="精确模式",
                champion_count=champion_count,
                map_count=map_count,
            )
        else:
            # 批量模式：使用target控制
            batch_data = self._filter_default_champions(data) if target in ["skin", "all"] else data
            champion_count = len(batch_data.get("champions", {})) if target in ["skin", "all"] else 0
            hidden_count = len(data.get("champions", {})) - champion_count if target in ["skin", "all"] else 0
            map_count = len(data.get("maps", {})) if target in ["map", "all"] else 0
            if hidden_count:
                logger.debug(f"默认批量更新已排除 {hidden_count} 个隐藏英雄实体")
            if target in ["skin", "all"]:
                results.extend(self._champion_processor._update_champions(batch_data))
            if target in ["map", "all"]:
                results.extend(self._map_processor._update_maps(data))
            self._log_completion(
                results,
                mode="批量模式",
                champion_count=champion_count,
                map_count=map_count,
            )
        return tuple(results)

    def _filter_data_by_ids(self, data: dict, champion_ids: list[str] | None, map_ids: list[str] | None) -> dict:
        """
        根据指定的ID列表筛选数据

        :param data: 完整的数据字典
        :param champion_ids: 要筛选的英雄ID列表
        :param map_ids: 要筛选的地图ID列表
        :returns: 筛选后的数据字典，保持原有结构
        """
        filtered_data: dict = {
            # 其他基础字段保持不变
        }

        # 筛选英雄数据
        if champion_ids:
            all_champions = data.get("champions", {})
            filtered_champions = {}
            for champion_id in champion_ids:
                if champion_id in all_champions:
                    filtered_champions[champion_id] = all_champions[champion_id]
                else:
                    logger.warning(f"指定的英雄ID {champion_id} 在数据中不存在")
            if filtered_champions:
                filtered_data["champions"] = filtered_champions

        # 筛选地图数据
        if map_ids:
            all_maps = data.get("maps", {})
            filtered_maps = {}
            for map_id in map_ids:
                if map_id in all_maps:
                    filtered_maps[map_id] = all_maps[map_id]
                else:
                    logger.warning(f"指定的地图ID {map_id} 在数据中不存在")
            if filtered_maps:
                filtered_data["maps"] = filtered_maps

        return filtered_data
