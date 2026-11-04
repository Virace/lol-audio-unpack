"""结构化数据更新与多语言资源合并逻辑。"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from league_tools.formats import WAD
from loguru import logger

from lol_audio_unpack.app.game_version import resolve_game_version
from lol_audio_unpack.manager.files import copy_file_atomic, needs_update, read_data, write_data
from lol_audio_unpack.manager.lobby import LOBBY_FILES, find_lobby_source
from lol_audio_unpack.manager.utils import build_metadata_payload
from lol_audio_unpack.utils.atomic import replace_file
from lol_audio_unpack.utils.common import format_region, load_json
from lol_audio_unpack.utils.game_layout import CHAMPIONS_DIR as CHAMPIONS_REL_PATH
from lol_audio_unpack.utils.game_layout import LCU_DESCRIPTION
from lol_audio_unpack.utils.game_layout import MAPS_DIR as MAPS_SHIPPING_REL_PATH
from lol_audio_unpack.utils.game_resources import RCP_GLOBAL_PREFIX, WAD_SUFFIX
from lol_audio_unpack.utils.logging import performance_monitor
from lol_audio_unpack.utils.type_hints import StrPath

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext

# 未使用，考虑是否删除；当前值仅表示嵌套的 LCU 布局。
LCU_PLUGIN_REL_PATH = Path("LeagueClient") / "Plugins" / "rcp-be-lol-game-data"
LOCALIZED_LOBBY_AUDIO_CATEGORIES = ("champion-ban-vo", "champion-choose-vo")
DEFAULT_LOBBY_AUDIO_CATEGORIES = ("champion-sfx-audios",)
LOBBY_AUDIO_CATEGORIES = LOCALIZED_LOBBY_AUDIO_CATEGORIES + DEFAULT_LOBBY_AUDIO_CATEGORIES


class DataUpdater:
    """
    负责游戏数据的更新和多语言JSON合并
    """

    def __init__(
        self,
        ctx: AppContext,
        languages: list[str] | None = None,
        force_update: bool = False,
    ) -> None:
        """
        初始化数据更新器

        :param languages: 需要处理的语言列表（不包括default，default会自动添加）。
                        如果为None，则使用config中的GAME_REGION。
        :param force_update: 是否强制更新
        :param ctx: 运行时上下文。
        """
        self.ctx = ctx
        self.game_path = Path(self.ctx.config.game_path)
        self.manifest_path = Path(self.ctx.paths.manifest_path)
        self.temp_path = Path(self.ctx.paths.temp_path)

        if not self.game_path or not self.manifest_path:
            raise ValueError("GAME_PATH 和 MANIFEST_PATH 必须在配置中设置")

        if languages is None:
            game_region = self.ctx.config.game_region
            if not game_region:
                raise ValueError("请选择游戏资源语言")
            self.languages: list[str] = [game_region]
        else:
            self.languages: list[str] = languages

        self.version: str = resolve_game_version(self.ctx)
        self.version_manifest_path: Path = self.ctx.version_path("manifest", self.version)
        self.data_file_base: Path = self.version_manifest_path / "data"
        self.process_languages: list[str] = self._prepare_language_list(self.languages)
        self.force_update = force_update

        self.version_manifest_path.mkdir(parents=True, exist_ok=True)

        # 记录初始化信息
        logger.debug(
            f"DataUpdater 初始化完成 - 版本: {self.version}, 语言: {self.process_languages}, 强制更新: {force_update}"
        )

    def _prepare_language_list(self, languages: list[str]) -> list[str]:
        """准备处理语言列表，确保default在列表中"""
        process_languages = ["default"]
        for lang in languages:
            if lang.lower() not in ["default", "en_us"]:
                process_languages.append(lang)
        return process_languages

    def _is_lobby_audio_enabled(self) -> bool:
        """安全读取大厅音频开关。"""
        return bool(self.ctx.config.lobby_audio)

    def _is_dev_mode(self) -> bool:
        """返回当前运行是否为开发模式。"""
        return bool(self.ctx.config.dev_mode)

    def _get_game_maps_path(self) -> Path:
        """获取地图资源根目录。"""
        return Path(self.ctx.paths.game_maps_path)

    def _get_game_champions_path(self) -> Path:
        """获取英雄资源根目录。"""
        return Path(self.ctx.paths.game_champion_path)

    def _get_game_lcu_path(self) -> Path:
        """获取 LCU 数据根目录。"""
        return Path(self.ctx.paths.game_lcu_path)

    def _resolve_relative_game_path(self, target_path: Path, fallback: Path) -> str:
        """将绝对路径转换为相对游戏根目录路径。"""
        try:
            return target_path.relative_to(self.game_path).as_posix()
        except ValueError:
            logger.warning(f"路径不在游戏目录内，回退到默认相对路径: target={target_path}, fallback={fallback}")
            return fallback.as_posix()

    def _build_champion_wad_info(self, alias: str) -> dict[str, str]:
        """构建英雄 WAD 路径信息。"""
        champions_rel_base = self._resolve_relative_game_path(
            self._get_game_champions_path(),
            CHAMPIONS_REL_PATH,
        )
        wad_path_base = f"{champions_rel_base}/{alias}"
        return {
            "root": f"{wad_path_base}{WAD_SUFFIX}",
            "default": f"{wad_path_base}.en_US{WAD_SUFFIX}",
            **{lang: f"{wad_path_base}.{lang}{WAD_SUFFIX}" for lang in self.process_languages if lang != "default"},
        }

    def _build_map_wad_info(self, wad_prefix: str) -> dict[str, str]:
        """构建地图 WAD 路径信息。"""
        maps_rel_base = self._resolve_relative_game_path(
            self._get_game_maps_path(),
            MAPS_SHIPPING_REL_PATH,
        )
        wad_path_base = f"{maps_rel_base}/{wad_prefix}"
        return {
            "root": f"{wad_path_base}{WAD_SUFFIX}",
            "default": f"{wad_path_base}.en_US{WAD_SUFFIX}",
            **{lang: f"{wad_path_base}.{lang}{WAD_SUFFIX}" for lang in self.process_languages if lang != "default"},
        }

    @staticmethod
    def _build_rcp_v1_path(region: str, entry: str) -> str:
        """构建 rcp-be-lol-game-data 的 v1 资源路径。"""
        return f"{RCP_GLOBAL_PREFIX}/{region}/v1/{entry}"

    def _resolve_bundle_names(self, region: str, head: str) -> list[str]:
        """从 description.json 解析指定区域对应的 asset bundle 文件名列表。

        Args:
            region: 规范化后的区域（例如 ``default``、``zh_CN``）。
            head: 经过 ``format_region`` 处理后的区域头（例如 ``default``、``zh_CN``）。

        Returns:
            bundle 文件名列表；解析失败时返回空列表。
        """
        description_file = self._get_game_lcu_path() / LCU_DESCRIPTION
        if not description_file.exists():
            logger.warning(f"未找到 description.json，无法按清单解析资源: {description_file}")
            return []

        try:
            description = load_json(description_file)
        except Exception:
            logger.opt(exception=True).warning(f"读取 description.json 失败: {description_file}")
            return []

        riot_meta = description.get("riotMeta")
        if not isinstance(riot_meta, dict):
            logger.warning("description.json 缺少 riotMeta 字段，无法解析资源清单")
            return []

        if head == "default":
            bundle_names = riot_meta.get("globalAssetBundles", [])
        else:
            per_locale = riot_meta.get("perLocaleAssetBundles", {})
            if not isinstance(per_locale, dict):
                logger.warning("description.json 的 perLocaleAssetBundles 字段类型异常")
                return []
            bundle_names = (
                per_locale.get(head)
                or per_locale.get(region)
                or per_locale.get(str(region).lower())
                or per_locale.get(str(region).upper())
                or []
            )

        if not isinstance(bundle_names, list):
            logger.warning(
                f"description.json 中 bundle 列表类型异常: region={region}, head={head}, type={type(bundle_names)}"
            )
            return []

        return [str(name).strip() for name in bundle_names if str(name).strip()]

    def _resolve_wad_files(self, region: str, head: str) -> list[Path]:
        """解析待处理 WAD 文件列表（仅使用 description.json 清单）。"""
        lcu_root = self._get_game_lcu_path()
        bundle_names = self._resolve_bundle_names(region, head)

        if not bundle_names:
            logger.error(f"description.json 未提供区域资源清单: region={region}, head={head}")
            return []

        resolved_files: list[Path] = []
        missing_names: list[str] = []
        for bundle_name in bundle_names:
            wad_file = lcu_root / bundle_name
            if wad_file.exists():
                resolved_files.append(wad_file)
            else:
                missing_names.append(bundle_name)

        if missing_names:
            logger.warning(f"description.json 中有 {len(missing_names)} 个资源文件不存在: {missing_names[:5]}")

        return resolved_files

    @staticmethod
    def _normalize_text(text: str) -> str:
        """标准化文本"""
        if not isinstance(text, str):
            return text
        return text.replace("\u00a0", " ")

    @performance_monitor(level="INFO")
    def check_and_update(self) -> Path:
        """检查游戏版本并更新数据"""
        if (
            not needs_update(self.data_file_base, self.version, self.force_update, dev_mode=self._is_dev_mode())
            and self._check_languages()
        ):
            if self._is_lobby_audio_enabled():
                self.ensure_lobby_audio(read_data(self.data_file_base).get("champions", {}))
            logger.info(f"数据文件已是最新版本 {self.version} 且包含所有请求的语言，无需更新。")
            # 返回基础路径，让调用者决定使用哪个具体文件
            return self.data_file_base

        run_temp_path = self.temp_path / f"update_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        run_temp_path.mkdir(parents=True, exist_ok=True)
        logger.debug(f"创建临时目录用于解包: {run_temp_path}")

        try:
            self._process_data(run_temp_path)
            # 日志展示已经完成发布的固定格式路径。
            logger.success(f"数据更新完成: {self.data_file_base.with_suffix('.msgpack')}")
            return self.data_file_base
        finally:
            if not self._is_dev_mode():
                try:
                    shutil.rmtree(run_temp_path)
                    logger.debug(f"已清理临时目录: {run_temp_path}")
                except OSError:
                    # 使用现代的异常记录方式
                    logger.opt(exception=True).error(f"清理临时目录失败: {run_temp_path}")
            else:
                logger.warning(f"开发模式，临时目录未删除: {run_temp_path}")

    def ensure_lobby_audio(self, champion_ids: Iterable[str | int]) -> None:
        """只补齐指定英雄缺少的大厅文件，独立于共享元数据是否已更新。"""
        pending = {}
        for champion_id in champion_ids:
            for category in LOBBY_FILES:
                if find_lobby_source(self.version_manifest_path, self.ctx.game_region, str(champion_id), category):
                    continue
                region = self.ctx.game_region
                if category == "champion-sfx-audios" or region.lower() in {"default", "en_us"}:
                    region = "default"
                pending.setdefault(region, []).append((category, str(champion_id)))
        if not pending:
            return
        logger.info("开始补齐大厅音频：{}，{} 个目标文件", self.version, sum(map(len, pending.values())))
        try:
            for region, entries in pending.items():
                paths = [self._build_rcp_v1_path(region, f"{category}/{key}.ogg") for category, key in entries]

                for wad in self._resolve_wad_files(region, format_region(region)):
                    for path, data in zip(paths, WAD(wad).extract(paths, raw=True), strict=True):
                        if data is None:
                            continue
                        category, filename = path.rsplit("/", 2)[-2:]
                        target = self.version_manifest_path / "lobby" / region / category / filename
                        replace_file(target, lambda temp, payload=data: temp.write_bytes(payload), write_stage="lobby")
            missing = sum(
                find_lobby_source(self.version_manifest_path, self.ctx.game_region, key, category) is None
                for entries in pending.values()
                for category, key in entries
            )
            if missing:
                logger.warning("大厅音频补齐结束：源资源中仍缺少 {} 个目标文件", missing)
            else:
                logger.success("大厅音频补齐完成：{} 个文件", sum(map(len, pending.values())))
        except Exception:
            logger.exception("大厅音频补齐失败：{}", self.version)
            raise

    def _check_languages(self) -> bool:
        """检查 canonical metadata 是否包含所有请求语言。

        writer 不保存隐含基础语言 ``default``，因此合法空列表表示仅包含该基础语言。

        Returns:
            metadata 完整且覆盖全部请求语言时返回 ``True``。
        """
        data = read_data(self.data_file_base, dev_mode=self._is_dev_mode())
        if not data:
            return False

        metadata = data.get("metadata")
        languages = metadata.get("languages") if isinstance(metadata, dict) else None
        if not isinstance(languages, (list, tuple)) or any(
            not isinstance(language, str) or not language.strip() for language in languages
        ):
            logger.warning("现有数据缺少有效的 metadata.languages，需要重新更新")
            return False

        existing_languages = {language.strip() for language in languages}
        existing_languages.add("default")
        requested_languages = {language.strip() for language in self.process_languages}

        if requested_languages.issubset(existing_languages):
            return True

        missing_languages = sorted(requested_languages - existing_languages)
        logger.info(f"需要更新数据文件，缺少语言: {missing_languages}")
        return False

    @performance_monitor(level="DEBUG")
    def _process_data(self, temp_path: Path) -> None:
        """处理游戏数据，包括提取、合并和验证"""

        for language in self.process_languages:
            logger.info(f"正在处理 {language} 语言数据...")
            self._extract_wad_data(temp_path, language)

        logger.info("合并多语言数据...")
        self._merge_and_build_data(temp_path)

        if self._is_lobby_audio_enabled():
            self._persist_lobby_audio_files(temp_path)

        # 从临时目录复制最终生成的数据文件到目标目录
        temp_data_file_base = temp_path / self.version / "data"
        source_file = temp_data_file_base.with_suffix(".msgpack")

        if source_file.exists():
            target = copy_file_atomic(source_file, self.data_file_base.with_suffix(".msgpack"))
            logger.debug(f"已复制合并数据到: {target}")
        else:
            raise FileNotFoundError(f"未能创建合并数据文件: {source_file}")

    @performance_monitor(level="DEBUG")
    def _persist_lobby_audio_files(self, temp_path: Path) -> None:
        """将临时目录中的大厅音频持久化到 manifest 目录。"""
        temp_version_path = temp_path / self.version
        target_root = self.version_manifest_path / "lobby"
        copied_count = 0

        for region in self.process_languages:
            for category in LOBBY_AUDIO_CATEGORIES:
                source_dir = temp_version_path / region / category
                if not source_dir.exists():
                    continue

                target_dir = target_root / region / category
                target_dir.mkdir(parents=True, exist_ok=True)

                for source_file in source_dir.glob("*.ogg"):
                    copy_file_atomic(source_file, target_dir / source_file.name)
                    copied_count += 1

        if copied_count > 0:
            logger.success(f"大厅音频持久化完成，共 {copied_count} 个文件: {target_root}")
        else:
            logger.warning("已启用 LOBBY_AUDIO，但未提取到任何大厅音频文件。")

    def _load_language_json(self, base_path: Path, filename_template: str) -> dict[str, Any]:
        """加载指定模板的、所有语言的JSON文件"""
        loaded_data = {}
        logger.trace(f"加载多语言JSON文件模板: {filename_template}")

        for lang in self.process_languages:
            file_path = base_path / lang / filename_template.format(lang=lang)
            if file_path.exists():
                # 这里读取的是WAD解包出的原始json，所以必须用load_json
                loaded_data[lang] = load_json(file_path)
                logger.trace(f"成功加载 {lang} 语言文件: {file_path}")
            else:
                logger.warning(f"未找到JSON文件: {file_path}")

        logger.trace(f"多语言JSON加载完成，共 {len(loaded_data)} 种语言")
        return loaded_data

    @performance_monitor(level="DEBUG")
    def _merge_and_build_data(self, temp_dir: Path) -> None:
        """聚合所有数据处理和合并逻辑"""
        base_path = temp_dir / self.version

        summaries = self._load_language_json(base_path, "champion-summary.json")

        if "default" not in summaries:
            raise FileNotFoundError("未找到 default 语言的英雄概要数据，无法构建核心 data artifact")

        final_champions = {}
        skin_bin_count = 0
        chroma_bin_count = 0

        logger.info("合并英雄数据并装配 bin 元数据...")

        # 使用 lazy 求值记录英雄处理统计
        logger.opt(lazy=True).debug(
            "英雄数据统计: {champion_stats}",
            champion_stats=lambda: {
                "total_languages": len(summaries),
                "total_champions": len(summaries.get("default", [])),
                "language_breakdown": {lang: len(champions) for lang, champions in summaries.items()},
            },
        )

        for i, default_summary in enumerate(summaries["default"]):
            champ_id = str(default_summary["id"])
            if champ_id == "-1":
                continue

            alias = self._normalize_text(default_summary["alias"])
            details = self._load_language_json(base_path, f"champions/{champ_id}.json")
            default_details = details.get("default", {})

            # 使用 TRACE 级别记录每个英雄的处理进度
            logger.trace(f"处理英雄: {alias} (ID: {champ_id})")

            names = {lang: self._normalize_text(summ[i]["name"]) for lang, summ in summaries.items() if i < len(summ)}
            titles = {lang: self._normalize_text(det.get("title", "")) for lang, det in details.items()}
            descriptions = {
                lang: self._normalize_text(summ[i].get("description", ""))
                for lang, summ in summaries.items()
                if i < len(summ)
            }

            processed_skins = []
            for skin_idx, skin_detail in enumerate(default_details.get("skins", [])):
                skin_id_num = self._parse_skin_id(skin_detail["id"], int(champ_id))
                skin_names = {
                    lang: self._normalize_text(det.get("skins", [])[skin_idx].get("name", ""))
                    for lang, det in details.items()
                    if skin_idx < len(det.get("skins", []))
                }

                skin_data = {
                    "id": skin_detail["id"],
                    "isBase": skin_detail.get("isBase", False),
                    "skinNames": skin_names,
                    "binPath": f"data/characters/{alias}/skins/skin{skin_id_num}.bin",
                }
                skin_bin_count += 1

                processed_chromas = []
                for chroma_idx, chroma_detail in enumerate(skin_detail.get("chromas", [])):
                    chroma_id_num = self._parse_skin_id(chroma_detail["id"], int(champ_id))
                    chroma_names = {
                        lang: self._normalize_text(
                            det.get("skins", [])[skin_idx].get("chromas", [])[chroma_idx].get("name", "")
                        )
                        for lang, det in details.items()
                        if skin_idx < len(det.get("skins", []))
                        and chroma_idx < len(det.get("skins", [])[skin_idx].get("chromas", []))
                    }
                    processed_chromas.append(
                        {
                            "id": chroma_detail["id"],
                            "chromaNames": chroma_names,
                            "binPath": f"data/characters/{alias}/skins/skin{chroma_id_num}.bin",
                        }
                    )
                    chroma_bin_count += 1

                if processed_chromas:
                    skin_data["chromas"] = processed_chromas

                processed_skins.append(skin_data)

            final_champions[champ_id] = {
                "id": default_summary["id"],
                "alias": alias,
                "names": names,
                "titles": titles,
                "descriptions": {k: v for k, v in descriptions.items() if v},
                "skins": processed_skins,
                "wad": self._build_champion_wad_info(alias),
            }

        final_result = build_metadata_payload(
            self.version, [lang for lang in self.process_languages if lang != "default"]
        )
        final_result["champions"] = final_champions

        logger.debug(
            f"英雄 bin 元数据装配完成，共 {len(final_champions)} 个英雄，"
            f"{skin_bin_count} 个皮肤 binPath，"
            f"{chroma_bin_count} 个炫彩 binPath"
        )

        # 记录英雄处理完成统计
        logger.success(f"英雄数据合并完成，共处理 {len(final_champions)} 个英雄")

        logger.info("合并地图数据并装配 bin 元数据...")
        maps_by_lang = self._load_language_json(base_path, "maps.json")
        if "default" in maps_by_lang:
            final_maps = {}
            map_bin_count = 0
            map_index_by_lang = {lang: {m["id"]: i for i, m in enumerate(maps)} for lang, maps in maps_by_lang.items()}

            # 使用 lazy 求值记录详细的地图统计信息
            logger.opt(lazy=True).debug(
                "地图数据统计: {map_stats}",
                map_stats=lambda: {
                    "total_languages": len(maps_by_lang),
                    "total_maps": len(maps_by_lang.get("default", [])),
                    "language_breakdown": {lang: len(maps) for lang, maps in maps_by_lang.items()},
                },
            )

            for default_map in maps_by_lang["default"]:
                map_id = default_map["id"]
                map_string_id = default_map["mapStringId"]

                names = {}
                for lang, maps in maps_by_lang.items():
                    if map_id in map_index_by_lang.get(lang, {}):
                        idx = map_index_by_lang[lang][map_id]
                        names[lang] = self._normalize_text(maps[idx]["name"])

                map_data = {"id": map_id, "mapStringId": map_string_id, "names": names}

                wad_prefix = f"Map{map_id}" if map_id != 0 else "Common"
                map_data["binPath"] = f"data/maps/shipping/{wad_prefix.lower()}/{wad_prefix.lower()}.bin"
                map_bin_count += 1
                wad_info = self._build_map_wad_info(wad_prefix)
                # 元数据保留完整名单与物理规则，存在性由来源预检单独表达。
                map_data["wad"] = wad_info

                final_maps[str(map_id)] = map_data
            final_result["maps"] = final_maps

            logger.debug(f"地图 bin 元数据装配完成，共 {map_bin_count} 个地图 binPath")

            # 记录地图处理完成统计
            logger.success(f"地图数据合并完成，共处理 {len(final_maps)} 个地图")
        else:
            logger.warning("未找到default语言的地图数据，跳过处理。")

        # 共享元数据始终使用 MessagePack；开发模式只影响诊断与临时文件保留。
        write_data(final_result, base_path / "data", dev_mode=self._is_dev_mode())

        # 记录最终处理完成统计
        logger.success(
            f"数据合并完成 - 英雄: {len(final_result.get('champions', {}))}, "
            f"地图: {len(final_result.get('maps', {}))}, "
            f"语言: {len(self.process_languages)}"
        )

    @performance_monitor(level="DEBUG")
    def _extract_wad_data(self, out_dir: StrPath, region: str) -> None:
        """从WAD文件提取JSON数据"""
        out_path = Path(out_dir) / self.version / region
        out_path.mkdir(parents=True, exist_ok=True)
        _region = "default" if region.lower() == "en_us" else region
        _head = format_region(_region)

        wad_files = self._resolve_wad_files(_region, _head)

        if not wad_files:
            logger.error(f"未找到 {_region} 区域的WAD文件")
            return

        logger.debug(f"找到 {len(wad_files)} 个WAD文件需要处理")
        hash_table = [
            self._build_rcp_v1_path(_region, "champion-summary.json"),
            self._build_rcp_v1_path(_region, "maps.json"),
        ]

        def output_file_name(path: str) -> Path:
            # 这里统一裁掉任意语言段，保证 default-only 资源也能写回当前语言目录。
            reg = re.compile(rf"{RCP_GLOBAL_PREFIX}/[^/]+/v\d+/", re.IGNORECASE)
            new = reg.sub("", path)
            return out_path / new

        # 提取基础数据文件
        logger.debug(f"开始提取基础数据文件，共 {len(hash_table)} 个目标文件")
        for wad_file in wad_files:
            logger.trace(f"从WAD文件提取: {wad_file.name}")
            WAD(wad_file).extract(hash_table, output_file_name)

        # 提取英雄详细信息
        summary_file = out_path / "champion-summary.json"
        if summary_file.exists():
            try:
                champions = load_json(summary_file)
                champion_hashes = [
                    self._build_rcp_v1_path(_region, f"champions/{item['id']}.json")
                    for item in champions
                    if item["id"] != -1
                ]

                logger.debug(f"准备提取 {len(champion_hashes)} 个英雄详细信息，用于后续 bin 元数据装配")
                (out_path / "champions").mkdir(exist_ok=True)

                for wad_file in wad_files:
                    logger.trace(f"从 {wad_file.name} 提取英雄详细信息")
                    WAD(wad_file).extract(champion_hashes, output_file_name)

                logger.success(f"英雄信息提取完成，共 {len(champion_hashes)} 个英雄，将进入 bin 元数据装配")

                if self._is_lobby_audio_enabled():
                    lobby_audio_hashes: list[str] = []
                    region_candidates = [_region]
                    region_lower = _region.lower()
                    if region_lower not in region_candidates:
                        region_candidates.append(region_lower)

                    for item in champions:
                        champion_id = item.get("id")
                        if champion_id in (-1, None):
                            continue

                        for region_name in region_candidates:
                            for category in LOCALIZED_LOBBY_AUDIO_CATEGORIES:
                                lobby_audio_hashes.append(
                                    self._build_rcp_v1_path(region_name, f"{category}/{champion_id}.ogg")
                                )

                        for category in DEFAULT_LOBBY_AUDIO_CATEGORIES:
                            lobby_audio_hashes.append(
                                self._build_rcp_v1_path("default", f"{category}/{champion_id}.ogg")
                            )

                    if lobby_audio_hashes:
                        logger.debug(f"准备提取大厅音频，共 {len(lobby_audio_hashes)} 个目标路径")
                        for wad_file in wad_files:
                            logger.trace(f"从 {wad_file.name} 提取大厅音频")
                            WAD(wad_file).extract(lobby_audio_hashes, output_file_name)
            except Exception:
                logger.opt(exception=True).error(f"解包 {_region} 区域英雄信息时出错")
                if self._is_dev_mode():
                    raise
        else:
            logger.warning("未找到英雄概要文件，跳过英雄详细信息提取")

    def _parse_skin_id(self, full_id: int, champion_id: int) -> int:
        """从完整的皮肤ID中提取皮肤编号"""
        champion_id_len = len(str(champion_id))
        skin_id_str = str(full_id)[champion_id_len:]
        return int(skin_id_str)
