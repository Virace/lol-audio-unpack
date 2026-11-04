"""按目标路径 hash 查询本地 FINAL WAD TOC 的运行期索引。"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from league_tools.formats import WAD

from lol_audio_unpack.model.binding import (
    BindingCandidate,
    BindingRole,
    BindingStatus,
    format_entry_hash,
    normalize_logical_path,
    normalize_wad_identity,
)
from lol_audio_unpack.utils.game_layout import FINAL_DIR
from lol_audio_unpack.utils.game_resources import WAD_SUFFIX

_LOCALE_WAD_RE = re.compile(r"\.[a-z]{2}_[a-z]{2}\.wad\.client$", re.IGNORECASE)


@dataclass(frozen=True)
class ResolutionRequest:
    """一条逻辑路径及其首选容器角色。"""

    path: str
    preferred_role: BindingRole


@dataclass(frozen=True)
class WadResolution:
    """逻辑路径的一次 WAD 解析结果。"""

    path: str
    normalized_path: str
    entry_hash: str
    status: BindingStatus
    wad: str | None
    role: BindingRole | None
    candidates: tuple[BindingCandidate, ...]
    payload: bytes | None = field(default=None, repr=False)
    diagnostic: str | None = None


@dataclass(frozen=True)
class _WadView:
    identity: str
    path: Path = field(repr=False)
    wad: Any = field(repr=False)
    format_version: str
    sections_by_hash: dict[int, tuple[Any, ...]] = field(repr=False)


@dataclass(frozen=True)
class _Match:
    view: _WadView
    section: Any = field(repr=False)
    role: BindingRole

    @property
    def candidate(self) -> BindingCandidate:
        checksum = getattr(self.section, "sha256", None)
        return BindingCandidate(
            wad=self.view.identity,
            entry_hash=format_entry_hash(self.section.path_hash),
            role=self.role,
            size=int(self.section.size),
            checksum=f"{checksum:016x}" if checksum is not None else None,
            offset=int(self.section.offset),
        )


class WadTocCache:
    """以路径、stat 与 WAD format version 复用已解析 TOC。"""

    def __init__(self, wad_factory: Callable[[Path], Any] = WAD):
        """初始化进程内 TOC cache。

        Args:
            wad_factory: 从物理路径构造 WAD 解析对象的边界函数。
        """
        self._wad_factory = wad_factory
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, int, int, str], _WadView] = {}
        self._stat_keys: dict[tuple[str, int, int], tuple[str, int, int, str]] = {}
        self._pending: dict[tuple[str, int, int], threading.Event] = {}
        self._loaded_stat_keys: set[tuple[str, int, int]] = set()
        self.hits = 0
        self.misses = 0
        self.unique_loads = 0
        self.duplicate_loads = 0

    def get(self, game_root: Path, path: Path) -> tuple[_WadView, bool]:
        """读取或复用一个 WAD TOC，慢解析发生在 cache 锁外。"""
        identity = normalize_wad_identity(path.resolve().relative_to(game_root.resolve()).as_posix())
        stat = path.stat()
        stat_key = (identity.casefold(), stat.st_size, stat.st_mtime_ns)

        while True:
            with self._lock:
                if full_key := self._stat_keys.get(stat_key):
                    self.hits += 1
                    return self._cache[full_key], True
                if pending := self._pending.get(stat_key):
                    waiter = pending
                else:
                    waiter = threading.Event()
                    self._pending[stat_key] = waiter
                    self.misses += 1
                    if stat_key in self._loaded_stat_keys:
                        self.duplicate_loads += 1
                    else:
                        self._loaded_stat_keys.add(stat_key)
                        self.unique_loads += 1
                    break
            waiter.wait()

        try:
            wad = self._wad_factory(path)
            version = ".".join(str(part) for part in getattr(wad, "version", ("unknown",)))
            sections: dict[int, list[Any]] = defaultdict(list)
            for section in wad.files:
                sections[section.path_hash].append(section)
            view = _WadView(
                identity=identity,
                path=path,
                wad=wad,
                format_version=version,
                sections_by_hash={path_hash: tuple(items) for path_hash, items in sections.items()},
            )
            full_key = (*stat_key, version)
            with self._lock:
                self._cache[full_key] = view
                self._stat_keys[stat_key] = full_key
            return view, False
        finally:
            with self._lock:
                event = self._pending.pop(stat_key)
                event.set()


class WadIndex:
    """在 root/current-language 候选中执行目标驱动资源解析。"""

    def __init__(  # noqa: PLR0913
        self,
        game_root: Path,
        region: str,
        *,
        cache: WadTocCache | None = None,
        root_wads: Iterable[Path] | None = None,
        localized_wads: Iterable[Path] | None = None,
        hash_path: Callable[[str], int] = WAD.get_hash,
    ):
        """初始化索引并冻结本次运行的候选 WAD 集合。"""
        self.game_root = game_root.resolve()
        # default 仅是 LCU 命名空间；GAME 英语使用独立 en_US WAD。
        self.region = "en_US" if region.casefold() == "default" else region
        self.cache = cache or WadTocCache()
        self.hash_path = hash_path
        if root_wads is None or localized_wads is None:
            discovered_root, discovered_localized = self._discover_wads()
            root_wads = discovered_root if root_wads is None else root_wads
            localized_wads = discovered_localized if localized_wads is None else localized_wads
        self.root_wads = self._sort_wads(root_wads)
        self.localized_wads = self._sort_wads(localized_wads)
        self.errors: list[str] = []
        self.metrics = {
            "candidateWads": len(self.root_wads) + len(self.localized_wads),
            "rootWads": len(self.root_wads),
            "localizedWads": len(self.localized_wads),
            "requests": 0,
            "resolved": 0,
            "missing": 0,
            "conflicts": 0,
            "parseFailed": 0,
            "tocSeconds": 0.0,
        }

    def _discover_wads(self) -> tuple[list[Path], list[Path]]:
        """只枚举本地 FINAL 下的 root 与当前语言 WAD。"""
        final_root = self.game_root / FINAL_DIR
        if not final_root.is_dir():
            return [], []

        root_wads: list[Path] = []
        localized_wads: list[Path] = []
        localized_suffix = f".{self.region}{WAD_SUFFIX}".casefold()
        for path in final_root.rglob(f"*{WAD_SUFFIX}"):
            name = path.name.casefold()
            if name.endswith(localized_suffix):
                localized_wads.append(path)
            elif not _LOCALE_WAD_RE.search(name):
                root_wads.append(path)
        return root_wads, localized_wads

    def _sort_wads(self, paths: Iterable[Path]) -> tuple[Path, ...]:
        """按相对 identity 排序以保证歧义选择确定。"""
        return tuple(
            sorted(
                paths,
                key=lambda path: normalize_wad_identity(
                    path.resolve().relative_to(self.game_root).as_posix()
                ).casefold(),
            )
        )

    def _scan(
        self,
        paths: Iterable[Path],
        target_hashes: set[int],
        role: BindingRole,
    ) -> dict[int, list[_Match]]:
        """扫描候选 TOC，但只保留当前批次请求的 hashes。"""
        matches: dict[int, list[_Match]] = defaultdict(list)
        if not target_hashes:
            return matches

        started = time.perf_counter()
        for path in paths:
            try:
                view, _cache_hit = self.cache.get(self.game_root, path)
                for path_hash in target_hashes:
                    for section in view.sections_by_hash.get(path_hash, ()):
                        matches[section.path_hash].append(_Match(view=view, section=section, role=role))
            except Exception as exc:
                identity = normalize_wad_identity(path.resolve().relative_to(self.game_root).as_posix())
                self.errors.append(f"{identity}: {type(exc).__name__}")
        self.metrics["tocSeconds"] += time.perf_counter() - started
        return matches

    @staticmethod
    def _select_matches(
        request: ResolutionRequest,
        path_hash: int,
        root_matches: Mapping[int, list[_Match]],
        localized_matches: Mapping[int, list[_Match]],
    ) -> tuple[list[_Match], BindingRole | None]:
        preferred = localized_matches if request.preferred_role is BindingRole.LOCALIZED else root_matches
        fallback = root_matches if request.preferred_role is BindingRole.LOCALIZED else localized_matches
        if matches := list(preferred.get(path_hash, [])):
            return matches, request.preferred_role
        if matches := list(fallback.get(path_hash, [])):
            return matches, BindingRole.FALLBACK
        return [], None

    @staticmethod
    def _sort_matches(matches: list[_Match]) -> list[_Match]:
        return sorted(matches, key=lambda item: (item.view.identity.casefold(), int(item.section.offset)))

    @staticmethod
    def _extract(match: _Match) -> bytes | None:
        return match.view.wad.extract_by_section(match.section, "", raw=True)

    def _resolve(
        self,
        request: ResolutionRequest,
        path_hash: int,
        matches: list[_Match],
        selected_role: BindingRole | None,
        *,
        load_payload: bool,
    ) -> WadResolution:
        normalized = normalize_logical_path(request.path)
        entry_hash = format_entry_hash(path_hash)
        if not matches:
            self.metrics["missing"] += 1
            return WadResolution(
                path=request.path,
                normalized_path=normalized,
                entry_hash=entry_hash,
                status=BindingStatus.MISSING,
                wad=None,
                role=None,
                candidates=(),
                diagnostic="root 与当前语言 WAD 均未命中目标 hash",
            )

        matches = self._sort_matches(matches)
        candidates = tuple(match.candidate for match in matches)
        chosen = matches[0]
        if len(matches) == 1:
            payload = self._extract(chosen) if load_payload else None
            if load_payload and payload is None:
                self.metrics["parseFailed"] += 1
                return WadResolution(
                    path=request.path,
                    normalized_path=normalized,
                    entry_hash=entry_hash,
                    status=BindingStatus.PARSE_FAILED,
                    wad=chosen.view.identity,
                    role=selected_role,
                    candidates=candidates,
                    diagnostic="WAD entry 解压失败",
                )
            self.metrics["resolved"] += 1
            return WadResolution(
                path=request.path,
                normalized_path=normalized,
                entry_hash=entry_hash,
                status=BindingStatus.RESOLVED,
                wad=chosen.view.identity,
                role=selected_role,
                candidates=candidates,
                payload=payload,
            )

        payloads = [self._extract(match) for match in matches]
        if any(payload is None for payload in payloads):
            digests: set[bytes | None] = {None}
        else:
            digests = {hashlib.sha256(payload).digest() for payload in payloads if payload is not None}
        if len(digests) == 1 and None not in digests:
            self.metrics["resolved"] += 1
            return WadResolution(
                path=request.path,
                normalized_path=normalized,
                entry_hash=entry_hash,
                status=BindingStatus.AMBIGUOUS_IDENTICAL,
                wad=chosen.view.identity,
                role=selected_role,
                candidates=candidates,
                payload=payloads[0] if load_payload else None,
                diagnostic="多个 WAD 命中相同内容，已按 WAD identity 确定性选择",
            )

        self.metrics["conflicts"] += 1
        return WadResolution(
            path=request.path,
            normalized_path=normalized,
            entry_hash=entry_hash,
            status=BindingStatus.AMBIGUOUS_CONFLICT,
            wad=None,
            role=None,
            candidates=candidates,
            diagnostic="多个 WAD 命中不同内容，拒绝静默选择",
        )

    def resolve_many(
        self,
        requests: Iterable[ResolutionRequest],
        *,
        load_payload: bool = False,
    ) -> list[WadResolution]:
        """批量解析目标路径，按首选角色执行受控 fallback。"""
        request_list = list(requests)
        path_hashes = [self.hash_path(normalize_logical_path(request.path)) for request in request_list]
        target_hashes = set(path_hashes)
        root_matches = self._scan(self.root_wads, target_hashes, BindingRole.ROOT)

        # root 首选请求只在未命中时查询当前语言；localized 首选请求始终需要语言 TOC。
        localized_hashes = {
            path_hash
            for request, path_hash in zip(request_list, path_hashes, strict=True)
            if request.preferred_role is BindingRole.LOCALIZED or not root_matches.get(path_hash)
        }
        localized_matches = self._scan(self.localized_wads, localized_hashes, BindingRole.LOCALIZED)

        self.metrics["requests"] += len(request_list)
        return [
            self._resolve(
                request,
                path_hash,
                *self._select_matches(request, path_hash, root_matches, localized_matches),
                load_payload=load_payload,
            )
            for request, path_hash in zip(request_list, path_hashes, strict=True)
        ]

    def snapshot_metrics(self) -> dict[str, int | float]:
        """返回可序列化的累计索引指标。"""
        return {
            **self.metrics,
            "cacheHits": self.cache.hits,
            "cacheMisses": self.cache.misses,
            "uniqueTocLoads": self.cache.unique_loads,
            "duplicatePhysicalWadLoads": self.cache.duplicate_loads,
            "indexErrors": len(self.errors),
        }


__all__ = [
    "ResolutionRequest",
    "WadIndex",
    "WadResolution",
    "WadTocCache",
]
