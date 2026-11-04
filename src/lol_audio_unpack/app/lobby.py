"""用同版本大厅资源补齐已有英雄输出，不重新解包 WEM。"""

from loguru import logger

from lol_audio_unpack.manager.data_updater import DataUpdater
from lol_audio_unpack.manager.lobby import LOBBY_FILES, find_lobby_source
from lol_audio_unpack.runtime.library import Library
from lol_audio_unpack.runtime.library.types import normalize_region

from .game_version import resolve_game_version
from .outputs import output_summary, save_outputs
from .types import AppContext


def repair_lobby(ctx: AppContext) -> None:
    """补齐已有英雄输出的大厅文件；历史版本只使用自身保留的源资源。"""
    if not ctx.game_region:
        return
    root = ctx.config.output_path
    region = normalize_region(ctx.game_region)
    pending = []
    for catalog in sorted((root / "reports").glob(f"*/{region}/champions/*.audios.msgpack")):
        version = catalog.parents[2].name
        champion_id = catalog.name.removesuffix(".audios.msgpack")
        count, roots = output_summary(root, version, region, "champion", champion_id)
        if not count or not roots:
            continue
        target = root / "audios" / version / region / "champions" / roots[0].name / "lobby"
        if any(not (target / name).is_file() for name in LOBBY_FILES.values()):
            pending.append((version, champion_id, target))
    if not pending:
        return
    version = resolve_game_version(ctx)
    logger.info("开始补齐已有英雄大厅音频：{} 个实体目录", len(pending))
    try:
        with Library(root) as writer:
            current = {key for patch, key, _ in pending if patch == version}
            if current:
                DataUpdater(ctx).ensure_lobby_audio(current)
            added = missing = 0
            for patch, key, target in pending:
                paths = []
                manifest = root / "manifest" / patch / region
                for category, filename in LOBBY_FILES.items():
                    path = target / filename
                    if not path.is_file():
                        source = find_lobby_source(manifest, region, key, category)
                        if source is None:
                            missing += 1
                            continue
                        writer.link_file(source, path.relative_to(root).as_posix())
                        if source.stem != key:
                            logger.debug("已有英雄大厅音频复用原英雄：{} → {}，{}", key, source.stem, category)
                        added += 1
                    paths.append(path)
                if paths:
                    save_outputs(root, patch, region, "champion", key, paths)
            if missing:
                logger.warning("已有英雄大厅音频补齐结束：新增 {}，对应版本源文件缺失 {}", added, missing)
            else:
                logger.success("已有英雄大厅音频补齐完成：新增 {} 个文件", added)
    except Exception:
        logger.exception("已有英雄大厅音频补齐失败，已存在文件保持不变")
        raise
