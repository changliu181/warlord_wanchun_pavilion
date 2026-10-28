# -*- coding: utf-8 -*-
"""
三国战棋 —— 10x10 格子地图上的回合制对战小游戏（Pygame）

游戏本体：武将、对局规则、交战结算、绘制与主循环。constants.py 常量与配色，
config.py 解析 map.txt / generals.txt，stratagems.py 管计谋，ai.py 是电脑对手。

装好 pygame 后 python warlords.py 即可运行；规则、操作与界面的说明见 README.md。
"""

import math
import random
import sys
from collections import deque
from pathlib import Path

import pygame

from ai import ai_pump
from config import ConfigError, neighbors, parse_map, parse_roster, pick_spawns
from constants import (
    AI, AI_STEP_FRAMES, AI_VERSION_NOTE, AI_VERSION_ORDER,
    ATK_RETREAT_COST, ATTACK_COST, BOARD, CELL, CELL_CAPACITY,
    CITY, CITY_DEFENSE_MULT, DEFAULT_AI_VERSION,
    DEPLOY_COUNT, FACTION_SHORT, FPS, GRAIN_EAT_MAX, GRAIN_EVERY_TURNS, GRAIN_HEAL,
    GRAIN_PER_CITY, GRAIN_PER_STONE, GRAIN_TURN_FIRST, GRID, GeneralSpec, HUMAN,
    LOSS_MAX_PER_MIGHT, MAP_FILE, MARGIN,
    MAX_STAMINA, MOVE_POINTS, OBSTACLE, P1, P2, PLAYER_COLOR, PLAYER_COLOR_DARK,
    PLAYER_NAME, REGEN, REPLAY, RETREAT_MAX, ROSTER_FILE, SCORE_GENERAL_BASE, SCORE_GRAIN,
    SCORE_TERRAIN, SIDEBAR, STRATAGEM_MOVE_COST, STRATAGEM_RANGE, STRATAGEM_STAMINA_COST,
    TERRAIN_NAME, TURN_LIMIT, WIN_H, WIN_W,
    C_ATTACK, C_BG, C_BTN, C_BTN_HOVER, C_CITY, C_CITY_ALT, C_CUNNING, C_CUNNING_BG,
    C_CUNNING_DIM, C_GOLD, C_GRAIN, C_GRAIN_BG,
    C_GRAIN_DIM, C_GRID, C_OBSTACLE, C_OBSTACLE_LINE, C_PANEL, C_PLACE, C_PLACE_CITY,
    C_PLAIN, C_PLAIN_ALT, C_SELECT, C_TEXT, C_TEXT_DIM, C_WARN,
)
import stratagems
from replay import Recorder


def round_half_up(value):
    """四舍五入取整。

    不能用内置 round：它是"银行家舍入"（round(2.5) == 2），而守城加成乘 1.2 再除 1.2
    要靠两次取整精确还原，必须统一按"见五进一"。
    """
    return int(math.floor(value + 0.5))

# --------------------------------------------------------------------------
# 字体工具（需要 pygame）
# --------------------------------------------------------------------------
_font_cache = {}


def get_font(size, bold=False):
    """挑一个能显示中文的字体（macOS / Windows / Linux 常见字体依次尝试）。"""
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for name in ("PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti",
                 "Songti SC", "Arial Unicode MS", "Microsoft YaHei", "SimHei",
                 "Noto Sans CJK SC", "WenQuanYi Micro Hei"):
        path = pygame.font.match_font(name, bold=bold)
        if path:
            font = pygame.font.Font(path, size)
            break
    else:
        font = pygame.font.SysFont(None, size)
    _font_cache[key] = font
    return font


def fit_font(text, max_width, start=14, min_size=8, bold=True):
    """挑一个能把 text 塞进 max_width 的字号：从 start 往下试，都不行就用 min_size。"""
    for size in range(start, min_size - 1, -1):
        font = get_font(size, bold=bold)
        if font.size(text)[0] <= max_width:
            return font
    return get_font(min_size, bold=bold)

def wrap_text(text, font, max_width):
    """按像素宽度粗略折行（中文逐字）。"""
    lines, cur = [], ""
    for ch in text:
        if font.size(cur + ch)[0] > max_width and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------
# 武将
# --------------------------------------------------------------------------
class General:
    def __init__(self, name, might, intellect, owner, row, col, stamina=None):
        self.name = name
        self.might = might                # 武力（固定）
        self.intellect = intellect        # 智力（固定）
        self.max_stamina = MAX_STAMINA
        self.stamina = MAX_STAMINA if stamina is None else stamina
        self.owner = owner
        self.row = row
        self.col = col
        self.alive = True
        self.move_points = MOVE_POINTS    # 本回合剩余移动力，每走相邻一格 -1（进攻也 -1）
        self.move_debt = 0                # 本回合守方撤退累计了几格 = 下回合要还的行动力
        self.city_boost = False           # 守城加成是否生效（只在一次交战期间为 True）

    @property
    def pos(self):
        return (self.row, self.col)

    @property
    def exhausted(self):
        """本回合移动力已用尽，不能再走也不能交战。"""
        return self.move_points <= 0

    def reset_turn(self):
        """回合开始重置移动力：先扣掉上回合**守方撤退**透支的部分，不够扣就是 0。"""
        self.move_points = max(0, MOVE_POINTS - self.move_debt)
        self.move_debt = 0

    @property
    def retreat_room(self):
        """**守方**这一回合还允许再撤几格（累计撤退格数封顶 MOVE_POINTS）。

        超额度的落脚点不算合法，不会列出来；攻方撤退不走这条账（见 can_retreat）。
        """
        return max(0, MOVE_POINTS - self.move_debt)

    def add_move_debt(self, steps):
        """记一笔**守方**撤退透支，下回合要还（调用方须先确认 steps 不超 retreat_room）。"""
        self.move_debt += steps

    # ---------------- 体力 ----------------
    @property
    def stamina_cap(self):
        """当前的体力上限：守城加成期间临时抬高，回血和显示都以它为准。"""
        if self.city_boost:
            return round_half_up(self.max_stamina * CITY_DEFENSE_MULT)
        return self.max_stamina

    def apply_city_boost(self):
        """守城加成：开打前把体力抬高 CITY_DEFENSE_MULT 倍（四舍五入）。"""
        if self.city_boost:
            return
        self.stamina = round_half_up(self.stamina * CITY_DEFENSE_MULT)
        self.city_boost = True

    def drop_city_boost(self):
        """加成解除：打完压回原比例（四舍五入）。乘除各取整一次，0-120 内可精确还原。"""
        if not self.city_boost:
            return
        self.stamina = min(self.max_stamina,
                           round_half_up(self.stamina / CITY_DEFENSE_MULT))
        self.city_boost = False

    def add_stamina(self, amount):
        self.stamina = max(0, min(self.stamina_cap, self.stamina + amount))


# --------------------------------------------------------------------------
# 移动前的「带多少粮草」弹窗
# --------------------------------------------------------------------------
class GrainPick:
    """出发点堆着粮草时，走之前问一句「搬多少过去」（只问人类玩家，电脑由 ai.py 算）。

    取消就当没走过这一步。搬运没有上限，固定几个按钮不够用，
    所以做成可增减的步进控件。
    """

    BOX_W, BOX_H = 468, 262
    BTN_W, BTN_H, BTN_GAP = 100, 34, 12
    ROW_TOP, ROW_PITCH = 168, 44

    def __init__(self, game, gen, dest, steps):
        self.game = game
        self.gen = gen
        self.dest = dest
        self.steps = steps
        self.origin = gen.pos
        self.available = game.grain_at(*gen.pos)
        self.amount = self.available          # 默认全搬过去（最常见的一种搬法）

    # ---------------- 操作 ----------------
    def box_rect(self):
        return pygame.Rect(MARGIN + (BOARD - self.BOX_W) // 2,
                           MARGIN + (BOARD - self.BOX_H) // 2,
                           self.BOX_W, self.BOX_H)

    def button_rect(self, index):
        """按钮位置：一行 4 个，共两行，整体在弹窗里居中。"""
        box = self.box_rect()
        row, col = divmod(index, 4)
        total = 4 * self.BTN_W + 3 * self.BTN_GAP
        return pygame.Rect(box.centerx - total // 2 + col * (self.BTN_W + self.BTN_GAP),
                           box.top + self.ROW_TOP + row * self.ROW_PITCH,
                           self.BTN_W, self.BTN_H)

    def buttons(self):
        """(文字, 动作)；动作里的 ± 只改数量，确定才真的落子。"""
        return [("－10", "sub10"), ("－1", "sub1"), ("＋1", "add1"), ("＋10", "add10"),
                ("不带", "none"), ("全带", "all"), ("确定", "ok"), ("取消", "cancel")]

    def adjust(self, delta):
        """改动携带数，夹在 [0, 此地存量] 之间。"""
        self.amount = max(0, min(self.available, self.amount + delta))

    def inert(self, action):
        """这个按钮现在按下去没用（到头了），画得暗一点。"""
        if action in ("sub10", "sub1", "none"):
            return self.amount <= 0
        if action in ("add1", "add10", "all"):
            return self.amount >= self.available
        return False

    def act(self, action):
        game = self.game
        if action in ("sub10", "sub1", "add1", "add10"):
            self.adjust({"sub10": -10, "sub1": -1, "add1": 1, "add10": 10}[action])
        elif action == "none":
            self.amount = 0
        elif action == "all":
            self.amount = self.available
        elif action == "cancel":
            game.grain_pick = None            # 当没走过，武将继续选着
        elif action == "ok":
            steps = game.compute_reachable(self.gen).get(self.dest)
            game.grain_pick = None
            if steps is not None:             # 兜底：局面没变就该算得出来
                game.walk(self.gen, self.dest[0], self.dest[1], steps, self.amount)

    def handle_click(self, pos):
        for i, (_label, action) in enumerate(self.buttons()):
            if self.button_rect(i).collidepoint(pos):
                self.act(action)
                return

    def primary(self):
        """回车 / 空格 = 确定（数量已经调好了，直接走）。"""
        self.act("ok")

    # ---------------- 绘制 ----------------
    def draw(self, screen, mouse_pos):
        veil = pygame.Surface((BOARD, BOARD), pygame.SRCALPHA)
        veil.fill((8, 8, 12, 176))            # 压暗棋盘，注意力落到弹窗上
        screen.blit(veil, (MARGIN, MARGIN))
        box = self.box_rect()
        pygame.draw.rect(screen, C_PANEL, box, border_radius=12)
        pygame.draw.rect(screen, C_GRAIN, box, 2, border_radius=12)

        cx = box.centerx
        title = get_font(22, bold=True).render("携带粮草", True, C_GRAIN)
        screen.blit(title, title.get_rect(midtop=(cx, box.top + 14)))
        line = get_font(13).render(
            f"{self.gen.name} 从 {self.game.describe(*self.origin)} 出发", True, C_TEXT)
        screen.blit(line, line.get_rect(midtop=(cx, box.top + 48)))
        line = get_font(13).render(
            f"此地共 {self.available} {GRAIN_PER_STONE}，搬到目的地多少？", True, C_TEXT_DIM)
        screen.blit(line, line.get_rect(midtop=(cx, box.top + 70)))

        big = get_font(32, bold=True).render(f"{self.amount} {GRAIN_PER_STONE}", True, C_GRAIN)
        screen.blit(big, big.get_rect(midtop=(cx, box.top + 94)))

        note = get_font(11).render(
            "带过去的会卸在目的地（武将身上不存粮）；搬多少都不影响移动力",
            True, C_GRAIN_DIM)
        screen.blit(note, note.get_rect(midtop=(cx, box.top + 142)))

        for i, (label, action) in enumerate(self.buttons()):
            rect = self.button_rect(i)
            hover = rect.collidepoint(mouse_pos) and not self.inert(action)
            key = (action == "ok")
            base = C_GOLD if key else (C_BTN_HOVER if hover else C_BTN)
            pygame.draw.rect(screen, base, rect, border_radius=8)
            pygame.draw.rect(screen, (86, 94, 112), rect, 1, border_radius=8)
            color = (20, 22, 28) if key else (C_TEXT if not self.inert(action) else (110, 116, 130))
            txt = get_font(14, bold=True).render(label, True, color)
            screen.blit(txt, txt.get_rect(center=rect.center))


# --------------------------------------------------------------------------
# 计谋的界面：先挑计谋（CunningPick），再在棋盘上点目标（CunningAim）
# --------------------------------------------------------------------------
# 四条计谋各自"现在使不出来"的原因；点不动的时候总得说清楚为什么
_NO_TARGET_HINT = {
    "sneak": "身边没有可跃的障碍（要隔一格障碍、正对面能落脚）",
    "lure": f"{STRATAGEM_RANGE} 格内没有调得动的敌将（智力差不够，或他无路可调）",
    "infight": f"{STRATAGEM_RANGE} 格内没有能挑拨的敌将（他边上凑不出第二个人）",
    "raid": "场上没有别处的粮草可劫",
}


def cunning_block_reason(gen):
    """现在为什么打不开用计弹窗；能开就返回 None。

    只卡代价这两样，不卡"四条计谋有没有目标"——后者是弹窗里逐条说的。
    """
    if gen is None or not gen.alive:
        return "先选一名己方武将"
    if gen.move_points < STRATAGEM_MOVE_COST:
        return f"行动力不够（用计要 {STRATAGEM_MOVE_COST} 点）"
    if gen.stamina <= STRATAGEM_STAMINA_COST:
        return f"体力不够（用计要 10 点以上，现在只剩 {gen.stamina}）"
    return None


