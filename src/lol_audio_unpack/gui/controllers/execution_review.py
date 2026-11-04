"""从已有目录生成确认与执行共用的目标快照，不访问资源文件。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from lol_audio_unpack.app.targets import check_ids, filter_default_visible_champions
from lol_audio_unpack.gui.task_models import ExecutionTaskDraft


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """确认清单中的一行展示数据。"""

    group: str
    name: str
    identifier: str


@dataclass(frozen=True, slots=True)
class ExecutionReview:
    """同一次确认的任务、名单与诊断。"""

    draft: ExecutionTaskDraft
    items: tuple[ReviewItem, ...]
    warnings: tuple[str, ...]

    @property
    def can_submit(self) -> bool:
        """只允许至少一个有效目标进入实际任务。"""
        params = self.draft.task_params
        return bool(params.champion_ids or params.map_ids or params.special_targets)


def build_review(draft: ExecutionTaskDraft, catalog: dict[str, list[dict]]) -> ExecutionReview:
    """使用共享目录收敛目标，冻结全部范围，并保留未知 ID。

    Args:
        draft: 完成模式与文本校验的草稿。
        catalog: 已准备的英雄、地图和特殊内容目录。
    """
    params = draft.task_params
    items: list[ReviewItem] = []
    warnings: list[str] = []
    excluded: list[str] = []
    requested: list[str] = []
    resolved: list[tuple[int, ...]] = []
    counts = []
    for key, label, ids in (("champions", "英雄", params.champion_ids), ("maps", "地图", params.map_ids)):
        rows = catalog.get(key, [])
        if ids != () and not rows:
            raise ValueError(f"{label}目录尚未就绪，请刷新共享数据后再试。")
        defaults = filter_default_visible_champions(rows) if key == "champions" else rows
        if key == "champions":
            rows = [*rows, *(row for row in catalog.get("special", []) if str(row.get("id", "")).isdecimal())]
        selected = tuple(int(row["id"]) for row in defaults) if ids is None else ids
        result = check_ids(selected, rows)
        row_by_id = {int(row["id"]): row for row in rows}
        requested.extend(f"{label} ID {value}" for value in dict.fromkeys(selected))
        resolved.append(result.valid)
        if result.valid:
            counts.append(f"{label} {len(result.valid)} 个")
        if result.unknown:
            warnings.append(f"{len(result.unknown)} 个{label} ID 未找到，确认后跳过")
            excluded.extend(f"{label} ID {value}" for value in result.unknown)
            items.extend(ReviewItem("未找到的 ID（将跳过）", label, str(value)) for value in result.unknown)
        if ids is None and result.valid:
            # 仅折叠确认名单，执行目标仍使用上面冻结的完整 ID 快照。
            items.append(ReviewItem(label, "全部", ""))
        else:
            for value in result.valid:
                row = row_by_id[value]
                items.append(ReviewItem(label, str(row.get("name") or row.get("alias") or value), str(value)))

    special_by_key = {str(row.get("key")): row for row in catalog.get("special", [])}
    for key in params.special_targets:
        row = special_by_key.get(key)
        if row is None:
            raise ValueError("已选特殊内容不在当前目录中，请返回实体总览重新选择。")
        items.append(ReviewItem("特殊内容", str(row.get("display_name") or row.get("name") or key), str(row["id"])))
        requested.append(key)
    if params.special_targets:
        counts.append(f"特殊内容 {len(params.special_targets)} 个")
    summary = " · ".join(counts)
    if excluded:
        summary += f"；跳过 {len(excluded)} 个无效 ID"
    return ExecutionReview(
        replace(
            draft,
            source_summary=summary,
            task_params=replace(params, champion_ids=resolved[0], map_ids=resolved[1]),
            requested_targets=tuple(requested),
            excluded_targets=tuple(excluded),
        ),
        tuple(sorted(items, key=lambda item: item.group != "未找到的 ID（将跳过）")),
        tuple(warnings),
    )
