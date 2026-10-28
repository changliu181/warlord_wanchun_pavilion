# -*- coding: utf-8 -*-
"""战术推演的原语：交战屏那一摊子共用的底层模型。

只放"一格归谁"这类**不带倾向**的东西；倾向（守还是撤、打还是不打）在 defense.py 与
offense.py 里。依赖单向：tactics.py <- defense.py <- offense.py <- ai.py。只读 game / battle 的公开状态、
不 import 它们，所以不依赖 pygame。

**本模块有什么**（按用途分堆）：
  * 距离：`path_steps`（通路步数查表）、`_BFS_FAR`；
  * 分数与单挑：`general_value` / `cell_value`、`average_damage` / `duel_race` / `wins_duel` /
    `death_risk`、`defender_stamina` / `after_battle_stamina`、`sigma`；
  * 退路：`_routes` / `retreat_spot`；
  * 一仗的推演：`SimBattle` / `_simulate` / `plan_assault` / `resolve_assault` / `Assault`；
  * 局面缓存：`Board`（全算）、`can_strike` / `strikers`；
  * 攻方的"继续还是收手"：`atk_should_hold`（真实对局与搜索**共用**这一套）。

**一仗到底怎么算**（`plan_assault` / `resolve_assault`）是**逐回合随机推演**：
  * 每回合双方各掷一个 [1, 2×对方武力] 的伤害（和真实 `Battle.roll` 同一套）；
  * 守方**永远死战不退**；谁先倒下就是谁，同时倒算同归于尽；
  * 守将一位一位上，顺序走"守方自己那套点将"（`pick`）——**歼灭入口**就在"把他打倒"这一刻；
  * **攻方每个回合开头问一句自己那套 `atk_should_hold`**：说不打了就整场撤退（不占格、也不接着打
    后面的守将）。所以搜索里的撤退时机 = 真实对局里攻方的判断，不再是另拍一套。

因为伤害是掷出来的，一仗推出来的局面是**随机的那一种可能**；但随机数由局面派生（见 `battle_seed`），
同一局面每次都推演到同一个结果，搜索照样可复现。
"""

import math
from collections import deque, namedtuple

from config import neighbors, parse_map
from constants import (
    ATK_RETREAT_COST, CITY, CITY_DEFENSE_MULT, GRID, LOSS_MAX_PER_MIGHT, MAP_FILE,
    OBSTACLE, P1, P2, RETREAT_MAX, SCORE_GENERAL_BASE, SCORE_GRAIN, SCORE_TERRAIN,
)

PLAYERS = (P1, P2)
DIRS = ((-1, 0), (1, 0), (0, -1), (0, 1))

# 两格之间的**通路步数**（4 邻 BFS 绕障碍，和 `Game.compute_reachable` / `distance_field` 同一套走法）。
# 地图固定，所以整张表**只算一次**（每格各做一趟 BFS，10×10 也就五十几个通路格），之后纯查表——
# O(1)。表是**懒建**的（`_bfs_distances` 第一次被问时才读 map.txt），好处是纯逻辑测试不 import 也不吃亏。
_BFS_MAP = None
_BFS_FAR = GRID * GRID      # 兜底值：起终点不连通（或表里没有）时用。比任何真实距离都大，
                            # 所以"够不够得着""离多远"这类比较一律会把它判成"太远"。

def _bfs_distances():
    """{起点格: {终点格: 通路步数}}；只含通路/城池格（障碍格不参与）。"""
    global _BFS_MAP
    if _BFS_MAP is None:
        grid, _places = parse_map(MAP_FILE)
        walkable = [(r, c) for r in range(GRID) for c in range(GRID)
                    if grid[r][c] != OBSTACLE]
        table = {}
        for src in walkable:
            dist = {src: 0}
            q = deque([src])
            while q:
                r, c = q.popleft()
                for nb in neighbors(r, c):
                    if nb in dist or grid[nb[0]][nb[1]] == OBSTACLE:
                        continue
                    dist[nb] = dist[(r, c)] + 1
                    q.append(nb)
            table[src] = dist
        _BFS_MAP = table
    return _BFS_MAP

