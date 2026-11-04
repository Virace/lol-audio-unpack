"""客户端物理目录布局与本地 LCU 位置识别。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GAME_DIR = Path("Game")
FINAL_DIR = GAME_DIR / "DATA" / "FINAL"
GAME_DIR_CHAMPIONS = "Champions"
GAME_DIR_MAPS = "Maps"
CHAMPIONS_DIR = FINAL_DIR / GAME_DIR_CHAMPIONS
MAPS_DIR = FINAL_DIR / GAME_DIR_MAPS / "Shipping"
GAME_METADATA = GAME_DIR / "content-metadata.json"
GAME_EXE = GAME_DIR / "League of Legends.exe"
LCU_CLIENT_DIRS = (Path("LeagueClient"), Path())
LCU_PLUGIN_DIR = Path("Plugins") / "rcp-be-lol-game-data"
LCU_DIRS = tuple(client / LCU_PLUGIN_DIR for client in LCU_CLIENT_DIRS)
LCU_EXE = "LeagueClient.exe"
LCU_DESCRIPTION = "description.json"


class GameLayoutError(ValueError):
    """同一游戏根包含无法唯一选择的 LCU 布局。"""


@dataclass(frozen=True)
class GameLayout:
    """一个游戏根对应的 LCU 位置，不保存全局当前客户端。"""

    root: Path
    client_root: Path
    kind: str

    @property
    def lcu_path(self) -> Path:
        """返回客户端 game-data 目录。"""
        return self.client_root / LCU_PLUGIN_DIR

    @property
    def lcu_relative(self) -> Path:
        """返回相对于游戏根的 game-data 身份。"""
        return self.lcu_path.relative_to(self.root)


def resolve_layout(root: Path) -> GameLayout:
    """按声明和实际目录识别 LCU，内容完整性由消费层校验。

    Args:
        root: 已选定的游戏根目录。

    Returns:
        选中的布局；无标志时保留嵌套候选供原预检报告缺失项。

    Raises:
        GameLayoutError: 存在两套独立且同等可用的 LCU 候选。
    """
    clients = tuple(root / relative for relative in LCU_CLIENT_DIRS)
    declared = [client for client in clients if (client / LCU_PLUGIN_DIR / LCU_DESCRIPTION).is_file()]
    candidates = declared or [client for client in clients if (client / LCU_PLUGIN_DIR).is_dir()]
    if not candidates:
        # 版本查询和安装诊断也可在只有 exe/system.yaml 的下载中目录运行。
        candidates = [
            client for client in clients if (client / LCU_EXE).is_file() or (client / "system.yaml").is_file()
        ]
    unique = {(client / LCU_PLUGIN_DIR).resolve(): client for client in reversed(candidates)}
    if len(unique) > 1:
        paths = "、".join(str(client / LCU_PLUGIN_DIR) for client in unique.values())
        raise GameLayoutError(f"本地数据源的 LCU 布局不明确，发现多个独立目录: {paths}")
    client = next(iter(unique.values()), clients[0])
    kind = ("flat" if client == root else "nested") if unique else "unknown"
    return GameLayout(root, client, kind)
