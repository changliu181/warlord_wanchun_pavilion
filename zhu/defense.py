# -*- coding: utf-8 -*-
"""**防守回合策略**：被打到一格、该做决定的时候，守方怎么选"打还是撤"。

守城时，在无死亡风险下，守方不动脑子，直接接着打。
非守城时，思路是**磁性战术**：不跟优势之敌硬碰。先用消耗换掉对手一部分体力，再把这片地域让出去、
退到安全的后方喘口气，等下一回合回过头来反击。判断"该守还是该让"的依据不是一格值多少钱，
而是**这一片地域在本回合末归谁的概率**：归属概率高就坚守，低就只做必要的消耗然后退。

概率是**解析算出来**的，不掷骰子，分两批推演（`cell_hold_p`）：本批是正压上来的 `battle.atk`，
一定来；下一批是这回合还够得着本格的其他敌将，按"够不够得着 / 值不值得来"折一个出现概率 q。
最后把推演**真的落回局面**、读一遍计分板（`_outcome`），于是每个选择都有分数：接着打是
`p × 守成 + (1 − p) × 守败`，撤退是一个确定值，两者比大小做决定。

依赖单向：`tactics.py` <- `defense.py`。只读 game / battle 的公开状态、**不 import 它们**。
"""

import math

from constants import CITY, LOSS_MAX_PER_MIGHT, MOVE_POINTS
from .knobs import AI_CITY_PULL, AI_DEBT_WORTH, AI_THREAT_WORTH
from .tactics import (
    Board, Snapshot, after_battle_stamina, average_damage, cell_value,
    death_risk, defender_stamina, general_value, retreat_spot, strikers, wins_duel,
)

# === 旋钮（含义与调法见 README「六、电脑对手」） ===
OWN_TIE = 0.5           # "打平"（同归于尽）算谁赢：0.5 = 对半开
OWN_DEFEND_TOP = 0.5    # 本格归属概率高过它就坚守；低过它才动算盘比"守划不划算"
OWN_HOLD_EDGE = 0       # 低概率那一支的门槛：守要比撤多值这么多分才顶（防临界反复）
OWN_SCALE_WORTH = 0.35  # 归属概率每偏离 0.5 一格，按守将身价的几成折进落脚点的分
OWN_TURN_SD = 0.8       # "第几回合能走到某格"的不确定性（回合），赛跑时铺成分布
OWN_UNREACHABLE = 12    # 到不了某格时，把到达时间推到这么多回合之后
Q_REACH = 0.5           # 第二波敌将够得着本格 -> 他来的概率
Q_WINS = 0.3            # 他自认打得过守将 -> 出现概率再加这么多
Q_CAP = 0.85            # 出现概率的上限

# === 一、谁先倒下：单挑的胜负分布（正态近似，不掷骰子） ===
def _spread(center, sd, floor=1):
    """把均值 center、标准差 sd 的正态铺到整数点 -> {整数: 概率}。"""
    lo = max(floor, int(math.floor(center - 3 * sd)))
    hi = max(lo + 1, int(math.ceil(center + 3 * sd)))
    mass, total = {}, 0.0
    for t in range(lo, hi + 1):
        w = math.exp(-0.5 * ((t - center) / sd) ** 2)
        mass[t] = w
        total += w
    if total <= 0:
        return {max(floor, int(round(center))): 1.0}
    return {t: w / total for t, w in mass.items()}

def _rounds_mass(stamina, might):
    """"把 stamina 点体力打光要几回合"的分布 -> {回合数: 概率}。"""
    if stamina <= 0:
        return {0: 1.0}                       # 已经倒下：不用回合
    span = max(1, LOSS_MAX_PER_MIGHT * might)
    mean = (1 + span) / 2
    m = stamina / mean
    return _spread(m, math.sqrt(max(0.25, stamina * (span * span - 1) / 12 / mean ** 3)))

def _turns_mass(turns):
    """"第几回合能走到这一格"的分布 -> {回合: 概率}（到不了就推到很远）。"""
    return _spread(OWN_UNREACHABLE if turns is None else turns, OWN_TURN_SD, floor=0)

def _race(ours, theirs):
    """两边"到达/打光回合数"的分布之比 -> (我方先到的概率, 预计拉锯几回合)。"""
    win = tie = span = 0.0
    for ta, wa in ours.items():
        for tb, wb in theirs.items():
            p = wa * wb
            if ta < tb:
                win += p
                span += ta * p
            elif ta == tb:
                tie += p
                span += ta * p
            else:
                span += tb * p
    return win + OWN_TIE * tie, span

