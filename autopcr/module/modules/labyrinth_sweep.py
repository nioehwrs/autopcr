from typing import Optional

from ..modulebase import *
from ..config import *
from ...core.pcrclient import pcrclient
from ...db.database import db
from ...model.requests import LabyrinthRetireRequest


@name('黎明界扫荡')
@inttype('labyrinth_sweep_ticket_hold', '保留黎明界票数', 96, list(range(100)))
@LabyrinthGuildConfig('labyrinth_sweep_guild_id', '公会', 5)
@default(True)
class labyrinth_sweep(Module):
    def _max_cleared_difficulty(self, top, guild_id: int) -> Optional[int]:
        cleared = [
            info.difficulty
            for info in (top.guild_cleared_difficulty_list or [])
            if info.guild_id == guild_id and info.difficulty is not None
        ]
        return max(cleared) if cleared else None

    async def do_task(self, client: pcrclient):
        ticket_hold: int = self.get_config('labyrinth_sweep_ticket_hold')
        guild_id: int = self.get_config('labyrinth_sweep_guild_id')
        ticket_count = client.data.get_inventory(db.labyrinth_ticket)
        skip_count = ticket_count - ticket_hold
        if skip_count <= 0:
            raise SkipError(f'当前黎明界票数为{ticket_count}，不超过保留数量{ticket_hold}')

        top = await client.labyrinth_top()
        # 检测到已有黎明界开局，先撤退（撤退不会消耗门票）
        if top.enter_id:
            await client.request(LabyrinthRetireRequest(enter_id=top.enter_id))
            top = await client.labyrinth_top()

        difficulty = self._max_cleared_difficulty(top, guild_id)
        if difficulty is None:
            raise AbortError(f'公会{guild_id}尚未通关黎明界，无法扫荡！')

        self._log(f'公会{guild_id}，难度{difficulty}，当前票数{ticket_count}，扫荡{skip_count}次')
        response = await client.labyrinth_skip(guild_id, skip_count)
        rewards = []
        for reward_list in (
            response.skip_reward_list,
            response.treasure_box_reward_list,
            response.item_list,
        ):
            rewards.extend(reward_list or [])
        if rewards:
            self._log(await client.serialize_reward_summary(rewards))