class CunningPick:
    """用计弹窗：四条计谋各占一行，写明成功率或"使不出来"的原因。"""

    BOX_W, BOX_H = 566, 394
    ROW_TOP, ROW_H = 92, 62
    ROW_GAP = 6

    def __init__(self, game, gen):
        self.game = game
        self.gen = gen
        self.rows = []
        self.refresh()

    def refresh(self):
        """把四条计谋各自的目标和说明算好（弹窗期间局面不会变，所以只算这一遍）。"""
        self.rows = []
        for spec in stratagems.STRATAGEMS:
            found = stratagems.targets(self.game, self.gen, spec.key)
            self.rows.append((spec.key, spec.name, spec.blurb,
                              self._status(spec.key, found), bool(found)))

    def _status(self, key, found):
        """这一行右边那截说明：有目标就报成功率，没有就报为什么使不出来。"""
        if not found:
            return _NO_TARGET_HINT[key]
        game, gen = self.game, self.gen
        if key == "sneak":                      # 暗度陈仓不看目标，成功率是个定数
            odds = f"{stratagems.chance_of(game, gen, key, next(iter(found.values()))):.0%}"
            return f"成功率 {odds}　·　{len(found)} 个落点"
        rates = [stratagems.chance_of(game, gen, key, target) for target in found.values()]
        lo, hi = min(rates), max(rates)
        odds = f"{lo:.0%}" if hi - lo < 1e-9 else f"{lo:.0%}～{hi:.0%}"
        unit = "堆粮" if key == "raid" else "个目标"
        return f"成功率 {odds}　·　{len(found)} {unit}"

    # ---------------- 操作 ----------------
    def box_rect(self):
        return pygame.Rect(MARGIN + (BOARD - self.BOX_W) // 2,
                           MARGIN + (BOARD - self.BOX_H) // 2,
                           self.BOX_W, self.BOX_H)

    def row_rect(self, index):
        box = self.box_rect()
        return pygame.Rect(box.left + 14, box.top + self.ROW_TOP + index * (self.ROW_H + self.ROW_GAP),
                           box.width - 28, self.ROW_H)

    def cancel_rect(self):
        box = self.box_rect()
        return pygame.Rect(box.centerx - 70, box.bottom - 46, 140, 32)

    def pick(self, index):
        """选中第 index 条计谋（点得动的才作数）。"""
        key, _name, _blurb, _status, usable = self.rows[index]
        if not usable:
            return
        self.game.start_cunning(key)

    def handle_click(self, pos):
        if self.cancel_rect().collidepoint(pos):
            self.game.cunning = None
            return
        for i in range(len(self.rows)):
            if self.row_rect(i).collidepoint(pos):
                self.pick(i)
                return

    def handle_key(self, key):
        """1~4 选计谋，ESC / 回车之外什么都不接。"""
        index = {pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2, pygame.K_4: 3,
                 pygame.K_KP1: 0, pygame.K_KP2: 1, pygame.K_KP3: 2, pygame.K_KP4: 3}.get(key)
        if index is not None:
            self.pick(index)
            return True
        if key == pygame.K_ESCAPE:
            self.game.cunning = None
            return True
        return False

    # ---------------- 绘制 ----------------
    def draw(self, screen, mouse_pos):
        veil = pygame.Surface((BOARD, BOARD), pygame.SRCALPHA)
        veil.fill((8, 8, 12, 150))            # 比粮草那个淡一点：挑计谋时得看得见棋盘
        screen.blit(veil, (MARGIN, MARGIN))
        box = self.box_rect()
        pygame.draw.rect(screen, C_PANEL, box, border_radius=12)
        pygame.draw.rect(screen, C_CUNNING, box, 2, border_radius=12)

        title = get_font(21, bold=True).render(f"用计 · {self.gen.name}", True, C_CUNNING)
        screen.blit(title, title.get_rect(midtop=(box.centerx, box.top + 14)))
        cost = get_font(12).render(
            f"智力 {self.gen.intellect}　·　代价 {STRATAGEM_MOVE_COST} 点行动力 + "
            f"{STRATAGEM_STAMINA_COST} 点体力（选定目标时结算，成没成都照付）",
            True, C_TEXT_DIM)
        screen.blit(cost, cost.get_rect(midtop=(box.centerx, box.top + 44)))
        line = get_font(11).render(
            f"现在：行动力 {self.gen.move_points}/{MOVE_POINTS}　体力 {self.gen.stamina}",
            True, C_TEXT_DIM)
        screen.blit(line, line.get_rect(midtop=(box.centerx, box.top + 66)))

        for i, (_key, name, blurb, status, usable) in enumerate(self.rows):
            rect = self.row_rect(i)
            hover = rect.collidepoint(mouse_pos) and usable
            pygame.draw.rect(screen, C_CUNNING_BG if (hover or usable) else (38, 38, 46),
                             rect, border_radius=8)
            pygame.draw.rect(screen, C_CUNNING if usable else (62, 62, 72),
                             rect, 1, border_radius=8)
            num = get_font(13, bold=True).render(str(i + 1), True,
                                                 C_CUNNING if usable else C_CUNNING_DIM)
            screen.blit(num, num.get_rect(center=(rect.left + 20, rect.centery)))
            name_color = C_TEXT if usable else (122, 124, 136)
            screen.blit(get_font(15, bold=True).render(name, True, name_color),
                        (rect.left + 40, rect.top + 8))
            screen.blit(get_font(11).render(blurb, True, C_TEXT_DIM),
                        (rect.left + 40, rect.top + 32))
            # 右边那截：成功率亮一点，使不出来的原因用计谋自己的暗紫色
            color = C_CUNNING if usable else C_CUNNING_DIM
            font = fit_font(status, rect.width - 240, start=12, min_size=9, bold=False)
            screen.blit(font.render(status, True, color),
                        font.render(status, True, color).get_rect(
                            midright=(rect.right - 12, rect.centery)))

        rect = self.cancel_rect()
        hover = rect.collidepoint(mouse_pos)
        pygame.draw.rect(screen, C_BTN_HOVER if hover else C_BTN, rect, border_radius=8)
        pygame.draw.rect(screen, (86, 94, 112), rect, 1, border_radius=8)
        txt = get_font(13, bold=True).render("取消 (ESC)", True, C_TEXT)
        screen.blit(txt, txt.get_rect(center=rect.center))


