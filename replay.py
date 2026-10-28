# -*- coding: utf-8 -*-
"""对局录像：把一局从头到尾记成一个 JSONL 文件，**边打边写**，打完即完整。

每一行是一个事件（JSON 对象），`k` 是事件类型：

    header  开局信息：种子、模式、双方电脑版本、地图、名册
    turn    每回合**开局时**的全盘快照：所有武将（体力/位置/移动力/欠账）+ 粮草 + 比分
    act     棋盘上的一个动作：走 / 吃粮 / 进攻 / 用计
    battle  交战屏上的一个子事件：开打 / 点将 / 交手骰点 / 续战 / 撤退 / 收场
    grain   城池入库
    end     终局：胜方 + 最终比分

事件**按发生的先后顺序**写进去，写完一行就 flush——所以游戏中途崩了，文件里也已经
记到崩之前那一步。看懂原始 JSONL 不方便，直接丢给自带的小查看器：

    python replay.py replays/xxx.jsonl          # 完整复盘
    python replay.py replays/xxx.jsonl --brief  # 只看每回合快照和终局
    python replay.py replays/xxx.jsonl --gui    # 开窗口，按 ← → 一帧一帧看棋盘

录像只在有电脑参与的局里写（`Game` 开局时按 `REPLAY` 常量决定），写文件不碰
`game.rng`，所以**同一局面跑出来还是一模一样**，不影响胜负判定与回归测试。

这个模块平时是被对局进程 import 的（`Game.start_recording`），所以**顶层不 import
pygame**，图形复盘要用到的 pygame / warlord 都留到 `play()` 里面再 import。
"""

import json
import math
import os
import time
from pathlib import Path

from constants import (
    ATK_RETREAT_COST, BOARD, CITY, CITY_DEFENSE_MULT, C_ATTACK, C_BG, C_CUNNING,
    C_GRAIN, C_PANEL, C_SELECT, C_TEXT, C_TEXT_DIM, C_WARN, FACTION_SHORT, FPS,
    GRAIN_PER_CITY, MARGIN, MAX_STAMINA, PLAYER_COLOR, SIDEBAR, STRATAGEM_MOVE_COST,
    STRATAGEM_STAMINA_COST, TERRAIN_NAME, TURN_LIMIT, WIN_H, WIN_W,
)

REPLAY_DIR = Path(__file__).with_name("replays")
FORMAT = 1
_seq = 0                    # 进程内计数，让同一秒的多个**进程**也能分开（见 _unique_path）


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _unique_path(stem):
    """给录像挑一个**此时此刻唯一**的文件名（含 pid，重复就再加序号）。

    光靠"时间戳+进程内序号"不够：两个进程（比如并行跑两批对战）在同一秒起手时，
    各自的序号都是 001，会撞名、互相覆盖，留下空文件。所以把 pid 也编进去，并
    兜底地试到不重名为止。
    """
    root = f"{stem}-p{os.getpid()}"
    for n in range(1000):
        suffix = "" if n == 0 else f"-{n}"
        candidate = REPLAY_DIR / f"{root}{suffix}.jsonl"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"挑不出没被占用的录像文件名：{root}*")


def _key(player):
    """玩家的键：JSON 的对象键只能是字符串，所以用 "0" / "1"。"""
    return str(int(player))


def serial_gen(gen):
    """一名武将的紧凑记法（字段名取短的，文件小一半）。"""
    return {"n": gen.name, "o": gen.owner, "r": gen.row, "c": gen.col,
            "hp": gen.stamina, "mp": gen.move_points, "debt": gen.move_debt,
            "alive": gen.alive, "boost": gen.city_boost,
            "mv": gen.might, "iq": gen.intellect}


class Recorder:
    """一局录像。开局建它（写好 header），之后由 Game / Battle 在关键点喂事件。"""

    def __init__(self, game, path=None):
        global _seq
        REPLAY_DIR.mkdir(exist_ok=True)
        _seq += 1
        if path is None:
            # 文件名带上这局是谁在打：两个版本名（菊 vs 梅），或 2P 同机
            tag = ("2p" if self._is_human_match(game)
                   else "vs".join(game.ai_version(p) for p in (0, 1)))
            path = _unique_path(f"{_stamp()}-{_seq:03d}-s{game.seed}-{tag}")
        self.path = Path(path)
        self.file = open(self.path, "w", encoding="utf-8")
        self.closed = False
        self._header(game)

    @staticmethod
    def _is_human_match(game):
        """两边都是人（2P 同机）——那种局不该走到这儿（没电脑就不录），只是兜底。"""
        from constants import AI
        return AI not in game.controllers.values()

    # ---------------- 底层 ----------------
    def event(self, kind, **data):
        """写一行事件并 flush（增量存储：崩了也不丢已经记下的）。"""
        if self.closed:
            return
        data["k"] = kind
        self.file.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.file.flush()

    def close(self):
        if not self.closed:
            self.file.flush()
            self.file.close()
            self.closed = True

    # ---------------- 各类事件 ----------------
    def _header(self, game):
        """开局：种子、模式、双方版本、地图、名册。地图是静态的，只写这一次。"""
        self.event("header", v=FORMAT, seed=game.seed,
                   players=game.players, human_side=game.human_side,
                   ai={_key(p): game.ai_version(p) for p in (0, 1)},
                   map=[list(row) for row in game.grid],
                   places=[[r, c, name] for (r, c), name in game.places.items()],
                   roster={_key(p): [[s.name, s.might, s.intellect]
                                     for s in game.roster[p]] for p in (0, 1)},
                   stamp=time.strftime("%Y-%m-%d %H:%M:%S"))

    def turn(self, game):
        """回合开始的快照。"""
        score, contested = self._scores(game)
        self.event("turn", n=game.turn, p=game.current,
                   gens=[serial_gen(g) for g in game.generals],
                   grain=[[r, c, v] for (r, c), v in sorted(game.grain.items()) if v],
                   score=score, contested=contested)

    def _scores(self, game):
        board, contested = game.scoreboard()
        return {_key(p): list(board[p]) for p in (0, 1)}, contested

    def act(self, do, **kw):
        self.event("act", do=do, **kw)

    def battle(self, do, **kw):
        self.event("battle", do=do, **kw)

    def grain(self, game, made):
        self.event("grain", n=game.turn, made=made, stock=game.grain_stock())

    def end(self, game):
        score, contested = self._scores(game)
        self.event("end", winner=game.winner,
                   text=(game.banner[0] if game.banner else None),
                   score=score, contested=contested)
        self.close()