def path_steps(a, b):
    """两格之间的**通路步数**——走几步能从 a 到 b（4 邻、绕障碍）。

    这是"离得远不远 / 够不够得着"该看的那把尺子，不是切比雪夫（`max(|Δ行|,|Δ列|)`——那个把斜着
    走也算一步，跟"实际走几步"差得远）：本图上 (0,9) 到 (9,0) 切比雪夫是 9、通路步数是 18。地图
    固定，所以查一张预先算好的表，仍是 O(1)。

    a / b 得是通路或城池格（要问的那些地方都满足：武将只会站在可通行格上）。
    """
    return _bfs_distances().get(a, {}).get(b, _BFS_FAR)


# 同一局面进同一个搜索节点，允许因为随机数不同而走出不同的结果。搜索用的是**专用的一股**随机数
# （绝不碰 game.rng），种子由真实对局的种子派生——所以同一 seed 整局逐字节可复现，而搜索里每次掷骰
# 又确实带随机性。传的是**种子整数**：同一节点"先推演、后落实"两次都能各建一个 `random.Random`
# 而复现同一串骰子，所以推演出来的局面就是真落实进去的那个。
def battle_seed(game):
    """从真实对局的种子派生搜索用的战斗随机种子；同一 seed、同一回合可复现。"""
    return (game.seed or 0) * 1000003 + game.turn * 1009

def child_seed(parent, gen, cell):
    """子节点的战斗随机种子：把"动的是谁、动到哪"揉进父种子（用固定算法，不依赖 hash 的随机盐）。"""
    return (parent * 6364136223846793005
            + ((gen.row * 131 + gen.col) * 131 + cell[0]) * 131 + cell[1]) & 0xFFFFFFFFFFFFFFFF

# === 分数 ===
def general_value(gen):
    """一名武将的身价 / 打死他能让对方掉多少分：(100 + 体力) × 武力。"""
    return (SCORE_GENERAL_BASE + max(0, gen.stamina)) * gen.might

def cell_value(game, cell):
    """一格地值多少分：地形分（通路 200 / 城池 3000）+ 格上粮草每石 150。"""
    return SCORE_TERRAIN[game.grid[cell[0]][cell[1]]] + SCORE_GRAIN * game.grain_at(*cell)

# === 单挑的解析推演（期望值口径，只给"该不该动"的门槛用） ===
# `death_risk` / `wins_duel` / `average_damage` 都按**期望值**算"下一刀会不会死""死战拼不拼得过"。
# 它们只用在粗判的地方（`_blow_gate` 规则一、`_moves` 规则二、defense 里那几个闸门）——这些是
# "该不该动"的闸门，不该拿一仗的随机结果去赌；真正的一仗由 `resolve_assault` 掷骰推演（见上）。
def average_damage(might):
    """每回合平均打掉对方多少体力：[1, LOSS_MAX_PER_MIGHT × might] 的均值。"""
    return 0.5 + LOSS_MAX_PER_MIGHT * might / 2

def duel_race(my_stamina, my_might, foe_stamina, foe_might):
    """双方死战不退的赛跑 -> (我几回合打光他, 他几回合打光我)。小者赢，打平是同归于尽。"""
    return (max(1, math.ceil(foe_stamina / average_damage(my_might))),
            max(1, math.ceil(my_stamina / average_damage(foe_might))))

def wins_duel(my_stamina, my_might, foe_stamina, foe_might):
    """双方死战不退的话，我是不是先把他打倒（同时倒算没赢）。"""
    mine, theirs = duel_race(my_stamina, my_might, foe_stamina, foe_might)
    return mine < theirs

def death_risk(stamina, foe_might):
    """挨对方**下一刀**阵亡的概率（0~1）。"""
    if stamina <= 0:
        return 1.0
    top = LOSS_MAX_PER_MIGHT * foe_might
    return 0.0 if top <= 0 else (top - min(top, stamina - 1)) / top

def defender_stamina(game, gen, cell):
    """守城加成：守军在城池里开打，体力先 ×CITY_DEFENSE_MULT（四舍五入）。"""
    if game.grid[cell[0]][cell[1]] == CITY and not gen.city_boost:
        return int(math.floor(gen.stamina * CITY_DEFENSE_MULT + 0.5))
    return gen.stamina

def after_battle_stamina(game, gen, cell, stamina):
    """打完把守城加成压回去（和 drop_city_boost 同一套取整），夹回上限。"""
    if game.grid[cell[0]][cell[1]] == CITY:
        stamina = int(math.floor(stamina / CITY_DEFENSE_MULT + 0.5))
    return max(0, min(gen.max_stamina, stamina))

