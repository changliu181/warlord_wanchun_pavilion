# -*- coding: utf-8 -*-
"""**竹版**：搜索能力局部剪枝优化，优先据城、优先吃粮草、优先打掉对方将领。

电脑对手（1P 模式）的**策略入口**：棋盘走位一步 + 攻方的「继续还是收手」。

这一包（`tactics` / `defense` / `offense` / `knobs` + 本文件）就是一版完整的电脑，
别的版本是同级目录的另一个包，各版互不 import。**顶层 ai.py 只负责分派**，
它按「此刻该做决定的那一方」取版本，所以竹对梅时攻守各用各的账。

本模块对外只出这几样（顶层会调，改 竹 时这些**名字与签名别动**）：

    map_step(game)              棋盘上走一步、不值得动就收工
    atk_should_hold(battle)     攻方继续压上还是收手
    defense.ai_should_hold / ai_pick_defender / ai_pick_retreat   守方那三个选择

交战屏的**状态机**（谁该走哪一支）在顶层 ai.py，不在这里——它要按攻守各自的
版本分派，放在版本包里就会串味。评估一律换算成比分板那一套分（将领分
= (100+体力)×武力；地盘分 = 每格地形分 + 格上粮草分）：走位规划（`offense`）
只拿**将领分**那一半当硬指标，地盘另按战线分/据城分从走位意义上折算；
防守那摊（`defense`）则直接读整张计分板。
"""


from constants import CITY
from .knobs import AI_GRAIN_CARRY
from .tactics import atk_should_hold as _atk_should_hold
from . import offense


# 攻方"继续还是收手"那一套（σ 优势过 sigmoid、下一刀有死险就收手）在 tactics.atk_should_hold 里，
# 真实对局与搜索**共用**它（见 tactics.py）——所以推演里那仗什么时候收手，就是实战里什么时候收手。


# --------------------------------------------------------------------------
# 一、进攻回合策略：交给 offense.py 的"回合级模拟规划"
# --------------------------------------------------------------------------
def apply_step(game, step):
    """把 offense.plan_step 挑出的第一步真的落到棋盘上。"""
    gen, kind, cell, info = step
    if kind == "attack":
        game.charge(gen, cell[0], cell[1])
    elif kind == "eat":
        game.eat_grain(gen, info)
    else:
        carry = 0
        if game.grid[cell[0]][cell[1]] == CITY or (
                gen.stamina < gen.stamina_cap * 0.8 and game.grain_at(*gen.pos)):
            carry = min(game.grain_at(*gen.pos), AI_GRAIN_CARRY)
        game.select(gen)                          # 选中一下，让人看得见是谁在动
        game.move(gen, cell[0], cell[1], carry=carry)


def map_step(game):
    """电脑在棋盘上走一步：进攻回合策略规划出"现在该谁动、动什么"，只落实第一步。

    规划是**回合级模拟**（见 offense.plan_step）：先动谁、后动谁、甲一步乙一步
    再甲一步，都在搜索树里比过；这里只执行最优分支的第一步，走完（或打完一仗）
    再重新规划，一直到所有人都动不了、或者再动也没有实质收益，就收工交回合。
    """
    step = offense.plan_step(game, game.current)
    if step is None:
        game.end_turn()                           # 再动也没实质收益：收工，把回合交给对方
        return
    apply_step(game, step)


# --------------------------------------------------------------------------
# 二、交战屏上的决策
# --------------------------------------------------------------------------
def atk_should_hold(battle):
    """攻方的"继续压上还是收手" -> 直接用 tactics 里那一套。

    搜索里也调**同一个** `tactics.atk_should_hold`（见 tactics.py），所以"一仗打到什么时候收手"
    在推演和实战里是同一套判断，不会两边各拍一个。
    """
    return _atk_should_hold(battle)
