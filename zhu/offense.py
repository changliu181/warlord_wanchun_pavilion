# -*- coding: utf-8 -*-
"""**进攻回合策略**：这一回合全军怎么走，局面分才最高。

做法是**回合级模拟规划**：一个候选 = 让某名还有移动力的武将走一件**有意义的动作**（强攻 / 走位
到关键落脚点），落在局面**副本**上（`_fork`）；子节点 = 新副本上让别人接着动，于是"甲一步、
乙一步、再甲一步"这种交错走法都在搜索树里；宽搜 + 剪枝取最优，**一次只落实第一步**，走完再重新
规划，直到谁都动不了、或再动也没实质收益。

三步走，顺序就是 `plan_step` 里的顺序：
  1. **吃粮**（`_eat_pick`）：该吃就直接返回那一步，不进搜索（黑白分明的一档）；
  2. **搜索**（走位 + 强攻）：逐层展开、`BEAM` 收窄，挑累计收益最高的那条链；
  3. **落地**：返回 `best` 那条链的**第一层**动作。

依赖单向：tactics.py <- defense.py <- offense.py <- ai.py。
只读 game 的公开状态、不 import pygame，可以脱离图形环境单独测试。
"""

import copy
import random

from config import neighbors
from constants import CITY, CITY_DEFENSE_MULT, GRAIN_HEAL
from .knobs import AI_GRAIN_CARRY, AI_KILL_WORTH
from .tactics import (
    _BFS_FAR, atk_should_hold, battle_seed, child_seed, death_risk,
    defender_stamina, general_value, path_steps, resolve_assault, sigma,
)
from . import defense

# === 旋钮（含义与调法见 README「六、电脑对手」） ===
ADVANCE_W = 1           # 近敌这项整体系数（表里的数已经是"分"，这一项平时就留在 1）
# 近敌分查表（见 `_advance_of`）：强将 / 弱将各一张，前几格由表点死、之后按 tail 等差续下去。
#   强将（体力 ≥ STRONG_GEN）——贴着才值钱，离远了倒扣：
#       gap 1 = 0（贴上去是最优，不奖也不罚）；gap 2~4 = −100,−250,−350；此后每远一格再低 50
#   弱将（体力 ≤ WEAK_GEN）——离得越远越安全，一格的涨幅在收窄：
#       gap 1~4 = 0,200,400,500；此后每远一格再高 20
STRONG_HEAD = (0, -100, -250, -350)
STRONG_TAIL = -50
WEAK_HEAD = (0, 200, 400, 500)
WEAK_TAIL = 20
# 战线分（`_squash` / `line_score` / `_line_delta` 一串）**当前已停用**：`position_eval` 与粗排
# `_quick` 都把它摘掉了（为提效），代码留着备用。这两个旋钮只服务于那一串，恢复战线分时才会再被读到。
LINE_W = 2              # 战线分的倍率（position_eval 与粗排 _quick **必须用同一个**，见那里的注释）
CITY_W = 2              # 据城分的倍率
ATK_SIGMA_EDGE = 400    # 规则 2 的门槛：进攻者的 σ 要比守军 Σσ 多出这么多才敢动手（σ = 武力×体力）
CITY_HOLD_BONUS = 45   # 据城分（一）：每个我方武将站在城池里加多少分，以5点体力×18武力估算，需反除CITY_W
# 终局哨兵分（见 position_eval）：把对方打光 = 赢下整局，比分要远高于任何中间局面（实战里那些项
# 加起来也就几千到几万，所以 1e9 足够压过一切，又小到不会把浮点精度用坏）。反过来我方全灭（只有
# 同归于尽才可能）给负的同一个数，让搜索躲开"打平"。
WIN_BONUS = 1e9
FRONT_STEPS = 3         # "靠近前线"的视野：有敌将走到本格的通路步数 ≤ 这么多。
BEAM = 8                # 除首层外，每层保留几条分支（首层见 BEAM_FIRST）
BEAM_FIRST = None       # **首层**保留几条分支的上限；None = 不设限（首层展开的全进下一层，见 plan_step）
BRANCH = 4              # 除首层外，每个节点最多展开几个候选动作（先粗排，再精算）
BRANCH_FIRST = 6        # **首层**挑几个走位展开：一半按一眼分、一半随机（见 _pick_moves；同种子可复现）
DEPTH = 4               # 搜索树最多往下几层（一层 = 一名武将走一步；宽度已被 FOCUS_R 收窄）
FOCUS_R = 3             # 下探时只让"第一步落点附近这么些格内"的走法接着搜（出发格或落点任一在圈内，见 _focus）
SQUASH_SCALE = 0.922    # 战线分里把"我减他"的差压进 (-1, 1) 用的曲线：n=5 的幂次有理函数，自变量再缩这个倍数。
STRONG_GEN = 0.7       # 体力达到上限这个比例以上，称为"强将"，可以优先近敌
WEAK_GEN = 0.4         # 体力低于上限这个比例以下，称为"弱将"，要尽量远敌


# 搜索追踪：`trace` 非 None 时，`plan_step` 会把每个展开的节点记进去（见 testbench / offense_bench）。
# 平时它是一个空壳（只存一个字典），开销可以忽略——只在想看清"AI 到底搜了些什么"时才填。
class _Trace:
    """一次 `plan_step` 的搜索树流水：每个节点的动作文字、收益、父子关系、层号。"""

    def __init__(self):
        self.nodes = []              # [dict(action, gain, before, delta, depth, parent, best)]

    def add(self, action, gain, delta, depth, parent):
        self.nodes.append({"i": len(self.nodes), "action": action, "gain": gain, "delta": delta,
                           "depth": depth, "parent": parent, "best": False})
        return len(self.nodes) - 1