# --------------------------------------------------------------------------
# 查看器：把 JSONL 渲染成一段人读的复盘
# --------------------------------------------------------------------------
def _load(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


class _View:
    def __init__(self, header):
        self.grid = header["map"]
        self.places = {(r, c): name for r, c, name in header["places"]}

    def place(self, cell):
        """格子的称呼：有地名用「地名」，没有（障碍格）退回地形名。"""
        r, c = cell
        name = self.places.get((r, c))
        return name if name else TERRAIN_NAME.get(self.grid[r][c], "?")

    @staticmethod
    def gen_line(gens, player):
        """一方的武将从简一行：名字(行,列)体力/移动力；死了标 ☠。"""
        out = []
        for g in gens:
            if g["o"] != player:
                continue
            if not g["alive"]:
                out.append(f"{g['n']}☠")
            else:
                mark = "*" if g["boost"] else ""      # * = 守城加成中
                out.append(f"{g['n']}({g['r']},{g['c']}){g['hp']}{mark}/{g['mp']}")
        return "、".join(out) or "（无）"

    @staticmethod
    def score_line(score, contested):
        parts = []
        for k in ("0", "1"):
            total, gens, land = score[k]
            parts.append(f"{FACTION_SHORT[int(k)]} {total}（将 {gens} 地 {land}）")
        return "　｜　".join(parts) + f"　｜　争夺 {contested} 格"


def view(path, brief=False):
    events = _load(path)
    header = next((e for e in events if e["k"] == "header"), None)
    if header is None:
        print(f"{path}：不是一份录像（没有 header）。")
        return
    v = _View(header)
    mode, roles = _header_roles(header)
    ai = header.get("ai", {})
    print(f"=== 录像 s{header['seed']} · {mode} · {roles} · {header['stamp']} ===")

    for e in events:
        k = e["k"]
        if k == "header":
            continue
        if k == "turn":
            cur = e["p"]
            ver = ai.get(str(cur), "")
            who = f"{FACTION_SHORT[cur]}（{ver}）" if ver else FACTION_SHORT[cur]
            print(f"\n--- 第 {e['n']} / {TURN_LIMIT} 回合 · {who} ---")
            print(f"  {FACTION_SHORT[0]}  {v.gen_line(e['gens'], 0)}")
            print(f"  {FACTION_SHORT[1]}  {v.gen_line(e['gens'], 1)}")
            print(f"  {v.score_line(e['score'], e['contested'])}")
        elif k == "grain":
            print(f"  ⚑ 第 {e['n']} 回合入库 {e['made']} 石（散落野地共 {e['stock']} 石）")
        elif brief:
            continue
        elif k == "act":
            print("  " + _act_line(v, e))
        elif k == "battle":
            print(_battle_line(v, e))
        elif k == "end":
            print(f"\n=== 终局：{e['text'] or '（无）'} ===")
            print(f"  最终 {v.score_line(e['score'], e['contested'])}")


def _act_line(v, e):
    do, who = e["do"], f"{e['by']}"
    if do == "move":
        extra = f"，顺路搬 {e['carry']} 石粮" if e.get("carry") else ""
        return (f"> {who} 移动 {e['steps']} 格 → {v.place(e['to'])}{tuple(e['to'])}"
                f"，剩移动力 {e['mp']}{extra}")
    if do == "eat":
        before, after = e["hp"]
        return f"> {who} 吃粮 {e['amount']} 石，体力 {before}→{after}，剩移动力 {e['mp']}"
    if do == "charge":
        return f"> {who} 进攻 {v.place(e['cell'])}（守军：{('、'.join(e['defenders']))}）"
    if do == "strat":
        got = "得手！" if e["got"] else "失手。"
        return (f"> 🎴 {who} 对 {v.place(e['cell'])} 用「{e['name']}」："
                f"成功率 {e['rate']:.0%}，掷出 {e['roll']:.0%} —— {got}")
    if do == "strat_effect":
        return f"    ↳ {e['detail']}"
    return f"> {who} {do} {e}"


def _battle_line(v, e):
    do = e["do"]
    if do == "begin":
        return (f"    ⚔ 交战于 {e['place']}：{e['atk']} 进攻 "
                f"{('、'.join(e['dfd']))}")
    if do == "pick":
        return f"      · {e['first']} 先出阵"
    if do == "roll":
        return (f"      · 第 {e['round']} 回合：{e['atk']} -{e['atk_loss']}"
                f"（→{e['atk_hp']}）　{e['dfd']} -{e['dfd_loss']}（→{e['dfd_hp']}）")
    if do == "hold":
        return f"      · {e['name']} 继续缠斗"
    if do == "retreat":
        who = "守方" if e["who"] == "dfd" else "攻方"
        return f"      · {e['name']}（{who}）选择撤退"
    if do == "retreat_done":
        return (f"        ↳ 撤 {e['steps']} 格到 {v.place(e['dest'])}"
                f"（透支下回合 {e['debt']} 点行动力）")
    if do == "duel_end":
        return f"      · {e['atk']} vs {e['dfd']} 打了 {e['rounds']} 回合：{e['text']}"
    if do == "end":
        tail = "　撤退，攻方原地不动" if e.get("atk_retreated") else ""
        return f"    ⚔ 收场（{e['place']}）：{e['text']}{tail}"
    return f"    ⚔ {do} {e}"


# --------------------------------------------------------------------------
# 图形复盘：在棋盘上一帧一帧地放录像（← → 前后翻一步，不用进交战屏）
# --------------------------------------------------------------------------
_AUTO_HOLD = 16      # 自动播放时两帧之间停几帧（60fps 下约 0.27 秒）
_HINT = "← → 前后一步　PgUp/PgDn 上/下一回合　空格 自动　Home/End 首尾　Esc 退出"
# 复盘侧边栏里几处用到的颜色：跟 warlord.py 的配色对齐（那边的面板/描边/日志字号同色），
# 集中在这里，免得同一块面板在两边各写一遍 RGB。
C_BOX = (42, 46, 58)          # 内容框底色（同 Game.draw_selected_info / draw_score_board）
C_BOX_LINE = (86, 94, 112)    # 内容框描边（同 Game 的按钮描边）
C_BATTLE_BG = (30, 34, 44)    # 交战那块底色
C_LOG = (198, 203, 214)       # 侧边栏「最近几步」的字色（同 Game.draw_sidebar 的战报）
C_HP_HI, C_HP_MID, C_HP_LO = (96, 208, 112), (232, 196, 72), (226, 84, 72)  # 血条三档


def _brief(text):
    """把复盘行首的 "> " / "↳ " 记号抹掉，留着当侧边栏那一句话。"""
    return text.strip().lstrip(">↳").strip()


def _header_roles(header):
    """录像头部 -> (模式, 「蜀=菊　魏=梅」那行)。文字复盘和图形复盘共用同一套说法。"""
    mode = {1: "一人游玩", 2: "两人同机"}.get(header.get("players"), "?")
    ai = header.get("ai", {})
    roles = "　".join(f"{FACTION_SHORT[int(k)]}={ai[k]}" for k in sorted(ai))
    return mode, roles


class _Timeline:
    """把一份录像摊成一串「帧」：每按一次 → 就往后挪一帧。

    帧里存的是**该事件发生之后**的全盘状态（武将 + 粮草 + 回合 + 比分），所以来回翻
    就是查表，不用重放。状态在每一条 `turn` 快照处**整个重设**——同一条回合里万一个
    别事件重建得不够准，一到下一回合开局就自动校正回来。

    重建是"事件给什么就记什么"：走、吃粮、进攻、交战的掉血与撤退、入库、终局都是
    逐字照着事件落的。只有两处事件本身没记全，先按下不表（下一回合快照会校正）：
      * 内讧：两名敌将互砍掉多少血，事件里只有一句说明文字，没有数值；
      * 调虎离山：被调走的是**哪一名**敌将，事件里没写（只写了被调去的格子）。
    这两种局眼下只有 1P 人类玩家会用，而 1P 的录像本来就少；真碰上了，棋盘会在下一条
    `turn` 快照处对齐。
    """

    def __init__(self, events):
        header = next((e for e in events if e.get("k") == "header"), None)
        if header is None:
            raise ValueError("不是一份录像：没有 header")
        self.header = header
        self.view = _View(header)                 # 借它把格子叫成「地名（地形）」
        self.grid = header["map"]
        self.places = {(r, c): n for r, c, n in header["places"]}
        self.cities = [(r, c) for r, row in enumerate(self.grid)
                       for c, t in enumerate(row) if t == CITY]   # 地图不变，数一次就够
        self.frames = []
        self._gens = {}          # 名字 -> [o, r, c, hp, mp, debt, alive, boost, mv, iq]
        self._order = []
        self._grain = {}
        self._turn, self._cur = 1, 0
        self._over, self._winner = False, None
        self._battle = None
        self._dirty = True
        self._snap_gens, self._snap_grain = (), {}
        for e in events:
            self._step(e)
        # 第 0 帧（header）里还没有武将，开窗口时棋盘会是一张空图。把第 1 回合的列阵
        # 借过来垫上——"开局"就该看到双方摆在半场上的样子。
        first = next((f for f in self.frames if f["k"] == "turn"), None)
        if first is not None:
            self.frames[0].update(turn=first["turn"], cur=first["cur"],
                                  gens=first["gens"], grain=first["grain"])

    # ---------------- 快照 ----------------
    def _touch(self):
        self._dirty = True

    def _take(self):
        """当前状态的快照；状态没动过（比如「继续缠斗」）就把上一帧那份接着用。"""
        if self._dirty:
            self._snap_gens = tuple((n, *self._gens[n]) for n in self._order)
            self._snap_grain = dict(self._grain)
            self._dirty = False
        return self._snap_gens, self._snap_grain

    # ---------------- 走事件 ----------------
    def _step(self, e):
        k = e.get("k")
        hl, desc = {}, ""
        if k == "header":
            desc = f"开局 · 录像 s{self.header.get('seed')}"
        elif k == "turn":
            self._resync(e)
            desc = f"第 {self._turn} 回合开局 · 轮到 {FACTION_SHORT[self._cur]}"
        elif k == "act":
            desc, hl = self._act(e)
        elif k == "battle":
            desc, hl = self._duel(e)
        elif k == "grain":
            hl = self._produce()
            desc = f"城池入库 {e['made']} 石（散落野地共 {e['stock']} 石）"
        elif k == "end":
            self._over, self._winner = True, e.get("winner")
            desc = f"终局：{e.get('text') or '（无）'}"
        else:
            desc = str(k)
        gens, grain = self._take()
        # 交战那块按**值**拷一份：战况本身是逐个事件改的，存引用的话同一场交战的
        # 每一帧都会显示出最后那一步的攻守双方。
        self.frames.append({"turn": self._turn, "cur": self._cur, "gens": gens,
                            "grain": grain, "over": self._over, "winner": self._winner,
                            "k": k, "do": e.get("do"), "desc": desc, "hl": hl,
                            "battle": dict(self._battle) if self._battle else None})

    def _resync(self, e):
        """回合快照：全盘状态照单重设（武将、粮草、轮到谁）。"""
        self._turn, self._cur, self._over = e["n"], e["p"], False
        self._gens.clear()
        self._order.clear()
        self._grain.clear()
        for g in e["gens"]:
            self._gens[g["n"]] = [g["o"], g["r"], g["c"], g["hp"], g["mp"],
                                  g["debt"], g["alive"], g["boost"], g["mv"], g["iq"]]
            self._order.append(g["n"])
        for r, c, v in e["grain"]:
            if v:
                self._grain[(r, c)] = v
        self._touch()

    def _produce(self):
        """入库回合：每座城池 +GRAIN_PER_CITY（跟 Game.produce_grain 一致）。"""
        for cell in self.cities:
            self._grain[cell] = self._grain.get(cell, 0) + GRAIN_PER_CITY
        self._touch()
        return {"rings": [(r, c, "gold") for r, c in self.cities]}

    # ---------------- 棋盘动作 ----------------
    def _act(self, e):
        do, who = e["do"], e.get("by")
        hl = {}
        if do == "move":
            g = self._gens[who]
            frm, to = tuple(e["frm"]), tuple(e["to"])
            carry = e.get("carry", 0)
            if carry:                              # 这人只是脚夫：从出发格搬到落脚格
                self._spend_grain(frm, carry)
                self._grain[to] = self._grain.get(to, 0) + carry
            g[1], g[2], g[4] = to[0], to[1], e["mp"]
            self._touch()
            hl = {"arrow": (frm, to), "focus": [who]}
        elif do == "eat":
            g = self._gens[who]
            self._spend_grain((g[1], g[2]), e["amount"])
            g[3], g[4] = e["hp"][1], e["mp"]
            self._touch()
            hl = {"focus": [who],
                  "marks": [(g[1], g[2], f"+{e['hp'][1] - e['hp'][0]}", "heal")]}
        elif do == "charge":
            self._gens[who][4] = e["mp"]
            self._touch()
            cell = tuple(e["cell"])
            hl = {"focus": [who], "rings": [(cell[0], cell[1], "atk")]}
        elif do == "strat":
            g = self._gens[who]                    # 代价在用出去的那一刻就结清
            g[3] = max(0, g[3] - STRATAGEM_STAMINA_COST)
            g[4] = max(0, g[4] - STRATAGEM_MOVE_COST)
            cell = tuple(e["cell"])
            if e.get("got"):
                if e.get("key") == "sneak":        # 人就那么跃过去
                    g[1], g[2] = cell
                elif e.get("key") == "raid":       # 目标格的粮草整堆搬到脚下
                    amount = self._spend_grain(cell, self._grain.get(cell, 0))
                    if amount:
                        self._grain[(g[1], g[2])] = self._grain.get((g[1], g[2]), 0) + amount
            self._touch()
            hl = {"focus": [who], "rings": [(cell[0], cell[1], "cunning")]}
        # 说明文字统一走文字复盘那套（strat_effect 的落点说明也在这里）
        return _brief(_act_line(self.view, e)), hl

    def _spend_grain(self, cell, amount):
        """从格子上取走 amount 石（取不到就取到空为止），返回实际取走多少。"""
        got = min(amount, self._grain.get(cell, 0))
        if got <= 0:
            return 0
        left = self._grain[cell] - got
        if left:
            self._grain[cell] = left
        else:
            self._grain.pop(cell, None)
        return got

    # ---------------- 交战 ----------------
    def _duel(self, e):
        do = e["do"]
        G, hl = self._gens, {}
        if do == "begin":
            cell = tuple(e["cell"])
            if e.get("city"):                      # 守军据城：开打前体力先抬 20%
                for name in e["dfd"]:
                    g = G.get(name)
                    if g and not g[7]:
                        g[3] = math.floor(g[3] * CITY_DEFENSE_MULT + 0.5)
                        g[7] = True
                self._touch()
            self._battle = {"cell": cell, "place": e.get("place"),
                            "atk": e.get("atk"), "dfd": (e.get("dfd") or [None])[0],
                            "all_dfd": list(e.get("dfd", []))}
            hl = {"rings": [(cell[0], cell[1], "atk")]}
        elif do == "pick":
            if self._battle:
                self._battle["dfd"] = e.get("first")
        elif do == "roll":
            marks = []
            for name, loss, hp in ((e["atk"], e["atk_loss"], e["atk_hp"]),
                                   (e["dfd"], e["dfd_loss"], e["dfd_hp"])):
                g = G.get(name)
                if not g:
                    continue
                g[3], g[6] = hp, hp > 0            # 掉到 0 或以下当场阵亡
                marks.append((g[1], g[2], f"-{loss}", "dmg"))
            if self._battle:
                self._battle["atk"], self._battle["dfd"] = e["atk"], e["dfd"]
            self._touch()
            hl = {"marks": marks, "focus": [n for n in (e["atk"], e["dfd"]) if n in G]}
        elif do == "hold":
            hl = {}                                # 什么都没变，帧与上一帧共用快照
        elif do == "retreat":
            g = G.get(e["name"])
            if g and e.get("who") == "atk":        # 攻方收手：在进攻那笔钱上再补一笔
                g[4] = max(0, e["mp"] - ATK_RETREAT_COST)
                self._touch()
            hl = {"focus": [e["name"]]} if e.get("name") in G else {}
        elif do == "retreat_done":
            g = G.get(e["name"])
            dest = tuple(e["dest"])
            if g:
                g[1], g[2], g[5] = dest[0], dest[1], e["debt"]
                self._touch()
            frm = self._battle["cell"] if self._battle else dest
            hl = {"arrow": (frm, dest), "focus": [e["name"]]}
        elif do == "duel_end":
            for name in e.get("deaths", []):
                if name in G:
                    G[name][6] = False
            self._touch()
            hl = {"marks": [(G[n][1], G[n][2], "☠", "dead")]
                  for n in e.get("deaths", []) if n in G}
        elif do == "end":
            info = self._battle or {}
            g = G.get(e.get("atk"))
            if g and e.get("atk_pos"):             # 打赢了就进占这一格，否则原地不动
                g[1], g[2] = tuple(e["atk_pos"])
            for name in info.get("all_dfd", []):   # 守城加成到此为止，体力压回原比例
                d = G.get(name)
                if d and d[7]:
                    d[3] = min(MAX_STAMINA, math.floor(d[3] / CITY_DEFENSE_MULT + 0.5))
                    d[7] = False
            self._touch()
            cell = info.get("cell")
            hl = {"rings": [(cell[0], cell[1], "atk")]} if cell else {}
            self._battle = None
        return _brief(_battle_line(self.view, e)), hl

    # ---------------- 给窗口用的查询 ----------------
    def meta(self):
        mode, roles = _header_roles(self.header)
        return f"{mode}　{roles}　{self.header.get('stamp', '')}"

    def turn_index(self, i, step):
        """从第 i 帧起，往后（step=+1）或往前（step=-1）找最近的**另一条**回合快照。

        已经在回合开头时，`step=-1` 拿到的是上一个回合（不是原地打转）；没有就 None。
        """
        j = i + step
        start = self.frames[i]["turn"] if 0 <= i < len(self.frames) else None
        while 0 <= j < len(self.frames):
            if self.frames[j]["k"] == "turn" and self.frames[j]["turn"] != start:
                return j
            j += step
        return None


class _Gui:
    """录像的图形复盘窗口：一帧一帧画棋盘，← → 前后翻一步。"""

    def __init__(self, timeline, path, pygame, warlord):
        self.pg, self.wl = pygame, warlord
        self.timeline, self.frames = timeline, timeline.frames
        self.header, self.path = timeline.header, path
        self.meta = timeline.meta()
        self.i, self.auto, self._wait = 0, False, _AUTO_HOLD
        # 一帧的画面在**同一步**里是定死的（60fps 也只是反复画同一张），所以凡是
        # 只跟"第几步"有关的东西都算一次就存下来：装好的武将、比分、量好字号的那几行。
        self._synced = None                        # 已经装进 game 的是第几步
        self._score = None                         # (board, contested) 缓存
        self._fixed = None                         # 文件名 / 模式 / 操作提示那几行（定死）

        # 先起 pygame 再建 game：棋盘和地名全部来自**录像头部**，所以用
        # `Game.for_replay()`（不读 map.txt / generals.txt）——这样当前的配置文件坏了、
        # 或者改过了，也不影响放老录像。
        pygame.init()
        self.game = warlord.Game.for_replay()
        self.game.grid = timeline.grid             # 地图以录像头部为准
        self.game.places = timeline.places
        # 比分板自己会算一遍双方地盘（两趟全盘 BFS）。一步之内棋盘不动，所以按"第几步"
        # 缓存结果——同一步被画 60 次也只算一次。
        self._real_scoreboard = self.game.scoreboard
        self.game.scoreboard = self._cached_score
        self.panel = pygame.Rect(self.game.SIDEBAR_X, MARGIN, SIDEBAR, BOARD)

        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        pygame.display.set_caption(f"三国战棋 - 录像复盘 · {path.name}")
        self.clock = pygame.time.Clock()
        pygame.key.set_repeat(300, 55)             # 按住 → 就一路快进

    # ---------------- 把一帧的状态装进 game，交给它画 ----------------
    def _cached_score(self):
        """替 Game.scoreboard 算一次、存一次（见 __init__ 的说明）。"""
        if self._score is None:
            self._score = self._real_scoreboard()
        return self._score

    def _sync(self, i, frame):
        """把第 i 帧的状态装进 game。同一步反复画时（60fps）直接跳过。"""
        if self._synced == i:
            return
        self._synced = i
        self._score = None                         # 状态换了，比分缓存作废
        gens = []
        for name, o, r, c, hp, mp, debt, alive, boost, mv, iq in frame["gens"]:
            g = self.wl.General(name, mv, iq, o, r, c, stamina=hp)
            g.alive, g.move_points = alive, mp
            g.move_debt, g.city_boost = debt, boost
            gens.append(g)
        game = self.game
        game.generals = gens
        game.grain = dict(frame["grain"])
        game.turn, game.current = frame["turn"], frame["cur"]
        game.over, game.winner = frame["over"], frame["winner"]
        game.clear_selection()                     # 复盘没有选中态
        game.drop_cunning()                        # 也没有用计弹窗/瞄准态
        game.banner = None

    # ---------------- 主循环 ----------------
    def run(self):
        pg = self.pg
        while True:
            for event in pg.event.get():
                if event.type == pg.QUIT:
                    pg.quit()
                    return 0
                if event.type == pg.KEYDOWN and not self._key(event.key):
                    pg.quit()
                    return 0
            if self.auto and self.i < len(self.frames) - 1:
                self._wait -= 1
                if self._wait <= 0:
                    self.i += 1
                    self._wait = _AUTO_HOLD
            self._draw()
            pg.display.flip()
            self.clock.tick(FPS)

    def _key(self, key):
        """按键 -> 要不要继续开着窗口。"""
        pg, last = self.pg, len(self.frames) - 1
        if key in (pg.K_ESCAPE, pg.K_q):
            return False
        if key == pg.K_RIGHT:
            self.i = min(last, self.i + 1)
        elif key == pg.K_LEFT:
            self.i = max(0, self.i - 1)
        elif key == pg.K_HOME:
            self.i = 0
        elif key == pg.K_END:
            self.i = last
        elif key == pg.K_PAGEDOWN:                 # 下一回合开局
            self.i = self.timeline.turn_index(self.i, 1) or last
        elif key == pg.K_PAGEUP:                   # 上一回合开局
            j = self.timeline.turn_index(self.i, -1)
            self.i = 0 if j is None else j
        elif key == pg.K_SPACE:
            self.auto = not self.auto
            self._wait = _AUTO_HOLD
        return True

    # ---------------- 画面 ----------------
    def _draw(self):
        frame = self.frames[self.i]
        self._sync(self.i, frame)
        self.screen.fill(C_BG)
        self.game.draw_board(self.screen, self.pg.mouse.get_pos())
        self._marks(frame)
        self._sidebar(frame)

    def _marks(self, frame):
        """把这一步「动在哪」画出来：移动的箭头、目标格、掉血的数字。"""
        pg, game = self.pg, self.game
        hl = frame.get("hl") or {}
        for r, c, kind in hl.get("rings", []):
            color = {"atk": C_ATTACK, "cunning": C_CUNNING, "gold": C_GRAIN}.get(kind, C_ATTACK)
            pg.draw.rect(self.screen, color, game.cell_rect(r, c).inflate(-4, -4), 3,
                         border_radius=6)
        for name in hl.get("focus", []):
            g = next((x for x in game.generals if x.name == name), None)
            if g:
                pg.draw.rect(self.screen, PLAYER_COLOR[g.owner],
                             game.cell_rect(g.row, g.col).inflate(-2, -2), 4, border_radius=6)
        if hl.get("arrow"):
            self._arrow(*hl["arrow"])
        for r, c, text, kind in hl.get("marks", []):
            self._badge(r, c, text, kind)

    def _arrow(self, frm, to):
        """从上一步的格子指向这一步的格子（移动、撤退都靠它一眼看出来）。

        半透明的箭头画在一张**只包住箭头**的小画布上再贴回去——整窗那么大的
        SRCALPHA 画布每帧建一张太浪费（60fps 下尤其）。
        """
        pg, game = self.pg, self.game
        a = game.cell_rect(*frm).center
        b = game.cell_rect(*to).center
        vx, vy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(vx, vy) or 1
        ux, uy = vx / n, vy / n
        tip = (b[0] - ux * 13, b[1] - uy * 13)
        side = (uy * 9, -ux * 9)
        back = (ux * 17, uy * 17)
        tri = [tip, (tip[0] + side[0] - back[0], tip[1] + side[1] - back[1]),
               (tip[0] - side[0] - back[0], tip[1] - side[1] - back[1])]
        pts = [a, b, *tri]
        left, top = min(p[0] for p in pts), min(p[1] for p in pts)
        right, bottom = max(p[0] for p in pts), max(p[1] for p in pts)
        box = pg.Rect(left, top, right - left, bottom - top).inflate(24, 24)  # 给线宽/箭头留余量
        layer = pg.Surface(box.size, pg.SRCALPHA)
        shift = (-box.left, -box.top)
        pg.draw.line(layer, (*C_SELECT, 190), (a[0] + shift[0], a[1] + shift[1]),
                     (b[0] + shift[0], b[1] + shift[1]), 5)
        pg.draw.polygon(layer, (*C_SELECT, 220),
                        [(p[0] + shift[0], p[1] + shift[1]) for p in tri])
        self.screen.blit(layer, box.topleft)

    def _badge(self, r, c, text, kind):
        """格子顶上挂一个小牌：掉了多少血、回了多少血、还是阵亡。"""
        pg, wl = self.pg, self.wl
        color = {"dmg": C_ATTACK, "heal": C_HP_HI, "dead": C_WARN}.get(kind, C_TEXT)
        surf = wl.get_font(15, bold=True).render(text, True, color)
        cell = self.game.cell_rect(r, c)
        box = surf.get_rect().inflate(14, 7)
        box.midbottom = (cell.centerx, cell.top + 8)
        bg = pg.Surface(box.size, pg.SRCALPHA)
        bg.fill((16, 16, 20, 224))
        self.screen.blit(bg, box.topleft)
        pg.draw.rect(self.screen, color, box, 1, border_radius=5)
        self.screen.blit(surf, surf.get_rect(center=box.center))

    def _text(self, text, size, color, x, y, bold=False):
        surf = self.wl.get_font(size, bold=bold).render(text, True, color)
        self.screen.blit(surf, (x, y))
        return surf

    def _sidebar(self, frame):
        pg, wl = self.pg, self.wl
        panel = self.panel
        pg.draw.rect(self.screen, C_PANEL, panel, border_radius=10)
        x, right = panel.left + 16, panel.right - 16
        inner = right - x                            # 内容区宽度（弹窗左右各留 16）
        y = panel.top + 12

        self._text("录像复盘", 22, C_TEXT, x, y, bold=True)
        tag = self._text(f"s{self.header.get('seed')}", 12, C_TEXT_DIM, 0, 0)
        self.screen.blit(tag, tag.get_rect(midright=(right, y + 14)))
        y += 32
        # 文件名 / 模式 / 操作提示这三行整局不变：量字号很贵（fit_font 逐个字号试探），
        # 量一次存下来，别每帧重量。
        if self._fixed is None:
            self._fixed = {
                "name": self.wl.fit_font(self.path.name, inner, start=12, min_size=9, bold=False),
                "meta": self.wl.fit_font(self.meta, inner, start=12, min_size=9, bold=False),
                "hint": self.wl.fit_font(_HINT, inner, start=11, min_size=9, bold=False),
            }
        self.screen.blit(self._fixed["name"].render(self.path.name, True, C_TEXT_DIM), (x, y))
        y += 18
        self.screen.blit(self._fixed["meta"].render(self.meta, True, C_TEXT_DIM), (x, y))
        y += 24

        # 回合 + 轮到谁
        head = "已终局" if frame["over"] else f"第 {frame['turn']} / {TURN_LIMIT} 回合"
        self._text(head, 15, C_TEXT, x, y, bold=True)
        chip = pg.Rect(right - 66, y + 3, 12, 12)
        pg.draw.rect(self.screen, PLAYER_COLOR[frame["cur"]], chip, border_radius=3)
        self._text(f"{FACTION_SHORT[frame['cur']]}"
                   + ("（待定）" if frame["over"] and frame["winner"] is None else ""),
                   14, PLAYER_COLOR[frame["cur"]], right - 48, y, bold=True)
        y += 26

        total = max(1, len(self.frames) - 1)
        self._text(f"第 {self.i} / {total} 步" + ("　▶ 自动播放中" if self.auto else ""),
                   12, C_TEXT_DIM, x, y)
        y += 20

        # 本步做了什么
        box = pg.Rect(x, y, inner, 108)
        self._draw_action(box, frame)
        y = box.bottom + 12

        # 比分板（直接借 Game 的画法，跟对局里那块一模一样）
        self.game.draw_score_board(self.screen, pg.Rect(x, y, inner, 146))
        y += 158

        # 交战中的话，补一块「谁打谁、各剩多少血」
        if frame["battle"]:
            y = self._draw_battle(pg.Rect(x, y, inner, 86), frame["battle"]) + 12

        # 最近几步（跳过「继续缠斗」这种没什么信息的）
        self._text("最近几步", 12, C_TEXT_DIM, x, y, bold=True)
        y += 18
        room = max(1, (panel.bottom - 58 - y) // 17)
        for line in self._recent(room):
            f = wl.get_font(12)
            chunk = wl.wrap_text(line, f, inner)[0]
            self.screen.blit(f.render(chunk, True, C_LOG), (x, y))
            y += 17

        self._scrubber(pg.Rect(x, panel.bottom - 44, inner, 6), total)
        self.screen.blit(self._fixed["hint"].render(_HINT, True, C_TEXT_DIM), (x, panel.bottom - 30))

    def _draw_action(self, box, frame):
        pg, wl = self.pg, self.wl
        pg.draw.rect(self.screen, C_BOX, box, border_radius=8)
        pg.draw.rect(self.screen, C_BOX_LINE, box, 2, border_radius=8)
        self._text("本步", 12, C_TEXT_DIM, box.left + 12, box.top + 8, bold=True)
        lines = wl.wrap_text(frame["desc"] or "（无）", wl.get_font(13), box.width - 24)
        yy = box.top + 30
        for line in lines[:4]:
            self.screen.blit(wl.get_font(13).render(line, True, C_TEXT), (box.left + 12, yy))
            yy += 19

    def _draw_battle(self, box, battle):
        """交战那块：攻守两名武将当前的血条，名字跟棋盘上的一方同色。"""
        pg = self.pg
        pg.draw.rect(self.screen, C_BATTLE_BG, box, border_radius=8)
        pg.draw.rect(self.screen, C_ATTACK, box, 2, border_radius=8)
        title = f"⚔ 交战 · {battle.get('place') or ''}"
        self._text(title, 13, C_ATTACK, box.left + 12, box.top + 8, bold=True)
        rows = [("攻", battle.get("atk")), ("守", battle.get("dfd"))]
        gens = {g.name: g for g in self.game.generals}
        yy = box.top + 30
        for label, name in rows:
            g = gens.get(name)
            self._text(label, 12, C_TEXT_DIM, box.left + 12, yy)
            if g is None:
                yy += 24
                continue
            self._text(g.name, 13, PLAYER_COLOR[g.owner], box.left + 34, yy, bold=True)
            bar = pg.Rect(box.left + 110, yy + 4, box.right - 130 - box.left, 9)
            pg.draw.rect(self.screen, (20, 20, 24), bar, border_radius=4)
            cap = max(1, g.stamina_cap)
            ratio = max(0.0, min(1.0, g.stamina / cap))
            fill = bar.copy()
            fill.width = max(1, int(bar.width * ratio))
            # 三档配色跟 warlord.Battle.draw_stamina_bar 一致（见 C_HP_*），免得同一名
            # 武将在复盘里和在局中显示成两个颜色
            color = C_HP_HI if ratio > 0.5 else C_HP_MID if ratio > 0.25 else C_HP_LO
            pg.draw.rect(self.screen, color, fill, border_radius=4)
            self._text(str(g.stamina), 12, C_TEXT, bar.right + 6, yy)
            yy += 24
        return box.bottom

    def _recent(self, n):
        out, j = [], self.i - 1
        while j >= 0 and len(out) < n:
            f = self.frames[j]
            if f["desc"] and f["do"] != "hold" and f["k"] in ("act", "battle", "grain", "end"):
                out.append(f["desc"])
            j -= 1
        return list(reversed(out))

    def _scrubber(self, bar, total):
        pg = self.pg
        pg.draw.rect(self.screen, (48, 52, 64), bar, border_radius=3)
        fill = bar.copy()
        fill.width = max(2, int(bar.width * self.i / total))
        pg.draw.rect(self.screen, C_SELECT, fill, border_radius=3)


def play(path):
    """开一个窗口，把这份录像在棋盘上一帧一帧放出来；返回进程退出码。

    先把录像读好、帧全摊开，再碰 pygame——文件读不了 / 不是录像时，不必先弹一遍
    pygame 的开场白才报错。
    """
    try:
        timeline = _Timeline(_load(path))
    except OSError as exc:
        print(f"读不到 {path}：{exc}")
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"{path}：读不出这份录像（{exc}）")
        return 1
    try:
        import pygame
        import warlord
    except ImportError as exc:                     # 没装 pygame 也还能看文字复盘
        print(f"图形复盘需要 pygame（{exc}）。")
        print("不想装的话，纯文字复盘一样能看：python replay.py 录像文件")
        return 1
    try:
        app = _Gui(timeline, Path(path), pygame, warlord)
    except pygame.error as exc:                    # 比如机器上没有可用的显示
        print(f"开不了图形复盘：{exc}")
        return 1
    return app.run()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="看一份对局录像（JSONL）的复盘。")
    parser.add_argument("path", help="录像文件路径（replays/ 下那些 .jsonl）")
    parser.add_argument("--brief", action="store_true", help="只看每回合快照和终局")
    parser.add_argument("--gui", action="store_true",
                        help="开窗口在棋盘上一步步看（← → 前后翻一步，不进交战屏）")
    args = parser.parse_args()
    if args.gui:
        raise SystemExit(play(args.path))
    view(args.path, brief=args.brief)


if __name__ == "__main__":
    main()