def sigma(game, gens, cell):
    """一组武将**站在 cell 上**的战斗力之和 Σ(武力 × 加成后的体力)。

    单回合掉血 ∝ 对方武力、能挨的回合数 ∝ 体力，所以一名武将"能换掉的血量"正比于 武力×体力——
    这个乘积就是他的战斗力。体力取**守城加成后**的（`defender_stamina`，城池 ×1.2）：这一组是
    **驻在 cell 上**的那一方，守城加成该算给他们（规则二比守军，就是这个口径）。

    攻方不站在这一格上，他们的战斗力**不走加成**（直接 `武力×体力`，见 `_take_cell_prob`）。
    """
    return sum(g.might * defender_stamina(game, g, cell) for g in gens)

# === 退路 ===
def _routes(game, gen, threat_from, cell):
    """gen 被 threat_from 那一格上的敌人逼住时，能退到哪几格 -> {落脚格: 步数}。"""
    cell = gen.pos if cell is None else cell
    limit = min(RETREAT_MAX, gen.retreat_room)
    if limit <= 0:
        return {}
    came = ((threat_from[0] > cell[0]) - (threat_from[0] < cell[0]),
            (threat_from[1] > cell[1]) - (threat_from[1] < cell[1]))
    seen, found = {cell}, {}
    q = deque([(cell[0], cell[1], 0)])
    while q:
        r, c, d = q.popleft()
        if d >= limit:
            continue
        for dr, dc in DIRS:
            if d == 0 and (dr, dc) == came:
                continue
            step = (r + dr, c + dc)
            if step in seen or not game.can_transit(gen, *step):
                continue
            seen.add(step)
            if game.can_stop(gen, *step):
                found[step] = d + 1
            q.append((step[0], step[1], d + 1))
    return found

def retreat_spot(game, gen, threat_from, cell=None):
    """守将被逼退之后会落在哪：步数最少的落脚点，同一步数再挑离威胁最远的。"""
    spots = _routes(game, gen, threat_from, cell)
    if not spots:
        return None
    return min(spots, key=lambda s: (spots[s], -path_steps(threat_from, s)))

class Snapshot:
    """临时把局面改一改再算分用的存档（位置、体力、死活、守城加成、粮草）；算完**必须**原样还回去。"""

    def __init__(self, game):
        self.game = game
        self.saved = [(g, g.row, g.col, g.stamina, g.alive, g.city_boost) for g in game.generals]
        self.grain = dict(game.grain)

    def restore(self):
        for gen, row, col, stamina, alive, boost in self.saved:
            gen.row, gen.col, gen.stamina, gen.alive, gen.city_boost = row, col, stamina, alive, boost
        self.game.grain.clear()
        self.game.grain.update(self.grain)

# 一次强攻的推演结果（`plan_assault` 的返回值）。
#   kills  = 被打死的守将（**歼灭入口**——搜索靠它补一笔歼灭分）
#   dealt  = [(被逼退 / 打残的守将, 他掉了多少体力)]
#   taken  = 攻方是否把守军清空（就地进占）
#   died   = 攻方自己是否倒下
#   cost   = 攻方自己掉了多少体力
# 注：各版互相独立、各有一份 `tactics.py`，所以这里是**竹版这一份**。竹版搜索只在 `_do` 里
# **真打进去**（`resolve_assault`），不再走"先推演、后落实"的两段式，于是本文件的 `plan_assault`
# 与 `Assault` 的字段在**竹版内已无人调用/读取**——留着是为了和梅/兰/菊那几份保持同一套接口，
# 也方便随时改回"先看战果再决定"的写法。
Assault = namedtuple("Assault", "cell kills dealt taken cost died")

