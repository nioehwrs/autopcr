from ..modulebase import *
from ..config import *
from ...core.pcrclient import pcrclient
from ...db.database import db
from ...model.enums import *
from .autosweep import UniqueEquip1SPMemory

# 专武 0->370 分阶段碎片（未开专从 0 级起算）
# 0->30: 50; 30->130: 10×5次; 130->230: 15×10次; 230->360: 5×13次; 360->370: 5。合计 320
_UNIQUE_EQUIP_STEPS = [
    (30, 50),
    (50, 10), (70, 10), (90, 10), (110, 10), (130, 10),
    (140, 15), (150, 15), (160, 15), (170, 15), (180, 15), (190, 15), (200, 15), (210, 15), (220, 15), (230, 15),
    (240, 5), (250, 5), (260, 5), (270, 5), (280, 5), (290, 5), (300, 5), (310, 5), (320, 5), (330, 5), (340, 5), (350, 5), (360, 5),
    (370, 5),
]

def _unique_equip_memory_to_370(start_level: int) -> int:
    return sum(cost for (end, cost) in _UNIQUE_EQUIP_STEPS if start_level < end)

def _exceed_demand(client: pcrclient, unit_id: int) -> int:
    if unit_id not in db.exceed_level_unit_required:
        return 0
    if unit_id in client.data.unit and client.data.unit[unit_id].exceed_stage:
        return 0
    return db.exceed_level_unit_required[unit_id].consume_num_1

@name('角色碎片缺口')
@notlogin(check_data = True)
@default(True)
@description('计算角色的星级/专武/突破/SP专的总缺口')
class get_explore_drop_memory_demand(Module):
    async def do_task(self, client: pcrclient):
        # 母猪石已购数量 + 商店排除 token
        memory_piece_bought: dict = {}
        exclude_tokens = set()
        shop_content = await client.get_shop_item_list()
        for shop in shop_content.shop_list:
            if shop.system_id == eSystemId.MEMORY_PIECE_SHOP:
                for item in shop.item_list:
                    memory_piece_bought[item.item_id] = item.exchange_count
            elif shop.system_id in (eSystemId.EXPEDITION_SHOP, eSystemId.ARENA_SHOP,
                                    eSystemId.GRAND_ARENA_SHOP, eSystemId.CLAN_BATTLE_SHOP):
                for item in shop.item_list:
                    exclude_tokens.add((item.type, item.item_id))

        # SP专清单（含国服 db 无专数据、仅台服已实装的角色）也按有专武计算
        sp_units = {uid for uid, _ in UniqueEquip1SPMemory.unique_equip_1_sp_memory_id}

        result = []
        for unit_id, memory_id in db.unit_to_memory.items():
            # 排除剧情/NPC/召唤物等非玩家可控单位（集中在特殊 id 段：4xxxx 召唤物、9xxxx/17-19xxxx 剧情角色等）
            # 正常玩家可拥有角色均在 10xxxx~13xxxx 段
            if (unit_id // 10000) in (17, 18, 19, 40, 41, 42, 43, 90, 92, 93):
                continue
            token = (eInventoryType.Item, memory_id)
            if token in db.memory_hard_quest or token in db.memory_shiori_quest:
                continue
            if token in exclude_tokens:
                continue

            owned = unit_id in client.data.unit
            ud = client.data.unit[unit_id] if owned else db.unit_data.get(unit_id)
            if ud is None:
                # 不在角色主表中的单位（如特殊/异常单位：有碎片映射但无角色数据）无法统计，跳过
                continue
            # 已拥有角色起点为当前星级 unit_rarity；未拥有角色在 unit_data 表中只有基础稀有度 rarity
            start_rarity = ud.unit_rarity if owned else ud.rarity
            # 是否明确开 6 星：升星需求表含 6 级即表示可 6 星（对所有角色一致，含未拥有）
            # 避免依赖已拥有角色专属的 unlock_rarity_6_item 关系
            can_rarity_6 = unit_id in db.rarity_up_required and 6 in db.rarity_up_required[unit_id]
            target = 6 if can_rarity_6 else 5
            # rarity_up_required 仅含可升星角色；极端情况下不在表中的角色无法计算星级缺口，记为 0
            rarity_demand = db.get_rarity_memory_demand(unit_id, start_rarity, target, token) if unit_id in db.rarity_up_required else 0

            has_ue = (unit_id in db.unit_unique_equip[1]) or (unit_id in sp_units)
            if has_ue and owned and ud.unique_equip_slot and ud.unique_equip_slot[0].is_slot:
                ue_level = ud.unique_equip_slot[0].enhancement_pt
            else:
                ue_level = 0
            ue_demand = _unique_equip_memory_to_370(ue_level) if has_ue else 0

            exceed_demand = _exceed_demand(client, unit_id)
            sp_demand = 300 if unit_id in sp_units else 0

            need = rarity_demand + ue_demand + exceed_demand + sp_demand
            need -= client.data.get_inventory(token)
            need -= max(0, 80 - memory_piece_bought.get(memory_id, 0))
            gap = max(0, need)
            if gap > 0:
                result.append((unit_id, gap, owned))

        result.sort(key=lambda x: x[1], reverse=True)
        if not result:
            self._log('没有需要靠探险随机掉落补充碎片的角色')
            return
        msg = '\n'.join([
            f'{db.get_unit_name(u)}{"(未拥有)" if not o else ""}: 缺口{g}片'
            for u, g, o in result
        ])
        self._log(msg)
