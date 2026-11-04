"""显式资源包的 WAD 选择、稳定身份与目标分区规则。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote_from_bytes, unquote_to_bytes

from lol_audio_unpack.utils.game_layout import FINAL_DIR
from lol_audio_unpack.utils.game_resources import WAD_SUFFIX as _WAD_SUFFIX

RESOURCE_PACK_PREFIX = "resource_pack"
RESOURCE_PACK_ENTITY_TYPE = "resource_pack"
RESOURCE_PACK_GROUP = "resource_packs"
_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[a-zA-Z]:/")


class ResourcePackSelectionError(ValueError):
    """资源包 WAD 选择不满足本地安全边界。"""


class ResourcePackStaleSelectionError(ResourcePackSelectionError):
    """资源包 WAD 在选择后发生了变化。"""


@dataclass(frozen=True, slots=True)
class ResourcePackWadRef:
    """可持久化的 selected-WAD 相对路径与 stat 快照。

    Args:
        identity: 相对于游戏根的规范化 WAD identity。
        size: 选择时记录的文件大小。
        mtime_ns: 选择时记录的纳秒级修改时间。
    """

    identity: str
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        """拒绝绝对路径与不受支持的 WAD 后缀。"""
        identity = _normalize_wad_identity(self.identity)
        if not identity.casefold().endswith(_WAD_SUFFIX):
            raise ResourcePackSelectionError(f"资源包必须是 .wad.client 文件: {self.identity}")
        if self.size < 0 or self.mtime_ns < 0:
            raise ResourcePackSelectionError("资源包 WAD 的 stat 快照无效。")
        object.__setattr__(self, "identity", identity)

    @classmethod
    def from_path(cls, game_root: Path, path: Path) -> ResourcePackWadRef:
        """从用户选定的本地 WAD 创建受限相对引用。

        Args:
            game_root: 当前游戏根目录。
            path: 用户选定的 WAD 绝对或相对路径。

        Returns:
            已记录相对 identity 与 stat 快照的引用。

        Raises:
            ResourcePackSelectionError: 文件不存在、越出 FINAL 或后缀不受支持时抛出。
        """
        root = _resolve_game_root(game_root)
        candidate = path if path.is_absolute() else root / path
        resolved = _resolve_final_wad(root, candidate)
        stat = resolved.stat()
        return cls(
            identity=_normalize_wad_identity(resolved.relative_to(root).as_posix()),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )

    def resolve(self, game_root: Path) -> Path:
        """重新验证 selected-WAD，并返回已 containment 校验的绝对路径。

        Args:
            game_root: 当前游戏根目录。

        Returns:
            已解析且仍位于 FINAL 下的 WAD 路径。

        Raises:
            ResourcePackSelectionError: 文件已不存在或不再位于 FINAL 时抛出。
            ResourcePackStaleSelectionError: stat 快照与选择时不一致时抛出。
        """
        root = _resolve_game_root(game_root)
        path = _resolve_final_wad(root, root / Path(self.identity))
        stat = path.stat()
        if stat.st_size != self.size or stat.st_mtime_ns != self.mtime_ns:
            raise ResourcePackStaleSelectionError(f"资源包 WAD 已变化，请重新选择: {self.identity}")
        return path

    @property
    def fingerprint(self) -> str:
        """返回用于发现冲突比较的稳定 source fingerprint。"""
        raw = f"{self.identity.casefold()}\0{self.size}\0{self.mtime_ns}".encode()
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ResourcePackKey:
    """解析后的 resource-pack 稳定 key。

    Args:
        wad: WAD basename 派生的原始组件。
        namespace: BIN BANK_UNITS category 派生的原始组件。
    """

    wad: str
    namespace: str

    @property
    def value(self) -> str:
        """返回冻结的 string identity。"""
        return build_resource_pack_key(self.wad, self.namespace)


@dataclass(frozen=True, slots=True)
class SpecialTargetPartition:
    """将异构 special target 安全分成数值英雄与资源包 key。

    Args:
        champion_targets: 保持原有 ``champion:<id>`` 合同的 targets。
        resource_pack_targets: 已验证 canonical 的 resource-pack keys。
    """

    champion_targets: tuple[str, ...]
    resource_pack_targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveredResourcePack:
    """供应用层与 catalog 读取的轻量发现结果行。

    Args:
        key: resource pack 稳定 key。
        wad: 相对于游戏根的来源 WAD identity。
        namespace: 成功解析的 BANK_UNITS category。
        status: 本次发现或写入状态。
        completeness: v2 binding artifact 的完整度；未生成 artifact 时为 ``None``。
    """

    key: str
    wad: str
    namespace: str
    status: str
    completeness: str | None = None


def build_resource_pack_key(wad: str, namespace: str) -> str:
    """构造可逆且跨运行稳定的 resource-pack key。

    Args:
        wad: WAD basename 或已去后缀的 WAD 名称。
        namespace: 从成功 BANK_UNITS 读取的非空 category。

    Returns:
        ``resource_pack:<wad-component>:<namespace-component>`` 格式的 key。

    Raises:
        ValueError: 任一组件为空时抛出。
    """
    wad_name = PurePosixPath(str(wad).replace("\\", "/")).name
    if wad_name.casefold().endswith(_WAD_SUFFIX):
        wad_name = wad_name[: -len(_WAD_SUFFIX)]
    return f"{RESOURCE_PACK_PREFIX}:{canonical_resource_pack_component(wad_name)}:{canonical_resource_pack_component(namespace)}"


def build_resource_pack_key_for_wad(wad: ResourcePackWadRef, namespace: str) -> str:
    """基于 selected-WAD identity 与 BANK_UNITS category 构造 stable key。"""
    return build_resource_pack_key(PurePosixPath(wad.identity).name, namespace)


def canonical_resource_pack_component(value: str) -> str:
    """把一个 resource-pack 组件规范化为可逆 percent 编码。

    Args:
        value: 原始 WAD basename 或 category。

    Returns:
        仅保留 RFC 3986 unreserved 字符的 canonical percent 编码。

    Raises:
        ValueError: 空白或规范化后为空时抛出。
    """
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    if not normalized:
        raise ValueError("resource pack key 组件不能为空。")
    return quote_from_bytes(normalized.encode("utf-8"), safe="-._~")


def parse_resource_pack_key(value: str) -> ResourcePackKey:
    """验证并解析 canonical resource-pack key。

    Args:
        value: 待验证的 stable key。

    Returns:
        可重新构造相同 stable key 的组件对象。

    Raises:
        ValueError: key 格式、UTF-8 编码或 canonical 形式无效时抛出。
    """
    prefix, separator, rest = str(value).partition(":")
    if prefix != RESOURCE_PACK_PREFIX or separator != ":":
        raise ValueError(f"资源包目标无效: {value}")
    wad_component, separator, namespace_component = rest.partition(":")
    if not separator or ":" in namespace_component:
        raise ValueError(f"资源包目标无效: {value}")
    wad = _decode_component(wad_component)
    namespace = _decode_component(namespace_component)
    parsed = ResourcePackKey(wad=wad, namespace=namespace)
    if parsed.value != value:
        raise ValueError(f"资源包目标必须使用 canonical key: {value}")
    return parsed


def resource_pack_path_component(key: str) -> str:
    """将完整 stable key 转换成 Windows-safe artifact 文件名组件。"""
    parse_resource_pack_key(key)
    return quote_from_bytes(key.encode("utf-8"), safe="-._~")


def partition_special_targets(targets: tuple[str, ...] | list[str]) -> SpecialTargetPartition:
    """分区异构 special target，禁止 resource pack 进入数值英雄归约。

    Args:
        targets: ``champion:<id>`` 与 ``resource_pack:...`` 的混合选择。

    Returns:
        保持原始英雄顺序与 canonical resource-pack 顺序的分区结果。

    Raises:
        ValueError: target 前缀未知或 resource-pack key 非 canonical 时抛出。
    """
    champion_targets: list[str] = []
    resource_pack_targets: list[str] = []
    seen_champions: set[str] = set()
    seen_resource_packs: set[str] = set()
    for target in targets:
        value = str(target)
        if value.startswith("champion:"):
            if value not in seen_champions:
                seen_champions.add(value)
                champion_targets.append(value)
            continue
        if value.startswith(f"{RESOURCE_PACK_PREFIX}:"):
            key = parse_resource_pack_key(value).value
            if key not in seen_resource_packs:
                seen_resource_packs.add(key)
                resource_pack_targets.append(key)
            continue
        raise ValueError(f"特殊内容目标无效: {target}")
    return SpecialTargetPartition(tuple(champion_targets), tuple(resource_pack_targets))


def _resolve_final_wad(game_root: Path, path: Path) -> Path:
    """解析 WAD 并确认最终目标没有经 symlink 逃出 FINAL。"""
    try:
        final_root = (game_root / FINAL_DIR).resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(final_root)
    except (OSError, ValueError) as exc:
        raise ResourcePackSelectionError("资源包 WAD 必须位于当前游戏的 Game/DATA/FINAL 下。") from exc
    if not resolved.is_file() or not resolved.name.casefold().endswith(_WAD_SUFFIX):
        raise ResourcePackSelectionError(f"资源包必须是 FINAL 下现有的 .wad.client 文件: {path}")
    return resolved


def _resolve_game_root(game_root: Path) -> Path:
    """解析当前游戏根，并把底层文件系统错误收敛为选择错误。"""
    try:
        return game_root.resolve(strict=True)
    except OSError as exc:
        raise ResourcePackSelectionError(f"当前游戏根目录不存在或不可访问: {game_root}") from exc


def _normalize_wad_identity(path: str | PurePosixPath) -> str:
    """规范化相对 WAD identity，避免 app 层反向依赖 model package。"""
    text = str(path).replace("\\", "/")
    if text.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(text):
        raise ResourcePackSelectionError(f"WAD identity 必须相对于游戏根目录: {path}")
    parts = tuple(part for part in text.split("/") if part not in {"", "."})
    if not parts or ".." in parts or ":" in parts[0]:
        raise ResourcePackSelectionError(f"WAD identity 无效或可能逃逸游戏根目录: {path}")
    return "/".join(parts)


def _decode_component(component: str) -> str:
    """解码并验证单个 percent 编码 key 组件。"""
    if not component or _PERCENT_ESCAPE_RE.search(component):
        raise ValueError(f"资源包 key 组件无效: {component}")
    try:
        decoded = unquote_to_bytes(component).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"资源包 key 组件不是有效 UTF-8: {component}") from exc
    if canonical_resource_pack_component(decoded) != component:
        raise ValueError(f"资源包 key 组件不是 canonical 编码: {component}")
    return decoded


__all__ = [
    "RESOURCE_PACK_ENTITY_TYPE",
    "RESOURCE_PACK_GROUP",
    "RESOURCE_PACK_PREFIX",
    "DiscoveredResourcePack",
    "ResourcePackKey",
    "ResourcePackSelectionError",
    "ResourcePackStaleSelectionError",
    "ResourcePackWadRef",
    "SpecialTargetPartition",
    "build_resource_pack_key",
    "build_resource_pack_key_for_wad",
    "canonical_resource_pack_component",
    "parse_resource_pack_key",
    "partition_special_targets",
    "resource_pack_path_component",
]
