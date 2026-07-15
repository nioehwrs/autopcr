from typing import Dict, List

from ..modulebase import *
from ..config import *
from ...core.pcrclient import pcrclient
from ...model.requests import (
    LabyrinthTopRequest, LabyrinthEnterRequest,
    LabyrinthRetireRequest, LabyrinthResumeRequest,
)
from ...model.enums import eLabyrinthBlockType, eInventoryType
from ...model.common import LabyrinthMapInfo
from ...db.database import db
from ...db.models import (
    LabyrinthQuestDatum, LabyrinthWaveGroupDatum,
    LabyrinthEnemyParameter,
)

# Cache: quest_id -> main boss unit_id (highest HP enemy in the wave)
_boss_unit_cache: Dict[int, int] = {}
# Cache: unit_id -> boss display name
_boss_name_cache: Dict[int, str] = {}


def _get_session():
    return db.dbmgr.session()


def _get_boss_unit_id(quest_id: int) -> int:
    """Get the main boss unit_id (highest HP enemy) for a given boss quest_id."""
    if quest_id in _boss_unit_cache:
        return _boss_unit_cache[quest_id]

    session = _get_session()
    try:
        quest = session.query(LabyrinthQuestDatum).filter(
            LabyrinthQuestDatum.quest_id == quest_id
        ).first()
        if not quest:
            _boss_unit_cache[quest_id] = 0
            return 0

        wave = session.query(LabyrinthWaveGroupDatum).filter(
            LabyrinthWaveGroupDatum.wave_group_id == quest.wave_group_id
        ).first()
        if not wave:
            _boss_unit_cache[quest_id] = 0
            return 0

        enemy_ids = [
            wave.enemy_id_1, wave.enemy_id_2, wave.enemy_id_3,
            wave.enemy_id_4, wave.enemy_id_5
        ]
        best_unit_id = 0
        best_hp = 0
        for eid in enemy_ids:
            if not eid:
                continue
            ep = session.query(LabyrinthEnemyParameter).filter(
                LabyrinthEnemyParameter.enemy_id == eid
            ).first()
            if ep and ep.hp > best_hp:
                best_hp = ep.hp
                best_unit_id = ep.unit_id

        _boss_unit_cache[quest_id] = best_unit_id
    finally:
        session.close()
    return _boss_unit_cache[quest_id]


def _get_boss_display_name(unit_id: int) -> str:
    """Get display name for a boss unit_id."""
    if not unit_id:
        return "未知"
    if unit_id in _boss_name_cache:
        return _boss_name_cache[unit_id]

    session = _get_session()
    try:
        ep = session.query(LabyrinthEnemyParameter).filter(
            LabyrinthEnemyParameter.unit_id == unit_id
        ).first()
        if ep:
            _boss_name_cache[unit_id] = ep.name
        else:
            _boss_name_cache[unit_id] = f"unit_{unit_id}"
    finally:
        session.close()
    return _boss_name_cache[unit_id]


# --- Static data: guilds and bosses ---

_GUILD_LIST: List[Tuple[int, str]] = []  # loaded lazily from DB

# Manual guild name overrides for known inconsistencies
_GUILD_NAME_MAP = {
    "破曉 之星": "破曉之星",
}


def _clean_guild_name(raw: str) -> str:
    name = raw.replace("\\n", " ")
    if name in _GUILD_NAME_MAP:
        return _GUILD_NAME_MAP[name]
    # Strip trailing parenthesized suffix e.g. "王宫骑士团（ランドソル支部）" → "王宫骑士团"
    for c in ("（", "("):
        idx = name.find(c)
        if idx > 0:
            return name[:idx]
    return name


def _load_guild_list() -> List[Tuple[int, str]]:
    global _GUILD_LIST
    if not _GUILD_LIST:
        session = _get_session()
        try:
            from ...db.models import LabyrinthEnterGuild
            guilds = session.query(LabyrinthEnterGuild).all()
            _GUILD_LIST = [(g.guild_id, _clean_guild_name(g.guild_name)) for g in guilds]
        finally:
            session.close()
    return _GUILD_LIST


# Area 3: 77033 series bosses
_AREA3_BOSS_DATA: List[Tuple[int, str]] = [
    (301206, "巨型魔像"),
    (303306, "暗黑滴水嘴兽"),
    (319604, "冰霜魔狼"),
    (312505, "厄勒克特拉夫人"),
    (306604, "毒液沙罗曼蛇"),
]
_AREA3_UNIT_IDS = [uid for uid, _ in _AREA3_BOSS_DATA]