class SimBattle:
    """一场**搜索用**的模拟交战：带随机伤害、守方死战、攻方按自己的账决定收不收手。

    只 mock 出 `atk_should_hold` / `ai_pick_defender` 会问到的那些公开状态——所以那两套策略能
    **原样**搬进搜索里（这正是要的：搜索里的撤退时机与点将 = 真实对局里那一套）。每掷一次骰子都真的
    改体力、判死活，所以打完就是"随机的那一种可能"，是一个具体的、能继续往下搜的局面。
    """

    def __init__(self, game, attacker, defenders, rng, cell):
        self.game = game
        self.rng = rng
        self.atk = attacker
        self.defenders = list(defenders)
        self.cell = cell
        self.origin = attacker.pos          # 攻方出发格（策略里不读，留着备用）
        self.dfd = None
        self.round = 0

    def can_retreat(self):
        """攻方付得起撤退那笔行动力就行（照 Game.can_retreat 的攻方那一支）。"""
        return self.atk.move_points >= ATK_RETREAT_COST

    def _clash(self):
        """交手一回合：双方同时各掷一个 [1, 2×对方武力] 的伤害（和真实 Battle.roll 同一套）。"""
        self.round += 1
        self.atk.add_stamina(-self.rng.randint(1, LOSS_MAX_PER_MIGHT * self.dfd.might))
        self.dfd.add_stamina(-self.rng.randint(1, LOSS_MAX_PER_MIGHT * self.atk.might))
        if self.atk.stamina <= 0:
            self.atk.stamina, self.atk.alive = 0, False
        if self.dfd.stamina <= 0:
            self.dfd.stamina, self.dfd.alive = 0, False
        return self.dfd.alive

    def resolve(self, should_hold, pick):
        """把这场仗打到有结果 -> (kills, driven, taken, died)。

        `should_hold(battle)` / `pick(battle)` 就是攻方的"继续还是收手"与守方的"谁先出阵"（真实对局
        里分别传 zhu.atk_should_hold 和 zhu.defense.ai_pick_defender）。守将一位一位上：每位开打前
        先问一句收不收手（收手就整场撤退，不占格、也不接着打后面的）；坚持就交手，直到一方倒下。
        """
        if len(self.defenders) > 1:                    # 出场顺序由守方自己那套点将来定
            first = pick(self)
            self.defenders.insert(0, self.defenders.pop(first))
        kills, driven = [], []
        for dfd in self.defenders:
            if not dfd.alive:
                continue
            self.dfd = dfd
            start = dfd.stamina                         # 这一场开打前，他还剩多少
            while True:
                if not should_hold(self):               # 攻方收手：整场撤退
                    if dfd.alive and dfd.stamina < start:
                        driven.append((dfd, start - dfd.stamina))   # 打残、没打死的守将
                    return tuple(kills), tuple(driven), False, False
                if not self._clash():                   # 守将倒下：被歼灭
                    kills.append(dfd)
                    break
                if not self.atk.alive:                  # 攻方倒下：后面不打了
                    if dfd.alive and dfd.stamina < start:
                        driven.append((dfd, start - dfd.stamina))
                    return tuple(kills), tuple(driven), False, True
        return tuple(kills), tuple(driven), True, False  # 守军清空、就地进占

def _simulate(game, gen, cell, rng, should_hold, pick):
    """掷骰推演这一仗（**真的改** gen / 守将 的体力与位置）-> (kills, driven, taken, died, round)。

    这是 `resolve_assault` 与 `plan_assault` 的**共同内核**，只负责"打"，不负责"收场"：
      * 谁想不动局面，谁就自己拿 `Snapshot` 包起来（`plan_assault` 就是这么做"只推演"的）；
      * 竹版搜索只走 `resolve_assault`（要的就是"真打进去"），`plan_assault` 是给别的用法留的。

    守军站在城池里就先抬体力（照真实 `Battle` 的守城加成），打完再压回去。
    **守方死战不退——活下来的守将不挪窝，仍守着这一格**（不调用 `retreat_spot`）。
    """
    defenders = [g for g in game.generals_at(*cell) if g.owner != gen.owner]
    if not defenders or gen.exhausted:
        return (), (), False, False, 0
    city = game.grid[cell[0]][cell[1]] == CITY
    if city:
        for dfd in defenders:
            dfd.apply_city_boost()                    # 守城加成 ×1.2（和真实交战同一刻、同一倍率）
    battle = SimBattle(game, gen, defenders, rng, cell)
    kills, driven, taken, died = battle.resolve(should_hold, pick)
    if city:
        for dfd in defenders:
            dfd.drop_city_boost()                     # 打完压回原比例（照真实 finish，活死都压）
    return kills, driven, taken, died, battle.round

