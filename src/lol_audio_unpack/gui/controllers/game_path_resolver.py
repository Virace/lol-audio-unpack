"""GUI 游戏目录识别与归一化。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from lol_audio_unpack.app.game_version import get_game_version
from lol_audio_unpack.utils.game_layout import (
    CHAMPIONS_DIR,
    GAME_DIR,
    GAME_EXE,
    GAME_METADATA,
    LCU_CLIENT_DIRS,
    LCU_DIRS,
    LCU_EXE,
    MAPS_DIR,
    GameLayoutError,
    resolve_layout,
)

GamePathResultReason = Literal["resolved", "not_found", "ambiguous"]

_MAX_ANCESTORS = 8
_MAX_SHALLOW_CHILDREN = 80
_FIXED_SUFFIXES = tuple(
    tuple(part.lower() for part in relative.parts)
    for relative in (GAME_DIR, *LCU_CLIENT_DIRS, *LCU_DIRS, CHAMPIONS_DIR, MAPS_DIR)
    if relative.parts
)


@dataclass(frozen=True, slots=True)
class PathProbe:
    """单个候选游戏根目录的识别结果。"""

    root: Path
    version: str | None
    markers: tuple[str, ...]
    score: int


@dataclass(frozen=True, slots=True)
class PathProbeResult:
    """用户输入路径的游戏目录识别结果。"""

    input_path: Path
    root: Path | None
    version: str | None
    candidates: tuple[PathProbe, ...]
    reason: GamePathResultReason

    @property
    def resolved(self) -> bool:
        """返回是否已经唯一识别到游戏根目录。"""

        return self.reason == "resolved" and self.root is not None


def resolve_game_path(input_path: str | Path) -> PathProbeResult:
    """从用户选择的位置识别并归一化英雄联盟客户端根目录。

    Args:
        input_path: 用户选择或输入的游戏安装相关路径。

    Returns:
        PathProbeResult: 识别结果。只有唯一强匹配时才返回 resolved。
    """

    raw = Path(input_path).expanduser()
    probes = tuple(
        probe
        for candidate in _candidate_roots(raw)
        if (probe := _probe_root(candidate.resolve(strict=False))) is not None
    )

    if len(probes) == 1:
        probe = probes[0]
        return PathProbeResult(
            input_path=raw,
            root=probe.root,
            version=probe.version,
            candidates=probes,
            reason="resolved",
        )

    if len(probes) > 1:
        return PathProbeResult(
            input_path=raw,
            root=None,
            version=None,
            candidates=probes,
            reason="ambiguous",
        )

    return PathProbeResult(input_path=raw, root=None, version=None, candidates=(), reason="not_found")


def _candidate_roots(input_path: Path) -> tuple[Path, ...]:
    """生成有限数量的候选客户端根目录。"""

    start = input_path.parent if input_path.is_file() else input_path
    candidates: list[Path] = []

    _append_unique(candidates, start)
    for root in _roots_from_fixed_suffix(start):
        _append_unique(candidates, root)

    current = start
    for _index in range(_MAX_ANCESTORS):
        parent = current.parent
        if parent == current:
            break
        _append_unique(candidates, parent)
        for root in _roots_from_fixed_suffix(parent):
            _append_unique(candidates, root)
        current = parent

    for child in _shallow_children(start):
        _append_unique(candidates, child)

    return tuple(candidates)


def _probe_root(root: Path) -> PathProbe | None:
    """检查候选根目录是否符合现行 LCU/WAD 安装结构。"""

    try:
        version = get_game_version(root)
    except (OSError, KeyError, TypeError, ValueError):
        return None

    score, markers = _marker_score(root)
    return PathProbe(root=root, version=version, markers=markers, score=score)


def _marker_score(root: Path) -> tuple[int, tuple[str, ...]]:
    """统计候选根目录的佐证标志。"""

    checks = (
        ("content-metadata", root / GAME_METADATA),
        ("game-binary", root / GAME_EXE),
        ("champions-data", root / CHAMPIONS_DIR),
        ("maps-data", root / MAPS_DIR),
    )
    try:
        layout = resolve_layout(root)
    except GameLayoutError as exc:
        # GAME 元数据仍可识别根目录；LCU 歧义由后续核心预检报告。
        logger.debug("游戏目录的 LCU 标志不明确: {}", exc)
    else:
        checks += (
            ("lcu-binary", layout.client_root / LCU_EXE),
            ("lcu-game-data", layout.lcu_path),
        )
    markers = tuple(name for name, path in checks if path.exists())
    return 100 + len(markers), markers


def _roots_from_fixed_suffix(path: Path) -> tuple[Path, ...]:
    """根据固定目录后缀反推出可能的客户端根目录。"""

    parts = tuple(part.lower() for part in path.parts)
    roots: list[Path] = []
    for suffix in _FIXED_SUFFIXES:
        if len(parts) >= len(suffix) and parts[-len(suffix) :] == suffix:
            _append_unique(roots, path.parents[len(suffix) - 1])
    return tuple(roots)


def _shallow_children(path: Path) -> tuple[Path, ...]:
    """生成一层子目录候选，避免递归扫描整盘。"""

    if not path.is_dir():
        return ()

    roots: list[Path] = []
    try:
        children = [child for child in path.iterdir() if child.is_dir()]
    except OSError:
        return ()

    for child in children[:_MAX_SHALLOW_CHILDREN]:
        if (child / GAME_DIR).exists() or any((child / relative).exists() for relative in LCU_DIRS):
            _append_unique(roots, child)

    return tuple(roots)


def _append_unique(paths: list[Path], path: Path) -> None:
    """按解析后的路径去重并保留首次出现顺序。"""

    resolved = path.resolve(strict=False)
    if resolved not in {item.resolve(strict=False) for item in paths}:
        paths.append(resolved)


__all__ = [
    "GamePathResultReason",
    "PathProbe",
    "PathProbeResult",
    "resolve_game_path",
]