trace = None                     # 由调用方（测试台）临时挂上一个 _Trace 来收集

# === 一、局面打分：将领分 + 近敌分 + 据城分（终局另给哨兵分） ===
def _live(game, player):
    return [g for g in game.generals if g.alive and g.owner == player]

def _nearest_foe(game, cell, player, foe_field=None):
    """离 cell 格数最近的敌人有多远；没有敌人就是 None。

    `foe_field` 是**敌方的距离场**（`game.distance_field(1 - player)`，格子 -> 几回合走到）。给了就
    直接查——它和"逐个敌人查 `path_steps` 取最小"**完全等价**（到不了时都是那张表的兜底值），
    而搜索里每个节点本来就要算一份（见 `plan_step`），别白算两遍。
    """
    if foe_field is not None:
        # 空场 = 一个敌人都没有 -> 和原来一样返回 None（有敌人、但这格走不到 -> 兜底的大数）
        return foe_field.get(cell, _BFS_FAR) if foe_field else None
    best = None
    for g in game.generals:
        if not g.alive or g.owner == player:
            continue
        gap = path_steps(g.pos, cell)
        if best is None or gap < best:
            best = gap
    return best

def _advance_face(gap, head, tail):
    """近敌这一项的**原值**（还没乘 ADVANCE_W）：离最近的敌人 gap 格 -> 值多少分。

    直接查表：前几格由 `head` 点死，之后按 `tail` 等差续下去（不再有解析曲线）。
    gap 兜到 1（最小距离就是 1 格），保证查表不越界。
    """
    gap = max(1, gap)
    if gap <= len(head):
        return head[gap - 1]
    return head[-1] + tail * (gap - len(head))

def _advance_of(gen, gap):
    """这名武将离敌 gap 格时，近敌这一项记多少分。

    **强将要贴上去、弱将想拉开**是两条反向的意图，所以各有各的表：
      * 强将（体力 ≥ STRONG_GEN）：按 `STRONG_HEAD` / `STRONG_TAIL` 查表（贴着不罚、越远越扣）；
      * 弱将（体力 ≤ WEAK_GEN）：按 `WEAK_HEAD` / `WEAK_TAIL` 查表（越远越值钱，涨幅收窄）；
      * 中段体力（WEAK_GEN < 体力 < STRONG_GEN）：两张表**线性插值**——这样体力一点点变化，
        这一项也跟着一点点挪，不会在某个阈值上跳一下。
    两张表 gap = 1 都是 0，所以体力在"离敌 1 格"上怎么变都不抖（特意保证的性质）。
    """
    strong = _advance_face(gap, STRONG_HEAD, STRONG_TAIL)
    weak = _advance_face(gap, WEAK_HEAD, WEAK_TAIL)
    if gen.stamina >= gen.stamina_cap * STRONG_GEN:
        return _advance_face(gap, STRONG_HEAD, STRONG_TAIL)
    if gen.stamina <= gen.stamina_cap * WEAK_GEN:
        return _advance_face(gap, WEAK_HEAD, WEAK_TAIL)
    # 中段体力：用线性插值
    t = (gen.stamina - gen.stamina_cap * WEAK_GEN) / (gen.stamina_cap * STRONG_GEN - gen.stamina_cap * WEAK_GEN)
    return weak + t * (strong - weak)

def _squash(x):
    """把一个"我减他"的差压进 (-1, 1)：sign(x) · |0.922x|⁵ / (1 + |0.922x|⁵)。

    **当前停用**：只被战线分那一串用，而那一串已从打分里摘掉（见 `_line_score`）。

    和 arctan 一样是**奇函数**（差为 0 就是 0 分，两边对称），也**有界**（一条线再悬殊也不会把
    局面分压过去）；区别只在形状：中间那一段更平（差得不多就几乎不算分），过档更陡（差出量级
    就迅速吃满 1 分）。

    n=5 的这个形式只在 u ≥ 0 上良好（u = −1 处分母为零、且过了它还要翻号），所以负数一侧按
    奇函数取 −f(|x|)：战线分的求和、以及 `_line_delta` 的"两端对调反号"都要求它是奇函数。
    """
    u = abs(SQUASH_SCALE * x) ** 5
    mag = u / (1.0 + u)
    return -mag if x < 0 else mag

def _line_score(game, board, cell_mine, cell_foe):
    """一条战线（我方一格 vs 对方一格）的分 -> 正数 = 这条线对我有利。

    **当前停用**（连同下面 `line_score` / `_line_delta` / `_LineShim` / `_held` 一串，为提效摘掉，
    代码留档备用）。

    战线 = 相邻两格之间的边界线，一边站着我方武将、另一边站着敌方武将。两项其实都在量同一件事
    "这条线上谁更硬"，只是横切面不同：兵力总量、以及"我的刀能不能砍死你的软肋（杀伤机会）"。
    第一项直接把差压进 (-1, 1)（`_squash`），第二项本身就是 0~1 的概率。

    体力一律用**守城加成后**的（`defender_stamina`，城池 ×1.2）：站在城里的武将更难啃，所以这个
    加成不只影响推演，也该直接进战线分的"兵力总量"与"杀伤机会"两项。
    """
    mine = board.garrison.get(cell_mine, ())
    foe = board.garrison.get(cell_foe, ())
    if not mine or not foe:
        return 0.0
    # 一、兵力总量：我方 Σσ − 对方 Σσ，每 400 σ 折一档（σ = 武力 × 加成后的体力）
    bulk = _squash((sigma(game, mine, cell_mine)
                    - sigma(game, foe, cell_foe)) / 400)
    # 二、杀伤机会：我的尖刀一刀能砍死他哪位 -> 值多少分；减掉他的尖刀能砍死我哪位的。
    #     一名武将挨一刀阵亡的概率 = 他的"死险"（见 tactics.death_risk），所以这一项就是
    #     **一刀砍死的概率 × 死者的武力**——砍掉的体力本来就按武力折价（同一把尺子），
    #     被砍死的又是最薄的那位，所以取对方体力最低者。
    kill = (max(death_risk(defender_stamina(game, d, cell_foe), max(g.might for g in mine))
                * d.might for d in foe)
            - max(death_risk(defender_stamina(game, d, cell_mine), max(g.might for g in foe))
                  * d.might for d in mine))
    return 400 * bulk + 15 * kill

