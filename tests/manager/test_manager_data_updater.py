"""结构化游戏数据更新与持久化边界测试。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from lol_audio_unpack.manager import data_updater as m_data_updater
from lol_audio_unpack.manager.errors import ArtifactWriteError

pytestmark = pytest.mark.integration


def _build_updater(game_path: Path, version: str = "16.3"):
    updater = m_data_updater.DataUpdater.__new__(m_data_updater.DataUpdater)
    updater.ctx = SimpleNamespace(
        config=SimpleNamespace(
            game_path=game_path,
            output_path=game_path,
            game_region="zh_CN",
            dev_mode=False,
            lobby_audio=False,
        ),
        runtime_cache={},
        paths=SimpleNamespace(
            game_maps_path=game_path / "Game" / "DATA" / "FINAL" / "Maps" / "Shipping",
            game_champion_path=game_path / "Game" / "DATA" / "FINAL" / "Champions",
            game_lcu_path=game_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data",
        ),
    )
    updater.game_path = game_path
    updater.version = version
    return updater


def test_process_data_propagates_final_artifact_copy_failure(tmp_path, monkeypatch):
    updater = _build_updater(tmp_path)
    updater.process_languages = []
    updater.version_manifest_path = tmp_path / "manifest" / updater.version
    updater.data_file_base = updater.version_manifest_path / "data"
    target = updater.data_file_base.with_suffix(".msgpack")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-data")

    def build_temp_data(temp_path: Path) -> None:
        source = temp_path / updater.version / "data.msgpack"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"new-data")

    def fail_copy(_source: Path, path: Path) -> Path:
        raise ArtifactWriteError(path, "replace")

    monkeypatch.setattr(updater, "_merge_and_build_data", build_temp_data)
    monkeypatch.setattr(m_data_updater, "copy_file_atomic", fail_copy)

    with pytest.raises(ArtifactWriteError, match="replace"):
        updater._process_data(tmp_path / "run")

    assert target.read_bytes() == b"old-data"


def test_check_and_update_propagates_artifact_write_error(tmp_path, monkeypatch):
    updater = _build_updater(tmp_path)
    updater.force_update = True
    updater.temp_path = tmp_path / "temp"
    updater.version_manifest_path = tmp_path / "manifest" / updater.version
    updater.data_file_base = updater.version_manifest_path / "data"
    error = ArtifactWriteError(updater.data_file_base.with_suffix(".msgpack"), "replace")

    def fail_update(_temp_path: Path) -> None:
        raise error

    monkeypatch.setattr(updater, "_process_data", fail_update)

    with pytest.raises(ArtifactWriteError) as raised:
        updater.check_and_update()

    assert raised.value is error


def test_merge_and_build_data_requires_default_champion_summary(tmp_path, monkeypatch):
    updater = _build_updater(tmp_path)
    monkeypatch.setattr(updater, "_load_language_json", lambda _base, _name: {})

    with pytest.raises(FileNotFoundError, match="default 语言的英雄概要数据"):
        updater._merge_and_build_data(tmp_path)


def test_check_languages_reads_canonical_metadata(tmp_path):
    updater = _build_updater(tmp_path)
    updater.data_file_base = tmp_path / "manifest" / updater.version / "data"
    updater.process_languages = ["default", "zh_CN"]
    m_data_updater.write_data(
        {"metadata": {"gameVersion": updater.version, "languages": ["zh_CN"]}},
        updater.data_file_base,
        dev_mode=False,
    )

    assert updater._check_languages() is True


def test_check_languages_treats_empty_list_as_default_only(tmp_path):
    updater = _build_updater(tmp_path)
    updater.data_file_base = tmp_path / "manifest" / updater.version / "data"
    m_data_updater.write_data(
        {"metadata": {"gameVersion": updater.version, "languages": []}},
        updater.data_file_base,
        dev_mode=False,
    )

    updater.process_languages = ["default"]
    assert updater._check_languages() is True

    updater.process_languages = ["default", "zh_CN"]
    assert updater._check_languages() is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"metadata": {}},
        {"metadata": {"languages": "zh_CN"}},
        {"metadata": {"languages": [None]}},
        {"metadata": {"languages": [""]}},
    ],
)
def test_check_languages_rejects_invalid_metadata(tmp_path, payload):
    updater = _build_updater(tmp_path)
    updater.data_file_base = tmp_path / "manifest" / updater.version / "data"
    updater.process_languages = ["default"]
    m_data_updater.write_data(payload, updater.data_file_base, dev_mode=False)

    assert updater._check_languages() is False


@pytest.mark.parametrize("lobby_audio", [False, True])
def test_check_and_update_skips_when_canonical_languages_are_fresh(tmp_path, monkeypatch, lobby_audio):
    updater = _build_updater(tmp_path)
    updater.ctx.config.lobby_audio = lobby_audio
    updater.force_update = False
    updater.temp_path = tmp_path / "temp"
    updater.version_manifest_path = tmp_path / "manifest" / updater.version
    updater.data_file_base = updater.version_manifest_path / "data"
    updater.process_languages = ["default", "zh_CN"]
    m_data_updater.write_data(
        {"metadata": {"gameVersion": updater.version, "languages": ["zh_CN"]}, "champions": {"1": {}}},
        updater.data_file_base,
        dev_mode=False,
    )
    process_calls: list[Path] = []
    lobby_calls = []
    monkeypatch.setattr(updater, "ensure_lobby_audio", lambda ids: lobby_calls.append(tuple(ids)))
    monkeypatch.setattr(updater, "_process_data", process_calls.append)

    result = updater.check_and_update()

    assert result == updater.data_file_base
    assert process_calls == []
    assert lobby_calls == ([("1",)] if lobby_audio else [])


def test_extract_wad_data_collects_all_default_asset_volumes(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "default-assets.wad").write_bytes(b"")
    (wad_root / "default-assets2.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["default-assets.wad", "default-assets2.wad"],
                    "perLocaleAssetBundles": {"zh_CN": ["zh_CN-assets.wad"]},
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            calls.append((self.path.name, list(hash_table)))
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater._extract_wad_data(tmp_path / "out", "en_us")

    assert [name for name, _ in calls] == ["default-assets.wad", "default-assets2.wad"]
    for _, hash_table in calls:
        assert "plugins/rcp-be-lol-game-data/global/default/v1/champion-summary.json" in hash_table
        assert "plugins/rcp-be-lol-game-data/global/default/v1/maps.json" in hash_table


def test_extract_wad_data_collects_all_region_asset_volumes(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "zh_CN-assets.wad").write_bytes(b"")
    (wad_root / "zh_CN-assets2.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["default-assets.wad"],
                    "perLocaleAssetBundles": {"zh_CN": ["zh_CN-assets.wad", "zh_CN-assets2.wad"]},
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            calls.append((self.path.name, list(hash_table)))
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater._extract_wad_data(tmp_path / "out", "zh_CN")

    assert [name for name, _ in calls] == ["zh_CN-assets.wad", "zh_CN-assets2.wad"]
    for _, hash_table in calls:
        assert "plugins/rcp-be-lol-game-data/global/zh_CN/v1/champion-summary.json" in hash_table
        assert "plugins/rcp-be-lol-game-data/global/zh_CN/v1/maps.json" in hash_table


def test_extract_wad_data_uses_description_global_bundle_list(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "base-assets-a.wad").write_bytes(b"")
    (wad_root / "base-assets-b.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["base-assets-a.wad", "base-assets-b.wad"],
                    "perLocaleAssetBundles": {"zh_CN": ["zh_CN-assets.wad"]},
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            calls.append((self.path.name, list(hash_table)))
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater._extract_wad_data(tmp_path / "out", "en_us")

    assert [name for name, _ in calls] == ["base-assets-a.wad", "base-assets-b.wad"]


def test_extract_wad_data_uses_description_locale_bundle_list(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "zh_CN-pack-a.wad").write_bytes(b"")
    (wad_root / "zh_CN-pack-b.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["default-assets.wad"],
                    "perLocaleAssetBundles": {"zh_CN": ["zh_CN-pack-a.wad", "zh_CN-pack-b.wad"]},
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            calls.append((self.path.name, list(hash_table)))
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater._extract_wad_data(tmp_path / "out", "zh_CN")

    assert [name for name, _ in calls] == ["zh_CN-pack-a.wad", "zh_CN-pack-b.wad"]


def test_extract_wad_data_tries_champion_details_after_summary(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "default-assets.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["default-assets.wad"],
                    "perLocaleAssetBundles": {},
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            calls.append((self.path.name, list(hash_table)))

            # 第一轮提取基础文件时，伪造 champion-summary.json 供后续读取
            if any(path.endswith("champion-summary.json") for path in hash_table):
                summary_virtual_path = next(path for path in hash_table if path.endswith("champion-summary.json"))
                summary_output = out_dir(summary_virtual_path)
                summary_output.parent.mkdir(parents=True, exist_ok=True)
                summary_output.write_text(
                    json.dumps([{"id": 1, "alias": "Annie", "name": "安妮"}, {"id": -1, "alias": "None", "name": ""}]),
                    encoding="utf-8",
                )
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater._extract_wad_data(tmp_path / "out", "en_us")

    all_requested_paths = [path for _, hash_table in calls for path in hash_table]
    assert "plugins/rcp-be-lol-game-data/global/default/v1/champions/1.json" in all_requested_paths


def test_extract_wad_data_logs_bin_metadata_preparation(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "default-assets.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["default-assets.wad"],
                    "perLocaleAssetBundles": {},
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            if any(path.endswith("champion-summary.json") for path in hash_table):
                summary_virtual_path = next(path for path in hash_table if path.endswith("champion-summary.json"))
                summary_output = out_dir(summary_virtual_path)
                summary_output.parent.mkdir(parents=True, exist_ok=True)
                summary_output.write_text(
                    json.dumps([{"id": 1, "alias": "Annie", "name": "安妮"}, {"id": -1, "alias": "None", "name": ""}]),
                    encoding="utf-8",
                )
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    log_lines: list[str] = []
    logger.enable("lol_audio_unpack")
    sink_id = logger.add(lambda message: log_lines.append(str(message).rstrip()), format="{level}|{message}")

    try:
        updater._extract_wad_data(tmp_path / "out", "en_us")
    finally:
        logger.remove(sink_id)

    assert any("DEBUG|准备提取 1 个英雄详细信息，用于后续 bin 元数据装配" in line for line in log_lines)
    assert any("INFO|英雄信息提取完成，共 1 个英雄，将进入 bin 元数据装配" in line for line in log_lines)


def test_extract_wad_data_returns_when_region_wad_missing(tmp_path, monkeypatch):
    calls = []

    class FakeWAD:
        def __init__(self, path):
            self.path = path

        def extract(self, hash_table, out_dir):
            calls.append((self.path, hash_table))
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater._extract_wad_data(tmp_path / "out", "zh_CN")

    assert calls == []


def test_extract_wad_data_includes_lobby_audio_when_enabled(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "zh_CN-assets.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["default-assets.wad"],
                    "perLocaleAssetBundles": {"zh_CN": ["zh_CN-assets.wad"]},
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            calls.append((self.path.name, list(hash_table)))

            if any(path.endswith("champion-summary.json") for path in hash_table):
                summary_virtual_path = next(path for path in hash_table if path.endswith("champion-summary.json"))
                summary_output = out_dir(summary_virtual_path)
                summary_output.parent.mkdir(parents=True, exist_ok=True)
                summary_output.write_text(
                    json.dumps([{"id": 1, "alias": "Annie", "name": "安妮"}, {"id": -1, "alias": "None", "name": ""}]),
                    encoding="utf-8",
                )
            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater.ctx.config.lobby_audio = True
    updater._extract_wad_data(tmp_path / "out", "zh_CN")

    all_requested_paths = [path for _, hash_table in calls for path in hash_table]
    assert "plugins/rcp-be-lol-game-data/global/zh_CN/v1/champion-ban-vo/1.ogg" in all_requested_paths
    assert "plugins/rcp-be-lol-game-data/global/zh_CN/v1/champion-choose-vo/1.ogg" in all_requested_paths
    assert "plugins/rcp-be-lol-game-data/global/default/v1/champion-sfx-audios/1.ogg" in all_requested_paths


def test_extract_wad_data_writes_default_sfx_audio_into_region_output(tmp_path, monkeypatch):
    wad_root = tmp_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    wad_root.mkdir(parents=True, exist_ok=True)
    (wad_root / "zh_CN-assets.wad").write_bytes(b"")
    (wad_root / "description.json").write_text(
        json.dumps(
            {
                "riotMeta": {
                    "globalAssetBundles": ["default-assets.wad"],
                    "perLocaleAssetBundles": {"zh_CN": ["zh_CN-assets.wad"]},
                }
            }
        ),
        encoding="utf-8",
    )

    extracted_outputs: list[Path] = []

    class FakeWAD:
        def __init__(self, path):
            self.path = Path(path)

        def extract(self, hash_table, out_dir):
            if any(path.endswith("champion-summary.json") for path in hash_table):
                summary_virtual_path = next(path for path in hash_table if path.endswith("champion-summary.json"))
                summary_output = out_dir(summary_virtual_path)
                summary_output.parent.mkdir(parents=True, exist_ok=True)
                summary_output.write_text(
                    json.dumps([{"id": 1, "alias": "Annie", "name": "安妮"}]),
                    encoding="utf-8",
                )

            for path in hash_table:
                if path.endswith("champion-sfx-audios/1.ogg"):
                    output = out_dir(path)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"sfx")
                    extracted_outputs.append(output)

            return []

    monkeypatch.setattr(m_data_updater, "WAD", FakeWAD)

    updater = _build_updater(tmp_path)
    updater.ctx.config.lobby_audio = True
    updater._extract_wad_data(tmp_path / "out", "zh_CN")

    assert extracted_outputs == [tmp_path / "out" / updater.version / "zh_CN" / "champion-sfx-audios" / "1.ogg"]
    assert extracted_outputs[0].read_bytes() == b"sfx"


def test_persist_lobby_audio_files_keeps_sfx_in_lobby(tmp_path):
    updater = _build_updater(tmp_path)
    updater.version_manifest_path = tmp_path / "manifest" / updater.version
    updater.process_languages = ["zh_CN"]

    temp_root = tmp_path / "temp"
    source_dir = temp_root / updater.version / "zh_CN" / "champion-sfx-audios"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "1.ogg").write_bytes(b"sfx")

    updater._persist_lobby_audio_files(temp_root)

    target_file = updater.version_manifest_path / "lobby" / "zh_CN" / "champion-sfx-audios" / "1.ogg"
    assert target_file.read_bytes() == b"sfx"
    assert not (tmp_path / "audios/_data").exists()


def test_merge_and_build_data_logs_bin_metadata_summary(tmp_path):
    updater = _build_updater(tmp_path)
    updater.process_languages = ["default"]
    updater._is_dev_mode = lambda: False

    base_path = tmp_path / updater.version / "default"
    champions_path = base_path / "champions"
    champions_path.mkdir(parents=True, exist_ok=True)
    (base_path / "champion-summary.json").write_text(
        json.dumps([{"id": 1, "alias": "Annie", "name": "安妮", "description": "desc"}]),
        encoding="utf-8",
    )
    (champions_path / "1.json").write_text(
        json.dumps(
            {
                "title": "黑暗之女",
                "skins": [
                    {
                        "id": "1000",
                        "name": "经典",
                        "isBase": True,
                        "chromas": [{"id": "1001", "name": "猩红"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (base_path / "maps.json").write_text(
        json.dumps([{"id": 11, "mapStringId": "SR", "name": "召唤师峡谷"}]),
        encoding="utf-8",
    )

    captured = {}
    log_lines: list[str] = []

    def fake_write_data(data: dict, _base: Path, *, dev_mode: bool) -> None:
        captured["data"] = data
        captured["dev_mode"] = dev_mode

    original_write_data = m_data_updater.write_data
    m_data_updater.write_data = fake_write_data
    logger.enable("lol_audio_unpack")
    sink_id = logger.add(lambda message: log_lines.append(str(message).rstrip()), format="{level}|{message}")
    try:
        updater._merge_and_build_data(tmp_path)
    finally:
        logger.remove(sink_id)
        m_data_updater.write_data = original_write_data

    assert captured["dev_mode"] is False
    assert any("INFO|合并英雄数据并装配 bin 元数据..." in line for line in log_lines)
    assert any(
        "DEBUG|英雄 bin 元数据装配完成，共 1 个英雄，1 个皮肤 binPath，1 个炫彩 binPath" in line for line in log_lines
    )
    assert any("INFO|合并地图数据并装配 bin 元数据..." in line for line in log_lines)
    assert any("DEBUG|地图 bin 元数据装配完成，共 1 个地图 binPath" in line for line in log_lines)
