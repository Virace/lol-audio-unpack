"""验证大厅语音归属、共享音效与可见输出。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from lol_audio_unpack.app.types import AppConfig, AppContext, AppPaths
from lol_audio_unpack.manager import lobby
from lol_audio_unpack.manager.files import write_data
from lol_audio_unpack.manager.lobby import find_lobby_source
from lol_audio_unpack.model import AudioBank, AudioEntityData
from lol_audio_unpack.model import binding as resource_binding
from lol_audio_unpack.unpack import entity as unpack_entity
from lol_audio_unpack.unpack import lobby_audio as unpack_lobby_audio
from lol_audio_unpack.utils.common import load_yaml
from lol_audio_unpack.utils.path_constants import format_entity_folder_name
from tests.factories import make_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def prepared_lobby(monkeypatch):
    """链接用例隔离 LCU 补齐边界；真实资源读取另用客户端验证。"""
    monkeypatch.setattr(
        unpack_lobby_audio, "DataUpdater", lambda _ctx: SimpleNamespace(ensure_lobby_audio=lambda _ids: None)
    )


@pytest.mark.parametrize("region", ["en_US", "default"])
def test_english_voice_uses_default_namespace(tmp_path, region):
    """英语语音对应 LCU 的 default 路径，不被当作本地化缺失。"""
    ctx = make_context(tmp_path, game_region=region)
    source = ctx.version_path("manifest", "16.18") / "lobby/default/champion-choose-vo/1.ogg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"english")
    assert (
        unpack_lobby_audio.find_lobby_audio_source(
            SimpleNamespace(version="16.18", data={"champions": {}}), "1", "champion-choose-vo", ctx=ctx
        )
        == source
    )


def test_localized_voice_never_falls_back_to_default(tmp_path):
    """有英语语音也不能冒充缺失的当前语言；SFX 继续读取共享目录。"""
    lobby = tmp_path / "16.18" / "ja_JP" / "lobby"
    for category in ("champion-ban-vo", "champion-sfx-audios"):
        path = lobby / "default" / category / "1.ogg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"default audio")
    ctx = make_context(tmp_path, paths=SimpleNamespace(manifest_path=tmp_path), game_region="ja_JP")
    reader = SimpleNamespace(version="16.18", data={"champions": {}})
    assert unpack_lobby_audio.find_lobby_audio_source(reader, "1", "champion-ban-vo", ctx=ctx) is None
    assert unpack_lobby_audio.find_lobby_audio_source(reader, "1", "champion-sfx-audios", ctx=ctx) == (
        lobby / "default" / "champion-sfx-audios" / "1.ogg"
    )


@pytest.mark.parametrize("own_present", [False, True])
def test_lobby_uses_prime_voice_until_own_resource_exists(tmp_path, own_present):
    """Jade 沿声明复用原英雄语音，独立文件存在时优先使用自身文件。"""
    write_data({"champions": {"60001": {"relatedPrimeItemId": 1}}}, tmp_path / "data")
    source_dir = tmp_path / "lobby/zh_CN/champion-choose-vo"
    source_dir.mkdir(parents=True)
    prime = source_dir / "1.ogg"
    prime.write_bytes(b"prime voice")
    own = source_dir / "60001.ogg"
    if own_present:
        own.write_bytes(b"own voice")

    assert find_lobby_source(tmp_path, "zh_CN", "60001", "champion-choose-vo") == (own if own_present else prime)
    assert not own.exists() or own.read_bytes() == b"own voice"


def test_old_jade_catalog_reuses_prime_within_requested_language(tmp_path):
    """旧目录保留 Jade 身份时仍可复用原英雄；语音保持当前语言归属。"""
    write_data({"champions": {"60001": {"id": 60001, "alias": "Jade_Annie"}}}, tmp_path / "data")
    source = tmp_path / "lobby/default/champion-ban-vo/1.ogg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"english prime voice")

    assert find_lobby_source(tmp_path, "zh_CN", "60001", "champion-ban-vo") is None
    assert find_lobby_source(tmp_path, "en_US", "60001", "champion-ban-vo") == source


def test_lobby_lookup_reuses_reader_champions(tmp_path, monkeypatch):
    """共享来源查找复用已加载的英雄表，批量查找不再读取元数据文件。"""
    ctx = make_context(tmp_path, game_region="en_US")
    reader = SimpleNamespace(version="16.22", data={"champions": {"60001": {"relatedPrimeItemId": 1}}})
    manifest = ctx.version_path("manifest", reader.version)
    write_data(reader.data, manifest / "data")
    source = manifest / "lobby/default/champion-choose-vo/1.ogg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"prime voice")
    reads = []
    original = lobby.read_data

    def count_read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(lobby, "read_data", count_read)
    for _ in range(2):
        assert unpack_lobby_audio.find_lobby_audio_source(reader, "60001", "champion-choose-vo", ctx=ctx) == source
    assert reads == []


@pytest.mark.parametrize("enabled", [True, False])
def test_attach_lobby_audio_links_and_reports_persisted_files(tmp_path, monkeypatch, enabled):
    """默认配置落盘大厅音频，显式关闭时不产生音频或回调。"""
    version = "16.3"
    manifest_root = tmp_path / "manifest"
    audio_root = tmp_path / "audios"

    (manifest_root / version / "zh_CN" / "lobby" / "zh_CN" / "champion-ban-vo").mkdir(parents=True, exist_ok=True)
    (manifest_root / version / "zh_CN" / "lobby" / "zh_CN" / "champion-choose-vo").mkdir(parents=True, exist_ok=True)

    ban_source = manifest_root / version / "zh_CN" / "lobby" / "zh_CN" / "champion-ban-vo" / "1.ogg"
    choose_source = manifest_root / version / "zh_CN" / "lobby" / "zh_CN" / "champion-choose-vo" / "1.ogg"
    ban_source.write_bytes(b"ban")
    choose_source.write_bytes(b"choose")

    game_root = tmp_path / "game"
    output_root = tmp_path
    ctx = AppContext(
        config=AppConfig(
            game_path=game_root,
            output_path=output_root,
            game_region="zh_CN",
            group_by_type=False,
            **({} if enabled else {"lobby_audio": False}),
        ),
        paths=AppPaths(
            audio_path=audio_root,
            wav_path=output_root / "wavs",
            temp_path=output_root / "temps",
            log_path=output_root / "logs",
            cache_path=output_root / "cache",
            hash_path=output_root / "hashes",
            report_path=output_root / "reports",
            manifest_path=manifest_root,
            local_version_file=output_root / "game_version",
            game_champion_path=game_root / "Game" / "DATA" / "FINAL" / "Champions",
            game_maps_path=game_root / "Game" / "DATA" / "FINAL" / "Maps" / "Shipping",
            game_lcu_path=game_root / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data",
        ),
    )

    entity_data = AudioEntityData(
        entity_id="1",
        entity_name="安妮",
        entity_alias="annie",
        entity_title="黑暗之女",
        entity_type="champion",
        sub_entities={},
        wad_root="Game/DATA/FINAL/Champions/Annie.wad.client",
        wad_language=None,
    )
    reader = SimpleNamespace(version=version, data={"champions": {}})

    entity_folder = format_entity_folder_name("1", "annie", "安妮", "黑暗之女")
    target_dir = audio_root / version / "zh_CN" / "champions" / entity_folder / "lobby"
    persisted: list[Path] = []
    result = unpack_lobby_audio.attach_lobby_audio(
        entity_data,
        reader,
        ctx=ctx,
        persisted_artifact_callback=persisted.append,
    )

    if not enabled:
        assert result == ()
        assert persisted == []
        assert not target_dir.exists()
        return

    assert (target_dir / "ban.ogg").read_bytes() == b"ban"
    assert (target_dir / "choose.ogg").read_bytes() == b"choose"
    assert result == (target_dir / "ban.ogg", target_dir / "choose.ogg")
    assert persisted == list(result)
    assert (target_dir / "ban.ogg").samefile(ban_source)


def test_attach_lobby_audio_writes_sfx_audio_from_default_fallback(tmp_path, monkeypatch):
    version = "16.3"
    manifest_root = tmp_path / "manifest"
    audio_root = tmp_path / "audios"

    (manifest_root / version / "zh_CN" / "lobby" / "default" / "champion-sfx-audios").mkdir(parents=True, exist_ok=True)
    sfx_source = manifest_root / version / "zh_CN" / "lobby" / "default" / "champion-sfx-audios" / "1.ogg"
    sfx_source.write_bytes(b"sfx")

    game_root = tmp_path / "game"
    output_root = tmp_path
    ctx = AppContext(
        config=AppConfig(
            game_path=game_root,
            output_path=output_root,
            game_region="zh_CN",
            group_by_type=False,
            lobby_audio=True,
        ),
        paths=AppPaths(
            audio_path=audio_root,
            wav_path=output_root / "wavs",
            temp_path=output_root / "temps",
            log_path=output_root / "logs",
            cache_path=output_root / "cache",
            hash_path=output_root / "hashes",
            report_path=output_root / "reports",
            manifest_path=manifest_root,
            local_version_file=output_root / "game_version",
            game_champion_path=game_root / "Game" / "DATA" / "FINAL" / "Champions",
            game_maps_path=game_root / "Game" / "DATA" / "FINAL" / "Maps" / "Shipping",
            game_lcu_path=game_root / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data",
        ),
    )

    entity_data = AudioEntityData(
        entity_id="1",
        entity_name="安妮",
        entity_alias="annie",
        entity_title="黑暗之女",
        entity_type="champion",
        sub_entities={},
        wad_root="Game/DATA/FINAL/Champions/Annie.wad.client",
        wad_language=None,
    )
    reader = SimpleNamespace(version=version, data={"champions": {}})

    unpack_lobby_audio.attach_lobby_audio(entity_data, reader, ctx=ctx)

    entity_folder = format_entity_folder_name("1", "annie", "安妮", "黑暗之女")
    target_dir = audio_root / version / "zh_CN" / "champions" / entity_folder / "lobby"
    assert (target_dir / "sfx.ogg").read_bytes() == b"sfx"


def test_attach_lobby_audio_writes_all_lobby_audio_into_single_lobby_dir_when_grouped(tmp_path, monkeypatch):
    version = "16.3"
    manifest_root = tmp_path / "manifest"
    audio_root = tmp_path / "audios"

    (manifest_root / version / "zh_CN" / "lobby" / "zh_CN" / "champion-ban-vo").mkdir(parents=True, exist_ok=True)
    (manifest_root / version / "zh_CN" / "lobby" / "default" / "champion-sfx-audios").mkdir(parents=True, exist_ok=True)
    (manifest_root / version / "zh_CN" / "lobby" / "zh_CN" / "champion-ban-vo" / "1.ogg").write_bytes(b"ban")
    (manifest_root / version / "zh_CN" / "lobby" / "default" / "champion-sfx-audios" / "1.ogg").write_bytes(b"sfx")

    game_root = tmp_path / "game"
    output_root = tmp_path
    ctx = AppContext(
        config=AppConfig(
            game_path=game_root,
            output_path=output_root,
            game_region="zh_CN",
            group_by_type=True,
            lobby_audio=True,
        ),
        paths=AppPaths(
            audio_path=audio_root,
            wav_path=output_root / "wavs",
            temp_path=output_root / "temps",
            log_path=output_root / "logs",
            cache_path=output_root / "cache",
            hash_path=output_root / "hashes",
            report_path=output_root / "reports",
            manifest_path=manifest_root,
            local_version_file=output_root / "game_version",
            game_champion_path=game_root / "Game" / "DATA" / "FINAL" / "Champions",
            game_maps_path=game_root / "Game" / "DATA" / "FINAL" / "Maps" / "Shipping",
            game_lcu_path=game_root / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data",
        ),
    )

    entity_data = AudioEntityData(
        entity_id="1",
        entity_name="安妮",
        entity_alias="annie",
        entity_title="黑暗之女",
        entity_type="champion",
        sub_entities={},
        wad_root="Game/DATA/FINAL/Champions/Annie.wad.client",
        wad_language=None,
    )
    reader = SimpleNamespace(version=version, data={"champions": {}})

    unpack_lobby_audio.attach_lobby_audio(entity_data, reader, ctx=ctx)

    entity_folder = format_entity_folder_name("1", "annie", "安妮", "黑暗之女")
    lobby_dir = audio_root / version / "zh_CN" / "champions" / entity_folder / "lobby"
    assert (lobby_dir / "ban.ogg").read_bytes() == b"ban"
    assert (lobby_dir / "sfx.ogg").read_bytes() == b"sfx"


def test_unpack_entity_uses_warning_summary_for_partial_parse_failures(tmp_path, monkeypatch):
    version = "16.3"
    manifest_root = tmp_path / "manifest"
    audio_root = tmp_path / "audios"
    game_root = tmp_path / "game"
    output_root = tmp_path

    wad_dir = game_root / "Game"
    wad_dir.mkdir(parents=True, exist_ok=True)
    (wad_dir / "zh_audio.wad.client").write_bytes(b"wad")

    ctx = AppContext(
        config=AppConfig(
            game_path=game_root,
            output_path=output_root,
            game_region="zh_CN",
            group_by_type=False,
            lobby_audio=False,
        ),
        paths=AppPaths(
            audio_path=audio_root,
            wav_path=output_root / "wavs",
            temp_path=output_root / "temps",
            log_path=output_root / "logs",
            cache_path=output_root / "cache",
            hash_path=output_root / "hashes",
            report_path=output_root / "reports",
            manifest_path=manifest_root,
            local_version_file=output_root / "game_version",
            game_champion_path=game_root / "Game" / "DATA" / "FINAL" / "Champions",
            game_maps_path=game_root / "Game" / "DATA" / "FINAL" / "Maps" / "Shipping",
            game_lcu_path=game_root / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data",
        ),
    )

    entity_data = AudioEntityData(
        entity_id="1",
        entity_name="安妮",
        entity_alias="annie",
        entity_title="黑暗之女",
        entity_type="champion",
        sub_entities={
            "1000": {
                "name": "经典",
                "categories": {
                    "CHARACTER_VO": [["assets/test_audio.bnk", "assets/test_audio.wpk"]],
                },
            }
        },
        wad_root="Game/root.wad.client",
        wad_language="Game/zh_audio.wad.client",
    )

    reader = SimpleNamespace(version=version, get_audio_type=lambda _category: "VO")

    class _FakeWad:
        @staticmethod
        def extract(paths, raw=True):
            assert raw is True
            return [b"fake-bnk", b"fake-wpk"]

    class _FakeWpkFile:
        filename = "ok.wem"

        @staticmethod
        def save_file(target: Path) -> None:
            target.write_bytes(b"ok")

    class _FakeWPK:
        def __init__(self, _raw: bytes) -> None:
            pass

        @staticmethod
        def extract_files():
            return [_FakeWpkFile()]

    def _fail_bnk(_raw: bytes):
        raise RuntimeError("bnk boom")

    monkeypatch.setattr(unpack_entity, "_get_wad_instance", lambda *_args, **_kwargs: _FakeWad())
    monkeypatch.setattr(unpack_entity, "BNK", _fail_bnk)
    monkeypatch.setattr(unpack_entity, "WPK", _FakeWPK)

    log_lines: list[str] = []
    logger.enable("lol_audio_unpack")
    sink_id = logger.add(
        lambda message: log_lines.append(str(message).rstrip()),
        format="{level}|{message}",
    )

    try:
        unpack_entity.unpack_entity(entity_data, reader, ctx=ctx)
    finally:
        logger.remove(sink_id)

    assert any("WARNING|处理BNK文件失败: bnk boom | 文件路径: assets/test_audio.bnk" in line for line in log_lines)
    assert any("WARNING|⚠️ 安妮 解包完成 - 成功 1 个文件" in line and "失败 1" in line for line in log_lines)
    assert not any("ERROR|❌ 安妮 解包失败" in line for line in log_lines)


def test_unpack_entity_uses_each_local_binding_wad_and_reports_partial_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相同逻辑路径位于不同 WAD 时应分别提取并保留逻辑输出。"""
    version = "16.16"
    game_root = tmp_path / "game"
    output_root = tmp_path
    for name in ("alpha.wad.client", "beta.wad.client"):
        path = game_root / "Game" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())

    ctx = AppContext(
        config=AppConfig(game_path=game_root, output_path=output_root, game_region="zh_CN"),
        paths=AppPaths(
            audio_path=output_root / "audios",
            wav_path=output_root / "wavs",
            temp_path=output_root / "temps",
            log_path=output_root / "logs",
            cache_path=output_root / "cache",
            hash_path=output_root / "hashes",
            report_path=output_root / "reports",
            manifest_path=output_root / "manifest",
            local_version_file=output_root / "game_version",
            game_champion_path=game_root / "Game" / "DATA" / "FINAL" / "Champions",
            game_maps_path=game_root / "Game" / "DATA" / "FINAL" / "Maps" / "Shipping",
            game_lcu_path=game_root / "LeagueClient" / "Plugins" / "rcp-be-lol-game-data",
        ),
    )
    bindings = (
        resource_binding.BankBinding(
            category="CHARACTER_VO",
            path="assets/shared_audio.bnk",
            normalized_path="",
            kind="BNK",
            wad="Game/alpha.wad.client",
            entry_hash="0000000000000001",
            source_bin="data/alpha.bin",
            role=resource_binding.BindingRole.LOCALIZED,
            status=resource_binding.BindingStatus.RESOLVED,
            sub_entity="1000",
        ),
        resource_binding.BankBinding(
            category="CHARACTER_VO",
            path="assets/shared_audio.bnk",
            normalized_path="",
            kind="BNK",
            wad="Game/beta.wad.client",
            entry_hash="0000000000000002",
            source_bin="data/beta.bin",
            role=resource_binding.BindingRole.LOCALIZED,
            status=resource_binding.BindingStatus.RESOLVED,
            sub_entity="1001",
        ),
        resource_binding.BankBinding(
            category="CHARACTER_VO",
            path="assets/missing_audio.bnk",
            normalized_path="",
            kind="BNK",
            wad=None,
            entry_hash="0000000000000003",
            source_bin="data/missing.bin",
            role=None,
            status=resource_binding.BindingStatus.MISSING,
            sub_entity="1001",
        ),
    )
    entity_data = AudioEntityData(
        entity_id="1",
        entity_name="安妮",
        entity_alias="annie",
        entity_title="黑暗之女",
        entity_type="champion",
        sub_entities={
            "1000": {"name": "基础皮肤", "categories": {}},
            "1001": {"name": "哥特萝莉", "categories": {}},
        },
        wad_root="Game/root.wad.client",
        resource_banks=tuple(
            AudioBank(sub_id=binding.sub_entity or "", audio_type="VO", binding=binding) for binding in bindings
        ),
        binding_diagnostics=resource_binding.BindingDiagnostics(completeness=resource_binding.Completeness.PARTIAL),
    )
    extracted: list[tuple[str, tuple[str, ...]]] = []

    class _FakeWad:
        def __init__(self, path: Path) -> None:
            self.path = path

        def extract(self, paths: list[str], raw: bool = True) -> list[bytes]:
            assert raw is True
            extracted.append((self.path.name, tuple(paths)))
            return [self.path.stem.encode() for _path in paths]

    class _FakeFile:
        data = b"wem"

        def __init__(self, wem_id: int) -> None:
            self.id = wem_id

        def save_file(self, path: Path) -> None:
            path.write_bytes(str(self.id).encode())

    class _FakeBNK:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def extract_files(self) -> list[_FakeFile]:
            return [_FakeFile(101 if self.raw.startswith(b"alpha") else 102)]

    callbacks: list[Path] = []
    monkeypatch.setattr(unpack_entity, "_get_wad_instance", lambda path, **_kwargs: _FakeWad(path))
    monkeypatch.setattr(unpack_entity, "BNK", _FakeBNK)

    unpack_entity.unpack_entity(
        entity_data,
        SimpleNamespace(version=version),
        ctx=ctx,
        persisted_wem_callback=callbacks.append,
    )

    assert extracted == [
        ("alpha.wad.client", ("assets/shared_audio.bnk",)),
        ("beta.wad.client", ("assets/shared_audio.bnk",)),
    ]
    assert {path.name for path in callbacks} == {"101.wem", "102.wem"}
    report = load_yaml(ctx.version_path("report", version) / "champions" / "_1_metadata.yaml")
    diagnostics = report["report"]["bindingDiagnostics"]
    assert diagnostics["completeness"] == "partial"
    assert all(not Path(item["wad"]).is_absolute() for item in diagnostics["wads"])