def line_score(game, board, player):
    """全线战线分：所有战线的分求和（见 `_line_score`）；player 是哪一方就取哪个正负号。

    **战线只在敌我之间**：两边都是我的格子不算战线。这类边本来也不贡献分（`line_score` 从两端各
    数一次，而 `_line_score` 对调两端反号，两次正好抵消），所以跳过它**不改分数**，只是省掉白算
    的推演、并把口径写明；`_line_delta` 那边靠"只有一端归我"的过滤，跳过的是同一批边。
    """
    total = 0.0
    for cell, gens in board.garrison.items():
        if any(g.owner != player for g in gens):
            continue                                     # 只看我方格，一条线只数一遍
        for nb in neighbors(*cell):
            other = board.garrison.get(nb, ())
            if other and other[0].owner == player:
                continue                                 # 邻格也是我的：不是战线
            total += _line_score(game, board, cell, nb)
    return total

class _LineShim:
    """只带 garrison 的壳：`_line_score` 只读这一样，用不着 `_BoardLite` 的其余账（战线分停用中）。"""
    __slots__ = ("garrison",)

    def __init__(self, garrison):
        self.garrison = garrison

def _line_delta(sim, gen, cell, player):
    """gen 走到 cell 这一步会让**战线分**变多少 —— 给粗排（`_quick`）用。

    **当前停用**（`_quick` 已不调它，见那里的说明 / `_line_score`）。

    真走一步再整盘重算 `line_score` 太贵（那要连位置分和距离场一起算），但战线分只看"谁站在哪
    格"，而走位**只动出发格与落脚格两个桶**，所以只有挨着这两格的边会变。两条性质让这件事很便宜：

    * 一条边被数两遍——`line_score` 从"我方那一格"数它一次、从对面数它一次，而 `_line_score`
      对调两端会**反号**，所以两次相加 = (两端归我不归我之差) × ls(这一端, 另一端)：只有**一端
      归我**的边非零，两端同归或同不归的边整条跳过；
    * 能充当"我方那一端"的只有 src / dst 及其邻格，所以局部 garrison 取 1 环就够（两端都在环外的
      边不可能只有一端归我）。

    于是每条边最多一次 `_line_score`，一趟十来次；不碰 BFS、不碰距离场。
    """
    src, dst = gen.pos, cell
    if src == dst:
        return 0.0
    near = {src, dst} | set(neighbors(*src)) | set(neighbors(*dst))
    before = {}
    for g in sim.generals:
        if g.alive and g.pos in near:
            before.setdefault(g.pos, []).append(g)
    after = dict(before)                              # 只改这两个桶，别的格子照旧
    after[src] = [g for g in before.get(src, ()) if g is not gen]
    after[dst] = before.get(dst, []) + [gen]
    was, now = _LineShim(before), _LineShim(after)
    total = 0.0
    for a, b in {(c, nb) if c < nb else (nb, c) for c in (src, dst) for nb in neighbors(*c)}:
        for board, sign in ((was, -1.0), (now, 1.0)):
            held = ((1 if _held(board, a, player) else 0) - (1 if _held(board, b, player) else 0))
            if held:
                total += sign * held * _line_score(sim, board, a, b)
    return total

def _held(board, cell, player):
    """cell 上站的武将是不是全归 player（战线分只从"纯我方"的格子向外数；停用中）。"""
    gens = board.garrison.get(cell, ())
    return bool(gens) and all(g.owner == player for g in gens)

def _city_near_front(board, cell, player):
    """这座城算不算"靠近前线"：有敌将**走到本格**要 `FRONT_STEPS` 格以内 -> 算。

    距离用**通路步数**（`path_steps`）而不是"隔几格"：这才是"敌人几回合能贴上这座城"的真实距离
    （绕障碍、按 4 邻算）。比原来的曼哈顿半径 2 更准：那时隔着墙、斜角其实走不到的地方也算"前线"
    （如城 (2,4) 西北/东南那两个角，曼哈顿是 2、通路步数却是 4），而真正贴着城的三步内通路格反而
    漏在外面。
    """
    foe = 1 - player
    return any(g.alive and g.owner == foe and path_steps(cell, g.pos) <= FRONT_STEPS
               for gens in board.garrison.values() for g in gens)

