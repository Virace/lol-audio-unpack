"""共享实体目录 typed scan 与 worker 边界测试。"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from loguru import logger

import lol_audio_unpack.gui.service.data_loader as data_loader_module
import lol_audio_unpack.gui.service.worker as worker_module
from lol_audio_unpack.app.path_layout import format_entity_folder_name, format_sub_entity_folder_name
from lol_audio_unpack.gui.service.data_loader import EntityDataLoader, build_scan_failure_result
from lol_audio_unpack.gui.service.worker import SharedDataScanWorker
from lol_audio_unpack.gui.shared_data import (
    SharedDataProblemCode,
    SharedDataReadiness,
    SharedDataScanResult,
)
from lol_audio_unpack.manager.data_reader import DataReader
from lol_audio_unpack.manager.errors import (
    ResourceSchemaMismatchError,
    SharedDataCorruptError,
    SharedDataMissingError,
)
from lol_audio_unpack.manager.files import write_data
from lol_audio_unpack.manager.source_inventory import SourceEntity
from lol_audio_unpack.model import AudioEntityData
from lol_audio_unpack.model.binding import RESOURCE_SCHEMA_VERSION
from tests.factories import make_context, make_library_view

pytestmark = pytest.mark.unit

EXPECTED_REQUIRED_COUNT = 2
FAILING_CHAMPION_ID = 2
WORKER_GENERATION = 10


@pytest.mark.parametrize("require_resources", [False, True])
def test_catalog_without_bin_cache_is_browsable_by_default(tmp_path: Path, require_resources: bool) -> None:
    """真实基础清单足以浏览；只有提前准备模式才要求全部资源缓存。"""
    ctx = make_context(
        tmp_path,
        config=SimpleNamespace(
            dev_mode=True, game_path=tmp_path / "game", group_by_type=False, include_types=("VO", "SFX")
        ),
        paths=SimpleNamespace(
            manifest_path=tmp_path / "manifest", hash_path=tmp_path / "hashes", audio_path=tmp_path / "audios"
        ),
        runtime_cache={"resolved_runtime_version": "16.17"},
        game_region="zh_CN",
    )
    manifest = ctx.version_path("manifest", "16.17")
    write_data(
        {
            "metadata": {"gameVersion": "16.17"},
            "champions": {"1": _champion(1, "Annie"), "60001": _champion(60001, "Jade_Annie")},
            "maps": {"0": _map(0), "11": _map(11)},
        },
        manifest / "data",
        dev_mode=True,
    )
    loader = EntityDataLoader(ctx)

    result = loader.scan_catalog(1, require_resources=require_resources)

    if require_resources:
        assert result.readiness is SharedDataReadiness.FAILED
        assert {problem.code for problem in result.problems} == {SharedDataProblemCode.BANK_ARTIFACT_MISSING}
    else:
        assert result.readiness is SharedDataReadiness.COMPLETE
        assert result.champions.rows[0]["name"] == "Annie"
        assert result.champions.unprepared_ids == ("1",)
        assert result.maps.unprepared_ids == ("0", "11")
        assert result.special.unprepared_ids == ("champion:60001",)
        assert loader.load_audio_refs("champions", "1") == ()
        assert loader.load_audio_roots("maps", "11") == ()
        assert loader.load_mapping_preview("champions", "1") == (None, None, "")
    assert not (manifest / "banks").exists()
    assert not (manifest / "events").exists()

    if not require_resources:
        # BIN 缓存不是浏览新库的前提；媒体身份和路径由内容索引提供。
        write_data({"metadata": {"gameVersion": "16.17"}, "skins": {}}, manifest / "banks/champions/1", dev_mode=True)
        entity = AudioEntityData(
            "1",
            "Annie",
            "annie",
            None,
            "champion",
            {"1000": {"name": "基础皮肤", "categories": {}}},
            "Game/a.wad.client",
        )
        _, source = make_library_view(ctx, entity, "16.17", media_id=10)
        audio_dir = source.parents[2]
        refs = loader.load_audio_refs("champions", "1")
        assert len(refs) == 1 and refs[0].path == source.resolve()
        assert loader.load_audio_roots("champions", "1") == (audio_dir,)
        mapping = {
            "skins": {
                "1000": {
                    "events": {"Base_VO": {"play": [10]}},
                    "audioPaths": {"Base_VO": {"play": [source.relative_to(audio_dir).as_posix()]}},
                }
            }
        }
        assert loader.load_event_audio_refs("champions", "1", mapping) == refs


def test_preparation_checks_events_after_extraction_only(monkeypatch, tmp_path: Path) -> None:
    """已有绑定但缺英雄事件时，开启提前准备必须发现并补齐事件。"""
    loader = _build_loader(monkeypatch, champions=[_champion(1, "Annie")], maps=[_map(0), _map(11)])
    loader.data_reader.champion_events_dir = tmp_path / "champion-events"
    loader.data_reader.map_events_dir = tmp_path / "map-events"
    for entity_id in (0, 11):
        write_data(
            {"metadata": {"gameVersion": "16.16"}},
            loader.data_reader.map_events_dir / str(entity_id),
            dev_mode=False,
        )

    assert loader.scan_catalog(1).readiness is SharedDataReadiness.COMPLETE
    pending = loader.scan_catalog(2, require_resources=True)
    assert pending.champions.failures[0].code is SharedDataProblemCode.EVENT_ARTIFACT_MISSING
    assert pending.all_blocking_problems_repairable is True

    write_data(
        {"metadata": {"gameVersion": "16.16"}, "skins": {}},
        loader.data_reader.champion_events_dir / "1",
        dev_mode=False,
    )
    assert loader.scan_catalog(3, require_resources=True).readiness is SharedDataReadiness.COMPLETE


def test_optional_special_without_artifact_stays_unprepared_without_warning(tmp_path: Path) -> None:
    """真实读取器路径未准备时，扫描保留特殊行且不触发缺文件警告。"""
    loader = EntityDataLoader.__new__(EntityDataLoader)
    loader.ctx = SimpleNamespace(config=SimpleNamespace(dev_mode=False), game_region="zh_CN")
    loader.data_reader = DataReader.__new__(DataReader)
    loader.data_reader.legacy = False
    loader.data_reader.champion_banks_dir = tmp_path / "banks"
    loader.data_reader._champion_banks_cache = {}
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        row = loader._build_special_row(_champion(60001, "Jade_Annie"), "16.17", display_name="安妮")
    finally:
        logger.remove(sink)
    assert row is not None
    assert row["audio"] == "未准备"
    assert messages == []


def _champion(entity_id: int, alias: str) -> dict:
    """构造最小普通英雄 metadata。"""
    return {
        "id": entity_id,
        "alias": alias,
        "names": {"zh_CN": alias},
        "wad": {"root": f"Champions/{alias}.wad.client"},
    }


def _map(entity_id: int) -> dict:
    """构造最小地图 metadata。"""
    return {
        "id": entity_id,
        "names": {"zh_CN": f"Map {entity_id}"},
        "wad": {"root": f"Maps/{entity_id}.wad.client"},
    }


def _build_loader(monkeypatch, *, champions: list[dict], maps: list[dict]) -> EntityDataLoader:
    """构造只保留目录扫描边界的轻量 loader。"""
    loader = EntityDataLoader.__new__(EntityDataLoader)
    loader._source_entities = {}
    loader.ctx = SimpleNamespace(
        config=SimpleNamespace(dev_mode=False, game_path=Path("C:/Game")),
        game_region="zh_CN",
    )
    loader.data_reader = SimpleNamespace(
        legacy=False,
        version="16.16",
        get_champions=lambda: champions,
        get_maps=lambda: maps,
    )
    monkeypatch.setattr(loader, "_ensure_bank_dataset_ready", lambda _entity_type: None)
    monkeypatch.setattr(loader, "_preload_bank_artifact", lambda _entity_type, _entity_id: None)
    monkeypatch.setattr(
        loader,
        "_build_entity_row",
        lambda entity_type, entity, _version: {
            "id": str(entity["id"]),
            "name": str(entity.get("alias") or entity["id"]),
            "entity_type": entity_type,
        },
    )
    monkeypatch.setattr(loader, "load_resource_pack_rows", lambda **_kwargs: [])
    monkeypatch.setattr(
        loader,
        "_build_special_row",
        lambda _champion, _version, *, display_name: {
            "id": "66600",
            "key": "champion:66600",
            "name": display_name or "Urgot",
            "audio": "未准备",
            "mapping": "未准备",
        },
    )
    return loader


def test_missing_source_keeps_row_and_skips_resource_repair(monkeypatch) -> None:
    """缺源实体保留已有产物状态，禁选且不因缺 banks 触发自动重建循环。"""
    loader = _build_loader(monkeypatch, champions=[_champion(1, "Annie")], maps=[_map(0)])
    source = SourceEntity("champion", "1", "Annie", ("Annie.ja_JP.wad.client",), ("Annie.ja_JP.wad.client",))
    loader._source_entities = {("champion", "1"): source}
    preload = []
    monkeypatch.setattr(loader, "_preload_bank_artifact", lambda kind, key: preload.append((kind, key)))
    monkeypatch.setattr(loader, "_check_event_artifact", lambda *_: None)
    monkeypatch.setattr(
        loader,
        "_build_entity_row",
        lambda kind, entity, _: {
            "id": str(entity["id"]),
            "name": "Annie",
            "entity_type": kind,
            "audio": "已存在",
            "mapping": "已存在",
        },
    )
    scan = loader.scan_catalog(1, require_resources=True)
    row = scan.champions.rows[0]
    assert scan.readiness is SharedDataReadiness.COMPLETE
    assert not row["selectable"]
    assert row["audio"] == row["mapping"] == "已存在"
    assert source.missing[0] in row["tooltip"]
    assert ("champions", "1") not in preload
    assert ("maps", "0") in preload


def test_scan_catalog_complete_ignores_unprepared_optional_special(monkeypatch) -> None:
    """必需目录完整时，可选特殊内容未准备不能降级 ready。"""
    champions = [
        _champion(1, "Annie"),
        _champion(2, "Olaf"),
        {
            "id": 66600,
            "alias": "Ruby_Urgot",
            "wad": {"root": "Champions/Ruby_Urgot.wad.client"},
        },
    ]
    loader = _build_loader(monkeypatch, champions=champions, maps=[_map(0), _map(11)])
    progress_events = []

    result = loader.scan_catalog(4, progress=progress_events.append)

    assert result.readiness is SharedDataReadiness.COMPLETE
    assert result.summary.champion_expected == EXPECTED_REQUIRED_COUNT
    assert result.summary.champion_loaded == EXPECTED_REQUIRED_COUNT
    assert result.summary.map_loaded == EXPECTED_REQUIRED_COUNT
    assert result.summary.special_discovered == 1
    assert result.summary.special_unprepared == 1
    assert result.special.unprepared_ids == ("champion:66600",)
    assert [(event.stage_key, event.event, event.current, event.total) for event in progress_events] == [
        ("champions", "started", 0, 2),
        ("champions", "advanced", 1, 2),
        ("champions", "advanced", 2, 2),
        ("champions", "finished", 2, 2),
        ("special", "started", 0, None),
        ("special", "advanced", 1, None),
        ("special", "finished", 1, 1),
        ("maps", "started", 0, 2),
        ("maps", "advanced", 1, 2),
        ("maps", "advanced", 2, 2),
        ("maps", "finished", 2, 2),
    ]


def test_scan_catalog_single_schema_failure_is_partial_and_aggregated(monkeypatch) -> None:
    """单个普通实体旧 schema 必须形成 partial 与聚合问题。"""
    loader = _build_loader(
        monkeypatch,
        champions=[_champion(1, "Annie"), _champion(2, "Olaf")],
        maps=[_map(0), _map(11)],
    )
    original_builder = loader._build_entity_row

    def build_row(entity_type: str, entity: dict, version: str) -> dict:
        if entity_type == "champions" and entity["id"] == FAILING_CHAMPION_ID:
            raise ResourceSchemaMismatchError("old schema")
        return original_builder(entity_type, entity, version)

    monkeypatch.setattr(loader, "_build_entity_row", build_row)

    result = loader.scan_catalog(5)

    assert result.readiness is SharedDataReadiness.PARTIAL
    assert result.summary.champion_loaded == 1
    assert result.summary.champion_failed == 1
    problem = next(problem for problem in result.problems if problem.scope == "champions")
    assert problem.code is SharedDataProblemCode.RESOURCE_SCHEMA_MISMATCH
    assert problem.entity_ids == ("2",)
    assert result.all_blocking_problems_repairable is True


def test_scan_catalog_logs_schema_mismatches_once_without_tracebacks(monkeypatch) -> None:
    """同根因旧 schema 只形成一条扫描摘要，不逐实体记录 traceback。"""
    loader = _build_loader(
        monkeypatch,
        champions=[_champion(1, "Annie"), _champion(2, "Olaf")],
        maps=[_map(0), _map(11)],
    )
    original_builder = loader._build_entity_row

    def build_row(entity_type: str, entity: dict, version: str) -> dict:
        if entity_type == "champions":
            raise ResourceSchemaMismatchError("old schema")
        return original_builder(entity_type, entity, version)

    monkeypatch.setattr(loader, "_build_entity_row", build_row)
    debug_messages: list[str] = []
    opt_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        data_loader_module,
        "logger",
        SimpleNamespace(
            debug=debug_messages.append,
            warning=lambda _message: None,
            opt=lambda **kwargs: opt_calls.append(kwargs) or SimpleNamespace(error=lambda _message: None),
        ),
    )

    result = loader.scan_catalog(5)

    assert result.readiness is SharedDataReadiness.FAILED
    assert opt_calls == []
    scan_messages = [message for message in debug_messages if "共享实体目录扫描完成" in message]
    assert len(scan_messages) == 1
    assert "champions expected=2 loaded=0 failed=2" in scan_messages[0]


def test_scan_catalog_all_required_section_failures_are_failed(monkeypatch) -> None:
    """任一必需目录完全不可读时不能降级为 partial。"""
    loader = _build_loader(
        monkeypatch,
        champions=[_champion(1, "Annie")],
        maps=[_map(0), _map(11)],
    )
    monkeypatch.setattr(
        loader,
        "_build_entity_row",
        lambda entity_type, entity, _version: (
            (_ for _ in ()).throw(SharedDataMissingError("missing"))
            if entity_type == "champions"
            else {"id": str(entity["id"])}
        ),
    )

    result = loader.scan_catalog(6)

    assert result.readiness is SharedDataReadiness.FAILED
    assert result.summary.champion_loaded == 0
    assert result.summary.map_loaded == EXPECTED_REQUIRED_COUNT


def test_scan_catalog_requires_map_zero(monkeypatch) -> None:
    """metadata 未声明 Map 0 时，即使其余地图都可读也必须 failed。"""
    loader = _build_loader(
        monkeypatch,
        champions=[_champion(1, "Annie")],
        maps=[_map(11)],
    )

    result = loader.scan_catalog(7)

    assert result.readiness is SharedDataReadiness.FAILED
    assert SharedDataProblemCode.MAP_COMMON_MISSING in {problem.code for problem in result.problems}


def test_scan_catalog_empty_required_metadata_is_failed(monkeypatch) -> None:
    """可读但为空的必需 metadata 不能成为成功 no-op。"""
    loader = _build_loader(monkeypatch, champions=[], maps=[])

    result = loader.scan_catalog(8)

    assert result.readiness is SharedDataReadiness.FAILED
    assert SharedDataProblemCode.DATASET_EMPTY in {problem.code for problem in result.problems}


def test_scan_failure_result_classifies_corrupt_dataset_without_text_matching() -> None:
    """reader 初始化损坏应保留 artifact_corrupt typed code。"""
    result = build_scan_failure_result(9, SharedDataCorruptError("broken"))

    assert result.readiness is SharedDataReadiness.FAILED
    assert result.problems[0].code is SharedDataProblemCode.ARTIFACT_CORRUPT


def test_preload_bank_artifact_suppresses_per_item_read_traceback(monkeypatch, tmp_path: Path) -> None:
    """逐实体预检由扫描层聚合，read_data 必须关闭当前边界错误日志。"""
    loader = EntityDataLoader.__new__(EntityDataLoader)
    loader.ctx = SimpleNamespace(config=SimpleNamespace(dev_mode=False))
    loader.data_reader = SimpleNamespace(
        champion_banks_dir=tmp_path,
        map_banks_dir=tmp_path,
        _champion_banks_cache={},
        _map_banks_cache={},
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(data_loader_module, "find_data_file", lambda *_args, **_kwargs: tmp_path / "1.msgpack")

    def read_artifact(*_args, **kwargs) -> dict:
        captured.update(kwargs)
        return {"resourceSchemaVersion": RESOURCE_SCHEMA_VERSION - 1}

    monkeypatch.setattr(data_loader_module, "read_data", read_artifact)

    with pytest.raises(ResourceSchemaMismatchError):
        loader._preload_bank_artifact("champions", "1")

    assert captured["log_errors"] is False


def test_scan_worker_emits_one_typed_result_and_progress(monkeypatch, qtbot) -> None:
    """扫描期间转发进度，但必须等线程退出后才交付同一 generation 的结果。"""
    ctx = SimpleNamespace()
    expected = build_scan_failure_result(WORKER_GENERATION, SharedDataMissingError("missing"))

    class FakeLoader:
        def __init__(self, _ctx) -> None:
            pass

        def scan_catalog(self, generation: int, *, progress, require_resources: bool) -> SharedDataScanResult:
            assert require_resources is False
            progress(SimpleNamespace(generation=generation, stage_key="champions"))
            return expected

    monkeypatch.setattr(worker_module, "EntityDataLoader", FakeLoader)
    finished = []
    progress_events = []
    errors = []
    scanned = Event()
    release = Event()

    class DelayedWorker(SharedDataScanWorker):
        """暂停在扫描结束与线程退出之间，稳定复现结果交接竞态。"""

        def run(self) -> None:
            """完成真实扫描入口后等待测试允许线程退出。"""
            super().run()
            scanned.set()
            release.wait(5)

    worker = DelayedWorker(ctx, WORKER_GENERATION)
    running_at_result = []
    worker.result_ready.connect(finished.append)
    worker.result_ready.connect(lambda _result: running_at_result.append(worker.isRunning()))
    worker.progress.connect(progress_events.append)
    worker.error.connect(errors.append)

    try:
        worker.start()
        qtbot.waitUntil(lambda: scanned.is_set() and bool(progress_events))
        assert worker.isRunning()
        assert finished == []
        release.set()
        qtbot.waitUntil(lambda: bool(finished))
    finally:
        release.set()
        worker.wait()

    assert finished == [expected]
    assert progress_events[0].generation == WORKER_GENERATION
    assert errors == []
    assert running_at_result == [False]


def test_scan_worker_returns_typed_result_for_expected_init_failure(monkeypatch, qtbot) -> None:
    """缺失 dataset 在线程退出后交付 typed result，不退化为字符串 error。"""
    ctx = SimpleNamespace()

    class FakeLoader:
        def __init__(self, _ctx) -> None:
            raise SharedDataMissingError("missing")

    monkeypatch.setattr(worker_module, "EntityDataLoader", FakeLoader)
    finished = []
    errors = []
    worker = SharedDataScanWorker(ctx, 11)
    running_at_result = []
    worker.result_ready.connect(finished.append)
    worker.result_ready.connect(lambda _result: running_at_result.append(worker.isRunning()))
    worker.error.connect(errors.append)

    try:
        worker.start()
        qtbot.waitUntil(lambda: bool(finished))
    finally:
        worker.wait()

    assert finished[0].problems[0].code is SharedDataProblemCode.DATASET_MISSING
    assert errors == []
    assert running_at_result == [False]


def test_scan_worker_emits_typed_problem_for_unexpected_failure(monkeypatch, qtbot) -> None:
    """无法形成扫描结果的程序错误才使用 worker-level typed error。"""
    ctx = SimpleNamespace()

    class FakeLoader:
        def __init__(self, _ctx) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "EntityDataLoader", FakeLoader)
    monkeypatch.setattr(
        worker_module,
        "logger",
        SimpleNamespace(
            debug=lambda _message: None,
            info=lambda *_args: None,
            opt=lambda **_kwargs: SimpleNamespace(error=lambda _message: None),
        ),
    )
    finished = []
    errors = []
    worker = SharedDataScanWorker(ctx, 12)
    running_at_error = []
    worker.result_ready.connect(finished.append)
    worker.error.connect(errors.append)
    worker.error.connect(lambda _problem: running_at_error.append(worker.isRunning()))

    try:
        worker.start()
        qtbot.waitUntil(lambda: bool(errors))
    finally:
        worker.wait()

    assert finished == []
    assert errors[0].code is SharedDataProblemCode.UNEXPECTED
    assert running_at_error == [False]
