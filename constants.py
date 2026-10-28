# -*- coding: utf-8 -*-
"""全局常量与配色。

从 warlords.py 拆出：屏幕尺寸、地形枚举、规则数值、玩家与阵营、配置文件名、配色。
纯数据，不 import pygame——config.py / ai.py 因此可以脱离图形环境单独测试。
"""
from collections import namedtuple
from pathlib import Path

GRID = 10           # 10x10 格子
CELL = 64           # 每格像素
MARGIN = 20
# 侧边栏宽度：选中武将详情和「比分板」并排一行，所以比早先的 320 宽（见 Game.INFO_COL_GAP）
SIDEBAR = 460
BOARD = GRID * CELL
WIN_W = BOARD + MARGIN * 3 + SIDEBAR
WIN_H = BOARD + MARGIN * 2
FPS = 60

# 地形枚举（未来加第 4 种：追加常量 + 补 TERRAIN_NAME/REGEN/配色）
OBSTACLE, PLAIN, CITY = 0, 1, 2
TERRAIN_NAME = {OBSTACLE: "障碍", PLAIN: "通路", CITY: "城池"}

TURN_LIMIT = 100         # 总回合数上限（双方各行动一次算 2 回合）
MAX_STAMINA = 100
MOVE_POINTS = 3         # 每回合移动力：最多走 3 格，每次一格（与体力无关）
ATTACK_COST = 1         # 踏进敌格交战本身花几点移动力。把守军打光、就地进占，这就是全部花费
ATK_RETREAT_COST = 1    # 攻方打了一半收手撤退，要在进攻那笔钱上**再加**几点
CELL_CAPACITY = 2       # 同一格最多站几名同阵营武将（敌我永远不能同格）
RETREAT_MAX = MOVE_POINTS   # 一次撤退最多退几格；同一回合累计撤退格数封顶 MOVE_POINTS

# 撤退的两种算法不一样，别混：
#   攻方撤退是"当场收手"——人在交战期间本来就没动窝，撤退只是白扔 ATTACK_COST + ATK_RETREAT_COST
#   两点移动力，**不赊账**（攻方没有透支一说）。付不起 ATK_RETREAT_COST 时就没有撤退选项，
#   只能接着缠斗；打赢了（守军清空、就地进占）则只花 ATTACK_COST。
#   守方撤退是"真挪窝"——撤几格就**透支下回合几点**行动力（见 General.move_debt）。
REGEN = {OBSTACLE: 0, PLAIN: 2, CITY: 3}

# --------------------------------------------------------------------------
# 计谋（智力终于派上用场了；规则与算式都在 stratagems.py，界面在 warlords.py）
# --------------------------------------------------------------------------
# 用一次计谋的代价：1 点行动力 + 10 点体力，两样缺一不可，而且**用出去的那一刻**就结清，
# 不管成没成——扔出去的谋略没有收回来的。所以挑来挑去不花钱（弹窗里可以随便看）。
#
# 体力卡的是"**多于** 10"而不是"至少 10"：正好 10 点的话付完就归零，等于用计自尽。
# 那不是"够用"该有的样子，所以留 1 点底（见 stratagems.cost_ok）。
STRATAGEM_MOVE_COST = 1
STRATAGEM_STAMINA_COST = 10

# 四种计谋的算式（系数都摆在这儿，好调；成功率一律夹在 0~1）
#   暗度陈仓：成功率 = 智力 / 25                        —— 只看自己，不看目标
#   调虎离山：成功率 = (我方智力 - 敌方智力 - 6) / 8
#             调动格数上限 = min(floor((智力差 - 2) / 2), 6)
#   内讧    ：成功率 = (我方智力 - 敌方智力 - 8) / 8
#   劫粮    ：成功率 = (我方智力 - 敌方智力) / 20
STRATAGEM_SNEAK_DIVISOR = 25
STRATAGEM_LURE_OFFSET = 6
STRATAGEM_LURE_DIVISOR = 8
STRATAGEM_LURE_MOVE_OFFSET = 2
STRATAGEM_LURE_MOVE_DIVISOR = 2
STRATAGEM_LURE_MOVE_CAP = 6
STRATAGEM_INFIGHT_OFFSET = 8
STRATAGEM_INFIGHT_DIVISOR = 8
STRATAGEM_RAID_DIVISOR = 20

# 调虎离山 / 内讧的目标必须在我方这个**欧氏距离**以内（格子中心到格子中心的直线距离）
STRATAGEM_RANGE = 3
# 劫粮的目标格上没武将、或只有己方武将时，"敌方智力"按这个空场值算
STRATAGEM_RAID_BASE_INT = 10

