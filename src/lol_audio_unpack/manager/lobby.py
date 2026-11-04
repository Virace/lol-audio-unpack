"""大厅音频的文件名、语言归属与原英雄共享规则。"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lol_audio_unpack.app.special_content import get_prime_champion_id
from lol_audio_unpack.manager.files import read_data

LOBBY_FILES = {
    "champion-ban-vo": "ban.ogg",
    "champion-choose-vo": "choose.ogg",
    "champion-sfx-audios": "sfx.ogg",
}


def find_lobby_file(manifest: Path, region: str, champion_id: str, category: str) -> Path | None:
    """按分类的语言归属定位指定英雄自身的大厅文件。"""
    if category == "champion-sfx-audios" or region.lower() in {"default", "en_us"}:
        regions = ("default",)
    else:
        regions = (region, region.lower())
    return next(
        (
            path
            for language in regions
            if (path := manifest / "lobby" / language / category / f"{champion_id}.ogg").is_file()
        ),
        None,
    )


def find_lobby_source(
    manifest: Path,
    region: str,
    champion_id: str,
    category: str,
    *,
    champions: Mapping[str, Any] | None = None,
) -> Path | None:
    """在同版本、同语言内先查自身大厅文件，再查关联原英雄。"""
    own = find_lobby_file(manifest, region, champion_id, category)
    if own is not None:
        return own

    if champions is None:
        data_file = manifest / "data.msgpack"
        champions = read_data(data_file).get("champions", {}) if data_file.is_file() else {}
    prime_id = get_prime_champion_id(champions.get(champion_id, {}))
    if prime_id is None:
        return None
    # 当前 Jade 禁用、选用语音共享原英雄资源；独立文件始终优先。
    return find_lobby_file(manifest, region, str(prime_id), category)