def plan_assault(game, gen, cell, rng, should_hold, pick):
    """**只推演**：随机推演 gen 踏进 cell 会打成什么样 -> Assault（没敌人可打就是 None）。

    推演会真的改局面（体力、死活、位置），所以整个用 `Snapshot` 包起来、算完原样还回去——对外就是
    "只推演、不改局面"。同一局面 + 同一 `rng` 推出同一仗。

    注：竹版搜索**不用它**——强攻的收益改成"真打完之后读 `position_eval`"（见 offense `_expand`），
    所以只需要 `resolve_assault`。"推演一遍拿 `Assault` 字段打分"是另一套口径，留着备用。
    """
    defenders = [g for g in game.generals_at(*cell) if g.owner != gen.owner]
    if not defenders or gen.exhausted:
        return None
    start = gen.stamina
    snap = Snapshot(game)
    try:
        kills, driven, taken, died, rounds = _simulate(game, gen, cell, rng, should_hold, pick)
        left = gen.stamina                              # 落定后攻方还剩多少（算 cost）
    finally:
        snap.restore()                                  # 只推演：还原局面
    return Assault(cell, kills, tuple(driven), taken, start - left, died)

def resolve_assault(game, gen, cell, rng, should_hold, pick):
    """把一次强攻**真的打进局面**（供模拟规划用）-> (kills, driven, taken, died, round)。"""
    return _simulate(game, gen, cell, rng, should_hold, pick)

# === 一步之内反复要用的局面缓存 ===
class Board:
    """一次决策之内反复要用的局面缓存：**一次性算全**。

    上来就把全体武将的 reach、双方的距离场都算好，之后随便问。代价是"哪怕只问一个武将也要算全部"，
    所以搜索里用的是按需版的 `_BoardLite`（offense）/ `_ReachBoard`（下面）——它们只在真被问到某
    名武将时才算那一名。这里保留"全算"版，给**调用次数少**的地方用（防守那几个入口）。
    """

    def __init__(self, game):
        self.game = game
        self.reach = {}                      # 武将 -> {落脚格: 步数}（原地记 0 步）
        self.garrison = {}                   # 格子 -> [活着的武将]
        for gen in game.generals:
            if not gen.alive:
                continue
            self.garrison.setdefault(gen.pos, []).append(gen)
            self.reach[gen] = dict(game.compute_reachable(gen))
            self.reach[gen][gen.pos] = 0
        self.field = {player: game.distance_field(player) for player in PLAYERS}

def can_strike(board, gen, cell):
    """gen 这一回合能不能踏进 cell 跟人开打：先走到隔壁，再花 1 点移动力冲进去。

    `board` 只需要能回答"这名武将的 reach 是多少"，两种写法都行：
      * `Board.reach` 是现成的表（一次性算全，见 `Board`）；
      * `_ReachBoard.reach_for(gen)` 按需算（只在真被问到某名武将时才算那一名）。
    所以这里用 `hasattr` 探一下再取。**注意**：offense 的 `_BoardLite` 只带 `garrison`（没有
    reach），是给 `_city_near_front` 那类比大小用的，不能传进来。
    """
    if gen.exhausted:
        return False
    if path_steps(gen.pos, cell) > gen.move_points:
        return False                                  # 太远，一眼否掉（省下后面的查表）
    if gen.pos in tuple(neighbors(*cell)):            # 已经贴着了，原地就能冲
        return True
    reach = (board.reach_for(gen) if hasattr(board, "reach_for")
             else board.reach[gen])                   # _BoardLite 按需算，Board 是现成的
    return any(reach.get(nb, 99) < gen.move_points for nb in neighbors(*cell))

def strikers(board, cell, owner):
    """这一回合能踏进 cell 开打的 owner 方武将 -> [武将]。

    靠 `board.reach` 的**键**遍历（那就是"活着的武将"），所以只有 `Board` 能用；
    `_ReachBoard` 只有"一个一个问"的 `reach_for`、没有可遍历的键，`_BoardLite` 更是连 reach 都没有。
    """
    return [gen for gen in board.reach
            if gen.owner == owner and can_strike(board, gen, cell)]

