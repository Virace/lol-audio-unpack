"""验证应用上下文输入、配置优先级与输出初始化边界。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import lol_audio_unpack as app_pkg
import lol_audio_unpack.app.context as app_context_module
from lol_audio_unpack import setup_app
from lol_audio_unpack.app import AppContext, AppContextValidationError, create_app_context
from lol_audio_unpack.config import (
    CONFIG_SECTION,
    load_command_config,
    load_settings,
    resolve_default_path,
    write_settings,
)
from lol_audio_unpack.utils.runtime_paths import detect_runtime_paths

pytestmark = pytest.mark.unit


def _prepare_local_source(game_path: Path) -> Path:
    """创建满足公共预检合同的最小本地数据源。"""
    (game_path / "Game" / "DATA" / "FINAL").mkdir(parents=True)
    lcu_root = game_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data"
    lcu_root.mkdir(parents=True)
    (game_path / "Game" / "content-metadata.json").write_text(
        json.dumps({"version": "15.6.1"}),
        encoding="utf-8",
    )
    (lcu_root / "description.json").write_text(
        json.dumps({"riotMeta": {}}),
        encoding="utf-8",
    )
    return game_path


def _build_settings(tmp_path: Path) -> dict[str, object]:
    game_path = _prepare_local_source(tmp_path / "game")
    return {
        "GAME_PATH": str(game_path),
        "OUTPUT_PATH": str(tmp_path / "output"),
        "GAME_REGION": "zh_CN",
        "EXCLUDE_TYPE": "SFX,MUSIC",
    }


def test_explicit_empty_language_requires_discovery_context(tmp_path: Path) -> None:
    """GUI 发现允许留空，公共源处理默认拒绝显式空值且不创建输出。"""
    settings = _build_settings(tmp_path)
    settings["GAME_REGION"] = ""
    with pytest.raises(AppContextValidationError, match="请选择"):
        create_app_context(settings=settings)
    assert not (tmp_path / "output").exists()
    ctx = create_app_context(settings=settings, allow_empty_language=True)
    assert ctx.game_region == ""


def test_source_check_precedes_output_logging(tmp_path: Path, monkeypatch) -> None:
    """初始化的源检查失败时不得触发文件日志初始化。"""
    calls = []
    monkeypatch.setattr(app_pkg, "setup_logging", lambda **_: calls.append("file logging"))

    def reject(_ctx):
        raise ValueError("missing source")

    with pytest.raises(ValueError, match="missing source"):
        setup_app(settings=_build_settings(tmp_path), source_check=reject)
    assert calls == []
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("lobby_audio", [None, False])
def test_create_app_context_builds_typed_context_from_settings(tmp_path: Path, lobby_audio: bool | None) -> None:
    """默认包含大厅音频，并保留显式关闭的配置语义。"""
    settings = _build_settings(tmp_path)
    if lobby_audio is not None:
        settings["LOBBY_AUDIO"] = lobby_audio

    app_context = create_app_context(settings=settings)

    assert isinstance(app_context, AppContext)
    assert app_context.config.game_path == tmp_path / "game"
    assert app_context.config.output_path == tmp_path / "output"
    assert app_context.config.lobby_audio is (lobby_audio is not False)
    assert app_context.paths.audio_path == tmp_path / "output" / "audios"
    assert app_context.paths.manifest_path == tmp_path / "output" / "manifest"
    assert app_context.runtime_cache == {}


def test_create_app_context_applies_explicit_settings(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    settings.update({"EXCLUDE_TYPE": "VO", "GROUP_BY_TYPE": True})

    app_context = create_app_context(settings=settings)

    assert app_context.config.group_by_type is True
    assert app_context.config.exclude_types == ("VO",)
    assert set(app_context.config.include_types) == {"SFX", "MUSIC"}


def test_create_app_context_blank_exclude_type_clears_default(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    settings["EXCLUDE_TYPE"] = ""

    app_context = create_app_context(settings=settings)

    assert app_context.config.exclude_types == ()
    assert set(app_context.config.include_types) == {"VO", "SFX", "MUSIC"}


def test_create_app_context_uses_runtime_default_output_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir(parents=True, exist_ok=True)
    game_path = _prepare_local_source(tmp_path / "game")
    monkeypatch.setattr(
        app_context_module,
        "detect_runtime_paths",
        lambda: detect_runtime_paths(
            is_frozen=False,
            cwd=runtime_root,
            executable=tmp_path / "python" / "python.exe",
        ),
    )

    app_context = create_app_context(
        settings={
            "GAME_PATH": str(game_path),
            "GAME_REGION": "zh_CN",
        }
    )

    assert app_context.config.game_path == game_path
    assert app_context.config.output_path == runtime_root / "output"


def test_create_app_context_ignores_blank_output_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir(parents=True, exist_ok=True)
    game_path = _prepare_local_source(tmp_path / "game")
    monkeypatch.setattr(
        app_context_module,
        "detect_runtime_paths",
        lambda: detect_runtime_paths(
            is_frozen=False,
            cwd=runtime_root,
            executable=tmp_path / "python" / "python.exe",
        ),
    )

    app_context = create_app_context(
        settings={
            "GAME_PATH": str(game_path),
            "GAME_REGION": "zh_CN",
            "OUTPUT_PATH": "   ",
        }
    )

    assert app_context.config.output_path == runtime_root / "output"


def test_create_app_context_resolves_relative_paths_from_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir(parents=True, exist_ok=True)
    _prepare_local_source(runtime_root / "game-client")
    monkeypatch.setattr(
        app_context_module,
        "detect_runtime_paths",
        lambda: detect_runtime_paths(
            is_frozen=False,
            cwd=runtime_root,
            executable=tmp_path / "python" / "python.exe",
        ),
    )

    app_context = create_app_context(
        settings={
            "GAME_PATH": "./game-client",
            "OUTPUT_PATH": "./custom-output",
            "WWISER_PATH": "./tools/wwiser/wwiser.pyz",
            "GAME_REGION": "zh_CN",
        }
    )

    assert app_context.config.game_path == runtime_root / "game-client"
    assert app_context.config.output_path == runtime_root / "custom-output"
    assert app_context.config.wwiser_path == runtime_root / "tools" / "wwiser" / "wwiser.pyz"


def test_create_app_context_ignores_removed_remote_settings(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    settings.update(
        {
            "SOURCE_MODE": "remote_snapshot",
            "REMOTE_VERSION": "15.6",
            "REMOTE_LCU_MANIFEST_URL": "https://example.com/lcu.manifest",
            "REMOTE_GAME_MANIFEST_URL": "https://example.com/game.manifest",
        }
    )

    app_context = create_app_context(settings=settings)

    assert app_context.config.game_path == tmp_path / "game"
    assert not hasattr(app_context.config, "source_mode")
    assert not hasattr(app_context.config, "remote_snapshot")


def test_create_app_context_rejects_incomplete_source_before_creating_outputs(tmp_path: Path) -> None:
    game_path = tmp_path / "incomplete-game"
    game_path.mkdir()
    output_path = tmp_path / "output"

    with pytest.raises(AppContextValidationError, match="GAME FINAL 目录"):
        create_app_context(
            settings={
                "GAME_PATH": str(game_path),
                "OUTPUT_PATH": str(output_path),
            }
        )

    assert not output_path.exists()


def test_create_app_context_rejects_corrupt_metadata_before_creating_outputs(tmp_path: Path) -> None:
    game_path = _prepare_local_source(tmp_path / "game")
    (game_path / "Game" / "content-metadata.json").write_text("not-json", encoding="utf-8")
    output_path = tmp_path / "output"

    with pytest.raises(AppContextValidationError, match="content-metadata.json无法解析"):
        create_app_context(
            settings={
                "GAME_PATH": str(game_path),
                "OUTPUT_PATH": str(output_path),
            }
        )

    assert not output_path.exists()


def test_create_app_context_rejects_invalid_lcu_description(tmp_path: Path) -> None:
    game_path = _prepare_local_source(tmp_path / "game")
    description_path = game_path / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data" / "description.json"
    description_path.write_text(json.dumps({"unexpected": {}}), encoding="utf-8")

    with pytest.raises(AppContextValidationError, match="description.json 缺少有效 riotMeta"):
        create_app_context(
            settings={
                "GAME_PATH": str(game_path),
                "OUTPUT_PATH": str(tmp_path / "output"),
            }
        )


def test_create_app_context_missing_required_setting_raises(tmp_path: Path) -> None:
    with pytest.raises(AppContextValidationError, match="GAME_PATH"):
        create_app_context(settings={"OUTPUT_PATH": str(tmp_path / "output")})


def test_setup_app_returns_context_with_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    fake_context = SimpleNamespace(
        paths=SimpleNamespace(log_path=tmp_path / "output" / "logs"),
        config=SimpleNamespace(
            game_path=tmp_path / "game", game_region="zh_CN", output_path=tmp_path / "output", include_types=("VO",)
        ),
    )

    def fake_create_app_context(*, settings=None, force_reload=False, dev_mode=False, runtime_cache=None):
        captured["settings"] = settings
        captured["force_reload"] = force_reload
        captured["dev_mode"] = dev_mode
        captured["runtime_cache"] = runtime_cache
        return fake_context

    monkeypatch.setattr(app_pkg, "_create_app_context", fake_create_app_context)
    monkeypatch.setattr(app_pkg, "setup_logging", lambda **_kwargs: None)

    result = setup_app(
        dev_mode=True,
        log_level="DEBUG",
        settings={
            "GAME_PATH": str(tmp_path / "game"),
            "OUTPUT_PATH": str(tmp_path / "output"),
        },
    )

    assert result is fake_context
    assert captured["dev_mode"] is True
    assert captured["settings"] == {
        "GAME_PATH": str(tmp_path / "game"),
        "OUTPUT_PATH": str(tmp_path / "output"),
    }


def test_load_settings_reads_lowercase_ini_keys(tmp_path: Path) -> None:
    config_file = tmp_path / "lol-audio-unpack.ini"
    write_settings(
        config_file,
        {
            "GAME_PATH": str(tmp_path / "game"),
            "OUTPUT_PATH": str(tmp_path / "output"),
            "GROUP_BY_TYPE": True,
        },
    )

    settings = load_settings(config_file)

    assert settings == {
        "GAME_PATH": str(tmp_path / "game"),
        "OUTPUT_PATH": str(tmp_path / "output"),
        "GROUP_BY_TYPE": "true",
    }


def test_load_settings_requires_existing_file(tmp_path: Path) -> None:
    config_file = tmp_path / "missing.ini"

    with pytest.raises(FileNotFoundError, match="配置文件不存在"):
        load_settings(config_file)


@pytest.mark.parametrize("value", ["true", "false"])
def test_load_settings_migrates_lobby_key_without_reformatting(tmp_path: Path, value: str) -> None:
    """迁移旧键时保留原值、其他段落、注释及 Windows 换行。"""
    path = tmp_path / "legacy.ini"
    source = (
        f"; 用户备注\r\n[app]\r\nWITH_BP_VO : {value}\r\ngame_path = ./game\r\n"
        "\r\n[custom]\r\nwith_bp_vo = untouched\r\n"
    )
    path.write_bytes(source.encode("utf-8"))

    settings = load_settings(path)

    assert settings == {"LOBBY_AUDIO": value, "GAME_PATH": "./game"}
    expected = source.replace("WITH_BP_VO", "lobby_audio").encode("utf-8")
    assert path.read_bytes() == expected
    assert load_settings(path) == settings
    assert path.read_bytes() == expected


@pytest.mark.parametrize("new_first", [True, False])
def test_load_settings_prefers_new_lobby_key(tmp_path: Path, new_first: bool) -> None:
    """新旧配置冲突时不因行顺序改变新键的关闭选择。"""
    path = tmp_path / "mixed.ini"
    entries = ["with_bp_vo = true\n", "lobby_audio = false\n"]
    if new_first:
        entries.reverse()
    path.write_text("[app]\n" + "".join(entries), encoding="utf-8")

    assert load_settings(path) == {"LOBBY_AUDIO": "false"}
    assert path.read_text(encoding="utf-8") == "[app]\nlobby_audio = false\n"


def test_load_settings_keeps_legacy_value_when_migration_write_fails(tmp_path: Path, monkeypatch) -> None:
    """自动迁移不能写回时继续兼容读取，原文件保持完整并给出提醒。"""
    path = tmp_path / "read_only.ini"
    source = b"[app]\nwith_bp_vo = false\n"
    path.write_bytes(source)
    warnings = []

    def deny_replace(*args, **kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr("lol_audio_unpack.config.ini.replace_file", deny_replace)
    monkeypatch.setattr("lol_audio_unpack.config.ini.logger.warning", lambda message, *args: warnings.append(message))

    assert load_settings(path) == {"LOBBY_AUDIO": "false"}
    assert path.read_bytes() == source
    assert any("请手动改为 lobby_audio" in message for message in warnings)


def test_load_settings_warns_and_ignores_removed_remote_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "legacy.ini"
    config_file.write_text(
        (
            "[app]\n"
            "game_path = ./game\n"
            "source_mode = remote_snapshot\n"
            "remote_live_region = EUW\n"
            "cleanup_remote = false\n"
        ),
        encoding="utf-8",
    )
    warnings: list[str] = []
    monkeypatch.setattr("lol_audio_unpack.config.ini.logger.warning", warnings.append)

    settings = load_settings(config_file)

    assert settings == {"GAME_PATH": "./game"}
    assert warnings == [
        "忽略未知配置项: source_mode",
        "忽略未知配置项: remote_live_region",
        "忽略未知配置项: cleanup_remote",
    ]


def test_load_command_config_reads_targets_and_wav_sections(tmp_path: Path) -> None:
    config_file = tmp_path / "lol-audio-unpack.ini"
    config_file.write_text(
        ("[app]\ngame_path = ./game\n\n[targets]\nchampions = Annie,Ahri\n\n[wav]\nenable = true\n"),
        encoding="utf-8",
    )

    target_config = load_command_config(config_file, command="targets")
    wav_config = load_command_config(config_file, command="wav")

    assert target_config == {
        "champions": "Annie,Ahri",
    }
    assert wav_config == {
        "wav": True,
    }


def test_load_command_config_reads_runtime_and_wav_sections(tmp_path: Path) -> None:
    config_file = tmp_path / "lol-audio-unpack.ini"
    config_file.write_text(
        (
            "[app]\n"
            "game_path = ./game\n"
            "\n"
            "[runtime]\n"
            "max_workers = 8\n"
            "\n"
            "[wav]\n"
            "enable = true\n"
            "wav_workers = 3\n"
            "wav_timeout = 9\n"
            "wav_retries = 4\n"
            "wav_format = float\n"
        ),
        encoding="utf-8",
    )

    runtime_config = load_command_config(config_file, command="runtime")
    wav_config = load_command_config(config_file, command="wav")

    assert runtime_config == {
        "max_workers": 8,
    }
    assert wav_config == {
        "wav": True,
        "wav_workers": 3,
        "wav_timeout": 9,
        "wav_retries": 4,
        "wav_format": "float",
    }


def test_resolve_default_path_uses_runtime_config_root() -> None:
    config_file = resolve_default_path()
    assert config_file.name == "lol-audio-unpack.ini"
    assert config_file.parent.name == "config"
    assert config_file.parent.parent.name == "isolated_env"


def test_write_settings_creates_expected_section(tmp_path: Path) -> None:
    config_file = tmp_path / "lol-audio-unpack.ini"

    write_settings(
        config_file,
        {
            "GAME_PATH": str(tmp_path / "game"),
            "OUTPUT_PATH": str(tmp_path / "output"),
        },
    )

    text = config_file.read_text(encoding="utf-8")
    assert f"[{CONFIG_SECTION}]" in text
    assert "game_path =" in text
    assert "output_path =" in text