class CunningAim:
    """用计的瞄准态：挑完计谋之后，在棋盘上点目标。

    三个小状态：target 点这一计的对象（**代价在这一刻结算**）；partner 是内讧得手后
    点第二名敌将；lure 是调虎离山得手后挑落脚点。后两个按 ESC 不生效（已经成了）。
    """

    def __init__(self, game, gen, key):
        self.game = game
        self.gen = gen
        self.key = key
        self.state = "target"
        self.foe = None                   # 中计的敌将（调虎离山 / 内讧）
        self.steps = 0                    # 调虎离山掷出来的调动格数
        self.options = {}
        self.refresh()

    # ---------------- 状态 ----------------
    def refresh(self):
        """按当前状态重算可点的目标 -> {(行,列): 载荷}。"""
        if self.state == "target":
            self.options = stratagems.targets(self.game, self.gen, self.key)
        elif self.state == "partner":
            self.options = stratagems.infight_partners(self.game, self.foe)
        elif self.state == "lure":
            self.options = stratagems.lure_dests(self.game, self.foe, self.steps)

    def highlight(self):
        """棋盘上要高亮的格子。"""
        return set(self.options)

    def hint(self):
        """提示条那一句话。"""
        name = stratagems.name_of(self.key)
        if self.state == "target":
            what = {"sneak": "点一个能落下的格子（跃过紧邻的障碍）",
                    "lure": f"点一名 {STRATAGEM_RANGE} 格内的敌将",
                    "infight": f"点一名 {STRATAGEM_RANGE} 格内的敌将",
                    "raid": "点一处有粮草的格子"}[self.key]
            lead = f"「{name}」：{what}"
        elif self.state == "partner":
            lead = f"「{name}」得手：再点一名敌将（和 {self.foe.name} 同格或相邻），让他俩打起来"
        else:
            lead = (f"「{name}」得手：{self.foe.name} 会被牵着走 {self.steps} 格以内，"
                    f"点一个落脚点")
        tail = "　·　ESC 收手" if self.state == "target" else "　·　这一计已经成了，必须选一个"
        return lead + tail

    # ---------------- 点棋盘 ----------------
    def handle_click(self, row, col):
        cell = (row, col)
        if cell not in self.options:
            return
        if self.state == "target":
            self._aim(cell, self.options[cell])
        elif self.state == "partner":
            partner = stratagems.pick_partner(self.options[cell])
            stratagems.apply_infight(self.game, self.foe, partner)
            self._done(f"{self.foe.name} 与 {partner.name} 自相残杀")
        elif self.state == "lure":
            steps = self.options[cell]
            stratagems.apply_lure(self.game, self.foe, cell, steps)
            self._done(f"{self.foe.name} 被调往 {self.game.describe(*cell)}")

    def _aim(self, cell, target):
        """点定了目标：先结代价，再掷骰，最后按计谋各自的效果走。"""
        game, gen, key = self.game, self.gen, self.key
        if not stratagems.cost_ok(gen):                  # 兜底：局面变了就使不出来
            self._abort("代价不够了")
            return
        stratagems.pay(gen)
        rate = stratagems.chance_of(game, gen, key, target)
        roll = game.rng.random()
        name = stratagems.name_of(key)
        got = roll < rate
        game.push_log(f"🎴 {gen.name} 对 {game.describe(*cell)} 用「{name}」："
                      f"成功率 {rate * 100:.0f}%，掷出 {roll * 100:.0f}"
                      + ("—— 得手！" if got else "—— 失手。"))
        if game.recorder is not None:
            game.recorder.act("strat", by=gen.name, owner=gen.owner, key=key, name=name,
                              cell=[cell[0], cell[1]], rate=rate, roll=roll, got=got)
        if not got:
            self._done(f"{name}失手")
            return
        if key == "sneak":
            stratagems.apply_sneak(game, gen, cell)
            self._done(f"暗度陈仓得手，{gen.name} 跃入 {game.describe(*cell)}")
        elif key == "raid":
            amount = stratagems.apply_raid(game, gen, cell)
            self._done(f"劫粮得手，搬回 {amount} 石")
        elif key == "infight":
            self._to_partner(target)
        elif key == "lure":
            self._to_lure(target)

    def _to_partner(self, target):
        """内讧掷中了：转到"挑第二个人"。"""
        self.foe = target.foe
        self.state = "partner"
        self.refresh()
        if not self.options:                             # 选目标时就滤过了，这里只是兜底
            self._done("内讧落空：边上没人可挑拨")

    def _to_lure(self, target):
        """调虎离山掷中了：先定这次能调几格，再让用计方挑落脚点。"""
        game, gen = self.game, self.gen
        self.foe = target.foe
        cap = stratagems.lure_move_cap(gen.intellect, target.foe_int)
        self.steps = game.rng.randint(1, cap) if cap >= 1 else 0
        self.state = "lure"
        self.refresh()
        if not self.options:                             # 掷出来的格数太小，走不到任何落脚点
            game.push_log(f"🎴 {self.foe.name} 中了计却无处可去（只调 {self.steps} 格），"
                          f"这一计白费。")
            self._done("调虎离山扑空：敌人无处可去")

    def cancel(self):
        """ESC：只有还没扣代价的 target 阶段能收手。"""
        if self.state != "target":
            return False
        self.game.aim = None
        return True

    # ---------------- 收场 ----------------
    def _abort(self, text):
        self.game.aim = None
        self.game.flash(text)

    def _done(self, banner):
        """计谋结算完毕：关掉瞄准态，刷新选中武将的可走范围，报一句。"""
        game, gen = self.game, self.gen
        game.aim = None
        game.check_victory()                             # 内讧可能打死人
        if game.recorder is not None:
            game.recorder.act("strat_effect", detail=banner)   # 计谋的落点（成没成都记）
        if gen.alive:
            game.select(gen)                             # 行动力变了，可走范围跟着变
        else:
            game.clear_selection()
        game.flash(banner)

    # ---------------- 绘制 ----------------
    def draw(self, screen, mouse_pos):
        """棋盘下沿那条提示条（棋盘上的高亮由 draw_board 一起画）。"""
        text = self.hint()
        font = fit_font(text, BOARD - 40, start=14, min_size=10, bold=False)
        surf = font.render(text, True, (238, 230, 250))
        box = surf.get_rect(midbottom=(MARGIN + BOARD // 2, MARGIN + BOARD - 14)).inflate(28, 14)
        bg = pygame.Surface(box.size, pygame.SRCALPHA)
        bg.fill((22, 16, 32, 214))
        screen.blit(bg, box.topleft)
        pygame.draw.rect(screen, C_CUNNING, box, 2, border_radius=8)
        screen.blit(surf, surf.get_rect(center=box.center))


# --------------------------------------------------------------------------
# 游戏主体
# --------------------------------------------------------------------------
class Game:
    # 侧边栏「武将总览」的版面：栏高 = 标题 + 可见行数 * 行高
    ROSTER_TITLE_H = 18     # 一方标题（玩家名）那一行
    ROSTER_ROW_H = 17       # 每名武将一行
    ROSTER_COL_GAP = 12     # 两栏之间的间距
    # 一屏最多显示几名武将，超过就变成可滚动窗口：调大要掂量，人一多就会挤掉战报
    ROSTER_MAX_ROWS = 7

    # 侧边栏「选中武将」详情框的版面（右边并排一块同样高的「比分板」）
    INFO_COL_GAP = 12       # 详情框与比分板之间的间距（两者平分侧边栏宽度）
    INFO_BOX_H = 146        # 框高：4 行文字 + 两行按钮（吃粮 / 用计）
    INFO_BTN_TOP = 90       # 第一排按钮（吃粮）的 y（相对框顶）
    INFO_BTN_W = 50         # 「吃粮」按钮宽
    INFO_BTN_H = 22
    INFO_BTN_GAP = 6
    INFO_BTN2_TOP = 116     # 第二排按钮（用计）的 y（相对框顶）

    # 比分板的版面：标题 + 阵营行 + 总分/将领分/地盘分三行
    SCORE_ROW_TOP = 50      # 第一行数字的 y（相对框顶）
    SCORE_ROW_PITCH = 19
    SCORE_LABEL_W = 54      # 左边名目那一列（总分 / 将领分 / 地盘分）

    SIDEBAR_X = MARGIN * 2 + BOARD   # 侧边栏左边界

    def __init__(self, seed=None):
        self.seed = seed                  # 记住种子：录像头部要写它（按 R 重开也沿用同一个）
        self.rng = random.Random(seed)
        self.players = 2                  # 1 = 人机对战，2 = 同机对战
        self.human_side = P1              # 一人游玩时人类执哪一边
        self.controllers = {P1: HUMAN, P2: HUMAN}
        # 每一方的电脑用哪个版本（`梅` / `兰` / `竹` / `菊`，见 constants.AI_VERSIONS）。
        # 两边可以不一样——菊对梅就是靠这一条；人类那一边填了也没人看。
        self.ai_versions = {P1: DEFAULT_AI_VERSION, P2: DEFAULT_AI_VERSION}
        self.recorder = None              # 录像（replay.Recorder）；没开录时是 None
        self.new_game()

    @classmethod
    def for_replay(cls):
        """建一个**不读配置**的空壳 Game，只用来画复盘（replay.py 的图形复盘）。

        正常开局的 Game 会去读 map.txt / generals.txt，可复盘不同：棋盘、地名、武将
        全部来自**录像头部**，跟当前的配置文件毫无关系。所以这里绕开 `__init__`，只把
        "绘制路径要用到"的那些属性按下面的约定摆好；调用方（`replay._Gui`）随后把
        `grid` / `places` 换成录像里的、并把每帧的 `generals` / `grain` / `turn` 等灌进来。

        绘制契约（改了下面这些的读法，就得同步改 replay.py 的 `_Gui._sync`）：
        `grid`、`places` 是地图；每帧要设 `generals`（General 列表）、`grain`、
        `turn`、`current`、`over`、`winner`；`draw_board` 还会读 `selected` / `aim` /
        `reachable` / `attackable`，`draw_score_board` 会读 `generals` + `grain`。
        """
        game = cls.__new__(cls)
        game.grid = []                    # 调用方（replay._Gui）随后换成录像头部的地图
        game.places = {}
        game.roster = {P1: [], P2: []}
        game.spawns = {}
        game.generals = []
        game.current = P1
        game.turn = 1
        game.selected = None
        game.reachable = {}
        game.attackable = set()
        game.grain = {}
        game.grain_pick = None
        game.cunning = None
        game.aim = None
        game.over = False
        game.winner = None
        game.banner = None
        game.battle = None
        game.log = []
        game.roster_scroll = 0
        game.ai_timer = AI_STEP_FRAMES
        game.players = 2
        game.human_side = P1
        game.seed = None
        game.rng = random.Random()
        game.controllers = {P1: HUMAN, P2: HUMAN}
        game.ai_versions = {P1: DEFAULT_AI_VERSION, P2: DEFAULT_AI_VERSION}
        game.recorder = None
        return game

    # ---------------- 开局 ----------------
    def new_game(self):
        # 配置都先读好、校好，再动对局状态；配置有问题时当前对局原样保留
        grid, places = self.load_map()
        roster = self.load_roster()
        spawns = pick_spawns(grid, self.rng)   # 出生点不在地图里，开局随机分
        self.grid, self.roster, self.places, self.spawns = grid, roster, places, spawns
        self.generals = []
        self.current = P1
        self.turn = 1
        self.selected = None
        self.reachable = {}
        self.attackable = set()
        self.log = []
        self.over = False                 # 对局是否已结束（平局也算结束，那时 winner 是 None）
        self.winner = None                # 胜方；None 有可能是"还没结束"，也可能是"平局"
        self.banner = None                # (文本, 剩余帧数)；帧数是 None 表示常驻不消失
        self.battle = None                # 正在进行的交战（Battle），非 None 时是交战界面
        self.grain = {}                   # {(行,列): 石数}；堆在格子上的**公共**粮草，无归属
        self.grain_pick = None            # 移动时问"带多少粮"的弹窗；None = 没在问
        self.cunning = None               # 用计弹窗（CunningPick）；None = 没打开
        self.aim = None                   # 用计的瞄准态（CunningAim）；None = 没在瞄准
        self.roster_scroll = 0            # 武将总览滚到第几行开始显示（人多时才有用）
        self.ai_timer = AI_STEP_FRAMES    # 电脑下一步还要等几帧
        self.push_log("新的一局开始，玩家一先行。")
        self.create_generals()
        self.start_turn(announce=False)

    def start_match(self, human_side=P1, players=2, ai_version=DEFAULT_AI_VERSION):
        """按选定的模式开一局；配置有问题就抛 ConfigError，当前对局原样保留。

        `ai_version` 只给 1P 用——单机时两边（人类那一边也填上，免得从 2P 切回来还留着旧值）
        都记成这个版本，`ai_versions` 就是"某一方在用哪版电脑"的唯一出处。
        """
        self.new_game()                   # 先把新的地图/武将读好，失败就不会动到控制器
        self.players = players
        self.human_side = human_side
        if players == 1:
            self.controllers = {human_side: HUMAN, 1 - human_side: AI}
            self.ai_versions = {P1: ai_version, P2: ai_version}
            side = FACTION_SHORT[human_side]
            self.push_log(f"人机对战：你执{side}，电脑执另一边（{ai_version}版）。")
        else:
            self.controllers = {P1: HUMAN, P2: HUMAN}
            self.ai_versions = {P1: DEFAULT_AI_VERSION, P2: DEFAULT_AI_VERSION}
        self.ai_timer = AI_STEP_FRAMES
        self.start_recording()            # 有电脑参与就自动记一份录像（见 REPLAY）

    def start_recording(self, path=None):
        """开一份录像（有电脑参与才开）；已经有一份就静默跳过，免得连开两局时重复写。

        录像总是在**第 1 回合刚开局**时开（`start_match` 里，或迟一点到第一次 `ai_pump`），
        所以这里顺手补一条当前回合的快照——否则第 1 回合的动作会没有抬头。
        """
        if REPLAY and self.recorder is None and AI in self.controllers.values():
            self.recorder = Recorder(self, path)
            self.record_turn()

    def record_turn(self):
        """开局时记一条回合快照（棋盘与交战屏共用）。录像没开就是空操作。"""
        if self.recorder is not None:
            self.recorder.turn(self)


    def set_ai_versions(self, p1_version=None, p2_version=None):
        """指定某几方的电脑版本（没传的那一边不动）。对战脚本用它摆"菊对梅"这种局面。"""
        for player, version in ((P1, p1_version), (P2, p2_version)):
            if version is not None:
                self.ai_versions[player] = version

    def ai_version(self, player=None):
        """这一方的电脑是哪个版本（不传就是当前行动方）。

        交战屏上要用**做决定那一方**的版本，所以一律显式传 owner，别让它默认跟着 current 走。
        """
        return self.ai_versions[player if player is not None else self.current]

    def is_ai(self, player=None):
        """这一方是不是电脑在操控（不传就是当前行动方）。"""
        return self.controllers[player if player is not None else self.current] == AI

    def human_may_act(self):
        """现在轮到人类做决定吗（电脑思考时一律 False，键盘鼠标都不接）。"""
        if self.battle is not None:
            return self.battle.human_may_act()
        return not self.over and not self.is_ai(self.current)

    def load_map(self, path=None):
        """读地图配置 -> (grid, places)：地形和地名都来自同一个文件。"""
        return parse_map(Path(path) if path else MAP_FILE)

    def load_roster(self, path=None):
        """读武将配置 -> {玩家: [GeneralSpec]}（校验不过抛 RosterConfigError）。"""
        return parse_roster(Path(path) if path else ROSTER_FILE)

    def create_generals(self):
        """每方从本方名册里随机抽 DEPLOY_COUNT 名上阵，落到分好的出生点上（用 self.rng）。"""
        for player in (P1, P2):
            drafted = self.rng.sample(self.roster[player], DEPLOY_COUNT[player])
            for spec, (r, c) in zip(drafted, self.spawns[player]):
                self.generals.append(General(spec.name, spec.might, spec.intellect,
                                             player, r, c))
            self.push_log(f"{PLAYER_NAME[player]}上阵："
                          + "、".join(spec.name for spec in drafted) + "。")

    # ---------------- 基础查询 ----------------
    def generals_at(self, r, c):
        return [g for g in self.generals if g.alive and g.pos == (r, c)]

    def describe(self, r, c):
        """格子的称呼：地名（地形），如「襄阳（城池）」；障碍格没地名，只报地形。"""
        terrain = self.grid[r][c]
        if terrain == OBSTACLE:
            return TERRAIN_NAME[terrain]
        return f"{self.places[(r, c)]}（{TERRAIN_NAME[terrain]}）"

    def can_stop(self, gen, r, c):
        """gen 能不能停在 (r,c)：通路/城池、没有敌方、且己方在那格不满员。"""
        if not self.in_bounds(r, c) or self.grid[r][c] == OBSTACLE:
            return False
        others = [g for g in self.generals_at(r, c) if g is not gen]
        if any(g.owner != gen.owner for g in others):
            return False
        return len(others) < CELL_CAPACITY

    def can_transit(self, gen, r, c):
        """gen 能不能路过 (r,c)：通路/城池、无敌方；己方满了也能穿过（判终态）。"""
        if not self.in_bounds(r, c) or self.grid[r][c] == OBSTACLE:
            return False
        return not any(g.owner != gen.owner for g in self.generals_at(r, c))

    def in_bounds(self, r, c):
        return 0 <= r < GRID and 0 <= c < GRID

    def garrisons(self):
        """{格子: [武将]}，只含活着的。同一格站两名时要分别画出来。"""
        out = {}
        for gen in self.generals:
            if gen.alive:
                out.setdefault(gen.pos, []).append(gen)
        return out

    def compute_reachable(self, gen):
        """BFS 算出该武将本回合能走到的落脚点 -> {pos: 需要花掉的移动力}

        只收录**停得下**的格子：走得到的敌方格归 compute_attackable，
        己方站满两名的那格只能路过、不能停。
        """
        if gen.exhausted:
            return {}
        budget = gen.move_points
        result = {}
        seen = {gen.pos}
        q = deque([(gen.row, gen.col, 0)])
        while q:
            r, c, d = q.popleft()
            if d >= budget:                   # 再走一格就超出移动力了
                continue
            for nr, nc in neighbors(r, c):
                if (nr, nc) in seen or not self.can_transit(gen, nr, nc):
                    continue
                seen.add((nr, nc))
                if self.can_stop(gen, nr, nc):     # 站不下就只当路过，不算落脚点
                    result[(nr, nc)] = d + 1
                q.append((nr, nc, d + 1))
        return result

    def compute_attackable(self, gen):
        """能踏进去交战的格子：相邻的敌方武将格，每个消耗 1 点移动力。"""
        if gen.exhausted:
            return set()
        return {(nr, nc)
                for nr, nc in neighbors(gen.row, gen.col)
                if any(other.owner != gen.owner for other in self.generals_at(nr, nc))}

    # ---------------- 武将总览（可滚动） ----------------
    def roster_rows(self):
        """武将总览：(人多的一边共几行, 一屏看得见几行)；多出来的行靠滚轮翻看。"""
        per_side = max(sum(1 for g in self.generals if g.owner == p) for p in (P1, P2))
        return per_side, min(per_side, self.ROSTER_MAX_ROWS)

    def roster_rect(self, buttons):
        """武将总览那一块的外框（两栏 + 标题）。

        绘制和滚轮的命中判定都走这里，免得两边各算一套位置对不上。
        """
        per_side, visible = self.roster_rows()
        height = self.ROSTER_TITLE_H + visible * self.ROSTER_ROW_H + 8
        limit = min(rect.top for _l, rect, _k in buttons) - 10   # 内容区不得压到按钮上
        return pygame.Rect(self.SIDEBAR_X + 16, limit - height, SIDEBAR - 32, height)

    @property
    def roster_start(self):
        """总览从第几行开始显示（读的时候顺手夹回合法范围，免得人少了留白）。"""
        per_side, visible = self.roster_rows()
        return max(0, min(max(0, per_side - visible), self.roster_scroll))

    def roster_scrollable(self):
        per_side, visible = self.roster_rows()
        return per_side > visible

    def scroll_roster(self, steps):
        """滚动武将总览（滚轮一次一行）；到顶到底就停住。"""
        before = self.roster_start
        per_side, visible = self.roster_rows()
        top = max(0, per_side - visible)
        self.roster_scroll = max(0, min(top, before + steps))
        return self.roster_scroll != before

    # ---------------- 粮草 ----------------
    def grain_at(self, r, c):
        """这一格上堆着的公共粮草（石）；没有就是 0。"""
        return self.grain.get((r, c), 0)

    def add_grain(self, r, c, amount):
        """往格子上加粮草。"""
        if amount > 0:
            self.grain[(r, c)] = self.grain_at(r, c) + amount

    def take_grain(self, r, c, amount):
        """从格子上取粮 -> 实际取到几石（要的比有的多就取到没有为止）。"""
        got = min(amount, self.grain_at(r, c))
        if got <= 0:
            return 0
        left = self.grain_at(r, c) - got
        if left:
            self.grain[(r, c)] = left
        else:
            self.grain.pop((r, c), None)      # 取空了就把这一格从表里删掉
        return got

    def grain_stock(self):
        """全场散落在格子上的粮草总数。"""
        return sum(self.grain.values())

    def grain_turn(self):
        """这一回合是不是"入库回合"。

        即第 GRAIN_TURN_FIRST 回合起，每 GRAIN_EVERY_TURNS 回合来一次。
        """
        return self.turn % GRAIN_EVERY_TURNS == GRAIN_TURN_FIRST % GRAIN_EVERY_TURNS

    def next_grain_turn(self):
        """下一次城池入库在第几回合（入库发生在那个回合的**开头**）。"""
        gap = (GRAIN_TURN_FIRST % GRAIN_EVERY_TURNS - self.turn) % GRAIN_EVERY_TURNS
        return self.turn + (gap or GRAIN_EVERY_TURNS)

    def produce_grain(self):
        """入库回合：每座城池产 GRAIN_PER_CITY 石，堆在城池那一格上；平时什么都不做。"""
        if not self.grain_turn():
            return 0
        made = 0
        for r in range(GRID):
            for c in range(GRID):
                if self.grid[r][c] == CITY:
                    self.add_grain(r, c, GRAIN_PER_CITY)
                    made += GRAIN_PER_CITY
        if made:
            self.push_log(f"⚑ 各城池入库 {made} 石粮草"
                          f"（每 {GRAIN_EVERY_TURNS} 回合一次），"
                          f"散落在野的共 {self.grain_stock()} 石。")
            if self.recorder is not None:
                self.recorder.grain(self, made)
        return made

    def eat_amounts(self, gen):
        """这名武将这一口可以吃几石 -> [1, 2, 3] 的子集（不能吃就是空）。

        三个上限一起卡：GRAIN_EAT_MAX、脚下的粮、剩余移动力（吃 k 石花 k 点行动力）。
        """
        if gen is None or not gen.alive or gen.exhausted:
            return []
        top = min(GRAIN_EAT_MAX, self.grain_at(*gen.pos), gen.move_points)
        return list(range(1, top + 1))

    def eat_grain(self, gen, amount):
        """吃粮：吃 amount 石，回体力并花掉同样多的移动力；这一口吃不下就返回 False。"""
        if amount not in self.eat_amounts(gen):
            return False
        self.take_grain(gen.row, gen.col, amount)
        gen.move_points = max(0, gen.move_points - amount)
        before = gen.stamina
        gen.add_stamina(amount * GRAIN_HEAL)
        gained = gen.stamina - before
        self.push_log(f"🍚 {gen.name} 吃粮 {amount} {GRAIN_PER_STONE}，"
                      f"体力 +{gained}（{before}→{gen.stamina}），"
                      f"剩移动力 {gen.move_points}。")
        if self.recorder is not None:
            self.recorder.act("eat", by=gen.name, owner=gen.owner, amount=amount,
                              hp=[before, gen.stamina], mp=gen.move_points)
        self.select(gen)                  # 移动力变了，可走范围跟着变
        return True

    # ---------------- 玩家操作 ----------------
    def select(self, gen):
        self.selected = gen
        self.reachable = self.compute_reachable(gen) if gen else {}
        self.attackable = self.compute_attackable(gen) if gen else set()

    def clear_selection(self):
        self.selected = None
        self.reachable = {}
        self.attackable = set()

    def handle_board_click(self, row, col):
        # 先看是否走到这儿：落脚点只可能是空格或己方格（can_stop 挡掉了敌方格和本身）
        if self.selected and (row, col) in self.reachable:
            self.move(self.selected, row, col)
            return
        target = self.generals_at(row, col)
        if not target:
            self.clear_selection()
            return
        ours = [g for g in target if g.owner == self.current]
        if ours:
            self.click_own(ours)
            return
        # 点敌人：必须已选中、且相邻（相邻格才能踏进去交战）
        if self.selected and (row, col) in self.attackable:
            self.charge(self.selected, row, col)
        elif self.selected:
            self.push_log("要交战得先走到敌人旁边，从相邻格踏进去。")

    def click_own(self, ours):
        """点自己人：同格站着几名就依次轮换选中，轮完最后一个再点一下就取消选中。

        取消选中是必要的退路——点的格子若是当前武将的落脚点，会被判成"走过去"。
        """
        cycle = ours + [None]                 # 末尾的 None 表示"取消选中"
        pos = cycle.index(self.selected) if self.selected in ours else -1
        self.select(cycle[(pos + 1) % len(cycle)])

    def move(self, gen, row, col, carry=None):
        """走一步（或一次走好几格）：按格数扣移动力，不扣体力。

        步数现算，**不去读 self.reachable**——那是界面缓存，电脑走子也不能要求它
        先把选中态摆对。走不到就什么都不做。

        carry 是这一步从出发格搬走的粮草数（电脑传数字，人操作则传 None，由这里决定）。
        """
        steps = self.compute_reachable(gen).get((row, col))
        if steps is None:
            return False
        if carry is None:
            if not self.is_ai(gen.owner) and self.grain_at(*gen.pos):
                self.grain_pick = GrainPick(self, gen, (row, col), steps)
                return True
            carry = self.grain_at(*gen.pos)
        self.walk(gen, row, col, steps, carry)
        return True

    def walk(self, gen, row, col, steps, carry=0):
        """真正落地：从出发点取粮，走到目的地后**当场卸下**、并入那一格的粮堆。"""
        # 武将只是脚夫、不是粮仓：粮草从不带在身上，所以这一趟的净效果就是挪一格
        orig = gen.pos                        # 出发点：录像里记下从哪走到哪
        picked = self.take_grain(gen.row, gen.col, carry) if carry > 0 else 0
        gen.row, gen.col = row, col
        gen.move_points = max(0, gen.move_points - steps)
        if picked:
            self.add_grain(row, col, picked)      # 卸在目的地，谁站上来都能再取走
        with_grain = (f"，顺路把 {picked} {GRAIN_PER_STONE}粮草搬到 "
                      f"{self.describe(row, col)}" if picked else "")
        self.push_log(f"{gen.name} 移动 {steps} 格至 {self.describe(row, col)}，"
                      f"剩余移动力 {gen.move_points}{with_grain}。")
        if self.recorder is not None:
            self.recorder.act("move", by=gen.name, owner=gen.owner,
                              frm=[orig[0], orig[1]], to=[row, col], steps=steps,
                              mp=gen.move_points, carry=picked)
        self.select(gen)                      # 重新计算剩余可走范围
        # 移动后与敌人相邻，提示一句可以打
        if self.compute_attackable(gen):
            self.push_log(f"{gen.name} 与敌军相邻，可以踏进去交战！")
        return True

    # ---------------- 战斗 ----------------
    def charge(self, attacker, row, col):
        """向相邻的敌方武将格发动进攻：花 ATTACK_COST 点移动力，然后切到交战界面。

        这一笔只是"进攻"的钱：打退守军就地进占，账就结清；半路收手撤退另需
        ATK_RETREAT_COST 点（见 Battle.can_retreat）。交战不结束该武将的行动。
        """
        defenders = self.generals_at(row, col)
        attacker.move_points = max(0, attacker.move_points - ATTACK_COST)
        self.clear_selection()                    # 交战期间棋盘不响应操作
        if self.recorder is not None:             # 先记"进攻"，再建 Battle（它记"交战于"）
            self.recorder.act("charge", by=attacker.name, owner=attacker.owner,
                              cell=[row, col], defenders=[g.name for g in defenders],
                              mp=attacker.move_points)
        self.battle = Battle(self, attacker, defenders)
        self.push_log(f"⚔ {attacker.name} 进攻 {self.describe(row, col)}"
                      f"（守军：{'、'.join(g.name for g in defenders)}）。")

    def end_battle(self):
        """关掉交战界面，回到地图：让进攻方接着行动（前提是他还活着、还有移动力）。"""
        attacker = self.battle.atk
        self.battle = None
        self.check_victory()
        if self.over or not attacker.alive or attacker.exhausted:
            self.clear_selection()
        else:
            self.select(attacker)                 # 还有移动力就继续行动

    # ---------------- 回合流程 ----------------
    def end_turn(self):
        if self.over:                         # 已经分出胜负（含平局）就不再推进回合
            return
        self.clear_selection()
        self.grain_pick = None                # 换回合了，没选完的携带弹窗作废
        self.drop_cunning()                   # 没点完的用计瞄准态也作废
        # 回血算"这一方操作完毕"的结算，放在切边之前，对着刚行动完的一方
        self.regen_side(self.current)
        self.current = 1 - self.current
        self.turn += 1
        if self.turn > TURN_LIMIT:
            self.finish_by_score()
            return
        self.produce_grain()                  # 入库回合（4/9/14/…）才真的产，见 grain_turn
        self.start_turn()
        self.check_victory()

    def regen_side(self, player):
        """回合结束时的自动回血：这一方存活武将各按**当前所在地形**回体力。

        故意放在回合结束而不是下一回合开始：掉的血要撑过对方的整个回合才回。
        """
        for gen in self.generals:
            if gen.alive and gen.owner == player:
                gen.add_stamina(REGEN[self.grid[gen.row][gen.col]])

    def start_turn(self, announce=True):
        """新回合开始：当前方的移动力重置（回血已经在上一回合结束时结算过了）。"""
        for gen in self.generals:
            if gen.alive and gen.owner == self.current:
                gen.reset_turn()
        if announce:                          # 开局第一次不重复播报（前面已说"玩家一先行"）
            self.push_log(f"--- 第 {self.turn} 回合，轮到 {PLAYER_NAME[self.current]} ---")
        self.record_turn()                    # 录像：这一回合开始时的全盘快照

    def check_victory(self):
        alive1 = [g for g in self.generals if g.alive and g.owner == P1]
        alive2 = [g for g in self.generals if g.alive and g.owner == P2]
        if not alive1 and not alive2:
            self.set_winner(None, "两败俱伤，平局！")
        elif not alive1:
            self.set_winner(P2, f"{PLAYER_NAME[P2]} 全歼敌军，获胜！")
        elif not alive2:
            self.set_winner(P1, f"{PLAYER_NAME[P1]} 全歼敌军，获胜！")

    def finish_by_score(self):
        """回合用尽：按总分（将领分 + 控制地域分）判胜负，双方各报一次账。"""
        board, _contested = self.scoreboard()
        s1, s2 = board[P1][0], board[P2][0]
        self.push_log("回合用尽，结算比分：" + "；".join(
            self.score_text(player, board) for player in (P1, P2)) + "。")
        if s1 > s2:
            self.set_winner(P1, f"回合用尽，{PLAYER_NAME[P1]} 以 {s1}:{s2} 获胜！")
        elif s2 > s1:
            self.set_winner(P2, f"回合用尽，{PLAYER_NAME[P2]} 以 {s2}:{s1} 获胜！")
        else:
            self.set_winner(None, f"回合用尽，{s1}:{s2} 平局！")

    # ---------------- 计分 ----------------
    def general_score(self, player):
        """将领分：本方每名**存活**武将各算 (SCORE_GENERAL_BASE + 体力) × 武力，逐名求和。"""
        return sum((SCORE_GENERAL_BASE + gen.stamina) * gen.might
                   for gen in self.generals if gen.alive and gen.owner == player)

    def territory_value(self, r, c):
        """一块地域值多少分：地形分 + 格上粮草每石 SCORE_GRAIN。"""
        return SCORE_TERRAIN[self.grid[r][c]] + self.grain_at(r, c) * SCORE_GRAIN

    def distance_field(self, player):
        """这一方到各格的**行动距离**（步数）-> {格子: 步数}；到不了的不在表里。

        多源 BFS。**格上站着谁不影响**——量的是"这一方的兵要几步才能到这儿"，
        不是"现在谁能站上去"，所以地盘会随阵线推进而变。
        """
        dist = {}
        q = deque()
        for gen in self.generals:
            if gen.alive and gen.owner == player and gen.pos not in dist:
                dist[gen.pos] = 0
                q.append(gen.pos)
        while q:
            r, c = q.popleft()
            step = dist[(r, c)] + 1
            for nr, nc in neighbors(r, c):
                if (nr, nc) in dist or self.grid[nr][nc] == OBSTACLE:
                    continue
                dist[(nr, nc)] = step
                q.append((nr, nc))
        return dist

    def territory_scores(self):
        """控制地域（含物品）分 -> ({玩家: 分}, 争夺中的格数)。

        每块可通行地域归**行动距离最近**的那一方，一样近就算争夺中。
        """
        fields = {player: self.distance_field(player) for player in (P1, P2)}
        scores = {P1: 0, P2: 0}
        contested = 0
        for r in range(GRID):
            for c in range(GRID):
                if self.grid[r][c] == OBSTACLE:
                    continue
                d1 = fields[P1].get((r, c))
                d2 = fields[P2].get((r, c))
                if d1 is not None and (d2 is None or d1 < d2):
                    owner = P1
                elif d2 is not None and (d1 is None or d2 < d1):
                    owner = P2
                else:                                 # 一样近（含双方都到不了）：争夺中
                    owner = None
                if owner is None:
                    contested += 1
                else:
                    scores[owner] += self.territory_value(r, c)
        return scores, contested

    def scoreboard(self):
        """比分板上两个人的数 -> ({玩家: (总分, 将领分, 地盘分)}, 争夺中的格数)。"""
        territory, contested = self.territory_scores()
        board = {}
        for player in (P1, P2):
            gens = self.general_score(player)
            board[player] = (gens + territory[player], gens, territory[player])
        return board, contested

    def score(self, player):
        """这一方的总分 = 将领分 + 控制地域（含物品）分。"""
        return self.scoreboard()[0][player][0]

    def score_text(self, player, board):
        """把一方的分数写成「蜀 12345（将领 9000 + 地盘 3345）」——战报结算用。"""
        total, gens, land = board[player]
        return f"{FACTION_SHORT[player]} {total}（将领 {gens} + 地盘 {land}）"

    def set_winner(self, player, text):
        """定下胜负：player 为 None 就是平局。两种情况下对局都算结束。"""
        # 平局时 player 是 None；两种情况都算结束，banner 常驻不消失
        self.over = True
        self.winner = player
        self.push_log(text)
        self.banner = [text, None]            # 帧数 None = 常驻，胜负提示一直挂着
        if self.recorder is not None:         # 终局：写完这一条，整份录像就完整了
            self.recorder.end(self)

    # ---------------- 提示信息 ----------------
    def push_log(self, msg):
        self.log.append(msg)
        del self.log[:-9]

    def flash(self, msg, frames=90):
        self.banner = [msg, frames]

    def tick(self):
        if self.banner and self.banner[1] is not None:   # 剩余帧数 None = 常驻提示，不倒数
            self.banner[1] -= 1
            if self.banner[1] <= 0:
                self.banner = None

    # ---------------- 绘制 ----------------
    def cell_rect(self, row, col):
        return pygame.Rect(MARGIN + col * CELL, MARGIN + row * CELL, CELL, CELL)

    def draw(self, screen, mouse_pos, buttons):
        screen.fill(C_BG)
        self.draw_board(screen, mouse_pos)
        self.draw_sidebar(screen, mouse_pos, buttons)
        if self.aim is not None:              # 瞄准态的提示条，压在棋盘下沿
            self.aim.draw(screen, mouse_pos)
        if self.grain_pick is not None:       # 携带粮草的弹窗压在最上面
            self.grain_pick.draw(screen, mouse_pos)
        if self.cunning is not None:          # 用计弹窗在最顶层
            self.cunning.draw(screen, mouse_pos)

    def hovered_cell(self, mouse_pos):
        """鼠标落在棋盘上就是 (行, 列)，否则 None。"""
        x, y = mouse_pos[0] - MARGIN, mouse_pos[1] - MARGIN
        if 0 <= x < BOARD and 0 <= y < BOARD:
            return y // CELL, x // CELL
        return None

    def draw_board(self, screen, mouse_pos):
        # --- 地形 ---
        for r in range(GRID):
            for c in range(GRID):
                t = self.grid[r][c]
                rect = self.cell_rect(r, c)
                checker = (r + c) % 2 == 0
                if t == OBSTACLE:
                    pygame.draw.rect(screen, C_OBSTACLE, rect)
                    self.draw_obstacle(screen, rect)
                elif t == CITY:
                    pygame.draw.rect(screen, C_CITY if checker else C_CITY_ALT, rect)
                    self.draw_city(screen, rect, r, c)
                else:
                    pygame.draw.rect(screen, C_PLAIN if checker else C_PLAIN_ALT, rect)
                    self.draw_place(screen, rect, r, c, C_PLACE)
                pygame.draw.rect(screen, C_GRID, rect, 1)

        # --- 可走范围 / 可攻击目标 ---
        overlay = pygame.Surface((BOARD, BOARD), pygame.SRCALPHA)
        for (r, c) in self.reachable:                 # 能走到的格子：选中色压淡
            x, y = c * CELL, r * CELL
            pygame.draw.rect(overlay, (*C_SELECT, 55), (x, y, CELL, CELL))
        for (r, c) in self.attackable:                # 能进攻的格子：进攻色压淡
            x, y = c * CELL, r * CELL
            pygame.draw.rect(overlay, (*C_ATTACK, 70), (x, y, CELL, CELL))
        if self.aim is not None:                      # 用计的可点目标：计谋色（紫）压淡
            for (r, c) in self.aim.highlight():
                x, y = c * CELL, r * CELL
                pygame.draw.rect(overlay, (*C_CUNNING, 96), (x, y, CELL, CELL))
        screen.blit(overlay, (MARGIN, MARGIN))

        # --- 武将（同格站两名时左右分栏）---
        for (r, c), gens in self.garrisons().items():
            for gen, rect in zip(gens, self.general_rects(r, c, len(gens))):
                self.draw_general(screen, gen, rect)

        # --- 粮草徽标：画在武将之上，不然会被武将的身子压住 ---
        for (r, c) in self.grain:
            self.draw_grain(screen, r, c)

        # --- 选中框 ---
        if self.selected:
            rect = self.cell_rect(*self.selected.pos).inflate(2, 2)
            pygame.draw.rect(screen, C_SELECT, rect, 3, border_radius=4)

        # --- 悬停格的地名（被武将压住的地名这样看）---
        cell = self.hovered_cell(mouse_pos)
        if cell:
            self.draw_tooltip(screen, cell, mouse_pos)

    def draw_obstacle(self, screen, rect):
        pygame.draw.line(screen, C_OBSTACLE_LINE, rect.topleft, rect.bottomright, 2)
        pygame.draw.line(screen, C_OBSTACLE_LINE, rect.topright, rect.bottomleft, 2)

    def draw_place(self, screen, rect, r, c, color):
        """在格子底部写地名（通路青灰、城池金色）。"""
        txt = get_font(11, bold=True).render(self.places[(r, c)], True, color)
        screen.blit(txt, txt.get_rect(center=(rect.centerx, rect.bottom - 11)))

    def draw_grain(self, screen, r, c):
        """格子右上角挂一个粮草徽标（堆着的公共粮草有几石）；没粮就不画。

        数量大了字号会自己缩，免得徽标撑出格子。
        """
        n = self.grain_at(r, c)
        if not n:
            return
        rect = self.cell_rect(r, c)
        label = f"粮{n}"
        font = fit_font(label, CELL - 16, start=11, min_size=7)
        surf = font.render(label, True, C_GRAIN)
        badge = surf.get_rect().inflate(8, 4)
        badge.topright = (rect.right - 3, rect.top + 3)
        pygame.draw.rect(screen, C_GRAIN_BG, badge, border_radius=4)
        pygame.draw.rect(screen, C_GRAIN, badge, 1, border_radius=4)
        screen.blit(surf, surf.get_rect(center=badge.center))

    def draw_tooltip(self, screen, cell, mouse_pos):
        """鼠标旁边弹出这一格的「地名（地形）」和堆着的粮草。"""
        text = self.describe(*cell)
        if self.grain_at(*cell):
            text += f"　粮草 {self.grain_at(*cell)} {GRAIN_PER_STONE}"
        surf = get_font(13, bold=True).render(text, True, (255, 246, 214))
        box = surf.get_rect().inflate(18, 10)
        box.topleft = (mouse_pos[0] + 14, mouse_pos[1] + 16)
        box.clamp_ip(pygame.Rect(0, 0, WIN_W, WIN_H))       # 贴边时自动收回来
        bg = pygame.Surface(box.size, pygame.SRCALPHA)
        bg.fill((18, 18, 24, 226))
        screen.blit(bg, box.topleft)
        pygame.draw.rect(screen, (96, 104, 124), box, 1, border_radius=6)
        screen.blit(surf, surf.get_rect(center=box.center))

    def draw_city(self, screen, rect, r, c):
        # 城墙砖缝
        for i in range(1, 3):
            y = rect.top + CELL * i // 3
            pygame.draw.line(screen, (96, 78, 52), (rect.left + 3, y), (rect.right - 3, y), 1)
        # 垛口
        for i in range(3):
            x = rect.left + 6 + i * (CELL - 16) // 2
            pygame.draw.rect(screen, (150, 126, 88), (x, rect.top + 4, 8, 7))
        # 城名
        self.draw_place(screen, rect, r, c, C_PLACE_CITY)
        # 归属边框
        holder = self.generals_at(r, c)
        if holder:
            pygame.draw.rect(screen, PLAYER_COLOR[holder[0].owner], rect, 2, border_radius=3)

    def general_rects(self, r, c, count):
        """一格里的武将各占哪块地方：一个人占整格，两个人左右分栏。"""
        base = self.cell_rect(r, c)
        if count <= 1:
            return [base]
        half = base.width // 2
        return [pygame.Rect(base.left, base.top, half, base.height),
                pygame.Rect(base.left + half, base.top, base.width - half, base.height)]

    def draw_general(self, screen, gen, rect):
        """画一名武将；rect 是分给它的那一块（同格两名时只有半格宽）。"""
        slim = rect.width < CELL
        body = rect.inflate(-4 if slim else -8, -8)
        color = PLAYER_COLOR[gen.owner]
        dark = PLAYER_COLOR_DARK[gen.owner]
        # 圆角方块作为武将底盘
        pygame.draw.rect(screen, dark, body, border_radius=8)
        pygame.draw.rect(screen, color, body.inflate(-2 if slim else -4, -4), border_radius=6)

        # 姓名：字号按可用宽度收缩，三字名在半格宽里也塞得下
        name_font = fit_font(gen.name, body.width - 4, start=12 if slim else 14)
        name_txt = name_font.render(gen.name, True, (255, 255, 255))
        screen.blit(name_txt, name_txt.get_rect(
            center=(body.centerx, body.top + (12 if slim else 15))))

        # 属性行：半格宽实在放不下，索性省掉（悬停提示和侧边栏里还有）
        if not slim:
            stat_txt = get_font(11).render(f"武{gen.might} 智{gen.intellect}",
                                           True, (250, 250, 250))
            screen.blit(stat_txt, stat_txt.get_rect(center=(body.centerx, body.top + 32)))

        # 体力条
        bar = pygame.Rect(body.left + 3, body.bottom - 11, body.width - 6, 6)
        pygame.draw.rect(screen, (20, 20, 24), bar, border_radius=3)
        ratio = max(0.0, min(1.0, gen.stamina / gen.stamina_cap))   # 守城加成时上限被抬高
        fill = bar.copy()
        fill.width = max(1, int(bar.width * ratio))
        hp_color = (96, 208, 112) if ratio > 0.5 else (232, 196, 72) if ratio > 0.25 else (226, 84, 72)
        pygame.draw.rect(screen, hp_color, fill, border_radius=3)
        hp_txt = get_font(10).render(str(gen.stamina), True, (245, 245, 245))
        screen.blit(hp_txt, hp_txt.get_rect(center=(body.centerx, body.bottom - 2)))

        if gen.exhausted:
            veil = pygame.Surface(body.size, pygame.SRCALPHA)
            veil.fill((10, 10, 14, 110))
            screen.blit(veil, body.topleft)
            pygame.draw.rect(screen, (120, 126, 140), body, 2, border_radius=8)

    def info_top(self):
        """选中武将详情框的顶边。

        必须和 draw_sidebar 的排布对齐：点「吃粮」按钮要用同一个位置算矩形。
        """
        y = MARGIN + 14 + 32                      # 标题
        return y + (30 if self.over else 22 + 30)  # 结束横幅 / 回合行 + 行动方行

    def info_col_w(self):
        """详情框 / 比分板一共并排两个，各占多宽（侧边栏宽度一分两半）。"""
        return (SIDEBAR - 32 - self.INFO_COL_GAP) // 2

    def selected_box(self):
        return pygame.Rect(self.SIDEBAR_X + 16, self.info_top(),
                           self.info_col_w(), self.INFO_BOX_H)

    def score_box(self):
        """比分板：和选中武将详情并排在同一行，右半边。"""
        width = self.info_col_w()
        return pygame.Rect(self.SIDEBAR_X + 16 + width + self.INFO_COL_GAP, self.info_top(),
                           width, self.INFO_BOX_H)

    def eat_buttons(self):
        """详情框底部那排「吃粮」按钮 -> [(文字, Rect, 石数)]；不能吃就是空表。"""
        amounts = self.eat_amounts(self.selected)
        if not amounts:
            return []
        box = self.selected_box()
        return [(f"吃{n}{GRAIN_PER_STONE}",
                 pygame.Rect(box.left + 12 + i * (self.INFO_BTN_W + self.INFO_BTN_GAP),
                             box.top + self.INFO_BTN_TOP,
                             self.INFO_BTN_W, self.INFO_BTN_H),
                 n)
                for i, n in enumerate(amounts)]

    # ---------------- 计谋（规则见 stratagems.py） ----------------
    def cunning_button(self):
        """详情框第二排那颗「用计」按钮 -> Rect；没选中武将就没有。"""
        gen = self.selected
        if not gen or not gen.alive:
            return None
        box = self.selected_box()
        return pygame.Rect(box.left + 12, box.top + self.INFO_BTN2_TOP,
                           box.width - 24, self.INFO_BTN_H)

    def open_cunning(self):
        """点「用计」：开弹窗。付不起代价就只报一句为什么，不开。"""
        gen = self.selected
        block = cunning_block_reason(gen)
        if block:
            self.flash(block)
            return False
        self.cunning = CunningPick(self, gen)
        return True

    def start_cunning(self, key):
        """弹窗里挑定了某一计：关弹窗，进瞄准态（代价还没扣，点了目标才算数）。"""
        gen = self.cunning.gen
        self.cunning = None
        self.aim = CunningAim(self, gen, key)
        return True

    def drop_cunning(self):
        """把用计相关的弹窗/瞄准态一并收掉（换回合、重开时用）。"""
        self.cunning = None
        self.aim = None

    def draw_sidebar(self, screen, mouse_pos, buttons):
        x0 = MARGIN * 2 + BOARD
        panel = pygame.Rect(x0, MARGIN, SIDEBAR, BOARD)
        pygame.draw.rect(screen, C_PANEL, panel, border_radius=10)

        y = panel.top + 14
        screen.blit(get_font(22, bold=True).render("三国战棋", True, C_TEXT), (panel.left + 16, y))
        y += 32

        if not self.over:
            # 回合数后面跟一句"下批粮草第几回合"：粮草是攒着等的，玩家得能预判
            turn_line = f"第 {self.turn} / {TURN_LIMIT} 回合　·　下批粮草 第 {self.next_grain_turn()} 回合"
            screen.blit(get_font(13).render(turn_line, True, C_TEXT_DIM), (panel.left + 16, y))
            y += 22
            dot = pygame.Rect(panel.left + 16, y + 4, 14, 14)
            pygame.draw.rect(screen, PLAYER_COLOR[self.current], dot, border_radius=4)
            if self.is_ai():
                # 电脑行动时不用报"几人走完"，它自己会一直走到走完；但在用哪一版得报出来
                text = f"{PLAYER_NAME[self.current]} · {self.ai_version()}版思考中…"
            else:
                side = PLAYER_NAME[self.current]
                if self.is_ai(1 - self.current):
                    side += f"（对 {self.ai_version(1 - self.current)}版电脑）"
                gens = [g for g in self.generals if g.alive and g.owner == self.current]
                done = sum(1 for g in gens if g.exhausted)
                text = f"{side}  ({done}/{len(gens)} 已走完)"
            screen.blit(get_font(14, bold=True).render(text, True, C_TEXT), (panel.left + 38, y))
            y += 30
        else:
            label = "平局，双方罢兵" if self.winner is None else f"{PLAYER_NAME[self.winner]} 获胜"
            screen.blit(get_font(15, bold=True).render(label, True, C_TEXT), (panel.left + 16, y))
            y += 30

        # 详情 + 比分板并排一行；顶边由 info_top() 定死，吃粮按钮的命中矩形要用
        self.draw_score_board(screen, self.score_box())
        y = self.draw_selected_info(screen, panel, self.info_top())

        # 底部武将总览：一栏一方并排；一屏只放 ROSTER_MAX_ROWS 行，多出来的滚轮翻
        per_side, visible = self.roster_rows()
        start = self.roster_start
        roster = self.roster_rect(buttons)
        screen.blit(get_font(13, bold=True).render("战报", True, C_TEXT_DIM), (panel.left + 16, y))
        if per_side > visible:                    # 右边给一句提示，免得不知道能滚
            hint = get_font(11).render(
                f"▼▲ 武将 {start + 1}-{start + visible}/{per_side} 滚轮翻看", True, C_TEXT_DIM)
            screen.blit(hint, hint.get_rect(midright=(panel.right - 16, y + 8)))
        y += 20
        log_font = get_font(12)
        max_lines = max(1, (roster.top - 8 - y) // 17)
        lines = []
        for entry in reversed(self.log):          # 从最新往回取，保证最新的一定显示
            lines.extend(wrap_text(entry, log_font, panel.width - 32))
            if len(lines) >= max_lines:
                break
        for chunk in lines[:max_lines][::-1]:     # 再翻回时间顺序
            screen.blit(log_font.render(chunk, True, (198, 203, 214)), (panel.left + 16, y))
            y += 17

        col_w = (panel.width - 32 - self.ROSTER_COL_GAP) // 2
        for i, player in enumerate((P1, P2)):
            col = pygame.Rect(roster.left + i * (col_w + self.ROSTER_COL_GAP),
                              roster.top, col_w, roster.height)
            self.draw_roster(screen, col, player, start, visible)
        if per_side > visible:
            self.draw_roster_scrollbar(screen, roster, per_side, visible, start)

        # 按钮
        for label, rect, _key in buttons:
            hover = rect.collidepoint(mouse_pos)
            pygame.draw.rect(screen, C_BTN_HOVER if hover else C_BTN, rect, border_radius=8)
            pygame.draw.rect(screen, (86, 94, 112), rect, 1, border_radius=8)
            txt = get_font(14, bold=True).render(label, True, C_TEXT)
            screen.blit(txt, txt.get_rect(center=rect.center))

        # 顶部飘字提示
        if self.banner:
            text = self.banner[0]
            font = get_font(20, bold=True)
            surf = font.render(text, True, (255, 246, 214))
            box = surf.get_rect(center=(MARGIN + BOARD // 2, MARGIN + 30)).inflate(28, 16)
            bg = pygame.Surface(box.size, pygame.SRCALPHA)
            bg.fill((18, 18, 24, 214))
            screen.blit(bg, box.topleft)
            pygame.draw.rect(screen, (214, 180, 96), box, 2, border_radius=8)
            screen.blit(surf, surf.get_rect(center=box.center))

    def draw_score_board(self, screen, box):
        """常驻比分板：双方的总分 / 将领分 / 地盘分（怎么算见 Game.scoreboard）。"""
        board, contested = self.scoreboard()
        pygame.draw.rect(screen, (42, 46, 58), box, border_radius=8)
        pygame.draw.rect(screen, (86, 94, 112), box, 2, border_radius=8)
        screen.blit(get_font(13, bold=True).render("比分", True, C_TEXT), (box.left + 12, box.top + 8))
        if contested:
            hint = get_font(10).render(f"争夺中 {contested} 格", True, C_TEXT_DIM)
            screen.blit(hint, hint.get_rect(midright=(box.right - 10, box.top + 16)))

        left = box.left + 12
        col_w = (box.right - 10 - left - self.SCORE_LABEL_W) // 2
        centers = [left + self.SCORE_LABEL_W + i * col_w + col_w // 2 for i in (0, 1)]

        for i, player in enumerate((P1, P2)):       # 表头：一方一个小色块 + 阵营名
            chip = pygame.Rect(0, 0, 10, 10)
            chip.center = (centers[i] - 6, box.top + 38)
            pygame.draw.rect(screen, PLAYER_COLOR[player], chip, border_radius=3)
            name = get_font(11, bold=True).render(FACTION_SHORT[player], True, PLAYER_COLOR[player])
            screen.blit(name, name.get_rect(midleft=(centers[i] + 1, box.top + 38)))

        f_label = get_font(11)
        f_value = get_font(11, bold=True)
        for row, label in enumerate(("总分", "将领分", "地盘分")):
            y = box.top + self.SCORE_ROW_TOP + row * self.SCORE_ROW_PITCH
            screen.blit(f_label.render(label, True, C_TEXT_DIM), (left, y))
            for i, player in enumerate((P1, P2)):
                surf = f_value.render(str(board[player][row]), True, PLAYER_COLOR[player])
                screen.blit(surf, surf.get_rect(midtop=(centers[i], y)))

    def draw_selected_info(self, screen, panel, y):
        gen = self.selected
        box = self.selected_box()
        pygame.draw.rect(screen, (42, 46, 58), box, border_radius=8)
        if not gen or not gen.alive:
            tip = get_font(12).render("点己方武将选中，再点高亮格移动", True, C_TEXT_DIM)
            screen.blit(tip, tip.get_rect(center=box.center))
            return box.bottom + 12
        pygame.draw.rect(screen, PLAYER_COLOR[gen.owner], box, 2, border_radius=8)
        f_big = get_font(17, bold=True)
        f_sm = get_font(12)
        screen.blit(f_big.render(gen.name, True, C_TEXT), (box.left + 12, box.top + 8))
        stats = f"武力 {gen.might}   智力 {gen.intellect}"
        screen.blit(f_sm.render(stats, True, C_TEXT), (box.left + 12, box.top + 34))
        # 堆粮没上限、数字会长，用 fit_font 兜住（宁可缩字号）
        grain_txt = f"此地粮 {self.grain_at(*gen.pos)}"
        gx = box.left + 12 + f_sm.size(stats)[0] + 16
        grain_font = fit_font(grain_txt, box.right - 12 - gx, start=12, min_size=8, bold=False)
        screen.blit(grain_font.render(grain_txt, True, C_GRAIN), (gx, box.top + 34))
        stamina = f"体力 {gen.stamina}/{gen.stamina_cap}"
        if gen.city_boost:
            stamina += "（守城中）"          # 加成期间上限临时抬高，标一下免得看着像出错
        move_txt = f"移动力 {gen.move_points}/{MOVE_POINTS}"
        stamina_font = fit_font(f"{stamina}   {move_txt}", box.width - 24,
                                start=12, min_size=9, bold=False)
        screen.blit(stamina_font.render(f"{stamina}   {move_txt}", True, C_TEXT),
                    (box.left + 12, box.top + 54))
        note = f"位于 {self.describe(gen.row, gen.col)}"
        if gen.exhausted:
            note += "  · 本回合已走完"
        elif self.compute_attackable(gen):
            note += "  · 可踏进去交战"
        # 地名长短不一，窄框里用 fit_font 兜住
        note_font = fit_font(note, box.width - 24, start=12, min_size=9, bold=False)
        screen.blit(note_font.render(note, True, C_TEXT_DIM), (box.left + 12, box.top + 72))

        # 底部一行：吃粮按钮；吃不了但脚下有粮就写一句为什么
        buttons = self.eat_buttons()
        if not buttons and self.grain_at(*gen.pos) > 0:
            tip = get_font(11).render("本回合已走完，吃不了粮", True, C_TEXT_DIM)
            screen.blit(tip, (box.left + 12, box.top + self.INFO_BTN_TOP + 4))
        for label, rect, _n in buttons:
            hover = rect.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(screen, C_BTN_HOVER if hover else C_GRAIN_BG, rect, border_radius=6)
            pygame.draw.rect(screen, C_GRAIN, rect, 1, border_radius=6)
            txt = get_font(11, bold=True).render(label, True, C_GRAIN)
            screen.blit(txt, txt.get_rect(center=rect.center))

        # 第二行用计：付不起代价就画成暗色，点了只报一句为什么（见 open_cunning）
        rect = self.cunning_button()
        block = cunning_block_reason(gen)
        hover = rect.collidepoint(pygame.mouse.get_pos()) and not block
        pygame.draw.rect(screen, C_BTN_HOVER if hover else C_CUNNING_BG, rect, border_radius=6)
        pygame.draw.rect(screen, C_CUNNING_DIM if block else C_CUNNING, rect, 1, border_radius=6)
        label = (f"用计（{STRATAGEM_MOVE_COST} 行动力 + {STRATAGEM_STAMINA_COST} 体力）"
                 if not block else f"用计 · {block}")
        font = fit_font(label, rect.width - 12, start=11, min_size=9, bold=True)
        txt = font.render(label, True, C_CUNNING_DIM if block else C_CUNNING)
        screen.blit(txt, txt.get_rect(center=rect.center))
        return box.bottom + 12

    def draw_roster(self, screen, col, player, start, visible):
        """把一方武将列成一个竖栏（col 是这一栏的区域）；只画 [start, start+visible) 这几行。"""
        font = get_font(11)
        screen.blit(font.render(PLAYER_NAME[player], True, PLAYER_COLOR[player]),
                    (col.left, col.top))
        y = col.top + self.ROSTER_TITLE_H
        mine = [g for g in self.generals if g.owner == player]
        for gen in mine[start:start + visible]:
            if not gen.alive:
                txt, color = f"{gen.name} 阵亡", (128, 128, 138)
            else:
                txt = f"{gen.name} 武{gen.might} 智{gen.intellect} 体{gen.stamina}"
                color = (206, 211, 222)
            screen.blit(font.render(txt, True, color), (col.left, y))
            y += self.ROSTER_ROW_H

    def draw_roster_scrollbar(self, screen, roster, per_side, visible, start):
        """总览右侧的一根细滚动条——人多的时候告诉玩家"下面还有"。"""
        track = pygame.Rect(roster.right - 3, roster.top + self.ROSTER_TITLE_H,
                            3, visible * self.ROSTER_ROW_H)
        pygame.draw.rect(screen, (48, 52, 64), track, border_radius=2)
        thumb_h = max(16, int(track.height * visible / per_side))
        span = track.height - thumb_h
        top_row = max(1, per_side - visible)
        thumb_y = track.top + int(span * start / top_row)
        pygame.draw.rect(screen, C_TEXT_DIM,
                         pygame.Rect(track.left, thumb_y, track.width, thumb_h),
                         border_radius=2)


# --------------------------------------------------------------------------
# 交战界面（单独的一屏）
# --------------------------------------------------------------------------
class Battle:
    """一次交战的独立界面：一格守军最多两名，攻方要把他们一个个打完。

    出战顺序与单挑算式、各笔撤退代价的规则见 README.md，这里只说代码要点：
    * 守军两名时由守方点将（PICK_DEFENDER）定谁先迎战，打完一名（撤退或阵亡
      都算）就自动接着打下一名；**第二场开打前双方都能先撤**（仍是攻方先选）。
    * 每回合双方同时掉血，各自取 [1, loss_range(self)] 里的随机整数（见 roll）。
    * 攻方撤退当场再补 ATK_RETREAT_COST 点且**不赊账**，手头不够连按钮都不给
      （见 can_retreat）；守方撤退的额度见 retreat_paths。

    状态流转（第二场从 CHOOSE_ATK 起手）：
        一回合: CHOOSE_ATK --继续--> CHOOSE_DFD --继续--> 下一回合的 READY
        READY --交手--> IMPACT --动画放完--> 有人下场？ --否--> 上一行；是则换人 / 收场
        PICK_DEFENDER --点将--> READY    CHOOSE_DFD --撤退--> RETREAT_PICK --> 换人 / 进占
        CHOOSE_ATK --撤退--> OVER（攻方原地不动）    OVER --返回战场--> 回到地图
    """

    (PICK_DEFENDER, READY, IMPACT,
     CHOOSE_ATK, CHOOSE_DFD, RETREAT_PICK, OVER) = range(7)
    IMPACT_FRAMES = 42                     # 掉血动画时长（帧，60fps 约 0.7 秒）
    SHAKE = (0, -4, 3, -3, 2, -2, 1, 0)    # 掉血瞬间卡片的抖动偏移
    HIST_ROWS = 5                          # 战斗过程一屏显示几回合（多出来的可以翻）

    # 一整屏的版面（屏幕尺寸由主循环的 WIN_W x WIN_H 决定）
    CARD_W, CARD_H, CARD_Y = 420, 246, 86
    CARD_X = (40, WIN_W - 40 - CARD_W)
    BTN_W, BTN_H, BTN_Y = 220, 52, 412
    HIST_RECT = pygame.Rect(40, 476, WIN_W - 80, 166)
    PICK_COLS = 4                          # 撤退选项：4 列铺在战报那块地方
    HINT_Y, FOOT_Y = 348, 654

    def __init__(self, game, attacker, defenders):
        self.game = game
        self.rng = game.rng
        self.recorder = game.recorder         # 录像；整场交战的子事件都记进去
        self.atk = attacker
        self.defenders = list(defenders)      # 这一格的守军，出场顺序由守方定
        self.cell = self.defenders[0].pos     # 争夺中的格子（守方原本站的地方）
        self.origin = attacker.pos            # 攻方的出发格（攻方败了原地不动）
        self.place = game.describe(*self.cell)           # 争夺中的这一格：地名（地形）
        self.origin_place = game.describe(*self.origin)   # 攻方是从哪一格打过来的
        self.fight_no = 1                     # 第几场（守军最多两名，所以最多两场）
        self.dfd = None                       # 当前迎战的守将
        self.round = 0
        self.rounds = []                      # 每回合一条战报，见 roll()
        self.scroll = 0                       # 战斗过程往上翻了几回合（0 = 贴着最新一条）
        self.timer = 0
        self.atk_retreated = False            # 攻方退过，就不再进占这一格
        self.retreat_choices = []             # RETREAT_PICK 阶段可点的 (落脚点, 步数)
        self.retreat_from = None              # 从哪个状态点进撤退选择的（"再想想"用）
        self.ending = ""
        # 守的是城池就先抬高守军体力，打完再压回去（见 finish）
        self.city_defense = game.grid[self.cell[0]][self.cell[1]] == CITY
        if self.city_defense:
            for defender in self.defenders:
                defender.apply_city_boost()
        if len(self.defenders) > 1:
            self.state = self.PICK_DEFENDER
            self.hint = f"{self.place} 有两名守将，请守方决定谁先出阵迎战。"
        else:
            self.begin_fight(self.defenders[0])
            self.hint = f"两军在 {self.place} 列阵，准备交手。"
        if self.city_defense:
            self.hint += f"（{self.place}，守军据城力战，体力 +20%）"
        if self.recorder is not None:
            self.recorder.battle("begin", cell=[self.cell[0], self.cell[1]],
                                 place=self.place, atk=self.atk.name,
                                 atk_owner=self.atk.owner,
                                 dfd=[g.name for g in self.defenders],
                                 city=self.city_defense)

    def begin_fight(self, defender):
        """让 defender 上场迎战：第一场从 READY 起手，第二场从"开打前可撤退"起手。"""
        self.dfd = defender
        self.round = 0
        self.scroll = 0
        self.timer = 0
        self.state = self.CHOOSE_ATK if self.fight_no > 1 else self.READY
        if self.state == self.CHOOSE_ATK:
            self.hint = (f"第 {self.fight_no} 场：{defender.name} 接着迎战。"
                         f"开打前双方都还能收手——{self.atk.name}（攻方）先决定。")

    # ---------------- 规则 ----------------
    def foe_of(self, gen):
        """gen 的对手；守方的对手就是攻方，攻方的对手是当前迎战的那名守将。"""
        return self.dfd if gen is self.atk else self.atk

    def loss_range(self, victim):
        """victim 这一回合最多掉多少体力 = LOSS_MAX_PER_MIGHT * 对方武力。"""
        return LOSS_MAX_PER_MIGHT * self.foe_of(victim).might

    @property
    def chooser(self):
        """现在该谁做决定（继续 / 撤退）；没人要做决定时是 None。"""
        if self.state == self.CHOOSE_ATK:
            return self.atk
        if self.state == self.CHOOSE_DFD:
            return self.dfd
        return None

    @property
    def pre_round(self):
        """现在是不是"第一回合开打之前"的那个选择（只有第二场才有）。"""
        return self.round == 0 and self.state in (self.CHOOSE_ATK, self.CHOOSE_DFD)

    def human_may_act(self):
        """这一屏现在等人类做决定吗（1P 下跟着阵营走，不跟"谁在进攻"走）。"""
        game = self.game
        if self.state == self.PICK_DEFENDER:
            return not game.is_ai(self.defenders[0].owner)
        if self.state in (self.CHOOSE_ATK, self.CHOOSE_DFD):
            return not game.is_ai(self.chooser.owner)
        if self.state == self.RETREAT_PICK:
            return not game.is_ai(self.dfd.owner)
        if self.state == self.READY:
            return not game.is_ai(self.atk.owner)      # 谁进攻谁点"交手"
        if self.state == self.OVER:
            return not game.is_ai(self.atk.owner)      # 电脑进攻时自己收场
        return False                                   # IMPACT：掉血动画中

    @property
    def shown_round(self):
        """界面上该显示的回合数：准备阶段显示的是即将开打的这一回合。"""
        if self.state == self.READY or self.pre_round:
            return self.round + 1
        return max(1, self.round)

    def roll(self):
        """交手一回合：双方同时按对方武力掉体力（各取一个 [1, 2*对方武力] 的随机数）。"""
        if self.state != self.READY:
            return
        self.round += 1
        atk_loss = self.rng.randint(1, self.loss_range(self.atk))
        dfd_loss = self.rng.randint(1, self.loss_range(self.dfd))
        self.atk.add_stamina(-atk_loss)
        self.dfd.add_stamina(-dfd_loss)
        for gen in (self.atk, self.dfd):
            if gen.stamina <= 0:
                gen.alive = False             # 掉到 0 或以下当场阵亡
        # 战报里连守将一起记下来：换人之后还要能把这一场画回历史里
        self.rounds.append({"fight": self.fight_no, "no": self.round, "dfd": self.dfd,
                            "atk_loss": atk_loss, "dfd_loss": dfd_loss,
                            "atk_hp": self.atk.stamina, "dfd_hp": self.dfd.stamina})
        self.hint = (f"第 {self.round} 回合：{self.atk.name} 掉 {atk_loss} 点体力，"
                     f"{self.dfd.name} 掉 {dfd_loss} 点体力。")
        if self.recorder is not None:
            self.recorder.battle("roll", round=self.round, atk=self.atk.name,
                                 dfd=self.dfd.name, atk_loss=atk_loss, dfd_loss=dfd_loss,
                                 atk_hp=self.atk.stamina, dfd_hp=self.dfd.stamina)
        self.state, self.timer = self.IMPACT, self.IMPACT_FRAMES

    def update(self):
        """掉血动画放完后决定下一步：有人下场就换人/收场，否则交给双方做选择。"""
        if self.state != self.IMPACT:
            return
        self.timer -= 1
        if self.timer > 0:
            return
        if not self.atk.alive or not self.dfd.alive:
            self.settle_fight()
            return
        self.state = self.CHOOSE_ATK
        tail = ("继续缠斗，还是就此撤退？" if self.can_retreat()
                else f"继续缠斗（手头不够 {ATK_RETREAT_COST} 点，撤不了）。")
        self.hint = f"第 {self.round} 回合结束，{self.atk.name}（攻方）先决定：{tail}"

    def continue_fight(self):
        """选择继续：攻方选完轮到守方，两边都选继续就打下一回合。"""
        if self.recorder is not None:
            who = self.atk if self.state == self.CHOOSE_ATK else self.dfd
            side = "atk" if self.state == self.CHOOSE_ATK else "dfd"
            self.recorder.battle("hold", name=who.name, who=side, owner=who.owner)
        if self.state == self.CHOOSE_ATK:
            self.state = self.CHOOSE_DFD
            self.hint = (f"{self.atk.name} 继续缠斗，现在轮到 {self.dfd.name}（守方）"
                         f"决定：接着打，还是撤退？")
        elif self.state == self.CHOOSE_DFD:
            self.state = self.READY
            self.hint = f"双方各自重整旗鼓，第 {self.round + 1} 回合准备交手。"

    def settle_fight(self):
        """一场打完（有人阵亡）：报战报，然后决定是换人接着打还是收场。"""
        game, atk, dfd = self.game, self.atk, self.dfd
        for gen in (atk, dfd):
            if not gen.alive:
                game.push_log(f"☠ {gen.name} 体力耗尽，阵亡！")
        if not atk.alive and not dfd.alive:
            text = f"{atk.name} 与 {dfd.name} 同归于尽，双双阵亡。"
        elif not atk.alive:
            text = f"{atk.name} 力竭阵亡，{dfd.name} 守住了阵脚。"
        else:
            text = f"{dfd.name} 力竭阵亡，{atk.name} 获胜。"
        game.push_log(f"⚔ {atk.name} vs {dfd.name} 共 {self.round} 回合：{text}")
        self.ending = text
        if self.recorder is not None:
            deaths = [g.name for g in (atk, dfd) if not g.alive]
            self.recorder.battle("duel_end", atk=atk.name, dfd=dfd.name,
                                 rounds=self.round, text=text, deaths=deaths)
        self.advance(text)

    def advance(self, text):
        """一名守将下场了：攻方还在就换下一名接着打，守军打光了就收场。"""
        if self.atk_retreated or not self.atk.alive:
            self.finish(text)
            return
        rest = [g for g in self.defenders if g.alive and g.pos == self.cell]
        if rest:
            self.fight_no += 1
            self.begin_fight(rest[0])
            return
        self.finish(text)

    def finish(self, text):
        """整场交战结束：结算守城加成、把战果落回地图。

        粮草不用交接——它从不带在武将身上，始终堆在格子里。
        """
        self.state, self.hint = self.OVER, text
        for defender in self.defenders:
            defender.drop_city_boost()        # 守城加成到此为止，体力压回原比例
        self.apply()                          # 定下谁最终站在这一格上
        if self.recorder is not None:
            self.recorder.battle("end", text=text, place=self.place,
                                 atk=self.atk.name,
                                 atk_pos=[self.atk.row, self.atk.col],
                                 atk_retreated=self.atk_retreated)

    def apply(self):
        """战果落回地图：守军清空且攻方没退没死，攻方才进占这一格。"""
        game, atk = self.game, self.atk
        if self.atk_retreated or not atk.alive:
            return
        if any(g.alive and g.pos == self.cell for g in self.defenders):
            return                            # 还有守将在，格子不归攻方
        atk.row, atk.col = self.cell
        game.push_log(f"{atk.name} 进占 {game.describe(*self.cell)}。")

    # ---------------- 撤退 ----------------
    def came_direction(self):
        """攻方是从哪个方向来的（从争夺格指向攻方出发格的单位向量）。"""
        dr = self.origin[0] - self.cell[0]
        dc = self.origin[1] - self.cell[1]
        return ((dr > 0) - (dr < 0), (dc > 0) - (dc < 0))

    def retreat_paths(self):
        """守方能撤到哪几格、各要几步 -> {终点: 步数}；一处都去不了就是空。

        从争夺格做 BFS（可以拐弯、不要求走直线），四条约束：第一步不能迎着攻方来的
        方向；中途每格要过得去；落脚那格还要停得下；已撤格数不得超 MOVE_POINTS。
        步数 = BFS 最短距离，也就是这一撤要透支的行动力。
        """
        cell = self.cell
        came = self.came_direction()
        limit = min(RETREAT_MAX, self.dfd.retreat_room)   # 本回合还剩多少撤退额度
        dests = {}
        if limit <= 0:
            return dests                                # 退满了，没得退
        seen = {cell}
        q = deque([(cell[0], cell[1], 0)])
        while q:
            r, c, d = q.popleft()
            if d >= limit:
                continue
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                if d == 0 and (dr, dc) == came:
                    continue
                nr, nc = r + dr, c + dc
                if (nr, nc) in seen or not self.game.can_transit(self.dfd, nr, nc):
                    continue
                seen.add((nr, nc))
                if self.game.can_stop(self.dfd, nr, nc):   # 停不下就只当路过
                    dests[(nr, nc)] = d + 1
                q.append((nr, nc, d + 1))
        return dests

    def retreat_options(self):
        """候选落脚点，按"先近后远"排好 -> [(终点, 步数)]：退得越少，透支越少。"""
        return sorted(self.retreat_paths().items(), key=lambda kv: (kv[1], kv[0]))

    def can_retreat(self):
        """当前该做决定的一方有没有撤退的权利。

        攻方要补得起 ATK_RETREAT_COST，守方得真有退路（见 retreat_paths）。
        """
        if self.chooser is self.atk:
            return self.atk.move_points >= ATK_RETREAT_COST
        return bool(self.retreat_paths())

    def request_retreat(self, gen):
        """点了"撤退"：攻方就地收手（补一笔移动力），守方先挑撤到哪一格。"""
        if self.recorder is not None:
            who = "atk" if gen is self.atk else "dfd"
            self.recorder.battle("retreat", name=gen.name, who=who, owner=gen.owner,
                                 mp=gen.move_points)
        if gen is self.atk:
            if gen.move_points < ATK_RETREAT_COST:
                return                        # 付不起时本来就不给按钮，这里只是兜底
            self.atk_retreated = True
            gen.move_points -= ATK_RETREAT_COST
            text = f"{self.atk.name} 撤退，判为败方；{self.dfd.name} 守住 {self.place}。"
            self.game.push_log(
                f"⚔ {self.atk.name} 从 {self.place} 撤退，退回原地"
                f"（进攻 {ATTACK_COST} 点 + 撤退 {ATK_RETREAT_COST} 点，"
                f"剩余移动力 {gen.move_points}）。")
            self.finish(text)
            return
        options = self.retreat_options()
        if not options:
            return                            # 没退路时本来就不给按钮，这里只是兜底
        if len(options) == 1:                 # 只有一条路，不用再让玩家点一次
            self.apply_retreat(*options[0])
            return
        self.retreat_choices = options
        self.retreat_from = self.state
        self.state = self.RETREAT_PICK
        self.hint = (f"{gen.name} 往哪撤？（{len(options)} 个落脚点可选，"
                     f"第一步不能迎着攻方来；本回合还剩 {gen.retreat_room} 格额度）")

    def apply_retreat(self, dest, steps):
        """守方撤退落地：挪过去，并把步数记成下回合要还的行动力。"""
        gen = self.dfd
        if steps > gen.retreat_room:            # 选项都是按额度生成的，这里只是兜底
            return
        self.retreat_choices = []               # 选完了，选项作废
        gen.row, gen.col = dest
        gen.add_move_debt(steps)
        to = self.game.describe(*dest)
        self.game.push_log(
            f"⚔ {gen.name} 从 {self.place} 撤退 {steps} 格到 {to}"
            f"（判为败方，本回合累计撤退 {gen.move_debt} 格，"
            f"下回合透支 {gen.move_debt} 点行动力）。")
        self.ending = f"{gen.name} 撤到 {to}，判为败方；{self.atk.name} 占上风。"
        if self.recorder is not None:
            self.recorder.battle("retreat_done", name=gen.name, who="dfd",
                                 dest=[dest[0], dest[1]], steps=steps,
                                 debt=gen.move_debt)
        self.advance(self.ending)

    # ---------------- 操作 ----------------
    def button_rect(self, index, count):
        """第 index 个按钮（一行 count 个，整体居中）。"""
        gap = 24
        total = count * self.BTN_W + (count - 1) * gap
        left = (WIN_W - total) // 2 + index * (self.BTN_W + gap)
        return pygame.Rect(left, self.BTN_Y, self.BTN_W, self.BTN_H)

    def pick_rect(self, index, count):
        """撤退选项按钮：PICK_COLS 列，铺在战报那块地方（这一屏不显示战报）。"""
        rect, gap = self.HIST_RECT, 12
        cols = self.PICK_COLS
        rows = max(1, -(-count // cols))       # 向上取整
        w = (rect.width - (cols - 1) * gap) // cols
        h = (rect.height - (rows - 1) * gap) // rows
        return pygame.Rect(rect.left + (index % cols) * (w + gap),
                           rect.top + (index // cols) * (h + gap), w, h)

    def buttons(self):
        """当前状态下可点的按钮 -> [(文字, Rect, 动作)]。"""
        if self.state == self.PICK_DEFENDER:
            count = len(self.defenders)
            return [(f"{g.name} 先出阵", self.button_rect(i, count), f"pick:{i}")
                    for i, g in enumerate(self.defenders)]
        if self.state == self.READY:
            return [("交手", self.button_rect(0, 1), "roll")]
        if self.state in (self.CHOOSE_ATK, self.CHOOSE_DFD):
            action = "atk_retreat" if self.state == self.CHOOSE_ATK else "dfd_retreat"
            options = [("继续缠斗", "hold")]
            # 退不了的一方连按钮都不给：守方是没退路，攻方是补不起那点移动力
            if self.can_retreat():
                label = (f"撤退（判负，再补 {ATK_RETREAT_COST} 点）"
                         if self.state == self.CHOOSE_ATK else "撤退（判负）")
                options.append((label, action))
            return [(label, self.button_rect(i, len(options)), act)
                    for i, (label, act) in enumerate(options)]
        if self.state == self.RETREAT_PICK:
            count = len(self.retreat_choices)
            picks = [(f"撤至 {self.game.places[dest]}（{n} 点）",
                      self.pick_rect(i, count), f"retreat:{i}")
                     for i, (dest, n) in enumerate(self.retreat_choices)]
            picks.append(("再想想（回去缠斗）", self.button_rect(0, 1), "back"))
            return picks
        if self.state == self.OVER:
            return [("返回战场", self.button_rect(0, 1), "close")]
        return []                             # IMPACT：掉血动画中，不接受操作

    def act(self, action):
        if action == "roll":
            self.roll()
        elif action == "hold":
            self.continue_fight()
        elif action == "atk_retreat":
            self.request_retreat(self.atk)
        elif action == "dfd_retreat":
            self.request_retreat(self.dfd)
        elif action == "back":
            self.retreat_choices = []                 # 别把这一轮的选项留到下一轮
            self.state = self.retreat_from
            self.hint = "重新决定：继续缠斗，还是撤退？"
        elif action.startswith("pick:"):
            self.pick_defender(int(action.split(":")[1]))
        elif action.startswith("retreat:"):
            self.apply_retreat(*self.retreat_choices[int(action.split(":")[1])])
        elif action == "close":
            self.game.end_battle()

    def pick_defender(self, index):
        """守方定下谁先出阵：选中的先上，另一名排在后面等着。"""
        first = self.defenders[index]
        self.defenders = [first] + [g for g in self.defenders if g is not first]
        self.game.push_log(f"{first.name} 出阵迎战 {self.atk.name}。")
        if self.recorder is not None:
            self.recorder.battle("pick", first=first.name, atk=self.atk.name)
        self.begin_fight(first)

    def handle_click(self, pos):
        # 点将那一屏：直接点守将的**卡片**也算选中（卡片又大又显眼，是人的自然动作），
        # 不必非得去够底部那排小按钮。
        if self.state == self.PICK_DEFENDER:
            for i, gen in enumerate(self.defenders):
                if self.card_rect(gen).collidepoint(pos):
                    self.act(f"pick:{i}")
                    return
        for _label, rect, action in self.buttons():
            if rect.collidepoint(pos):
                self.act(action)
                return

    def scroll_history(self, steps):
        """往上翻 / 往下翻战斗过程（滚轮一次几行、方向键一行）。"""
        self.scroll = max(0, min(self.max_scroll, self.scroll + steps))

    @property
    def max_scroll(self):
        return max(0, len(self.rounds) - self.HIST_ROWS)

    def primary(self):
        """空格 / 回车：走第一个按钮，不会误触撤退。

        点将和挑撤退方向那两屏例外：那里的按钮是"替自己人做决定"，按空格什么都不做。
        """
        if self.state in (self.PICK_DEFENDER, self.RETREAT_PICK):
            return
        buttons = self.buttons()
        if buttons:
            self.act(buttons[0][2])

    # ---------------- 绘制 ----------------
    def card_rect(self, gen):
        if self.state == self.PICK_DEFENDER:   # 点将那一屏：两名守将各占一张牌
            return pygame.Rect(self.CARD_X[self.defenders.index(gen) % 2],
                               self.CARD_Y, self.CARD_W, self.CARD_H)
        return pygame.Rect(self.CARD_X[0 if gen is self.atk else 1],
                           self.CARD_Y, self.CARD_W, self.CARD_H)

    def draw(self, screen, mouse_pos):
        screen.fill(C_BG)
        self.draw_header(screen)
        if self.state == self.PICK_DEFENDER:
            for i, gen in enumerate(self.defenders):
                self.draw_card(screen, gen, f"守将 {i + 1}")
        else:
            self.draw_card(screen, self.atk, "攻方")
            self.draw_card(screen, self.dfd, "守方")
            self.draw_vs(screen)
        if self.state != self.RETREAT_PICK:    # 挑撤退方向时，那块地方让给选项按钮
            self.draw_history(screen)
        self.draw_controls(screen, mouse_pos)

    def draw_header(self, screen):
        title = get_font(26, bold=True).render("交战", True, C_TEXT)
        screen.blit(title, title.get_rect(midtop=(WIN_W // 2, 16)))
        if self.dfd is None:                   # 点将阶段还没有"当前守将"
            sub = (f"{self.atk.name}（{PLAYER_NAME[self.atk.owner]}）进攻 {self.place}，"
                   f"守军 {len(self.defenders)} 人")
        else:
            sub = (f"{self.atk.name}（{PLAYER_NAME[self.atk.owner]}）进攻 "
                   f"{self.dfd.name}（{PLAYER_NAME[self.dfd.owner]}）驻守的 {self.place}")
            if len(self.defenders) > 1:
                sub += f"　·　第 {self.fight_no} 场 / 共 {len(self.defenders)} 场"
        surf = get_font(14).render(sub, True, C_TEXT_DIM)
        screen.blit(surf, surf.get_rect(midtop=(WIN_W // 2, 52)))

    def draw_vs(self, screen):
        cx = (self.CARD_X[0] + self.CARD_W + self.CARD_X[1]) // 2
        vs = get_font(30, bold=True).render("VS", True, (108, 116, 136))
        screen.blit(vs, vs.get_rect(center=(cx, self.CARD_Y + 56)))
        rnd = get_font(18, bold=True).render(f"第 {self.shown_round} 回合", True, C_TEXT)
        screen.blit(rnd, rnd.get_rect(center=(cx, self.CARD_Y + 100)))

    def card_frames(self, gen):
        """(没被抖动的卡片矩形, 掉血进度 0->1)——不在掉血动画中时进度是 None。"""
        rect = self.card_rect(gen)
        if self.state != self.IMPACT or not self.rounds:
            return rect, None
        progress = 1 - self.timer / self.IMPACT_FRAMES
        if self.timer > self.IMPACT_FRAMES - len(self.SHAKE):
            rect = rect.move(self.SHAKE[self.IMPACT_FRAMES - self.timer], 0)
        return rect, progress

    def draw_card(self, screen, gen, side_label):
        rect, progress = self.card_frames(gen)
        other = self.foe_of(gen)
        color = PLAYER_COLOR[gen.owner]
        pygame.draw.rect(screen, C_PANEL, rect, border_radius=12)

        # 顶栏：攻方/守方 + 站位
        tag = get_font(14, bold=True).render(side_label, True, color)
        screen.blit(tag, (rect.left + 16, rect.top + 12))
        where = (f"从 {self.origin_place} 进攻" if gen is self.atk else
                 f"驻守 {self.place}")
        if gen.city_boost:
            where += f"·据城力战 +{int(round_half_up((CITY_DEFENSE_MULT - 1) * 100))}%"
        info = get_font(12).render(where, True, C_TEXT_DIM)
        screen.blit(info, info.get_rect(midright=(rect.right - 16, rect.top + 20)))

        # 姓名 + 属性
        screen.blit(get_font(26, bold=True).render(gen.name, True, C_TEXT),
                    (rect.left + 16, rect.top + 42))
        screen.blit(get_font(14).render(
            f"武力 {gen.might}    智力 {gen.intellect}    {PLAYER_NAME[gen.owner]}",
            True, C_TEXT_DIM), (rect.left + 16, rect.top + 80))

        # 体力条 + 本回合的掉血范围
        self.draw_stamina_bar(screen, gen, pygame.Rect(rect.left + 16, rect.top + 116,
                                                       rect.width - 32, 30))
        screen.blit(get_font(13).render(
            f"每回合掉 1 ~ {self.loss_range(gen)} 点体力"
            f"（对方武力 {other.might} × {LOSS_MAX_PER_MIGHT}）", True, C_TEXT_DIM),
            (rect.left + 16, rect.top + 158))

        # 状态提示
        if not gen.alive:
            note, note_color = "阵亡", C_WARN
        elif self.chooser is gen:
            note, note_color = "该你决定（继续 / 撤退）", C_SELECT
        else:
            note, note_color = "", C_TEXT_DIM
        if self.state == self.PICK_DEFENDER:
            note, note_color = "点下方按钮决定谁先出阵", C_SELECT
        if note:
            screen.blit(get_font(16, bold=True).render(note, True, note_color),
                        (rect.left + 16, rect.top + 186))

        # 守方卡片：这一格还有谁在后面等着上场
        if gen is self.dfd and len(self.defenders) > 1:
            waiting = [g.name for g in self.defenders if g is not gen and g.alive]
            if waiting:
                screen.blit(get_font(12).render(f"阵中还有：{'、'.join(waiting)}",
                                                True, C_TEXT_DIM),
                            (rect.left + 16, rect.top + 214))

        # 掉血动画：卡片泛红 + 上浮的掉血数字
        if progress is not None:
            flash = pygame.Surface(rect.size, pygame.SRCALPHA)
            flash.fill((226, 84, 72, int(90 * max(0.0, 1 - progress * 2))))
            screen.blit(flash, rect.topleft)
            self.draw_loss_popup(screen, gen, rect, progress)

        if not gen.alive:
            veil = pygame.Surface(rect.size, pygame.SRCALPHA)
            veil.fill((10, 10, 14, 170))
            screen.blit(veil, rect.topleft)
            big = get_font(34, bold=True).render("阵亡", True, C_WARN)
            screen.blit(big, big.get_rect(center=rect.center))

        pygame.draw.rect(screen, color if gen.alive else (110, 110, 120),
                         rect, 3, border_radius=12)

    def draw_loss_popup(self, screen, gen, rect, progress):
        record = self.rounds[-1]
        loss = record["atk_loss"] if gen is self.atk else record["dfd_loss"]
        alpha = 255 if progress < 0.55 else max(0, int(255 * (1 - progress) / 0.45))
        pos = pygame.Rect(0, 0, 0, 0)
        pos.center = (rect.centerx, rect.centery - 24 - int(26 * progress))
        for dx, dy, col in ((2, 2, (40, 20, 16)), (0, 0, (255, 226, 150))):
            surf = get_font(46, bold=True).render(f"-{loss}", True, col)
            surf.set_alpha(alpha)
            screen.blit(surf, surf.get_rect(center=(pos.centerx + dx, pos.centery + dy)))

    def draw_stamina_bar(self, screen, gen, bar):
        pygame.draw.rect(screen, (20, 20, 26), bar, border_radius=6)
        cap = gen.stamina_cap                     # 守城加成时上限临时抬高
        ratio = max(0.0, min(1.0, gen.stamina / cap))
        fill = bar.copy()
        fill.width = max(1, int(bar.width * ratio))
        color = ((96, 208, 112) if ratio > 0.5 else
                 (232, 196, 72) if ratio > 0.25 else (226, 84, 72))
        pygame.draw.rect(screen, color, fill, border_radius=6)
        pygame.draw.rect(screen, (86, 94, 112), bar, 1, border_radius=6)
        txt = get_font(15, bold=True).render(
            f"体力 {gen.stamina} / {cap}", True, (245, 245, 245))
        screen.blit(txt, txt.get_rect(center=bar.center))

    def draw_history(self, screen):
        rect = self.HIST_RECT
        pygame.draw.rect(screen, C_PANEL, rect, border_radius=10)
        pygame.draw.rect(screen, C_BTN, rect, 1, border_radius=10)
        total = len(self.rounds)
        title = f"战斗过程（共 {total} 回合）" if total else "战斗过程"
        screen.blit(get_font(14, bold=True).render(title, True, C_TEXT_DIM),
                    (rect.left + 16, rect.top + 10))
        if not total:
            screen.blit(get_font(13).render("尚未交手。", True, C_TEXT_DIM),
                        (rect.left + 16, rect.top + 40))
            return

        end = total - self.scroll                        # 显示第 ... 到第 end 回合
        shown = self.rounds[max(0, end - self.HIST_ROWS):end]
        multi = len(self.defenders) > 1                  # 两场时回合号要带上场次
        if total > self.HIST_ROWS:
            first, last = shown[0], shown[-1]
            span = (f"{first['fight']}-{first['no']} ~ {last['fight']}-{last['no']}"
                    if multi else f"{first['no']}-{last['no']}")
            tip = f"显示第 {span} 回合 · 滚轮 / ↑↓ 翻看"
            tip_surf = get_font(12).render(tip, True, C_TEXT_DIM)
            screen.blit(tip_surf, tip_surf.get_rect(midright=(rect.right - 16, rect.top + 18)))

        font = get_font(14)
        x_round = rect.left + 16
        x_atk = x_round + (136 if multi else 104)
        x_dfd = x_atk + (rect.right - 16 - x_atk) // 2
        y = rect.top + 38
        for record in shown:
            label = (f"第 {record['fight']} 场·{record['no']} 回合" if multi
                     else f"第 {record['no']} 回合")
            screen.blit(font.render(label, True, C_TEXT_DIM), (x_round, y))
            for gen, x in ((self.atk, x_atk), (record["dfd"], x_dfd)):
                loss, hp = ((record["atk_loss"], record["atk_hp"]) if gen is self.atk else
                            (record["dfd_loss"], record["dfd_hp"]))
                text = f"{gen.name}  -{loss}  体力剩 {hp}"
                if hp <= 0:
                    text += "  阵亡"
                screen.blit(font.render(text, True, PLAYER_COLOR[gen.owner]), (x, y))
            y += 22

    def draw_controls(self, screen, mouse_pos):
        font = get_font(17, bold=True)
        if self.state == self.OVER:
            color = (255, 236, 160)
        elif self.state in (self.PICK_DEFENDER, self.RETREAT_PICK):
            color = C_SELECT                   # 这两步要人做选择，字提亮一点
        else:
            color = C_TEXT
        y = self.HINT_Y
        for line in wrap_text(self.hint, font, WIN_W - 120)[:2]:
            surf = font.render(line, True, color)
            screen.blit(surf, surf.get_rect(center=(WIN_W // 2, y)))
            y += 26
        for label, rect, action in self.buttons():
            hover = rect.collidepoint(mouse_pos)
            pygame.draw.rect(screen, C_BTN_HOVER if hover else C_BTN, rect, border_radius=10)
            pygame.draw.rect(screen, self.button_accent(action), rect, 2, border_radius=10)
            if action.startswith("retreat:"):     # 撤退选项格子小，字号跟着位置缩
                font = fit_font(label, rect.width - 14, start=min(15, rect.height - 8),
                                min_size=9)
            else:
                # 攻方那个"撤退（判负，再补 N 点）"比别的按钮长，兜一下免得溢出
                font = fit_font(label, rect.width - 14, start=16, min_size=9)
            txt = font.render(label, True, C_TEXT)
            screen.blit(txt, txt.get_rect(center=rect.center))
        if self.state == self.RETREAT_PICK:
            foot_text = ("点一个落脚点撤退 · 括号里是这次撤退要透支的行动力，"
                         "本回合累计不能超过 MOVE_POINTS"
                         " · 这里按空格 / 回车不生效，免得误撤")
        elif self.state == self.PICK_DEFENDER:
            foot_text = ("点守将卡片（或下方按钮）决定谁先出阵 · 排在后面的那位会在前一位下场后接着打"
                         " · 这里按空格 / 回车不生效，免得替你定顺序")
        else:
            foot_text = ("空格 / 回车 = 第一个按钮（交手、继续缠斗、返回战场）  ·  撤退请点按钮"
                         "  ·  滚轮 / ↑↓ 翻看战斗过程  ·  战斗中 ESC / R 不生效")
        foot = get_font(12).render(foot_text, True, C_TEXT_DIM)
        screen.blit(foot, foot.get_rect(center=(WIN_W // 2, self.FOOT_Y)))

    def button_accent(self, action):
        if action in ("atk_retreat", "dfd_retreat") or action.startswith("retreat:"):
            return C_WARN
        if action == "roll" or action == "back" or action.startswith("pick:"):
            return C_GOLD
        return (86, 94, 112)




# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def make_buttons():
    x0 = MARGIN * 2 + BOARD
    w = SIDEBAR - 32
    labels = [("结束回合 (空格)", "end"), ("重新开局 (R)", "restart"), ("退出 (ESC)", "quit")]
    rects = []
    # 从面板底部往上排，与 draw_sidebar 保持一致
    panel_bottom = MARGIN + BOARD
    for i, (label, key) in enumerate(reversed(labels)):
        top = panel_bottom - 14 - (i + 1) * 44
        rects.append((label, pygame.Rect(x0 + 16, top, w, 36), key))
    return list(reversed(rects))


def show_config_error(exc):
    """开局就读不到合法配置：开个小窗把原因显示出来，等玩家关掉。"""
    pygame.init()
    screen = pygame.display.set_mode((780, 420))
    pygame.display.set_caption("三国战棋 - 配置有误")
    clock = pygame.time.Clock()
    font = get_font(15)

    lines = wrap_text(f"配置有误：{exc}", font, 730)
    lines += [""]
    for path in (MAP_FILE, ROSTER_FILE):
        lines += wrap_text(f"配置文件：{path}", font, 730)
    lines += wrap_text("修好后重新运行程序即可。", font, 730)

    waiting = True
    while waiting:
        for event in pygame.event.get():
            if event.type in (pygame.QUIT, pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
                waiting = False
        screen.fill(C_BG)
        y = 28
        for line in lines:
            screen.blit(font.render(line, True, C_TEXT), (26, y))
            y += 26
        pygame.display.flip()
        clock.tick(FPS)


def choose_mode(screen, clock):
    """开局的模式选择 -> (人类执哪边, 玩家人数, 电脑版本)；关窗口 / ESC 退出返回 None。

    三步走：先选一人游玩还是两人同机；选了一人再选执蜀还是执魏，最后选电脑用哪个版本。
    中间两步按 ESC 是回上一步，不是退出（版本那一步退回"执哪边"）。
    """
    idx = menu_screen(screen, clock, "选择对局模式",
                      [("一人游玩（对抗电脑）", "你执一边，另一边交给电脑"),
                       ("两人同机对战", "两个人轮流用同一台电脑")],
                      "点一下选项，或按 1 / 2　·　ESC 退出")
    if idx is None:
        return None
    if idx == 1:
        return P1, 2, DEFAULT_AI_VERSION      # 两人同机：没有电脑，版本用默认值占位
    return choose_side(screen, clock)


def choose_side(screen, clock):
    """选执哪一边 -> 继续走下一步；按 ESC 回第一步（只有第一步的 ESC 才是退出）。"""
    idx = menu_screen(screen, clock, "你执哪一边？",
                      [(f"执 {FACTION_SHORT[P1]}（红，先手）", "玩家一，开局先走"),
                       (f"执 {FACTION_SHORT[P2]}（蓝，后手）", "玩家二，电脑先走一步")],
                      "点一下选项，或按 1 / 2　·　ESC 返回上一步")
    if idx is None:
        return choose_mode(screen, clock)      # 退回第一步
    return choose_ai_version(screen, clock, P1 if idx == 0 else P2)


def choose_ai_version(screen, clock, human_side):
    """选电脑用哪个版本 -> (人类执哪边, 1, 版本名)；按 ESC 回上一步。

    版本只是电脑的棋力档位（`AI_VERSION_ORDER`：梅 / 兰 / 竹 / 菊，第一项是默认的参照版），
    选哪个都不影响规则，只影响对面怎么想。
    """
    other = FACTION_SHORT[1 - human_side]
    options = [(f"{name}版——{AI_VERSION_NOTE[name]}", f"电脑执 {other}")
               for name in AI_VERSION_ORDER]
    idx = menu_screen(screen, clock, "电脑用哪个版本？", options,
                      f"点一下选项，或按 1–{len(options)}　·　ESC 返回上一步")
    if idx is None:
        return choose_side(screen, clock)      # 退回上一步
    return human_side, 1, AI_VERSION_ORDER[idx]


def menu_screen(screen, clock, title, options, foot):
    """画一屏选项 -> 选中的下标；关窗口 / 按 ESC 返回 None（选项是 (标题, 说明) 两项）。

    按钮高度随选项个数自适应：选项多时压缩高度、收紧间距，保证整屏（含标题和底部
    提示）都落在窗口里——版本从 2 个加到 4 个就是这么撑住的。
    """
    f_title = get_font(30, bold=True)
    f_btn = get_font(19, bold=True)
    f_note = get_font(13)
    f_foot = get_font(13)
    n = len(options)
    width = 520
    gap = 18 if n <= 3 else 12
    height = min(72, (WIN_H - 210 - (n - 1) * gap) // n)   # 210 ≈ 上留白 + 标题 + 底部提示
    block = n * height + (n - 1) * gap
    top = max(96, WIN_H // 2 - block // 2)                 # 太靠上就贴住，别越过标题
    rects = [pygame.Rect((WIN_W - width) // 2, top + i * (height + gap), width, height)
             for i in range(n)]

    while True:
        mouse = pygame.mouse.get_pos()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return None
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return None
                if pygame.K_1 <= event.key <= pygame.K_9:
                    idx = event.key - pygame.K_1
                    if idx < n:
                        return idx
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                for i, rect in enumerate(rects):
                    if rect.collidepoint(event.pos):
                        return i

        screen.fill(C_BG)
        head = f_title.render(title, True, C_TEXT)
        screen.blit(head, head.get_rect(midtop=(WIN_W // 2, top - 76)))
        for i, (label, note) in enumerate(options):
            rect = rects[i]
            hover = rect.collidepoint(mouse)
            pygame.draw.rect(screen, C_BTN_HOVER if hover else C_PANEL, rect, border_radius=10)
            pygame.draw.rect(screen, C_GOLD if hover else C_BTN, rect, 2, border_radius=10)
            txt = f_btn.render(label, True, C_TEXT)
            screen.blit(txt, txt.get_rect(midleft=(rect.left + 52, rect.centery - 10)))
            sub = f_note.render(note, True, C_TEXT_DIM)
            screen.blit(sub, sub.get_rect(midleft=(rect.left + 52, rect.centery + 14)))
            num = get_font(16, bold=True).render(str(i + 1), True, C_GOLD)
            screen.blit(num, num.get_rect(center=(rect.left + 28, rect.center[1])))
        tip = f_foot.render(foot, True, C_TEXT_DIM)
        screen.blit(tip, tip.get_rect(midbottom=(WIN_W // 2, WIN_H - 44)))
        pygame.display.flip()
        clock.tick(FPS)


def handle_game_event(game, event, buttons):
    """处理一个 pygame 事件（棋盘 / 交战屏 / 各种弹窗都归这里）。-> None / "menu" / "quit"。

    主程序 `play_match` 和进攻策略测试台（`offense_bench.py`）**共用这一套**：键盘鼠标的归属规则只写
    一份，测试台里守方的点选、弹窗、结束回合就跟正式对局一模一样。返回 `"menu"` 表示该退回选择屏
    （主程序按 R）、`"quit"` 表示退出程序（ESC / 关窗口）；其余返回 None，调用方继续跑。
    """
    if event.type == pygame.QUIT:
        return "quit"
    if game.battle is not None:
        # 交战界面键盘鼠标都归它管；ESC / R 故意不生效，免得误触把这一局丢了
        battle = game.battle
        key = event.key if event.type == pygame.KEYDOWN else None
        if key == pygame.K_UP:                 # 翻看战报随时都行，不算做决定
            battle.scroll_history(1)
        elif key == pygame.K_DOWN:
            battle.scroll_history(-1)
        elif key == pygame.K_PAGEUP:
            battle.scroll_history(battle.HIST_ROWS)
        elif key == pygame.K_PAGEDOWN:
            battle.scroll_history(-battle.HIST_ROWS)
        elif event.type == pygame.MOUSEWHEEL:
            battle.scroll_history(event.y)
        elif game.human_may_act():
            # 轮到人类这一方做决定时键盘鼠标才接；电脑在想的时候点了不算
            if key in (pygame.K_SPACE, pygame.K_RETURN):
                battle.primary()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                battle.handle_click(event.pos)
        return None
    if game.grain_pick is not None:
        # 「带多少粮草」弹窗：这一步的键盘鼠标都归它管；放在前面是为了盖住
        # 下面的空格结束回合 / 点棋盘，不然会带着弹窗走子
        pick = game.grain_pick
        if event.type == pygame.KEYDOWN:
            step = {pygame.K_LEFT: -1, pygame.K_RIGHT: 1,
                    pygame.K_DOWN: -10, pygame.K_UP: 10}.get(event.key)
            if step is not None:
                pick.adjust(step)
            elif event.key in (pygame.K_SPACE, pygame.K_RETURN):
                pick.primary()
            elif event.key == pygame.K_ESCAPE:
                pick.act("cancel")        # 这一步当作没走，武将继续选着
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            pick.handle_click(event.pos)
        return None
    if game.cunning is not None:
        # 用计弹窗：键盘鼠标都归它管，挑定计谋（或取消）才回到棋盘。
        if event.type == pygame.KEYDOWN:
            game.cunning.handle_key(event.key)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            game.cunning.handle_click(event.pos)
        return None
    if game.aim is not None:
        # 用计瞄准态：点目标才结算
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            game.aim.cancel()
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            mx, my = event.pos
            board_x, board_y = mx - MARGIN, my - MARGIN
            if 0 <= board_x < BOARD and 0 <= board_y < BOARD:
                game.aim.handle_click(board_y // CELL, board_x // CELL)
        return None
    if event.type == pygame.MOUSEWHEEL:
        # 滚轮翻武将总览：鼠标得在总览那块上，免得跟别处的手感打架
        if game.roster_rect(buttons).collidepoint(pygame.mouse.get_pos()):
            game.scroll_roster(-event.y)      # 滚轮向下（y 为负）= 往后翻
        return None
    if event.type == pygame.KEYDOWN:
        if event.key == pygame.K_ESCAPE:
            return "quit"
        if event.key in (pygame.K_SPACE, pygame.K_RETURN):
            if game.human_may_act():
                game.end_turn()
        elif event.key == pygame.K_r:
            return "menu"
        return None
    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
        mx, my = event.pos
        hit_button = None
        for _label, rect, key in buttons:
            if rect.collidepoint(mx, my):
                hit_button = key
                break
        if hit_button == "end":
            if game.human_may_act():
                game.end_turn()
        elif hit_button == "restart":
            return "menu"
        elif hit_button == "quit":
            return "quit"
        elif game.human_may_act():
            # 「用计」和「吃粮」按钮都长在详情框里，先试它们，再交给棋盘
            cunning = game.cunning_button()
            if cunning is not None and cunning.collidepoint(mx, my):
                game.open_cunning()
                return None
            eat = next((n for _l, rect, n in game.eat_buttons()
                        if rect.collidepoint(mx, my)), None)
            if eat is not None:
                game.eat_grain(game.selected, eat)
                return None
            # 按钮都在右侧面板里（x 远大于棋盘右边界），所以落不到按钮上的点击
            # 只要在棋盘矩形内就交给棋盘；别再按 y 去卡，否则最下面几行点不到。
            board_x, board_y = mx - MARGIN, my - MARGIN
            if 0 <= board_x < BOARD and 0 <= board_y < BOARD:
                game.handle_board_click(board_y // CELL, board_x // CELL)
    return None


def play_match(screen, clock, game, buttons):
    """跑一局；按 R 回模式选择屏（返回 True），ESC / 关窗口退出程序（返回 False）。"""
    back_to_menu = False
    running = True
    while running:
        mouse_pos = pygame.mouse.get_pos()
        for event in pygame.event.get():
            verdict = handle_game_event(game, event, buttons)   # 输入处理与测试台共用
            if verdict == "quit":
                running = False
            elif verdict == "menu":
                back_to_menu = True
                running = False

        battle = game.battle
        if battle is not None:
            battle.update()                       # 掉血动画放完后自动进入下一步
            ai_pump(game)                         # 轮到电脑就让它走一步
            battle.draw(screen, mouse_pos)
        else:
            game.tick()
            ai_pump(game)
            game.draw(screen, mouse_pos, buttons)
        pygame.display.flip()
        clock.tick(FPS)

    return back_to_menu


def main():
    try:
        game = Game()                         # 先确认配置没问题，再开窗口
    except ConfigError as exc:
        print(f"配置有误：{exc}", file=sys.stderr)
        for path in (MAP_FILE, ROSTER_FILE):
            print(f"配置文件：{path}", file=sys.stderr)
        show_config_error(exc)
        pygame.quit()
        sys.exit(1)

    pygame.init()
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption("三国战棋 - 10x10 回合制对战")
    clock = pygame.time.Clock()

    buttons = make_buttons()

    while True:
        choice = choose_mode(screen, clock)
        if choice is None:
            break
        human_side, players, ai_version = choice
        try:
            game.start_match(human_side, players, ai_version)
        except ConfigError as exc:
            # 选完模式才发现配置被改坏了：提示一下，回到选择屏
            game.push_log(f"开不了新局：{exc}")
            game.flash("配置有误，开不了新局（详见战报）")
            continue
        if not play_match(screen, clock, game, buttons):
            break

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