# === 攻方的"继续还是收手"：真实对局与搜索共用这一套 ===
# 放在 tactics（最底层）里，是为了让 ai.py 和搜索**用同一个函数**：搜索里的撤退时机必须就是真实
# 对局里的那套判断，不能另拍一份。它只读 battle 上的公开状态，所以两边都能调。
#   battle.game / battle.atk / battle.dfd / battle.cell / battle.can_retreat()
ATK_ROLL_SCALE = 1500   # 攻方"能不能拿下这一格"的 sigmoid 陡峭度（σ 差一份满血武将 ≈ 1500）

class _ReachBoard:
    """只带 `reach_for` 的最小局面缓存——`can_strike` 只用这一样。

    和 `Board` 的区别在于**按需算、且只算被问到的武将**：`Board.__init__` 上来就把**全体**武将的
    `compute_reachable` 都算一遍（一局下来是全程序最大的 `compute_reachable` 调用方），而
    `_take_cell_prob` 只想问"谁够得着这一格"，通常也就一两个人。
    """

    __slots__ = ("game", "_reach")

    def __init__(self, game):
        self.game = game
        self._reach = {}

    def reach_for(self, gen):
        if gen not in self._reach:
            r = dict(self.game.compute_reachable(gen))
            r[gen.pos] = 0
            self._reach[gen] = r
        return self._reach[gen]

def _take_cell_prob(game, atk, defenders, cell):
    """本回合我方能不能攻下这一格的概率 -> 0~1（σ 优势过一个 sigmoid）。

    σ = Σ 武力×体力：单回合掉血 ∝ 对方武力、能挨的回合数 ∝ 体力，所以"能换掉的血量"正比于这个乘积。
    守军 σ 按 **`defender_stamina`** 算（守城 ×1.2）——真实对局里守方在城里有加成；搜索里守方死战、
    不用加成，所以换算出来一样。`defenders` 传哪几名就按哪几名算。
    """
    if not defenders:
        return 1.0                                   # 已经没守军了，自然算攻下
    # 用 `_ReachBoard`（按需算）而不是整块 `Board`：`can_strike` 头一步就用 `path_steps` 查表把
    # "够不着"的挡掉，够得着的才会真的去问 `reach_for`——所以整块 `Board` 那 24 份 reach 绝大多数
    # 是白算的（这也是全程序 `compute_reachable` 最大的一处调用来源）。
    board = _ReachBoard(game)
    ours = [g for g in game.generals
            if g.alive and g.owner == atk.owner and can_strike(board, g, cell)]
    our_sigma = sum(g.might * g.stamina for g in ours)   # 攻方不在这一格上，不加守城
    their_sigma = sigma(game, defenders, cell)           # 守军驻此格，城池 ×1.2 算在内
    if our_sigma == 0 and their_sigma == 0:
        return 0.5
    return 1.0 / (1.0 + math.exp(-(our_sigma - their_sigma) / ATK_ROLL_SCALE))

def atk_should_hold(battle):
    """攻方的"这一刀接着砍还是收手" —— 真实对局与搜索**共用这一套**。

    先看一条**最高优先级**：比双方"挨对方下一刀阵亡的概率"——只要**我阵亡的概率大于对方**，说明
    这一刀我更可能先倒，**直接收手**（能撤就撤；没退路只能打）。

    这条不成立时，才看能不能拿下这一格（`_take_cell_prob`）：
      * 概率 > 0.5（有把握拿下）：接着砍——除非我的死险超过他的，否则总不亏；
      * 概率 ≤ 0.5（拿不下）：不白耗，立即收手。
    """
    game = battle.game
    if battle.dfd is None:
        return True                                  # 还没定守将（点将阶段），先接着
    dfd = battle.dfd
    mine = death_risk(battle.atk.stamina, dfd.might)
    theirs = death_risk(defender_stamina(game, dfd, battle.cell), battle.atk.might)
    if mine > theirs:
        return not battle.can_retreat()              # 会死：能撤就撤，没退路只能接着打
    if not battle.can_retreat():
        return True
    defenders = [g for g in game.generals_at(*battle.cell) if g.owner != battle.atk.owner]
    if _take_cell_prob(game, battle.atk, defenders, battle.cell) <= 0.5:
        return False                                 # 拿不下：立即收手，不白耗
    # 有把握拿下 -> 只要**对方**那一刀的死险不比我小，就接着磨（我更可能先倒的情况，上面那条已经拦掉了）
    return mine <= theirs
