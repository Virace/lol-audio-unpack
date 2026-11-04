"""共享数据读取器。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from lol_audio_unpack.app.game_version import resolve_game_version
from lol_audio_unpack.app.resource_pack import parse_resource_pack_key, resource_pack_path_component
from lol_audio_unpack.app.targets import (
    filter_default_visible_champions,
    get_default_hidden_champion_markers,
    get_default_visible_champions,
    should_hide_champion_by_default,
)
from lol_audio_unpack.manager.errors import (
    DataVersionMismatchError,
    ResourceSchemaMismatchError,
    SharedDataCorruptError,
    SharedDataMissingError,
)
from lol_audio_unpack.manager.files import find_data_file, read_data
from lol_audio_unpack.model.binding import RESOURCE_SCHEMA_VERSION, ResourceBindings
from lol_audio_unpack.utils.logging import performance_monitor

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext


class DataReader:
    """读取单一应用上下文的结构化游戏数据与分散资源 artifact。

    每次构造都创建独立实例；实例内缓存只在其所属 app/context 生命周期内复用。
    """

    MAX_MINOR_DIFF = 2
    AUDIO_TYPE_VO = "VO"
    AUDIO_TYPE_SFX = "SFX"
    AUDIO_TYPE_MUSIC = "MUSIC"

    def __init__(self, ctx: AppContext, *, read_only: bool = False):
        """
        初始化数据读取器

        从合并后的数据文件和分散的banks/events文件中读取游戏数据

        Args:
            ctx: 运行时上下文。
            read_only: 浏览时禁止写回未知类别记录；所有读取统一使用新布局。
        """
        self.ctx = ctx
        self.game_path = Path(self.ctx.config.game_path)
        self.manifest_path = Path(self.ctx.paths.manifest_path)

        if not self.game_path or not self.manifest_path:
            raise ValueError("GAME_PATH 和 MANIFEST_PATH 必须在配置中设置")

        self.version: str = resolve_game_version(self.ctx)
        self.version_manifest_path: Path = self.ctx.version_path("manifest", self.version)
        self.read_only = read_only

        # 使用固定 MessagePack 路径，旧格式由独立工具转换；
        # 当前边界负责把缺失与损坏分类，避免底层和 GUI 重复记录 traceback。
        data_file_base = self.version_manifest_path / "data"
        actual_data_file = find_data_file(data_file_base, dev_mode=self.ctx.config.dev_mode)
        self.data = read_data(
            data_file_base,
            dev_mode=self.ctx.config.dev_mode,
            log_errors=False,
        )
        if not self.data:
            if actual_data_file is not None:
                raise SharedDataCorruptError(f"核心数据文件无法读取或内容为空: {actual_data_file}")
            raise SharedDataMissingError(
                "核心数据文件 data.msgpack 不存在，请先运行更新程序；"
                "仅有历史 YAML/JSON 时可使用 scripts/convert_data.py 离线转换。"
            )
        if (
            not isinstance(self.data, dict)
            or not isinstance(self.data.get("champions"), dict)
            or not isinstance(self.data.get("maps"), dict)
        ):
            raise SharedDataCorruptError("核心数据文件缺少有效的 champions 或 maps 目录。")

        # 校验数据版本
        self._validate_data_version()

        # 更新为新的分散式文件结构路径
        self.champion_banks_dir: Path = self.version_manifest_path / "banks" / "champions"
        self.champion_events_dir: Path = self.version_manifest_path / "events" / "champions"
        self.map_banks_dir: Path = self.version_manifest_path / "banks" / "maps"
        self.map_events_dir: Path = self.version_manifest_path / "events" / "maps"
        self.resource_pack_banks_dir: Path = self.version_manifest_path / "banks" / "resource_packs"
        self.resource_pack_events_dir: Path = self.version_manifest_path / "events" / "resource_packs"
        self.unknown_categories_file: Path = self.version_manifest_path / "unknown-category.txt"

        # 简单缓存机制避免重复读取
        self._champion_banks_cache: dict[int, dict] = {}
        self._champion_events_cache: dict[int, dict] = {}
        self._map_banks_cache: dict[int, dict] = {}
        self._map_events_cache: dict[int, dict] = {}
        self._resource_pack_banks_cache: dict[str, dict] = {}
        self._resource_pack_events_cache: dict[str, dict] = {}

        # 防御性开发：记录未知的音频分类
        self.unknown_categories: set[str] = set()

    def _validate_data_version(self) -> None:
        """
        校验加载的数据文件版本与当前游戏版本的兼容性。

        - 大版本不一致: 抛出致命错误，程序终止。
        - 小版本差距过大 (>2): 记录错误日志，但程序继续。
        - 小版本差距较小 (<=2): 记录警告日志，程序继续。
        - 构建号不同: 忽略。
        """
        data_version_str = self.data.get("metadata", {}).get("gameVersion")
        if not data_version_str:
            logger.warning("数据文件中缺少 'gameVersion' 字段，无法进行版本校验。")
            return

        # 分割版本号，例如 "15.14" -> ["15", "14"]
        current_parts = self.version.split(".")
        data_parts = data_version_str.split(".")

        if len(current_parts) < self.MAX_MINOR_DIFF or len(data_parts) < self.MAX_MINOR_DIFF:
            logger.error(f"版本号格式无效。当前游戏: '{self.version}', 数据文件: '{data_version_str}'")
            return

        # 1. 检查大版本 (Major version)
        if current_parts[0] != data_parts[0]:
            error_msg = (
                f"数据版本与游戏版本严重不匹配 (大版本不同)！\n"
                f"  - 当前游戏版本: {self.version}\n"
                f"  - 数据文件版本: {data_version_str}\n"
                f"请立即运行数据更新程序。"
            )
            logger.critical(error_msg)
            raise DataVersionMismatchError(error_msg)

        try:
            # 2. 检查小版本 (Minor version)
            current_minor = int(current_parts[1])
            data_minor = int(data_parts[1])
            minor_diff = abs(current_minor - data_minor)

            if minor_diff > 0:
                version_msg = (
                    f"数据版本与当前游戏版本存在差异。\n  - 游戏版本: {self.version}\n  - 数据版本: {data_version_str}"
                )
                if minor_diff > self.MAX_MINOR_DIFF:
                    logger.error(
                        f"{version_msg}\n小版本差距过大(>{self.MAX_MINOR_DIFF})，数据可能不准确，请立即更新数据。"
                    )
                else:
                    logger.warning(
                        f"{version_msg}\n小版本差距较小(≤{self.MAX_MINOR_DIFF})，数据有可能不准确，建议更新数据。"
                    )

        except ValueError:
            logger.opt(exception=True).error(
                f"解析版本号时出错。当前游戏: '{self.version}', 数据文件: '{data_version_str}'"
            )
            return

    def get_audio_type(self, category: str) -> str:
        """从分类字符串中识别出音频的大类（VO, SFX, MUSIC）"""
        category_upper = category.upper()
        if "ANNOUNCER" in category_upper or "_VO" in category_upper:
            return self.AUDIO_TYPE_VO
        if category_upper.startswith("MUS_") or "MUSIC" in category_upper:
            return self.AUDIO_TYPE_MUSIC
        if "_SFX" in category_upper or category_upper == "INIT" or "HUD" in category_upper:
            return self.AUDIO_TYPE_SFX

        self.unknown_categories.add(category)
        return self.AUDIO_TYPE_SFX

    def get_languages(self) -> list[str]:
        """获取支持的语言列表"""
        languages = self.data.get("metadata", {}).get("languages", [])
        languages_set = set(languages)
        languages_set.add("default")
        return list(languages_set)

    @performance_monitor(level="TRACE")
    def get_champion_banks(self, champion_id: int, *, require_bindings: bool = False) -> dict | None:
        """
        读取指定英雄的banks数据

        :param champion_id: 英雄ID
        :returns: 英雄banks数据字典，失败时返回None
        :rtype: dict | None
        """
        if champion_id in self._champion_banks_cache:
            banks_data = self._champion_banks_cache[champion_id]
            self._validate_resource_schema(banks_data, f"英雄 {champion_id}", require_bindings=require_bindings)
            return banks_data

        try:
            banks_file_base = self.champion_banks_dir / str(champion_id)
            banks_data = read_data(banks_file_base, dev_mode=self.ctx.config.dev_mode)
        except Exception:
            logger.opt(exception=True).error(f"读取英雄 banks 数据失败: champion_id={champion_id}")
            return None

        if banks_data:
            self._validate_resource_schema(banks_data, f"英雄 {champion_id}", require_bindings=require_bindings)
            self._champion_banks_cache[champion_id] = banks_data

        return banks_data

    def get_champion_resource_bindings(self, champion_id: int) -> ResourceBindings | None:
        """读取本地英雄 v2 resource bindings。"""
        payload = self.get_champion_banks(champion_id, require_bindings=True)
        return ResourceBindings.from_payload(payload) if payload else None

    @performance_monitor(level="TRACE")
    def write_unknown_categories(self) -> None:
        """将本次运行中收集到的所有未知分类写入到文件中"""
        if self.read_only or not self.unknown_categories:
            return

        try:
            existing_unknowns = set()
            if self.unknown_categories_file.exists():
                with open(self.unknown_categories_file, encoding="utf-8") as f:
                    existing_unknowns = {line.strip() for line in f if line.strip()}

            new_unknowns = self.unknown_categories - existing_unknowns
            if not new_unknowns:
                return

            with open(self.unknown_categories_file, "a", encoding="utf-8") as f:
                for category in sorted(list(new_unknowns)):
                    f.write(f"{category}\n")
            logger.info(f"已记录 {len(new_unknowns)} 个新的未知音频分类")
        except Exception as e:
            logger.opt(exception=True).error(f"写入未知分类文件时出错: {e}")

    @performance_monitor(level="TRACE")
    def get_champion_events(self, champion_id: int) -> dict | None:
        """
        读取指定英雄的events数据

        :param champion_id: 英雄ID
        :returns: 英雄events数据字典，失败时返回None
        :rtype: dict | None
        """
        if champion_id in self._champion_events_cache:
            return self._champion_events_cache[champion_id]

        try:
            events_file_base = self.champion_events_dir / str(champion_id)
            events_data = read_data(events_file_base, dev_mode=self.ctx.config.dev_mode)
        except Exception:
            logger.opt(exception=True).error(f"读取英雄 events 数据失败: champion_id={champion_id}")
            return None

        if events_data:
            self._champion_events_cache[champion_id] = events_data

        return events_data

    @performance_monitor(level="TRACE")
    def get_map_banks(self, map_id: int, *, require_bindings: bool = False) -> dict | None:
        """
        读取指定地图的banks数据

        :param map_id: 地图ID
        :returns: 地图banks数据字典，失败时返回None
        :rtype: dict | None
        """
        if map_id in self._map_banks_cache:
            banks_data = self._map_banks_cache[map_id]
            self._validate_resource_schema(banks_data, f"地图 {map_id}", require_bindings=require_bindings)
            return banks_data

        try:
            banks_file_base = self.map_banks_dir / str(map_id)
            banks_data = read_data(banks_file_base, dev_mode=self.ctx.config.dev_mode)
        except Exception:
            logger.opt(exception=True).error(f"读取地图 banks 数据失败: map_id={map_id}")
            return None

        if banks_data:
            self._validate_resource_schema(banks_data, f"地图 {map_id}", require_bindings=require_bindings)
            self._map_banks_cache[map_id] = banks_data

        return banks_data

    def get_map_resource_bindings(self, map_id: int) -> ResourceBindings | None:
        """读取本地地图 v2 resource bindings。"""
        payload = self.get_map_banks(map_id, require_bindings=True)
        return ResourceBindings.from_payload(payload) if payload else None

    @performance_monitor(level="TRACE")
    def get_resource_pack_banks(self, key: str, *, require_bindings: bool = False) -> dict | None:
        """读取指定 resource-pack 的 banks artifact。

        Args:
            key: canonical ``resource_pack:...`` 稳定 key。
            require_bindings: 为 ``True`` 时，本地模式拒绝不含 v2 bindings 的旧 artifact。

        Returns:
            resource-pack banks 字典；文件不存在或读取失败时返回 ``None``。
        """
        component = resource_pack_path_component(key)
        if key in self._resource_pack_banks_cache:
            banks_data = self._resource_pack_banks_cache[key]
            self._validate_resource_schema(banks_data, f"资源包 {key}", require_bindings=require_bindings)
            return banks_data

        try:
            banks_data = read_data(self.resource_pack_banks_dir / component, dev_mode=self.ctx.config.dev_mode)
        except Exception:
            logger.opt(exception=True).error(f"读取 resource pack banks 数据失败: key={key}")
            return None

        if banks_data:
            self._validate_resource_schema(banks_data, f"资源包 {key}", require_bindings=require_bindings)
            self._resource_pack_banks_cache[key] = banks_data
        return banks_data

    def get_resource_pack_resource_bindings(self, key: str) -> ResourceBindings | None:
        """读取本地 resource-pack v2 bindings。"""
        payload = self.get_resource_pack_banks(key, require_bindings=True)
        return ResourceBindings.from_payload(payload) if payload else None

    def list_resource_pack_banks(self) -> list[dict]:
        """枚举已持久化的 resource-pack banks artifact。

        本方法只读取 manifest 产物的 metadata，不会访问来源 WAD payload。

        Returns:
            按 canonical stable key 排序的有效 banks artifact 列表。
        """
        if not self.resource_pack_banks_dir.is_dir():
            return []

        bases = {
            path.with_suffix("")
            for path in self.resource_pack_banks_dir.iterdir()
            if path.suffix.casefold() == ".msgpack"
        }
        artifacts_by_key: dict[str, dict] = {}
        for base in sorted(bases, key=lambda item: item.name.casefold()):
            try:
                # 与其他 manifest artifact 共用固定 MessagePack 读取合同。
                payload = read_data(base, dev_mode=self.ctx.config.dev_mode)
            except Exception as exc:  # noqa: BLE001
                logger.warning("忽略无法读取的 resource-pack artifact {}: {}", base.name, exc)
                continue
            metadata = payload.get("resourcePack") if isinstance(payload, dict) else None
            key = metadata.get("key") if isinstance(metadata, dict) else None
            if not isinstance(key, str):
                continue
            try:
                parse_resource_pack_key(key)
            except ValueError:
                logger.warning("忽略 identity 无效的 resource-pack artifact: {}", base.name)
                continue
            if base.name != resource_pack_path_component(key):
                logger.warning("忽略文件名与 identity 不匹配的 resource-pack artifact: {}", base.name)
                continue
            self._resource_pack_banks_cache[key] = payload
            artifacts_by_key.setdefault(key, payload)
        return [artifacts_by_key[key] for key in sorted(artifacts_by_key)]

    @performance_monitor(level="TRACE")
    def get_resource_pack_events(self, key: str) -> dict | None:
        """读取并缓存指定 resource-pack 的 events artifact。

        Args:
            key: canonical ``resource_pack:...`` 稳定 key。

        Returns:
            resource-pack events 字典；文件不存在或读取失败时返回 ``None``。
        """
        component = resource_pack_path_component(key)
        if key in self._resource_pack_events_cache:
            return self._resource_pack_events_cache[key]

        try:
            events_data = read_data(self.resource_pack_events_dir / component, dev_mode=self.ctx.config.dev_mode)
        except Exception:
            logger.opt(exception=True).error(f"读取 resource pack events 数据失败: key={key}")
            return None

        if events_data:
            self._resource_pack_events_cache[key] = events_data
        return events_data

    def _validate_resource_schema(self, data: dict, label: str, *, require_bindings: bool) -> None:
        """在显式消费 binding 时拒绝旧 artifact。"""
        if not require_bindings:
            return
        if data.get("resourceSchemaVersion") == RESOURCE_SCHEMA_VERSION:
            return
        raise ResourceSchemaMismatchError(f"{label} 的 banks artifact 缺少 resource schema v2，请先重新运行 update。")

    @performance_monitor(level="TRACE")
    def get_map_events(self, map_id: int) -> dict | None:
        """读取并缓存指定地图的 events 数据。

        Args:
            map_id: 地图 ID。

        Returns:
            地图事件映射字典；读取失败时返回 ``None``。
        """
        if map_id in self._map_events_cache:
            return self._map_events_cache[map_id]

        try:
            events_file_base = self.map_events_dir / str(map_id)
            map_events_data = read_data(events_file_base, dev_mode=self.ctx.config.dev_mode)
            result = map_events_data.get("map") if map_events_data else None
        except Exception:
            logger.opt(exception=True).error(f"读取地图 events 数据失败: map_id={map_id}")
            return None

        if result:
            self._map_events_cache[map_id] = result

        return result

    def get_champion(self, champion_id: int) -> dict:
        """根据ID获取英雄信息"""
        return self.data.get("champions", {}).get(str(champion_id), {})

    def get_champions(self) -> list[dict]:
        """获取所有英雄列表"""
        return list(self.data.get("champions", {}).values())

    def get_map(self, map_id: int) -> dict:
        """
        根据ID获取地图信息

        :param map_id: 地图ID
        :returns: 地图信息字典，失败时返回空字典
        """
        return self.data.get("maps", {}).get(str(map_id), {})

    def get_maps(self) -> list[dict]:
        """
        获取所有地图列表

        :returns: 地图信息列表
        """
        return list(self.data.get("maps", {}).values())