# 分数：总分 = 将领分 + 控制地域分（侧边栏常驻一块比分板实时显示，见 Game.scoreboard）
# 将领分：本方每名**存活**武将各算 (SCORE_GENERAL_BASE + 体力) × 武力，逐名求和
# 控制地域分：每块可通行地域归**行动距离最近**的一方（两边一样近算争夺中，谁也不算），
#            一格的分数 = 地形分 + 格上物品（粮草）分。障碍不算地盘，不参与归属判定。
SCORE_GENERAL_BASE = 100
SCORE_TERRAIN = {OBSTACLE: 0, PLAIN: 200, CITY: 3000}
SCORE_GRAIN = 150        # 每石粮草值多少分（粮草堆在格子上，所以是"地域上的物品"）

MAX_STAT = 22           # generals.txt 里武力/智力的上限（下限 1）

# 粮草：每 GRAIN_EVERY_TURNS 个回合，各城池入库一次（见 Game.produce_grain），
# 具体落在 4 / 9 / 14 / 19 … 这些回合的**开头**（回合数 % 5 == 4），其余回合不产粮。
# 一局 100 回合下来共入库 20 批，不算堆积如山；开局先攒三回合兵，第 4 回合才见第一批粮。
# 粮草**没有归属**——谁站到那一格都能取用。武将只是脚夫：搬的时候从出发格取、
# 到落点格当场卸下，身上不存粮（见 Game.walk），所以粮草永远只存在于格子上。
GRAIN_EVERY_TURNS = 5    # 每几个回合入库一次
GRAIN_TURN_FIRST = 4     # 第一次入库在第几回合（之后每隔 GRAIN_EVERY_TURNS 回合再来一次）
GRAIN_PER_CITY = 1       # 每个入库回合，每座城池产几石
GRAIN_HEAL = 10          # 吃 1 石回多少体力
GRAIN_EAT_MAX = 3        # 一口最多吃几石（吃 k 石要花 k 点移动力，所以这也是花费上限）
GRAIN_PER_STONE = "石"    # 单位，只用于显示

# 守城加成：防守方脚下是城池时，开打前体力 ×CITY_DEFENSE_MULT（四舍五入），
# 打完 ÷CITY_DEFENSE_MULT 还原（同样四舍五入）。两次取整在 0-120 内可精确还原。
CITY_DEFENSE_MULT = 1.2

# 电脑的粮草性子、以及它其余的"性子"旋钮，都**不在这个文件里了**：电脑拆成了两个版本
# （`mei/` 与 `ju/`），每版各带一整套性子旋钮——通用性子在 `<版本>/knobs.py`，
# 进攻的 `KILL_W` 那批在 `<版本>/offense.py` 顶部，防守的 `OWN_*` 在 `<版本>/defense.py` 顶部。
# 所以改"菊"的性子不会牵动"梅"。这个文件里只剩跟策略无关的调度常量 `AI_STEP_FRAMES`。

# 交战每回合掉的体力：只看**对方**武力，取闭区间 [1, LOSS_MAX_PER_MIGHT * 对方武力]
# 里的一个随机整数。武力上限 22 -> 单回合最多掉 44 点，所以体力 100 的武将大约 3-6 回合
# 就会见底；又因为双方每回合至少掉 1 点，单挑最迟 100 回合内必定分出结果，不会无限拖下去。
LOSS_MAX_PER_MIGHT = 2

P1, P2 = 0, 1
PLAYER_NAME = {P1: "玩家一（红·蜀）", P2: "玩家二（蓝·魏）"}
PLAYER_COLOR = {P1: (206, 74, 62), P2: (72, 124, 214)}
PLAYER_COLOR_DARK = {P1: (128, 42, 36), P2: (40, 74, 136)}

# 一方由谁操控（1P 模式下一人一机，2P 模式下都是人）
HUMAN, AI = "human", "ai"
FACTION_SHORT = {P1: "蜀", P2: "魏"}
AI_STEP_FRAMES = 24     # 电脑每步之间的停顿（帧，60fps 约 0.4 秒，让人看清它干了什么）

