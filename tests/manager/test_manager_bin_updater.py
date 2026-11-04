"""英雄与地图 BIN 更新、缓存复用及逐实体结果测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from lol_audio_unpack.manager import bin_source as m_bin_source
from lol_audio_unpack.manager import bin_updater as m_bin_updater
from lol_audio_unpack.manager import champion_bin_processor as m_champion_processor
from lol_audio_unpack.manager import map_bin_processor as m_map_processor
from lol_audio_unpack.manager.files import needs_update, read_data, write_data
from lol_audio_unpack.manager.update_result import UpdateEntityResult, UpdateStatus
from lol_audio_unpack.model.binding import RESOURCE_SCHEMA_VERSION, BankBinding, BinBinding, BindingRole, BindingStatus
from lol_audio_unpack.utils.run_summary import get_or_create_run_summary

pytestmark = pytest.mark.unit


def test_process_champion_skins_reports_missing_bin_as_failure_input(tmp_path, monkeypatch):
    """验证所有 declared BIN 缺失时会返回失败的 v2 结果。"""
    processor = m_champion_processor.ChampionBinProcessor.__new__(m_champion_processor.ChampionBinProcessor)
    processor.ctx = SimpleNamespace(config=SimpleNamespace(game_path=tmp_path, dev_mode=False), paths=SimpleNamespace())
    processor.force_update = False
    processor.process_events = False
    processor.version = "16.3"
    processor.game_path = tmp_path
    processor.champion_banks_dir = tmp_path / "banks" / "champions"
    processor.champion_events_dir = tmp_path / "events" / "champions"
    missing_path = "data/characters/Annie/skins/skin0001.bin"
    batch = m_bin_source.BinBatch(
        raws={},
        bindings=[
            BinBinding(
                path=missing_path,
                normalized_path=missing_path.casefold(),
                wad=None,
                entry_hash="0000000000000001",
                status=BindingStatus.MISSING,
                role=BindingRole.ROOT,
            )
        ],
    )
    processor.bin_source = SimpleNamespace(
        _resolve_bin_resources=lambda *_args, **_kwargs: batch,
        _resolve_bank_bindings=lambda _references: [],
        _resource_index_diagnostics=lambda: ({"requests": 1}, []),
        _create_base_data=lambda entity_id, _entity_type, **payload: {
            "metadata": {"gameVersion": "16.3"},
            "championId": entity_id,
            **payload,
        },
    )

    monkeypatch.setattr(m_champion_processor, "needs_update", lambda *args, **kwargs: True)

    write_calls: list[dict] = []
    monkeypatch.setattr(
        m_champion_processor,
        "write_data",
        lambda data, *_args, **_kwargs: write_calls.append(data) or tmp_path / "banks.msgpack",
    )

    champion_data = {
        "alias": "Annie",
        "skins": [
            {"id": "1", "isBase": True, "binPath": missing_path},
        ],
        "wad": {"root": "Game/DATA/FINAL/Champions/Annie.wad.client"},
    }

    result = processor._process_champion_skins(champion_data, "1")

    assert result.status is UpdateStatus.FAILED
    assert write_calls[0]["diagnostics"]["completeness"] == "failed"


@pytest.mark.parametrize("event_version, expected_calls", [(1, []), (None, ["events"])])
def test_champion_bank_migration_respects_event_schema(tmp_path, monkeypatch, event_version, expected_calls) -> None:
    """banks 迁移复用完整事件缓存；旧缓存须重建，以恢复曾被过滤的 Base 声明。"""
    processor = m_champion_processor.ChampionBinProcessor.__new__(m_champion_processor.ChampionBinProcessor)
    processor.ctx = SimpleNamespace(config=SimpleNamespace(game_path=tmp_path, dev_mode=False), paths=SimpleNamespace())
    processor.force_update = False
    processor.process_events = True
    processor.version = "16.16"
    processor.game_path = tmp_path
    processor.champion_banks_dir = tmp_path / "banks" / "champions"
    processor.champion_events_dir = tmp_path / "events" / "champions"
    write_data(
        {"metadata": {"gameVersion": "16.16"}, "skinAudioVersion": event_version, "skins": {}},
        processor.champion_events_dir / "1",
        dev_mode=False,
    )
    processor.bin_source = SimpleNamespace(
        _resolve_bank_bindings=lambda _references: [
            BankBinding(
                category="Characters/Annie/Skins/Skin1/VO",
                path="assets/sounds/wwise2016/vo/annie_audio.bnk",
                normalized_path="",
                kind="BNK",
                wad="Game/DATA/FINAL/Champions/Annie.wad.client",
                entry_hash="0000000000000002",
                source_bin="data/characters/Annie/skins/skin0001.bin",
                role=BindingRole.ROOT,
                status=BindingStatus.RESOLVED,
                sub_entity="1",
                group=0,
            )
        ],
        _resource_index_diagnostics=lambda: ({"requests": 2}, []),
        _create_base_data=lambda entity_id, _entity_type, **payload: {
            "metadata": {"gameVersion": "16.16"},
            "championId": entity_id,
            **payload,
        },
    )
    bin_path = "data/characters/Annie/skins/skin0001.bin"
    fake_bin = SimpleNamespace(
        theme_music=None,
        data=[
            SimpleNamespace(
                music=None,
                bank_units=[
                    SimpleNamespace(
                        category="Characters/Annie/Skins/Skin1/VO",
                        bank_path=["assets/sounds/wwise2016/vo/annie_audio.bnk"],
                        events=[SimpleNamespace(string="Play_Annie_VO")],
                    )
                ],
            )
        ],
    )
    monkeypatch.setattr(
        processor,
        "_read_bin_batch",
        lambda *_args, **_kwargs: m_bin_source.BinBatch(
            raws={bin_path: b"bin"},
            bindings=[
                BinBinding(
                    path=bin_path,
                    normalized_path="",
                    wad="Game/DATA/FINAL/Champions/Annie.wad.client",
                    entry_hash="0000000000000001",
                    status=BindingStatus.RESOLVED,
                    role=BindingRole.ROOT,
                )
            ],
        ),
    )
    monkeypatch.setattr(m_champion_processor, "BIN", lambda _raw: fake_bin)
    monkeypatch.setattr(
        m_champion_processor,
        "needs_update",
        lambda base_path, *_args, **_kwargs: "banks" in Path(base_path).parts,
    )
    monkeypatch.setattr(
        m_champion_processor,
        "write_data",
        lambda *_args, **_kwargs: tmp_path / "banks" / "champions" / "1.msgpack",
    )
    event_parse_calls: list[str] = []
    monkeypatch.setattr(
        processor,
        "_extract_skin_events",
        lambda *_args, **_kwargs: event_parse_calls.append("events") or {"events": {}},
    )

    result = processor._process_champion_skins(
        {
            "alias": "Annie",
            "skins": [{"id": "1", "isBase": True, "binPath": bin_path}],
        },
        "1",
    )

    assert result.status is UpdateStatus.SUCCESS
    assert event_parse_calls == expected_calls


def test_skip_events_ignores_missing_champion_event_artifact(tmp_path, monkeypatch) -> None:
    """显式跳过 events 时，已就绪 banks 不应因 events 缺失而重复读取 BIN。"""
    processor = m_champion_processor.ChampionBinProcessor.__new__(m_champion_processor.ChampionBinProcessor)
    processor.ctx = SimpleNamespace(config=SimpleNamespace(game_path=tmp_path, dev_mode=False), paths=SimpleNamespace())
    processor.force_update = False
    processor.process_events = False
    processor.version = "16.16"
    processor.game_path = tmp_path
    processor.champion_banks_dir = tmp_path / "banks" / "champions"
    processor.champion_events_dir = tmp_path / "events" / "champions"
    processor.bin_source = SimpleNamespace()
    monkeypatch.setattr(
        m_champion_processor,
        "needs_update",
        lambda base_path, *_args, **_kwargs: "events" in Path(base_path).parts,
    )
    monkeypatch.setattr(
        processor,
        "_read_bin_batch",
        lambda *_args, **_kwargs: pytest.fail("skip-events 不应读取英雄 BIN"),
    )

    result = processor._process_champion_skins({"alias": "Annie", "skins": []}, "1")

    assert result.status is UpdateStatus.SUCCESS


def test_skip_events_ignores_missing_map_event_artifact(tmp_path, monkeypatch) -> None:
    """只解包时复用实际 banks 缓存，连公共地图预处理也不能重复读取 BIN。"""
    processor = m_map_processor.MapBinProcessor.__new__(m_map_processor.MapBinProcessor)
    processor.ctx = SimpleNamespace(config=SimpleNamespace(dev_mode=False), runtime_cache={})
    processor.force_update = False
    processor.process_events = False
    processor.version = "16.16"
    processor.languages = []
    processor.map_banks_dir = tmp_path / "banks" / "maps"
    processor.map_events_dir = tmp_path / "events" / "maps"
    processor._progress_callback = None
    processor.bin_source = SimpleNamespace(
        _load_map_bin_file=lambda *_args: pytest.fail("缓存齐全时不应预读公共地图 BIN"),
    )
    maps = {
        str(entity_id): {"binPath": f"data/maps/map{entity_id}.bin", "names": {"default": f"Map{entity_id}"}}
        for entity_id in (0, 11)
    }
    for entity_id in maps:
        write_data(
            {"metadata": {"gameVersion": "16.16"}, "resourceSchemaVersion": RESOURCE_SCHEMA_VERSION},
            processor.map_banks_dir / entity_id,
            dev_mode=False,
        )
    monkeypatch.setattr(
        processor,
        "_load_map_resource",
        lambda *_args, **_kwargs: pytest.fail("skip-events 不应读取地图 BIN"),
    )

    results = processor._update_maps({"maps": maps})

    assert all(result.status is UpdateStatus.SUCCESS for result in results)
    assert not list(processor.map_events_dir.iterdir())


def test_map_event_preparation_reports_missing_bin_with_cached_banks(tmp_path) -> None:
    """只补事件时 BIN 读取失败必须失败，不能借旧 banks 缓存伪装就绪。"""
    path = "data/maps/map11.bin"
    batch = m_bin_source.BinBatch(
        raws={},
        bindings=[
            BinBinding(
                path=path,
                normalized_path=path,
                wad=None,
                entry_hash="0000000000000011",
                status=BindingStatus.MISSING,
                role=BindingRole.ROOT,
            )
        ],
    )
    source = SimpleNamespace(
        _load_map_bin_resource=lambda *_args: m_bin_source.LoadedBin(None, batch),
        _resolve_bank_bindings=lambda _references: [],
        _resource_index_diagnostics=lambda: ({}, []),
    )
    processor = m_map_processor.MapBinProcessor(
        source,
        ctx=SimpleNamespace(config=SimpleNamespace(dev_mode=False)),
        version="16.17",
        force_update=False,
        process_events=True,
        map_banks_dir=tmp_path / "banks",
        map_events_dir=tmp_path / "events",
    )
    cached = {"metadata": {"gameVersion": "16.17"}, "resourceSchemaVersion": RESOURCE_SCHEMA_VERSION}
    write_data(cached, processor.map_banks_dir / "11", dev_mode=False)

    result = processor._process_single_map("11", {"binPath": path, "names": {"default": "Map11"}})

    assert result.status is UpdateStatus.FAILED
    assert not processor.map_events_dir.exists()
    assert read_data(processor.map_banks_dir / "11", dev_mode=False) == cached


@pytest.mark.parametrize("selected,expected", [(["33"], ["0", "33"]), ([], [])])
def test_update_preserves_explicit_map_scope(tmp_path, monkeypatch, selected, expected):
    """非空地图范围自动包含 Common；显式空范围不启动全量更新。"""
    updater = m_bin_updater.BinUpdater.__new__(m_bin_updater.BinUpdater)
    updater.ctx = SimpleNamespace(config=SimpleNamespace(dev_mode=False), runtime_cache={}, paths=SimpleNamespace())
    updater.force_update = False
    updater.process_events = True
    updater.version = "16.3"
    updater.data_file_base = tmp_path / "data"
    updater.languages = []
    updater.bin_source = SimpleNamespace(languages=[])
    map_updates: list[dict] = []

    def update_maps(data: dict) -> tuple[UpdateEntityResult, ...]:
        map_updates.append(data)
        return tuple(UpdateEntityResult.success("map", map_id) for map_id in data["maps"])

    updater._map_processor = SimpleNamespace(languages=[], _update_maps=update_maps)

    monkeypatch.setattr(
        m_bin_updater,
        "read_data",
        lambda *args, **kwargs: {
            "metadata": {"languages": ["zh_CN"]},
            "maps": {"0": {"id": 0}, "33": {"id": 33}},
        },
    )

    results = updater.update(target="map", map_ids=selected)

    assert [key for update in map_updates for key in update["maps"]] == expected
    assert [result.entity_id for result in results] == expected


def test_update_logs_stage_start_and_summary_for_targeted_mode(tmp_path, monkeypatch):
    """内部更新保持安静，处理结果交由应用层统一播报。"""
    updater = m_bin_updater.BinUpdater.__new__(m_bin_updater.BinUpdater)
    updater.ctx = SimpleNamespace(config=SimpleNamespace(dev_mode=False), runtime_cache={}, paths=SimpleNamespace())
    updater.force_update = False
    updater.process_events = True
    updater.version = "16.3"
    updater.data_file_base = tmp_path / "data"
    updater.languages = []
    updater.bin_source = SimpleNamespace(languages=[])
    updater._champion_processor = SimpleNamespace(
        _update_champions=lambda _data: (UpdateEntityResult.success("champion", "1"),)
    )
    updater._map_processor = SimpleNamespace(
        languages=[],
        _update_maps=lambda _data: (
            UpdateEntityResult.success("map", "0"),
            UpdateEntityResult.success("map", "11"),
        ),
    )

    info_messages: list[str] = []
    debug_messages: list[str] = []

    monkeypatch.setattr(
        m_bin_updater,
        "read_data",
        lambda *args, **kwargs: {
            "metadata": {"languages": ["zh_CN"]},
            "champions": {"1": {"alias": "Annie"}},
            "maps": {"0": {"id": 0}, "11": {"id": 11}},
        },
    )
    monkeypatch.setattr(
        m_bin_updater,
        "logger",
        SimpleNamespace(
            info=lambda message: info_messages.append(str(message)),
            debug=lambda message: debug_messages.append(str(message)),
        ),
    )
    updater.update(target="all", champion_ids=["1"], map_ids=["11"])

    assert info_messages == []
    assert debug_messages == []


def test_bin_updater_completion_does_not_report_partial_as_success(monkeypatch) -> None:
    """逐实体不完整时只能记录 warning 摘要。"""
    warning_messages: list[str] = []
    success_messages: list[str] = []
    monkeypatch.setattr(
        m_bin_updater,
        "logger",
        SimpleNamespace(
            warning=warning_messages.append,
            success=success_messages.append,
        ),
    )

    m_bin_updater.BinUpdater._log_completion(
        [UpdateEntityResult.incomplete("map", "11", message="事件绑定不完整")],
        mode="精确模式",
        champion_count=0,
        map_count=1,
    )

    assert success_messages == []
    assert warning_messages == ["BinUpdater 更新完成（精确模式）：英雄 0 个，地图 1 个，部分成功 1 个，失败 0 个"]


def test_update_filters_hidden_champions_only_in_batch_mode(tmp_path, monkeypatch):
    """验证默认批量排除特殊英雄，但精确模式仍允许显式处理。"""
    updater = m_bin_updater.BinUpdater.__new__(m_bin_updater.BinUpdater)
    updater.ctx = SimpleNamespace(config=SimpleNamespace(dev_mode=False), runtime_cache={}, paths=SimpleNamespace())
    updater.force_update = False
    updater.process_events = True
    updater.version = "16.16"
    updater.data_file_base = tmp_path / "data"
    updater.languages = []
    updater.bin_source = SimpleNamespace(languages=[])

    champion_updates: list[dict] = []
    map_updates: list[dict] = []

    def update_champions(data: dict) -> tuple[UpdateEntityResult, ...]:
        champion_updates.append(data)
        return tuple(UpdateEntityResult.success("champion", champion_id) for champion_id in data["champions"])

    def update_maps(data: dict) -> tuple[UpdateEntityResult, ...]:
        map_updates.append(data)
        return tuple(UpdateEntityResult.success("map", map_id) for map_id in data["maps"])

    updater._champion_processor = SimpleNamespace(_update_champions=update_champions)
    updater._map_processor = SimpleNamespace(languages=[], _update_maps=update_maps)

    monkeypatch.setattr(
        m_bin_updater,
        "read_data",
        lambda *args, **kwargs: {
            "metadata": {"languages": ["zh_CN"]},
            "champions": {
                "1": {"id": 1, "alias": "Annie", "wad": {"root": "Champions/Annie.wad.client"}},
                "60001": {
                    "id": 60001,
                    "alias": "Jade_Annie",
                    "wad": {"root": "Champions/Jade_Annie.wad.client"},
                },
            },
            "maps": {"11": {"id": 11}},
        },
    )

    updater.update(target="all")

    assert list(champion_updates[0]["champions"]) == ["1"]
    assert champion_updates[0]["maps"] == {"11": {"id": 11}}
    assert map_updates[0]["maps"] == {"11": {"id": 11}}

    updater.update(target="skin", champion_ids=["60001"])

    assert list(champion_updates[1]["champions"]) == ["60001"]


def test_process_single_map_records_note_when_common_dedup_removes_all_events(tmp_path, monkeypatch):
    """验证公共事件去重清空结果时会记录可解释差异。"""
    processor = m_map_processor.MapBinProcessor.__new__(m_map_processor.MapBinProcessor)
    processor.ctx = SimpleNamespace(
        config=SimpleNamespace(game_path=tmp_path, dev_mode=False),
        runtime_cache={},
        paths=SimpleNamespace(),
    )
    processor.force_update = False
    processor.process_events = True
    processor.version = "16.3"
    processor.languages = []
    processor.map_banks_dir = tmp_path / "banks" / "maps"
    processor.map_events_dir = tmp_path / "events" / "maps"

    fake_bin = SimpleNamespace(
        theme_music=None,
        data=[
            SimpleNamespace(
                music=None,
                bank_units=[
                    SimpleNamespace(
                        events=[SimpleNamespace(string="Play_Map33_SFX_Start")],
                        category="AMB_SFX",
                        bank_path=None,
                    )
                ],
            )
        ],
    )

    bin_path = "data/maps/shipping/map33/map33.bin"
    batch = m_bin_source.BinBatch(
        raws={bin_path: b"map-bin"},
        bindings=[
            BinBinding(
                path=bin_path,
                normalized_path="",
                wad="Game/DATA/FINAL/Maps/Shipping/Map33.wad.client",
                entry_hash="0000000000000033",
                status=BindingStatus.RESOLVED,
                role=BindingRole.ROOT,
            )
        ],
    )
    processor.bin_source = SimpleNamespace(
        _load_map_bin_resource=lambda *_args, **_kwargs: m_bin_source.LoadedBin(fake_bin, batch),
        _resolve_bank_bindings=lambda _references: [],
        _resource_index_diagnostics=lambda: ({"requests": 1}, []),
        _create_base_data=lambda _id, _type, **payload: {"metadata": {"gameVersion": "16.3"}, **payload},
    )

    processor._process_single_map(
        "33",
        {
            "binPath": bin_path,
            "names": {"default": "Map33"},
        },
        {"Play_Map33_SFX_Start": {"地图 0/AMB_SFX"}},
        set(),
    )

    summary = get_or_create_run_summary(processor.ctx.runtime_cache)
    assert any(
        "地图 33 (Map33) 的事件在与 地图 0 的公共事件去重后为空" in note for note in summary.stages["update"].notes
    )
    assert any("category=AMB_SFX" in detail for detail in summary.stages["update"].debug_details)
    events = processor.map_events_dir / "33"
    assert read_data(events, dev_mode=False)["map"] == {}
    assert not needs_update(events, "16.3", False, dev_mode=False)
    monkeypatch.setattr(processor, "_load_map_resource", lambda *_args: pytest.fail("空事件缓存应复用"))
    result = processor._process_single_map("33", {"names": {"default": "Map33"}})
    assert result.status is UpdateStatus.SUCCESS


def test_update_champions_logs_simple_progress_messages(tmp_path, monkeypatch):
    """验证英雄批量更新会输出简单的日志进度提示。"""
    processor = m_champion_processor.ChampionBinProcessor.__new__(m_champion_processor.ChampionBinProcessor)
    processor.ctx = SimpleNamespace(
        config=SimpleNamespace(game_path=tmp_path, dev_mode=False),
        runtime_cache={},
        paths=SimpleNamespace(),
    )
    processor.champion_banks_dir = tmp_path / "banks" / "champions"
    processor.champion_events_dir = tmp_path / "events" / "champions"
    progress_events = []
    processor._progress_callback = progress_events.append
    processed_ids: list[str] = []
    info_messages: list[str] = []
    success_messages: list[str] = []

    monkeypatch.setattr(
        m_champion_processor,
        "logger",
        SimpleNamespace(
            info=lambda message: info_messages.append(str(message)),
            success=lambda message: success_messages.append(str(message)),
        ),
    )
    monkeypatch.setattr(
        processor,
        "_process_champion_skins",
        lambda _champion_data, champion_id: (
            processed_ids.append(champion_id),
            UpdateEntityResult.success("champion", champion_id),
        )[1],
    )

    processor._update_champions({"champions": {"2": {}, "1": {}}})

    assert processed_ids == ["1", "2"]
    assert "处理英雄进度 1/2: 1" in info_messages
    assert "处理英雄进度 2/2: 2" in info_messages
    assert success_messages == []
    assert [(event.event, event.current, event.total) for event in progress_events] == [
        ("started", 0, 2),
        ("advanced", 1, 2),
        ("advanced", 2, 2),
        ("finished", 2, 2),
    ]


def test_update_maps_logs_simple_progress_messages(tmp_path, monkeypatch):
    """验证地图批量更新会输出简单的日志进度提示。"""
    processor = m_map_processor.MapBinProcessor.__new__(m_map_processor.MapBinProcessor)
    processor.ctx = SimpleNamespace(
        config=SimpleNamespace(game_path=tmp_path, dev_mode=False),
        runtime_cache={},
        paths=SimpleNamespace(),
    )
    processor.map_banks_dir = tmp_path / "banks" / "maps"
    processor.map_events_dir = tmp_path / "events" / "maps"
    processor.force_update = False
    processor.process_events = False
    processor.version = "16.3"
    progress_events = []
    processor._progress_callback = progress_events.append
    processed_ids: list[str] = []
    info_messages: list[str] = []
    success_messages: list[str] = []

    monkeypatch.setattr(
        m_map_processor,
        "logger",
        SimpleNamespace(
            info=lambda message: info_messages.append(str(message)),
            success=lambda message: success_messages.append(str(message)),
            debug=lambda _message: None,
            opt=lambda **_kwargs: SimpleNamespace(error=lambda _message: None),
        ),
    )
    monkeypatch.setattr(
        processor,
        "_process_single_map",
        lambda map_id, *_args: (
            processed_ids.append(map_id),
            UpdateEntityResult.success("map", map_id),
        )[1],
    )

    processor._update_maps({"maps": {"11": {"name": "Map11"}, "12": {"name": "Map12"}}})

    assert processed_ids == ["11", "12"]
    assert "处理地图进度 1/2: 11" in info_messages
    assert "处理地图进度 2/2: 12" in info_messages
    assert success_messages == []
    assert [(event.event, event.current, event.total) for event in progress_events] == [
        ("started", 0, 2),
        ("advanced", 1, 2),
        ("advanced", 2, 2),
        ("finished", 2, 2),
    ]


def test_update_champions_keeps_processing_after_one_entity_fails(tmp_path, monkeypatch):
    """验证逐实体失败进入 typed result，后续英雄仍会继续处理。"""
    processor = m_champion_processor.ChampionBinProcessor.__new__(m_champion_processor.ChampionBinProcessor)
    processor.ctx = SimpleNamespace(config=SimpleNamespace(dev_mode=False))
    processor.champion_banks_dir = tmp_path / "banks" / "champions"
    processor.champion_events_dir = tmp_path / "events" / "champions"
    processor._progress_callback = None

    def process(_data: dict, champion_id: str) -> UpdateEntityResult:
        if champion_id == "1":
            raise ValueError("missing bank")
        return UpdateEntityResult.success("champion", champion_id)

    monkeypatch.setattr(processor, "_process_champion_skins", process)

    results = processor._update_champions({"champions": {"1": {}, "2": {}}})

    assert [result.status for result in results] == [UpdateStatus.FAILED, UpdateStatus.SUCCESS]
    assert results[0].error_message == "missing bank"