# Area 5: 77053 series bosses
_AREA5_BOSS_DATA: List[Tuple[int, str]] = [
    (302501, "奇美拉"),
    (301701, "炸脖龙"),
    (310103, "愤怒巨龙"),
    (315004, "领主哥布林"),
    (319401, "究极守护者"),
]
_AREA5_UNIT_IDS = [uid for uid, _ in _AREA5_BOSS_DATA]

_BOSS_META: Dict[int, str] = {}
for uid, bname in _AREA3_BOSS_DATA + _AREA5_BOSS_DATA:
    _BOSS_META[uid] = bname

# --- Map visualization ---

_TYPE_SYMBOLS = {
    eLabyrinthBlockType.NONE: "·",
    eLabyrinthBlockType.NORMAL_QUEST: "⚔",
    eLabyrinthBlockType.HARD_QUEST: "⚔",
    eLabyrinthBlockType.TICKET: "🎫",
    eLabyrinthBlockType.EVENT: "❓",
    eLabyrinthBlockType.RELIC: "💎",
    eLabyrinthBlockType.SHOP: "🛒",
    eLabyrinthBlockType.BOSS_QUEST: "👑",
}
_TYPE_NAMES = {
    eLabyrinthBlockType.NONE: "空",
    eLabyrinthBlockType.NORMAL_QUEST: "普通战",
    eLabyrinthBlockType.HARD_QUEST: "精英战",
    eLabyrinthBlockType.TICKET: "门票",
    eLabyrinthBlockType.EVENT: "事件",
    eLabyrinthBlockType.RELIC: "遗物",
    eLabyrinthBlockType.SHOP: "商店",
    eLabyrinthBlockType.BOSS_QUEST: "Boss",
}


def _draw_map(log_func, map_list: List[LabyrinthMapInfo]):
    """Print a simple node map of the labyrinth."""
    areas: Dict[int, List[LabyrinthMapInfo]] = {}
    for block in map_list:
        areas.setdefault(block.area, []).append(block)

    for area_num in sorted(areas.keys()):
        blocks = areas[area_num]
        max_col = max(b.column for b in blocks)
        log_func("")
        log_func(f"  ═══ 区域 {area_num}（共{max_col}列）═══")
        log_func("")

        for col in range(1, max_col + 1):
            col_blocks = [b for b in blocks if b.column == col]
            if not col_blocks:
                continue

            log_func(f"  ── 列{col} ──────────────────────")
            for b in sorted(col_blocks, key=lambda x: x.row):
                sym = _TYPE_SYMBOLS.get(b.block_type, "?")
                vis = "✓" if b.is_visited else " "
                name = _TYPE_NAMES.get(b.block_type, "?")

                line = f"    ({b.row}) [{sym}{vis}] {name}"

                # Targets
                if b.next_block_id_list:
                    targets = []
                    for nid in b.next_block_id_list:
                        tgt = next((x for x in blocks if x.block_id == nid), None)
                        if tgt:
                            sym2 = _TYPE_SYMBOLS.get(tgt.block_type, "?")
                            targets.append(f"列{tgt.column}({tgt.row}){sym2}")
                    if targets:
                        line += "  → " + " ".join(targets)

                # Boss name
                if b.block_type == eLabyrinthBlockType.BOSS_QUEST:
                    boss_name = _get_boss_display_name(_get_boss_unit_id(b.quest_id))
                    line += f"  🏆{boss_name}"

                # Endpoint
                if b.IsAreaLastPoint:
                    line += " ⭐"

                log_func(line)

        # Column-to-column connection summary
        for col in range(1, max_col):
            pairs = set()
            for b in blocks:
                if b.column == col and b.next_block_id_list:
                    for nid in b.next_block_id_list:
                        tgt = next((x for x in blocks if x.block_id == nid), None)
                        if tgt:
                            pairs.add(f"{b.row}→{tgt.row}")
            if pairs:
                log_func(f"    列{col}→列{col+1}: {', '.join(sorted(pairs))}")

        # Summary
        from collections import Counter
        type_counts = Counter(b.block_type for b in blocks)
        parts = []
        for btype, count in type_counts.most_common():
            parts.append(f"{_TYPE_NAMES.get(btype, '?')}×{count}")
        log_func(f"  📊 {', '.join(parts)}")


# --- Path scoring and analysis ---