def duel_prob(mine, my_might, foe_stamina, foe_might):
    """我剩 mine 体力、对方剩 foe_stamina 时，我把对方磨倒的概率 -> 0~1。"""
    if mine <= 0:
        return 0.0
    if foe_stamina <= 0:
        return 1.0
    return _race(_rounds_mass(foe_stamina, my_might),   # 我要几回合打光他
                 _rounds_mass(mine, foe_might))[0]      # 他要几回合打光我

def duel_span(mine, my_might, foe_stamina, foe_might):
    """这场单挑预计打几回合（谁先到，事就停在谁那儿）。"""
    return _race(_rounds_mass(max(1, foe_stamina), my_might),
                 _rounds_mass(max(1, mine), foe_might))[1]

# === 二、本回合末每格归谁：两批推演 + 周围格子的赛跑 ===
def _next_batch(battle, defenders, board):
    """这一回合还够得着本格的其他敌将 -> [(敌将, 他来的概率 q)]。"""
    game = battle.game
    out = []
    for foe in strikers(board, battle.cell, 1 - defenders[0].owner):
        if foe is battle.atk or not foe.alive:
            continue
        q = Q_REACH
        if any(wins_duel(foe.stamina, foe.might,
                         defender_stamina(game, dfd, battle.cell), dfd.might)
               for dfd in defenders):
            q += Q_WINS
        out.append((foe, min(Q_CAP, q)))
    out.sort(key=lambda kv: -kv[1])
    return out

def _order_p(game, order, atk, cell, wound=0.0):
    """按**出场顺序**逐位推演 -> (这一格守住的概率, 守将全打光时攻方被磨掉的比例)。"""
    remain, alive, p = atk.stamina, 1.0, 0.0
    for dfd in order:
        if remain <= 0:
            return 1.0, 1.0
        mine = defender_stamina(game, dfd, cell)
        if wound:                  # 守将先被上一批磨过（同一笔账）
            mine = max(1.0, mine * (1 - wound))
        q = duel_prob(mine, dfd.might, remain, atk.might)     # 这位顶得住的概率
        p += alive * q             # 前几位都输了、轮到他、他赢了
        alive *= (1 - q)
        remain = max(0.0, remain - duel_span(mine, dfd.might, remain, atk.might)
                     * average_damage(dfd.might))              # 这位倒下，攻方也被磨了一轮
    return p, (1.0 - remain / atk.stamina if atk.stamina > 0 else 1.0)

def cell_hold_p(battle, defenders=None, board=None):
    """本回合末这一格仍归我方的概率 -> 0~1。"""
    if defenders is None:
        defenders = [g for g in battle.defenders if g.alive and g.pos == battle.cell]
    if not defenders:
        return 0.0
    game = battle.game
    if board is None:
        board = Board(game)
    p, wound = _order_p(game, defenders, battle.atk, battle.cell)
    for foe, q in _next_batch(battle, defenders, board):
        p_next, _ = _order_p(game, defenders, foe, battle.cell, wound=wound)
        p *= (1 - q) + q * p_next
    return p

def zone_p(game, cell, player, board=None, field=None):
    """本回合末 cell 归 player 的概率 -> 0~1（按"两边谁先走到"的赛跑算）。"""
    if field is None:
        field = board.field if board is not None else {
            p: game.distance_field(p) for p in (0, 1)}
    ours, theirs = field[player].get(cell), field[1 - player].get(cell)
    if ours is None and theirs is None:
        return 0.0
    return _race(_turns_mass(None if ours is None else ours / MOVE_POINTS),
                 _turns_mass(None if theirs is None else theirs / MOVE_POINTS))[0]

# === 三、把推演落回局面：读计分板，算出每个选择的分数 ===
# 一次选择的三种结局。**守败和撤退不是一回事**：选择坚守而没守住，是"人死格丢"；主动撤退是
# "保命让格"。把这两个混起来，会让人为一座城池做出无谓的牺牲。
HOLD_OK, HOLD_FAIL, LEAVE = "ok", "fail", "leave"