def _city_hold_score(game, board, player):
    """据城分 -> 分。两部分：
      1. 每名我方武将站在城池里 +`CITY_HOLD_BONUS`（站在城里本身的好处：回体力、收官快）；
      2. **靠近前线**的城里（见 `_city_near_front`），再加 城中武将 σ ×(CITY_DEFENSE_MULT − 1)——
         守城那 1.2 倍只在前线兑现（后方没人来打，加成拿不到手），所以把"多出来的那一份体力"
         折成分记在这儿。
    """
    total = 0.0
    for gen in _live(game, player):
        if game.grid[gen.row][gen.col] != CITY:
            continue
        total += CITY_HOLD_BONUS
        if _city_near_front(board, gen.pos, player):
            total += gen.might * gen.stamina * (CITY_DEFENSE_MULT - 1)
    return total

def _city_hold_delta(sim, board, gen, cell, player):
    """gen 走到 cell 这一步让**据城分**变多少 -> 分（供粗排 `_quick` 用）。

    据城分只看"谁站在城里"和"那座城 `FRONT_STEPS` 格内（通路）有没有敌人"，
    走位只动出发格与落脚格两处，所以照它差分只看这两格。**gen 自己不会改变任何一格的前线判定**——
    前线按**敌人**在不在算（`foe = 1 - player`），而 gen 是我方的人，所以两处都能直接用现成的
    `board.garrison`，不必重算。
    """
    if cell == gen.pos:
        return 0.0
    sigma_mine = gen.might * gen.stamina * (CITY_DEFENSE_MULT - 1)
    total = 0.0
    if sim.grid[cell[0]][cell[1]] == CITY:             # 落脚格：进城
        total += CITY_HOLD_BONUS
        if _city_near_front(board, cell, player):
            total += sigma_mine                        # 城里这一份加成，跟着人走到落脚格
    if sim.grid[gen.pos[0]][gen.pos[1]] == CITY:       # 出发格：出城
        total -= CITY_HOLD_BONUS
        if _city_near_front(board, gen.pos, player):
            total -= sigma_mine
    return total

def _advance_score(game, player, foe_field=None):
    """近敌分 -> 分：强将朝最近的敌人压过去的推力，弱将要尽量远敌。

    每名我方武将按"离最近的敌人几格"查一张表记一笔（表的形状见 `_advance_of`）。调用方保证此刻
    还有敌人（见 `position_eval`）。中间体力段（既不强也不弱）这一项记 0，两支的账都由各自的表
    直接给，gap = 1 都是 0——所以体力的变化本身不会在"离敌人 1 格"上把分数抖出来。

    `foe_field` 是**敌方的距离场**：搜索里外层已经算好了（见 `plan_step` / `_Branch.foe_field`），
    传进来就不必每算一次分都重扫一遍地图。**没传才现算**，保证单独调用时也对。
    """
    if foe_field is None:
        foe_field = game.distance_field(1 - player)
    total = 0.0
    for gen in _live(game, player):
        gap = _nearest_foe(game, gen.pos, player, foe_field)
        if gap is not None:
            total += _advance_of(gen, gap)
    return total

def position_eval(game, player, board=None, foe_field=None):
    """局面打分：将领分 + 近敌分 + 据城分（各项量的是不同的东西）。

    **终局直接给哨兵分**（`WIN_BONUS`）：把对方最后一名武将打死 = 赢下整局，这时的分数要**远高于**
    任何中间局面。这一条非有不可——中间那几项都是"此刻的兵力 / 地盘"，人打光了就全没了（近敌分
    都没了对象，将领分也归零），所以一仗打赢反而会算出一个**很大的负数**，搜索于是躲着胜利
    走。反过来我方全灭（只有同归于尽才可能）给负的同一个数，让搜索躲开打平。
    """
    if not any(g.alive and g.owner == player for g in game.generals):
        return -WIN_BONUS                            # 我方全灭（含同归于尽）：这局没得打
    if not any(g.alive and g.owner != player for g in game.generals):
        return WIN_BONUS                             # 对方全灭：直接赢下整局
    if board is None:
        board = _BoardLite(game)
    # 将领分（计分板那一笔的**将领**那一半；地盘分按"完全取消地盘分"拿掉了）。按**两边之差**计：
    # 打死/打残敌将 -> 他那边掉分、我这边涨；自己掉血/阵亡 -> 我这边掉分。它和另外三项有个要紧的
    # 分别：**不受走位影响**，只由交战结果决定——所以粗排 `_quick` 的走位那一支不用管它（纯走位
    # 改不动这一项），只有强攻那一支会动到它（走位的那条路见 `_quick`）。
    # 战线分（`LINE_W * line_score(...)`）原本是第三项，为提效摘掉了；`_line_delta` /
    # `_line_score` 那一串代码仍在文件里留档，要用时把这一行加回来即可。
    return (game.general_score(player) - game.general_score(1 - player)
            + ADVANCE_W * _advance_score(game, player, foe_field)
            + CITY_W * _city_hold_score(game, board, player))

# === 二、局面的副本：模拟在副本上做，真局面一个字节都不动 ===
class _BoardLite:
    """搜索用的那一小块局面缓存：**只有**"格子 -> 站着谁"。

    就一件事——把 `garrison` 建好，供 `_city_near_front`（"这座城 `FRONT_STEPS` 格内有没有敌人"）
    与 `_city_hold_score` / `_city_hold_delta` 按格取人。武将的 reach、双方的距离场都**不在这里**：
    搜索里需要时现算（距离场见 `plan_step` / `_Branch.foe_field`）。
    """

    __slots__ = ("game", "garrison")

    def __init__(self, game):
        self.game = game
        self.garrison = {}                   # 格子 -> [活着的武将]
        for gen in game.generals:
            if gen.alive:
                self.garrison.setdefault(gen.pos, []).append(gen)

