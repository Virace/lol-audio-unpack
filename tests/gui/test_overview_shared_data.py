"""实体总览共享数据阶段与选择门禁测试。"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QItemSelectionModel

from lol_audio_unpack.app.local_library import LibraryVersion
from lol_audio_unpack.gui.common.gui_config import GuiConfig
from lol_audio_unpack.gui.controllers.library_browser import LibraryBrowser
from lol_audio_unpack.gui.shared_data import SharedDataPhase, SharedDataState, SharedDataSummary
from lol_audio_unpack.gui.view.overview_page import OverviewPage
from lol_audio_unpack.manager.files import write_data
from tests.factories import make_context


def _make_local_catalog(root, version):
    """准备本项目已有输出目录；音频字节只用于路径选择，不进行解码。"""
    write_data(
        {
            "metadata": {"gameVersion": version},
            "champions": {"1": {"id": 1, "names": {"zh_CN": "旧安妮"}}},
            "maps": {},
        },
        root / "manifest" / version / "zh_CN/data",
    )
    audio = root / "audios" / version / "zh_CN/champions/1·旧安妮/1000/VO/101.wem"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"path-contract-only")
    return audio


def test_current_client_without_audio_keeps_task_entry(qtbot, tmp_path) -> None:
    """当前客户端尚未解包也必须保留任务入口，其他版本只能试听。"""
    _make_local_catalog(tmp_path, "16.19")
    source = make_context(tmp_path, game_region="zh_CN")
    metadata = source.game_path / "Game/content-metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"version": "16.21.1"}', encoding="utf-8")
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_app_context(source)
    assert page.library_browser.source == LibraryVersion("16.21", "zh_CN")
    page.set_shared_data_state(SharedDataState(SharedDataPhase.READY, 1))
    current_rows = [
        {"id": "1", "name": "当前安妮", "audio": "未准备", "mapping": "未准备"},
        {"id": "2", "name": "新增英雄", "audio": "未准备", "mapping": "未准备"},
    ]
    page.set_entity_data("champions", current_rows)
    view = page.entityListPanel.entity_lists["champions"]
    view.setCurrentIndex(view.find_index_by_entity_id("1"))
    page.library_browser.configure(tmp_path)
    qtbot.waitUntil(lambda: not page.library_browser._workers)
    assert [page.version_combo.itemData(i) for i in range(page.version_combo.count())] == [
        None,
        LibraryVersion("16.19", "zh_CN"),
    ]
    assert page.version_combo.currentData() is None
    page.library_browser.select(LibraryVersion("16.19", "zh_CN"))
    qtbot.waitUntil(lambda: page.library_browser.loader is not None and not page._preview_workers)

    assert page.entityListPanel.resolve_row_payload(view.currentIndex())["id"] == "1"
    assert [(row["id"], row["name"]) for row in page._entity_data_store.rows_for("champions")] == [("1", "旧安妮")]
    view.selectionModel().select(view.currentIndex(), QItemSelectionModel.SelectionFlag.Select)
    page.set_shared_data_state(SharedDataState(SharedDataPhase.VERIFYING, 2))
    page.clear_data()
    page.set_entity_data("champions", current_rows)
    page.update_entity_rows("champions", [{"id": "2", "name": "新增英雄", "audio": "已存在"}])
    qtbot.waitUntil(lambda: not page._library_refresh_timer.isActive() and not page.library_browser._workers)
    assert [(row["id"], row["name"]) for row in page._entity_data_store.rows_for("champions")] == [("1", "旧安妮")]
    assert not page.sync_selection_btn.isEnabled()
    page.set_shared_data_state(SharedDataState(SharedDataPhase.READY, 2))
    requests = []
    page.selection_sync_requested.connect(requests.append)
    page.sync_selection_btn.click()
    page._sync_selected_entities()

    assert requests == []
    assert not page.clear_selection_btn.isEnabled()
    assert page._app_context is source
    assert page.library_browser.source == LibraryVersion("16.21", "zh_CN")
    page.library_browser.select(None)
    assert {row["id"] for row in page._entity_data_store.rows_for("champions")} == {"1", "2"}
    view.setCurrentIndex(view.find_index_by_entity_id("1"))
    page.sync_selection_btn.click()
    assert len(requests) == 1
    assert requests[0].champion_ids == (1,)
    qtbot.waitUntil(lambda: not page._preview_workers)


def test_local_preview_builds_export_without_config_or_client(qtbot) -> None:
    """没有配置和客户端时，自动发现默认 output，并允许切换版本及导出。"""
    cfg = GuiConfig()
    cfg.load()
    root = cfg.resolve_output_path()
    audio = _make_local_catalog(root, "16.17")
    _make_local_catalog(root, "16.18")
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_gui_config(cfg)
    page.set_app_context(None)
    qtbot.waitUntil(lambda: page.library_browser.loader is not None)
    assert cfg.resolve_game_path() is None
    assert {page.version_combo.itemData(i) for i in range(page.version_combo.count())} == {
        LibraryVersion("16.17", "zh_CN"),
        LibraryVersion("16.18", "zh_CN"),
    }
    page.version_combo.setCurrentIndex(page.version_combo.findData(LibraryVersion("16.17", "zh_CN")))
    qtbot.waitUntil(lambda: page.library_browser.loader is not None)
    view = page.entityListPanel.entity_lists["champions"]
    view.setCurrentIndex(view.find_index_by_entity_id("1"))
    qtbot.waitUntil(lambda: page.export_controller.request is not None and page._audio_refs_loaded)

    request = page.export_controller.request
    assert page._app_context is None
    assert request.version == "16.17"
    assert request.version_root == root / "audios/16.17/zh_CN"
    assert request.targets[0].scope.root == root / "audios/16.17/zh_CN/champions/1·旧安妮"
    assert tuple(ref.path for ref in page._current_audio_refs) == (audio,)
    assert page.audioPreviewPanel.export_bar.mode_button.isEnabled()
    assert not page.sync_selection_btn.isEnabled()


def test_library_switch_ignores_stale_catalog_results(qtbot, tmp_path) -> None:
    """快速切换版本时旧目录与错误不得覆盖最后选择，也不得改变任务源。"""
    source = make_context(tmp_path, runtime_cache={"resolved_runtime_version": "16.19"}, game_region="zh_CN")
    browser = LibraryBrowser()
    scheduled = []
    browser._pool = SimpleNamespace(start=scheduled.append)
    browser.set_source(source)
    browser.configure(tmp_path)
    old = LibraryVersion("16.17", "zh_CN")
    recent = LibraryVersion("16.18", "zh_CN")
    scheduled[0].signals.finished.emit((recent, old))
    received = []
    errors = []
    browser.loaded.connect(received.append)
    browser.failed.connect(errors.append)

    browser.select(old)
    old_request = scheduled[-1]
    browser.select(recent)
    recent_request = scheduled[-1]
    old_request.signals.finished.emit((object(), {"champions": [{"id": "1"}]}))
    old_request.signals.failed.emit("旧目录读取失败")
    assert received == []
    assert errors == []
    assert browser.loading

    rows = {"champions": [{"id": "1"}, {"id": "2"}], "maps": [], "special": []}
    recent_request.signals.finished.emit((object(), rows))
    assert received == [rows]
    assert browser.selection == recent
    assert not browser.loading
    assert browser.source == LibraryVersion("16.19", "zh_CN")
    assert source.runtime_cache["resolved_runtime_version"] == "16.19"


def test_local_selection_reloads_when_library_root_changes(qtbot, tmp_path) -> None:
    """没有客户端时更换同样含所选版本的输出库，必须读取新库媒体。"""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_audio = _make_local_catalog(first, "16.17")
    second_audio = _make_local_catalog(second, "16.17")
    browser = LibraryBrowser()
    browser.configure(first)
    qtbot.waitUntil(lambda: browser.loader is not None)
    assert tuple(ref.path for ref in browser.loader.load_audio_refs("champions", "1")) == (first_audio,)

    browser.configure(second)
    qtbot.waitUntil(lambda: browser.loader is not None)

    assert browser.selection == LibraryVersion("16.17", "zh_CN")
    assert tuple(ref.path for ref in browser.loader.load_audio_refs("champions", "1")) == (second_audio,)


def test_client_switch_keeps_saved_newer_versions(qtbot, tmp_path) -> None:
    """当前客户端置顶且同版不重复，改选较旧客户端仍保留较新音频。"""
    versions = ("16.22", "16.21", "16.19")
    for version in versions:
        _make_local_catalog(tmp_path, version)
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_app_context(make_context(tmp_path, runtime_cache={"resolved_runtime_version": "16.22"}))
    page.library_browser.configure(tmp_path)
    qtbot.waitUntil(lambda: len(page.library_browser.versions) == len(versions))
    assert [page.version_combo.itemData(i) for i in range(page.version_combo.count())] == [
        None,
        LibraryVersion("16.21", "zh_CN"),
        LibraryVersion("16.19", "zh_CN"),
    ]
    assert page.version_combo.currentData() is None
    page.version_combo.setCurrentIndex(page.version_combo.findData(LibraryVersion("16.19", "zh_CN")))
    qtbot.waitUntil(lambda: page.library_browser.loader is not None)

    page.set_app_context(make_context(tmp_path, runtime_cache={"resolved_runtime_version": "16.21"}))
    qtbot.waitUntil(lambda: not page.library_browser._workers and not page._preview_workers)

    assert page.library_browser.source == LibraryVersion("16.21", "zh_CN")
    assert page.library_browser.selection is None
    assert page.version_combo.currentData() is None
    assert [page.version_combo.itemData(i) for i in range(page.version_combo.count())] == [
        None,
        LibraryVersion("16.22", "zh_CN"),
        LibraryVersion("16.19", "zh_CN"),
    ]


def test_refresh_preserves_map_selection_and_sync_payload(qtbot) -> None:
    """任务刷新地图状态后，已选对象与发送到执行中心的目标必须保持一致。"""
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_shared_data_state(SharedDataState(SharedDataPhase.READY, 1))
    page.set_entity_data("maps", [{"id": "0", "name": "常规"}, {"id": "11", "name": "召唤师峡谷"}])
    page.nav_pivot.setCurrentItem("maps")
    view = page.entityListPanel.entity_lists["maps"]
    view.setCurrentIndex(view.find_index_by_entity_id("0"))
    requests = []
    page.selection_sync_requested.connect(requests.append)

    page.update_entity_rows("maps", [{"id": "0", "name": "常规", "audio": "已存在", "mapping": "已存在"}])
    page.sync_selection_btn.click()

    assert view.selected_entity_ids() == {"0"}
    assert page._selected_entity_ids["maps"] == {"0"}
    assert len(requests) == 1
    assert requests[0].map_ids == (0,)
    assert not requests[0].champion_ids


def test_overview_partial_keeps_verified_rows_but_disables_sync(qtbot) -> None:
    """partial 可浏览已验证行，但不能把不完整目录发送到执行中心。"""
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_entity_data("champions", [{"id": "1", "name": "Annie"}])
    page.set_entity_data("maps", [{"id": "0", "name": "Common"}])
    page._selected_entity_ids["champions"] = {"1"}
    page.set_shared_data_state(
        SharedDataState(
            SharedDataPhase.PARTIAL,
            2,
            summary=SharedDataSummary(2, 1, 1, 1, 1, 0, 0, 0, 0),
        )
    )

    assert page._entity_data_store.rows_for("champions") == [{"id": "1", "name": "Annie"}]
    assert page.shared_data_status.isHidden() is False
    assert page.shared_data_status.statusLabel.text() == "部分实体数据未就绪"
    assert page.clear_selection_btn.isEnabled() is True
    assert page.sync_selection_btn.isEnabled() is False
    assert "新任务已暂停" in page.sync_selection_btn.toolTip()

    page.set_shared_data_state(SharedDataState(SharedDataPhase.READY, 2))

    assert page.shared_data_status.isHidden() is True
    assert page.sync_selection_btn.isEnabled() is True


def test_overview_active_state_uses_loading_placeholder_instead_of_empty_catalog(qtbot) -> None:
    """扫描阶段的空 rows 必须呈现当前阶段，而不是“没有实体”。"""
    page = OverviewPage()
    qtbot.addWidget(page)

    page.set_shared_data_state(SharedDataState(SharedDataPhase.VERIFYING, 3))

    assert page.shared_data_status.statusLabel.text() == "正在完整复检英雄与地图目录。"
    assert "验证实体数据" in page.text_preview.toPlainText()