def _outcome(battle, order, mode, rounds):
    """把一种结局**真的落回局面**，读计分板算我方净分（相对当前局面）。"""
    game, atk, cell = battle.game, battle.atk, battle.cell
    player = order[0].owner
    before = game.scoreboard()[0]
    snap = Snapshot(game)
    try:
        hurt = rounds * average_damage(atk.might)
        for dfd in order:
            left = after_battle_stamina(game, dfd, cell,
                                        defender_stamina(game, dfd, cell) - hurt)
            if mode == HOLD_OK:
                dfd.stamina = max(1, left)        # 守住了 = 活下来了
            else:
                dfd.stamina = left
                if left <= 0:                     # 守败，或撤退路上被追歼
                    dfd.stamina, dfd.alive = 0, False
        if mode == HOLD_OK:
            atk.stamina = max(0, atk.stamina - rounds * average_damage(order[0].might))
            if atk.stamina <= 0:
                atk.alive = False
        else:                                     # 没守住：攻方进占
            if mode == LEAVE:                     # 活着的守军先撤走
                for dfd in order:
                    if not dfd.alive:
                        continue
                    spot = retreat_spot(game, dfd, threat_from=battle.origin, cell=cell)
                    if spot is not None:
                        dfd.row, dfd.col = spot
            if atk.alive:
                atk.row, atk.col = cell
        now = game.scoreboard()[0]
        return (now[player][0] - now[1 - player][0]) \
            - (before[player][0] - before[1 - player][0])
    finally:
        snap.restore()

def _stay_expectation(battle, order, p):
    """选择**接着打**的期望分数 = p × 守成 + (1 − p) × 守败。"""
    game, atk = battle.game, battle.atk
    dfd = order[0]
    rounds = duel_span(defender_stamina(game, dfd, battle.cell), dfd.might,
                       max(1, atk.stamina), atk.might)
    return (p * _outcome(battle, order, HOLD_OK, rounds)
            + (1 - p) * _outcome(battle, order, HOLD_FAIL, rounds))

# === 四、对外接口：顶层 ai.py 在交战屏上调这三个 ===
def ai_pick_defender(battle):
    """守方挑谁先出阵 -> 下标。"""
    game, cell, foe_might = battle.game, battle.cell, battle.atk.might
    top = LOSS_MAX_PER_MIGHT * foe_might          # 一刀最狠能砍多少
    best, best_key = 0, None
    for i, gen in enumerate(battle.defenders):
        stamina = defender_stamina(game, gen, cell)          # 守城 1.2 倍算在体力上
        survive = 1.0 if top <= 0 else min(top, max(0, stamina - 1)) / top
        key = (survive, gen.might * stamina)  # 先保命，再按战斗力算谁最强
        if best_key is None or key > best_key:
            best, best_key = i, key
    return best

def ai_should_hold(battle):
    """守方这一步是接着打，还是撤？-> True 接着打，False 撤。"""
    game, dfd, cell = battle.game, battle.dfd, battle.cell
    if not battle.can_retreat():
        return True                          # 没退路，只能接着打
    atk = battle.atk
    mine = death_risk(defender_stamina(game, dfd, cell), atk.might)   # 我挨他一刀的死险
    theirs = death_risk(atk.stamina, dfd.might)                       # 他挨我一刀的死险
    if mine > theirs:
        return False                         # 我比对方更容易一刀死 -> 优先撤退
    if game.grid[cell[0]][cell[1]] == CITY:
        return True                          # 守城：不做计算，直接接着打
    board = Board(game)
    order = [dfd] + [g for g in battle.defenders
                     if g is not dfd and g.alive and g.pos == cell]
    p = cell_hold_p(battle, order, board)     # 本格此刻的归属概率
    if p >= OWN_DEFEND_TOP:
        return True                          # 本格偏向我方 -> 坚守（磁性战术的"吸住"）
    # 归属概率低 -> 只做必要的消耗。这里才动算盘：留下继续打（含"没守住"的断崖）划不划算，
    # 比不过就退。留个 OWN_HOLD_EDGE 的门槛，免得在临界点上反复横跳。
    stay = _stay_expectation(battle, order, p)
    leave = _outcome(battle, order, LEAVE, 0)
    return stay > leave + OWN_HOLD_EDGE

def ai_pick_retreat(battle):
    """守方往哪撤 -> battle.retreat_choices 里的下标。"""
    game, gen = battle.game, battle.dfd
    board = Board(game)
    best, best_score = 0, None
    for i, (dest, steps) in enumerate(battle.retreat_choices):
        score = -AI_DEBT_WORTH * steps                        # 退得越远，下回合越迈不开腿
        score += AI_CITY_PULL * cell_value(game, dest)        # 城池分高、回血快
        score += OWN_SCALE_WORTH * (zone_p(game, dest, gen.owner, board) - 0.5) \
            * general_value(gen)                              # 落在归属概率高的后方
        # "安全的后方"还得当下就安全：这一步就能追到落脚点、且对拼耗得过我的敌人，一个都不能有。
        for foe in strikers(board, dest, 1 - gen.owner):
            if wins_duel(foe.stamina, foe.might, gen.stamina, gen.might):
                score -= AI_THREAT_WORTH * general_value(gen)
                break
        if best_score is None or score > best_score:
            best, best_score = i, score
    return best
