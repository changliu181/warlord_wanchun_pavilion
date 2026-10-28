# -*- coding: utf-8 -*-
"""电脑对手的**分派层**：按"此刻该做决定的那一方"把调用转给对应版本的电脑。

电脑有四个版本，各是一个完整的包（`mei/` / `lan/` / `zhu/` / `ju/`，见各自的 `__init__.py`）：

    * **梅（mei/）**：参照版，冻结不动，只当基准；
    * **菊（ju/）**：升级版（当前主力），提水平就动它，拿"菊对梅的胜率"验证；
    * **兰（lan/）/ 竹（zhu/）**：从菊复制出来的实验位，此刻与菊策略完全一致，各占一个包独立迭代。

各包互不 import，各带自己的一整套旋钮（`<版本>/knobs.py` 是通用性子，进攻的
`KILL_W` 那批在 `<版本>/offense.py` 顶部，防守的 `OWN_*` 在 `<版本>/defense.py` 顶部），
所以改一版不会牵动别版。

**这里只做三件事**（策略一律不写在顶层，免得各版串味）：

1. `resolve(name)` —— 版本名 -> 版本包；
2. `ai_pump(game)` —— 驱动器：每 `AI_STEP_FRAMES` 帧走一步（棋盘与交战屏共用这个节拍器）；
3. `battle_step(battle)` —— **交战屏状态机**。它必须留在顶层：一场交战里攻守双方
   可能属于不同版本（菊对梅就这么打），所以每一个选择都按**该做决定的那一方**
   取版本，而不是按"谁在进攻"。

版本包里只出策略入口：`map_step(game)`、`atk_should_hold(battle)`、
`defense.ai_should_hold / ai_pick_defender / ai_pick_retreat`。
"""

import importlib

from constants import AI_VERSIONS, AI_STEP_FRAMES


def resolve(name):
    """版本名 -> 版本包（`mei` / `ju`）；名字不认识就抛 KeyError 并列出可用的版本。"""
    try:
        module_name = AI_VERSIONS[name]
    except KeyError:
        raise KeyError(
            f"没有名为 {name!r} 的电脑版本；可用的是 {list(AI_VERSIONS)}。") from None
    return importlib.import_module(module_name)


# --------------------------------------------------------------------------
# 一、驱动器：棋盘与交战屏共用这一个节拍器
# --------------------------------------------------------------------------
def ai_pump(game):
    """电脑的驱动器：每 AI_STEP_FRAMES 帧走一步，让人类看得清它在干什么。"""
    if game.ai_timer > 0:
        game.ai_timer -= 1
        return
    game.ai_timer = AI_STEP_FRAMES
    if game.over:
        return
    game.start_recording()        # 迟到的第一手：这一局有电脑（有的话），补开录像
    if game.battle is not None:
        battle_step(game.battle)
    elif game.is_ai():
        ai_map_step(game)


def ai_map_step(game):
    """棋盘上走一步：按当前行动方的版本交给它的 `map_step`。"""
    resolve(game.ai_version(game.current)).map_step(game)


# --------------------------------------------------------------------------
# 二、交战屏状态机：每一个决定按"该做决定的那一方"取版本
#
# 攻守属于不同版本时（菊 vs 梅），"谁先出阵 / 打还是撤 / 往哪撤"是**守方**的版本在算，
# "继续还是收手"是**攻方**的版本在算——两边各用各的账，不会串味。
# --------------------------------------------------------------------------
def battle_step(battle):
    """电脑在交战屏上做一步决定；轮不到它或正在放动画时就什么都不做。"""
    game = battle.game
    if battle.state == battle.PICK_DEFENDER:
        owner = battle.defenders[0].owner
        if game.is_ai(owner):
            battle.pick_defender(resolve(game.ai_version(owner)).defense.ai_pick_defender(battle))
    elif battle.state == battle.READY:
        if game.is_ai(battle.atk.owner):
            battle.act("roll")
    elif battle.state == battle.CHOOSE_ATK:
        owner = battle.atk.owner
        if not game.is_ai(owner):
            return
        brain = resolve(game.ai_version(owner))
        if brain.atk_should_hold(battle):
            battle.act("hold")
        elif battle.can_retreat():
            battle.act("atk_retreat")
        else:
            battle.act("hold")                  # 没退路，只能接着缠斗
    elif battle.state == battle.CHOOSE_DFD:
        owner = battle.dfd.owner
        if not game.is_ai(owner):
            return
        brain = resolve(game.ai_version(owner))
        if brain.defense.ai_should_hold(battle):
            battle.act("hold")
        elif battle.can_retreat():
            battle.act("dfd_retreat")
        else:
            battle.act("hold")                  # 没退路，只能接着缠斗
    elif battle.state == battle.RETREAT_PICK:
        owner = battle.dfd.owner
        if game.is_ai(owner):
            brain = resolve(game.ai_version(owner))
            battle.act(f"retreat:{brain.defense.ai_pick_retreat(battle)}")
    elif battle.state == battle.OVER and game.is_ai(battle.atk.owner):
        battle.act("close")                     # 电脑自己打完的，自己收场