_SCORE_MAP = {
    eLabyrinthBlockType.NONE: 0,
    eLabyrinthBlockType.NORMAL_QUEST: 300,
    eLabyrinthBlockType.HARD_QUEST: 1200,
    eLabyrinthBlockType.TICKET: 400,
    eLabyrinthBlockType.EVENT: 100,
    eLabyrinthBlockType.RELIC: 300,
    eLabyrinthBlockType.SHOP: 100,
    eLabyrinthBlockType.BOSS_QUEST: 1900,
}

_PATH_NAMES = {
    eLabyrinthBlockType.NONE: "起点",
    eLabyrinthBlockType.NORMAL_QUEST: "普通怪物",
    eLabyrinthBlockType.HARD_QUEST: "困难怪物",
    eLabyrinthBlockType.TICKET: "角色",
    eLabyrinthBlockType.EVENT: "事件",
    eLabyrinthBlockType.RELIC: "遗物",
    eLabyrinthBlockType.SHOP: "商店",
    eLabyrinthBlockType.BOSS_QUEST: "Boss",
}

_ROW_LABELS = {1: "上", 2: "中", 3: "下"}


def _optimal_path(blocks: List[LabyrinthMapInfo]):
    """DP: max score & optimal path. Returns (score, [block_id...])."""
    dp, nxt = {}, {}
    for col in range(max(b.column for b in blocks), 0, -1):
        for b in (x for x in blocks if x.column == col):
            s = _SCORE_MAP.get(b.block_type, 0)
            best, best_n = 0, None
            if b.next_block_id_list:
                for nid in b.next_block_id_list:
                    bs = dp.get(nid)
                    if bs is not None and bs > best:
                        best, best_n = bs, nid
            dp[b.block_id] = s + best
            nxt[b.block_id] = best_n
    starts = [b for b in blocks if b.column == 1]
    if not starts: return 0, []
    start = max(starts, key=lambda b: dp.get(b.block_id, 0))
    path = [start.block_id]
    while nxt.get(path[-1]) is not None:
        path.append(nxt[path[-1]])
    return dp.get(start.block_id, 0), path


def _fmt_path(blocks: List[LabyrinthMapInfo], path_ids: List[int]) -> str:
    """Format optimal path as compact string."""
    m = {b.block_id: b for b in blocks}
    cc = {}
    for b in blocks: cc[b.column] = cc.get(b.column, 0) + 1
    parts = []
    for bid in path_ids:
        b = m[bid]
        rl = "合流" if cc.get(b.column, 1) <= 1 else _ROW_LABELS.get(b.row, "?")
        tn = _PATH_NAMES.get(b.block_type, "?")
        if b.block_type == eLabyrinthBlockType.BOSS_QUEST:
            tn = f"Boss({_get_boss_display_name(_get_boss_unit_id(b.quest_id))})"
        parts.append(f"{b.column}{rl}【{tn}】")
    return "-".join(parts)


def _can_reach(blocks, fc, fr, tc, tr):
    src = next((b for b in blocks if b.column == fc and b.row == fr), None)
    dst = next((b for b in blocks if b.column == tc and b.row == tr), None)
    if not src or not dst: return False
    adj = {}
    for b in blocks:
        if b.next_block_id_list:
            for nid in b.next_block_id_list:
                tgt = next((x for x in blocks if x.block_id == nid), None)
                if tgt: adj.setdefault(b.block_id, []).append(tgt.block_id)
    seen, q = {src.block_id}, [src.block_id]
    while q:
        cur = q.pop(0)
        if cur == dst.block_id: return True
        for n in adj.get(cur, []):
            if n not in seen: seen.add(n); q.append(n)
    return False


def _has_path_with_min_types(area_blocks: List[LabyrinthMapInfo],
                             required: Dict[eLabyrinthBlockType, int]) -> List[int]:
    """Return a path (list of block_ids) from start to end with at least N nodes of each type, or empty list."""
    starts = [b for b in area_blocks if b.column == 1]
    if not starts:
        return []
    # Build adjacency
    adj: Dict[int, List[int]] = {}
    for b in area_blocks:
        if b.next_block_id_list:
            for nid in b.next_block_id_list:
                tgt = next((x for x in area_blocks if x.block_id == nid), None)
                if tgt:
                    adj.setdefault(b.block_id, []).append(tgt.block_id)
    # DFS all paths from each start node
    def dfs(node_id: int, visited: set, counts: Dict[eLabyrinthBlockType, int],
            path: List[int]) -> List[int]:
        node = next(b for b in area_blocks if b.block_id == node_id)
        new_counts = dict(counts)
        if node.block_type in new_counts:
            new_counts[node.block_type] = new_counts[node.block_type] - 1
        # 用无出边的节点作为终点
        if not adj.get(node_id):
            if all(v <= 0 for v in new_counts.values()):
                return path + [node_id]
        for nid in adj.get(node_id, []):
            if nid not in visited:
                result = dfs(nid, visited | {nid}, new_counts, path + [node_id])
                if result:
                    return result
        return []
    for s in starts:
        result = dfs(s.block_id, {s.block_id}, dict(required), [])
        if result:
            return result
    return []