def _fork(game, gens):
    """从 game 复制一个局面：新的武将列表（全是副本）+ 一份粮草副本。"""
    sim = copy.copy(game)
    sim.generals = [copy.copy(g) for g in gens]
    sim.grain = dict(game.grain)
    return sim

# === 三、候选动作：落在副本上，不动战报、不动界面 ===
def _blow_gate(game, gen, cell):
    """规则 1：面对**任何一位**守将，只要"我第一刀更容易死"就别发起这次进攻。

    守军最多两名、谁先上场选择权在对方，所以**每一位**都得过这个准则(不能只看最硬的那位)。
    """
    return any(death_risk(gen.stamina, dfd.might)
               > death_risk(defender_stamina(game, dfd, cell), gen.might)
               for dfd in game.generals_at(*cell) if dfd.owner != gen.owner)

def _moves(sim, index, player):
    """sim 里 index 号武将现在能做的候选动作 -> [(类型, 目标格, 附带信息)]。"""
    gen = sim.generals[index]
    out = []
    for cell in sim.compute_attackable(gen):
        # 规则 1：这一刀我更可能先倒，不打
        if _blow_gate(sim, gen, cell):
            continue
        # 规则 2：只剩 1 点移动力（打完就撤不了），要"胜算明显"才启动——用 σ = 武力×体力 比大小：
        # 我的 σ 得 **大于守军 Σσ + ATK_SIGMA_EDGE**（这一格上所有守将的 σ 之和，城池加成在内）。
        # 这是"获胜概率较大"的一个简化算法（胜率那套要铺分布，这里只比一条线）；多于 1 点移动力时
        # 不受这条约束（还能撤）。
        if gen.move_points <= 1:
            defenders = [g for g in sim.generals_at(*cell) if g.owner != gen.owner]
            if gen.might * gen.stamina <= sigma(sim, defenders, cell) + ATK_SIGMA_EDGE:
                continue
        out.append(("attack", cell, None))
    for cell, steps in sim.compute_reachable(gen).items():
        if cell != gen.pos:
            out.append(("move", cell, steps))
    # 这里**不产出 "eat"**：吃粮不进下游搜索，它由 `_eat_pick` 在 `plan_step` 里单独处理
    # （见那里的说明）。所以搜索的候选只有走位与强攻两类。
    return out

def _quick(sim, gen, move, player, board=None, foe_field=None):
    """粗排用的**一眼分**：算得便宜，好让"值得精算的"走位排到前面来。

    **只有走位读这个分**。`plan_step` 里强攻是**全量展开**的（原样交给 `_expand`，既不排序也不
    截断），它的 `_quick` 值算出来就扔；吃粮根本不进搜索（由 `_eat_pick` 单独定，见那里）。真正
    按它取前 `BRANCH` 名的只有走位。所以这里不必、也不该给强攻另拍一套"收益"，只管把**走位**算对。

    走位的一眼分 = `position_eval` 那几项里**走位改得动**的那几项的差分：
      * **将领分**：纯走位改不动（位置不进那个公式），跳过；
      * **近敌分**：查 `_advance_of`（认体力段），走前走后各一次，差就是它；
      * **据城分**：`_city_hold_delta`。
    这两项都是"只动出发格与落脚格"就能算清的局部量，所以差分很便宜。据城分不能省——少了它
    粗排就只按近敌那一项挑人，一些值钱的走位反而进不了 `BRANCH`。
    （**战线分**的 `_line_delta` 本来是第三项，为提效摘掉了；见 `_line_score`。）

    `foe_field` 是**敌方的距离场**（`plan_step` 每节点传进来）：给了就直接查表得到"离最近的敌人
    几格"，不必再逐个敌人扫一遍（两者等价，见 `_nearest_foe`）。
    """
    kind, cell, info = move
    score = 0.0
    if board is None:
        board = _BoardLite(sim)                      # 没人传就给一份（据城分要问"谁站在哪"）
    here = _nearest_foe(sim, gen.pos, player, foe_field)
    there = _nearest_foe(sim, cell, player, foe_field)
    # 和 position_eval 同一个函数（`_advance_of` 认的是武将的体力段）：走这一步前后各查一次表，
    # 差就是这一步的近敌收益。不能退回"从 1 格起算的线性式"——两张表都不是线性的。
    if here is not None and there is not None:
        score += _advance_of(gen, there) - _advance_of(gen, here)
    # 战线分那一项（`LINE_W * _line_delta(...)`）已摘掉，理由同上（见 `_line_score`）。
    score += CITY_W * _city_hold_delta(sim, board, gen, cell, player)
    return score

# === 四、模拟规划：宽搜 + 剪枝，挑本回合该走的下一步 ===
class _Branch:
    __slots__ = ("sim", "real", "gain", "before", "seed", "first", "anchor", "tid", "last",
                 "last_gain", "kind", "root1", "foe_field")

    def __init__(self, sim, real, gain, before, seed, first=None, anchor=None, tid=None):
        self.sim = sim            # 这个分支的局面副本
        self.real = real          # 真武将列表（与 sim.generals 同下标）
        self.gain = gain          # 累计收益
        self.before = before      # 本节点位置分
        self.seed = seed          # 这个分支的战斗随机种子（随机推演用）
        self.first = first        # 这个分支的第一步（真武将, 类型, 格, 信息）
        self.anchor = anchor      # 下探聚焦的锚点：**第一步的落点**，整条链固定不动
        self.tid = tid            # 追踪用：这个节点在 trace.nodes 里的下标（不追踪时是 None）
        self.last = None          # 追踪用：这个节点**这一步**走的是什么动作
        self.last_gain = 0.0      # 追踪用：这一步本身赚了多少分（不是累计）
        self.kind = None          # 这一步的类型（move / attack）——首层按类型分名额时要用
        self.root1 = None         # 这条链**第一层**的那个节点（保证每个首层节点都有活口，见 _narrow）
        # 这个节点局面上**敌方的距离场**（格子 -> 几步走到）。子节点一般**直接继承**父节点的这一份：
        # 我方走位 / 吃粮都改不动敌方的多源 BFS，"离敌人几格"自然也不变；**只有打死敌将**才要重算
        # （少了一个源）。见 `_expand` 里的 `child_field`。
        self.foe_field = None

