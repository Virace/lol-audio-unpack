"""单实体导出的冻结请求，供 GUI 和后台执行器共享。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .audio_scope import AudioScope
from .outputs import output_path
from .types import WavOutputOptions


def freeze_unlisted_export(request: AudioExportRequest, files: tuple[Path, ...] | None) -> AudioExportRequest:
    """为没有成功清单的本地音频冻结已索引文件，避免目录导出误取空清单。

    Args:
        request: 当前浏览版本的导出快照。
        files: 当前实体已完成的音频索引；未完成时为 None。

    Raises:
        ValueError: 无清单的目录范围尚未完成索引。
    """
    if request.library_root is None or not any(target.scope.directories for target in request.targets):
        return request
    kind = {"champions": "champion", "maps": "map", "resource_packs": "resource_pack"}[request.entity_type]
    manifest = output_path(request.library_root, request.version, request.region, kind, request.entity_id)
    if manifest.is_file():
        return request
    if files is None:
        raise ValueError("此版本缺少音频清单，请等待全部音频索引完成后再导出。")
    targets = []
    for target in request.targets:
        scope = target.scope
        selected = tuple(
            path.relative_to(scope.root).as_posix()
            for path in files
            if path.is_relative_to(scope.root) and scope.contains(path.relative_to(scope.root).as_posix())
        )
        targets.append(replace(target, scope=replace(scope, directories=(), files=(*scope.files, *selected))))
    return replace(request, targets=tuple(targets))


@dataclass(frozen=True, slots=True)
class ExportTarget:
    """描述一个领域源目录与其对应的镜像输出位置。"""

    scope: AudioScope
    output_root: Path


@dataclass(frozen=True, slots=True)
class AudioExportRequest:
    """冻结单个实体的导出范围和一次性冲突策略。

    多个 target 仅兼容既有按音频类型分目录的输出布局，不表示跨实体选择。
    """

    entity_type: str
    entity_id: str
    entity_name: str
    version: str
    version_root: Path
    targets: tuple[ExportTarget, ...]
    report_root: Path
    options: WavOutputOptions
    overwrite: bool = False
    output_file: Path | None = None
    reveal_output: bool = False
    region: str | None = None
    library_root: Path | None = None

    def build_targets(self, scopes: tuple[AudioScope, ...], output_root: Path) -> tuple[ExportTarget, ...]:
        """将所选目录视为 WAV 根，保留版本、语言和实体的完整镜像路径。

        Args:
            scopes: 当前实体内已选定的导出范围。
            output_root: 用户选择的额外 WAV 根目录。

        Returns:
            与正式 WAV 布局一致的镜像输出目标。
        """
        self.validate()
        root = self.version_root.resolve()
        prefix = Path(self.version)
        if root.name != self.version:
            prefix /= root.name
        # 范围根只用于限定输入，不能截掉输出中的类型、英雄或皮肤归属。
        return tuple(
            ExportTarget(scope, output_root / prefix / scope.root.resolve().relative_to(root)) for scope in scopes
        )

    def validate(self) -> None:
        """在后台开始前复核冻结的版本路径与所有源目录边界。"""
        if not self.targets:
            raise ValueError("尚未选择导出音频")
        root = self.version_root.resolve()
        if root.name != self.version and not (
            self.region and root.parent.name == self.version and root.name == self.region
        ):
            raise ValueError("导出版本与音频目录不匹配，请重新选择当前实体")
        for target in self.targets:
            target.scope.root.resolve().relative_to(root)
        if self.output_file is not None and len(self.targets) != 1:
            raise ValueError("单文件另存为只能使用一个源目录")