# 电脑的四个版本，各自是一个完整的包（互不 import，各带自己那一整套旋钮）。
# **梅**是参照版、冻结不动；**菊**是升级版（当前主力），以后提水平只动它；**兰**和**竹**
# 从菊复制而来、此刻与菊完全一致，供各自独立迭代。注册表只登记"版本名 -> 包"，
# 改版本名 / 加版本都在这里改（顶层 ai.resolve 按它取包）。
AI_VERSIONS = {"梅": "mei", "兰": "lan", "竹": "zhu", "菊": "ju"}
AI_VERSION_ORDER = ("梅", "兰", "竹", "菊")   # 开局选择屏上的排列顺序（第一项也是"默认"那一项）
AI_VERSION_NOTE = {               # 选择屏上跟在版本名后面的一句说明
    "梅": "参照版，冻结不动",
    "兰": "自菊复制，独立迭代",
    "竹": "自菊复制，独立迭代",
    "菊": "升级版，迭代这一版",
}
DEFAULT_AI_VERSION = "梅"         # 没显式指定版本时用哪个（对战脚本的默认对手）

# 对局录像（replay.py）：只要是**有电脑参与**的局（开战脚本、ai_match、1P）就自动记一份，
# 存到 replays/ 下。2P 纯人对战不记（没电脑、复盘意义小），也省得一堆文件。
REPLAY = True

# 固定地图配置（不做随机生成）：每格写「地形:地名」，地形和地名同在一个文件里
MAP_FILE = Path(__file__).with_name("map.txt")
CITY_COUNT = 10                   # map.txt 里应正好有 10 座城池
NAME_SEP = ":"                    # 地形与地名的分隔符：.:樊城 / C:襄阳
OBSTACLE_CHAR = "#"               # 障碍格只写这一个字符，没有地名
CITY_CHAR = "C"                   # 城池格写成 C:城名
MAP_LEGEND = {                    # 配置字符（冒号前的那个字符）-> 地形
    ".": PLAIN,
    OBSTACLE_CHAR: OBSTACLE,
    CITY_CHAR: CITY,
}
# 出生点不写进地图：开局时玩家二（魏）占北半场（前 5 行）、玩家一（蜀）占南半场（后 5 行），
# 各自的武将再从本半场的通路/城池格里随机抽（障碍格不能站人）
SPAWN_ROWS = {P2: range(0, GRID // 2), P1: range(GRID // 2, GRID)}

# 武将配置（写在 generals.txt 里；武力/智力是固定值，不随机）
ROSTER_FILE = Path(__file__).with_name("generals.txt")
ROSTER_SIZE = 20                  # generals.txt 里每方应有 20 名武将
# 开局每方从本方 20 名里随机抽几名上阵：双方各配各的，不要求一样多。
# 上限是 ROSTER_SIZE；改大要考虑两件事——本方半场得放得下（validate_map 会拦），
# 以及侧边栏的武将总览够不够显示（见 Game.ROSTER_MAX_ROWS，超过就靠滚轮翻）。
DEPLOY_COUNT = {P1: 13, P2: 13}
FACTION_PLAYER = {"蜀": P1, "魏": P2}   # 配置里的阵营 -> 玩家

# 配置里的一名武将：姓名 + 固定武力/智力（体力是每局开始时满值）
GeneralSpec = namedtuple("GeneralSpec", "name might intellect")

# 配色
C_BG = (22, 24, 30)
C_PANEL = (32, 35, 44)
C_PLAIN = (58, 76, 58)
C_PLAIN_ALT = (52, 69, 52)
C_OBSTACLE = (44, 41, 39)
C_OBSTACLE_LINE = (72, 66, 62)
C_CITY = (128, 106, 72)
C_CITY_ALT = (114, 94, 64)
C_GRID = (30, 33, 30)
C_PLACE = (168, 182, 164)        # 地图上通路的地名
C_PLACE_CITY = (214, 190, 140)   # 地图上城池的地名
C_TEXT = (228, 231, 238)
C_TEXT_DIM = (150, 156, 170)
C_SELECT = (255, 236, 150)
C_ATTACK = (255, 96, 82)
C_BTN = (52, 57, 70)
C_BTN_HOVER = (74, 82, 100)
C_GOLD = (198, 168, 96)          # 交战界面：主要按钮 / 结算文字
C_WARN = (206, 106, 92)          # 交战界面：撤退按钮 / 阵亡
C_GRAIN = (226, 198, 122)        # 粮草：徽标文字 / 边框
C_GRAIN_BG = (62, 50, 24)        # 粮草：徽标底色（压在城墙等花哨地形上也看得清）
C_GRAIN_DIM = (150, 128, 78)     # 粮草：不可取用 / 次要说明
C_CUNNING = (176, 138, 224)      # 计谋：瞄准高亮 / 弹窗边框（紫，跟粮草的金、交战的朱红都不撞）
C_CUNNING_BG = (46, 34, 62)      # 计谋：按钮底色
C_CUNNING_DIM = (126, 106, 152)  # 计谋：用不了的那几条 / 次要说明