def _check_filters(blocks: Dict[int, List[LabyrinthMapInfo]],
                   dual_ticket: bool, dual_elite: bool, dual_relic: bool,
                   quality: bool, log_func) -> bool:
    """Check additional filters. Returns True if all enabled checks pass."""

    def _has_dual_same_path(area_blocks: List[LabyrinthMapInfo],
                            target_type: eLabyrinthBlockType) -> bool:
        """Check if any two nodes of given type are on the same reachable path."""
        nodes = [b for b in (area_blocks or []) if b.block_type == target_type]
        if len(nodes) < 2:
            return False
        for i, n1 in enumerate(nodes):
            for n2 in nodes[i + 1:]:
                if _can_reach(area_blocks, n1.column, n1.row, n2.column, n2.row):
                    return True
        return False

    if dual_ticket and not _has_dual_same_path(blocks.get(1), eLabyrinthBlockType.TICKET):
        return False
    if dual_elite and not _has_dual_same_path(blocks.get(4), eLabyrinthBlockType.HARD_QUEST):
        return False

    if dual_relic:
        a4 = blocks.get(4)
        if a4:
            from collections import Counter
            t4 = Counter(b.block_type for b in a4)
            log_func(f"  4图节点: {', '.join(f'{_TYPE_NAMES[t]}×{c}' for t, c in t4.most_common())}")
        # 要求4图存在一条路径，至少包含2个遗物和2个精英
        if not a4 or not _has_path_with_min_types(a4, {
            eLabyrinthBlockType.RELIC: 2,
            eLabyrinthBlockType.HARD_QUEST: 2,
        }):
            log_func("  最多遗物路线不满足，打印4图地图：")
            _draw_map(log_func, a4)
            return False
        # _check_filters 不保存路径，成功后在 do_task 中重新获取用于显示

        # 其他区域必须达到理论最高分
        for area_num in sorted(blocks):
            if area_num == 4:
                continue
            blk = blocks[area_num]
            score, _ = _optimal_path(blk)
            cols: Dict[int, List[LabyrinthMapInfo]] = {}
            for b in blk: cols.setdefault(b.column, []).append(b)
            theoretical = sum(max(_SCORE_MAP.get(b.block_type, 0) for b in cols[c]) for c in cols)
            if score != theoretical:
                log_func(f"  区域{area_num}未达到理论最高分")
                return False

    if quality:
        total_actual = total_theoretical = 0
        for area_num in sorted(blocks):
            blk = blocks[area_num]
            score, _ = _optimal_path(blk)
            cols: Dict[int, List[LabyrinthMapInfo]] = {}
            for b in blk: cols.setdefault(b.column, []).append(b)
            theoretical = sum(max(_SCORE_MAP.get(b.block_type, 0) for b in cols[c]) for c in cols)
            total_actual += score
            total_theoretical += theoretical
        if total_actual < total_theoretical:
            return False

    return True


# --- Config classes ---

class LabyrinthDifficultyConfig(SingleChoiceConfig):
    def __init__(self, key: str, desc: str):
        super().__init__(key, desc, 1, [1, 2, 3, 4, 5])

    def candidate_display(self, candidate: int) -> str:
        return f"难度{candidate}"


class LabyrinthGuildConfig(SingleChoiceConfig):
    def __init__(self, key: str, desc: str):
        super().__init__(key, desc, 1, lambda: [gid for gid, _ in _load_guild_list()])

    def candidate_display(self, candidate: int) -> str:
        for gid, gname in _load_guild_list():
            if gid == candidate:
                return gname
        return str(candidate)

    def process_value(self, value):
        if isinstance(value, (tuple, list)):
            return int(value[0])
        if isinstance(value, str):
            return int(value.split(',')[0].strip())
        return int(value)

    def validate_value(self, value):
        valid_ids = {gid for gid, _ in _load_guild_list()}
        return value if value in valid_ids else None