def _focus(br, gen, cell):
    """这个节点还该不该让 gen 走这一格（组合剪枝，见 FOCUS_R）。

    一步棋的价值几乎都来自**局部组合**——抱团、合围、围歼都是同一片战场上的两三个人配合出来的；
    甲在城北打、乙在城南跑，两件事彼此无关，合起来搜只是在替不相干的组合穷举，白烧算力。

    锚点是**第一步的落点**（`br.anchor`），整条链**固定不变**；判定是"出发格或落点格，**任一**
    落在锚点 FOCUS_R 格内就放行"。两条合起来的意思正是"整条链（`DEPTH` 层）都在第一层那步附近"：

    * 锚点固定（而不是"父节点刚动过的人"，那会每跳往前滑，几跳能滑出半张盘）；
    * 放行看**两个端点里近的那个**，所以既不挡"远处的人走过来汇合"（落点近），也不挡"附近的人
      朝外扩张一步"（出发格近）；只有两个端点都跑出圈外的走法才剪掉。

    第一步自己（`anchor is None`）不受限：根节点还没人动过，谁都可以先动。
    """
    if br.anchor is None:
        return True                                   # 根节点：第一步，谁都可以先动
    return min(path_steps(gen.pos, br.anchor), path_steps(cell, br.anchor)) <= FOCUS_R

def _do(sim, gen, kind, cell, info, rng_seed):
    """在副本上真的走一步：走位 / 吃粮 / 强攻（复刻 Game 的效果，但不写战报、不动界面）。

    强攻把这一仗**被打死的守将**回给调用方（其余动作回空元组）——`_expand` 要拿他补一笔歼灭分。

    `"eat"` 这一支搜索里其实用不到（吃粮由 `_eat_pick` 在 `plan_step` 开头就定掉、不进搜索树），
    留着是为了和 `Game` 的三类动作对齐、也让这个函数自成一份"一步怎么走"的完整说明。
    """
    if kind == "move":
        carry = 0
        if sim.grid[cell[0]][cell[1]] == CITY or (
                gen.stamina < gen.stamina_cap * 0.8 and sim.grain_at(*gen.pos)):
            carry = min(sim.grain_at(*gen.pos), AI_GRAIN_CARRY)
        picked = sim.take_grain(gen.row, gen.col, carry) if carry > 0 else 0
        gen.row, gen.col = cell
        gen.move_points = max(0, gen.move_points - info)
        if picked:
            sim.add_grain(cell[0], cell[1], picked)
    elif kind == "eat":
        sim.take_grain(gen.row, gen.col, info)
        gen.move_points = max(0, gen.move_points - info)
        gen.add_stamina(info * GRAIN_HEAL)
    else:
        gen.move_points = max(0, gen.move_points - 1)
        kills, _driven, _taken, _died, _rounds = resolve_assault(
            sim, gen, cell, random.Random(rng_seed),
            atk_should_hold, defense.ai_pick_defender)
        return kills
    return ()

def _expand(br, index, move, player):
    """在 br 的副本上走 index 号武将的 move，回一个新分支（第一步沿用 br.first）。

    锚点只在这里定一次：根节点（`br.anchor is None`）走完这一步，就把**这一步的落点**锁成整条链的
    锚点，往下照抄不改——所以"整条链都在第一步附近"看的始终是同一个点，不会每跳前移。落点取
    `cell`（这一步的目标格）而不是 `gen.pos`：强攻在 `_do` 里并不把攻方挪进格（那是守方死战、只
    掉血），`gen.pos` 还是出发格，而这一步真正"踩上去"的是 `cell`。
    """
    sim = _fork(br.sim, br.sim.generals)
    gen = sim.generals[index]
    kind, cell, info = move
    seed = child_seed(br.seed, gen, cell)             # 子节点的战斗随机种子（和"动的是谁"绑定）
    before_gain = br.gain
    kills = _do(sim, gen, kind, cell, info, seed)
    # 敌方距离场：**继承父节点的**——我方走位 / 吃粮改不动它；只有打死敌将才少一个源、要重算。
    child_field = br.foe_field
    if kills:
        child_field = sim.distance_field(1 - player)
    after = position_eval(sim, player, foe_field=child_field)
    # 每一步的收益**一律**取局面分的差分（走位 / 吃粮 / 强攻同一口径）。不再给强攻单拍一套
    # "削弱 − 自损 + 进占 − 阵亡"的账：那套账只算这一步本身的得失，而强攻真正值钱的地方在于
    # **打完之后的局面**（逼退守将、打开通路、歼灭），那些本来就在 `position_eval` 里。
    # 分开算会把同一笔收益记两遍，也会让"首层排序的一眼分"和"真实收益"各说各话。
    gain = br.gain + (after - br.before)
    # 歼灭再补一笔放大（只在强攻这一支，且确实打死了人才加）：位置分里"阵亡"只记成一名武将的
    # 分**归零**（此刻体力已 0，只掉了那 100 底分），可是**人没了**是断崖——他从此不再赚分、
    # 不再守土、也不再有威胁，这笔长远价值远不止账面，所以按 AI_KILL_WORTH 放大。
    if kind == "attack" and kills:
        gain += (AI_KILL_WORTH - 1) * sum(general_value(g) + 1000 for g in kills)
    br2 = _Branch(sim, br.real, gain, after, seed,
                  br.first or (br.real[index], kind, cell, info),
                  br.anchor or cell)                  # 锚点：第一步的落点，之后固定
    br2.last = (br.real[index], kind, cell, info)     # 这一步**本身**的动作（追踪显示用）
    br2.last_gain = gain - before_gain
    br2.kind = kind
    br2.root1 = br.root1 or br2                       # 首层的孩子就是自己的 root1，往下一路照抄
    br2.foe_field = child_field
    return br2

