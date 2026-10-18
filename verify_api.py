"""Read-only check that every assistant tool works with your key.

Usage (from the project folder, with GW2_API_KEY set):
    python verify_api.py                # run everything
    python verify_api.py wizards pvp    # run only tools with these words in the name

Shows OK / WARNING / FAIL per tool, never printing your data.
  OK       the tool responded without errors.
  WARNING  responded, but with a partial error (e.g., missing permission, Wiki blocked).
  FAIL     the tool broke (bug or unexpected error).
Writes nothing: save_snapshot is left out on purpose.
"""

import os
import sys
import time

import gw2_server as g

# One line per tool: (filter name, callable).
# save_snapshot is left out on purpose (writes a file).
CHECKS = [
    ("diagnostics", lambda: g.diagnostics()),
    ("account_info", lambda: g.account_info()),
    ("account_wallet", lambda: g.account_wallet()),
    ("materials_with_names", lambda: g.materials_with_names(limit=5)),
    ("account_bank", lambda: g.account_bank(limit=5)),
    ("character_list", lambda: g.character_list()),
    ("character_crafting", lambda: g.character_crafting()),
    ("account_inventory", lambda: g.account_inventory(item_limit=3)),
    ("equipment", lambda: g.equipment()),
    ("daily_weekly_progress", lambda: g.daily_weekly_progress()),
    ("masteries", lambda: g.masteries()),
    ("achievement_details", lambda: g.achievement_details([1734])),
    ("achievement_points", lambda: g.achievement_points()),
    ("progress_guide", lambda: g.progress_guide(limit=3)),
    ("action_plan", lambda: g.action_plan(limit=3)),
    ("missing_achievements", lambda: g.missing_achievements(1734)),
    ("pending_areas", lambda: g.pending_areas(1734, locate=False)),
    ("find_area", lambda: g.find_area("Metrica Province")),
    ("unlocks", lambda: g.unlocks()),
    ("legendary_armory", lambda: g.legendary_armory(missing_limit=3)),
    ("wizards_vault", lambda: g.wizards_vault()),
    ("what_to_do_today", lambda: g.what_to_do_today()),
    ("event_timers", lambda: g.event_timers(horizon_minutes=30)),
    ("item_info", lambda: g.item_info(item_id=19684)),
    ("item_prices", lambda: g.item_prices([19684, 19707])),
    ("tp_listings", lambda: g.tp_listings(item_id=19684, depth=3)),
    ("account_value", lambda: g.account_value(limit=3)),
    ("tp_transactions", lambda: g.tp_transactions(limit=3)),
    ("pvp_wvw", lambda: g.pvp_wvw()),
    ("raid_progress", lambda: g.raid_progress()),
    ("item_craft_info", lambda: g.item_craft_info(item_name="Mystic Clover")),
    ("find_craftable", lambda: g.find_craftable(limit=3)),
    ("recipes_using", lambda: g.recipes_using(item_id=19721, limit=3)),
    ("craft_profit", lambda: g.craft_profit(limit=3)),
    ("bank_cleanup", lambda: g.bank_cleanup(min_value=5000, limit=3)),
    ("legendary_route", lambda: g.legendary_route(item_id=78425, depth=2)),
    ("search_wiki", lambda: g.search_wiki("Mystic Clover")),
    ("wiki_details", lambda: g.wiki_details("Mystic Clover")),
    ("compare_progress", lambda: g.compare_progress(7)),
]


# Server error keys are "error" / "errors"
def classify(result):
    """Returns ('OK' | 'WARNING', detail)."""
    if isinstance(result, dict):
        if "error" in result:
            return "WARNING", str(result["error"])
        if result.get("errors"):
            parts = result["errors"]
            names = ", ".join(parts) if isinstance(parts, dict) else str(parts)
            return "WARNING", f"partial errors in: {names}"
    return "OK", ""


def main(filters):
    if not (os.environ.get("GW2_API_KEY") or "").strip():
        print("GW2_API_KEY is not set in this terminal. Set it and run again.")
        return 2

    selected = [
        (name, fn) for name, fn in CHECKS
        if not filters or any(f.lower() in name for f in filters)
    ]
    counts = {"OK": 0, "WARNING": 0, "FAIL": 0}
    for name, fn in selected:
        start = time.time()
        try:
            status, detail = classify(fn())
        except g.GW2Error as err:
            status, detail = "WARNING", str(err)
        except Exception as err:  # unexpected bug
            status, detail = "FAIL", f"{type(err).__name__}: {err}"
        counts[status] += 1
        seconds = time.time() - start
        line = f"{status:<8} {name:<26} {seconds:5.1f}s"
        print(line + (f"  {detail[:140]}" if detail else ""))

    print(f"\n{counts['OK']} OK, {counts['WARNING']} warning(s), {counts['FAIL']} failure(s)")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