class LabyrinthBossMultiConfig(MultiChoiceConfig):
    def __init__(self, key: str, desc: str, area: int):
        self._area = area
        self._ids = _AREA3_UNIT_IDS if area == 3 else _AREA5_UNIT_IDS
        super().__init__(key, desc, [], lambda: list(self._ids), short_display=True)

    def candidate_display(self, candidate: int) -> str:
        return _BOSS_META.get(candidate, str(candidate))

    def candidate_tag(self, candidate: int) -> List[str]:
        return []

    def process_value(self, value):
        if not value:
            return []
        if not isinstance(value, list):
            value = [value]
        result = []
        for v in value:
            if v is None:
                continue
            if isinstance(v, (tuple, list)):
                result.append(int(v[0]))
            else:
                result.append(int(v))
        return result

    def validate_value(self, value: List):
        if not value:
            return []
        valid = [v for v in value if v in self._ids]
        return valid if valid else None


# --- Modules ---

@description('黎明界刷开局，支持 Boss 筛选。基础路线为1图双角色+4图双精英，最优路线与最多遗物路线为理论分数最高。')
@name("黎明界刷开局")
@LabyrinthBossMultiConfig("labyrinth_reset_boss_area5", "区域5 Boss（与区域3合计至少选4个）", area=5)
@LabyrinthBossMultiConfig("labyrinth_reset_boss_area3", "区域3 Boss（与区域5合计至少选4个）", area=3)
@LabyrinthGuildConfig("labyrinth_reset_guild", "公会")
@LabyrinthDifficultyConfig("labyrinth_reset_difficulty", "难度")
@SingleChoiceConfig("labyrinth_reset_route_pref", "路线偏好", "基础路线", ["基础路线", "最优路线", "最多遗物路线"])
@IntConfig("labyrinth_reset_max_attempts", "尝试次数上限", 100, list(range(1, 501)))
class labyrinth_reset(Module):
    async def do_task(self, client: pcrclient):
        difficulty = self.get_config("labyrinth_reset_difficulty")
        guild_id = self.get_config("labyrinth_reset_guild")
        target_bosses_area3: List[int] = self.get_config("labyrinth_reset_boss_area3")
        target_bosses_area5: List[int] = self.get_config("labyrinth_reset_boss_area5")
        route_pref = self.get_config("labyrinth_reset_route_pref")
        dual_ticket = (route_pref == "基础路线")
        dual_elite = (route_pref == "基础路线")
        dual_relic = (route_pref == "最多遗物路线")
        quality = (route_pref == "最优路线")
        max_attempts = self.get_config("labyrinth_reset_max_attempts")

        if not target_bosses_area3:
            raise AbortError("请至少选择一个区域3 Boss")
        if not target_bosses_area5:
            raise AbortError("请至少选择一个区域5 Boss")
        if len(target_bosses_area3) + len(target_bosses_area5) < 4:
            raise AbortError(
                f"区域3与区域5 Boss合计至少需选择 4 个，当前共 {len(target_bosses_area3) + len(target_bosses_area5)} 个")

        # Check labyrinth ticket count
        ticket_cnt = client.data.get_inventory((eInventoryType.Item, 99013))
        self._log(f"迷宫通行证: {ticket_cnt} 张")
        if ticket_cnt <= 0:
            raise AbortError("迷宫通行证不足，无法进入黎明界")

        names3 = ', '.join(_get_boss_display_name(uid) for uid in target_bosses_area3)
        names5 = ', '.join(_get_boss_display_name(uid) for uid in target_bosses_area5)

        self._log(f"目标: 难度{difficulty}, 公会{guild_id}")
        self._log(f"  区域3 Boss: {names3}")
        self._log(f"  区域5 Boss: {names5}")

        top = await client.request(LabyrinthTopRequest())
        if top.enter_id:
            raise AbortError(
                f"当前有进行中的黎明界 (enter_id={top.enter_id}, "
                f"公会={top.guild_id}, 难度={top.difficulty})。\n"
                f"请先通过网页端【放弃黎明界】或游戏内手动放弃后，再使用刷开局功能。"
            )

        max_attempts = self.get_config("labyrinth_reset_max_attempts")
        for attempt in range(1, max_attempts + 1):
            enter_resp = await client.request(LabyrinthEnterRequest(
                guild_id=guild_id,
                difficulty=difficulty
            ))
            enter_id = enter_resp.enter_id

            # Group by area & check
            areas_dict: Dict[int, List[LabyrinthMapInfo]] = {}
            for b in enter_resp.map_list:
                areas_dict.setdefault(b.area, []).append(b)

            boss_ok = True
            for area_num in sorted(areas_dict):
                for b in areas_dict[area_num]:
                    if b.block_type == eLabyrinthBlockType.BOSS_QUEST:
                        uid = _get_boss_unit_id(b.quest_id)
                        targets = target_bosses_area3 if b.area == 3 else target_bosses_area5
                        if uid not in targets:
                            boss_ok = False

            if not boss_ok:
                await client.request(LabyrinthRetireRequest(enter_id=enter_id))
                continue

            if not _check_filters(areas_dict, dual_ticket, dual_elite, dual_relic, quality, self._log):
                await client.request(LabyrinthRetireRequest(enter_id=enter_id))
                continue

            # Success - print full details
            self._log(f"--- 第{attempt}次尝试 ---")
            route_label = route_pref if route_pref != "基础路线" else "基础"
            self._log(f"🎉 命中目标{route_label}路线开局，总尝试次数:{attempt}")
            total_actual = 0
            for area_num in sorted(areas_dict):
                blk = areas_dict[area_num]
                if dual_relic and area_num == 4:
                    # 最多遗物路线：显示满足 2 遗物+2 精英的路径，而非最优路径
                    path_ids = _has_path_with_min_types(blk, {
                        eLabyrinthBlockType.RELIC: 2,
                        eLabyrinthBlockType.HARD_QUEST: 2,
                    })
                    actual_score = sum(_SCORE_MAP.get(
                        next(b for b in blk if b.block_id == bid).block_type, 0) for bid in path_ids)
                else:
                    actual_score, path_ids = _optimal_path(blk)
                path_str = _fmt_path(blk, path_ids)
                cols: Dict[int, List[LabyrinthMapInfo]] = {}
                for b in blk: cols.setdefault(b.column, []).append(b)
                theoretical = sum(max(_SCORE_MAP.get(b.block_type, 0) for b in cols[c]) for c in cols)
                total_actual += actual_score
                boss_tag = ""
                for b in blk:
                    if b.block_type == eLabyrinthBlockType.BOSS_QUEST:
                        boss_tag = f"  ✓{_get_boss_display_name(_get_boss_unit_id(b.quest_id))}"
                self._log(f"  区域{area_num}:{path_str}  理论:{theoretical} 实际:{actual_score}{boss_tag}")

            guild_name = ""
            for gid, gname in _load_guild_list():
                if gid == guild_id: guild_name = gname; break
            self._log(f"  难度:{difficulty} 公会:{guild_name} 总计理论:{total_actual}")

            self._table({
                "结果": "成功",
                "尝试次数": str(attempt),
                "enter_id": str(enter_id),
                "难度": str(difficulty),
                "公会": guild_name,
            })
            return

        raise AbortError(f"已达到最大尝试次数({max_attempts})，仍未刷到符合设定的开局")