def step_text(game, step):
    """把 offense 的一个 step 译成人能读的一句（供追踪 / 测试台显示）。"""
    if step is None:
        return "（不动了，收工交回合）"
    gen, kind, cell, info = step
    where = game.describe(*cell)
    if kind == "attack":
        foes = [g.name for g in game.generals_at(*cell) if g.owner != gen.owner]
        return f"{gen.name} 进攻 {where}（守军：{'、'.join(foes) or '无'}）"
    if kind == "eat":
        return f"{gen.name} 吃粮 {info} 石（{where}）"
    return f"{gen.name} 走到 {where}"

def _pick_moves(rest, k, seed):
    """首层挑走位：**一半按一眼分取前几名，一半随机抽**（合起来 k 个）。

    为什么不能全按分数：首层这一层的活儿是**决定第一步走什么**，而这跟"这一步本身赚多少分"关系
    不大——很多第一步一眼分很低（甚至为负）的走位，往下搜几层反而能攒到最高分，它们自己不起眼，
    靠后面几层的配合才出价值。全按分数取前几名，等于在第一层就把这些"慢热"的走位一刀切掉。

    为什么也不全随机：一眼分毕竟还是**有点信息**的（高分里好走位的比例明显更高），全随机等于把
    这份信息也扔了。所以一半一半：**确定的那一半保底**（好走位一定进得来），**随机的那一半拓宽**
    （检出低分里的"慢热货"）。

    抽签用**局面派生的种子**（和战斗推演同一套算法），所以同一 seed 整局照样逐字节可复现。
    `rest` 进来时已按一眼分从高到低排好（调用方排过），所以直接切前一半即可。
    """
    if len(rest) <= k:
        return rest
    half = k // 2
    best = rest[:half]                                # 前一半：按一眼分
    pool = rest[half:]                                # 后一半：从**剩下的人**里随机抽，
    return best + random.Random(seed ^ 0xA5A5A5A5).sample(pool, k - half)   # 所以不会抽到重复的

def _narrow(nodes, beam, keep_all=()):
    """把这一层展开的节点收成下一层的 frontier：按累计收益留前 `beam` 条，外加两条例外。

      * **`keep_all` 里的类型不占名额**（首层的强攻）：它是达成第一目标的唯一手段，数量又很少，
        一律留下——否则会被一堆走位按分数挤掉；
      * **每个首层节点至少留一条**（看 `root1`）：不然那一手首步就白搜了。首层的活儿就是选第一步，
        一条链都不剩，等于当初不该展开它。这一条只在**第二层及以后**生效——首层节点的 `root1` 就是
        它自己（见 `_expand`），不会被它撑成全留。

    保底可能让结果超过 `beam`，那是应该的：保底比名额重要。`beam=None` 表示不设名额（首层用，
    见 `plan_step`）。
    """
    nodes.sort(key=lambda b: -b.gain)
    keep, taken = [], set()
    for br in nodes:                                  # 一、keep_all 的类型：一并留，且**不占名额**
        if br.kind in keep_all:
            keep.append(br)
            taken.add(id(br))
    quota = 0                                         # 名额只数"非 keep_all"的那些
    for br in nodes:                                  # 二、其余按分数补到 beam 条
        if beam is not None and quota >= beam:
            break
        if id(br) not in taken:
            keep.append(br)
            taken.add(id(br))
            quota += 1
    have = {br.root1 for br in keep}                  # 三、每个首层节点保底一条（nodes 已按分排序，
    for br in nodes:                                  #     补进来的是该组里最好的那条）
        if (br.root1 is not None and br.root1 is not br
                and br.root1 not in have and id(br) not in taken):
            keep.append(br)
            taken.add(id(br))
            have.add(br.root1)
    return keep

