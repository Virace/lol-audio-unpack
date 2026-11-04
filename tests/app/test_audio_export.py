"""验证额外 WAV 根目录保留正式输出的版本、语言和实体归属。"""

from pathlib import Path

import pytest

from lol_audio_unpack.app.audio_export import AudioExportRequest, ExportTarget, freeze_unlisted_export
from lol_audio_unpack.app.audio_scope import AudioScope, MappingNode
from lol_audio_unpack.app.types import WavOutputOptions
from lol_audio_unpack.manager.files import write_data
from lol_audio_unpack.runtime.wav import build_output_path

pytestmark = pytest.mark.unit


def test_unlisted_export_freezes_index_without_losing_exclusions_or_nodes(tmp_path: Path) -> None:
    """缺少成功清单时使用完成的索引导出目录，并保留排除项与单独选择的事件节点。"""
    version_root = tmp_path / "audios/16.17/zh_CN"
    root = version_root / "champions/1"
    files = tuple(root / name for name in ("1000/VO/1.wem", "1001/VO/2.wem", "1001/VO/3.wem"))
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    mapping = tmp_path / "hashes/16.17/zh_CN/champions/1.msgpack"
    write_data(
        {
            "skins": {
                "1001": {
                    "events": {"VO": {"event": ["2", "3"]}},
                    "audioPaths": {"VO": {"event": ["1001/VO/2.wem", "1001/VO/3.wem"]}},
                }
            }
        },
        mapping,
    )
    stat = mapping.stat()
    node = MappingNode(mapping, "champions", "1", (stat.st_size, stat.st_mtime_ns), ("1001", "VO", "event"))
    scope = AudioScope(root, directories=("1000",), excluded=frozenset({"1001/VO/2.wem"}), nodes=(node,))
    request = AudioExportRequest(
        entity_type="champions",
        entity_id="1",
        entity_name="安妮",
        version="16.17",
        version_root=version_root,
        targets=(ExportTarget(scope, tmp_path / "wavs/16.17/zh_CN"),),
        report_root=tmp_path / "reports/16.17/zh_CN",
        options=WavOutputOptions(enabled=True),
        region="zh_CN",
        library_root=tmp_path,
    )

    with pytest.raises(ValueError, match="音频索引"):
        freeze_unlisted_export(request, None)
    frozen = freeze_unlisted_export(request, files)

    assert set(frozen.targets[0].scope.resolve_files()) == {files[0], files[2]}
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    ("version", "region", "files"),
    [
        ("16.18", "zh_CN", ("champions/1·Annie·安妮/1000·基础/VO/123.wem",)),
        ("16.19", "ja_JP", ("champions/1·Annie·安妮/1001·哥特萝莉/VO/123.wem",)),
        (
            "16.18",
            "en_US",
            (
                "VO/champions/1·Annie·安妮/1000·基础/123.wem",
                "SFX/champions/1·Annie·安妮/1000·基础/123.wem",
                "champions/1·Annie·安妮/lobby/choose.ogg",
            ),
        ),
    ],
)
def test_extra_wav_root_keeps_full_layout(tmp_path: Path, version: str, region: str, files: tuple[str, ...]) -> None:
    """额外根替代 wavs，单个与多个类型范围都保留版本、语言及完整实体路径。"""
    root = tmp_path / "audios" / version / region
    scopes = tuple(AudioScope(root / Path(file).parent, files=(Path(file).name,)) for file in files)
    request = AudioExportRequest(
        entity_type="champions",
        entity_id="1",
        entity_name="安妮",
        version=version,
        version_root=root,
        targets=tuple(ExportTarget(scope, tmp_path / "wavs") for scope in scopes),
        report_root=tmp_path / "reports",
        options=WavOutputOptions(enabled=True),
        region=region,
    )
    output = tmp_path / "selected-wavs"

    targets = request.build_targets(scopes, output)
    outputs = tuple(
        build_output_path(root / file, audio_root=target.scope.root, wav_root=target.output_root)
        for file, target in zip(files, targets, strict=True)
    )

    assert outputs == tuple(output / version / region / Path(file).with_suffix(".wav") for file in files)
