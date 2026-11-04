"""验证来源诊断随目录选择保存，任务上下文仍按配置独立构建。"""

from pathlib import Path
from types import SimpleNamespace

from loguru import logger

from lol_audio_unpack.app import source_info
from lol_audio_unpack.gui.service import source_context
from tests.factories import make_context


def test_source_diagnostics_follow_directory_instead_of_context(tmp_path, monkeypatch) -> None:
    """语言和输出变化不重复读取安装声明，切换根目录后才重新识别。"""
    roots = [tmp_path / "riot", tmp_path / "tencent"]
    for root, region in zip(roots, ("TW2", "TENCENT"), strict=True):
        (root / "Plugins" / "rcp-be-lol-game-data").mkdir(parents=True)
        (root / "system.yaml").write_text(f"default_region: {region}\n", encoding="utf-8")
    reads = []
    read_region = source_info._read_region

    def record_read(path, keys):
        reads.append(path)
        return read_region(path, keys)

    monkeypatch.setattr(source_info, "_read_region", record_read)
    monkeypatch.setattr(
        source_context,
        "create_app_context",
        lambda *, settings, **_kwargs: make_context(
            Path(settings["OUTPUT_PATH"]), game_path=Path(settings["GAME_PATH"]), game_region=settings["GAME_REGION"]
        ),
    )
    monkeypatch.setattr(
        source_context,
        "scan_inventory",
        lambda _root: SimpleNamespace(languages=("en_US",), get_language=lambda _: None),
    )
    messages = []
    logger.enable("lol_audio_unpack")
    sink_id = logger.add(lambda message: messages.append(message.record.copy()), level="INFO")
    settings = {"GAME_PATH": str(roots[0]), "GAME_REGION": "", "OUTPUT_PATH": str(tmp_path / "output")}
    builder = source_context.SourceContextBuilder()
    try:
        initial = builder(settings=settings)
        settings["GAME_REGION"] = "default"
        selected = builder(settings=settings)
        settings["OUTPUT_PATH"] = str(tmp_path / "other-output")
        builder(settings=settings)
        settings["GAME_REGION"] = "zh_CN"
        builder(settings=settings)
        expected_reads = [roots[0] / "system.yaml", roots[0] / "Config" / "LeagueClientSettings.yaml"]
        assert reads == expected_reads
        settings["GAME_PATH"] = str(roots[1])
        builder(settings=settings)
    finally:
        logger.remove(sink_id)

    assert reads == expected_reads + [roots[1] / "system.yaml", roots[1] / "Config" / "LeagueClientSettings.yaml"]
    assert initial is not selected
    sources = [record["message"] for record in messages if record["name"] == source_info.__name__]
    assert len(sources) == len(roots)
    assert "TW2" in sources[0] and "TENCENT" in sources[1]
    assert sum("en_US" in record["message"] for record in messages) == 1