@description('放弃当前进行中的黎明界探索，不进行结算。')
@name("放弃黎明界")
@default(True)
class labyrinth_retire(Module):
    async def do_task(self, client: pcrclient):
        top = await client.request(LabyrinthTopRequest())
        if not top.enter_id:
            self._log("当前没有进行中的黎明界，无需放弃")
            return

        self._log(f"检测到进行中的黎明界:")
        self._log(f"  enter_id: {top.enter_id}")
        self._log(f"  公会: {top.guild_id}")
        self._log(f"  难度: {top.difficulty}")

        await client.request(LabyrinthRetireRequest(enter_id=top.enter_id))
        self._log("已放弃本次探索")

        self._table({
            "结果": "已放弃",
            "enter_id": str(top.enter_id),
            "公会": str(top.guild_id),
            "难度": str(top.difficulty),
        })


@description('查看当前进行中的黎明界地图（不进入/不撤退，仅查看）。\n如果没有进行中的黎明界则无操作。')
@name("查看黎明界地图")
@default(True)
class labyrinth_view(Module):
    async def do_task(self, client: pcrclient):
        top = await client.request(LabyrinthTopRequest())
        if not top.enter_id:
            self._log("当前没有进行中的黎明界")
            return

        self._log(f"正在查看进行中的黎明界:")
        self._log(f"  enter_id: {top.enter_id}")
        self._log(f"  公会: {top.guild_id}")
        self._log(f"  难度: {top.difficulty}")

        resume = await client.request(LabyrinthResumeRequest(
            enter_id=top.enter_id
        ))

        if resume.map_list:
            _draw_map(self._log, resume.map_list)
        else:
            self._log("未获取到地图数据")
