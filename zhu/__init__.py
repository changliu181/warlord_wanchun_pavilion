# -*- coding: utf-8 -*-
"""电脑版本「竹」。

**竹版**：从菊版复制而来（当前策略与菊版完全一致），以后这一版独立迭代。

对外是一版完整的电脑：顶层 ai.py 按「该做决定的那一方」调它。
对外接口（**升级竹时别改名**）：`map_step(game)` / `atk_should_hold(battle)` /
`defense.ai_should_hold(battle)` / `defense.ai_pick_defender(battle)` /
`defense.ai_pick_retreat(battle)`。
"""
from .ai import atk_should_hold, map_step
from . import defense, offense, tactics, knobs

__all__ = ["atk_should_hold", "map_step", "defense", "offense", "tactics", "knobs"]