def _eat_pick(game, player):
    """这一回合要不要吃粮 -> 那一步 (武将, "eat", 格, 石数)；不吃就是 None。

    吃粮是**独立的一档**（在 `plan_step` 里先于搜索处理，且一步只吃一次）：**进不进得了这一档
    是黑白分明的**——吃完"补到上限"就**不吃**（`add_stamina` 会把体力夹回上限，吃下去多出来那
    部分纯属浪费），吃完仍**补不满**才吃；进了这一档就**不再把"吃粮"当候选丢进搜索**（搜索的候选
    只有走位与强攻两类），也不去和走位 / 强攻比分数。

    几石那一口：**从大吃起，挑第一个"吃完仍补不满"的档**（不是无脑取 `eat_amounts` 的最大值——
    那一档可能把体力顶过上限、被 `add_stamina` 夹回去，既浪费粮又把这位武将整个挡在门外）。

    多名武将同时够格时，**按身价高的先吃**——贵的将补回来的血更值钱（将领分里体力直接乘武力）。
    """
    eaters = []
    for gen in _live(game, player):
        if gen.exhausted:
            continue
        amounts = game.eat_amounts(gen)               # 空 = 这格没粮 / 没行动力 / 已动完
        if not amounts:
            continue
        # 该吃几石：**从大吃起，挑第一个"吃完还补不满"的档**。不能只看 max(amounts)：体力 88/100、
        # 行动力和粮都够时 max 会到 2~3，可一口 2~3 石会把体力顶过上限（被夹回 100）——浪费粮食
        # 不说，还会因为"吃完已满"把这位武将整个挡在门外，其实他吃 1 石（88→98）正合适。
        top = next((a for a in sorted(amounts, reverse=True)
                    if gen.stamina + a * GRAIN_HEAL < gen.stamina_cap), None)
        if top is None:
            continue                                  # 哪一档都会吃满：不浪费，交给搜索
        eaters.append((general_value(gen), gen, top))
    if not eaters:
        return None
    # 只按身价比大小。两个身价完全打平时不能接着比 `General`（它没有大小关系，会抛 TypeError），
    # 那种情况先后无所谓，取谁都可以。
    _worth, gen, top = max(eaters, key=lambda item: item[0])
    return (gen, "eat", gen.pos, top)

def plan_step(game, player):
    """规划这一回合 -> 第一步该执行的动作；不值得动就返回 None。

    **先看这一回合该不该吃粮**（见 `_eat_pick`）：该吃就返回那一步，**不进搜索**。吃粮是独立的一档，
    只有"吃 / 不吃"两种结果，不吃也不留什么"剩余的吃粮候选"进搜索。

    搜索的候选**只有走位与强攻**。逐层展开，每层按 `BEAM` 收窄（首层用 `BEAM_FIRST`）。首层还有
    两点不同：

      * 走位挑 `BRANCH_FIRST` 个，**一半按一眼分取前几名、一半随机抽**（理由见 `_pick_moves`）；
      * 强攻全量展开，且**不占 BEAM 名额**，一定进得了下一层（见 `_narrow`）；
      * 首层展开的**全部**进第二层，不再按分砍一刀——随机抽出来的低分首步，正是要靠往下几层才
        显出价值，留在首层按分筛等于把随机采样白做。所以 `BEAM_FIRST` 默认为 `None`（不设上限）；
        想给首层也设个上限，把 `BEAM_FIRST` 设成数字即可。
    """
    eat = _eat_pick(game, player)
    if eat is not None:
        return eat
    sim0, real0 = _fork(game, game.generals), list(game.generals)
    board0 = _BoardLite(sim0)
    seed0 = battle_seed(game)
    root_field = sim0.distance_field(1 - player)      # 全树共用一份（打死敌将时才重算，见 _expand）
    root = _Branch(sim0, real0, 0.0,
                   position_eval(sim0, player, board0, foe_field=root_field), seed0)
    root.foe_field = root_field
    if trace is not None:                              # 追踪：根节点也记一笔
        root.tid = trace.add("（根局面，尚未动作）", 0.0, 0.0, 0, None)
    best, frontier = root, [root]
    for _depth in range(DEPTH):
        first_layer = (_depth == 0)
        nxt = []
        for br in frontier:
            # 敌方距离场用这个节点自己那一份（父节点传下来的，见 `_expand`）——我方走位改不动它，
            # 所以整棵子树通常**共用同一个 dict**，一次 BFS 都不重复做。
            foe_field = br.foe_field
            board = _BoardLite(br.sim)
            atk, other = [], []
            for index in range(len(br.real)):
                gen = br.sim.generals[index]
                if not gen.alive or gen.exhausted or gen.owner != player:
                    continue                              # 只规划我方武将
                for move in _moves(br.sim, index, player):
                    if not _focus(br, gen, move[1]):
                        continue                          # 出发格与落点都离锚点太远：这个走法剪掉
                    item = (_quick(br.sim, gen, move, player, board, foe_field), index, move)
                    (atk if move[0] == "attack" else other).append(item)
            # 强攻一律精算（它是达成第一目标的唯一手段，且数量很少）；走位取前几名——**首层
            # 例外，改一半按分一半随机**（见 _pick_moves：一眼分低的走位常常靠后面几层配合才出价值）。
            # 吃粮不在这里：它已经在 `_eat_pick` 那一档定过了（见那里的说明）。
            other.sort(key=lambda item: -item[0])
            picked = (_pick_moves(other, BRANCH_FIRST, seed0) if first_layer
                      else other[:BRANCH])
            for _q, index, move in atk + picked:
                br2 = _expand(br, index, move, player)
                if trace is not None:
                    br2.tid = trace.add(step_text(br2.sim, br2.last), br2.gain, br2.last_gain,
                                        _depth + 1, br.tid)
                nxt.append(br2)
        if not nxt:
            break
        nxt.sort(key=lambda b: -b.gain)
        if nxt[0].gain > best.gain:
            best = nxt[0]
        frontier = _narrow(nxt, BEAM_FIRST if first_layer else BEAM,
                           keep_all=("attack",) if first_layer else ())
    if trace is not None and best.tid is not None:
        trace.nodes[best.tid]["best"] = True           # 标出最后选中的那一条
    return best.first
