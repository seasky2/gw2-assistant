"""GW2 server tests with mocked API responses (no network, no real key).

Run:  python -m unittest -v test_gw2_server
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

import gw2_server as g

KEY = "SECRET-KEY-123"
API = g.API_BASE
WIKI = g.WIKI_BASE


class FakeResp:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else "{}")
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Router:
    """Replaces Session.get: routes by exact URL and records calls."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, url, handler):
        self.routes[url] = handler
        return self

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if url not in self.routes:
            return FakeResp(404, {"text": f"route not mocked: {url}"})
        handler = self.routes[url]
        if callable(handler):
            handler = handler(dict(params or {}), dict(headers or {}))
        if isinstance(handler, FakeResp):
            return handler
        return FakeResp(200, handler)

    def count(self, url):
        return sum(1 for c in self.calls if c[0] == url)


def by_ids(tabela):
    def handler(params, headers):
        ids = [int(x) for x in params["ids"].split(",")]
        return [tabela[i] for i in ids if i in tabela]
    return handler


class Base(unittest.TestCase):
    def setUp(self):
        g._cache.clear()
        self.router = Router()
        patchers = [
            mock.patch.object(g._session, "get", self.router.get),
            mock.patch.object(g.time, "sleep", lambda s: None),
            mock.patch.dict(os.environ, {"GW2_API_KEY": KEY}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def route(self, caminho, handler):
        self.router.add(f"{API}/{caminho}", handler)


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

class TestInfra(Base):
    def test_key_goes_in_header_not_url(self):
        self.route("account", {"name": "x.1234"})
        g.gw2_get("account")
        url, params, headers = self.router.calls[0]
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertNotIn("access_token", params)
        self.assertNotIn(KEY, url)

    def test_public_endpoint_does_not_send_key(self):
        self.route("items", [])
        g.gw2_get("items", {"ids": "1"}, authenticated=False)
        self.assertNotIn("Authorization", self.router.calls[0][2])

    def test_403_error_does_not_leak_key_and_suggests_permission(self):
        self.route("account/wallet", FakeResp(403, {"text": "requires scope wallet"}))
        with self.assertRaises(g.GW2Error) as ctx:
            g.gw2_get("account/wallet")
        msg = str(ctx.exception)
        self.assertNotIn(KEY, msg)
        self.assertIn("wallet", msg)
        self.assertIn("diagnostics", msg)

    def test_401_error_clear_message(self):
        self.route("account", FakeResp(401, {"text": "invalid key"}))
        with self.assertRaises(g.GW2Error) as ctx:
            g.gw2_get("account")
        self.assertIn("invalid or expired", str(ctx.exception))

    def test_network_error_does_not_leak_key(self):
        def boom(url, params=None, headers=None, timeout=None):
            raise requests.ConnectionError(f"falhou {url}?access_token={KEY}")
        with mock.patch.object(g._session, "get", boom):
            with self.assertRaises(g.GW2Error) as ctx:
                g.gw2_get("account")
        msg = str(ctx.exception)
        self.assertNotIn(KEY, msg)
        self.assertIn("ConnectionError", msg)

    def test_retry_on_503_then_success(self):
        estado = {"n": 0}

        def handler(params, headers):
            estado["n"] += 1
            return FakeResp(503) if estado["n"] < 3 else FakeResp(200, {"ok": True})

        self.route("account", handler)
        self.assertEqual(g.gw2_get("account"), {"ok": True})
        self.assertEqual(estado["n"], 3)

    def test_missing_key_gives_clear_error(self):
        with mock.patch.dict(os.environ, {"GW2_API_KEY": ""}):
            with self.assertRaises(g.GW2Error) as ctx:
                g.gw2_get("account")
        self.assertIn("GW2_API_KEY", str(ctx.exception))

    def test_resolve_names_uses_cache(self):
        self.route("items", by_ids({1: {"id": 1, "name": "Ouro"}}))
        a = g.resolve_names("items", [1])
        b = g.resolve_names("items", [1])
        self.assertEqual(a[1]["name"], "Ouro")
        self.assertEqual(b[1]["name"], "Ouro")
        self.assertEqual(self.router.count(f"{API}/items"), 1)

    def test_resolve_names_survives_failed_batch(self):
        self.route("items", FakeResp(500))
        self.assertEqual(g.resolve_names("items", [1, 2]), {})

    def test_resolve_names_splits_batches_of_200(self):
        self.route("items", by_ids({}))
        g.resolve_names("items", list(range(1, 451)))
        self.assertEqual(self.router.count(f"{API}/items"), 3)

    def test_format_coins(self):
        self.assertEqual(g.format_coins(1046982), "104 gold, 69 silver, 82 copper")
        self.assertEqual(g.format_coins(0), "0 gold, 0 silver, 0 copper")
        self.assertTrue(g.format_coins(-150).startswith("-"))


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class TestAccount(Base):
    def test_diagnostics_lists_missing_scopes(self):
        self.route("tokeninfo", {
            "name": "my key", "type": "APIKey",
            "permissions": ["account", "wallet"],
        })
        r = g.diagnostics(test_api=False)
        self.assertTrue(r["key_configured"])
        self.assertIn("progression", r["key"]["missing_scopes"])
        self.assertIn("inventories", r["key"]["missing_scopes"])
        self.assertNotIn("wallet", r["key"]["missing_scopes"])
        self.assertNotIn(KEY, str(r))

    def test_diagnostics_without_key(self):
        with mock.patch.dict(os.environ, {"GW2_API_KEY": ""}):
            r = g.diagnostics(test_api=False)
        self.assertFalse(r["key_configured"])
        self.assertNotIn("key", r)

    def test_wallet_with_names_and_formatted_gold(self):
        self.route("account/wallet", [
            {"id": 1, "value": 1046982}, {"id": 4, "value": 0}, {"id": 99, "value": 5},
        ])
        self.route("currencies", by_ids({
            1: {"id": 1, "name": "Coin"}, 4: {"id": 4, "name": "Gem"},
        }))
        r = g.account_wallet()
        by_id = {x["id"]: x for x in r}
        self.assertEqual(by_id[1]["currency"], "Coin")
        self.assertEqual(by_id[1]["formatted_quantity"], "104 gold, 69 silver, 82 copper")
        self.assertEqual(by_id[99]["currency"], "Unknown")

    def test_materials_with_names_order_and_limit(self):
        self.route("account/materials", [
            {"id": 1, "count": 5, "category": 5},
            {"id": 2, "count": 250, "category": 6},
            {"id": 3, "count": 0, "category": 6},
            {"id": 4, "count": 50, "category": 6},
        ])
        self.route("items", by_ids({
            1: {"id": 1, "name": "Zebra"}, 2: {"id": 2, "name": "Ore"}, 4: {"id": 4, "name": "Alpha"},
        }))
        r = g.materials_with_names(sort_by="quantity", limit=2)
        self.assertEqual(r["material_types"], 3)  # count 0 is left out
        self.assertEqual([m["name"] for m in r["materials"]], ["Ore", "Alpha"])
        r2 = g.materials_with_names()
        self.assertEqual([m["name"] for m in r2["materials"]], ["Alpha", "Ore", "Zebra"])
        self.assertIn("error", g.materials_with_names(sort_by="x"))

    def test_material_item_without_name_uses_fallback(self):
        self.route("account/materials", [{"id": 77, "count": 3}])
        self.route("items", by_ids({}))
        r = g.materials_with_names()
        self.assertEqual(r["materials"][0]["name"], "Item 77")

    def test_inventory_grouped_list_and_empty_slots(self):
        # The original bug: /characters returns strings, not objects.
        self.route("characters", ["Alice", "Bob Smith"])
        self.route("characters/Alice/inventory", {"bags": [
            {"id": 1, "size": 3, "inventory": [{"id": 10, "count": 3}, None, {"id": 10, "count": 2}]},
            None,  # slot de bolsa vazio
        ]})
        self.route("characters/Bob%20Smith/inventory", {"bags": [
            {"id": 2, "size": 2, "inventory": [{"id": 11, "count": 1}]},
        ]})
        self.route("items", by_ids({10: {"id": 10, "name": "Pedra"}, 11: {"id": 11, "name": "Sword"}}))
        r = g.account_inventory()["characters"]
        self.assertEqual(r["Alice"]["bags"], 1)
        self.assertEqual(r["Alice"]["items"], [{"id": 10, "name": "Pedra", "quantity": 5}])
        self.assertEqual(r["Bob Smith"]["items"][0]["name"], "Sword")

    def test_inventory_filters_character_and_tolerates_individual_error(self):
        self.route("characters", ["Alice", "Bob"])
        self.route("characters/Alice/inventory", {"bags": []})
        self.route("characters/Bob/inventory", FakeResp(403, {"text": "no"}))
        self.route("items", by_ids({}))
        r = g.account_inventory()
        self.assertIn("Alice", r["characters"])
        self.assertIn("Bob", r["errors"])
        self.assertIn("error", g.account_inventory(character="Zed"))
        self.assertIn("Alice", g.account_inventory(character="alice")["characters"])

    def test_bank_groups_and_ignores_empty_slots(self):
        self.route("account/bank", [
            {"id": 5, "count": 2}, None, {"id": 5, "count": 3}, {"id": 6, "count": 1},
        ])
        self.route("items", by_ids({5: {"id": 5, "name": "A"}, 6: {"id": 6, "name": "B"}}))
        r = g.account_bank()
        self.assertEqual(r["slots_total"], 4)
        self.assertEqual(r["slots_occupied"], 3)
        self.assertEqual(r["items"][0], {"id": 5, "name": "A", "quantity": 5})

    def test_characters(self):
        self.route("characters", ["Alice", "Bob"])
        self.route("characters/Alice/core", {"name": "Alice", "race": "Asura", "profession": "Mesmer",
                                            "level": 80, "age": 7200, "deaths": 3})
        self.route("characters/Bob/core", {"name": "Bob", "level": 10, "age": 3600})
        r = g.character_list()
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["characters"][0]["name"], "Alice")
        self.assertEqual(r["characters"][0]["hours_played"], 2.0)


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------

DETAILS = {
    17: {"id": 17, "name": "Staff Master", "type": "Default", "flags": ["Permanent"],
         "requirement": "Deliver killing blows with a staff.",
         "tiers": [{"count": 1000, "points": 1}, {"count": 3000, "points": 5}, {"count": 5000, "points": 5}]},
    102: {"id": 102, "name": "Maguuma Explorer", "type": "Default", "flags": ["Permanent"],
          "requirement": "Explore all areas in Maguuma.",
          "bits": [{"type": "Text", "text": "Area A"},
                   {"type": "Text", "text": "Metrica Province: Calx's Hideout"},
                   {"type": "Text", "text": ""},
                   {"type": "Text", "text": "Area D"}],
          "tiers": [{"count": 2, "points": 5}, {"count": 4, "points": 10}]},
    5005: {"id": 5005, "name": "Festival Frequenter", "type": "ItemSet", "flags": ["Permanent"],
           "requirement": "Acquire an invitation to each annual festival.",
           "bits": [{"type": "Item", "id": 92152}, {"type": "Item", "id": 92123}],
           "tiers": [{"count": 1, "points": 1}, {"count": 2, "points": 4}],
           "rewards": [{"type": "Item", "id": 92108, "count": 1}]},
    900: {"id": 900, "name": "Feito", "tiers": [{"count": 1, "points": 3}]},
    901: {"id": 901, "name": "Zerado", "tiers": [{"count": 10, "points": 2}]},
    902: {"id": 902, "name": "Nunca Feito", "tiers": [{"count": 5, "points": 2}]},
}

PROGRESS = [
    {"id": 17, "current": 4613, "max": 5000, "done": False},
    {"id": 102, "bits": [0, 3], "current": 2, "max": 4, "done": False},
    {"id": 5005, "bits": [0], "current": 1, "max": 2, "done": False},
    {"id": 900, "current": 1, "max": 1, "done": True},
    {"id": 901, "current": 0, "max": 10, "done": False},
]


class TestAchievements(Base):
    def setUp(self):
        super().setUp()
        self.route("account/achievements", PROGRESS)
        self.route("achievements", by_ids(DETAILS))
        self.route("items", by_ids({
            92152: {"id": 92152, "name": "Invite A"},
            92123: {"id": 92123, "name": "Invite B"},
            92108: {"id": 92108, "name": "Universal Invitation"},
        }))

    def test_partial(self):
        self.assertTrue(g._is_partial(PROGRESS[0]))
        self.assertFalse(g._is_partial(PROGRESS[3]))   # completed
        self.assertFalse(g._is_partial(PROGRESS[4]))   # no progress
        self.assertFalse(g._is_partial({"id": 1, "done": False}))  # no numbers

    def test_points_per_tier(self):
        self.assertEqual(g._points(DETAILS[17], PROGRESS[0]), (6, 5))
        self.assertEqual(g._points(DETAILS[900], PROGRESS[3]), (3, 0))
        self.assertEqual(g._points(DETAILS[901], PROGRESS[4]), (0, 2))

    def test_next_tier(self):
        self.assertEqual(g._next_tier(DETAILS[17], 4613), {"target": 5000, "points": 5, "remaining": 387})
        self.assertIsNone(g._next_tier(DETAILS[900], 1))

    def test_guide_embeds_summary(self):
        r = g.progress_guide()
        self.assertEqual(r["summary"]["records"], 5)
        self.assertEqual(r["summary"]["completed"], 1)
        self.assertEqual(r["summary"]["in_progress"], 3)
        self.assertEqual(r["summary"]["no_progress_recorded"], 1)
        self.assertEqual(r["candidates_with_partial_progress"], r["summary"]["in_progress"])

    def test_progress_in_achievement_details(self):
        r = g.achievement_details([17, 102, 900, 902])
        by_id = {x["id"]: x for x in r}
        self.assertEqual(by_id[17]["progress"]["current"], 4613)
        self.assertFalse(by_id[17]["progress"]["done"])
        self.assertTrue(by_id[900]["progress"]["done"])
        self.assertEqual(by_id[102]["progress"]["bits_unlocked"], 2)
        self.assertEqual(by_id[902]["progress"]["note"], "no record for this account")
        # include_progress=False skips the call and omits the block
        sem = g.achievement_details([17], include_progress=False)[0]
        self.assertNotIn("progress", sem)

    def test_guide_sorts_by_percent(self):
        ids = [r["id"] for r in g.progress_guide()["recommendations"]]
        self.assertEqual(ids, [17, 5005, 102])  # empate 50%: menor "remaining" primeiro

    def test_guide_sorts_by_points_and_points_per_unit(self):
        ids = [r["id"] for r in g.progress_guide(sort_by="points")["recommendations"]]
        self.assertEqual(ids, [102, 17, 5005])
        ids = [r["id"] for r in g.progress_guide(sort_by="points_per_unit")["recommendations"]]
        self.assertEqual(ids, [102, 5005, 17])
        ids = [r["id"] for r in g.progress_guide(sort_by="remaining")["recommendations"]]
        self.assertEqual(ids, [5005, 102, 17])

    def test_guide_named_tiers_and_empty_ignored(self):
        r = {x["id"]: x for x in g.progress_guide()["recommendations"]}
        mag = r[102]
        self.assertEqual(mag["unregistered_bits_total"], 1)
        self.assertEqual(mag["unregistered_bits"][0]["description"], "Metrica Province: Calx's Hideout")
        self.assertEqual(mag["empty_entries_ignored"], 1)
        self.assertFalse(mag["bits_match_remaining"])  # missing 2, 1 named + 1 empty
        fest = r[5005]
        self.assertEqual(fest["unregistered_bits"][0]["description"], "Invite B")  # item resolvido
        self.assertTrue(fest["bits_match_remaining"])
        self.assertEqual(fest["rewards"][0]["name"], "Universal Invitation")
        self.assertNotIn("unregistered_bits", r[17])  # counter, no tiers

    def test_guide_without_tiers_when_asked_and_validation(self):
        r = g.progress_guide(include_bits=False)["recommendations"]
        self.assertTrue(all("unregistered_bits" not in x for x in r))
        self.assertIn("error", g.progress_guide(limit=0))
        self.assertIn("error", g.progress_guide(limit=31))
        self.assertIn("error", g.progress_guide(sort_by="x"))

    def test_guide_points_and_next_tier(self):
        r = {x["id"]: x for x in g.progress_guide()["recommendations"]}
        self.assertEqual(r[17]["points_earned"], 6)
        self.assertEqual(r[17]["points_pending"], 5)
        self.assertEqual(r[17]["next_tier"]["remaining"], 387)

    def test_plan_categories(self):
        acoes = {a["achievement_id"]: a for a in g.action_plan()["actions"]}
        self.assertEqual(acoes[102]["category"], "bits_to_verify")
        self.assertEqual(acoes[17]["category"], "counter_or_goal")
        # Festival is temporary: 'annual'/'festival' words in the text
        self.assertEqual(acoes[5005]["category"], "verify_availability")

    def test_plan_not_temporary_only_by_pvp_flag(self):
        # In the original code the "Pvp" flag was not the problem, but "Permanent"
        # must not trigger the temporary heuristic.
        acoes = {a["achievement_id"]: a for a in g.action_plan()["actions"]}
        self.assertNotEqual(acoes[17]["category"], "verify_availability")

    def test_plan_summary_per_category(self):
        r = g.action_plan()
        self.assertEqual(sum(r["summary_by_category"].values()), r["actions_returned"])

    def test_achievement_points_and_milestone(self):
        self.route("account", {"name": "x.1", "daily_ap": 10, "monthly_ap": 5})
        r = g.achievement_points()
        # 17: 6 | 102: 5 | 5005: 1 | 900: 3 | 901: 0  => 15, + 10 + 5
        self.assertEqual(r["achievement_points"], 15)
        self.assertEqual(r["estimated_total"], 30)
        self.assertEqual(r["next_milestone"], 500)
        self.assertEqual(r["remaining_to_milestone"], 470)

    def test_achievement_details_compact_and_with_tiers(self):
        r = g.achievement_details([102, 5005, 9999])
        by_id = {x["id"]: x for x in r}
        self.assertEqual(by_id[102]["total_bits"], 4)
        self.assertNotIn("bits", by_id[102])
        self.assertEqual(by_id[5005]["rewards"][0]["name"], "Universal Invitation")
        self.assertIn("error", by_id[9999])
        com = g.achievement_details([102], include_bits=True)[0]
        self.assertEqual(len(com["bits"]), 3)  # the empty one is omitted
        self.assertEqual(g.achievement_details([]), [])


# ---------------------------------------------------------------------------
# Area location
# ---------------------------------------------------------------------------

class TestAreaLookup(Base):
    def setUp(self):
        super().setUp()
        self.route("continents/1/floors", [1, 2])
        self.route("continents/1/floors/1", {"regions": {"1": {"name": "Maguuma", "maps": {"10": {
            "name": "Metrica Province", "min_level": 1, "max_level": 15,
            "sectors": [{"name": "Calx's Hideout", "level": 10, "coord": [100.5, 200.5], "chat_link": "[&AAA=]"}],
        }}}}})
        self.route("continents/1/floors/2", {"regions": {}})

    def test_normalize_and_split(self):
        self.assertEqual(g._split_area("Metrica Province: Calx's Hideout"),
                         ("metrica province", "calx's hideout"))
        self.assertEqual(g._split_area("Solo"), (None, "solo"))

    def test_find_area_finds(self):
        r = g.find_area("Metrica Province: Calx's Hideout")
        self.assertTrue(r["found"])
        self.assertEqual(r["location"]["map"], "Metrica Province")
        self.assertEqual(r["location"]["continent_coord"], [100.5, 200.5])

    def test_find_area_sector_only(self):
        self.assertTrue(g.find_area("calx's hideout")["found"])

    def test_find_area_not_found_and_map(self):
        self.assertFalse(g.find_area("Place That Does Not Exist")["found"])
        r = g.find_area("Metrica Province")
        self.assertTrue(r["found"])
        self.assertEqual(r["type"], "map")
        self.assertIn("error", g.find_area("  "))

    def test_floor_is_cached(self):
        g.find_area("Calx's Hideout")
        g.find_area("Calx's Hideout")
        self.assertEqual(self.router.count(f"{API}/continents/1/floors/1"), 1)

    def test_floor_failure_warns_on_not_found(self):
        self.route("continents/1/floors/2", FakeResp(500, {"text": "boom"}))
        r = g.find_area("Place That Does Not Exist")
        self.assertFalse(r["found"])
        self.assertIn("1 map floor(s) unreadable", r["warning"])
        # found despite failure: a positive answer carries no warning
        ok = g.find_area("Calx's Hideout")
        self.assertTrue(ok["found"])
        self.assertNotIn("warning", ok)

    def test_pending_areas_with_location(self):
        self.route("account/achievements", PROGRESS)
        self.route("achievements", by_ids(DETAILS))
        r = g.pending_areas(102)
        self.assertEqual(r["unregistered_bits_total"], 1)
        pend = r["pending"][0]
        self.assertEqual(pend["description"], "Metrica Province: Calx's Hideout")
        self.assertEqual(pend["location"]["sector"], "Calx's Hideout")

    def test_pending_areas_friendly_errors(self):
        self.route("account/achievements", PROGRESS)
        self.route("achievements", by_ids(DETAILS))
        self.assertIn("error", g.pending_areas(424242))
        r = g.pending_areas(17)  # counter, no tiers
        self.assertIn("error", r)


# ---------------------------------------------------------------------------
# Prices and account value
# ---------------------------------------------------------------------------

class TestPrices(Base):
    def test_item_info(self):
        self.route("items/19976", {"id": 19976, "name": "Mystic Coin",
                                  "type": "Material", "rarity": "Rare", "level": 0,
                                  "vendor_value": 12000, "flags": ["AccountBound"],
                                  "details": {"type": "T6", "bag_type": ""}})
        self.route("commerce/prices/19976", {
            "id": 19976, "buys": {"unit_price": 19265, "quantity": 10},
            "sells": {"unit_price": 20100, "quantity": 5}})
        r = g.item_info(item_id=19976)
        self.assertEqual(r["name"], "Mystic Coin")
        self.assertEqual(r["tp_buy_now_copper"], 20100)   # ask: o que se paga
        self.assertEqual(r["tp_sell_now_copper"], 19265)  # bid: o que se recebe
        self.assertEqual(r["tp_buy_now_quantity"], 5)
        self.assertEqual(r["tp_sell_now_quantity"], 10)
        self.assertEqual(r["vendor_value_copper"], 12000)
        self.assertIn("AccountBound", r["flags"])
        self.assertIn("wiki", r)

    def test_item_info_not_tradable(self):
        self.route("items/5", {"id": 5, "name": "Soulbound", "type": "Consumable"})
        self.route("commerce/prices/5", FakeResp(404, {"text": "no such id"}))
        r = g.item_info(item_id=5)
        self.assertEqual(r["name"], "Soulbound")
        self.assertTrue(r["no_tp_price"])

    def test_item_info_no_argument(self):
        self.assertIn("error", g.item_info())

    def test_item_prices_batch(self):
        self.route("items", by_ids({1: {"id": 1, "name": "A"}, 2: {"id": 2, "name": "B"}}))
        self.route("commerce/prices", by_ids({
            1: {"id": 1, "buys": {"unit_price": 100, "quantity": 1}, "sells": {"unit_price": 200, "quantity": 1}}}))
        r = {x["id"]: x for x in g.item_prices([1, 2, 1])}
        self.assertEqual(r[1]["sell_price_copper"], 200)
        self.assertTrue(r[2]["no_tp_price"])
        self.assertEqual(self.router.count(f"{API}/commerce/prices"), 1)

    def test_account_value_with_fees(self):
        self.route("account/materials", [{"id": 1, "count": 10}, {"id": 2, "count": 5}])
        self.route("account/bank", [{"id": 1, "count": 5}, None, {"id": 3, "count": 1}])
        self.route("account/wallet", [{"id": 1, "value": 123456}])
        self.route("commerce/prices", by_ids({
            1: {"id": 1, "buys": {"unit_price": 800, "quantity": 1}, "sells": {"unit_price": 1000, "quantity": 1}},
            2: {"id": 2, "buys": {"unit_price": 100, "quantity": 1}, "sells": {"unit_price": 200, "quantity": 1}},
        }))
        self.route("items", by_ids({1: {"id": 1, "name": "Ouro Bruto"}, 2: {"id": 2, "name": "Cobre"}}))
        r = g.account_value()
        self.assertEqual(r["items_with_price"], 2)
        self.assertEqual(r["items_without_tp_price"], 1)  # id 3 has no price
        topo = r["top_values"][0]
        self.assertEqual(topo["name"], "Ouro Bruto")
        self.assertEqual(topo["quantity"], 15)  # 10 materiais + 5 banco
        self.assertEqual(topo["listed_net_value_copper"], int(1000 * 15 * 0.85))
        self.assertEqual(topo["immediate_net_value_copper"], int(800 * 15 * 0.90))
        self.assertEqual(r["wallet_coins"], "12 gold, 34 silver, 56 copper")

    def test_account_value_without_bank_permission_continues_with_warning(self):
        self.route("account/materials", [{"id": 1, "count": 1}])
        self.route("account/bank", FakeResp(403, {"text": "no"}))
        self.route("account/wallet", [])
        self.route("commerce/prices", by_ids({
            1: {"id": 1, "buys": {"unit_price": 10, "quantity": 1}, "sells": {"unit_price": 20, "quantity": 1}}}))
        self.route("items", by_ids({1: {"id": 1, "name": "X"}}))
        r = g.account_value()
        self.assertTrue(any("Bank not included" in a for a in r["warnings"]))
        self.assertIn("error", g.account_value(limit=0))


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------

class TestWiki(Base):
    def test_search_wiki_strips_html(self):
        self.router.add(f"{WIKI}/api.php", {"query": {"search": [
            {"title": "Mystic Clover", "snippet": 'a <span class="searchmatch">Mystic</span> &amp; b'}]}})
        r = g.search_wiki("Mystic Clover")
        self.assertEqual(r["results"][0]["snippet"], "a Mystic & b")
        self.assertTrue(r["results"][0]["url"].endswith("/wiki/Mystic_Clover"))

    def test_search_wiki_403_becomes_friendly_error(self):
        self.router.add(f"{WIKI}/api.php", FakeResp(403))
        r = g.search_wiki("x")
        self.assertIn("403", r["error"])

    def test_wiki_details_falls_back_to_wikitext_on_403(self):
        self.router.add(f"{WIKI}/api.php", FakeResp(403))
        self.router.add(f"{WIKI}/wiki/Calx's_Hideout", FakeResp(
            200, text="'''Calx's Hideout''' is in [[Metrica Province|Metrica]].<!-- note -->"))
        r = g.wiki_details("Calx's Hideout")
        self.assertEqual(r["source"], "raw_wikitext")
        self.assertIn("is in Metrica.", r["text"])
        self.assertNotIn("nota", r["text"])
        self.assertIn("warning", r)

    def test_wiki_details_follows_redirect(self):
        self.router.add(f"{WIKI}/api.php", FakeResp(403))
        self.router.add(f"{WIKI}/wiki/Velho", FakeResp(200, text="#REDIRECT [[New]]"))
        self.router.add(f"{WIKI}/wiki/New", FakeResp(200, text="new content"))
        r = g.wiki_details("Velho")
        self.assertIn("new content", r["text"])

    def test_wiki_details_via_api_when_working(self):
        self.router.add(f"{WIKI}/api.php", {"query": {"pages": [
            {"title": "Pagina", "extract": "text", "fullurl": "https://wiki.guildwars2.com/wiki/Pagina"}]}})
        r = g.wiki_details("Pagina")
        self.assertEqual(r["source"], "wiki_api")
        self.assertEqual(r["text"], "text")

    def test_wiki_details_missing_page_and_validation(self):
        self.router.add(f"{WIKI}/api.php", {"query": {"pages": [{"title": "X", "missing": True}]}})
        self.assertIn("error", g.wiki_details("X"))
        self.assertIn("error", g.wiki_details("   "))
        self.assertIn("error", g.wiki_details("a" * 201))

    def test_wiki_caches_responses(self):
        self.router.add(f"{WIKI}/api.php", {"query": {"search": []}})
        g.search_wiki("abc")
        g.search_wiki("abc")
        self.assertEqual(self.router.count(f"{WIKI}/api.php"), 1)

    def test_wiki_gets_browser_user_agent(self):
        self.router.add(f"{WIKI}/api.php", {"query": {"search": []}})
        g.search_wiki("ua-teste")
        headers = self.router.calls[0][2]
        self.assertTrue(headers.get("User-Agent", "").startswith("Mozilla/5.0"))
        self.assertIn("GW2PersonalAssistant", headers["User-Agent"])


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------

TIMER_DATA = {"config": {"version": "v5.4"}, "events": {
    "a": {"name": "Evento A", "category": "Cat X",
          "segments": {"0": {"name": ""}, "1": {"name": "Fase 1", "link": "Link Um"}},
          "sequences": {"partial": [{"r": 0, "d": 10}],
                        "pattern": [{"r": 1, "d": 20}, {"r": 0, "d": 40}]}},
    "b": {"name": "Evento B", "category": "Cat Y",
          "segments": {"1": {"name": "Fase B"}},
          "sequences": {"partial": [], "pattern": [{"r": 1, "d": 30}]}},
    "t": {"name": "", "category": "", "segments": {"1": {"name": ""}},
          "sequences": {"partial": [], "pattern": [{"r": 1, "d": 15}]}},
    "ruim": {"name": "Nothing Here", "sequences": {"partial": [], "pattern": []}},
    "junk": "not a dict",
}}


class TestTimers(Base):
    NOW = datetime(2026, 10, 12, 0, 15, tzinfo=timezone.utc)

    def test_generate_phases_partial_before_default(self):
        fases = g._generate_phases(TIMER_DATA["events"]["a"]["sequences"], 100)
        self.assertEqual(fases[0], {"start": 0, "end": 10, "segment": "0"})
        self.assertEqual(fases[1], {"start": 10, "end": 30, "segment": "1"})
        self.assertEqual(fases[3], {"start": 70, "end": 90, "segment": "1"})

    def test_generate_phases_empty_default_does_not_hang(self):
        self.assertEqual(g._generate_phases({"partial": [], "pattern": []}, 100), [])
        self.assertEqual(g._generate_phases(None, 100), [])
        self.assertEqual(g._generate_phases({"pattern": [{"r": 1, "d": 0}]}, 100), [])

    def test_calculate_active_and_upcoming(self):
        tz = timezone(timedelta(hours=-3))
        ativos, proximos, ignorados = g._calculate_timers(TIMER_DATA, self.NOW, 60, tz)
        nomes_ativos = {(a["event"], a["phase"]) for a in ativos}
        self.assertIn(("Evento A", "Fase 1"), nomes_ativos)       # 00:10–00:30
        self.assertIn(("Evento B", "Fase B"), nomes_ativos)       # 00:00–00:30
        self.assertFalse(any(a["event"] == "" for a in ativos))  # phase without name is left out
        a_ativo = next(a for a in ativos if a["event"] == "Evento A")
        self.assertEqual(a_ativo["end_local"], "2026-10-11T21:30:00-03:00")
        self.assertTrue(a_ativo["wiki_link"].endswith("/wiki/Link_Um"))
        # nearest to A: 01:10 (55 min); to B: 00:30 (15 min)
        prox = {(p["event"], p["start_utc"][11:16]): p for p in proximos}
        self.assertEqual(prox[("Evento A", "01:10")]["minutes_until_start"], 55)
        self.assertEqual(prox[("Evento B", "00:30")]["minutes_until_start"], 15)
        self.assertEqual([p["start_utc"] for p in proximos], sorted(p["start_utc"] for p in proximos))
        self.assertEqual(ignorados, 2)  # 'ruim' e 'junk'

    def test_short_horizon_excludes_distant_upcoming(self):
        tz = timezone.utc
        _, proximos, _ = g._calculate_timers(TIMER_DATA, self.NOW, 20, tz)
        self.assertFalse(any(p["event"] == "Evento A" for p in proximos))

    def test_filter(self):
        tz = timezone.utc
        ativos, proximos, _ = g._calculate_timers(TIMER_DATA, self.NOW, 60, tz, filter_="cat y")
        self.assertTrue(ativos and all(a["event"] == "Evento B" for a in ativos))
        self.assertTrue(all(p["event"] == "Evento B" for p in proximos))

    def test_exact_start_now_is_not_upcoming_and_already_active(self):
        agora = datetime(2026, 10, 12, 0, 30, tzinfo=timezone.utc)
        ativos, proximos, _ = g._calculate_timers(TIMER_DATA, agora, 10, timezone.utc)
        self.assertTrue(any(a["event"] == "Evento B" for a in ativos))
        self.assertFalse(any(p["event"] == "Evento B" and p["minutes_until_start"] == 0 for p in proximos))

    def test_full_tool_with_timezone_and_validation(self):
        self.router.add(f"{WIKI}/wiki/Widget:Event_timer/data.json", TIMER_DATA)
        with mock.patch.object(g, "_utc_now", return_value=self.NOW):
            r = g.event_timers(60)
        self.assertEqual(r["display_timezone"], "America/Sao_Paulo")
        self.assertIn("-03:00", r["upcoming_events"][0]["start_local"])
        self.assertIn("error", g.event_timers(-1))
        self.assertIn("error", g.event_timers(2000))

    def test_alternative_timezone_when_tzdata_missing(self):
        with mock.patch("zoneinfo.ZoneInfo", side_effect=Exception("no tzdata")):
            tz, aviso = g._resolve_timezone("America/Sao_Paulo")
            self.assertIn("tzdata", aviso)
            self.assertEqual(self.NOW.astimezone(tz).hour, 21)
            with self.assertRaises(g.GW2Error):
                g._resolve_timezone("Europa/Inexistente")

    def test_tool_with_invalid_timezone_returns_error(self):
        self.router.add(f"{WIKI}/wiki/Widget:Event_timer/data.json", TIMER_DATA)
        r = g.event_timers(30, timezone_name="Planeta/Marte")
        self.assertIn("error", r)

    def test_timers_wiki_failure_becomes_error(self):
        self.router.add(f"{WIKI}/wiki/Widget:Event_timer/data.json", FakeResp(403))
        self.assertIn("403", g.event_timers(30)["error"])


# ---------------------------------------------------------------------------
# v2.1: texts, unlocks, chests, equipment, TP, craft...
# ---------------------------------------------------------------------------

class TestTexts(unittest.TestCase):
    def test_requirement_fills_number_api_omits(self):
        detail = {"requirement": "Obtain  rank points.", "tiers": [{"count": 1000, "points": 5}, {"count": 8500, "points": 40}]}
        self.assertEqual(g._readable_requirement(detail), "Obtain 8.500 rank points.")

    def test_requirement_normal_unchanged(self):
        detail = {"requirement": "Complete 8 dungeon stories.", "tiers": [{"count": 8, "points": 5}]}
        self.assertEqual(g._readable_requirement(detail), "Complete 8 dungeon stories.")

    def test_limit_helper(self):
        self.assertEqual(g._limit([1, 2, 3], 2), ([1, 2], 1))
        self.assertEqual(g._limit([1, 2, 3], 0), ([1, 2, 3], 0))


class TestUnlocks(Base):
    def test_summary_totals_and_permission_error(self):
        self.route("account/skins", [1, 2, 3])
        self.route("account/minis", [10])
        self.route("skins", [1, 2, 3, 4, 5, 6])
        self.route("minis", [10, 11])
        r = g.unlocks()
        self.assertEqual(r["categories"]["skins"], {"unlocked": 3, "total_in_game": 6, "percent": 50.0})
        self.assertEqual(r["categories"]["minis"]["percent"], 50.0)
        self.assertIn("dyes", r["errors"])  # route not mocked -> handled 404

    def test_403_suggests_unlocks(self):
        self.route("account/skins", FakeResp(403, {"text": "requires scope unlocks"}))
        with self.assertRaises(g.GW2Error) as ctx:
            g.gw2_get("account/skins")
        self.assertIn("unlocks", str(ctx.exception))

    def test_missing_crosses_with_unlocks(self):
        self.route("achievements", by_ids({7: {
            "id": 7, "name": "Mini Collection", "requirement": "Collect  miniatures.",
            "tiers": [{"count": 4, "points": 10}],
            "bits": [
                {"type": "Minipet", "id": 100},
                {"type": "Minipet", "id": 101},
                {"type": "Minipet", "id": 102},
                {"type": "Text", "text": ""},
            ],
        }}))
        self.route("account/achievements", [{"id": 7, "current": 1, "max": 4, "done": False, "bits": [0]}])
        self.route("account/minis", [100, 101])  # 101 unlocked but not recorded
        self.route("minis", by_ids({101: {"id": 101, "name": "Mini B"}, 102: {"id": 102, "name": "Mini C"}}))
        r = g.missing_achievements(7)
        self.assertEqual([e["description"] for e in r["actually_missing"]], ["Mini C"])
        self.assertEqual([e["description"] for e in r["unlocked_but_unregistered"]], ["Mini B"])
        self.assertEqual(r["empty_entries_ignored"], 1)
        self.assertIn("4", r["requirement"])

    def test_missing_without_unlocks_permission_becomes_unverifiable(self):
        self.route("achievements", by_ids({7: {"id": 7, "name": "X", "tiers": [{"count": 2, "points": 1}],
                                              "bits": [{"type": "Skin", "id": 5}, {"type": "Skin", "id": 6}]}}))
        self.route("account/achievements", [{"id": 7, "current": 0, "max": 2, "done": False, "bits": []}])
        self.route("account/skins", FakeResp(403, {"text": "scope"}))
        self.route("skins", by_ids({5: {"id": 5, "name": "S5"}, 6: {"id": 6, "name": "S6"}}))
        r = g.missing_achievements(7)
        self.assertEqual(r["unchecked_total"], 2)
        self.assertIn("Skin", r["crossing_errors"])
        self.assertNotIn(KEY, str(r))

    def test_missing_completed_and_nonexistent(self):
        self.route("achievements", by_ids({7: {"id": 7, "name": "X", "tiers": []}}))
        self.route("account/achievements", [{"id": 7, "done": True}])
        self.assertTrue(g.missing_achievements(7)["completed"])
        self.assertIn("error", g.missing_achievements(999))


DAILY_VAULT = {
    "meta_progress_current": 3, "meta_progress_complete": 7,
    "meta_reward_astral": 450, "meta_reward_claimed": False,
    "objectives": [
        {"id": 1, "title": "Kill 10 foes", "track": "PvE", "acclaim": 5,
         "progress_current": 10, "progress_complete": 10, "claimed": True},
        {"id": 2, "title": "Win a match", "track": "PvP", "acclaim": 5,
         "progress_current": 1, "progress_complete": 1, "claimed": False},
        {"id": 3, "title": "Open chests", "track": "PvE", "acclaim": 10,
         "progress_current": 2, "progress_complete": 5, "claimed": False},
    ],
}


class TestVault(Base):
    def test_vault_pending_and_claimable(self):
        self.route("account/wizardsvault/daily", DAILY_VAULT)
        r = g.wizards_vault("daily")["daily"]
        self.assertEqual(r["meta"]["progress"], "3 / 7")
        self.assertEqual(r["objectives_completed"], 2)
        self.assertEqual(r["pending_acclaim"], 10)
        self.assertEqual([o["title"] for o in r["pending"]], ["Open chests"])
        self.assertEqual([o["title"] for o in r["claimable"]], ["Win a match"])

    def test_vault_invalid_section_and_error_per_section(self):
        self.assertIn("error", g.wizards_vault("mensal"))
        self.route("account/wizardsvault/daily", DAILY_VAULT)
        r = g.wizards_vault()
        self.assertIn("daily", r)
        self.assertIn("weekly", r["errors"])
        self.assertIn("special", r["errors"])

    def test_what_to_do_today_joins_everything_and_tolerates_failures(self):
        self.route("account/wizardsvault/daily", DAILY_VAULT)
        self.route("account/mastery/points", {"totals": [{"region": "Maguuma", "spent": 5, "earned": 9}]})
        self.route("account/achievements", [{"id": 1, "current": 8, "max": 10, "done": False}])
        self.route("achievements", by_ids({1: {"id": 1, "name": "Quase la", "tiers": [{"count": 10, "points": 10}]}}))
        falso = {"active_phases": [{"event": "Cantha: Day and night"}, {"event": "A"}], "upcoming_events": [{"event": "B"}], "warning": "x"}
        with mock.patch.object(g, "event_timers", return_value=falso):
            r = g.what_to_do_today()
        self.assertEqual(r["spare_mastery_points"], {"Maguuma": 4})
        self.assertEqual(r["suggested_achievements"][0]["name"], "Quase la")
        self.assertEqual(r["events"]["upcoming"], [{"event": "B"}])
        self.assertTrue(any("Open chests" in x for x in r["wizards_vault"]["daily"]["pending"]))
        self.assertEqual(r["events"]["active"], [{"event": "A"}])  # without "Day and night"
        self.assertIn("weekly", "".join(r["errors"]))  # weekly not mocked
        self.assertIn("[PvE] Open chests (2 / 5, 10 acclaim)", r["wizards_vault"]["daily"]["pending"])
        self.assertIn("errors", r["done_today"])  # unmocked daily routes show up as errors
        self.assertNotIn(KEY, str(r))


class TestEquipment(Base):
    def test_reviews_rarity_runes_and_stats(self):
        self.route("characters", ["Alice"])
        self.route("characters/Alice/equipment", {"equipment": [
            {"id": 1, "slot": "HelmAquatic", "upgrades": [50], "infusions": [60], "stats": {"id": 9}, "binding": "Account"},
            {"id": 2, "slot": "Coat", "upgrades": []},
            {"id": 3, "slot": "Amulet"},
            {"id": 4, "slot": "Sickle"},
        ]})
        self.route("items", by_ids({
            1: {"id": 1, "name": "Capacete", "rarity": "Ascended", "level": 80, "type": "Armor"},
            2: {"id": 2, "name": "Casaco", "rarity": "Exotic", "level": 80, "type": "Armor"},
            3: {"id": 3, "name": "Amulet", "rarity": "Exotic", "level": 80, "type": "Trinket"},
            4: {"id": 4, "name": "Foice", "rarity": "Masterwork", "level": 0, "type": "Gathering"},
            50: {"id": 50, "name": "Runa"}, 60: {"id": 60, "name": "Infusao"},
        }))
        self.route("itemstats", by_ids({9: {"id": 9, "name": "Berserker's"}}))
        r = g.equipment("alice", detailed=True)["characters"]["Alice"]
        capacete = r["pieces"][0]
        self.assertEqual(capacete["stats"], "Berserker's")
        self.assertEqual(capacete["upgrades"], ["Runa"])
        self.assertEqual(capacete["infusions"], ["Infusao"])
        self.assertEqual(r["below_ascended"], ["Coat: Casaco (Exotic)", "Amulet: Amulet (Exotic)"])
        self.assertEqual(r["missing_rune_or_sigil"], ["Coat: Casaco"])  # amuleto e foice ficam de fora
        self.assertEqual(r["count_by_rarity"]["Exotic"], 2)

    def test_missing_character_and_individual_error(self):
        self.route("characters", ["Alice"])
        self.assertIn("error", g.equipment("Zed"))
        self.route("characters/Alice/equipment", FakeResp(403, {"text": "scope"}))
        r = g.equipment()
        self.assertIn("Alice", r["errors"])


class TestTradingPost(Base):
    def test_orders_history_and_fulfillment(self):
        self.route("commerce/transactions/current/buys", [
            {"id": 1, "item_id": 10, "price": 100, "quantity": 5, "created": "2026-10-01"}])
        self.route("commerce/transactions/current/sells", [
            {"id": 2, "item_id": 10, "price": 1000, "quantity": 2, "created": "2026-10-02"}])
        self.route("commerce/transactions/history/buys", [])
        self.route("commerce/transactions/history/sells", [
            {"id": 3, "item_id": 10, "price": 2000, "quantity": 1, "created": "a", "purchased": "b"}])
        self.route("commerce/delivery", {"coins": 12345, "items": [{"id": 10, "count": 3}]})
        self.route("items", by_ids({10: {"id": 10, "name": "Pedra"}}))
        self.route("commerce/prices", by_ids({10: {"id": 10, "buys": {"unit_price": 120}, "sells": {"unit_price": 900}}}))
        r = g.tp_transactions()
        compra = r["active_buys"]["orders"][0]
        self.assertTrue(compra["outbid"])  # 100 < 120
        venda = r["active_sells"]["orders"][0]
        self.assertTrue(venda["outbid"])  # 1000 > 900
        self.assertEqual(venda["estimated_net"], g.format_coins(int(2000 * 0.85)))
        self.assertEqual(r["history_sells"]["estimated_net"], g.format_coins(1700))
        self.assertEqual(r["delivery_box"]["coins"], g.format_coins(12345))
        self.assertEqual(r["delivery_box"]["items"], [{"item": "Pedra", "quantity": 3}])

    def test_without_tradingpost_permission(self):
        self.route("commerce/transactions/current/buys", FakeResp(403, {"text": "scope"}))
        r = g.tp_transactions()
        self.assertIn("tradingpost", r["errors"]["active_buys"])


class TestPvpWvw(Base):
    def test_pvp_and_wvw(self):
        self.route("pvp/stats", {"pvp_rank": 12, "pvp_rank_points": 500, "aggregate": {"wins": 3, "losses": 2}})
        self.route("account", {"wvw_rank": 40, "world": 1001})
        r = g.pvp_wvw()
        self.assertEqual(r["pvp"]["rank"], 12)
        self.assertEqual(r["pvp"]["wins"], 3)  # server returns 'wins'
        self.assertEqual(r["wvw"]["rank"], 40)

    def test_pvp_without_permission_keeps_wvw(self):
        self.route("pvp/stats", FakeResp(403, {"text": "scope"}))
        self.route("account", {"wvw_rank": 40})
        r = g.pvp_wvw()
        self.assertIn("pvp", r["errors"])
        self.assertEqual(r["wvw"]["rank"], 40)


class TestCraft(Base):
    def build(self):
        # 100 = Sword: 2x Bar (200) + 1x Hilt (300). Bar: 2x Ore (400) via recipe.
        self.route("recipes/search", lambda p, h: {
            "100": [1], "200": [2]}.get(str(p["output"]), []))
        self.route("recipes", by_ids({
            1: {"id": 1, "output_item_id": 100, "output_item_count": 1, "ingredients": [
                {"type": "Item", "id": 200, "count": 2}, {"type": "Item", "id": 300, "count": 1},
                {"type": "Currency", "id": 2, "count": 100}]},
            2: {"id": 2, "output_item_id": 200, "output_item_count": 1, "ingredients": [
                {"type": "Item", "id": 400, "count": 2}]},
        }))
        self.route("commerce/prices", by_ids({
            200: {"id": 200, "sells": {"unit_price": 1000}},   # comprar a barra sai mais caro...
            300: {"id": 300, "sells": {"unit_price": 50}},
            400: {"id": 400, "sells": {"unit_price": 100}},    # ...que craftar (2 x 100 = 200)
        }))
        self.route("items", by_ids({i: {"id": i, "name": n} for i, n in
                                    {100: "Sword", 200: "Bar", 300: "Hilt", 400: "Ore"}.items()}))
        self.route("account/materials", [{"id": 400, "count": 1, "category": 1}])
        self.route("account/bank", [None, {"id": 300, "count": 1}])

    def test_prefers_crafting_when_cheaper_and_uses_stock(self):
        self.build()
        r = g._crafting_cost(100)
        # 2x Bar = 4x Ore; 1 in stock -> buys 3 (300). Hilt comes from the bank.
        self.assertEqual(r["estimated_total_cost"], g.format_coins(300))
        purchases = {c["item"]: c for c in r["purchase_list"]}
        self.assertEqual(purchases["Ore"]["quantity"], 3)
        self.assertNotIn("Hilt", purchases)
        self.assertEqual(r["sub_items_to_craft"], [{"item": "Bar", "recipes": 2}])
        self.assertIn({"item": "Hilt", "quantity": 1}, r["from_stock"])
        self.assertEqual(r["non_item_ingredients_skipped"], 1)

    def test_depth_zero_buys_everything(self):
        self.build()
        r = g._crafting_cost(100, use_stock=False, depth=0)
        self.assertEqual(r["estimated_total_cost"], g.format_coins(2 * 1000 + 50))
        self.assertEqual(r["sub_items_to_craft"], [])

    def test_no_recipe_and_validation(self):
        self.build()
        self.assertIn("error", g._crafting_cost(999))
        self.assertIn("error", g._crafting_cost(100, quantity=0))
        self.assertIn("error", g._crafting_cost(100, depth=9))

    def test_stock_without_permission_warns_and_continues(self):
        self.build()
        self.route("account/materials", FakeResp(403, {"text": "scope"}))
        r = g._crafting_cost(100)
        self.assertIn("stock_warnings", r)
        self.assertEqual(r["estimated_total_cost"], g.format_coins(400))  # 4x Ore, Hilt from the bank


class TestHistory(Base):
    def setUp(self):
        super().setUp()
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.route("account/achievements", [{"id": 1, "done": True}, {"id": 2, "done": False}])
        self.route("achievements", by_ids({1: {"id": 1, "tiers": [{"count": 1, "points": 10}]},
                                           2: {"id": 2, "tiers": [{"count": 5, "points": 5}]}}))
        self.route("account/wallet", [{"id": 1, "value": 50000}])
        self.route("account/mastery/points", {"totals": [{"region": "X", "spent": 1, "earned": 3}]})
        self.route("characters", lambda p, h: [{"name": "A", "age": 7200}, {"name": "B", "age": 3600}])

    def test_save_and_compare(self):
        r = g.save_snapshot()
        self.assertEqual(r["snapshot"]["gold"], 5)
        self.assertEqual(r["snapshot"]["hours_played"], 3.0)
        self.assertEqual(r["snapshot"]["achievements_completed"], 1)
        self.assertTrue(os.path.exists(r["saved_to"]))
        # changes state and compares with the saved snapshot
        self.route("account/wallet", [{"id": 1, "value": 80000}])
        g._cache.clear()
        c = g.compare_progress(7)
        self.assertEqual(c["differences"]["gold"], {"before": 5, "now": 8, "change": 3})
        self.assertIn("warning", c)  # only a recent snapshot exists

    def test_compare_without_history_and_validation(self):
        self.assertIn("error", g.compare_progress(7))
        self.assertIn("error", g.compare_progress(0))

    def test_corrupted_line_is_ignored(self):
        g.save_snapshot()
        with open(g._history_file(), "a", encoding="utf-8") as f:
            f.write("{quebrado\n")
        self.assertEqual(len(g._read_history()), 1)

    def test_write_error_does_not_break(self):
        with mock.patch.dict(os.environ, {"GW2_DATA_DIR": os.path.join(self.pasta, "does", "not_exist")}):
            r = g.save_snapshot()
        self.assertIn("error", r)


class TestScopes(Base):
    def test_diagnostics_lists_new_missing_scopes(self):
        self.route("tokeninfo", {"name": "k", "permissions": ["account"]})
        r = g.diagnostics(test_api=False)
        missing = r["key"]["missing_scopes"]
        for scope in ("progression", "inventories", "unlocks", "tradingpost", "pvp"):
            self.assertIn(scope, missing)


# ---------------------------------------------------------------------------
# v2.2: availability, compact, legendaries, diagnostics, robustness
# ---------------------------------------------------------------------------

class TestAvailability(Base):
    def build(self, categorias):
        self.route("account/achievements", [
            {"id": 1, "current": 5, "max": 10, "done": False},   # em categoria
            {"id": 2, "current": 9, "max": 10, "done": False},   # fora (retirado)
        ])
        self.route("achievements", by_ids({
            1: {"id": 1, "name": "Atual", "tiers": [{"count": 10, "points": 10}]},
            2: {"id": 2, "name": "Retirado", "tiers": [{"count": 10, "points": 50}]},
        }))
        if categorias is not None:
            self.route("achievements/categories", categorias)

    def test_hides_outside_categories_and_reports(self):
        self.build([{"id": 1, "achievements": [1, {"id": 3, "flags": []}]}])
        r = g.action_plan()
        self.assertEqual([a["achievement_id"] for a in r["actions"]], [1])
        self.assertEqual(r["hidden_unavailable"], {"total": 1, "examples": ["Retirado"]})

    def test_can_include_unavailable(self):
        self.build([{"id": 1, "achievements": [1]}])
        r = g.action_plan(hide_unavailable=False)
        self.assertEqual(len(r["actions"]), 2)
        self.assertNotIn("hidden_unavailable", r)

    def test_guide_also_hides(self):
        self.build([{"id": 1, "achievements": [1]}])
        ids = [x["id"] for x in g.progress_guide()["recommendations"]]
        self.assertEqual(ids, [1])

    def test_categories_failure_hides_nothing_and_warns(self):
        self.build(None)  # rota inexistente -> 404
        r = g.action_plan()
        self.assertEqual(len(r["actions"]), 2)
        self.assertIn("availability_warning", r)

    def test_empty_categories_hide_nothing(self):
        self.build([])
        r = g.action_plan()
        self.assertEqual(len(r["actions"]), 2)
        self.assertIn("availability_warning", r)


class TestCompact(Base):
    def build(self):
        self.route("account/achievements", [{"id": 1, "current": 1, "max": 9, "done": False, "bits": [0]}])
        self.route("achievements", by_ids({1: {
            "id": 1, "name": "Tiers", "description": "long text", "requirement": "Do it.",
            "tiers": [{"count": 9, "points": 10}],
            "bits": [{"type": "Text", "text": f"E{i}"} for i in range(9)],
        }}))

    def test_compact_uses_names_only_and_limits(self):
        self.build()
        a = g.action_plan(max_bits=3)["actions"][0]
        self.assertEqual(a["bits_to_verify"], ["E1", "E2", "E3"])
        self.assertEqual(a["bits_omitted"], 5)
        self.assertNotIn("description", a)

    def test_full_keeps_old_format(self):
        self.build()
        a = g.action_plan(compact=False, max_bits=2)["actions"][0]
        self.assertEqual(a["bits_to_verify"][0], {"index": 1, "description": "E1"})
        self.assertEqual(a["description"], "long text")


class TestVaultNew(Base):
    def test_objective_without_counter_and_track_filter(self):
        self.route("account/wizardsvault/special", {"objectives": [
            {"id": 1, "title": "Colecionar", "track": "PvE", "acclaim": 150, "progress_current": 0, "progress_complete": 0},
            {"id": 2, "title": "Vencer", "track": "PvP", "acclaim": 10, "progress_current": 0, "progress_complete": 3},
        ]})
        r = g.wizards_vault("special")["special"]
        self.assertIsNone(r["pending"][0]["progress"])
        self.assertEqual(r["pending_acclaim_by_track"], {"PvE": 150, "PvP": 10})
        so_pvp = g.wizards_vault("special", track="pvp")["special"]
        self.assertEqual([o["title"] for o in so_pvp["pending"]], ["Vencer"])


class TestEquipmentNew(Base):
    def test_item_fixed_stats_and_optionals(self):
        self.route("characters", ["Alice"])
        self.route("characters/Alice/equipment", {"equipment": [
            {"id": 1, "slot": "Coat", "upgrades": [50]},
            {"id": 2, "slot": "HelmAquatic", "upgrades": []},
            {"id": 3, "slot": "Relic"},
        ]})
        self.route("items", by_ids({
            1: {"id": 1, "name": "Casaco", "rarity": "Ascended", "type": "Armor",
                "details": {"infix_upgrade": {"id": 9}}},
            2: {"id": 2, "name": "Breather", "rarity": "Rare", "type": "Armor"},
            3: {"id": 3, "name": "Relic", "rarity": "Exotic", "type": "Relic"},
            50: {"id": 50, "name": "Runa"},
        }))
        self.route("itemstats", by_ids({9: {"id": 9, "name": "Viper's"}}))
        r = g.equipment()["characters"]["Alice"]
        self.assertIn("Coat: Casaco [Ascended, Viper's] + Runa", r["pieces"])
        self.assertEqual(r["below_ascended"], [])
        self.assertEqual(r["optional_below_ascended"], ["HelmAquatic: Breather (Rare)", "Relic: Relic (Exotic)"])
        self.assertEqual(r["missing_rune_or_sigil"], [])  # aquatic is optional


class TestPvpWorld(Base):
    def test_resolve_world_name(self):
        self.route("pvp/stats", {"pvp_rank": 1})
        self.route("account", {"wvw_rank": 5, "world": 1015})
        self.route("worlds", by_ids({1015: {"id": 1015, "name": "Mundo Teste"}}))
        self.assertEqual(g.pvp_wvw()["wvw"]["world"], "Mundo Teste")


class TestCraftRobust(Base):
    def test_skips_recipe_without_items_and_uses_next(self):
        self.route("recipes/search", [10, 11])
        self.route("recipes", by_ids({
            10: {"id": 10, "output_item_id": 100, "ingredients": [{"type": "Currency", "id": 2, "count": 5}]},
            11: {"id": 11, "output_item_id": 100, "output_item_count": 1, "ingredients": [{"type": "Item", "id": 200, "count": 2}]},
        }))
        self.route("commerce/prices", by_ids({200: {"id": 200, "sells": {"unit_price": 10}}}))
        self.route("items", by_ids({100: {"id": 100, "name": "A"}, 200: {"id": 200, "name": "B"}}))
        r = g._crafting_cost(100, use_stock=False)
        self.assertEqual(r["recipe_id"], 11)
        self.assertEqual(r["estimated_total_cost"], g.format_coins(20))

    def test_accepts_old_schema_without_type(self):
        self.route("recipes/search", [10])
        self.route("recipes", by_ids({10: {"id": 10, "ingredients": [{"item_id": 200, "count": 1}]}}))
        self.route("commerce/prices", by_ids({200: {"id": 200, "sells": {"unit_price": 7}}}))
        self.route("items", by_ids({100: {"id": 100, "name": "A"}, 200: {"id": 200, "name": "B"}}))
        self.assertEqual(g._crafting_cost(100, use_stock=False)["estimated_total_cost"], g.format_coins(7))

    def test_coins_only_explains_what_it_saw(self):
        self.route("recipes/search", [10])
        self.route("recipes", by_ids({10: {"id": 10, "ingredients": [{"type": "GuildUpgrade", "id": 3, "count": 1}]}}))
        r = g._crafting_cost(100, use_stock=False)
        self.assertIn("error", r)
        self.assertEqual(r["ingredients_seen"][0]["type"], "GuildUpgrade")


class TestItemCraftInfo(Base):
    """item_craft_info: name lookup (on-disk index) + recipe + prices."""

    # Response of /v2/items?ids=all (builds the index) and of the ids route
    ALL_ITEMS = [
        {"id": 100, "name": "Sword Reta", "type": "Weapon", "rarity": "Exotic", "level": 80},
        {"id": 103, "name": "Sword", "type": "Weapon", "rarity": "Rare", "level": 60},
        {"id": 104, "name": "Sword", "type": "Weapon", "rarity": "Fine", "level": 40},
        {"id": 101, "name": "Sword Longa", "type": "Weapon", "rarity": "Exotic", "level": 80},
        {"id": 102, "name": "Sword Curta", "type": "Weapon", "rarity": "Fine", "level": 40},
        {"id": 200, "name": "Ore", "type": "Material"},
        {"id": 500, "name": "Runic Amulet", "type": "Trinket", "vendor_value": 123},
    ]

    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)

    def build(self):
        tabela = {i["id"]: i for i in self.ALL_ITEMS}

        def itens(params, headers):
            # without ids: only the light id list (index building)
            if "ids" not in params:
                return [i["id"] for i in self.ALL_ITEMS]
            return by_ids(tabela)(params, headers)

        self.route("items", itens)
        self.route("recipes/search", lambda p, h: [1] if str(p.get("output")) == "100" else [])
        self.route("recipes", by_ids({
            1: {"id": 1, "output_item_id": 100, "output_item_count": 1,
                "ingredients": [{"type": "Item", "id": 200, "count": 2}]},
        }))
        self.route("commerce/prices", by_ids({
            100: {"id": 100, "buys": {"unit_price": 900}, "sells": {"unit_price": 1000}},
            200: {"id": 200, "sells": {"unit_price": 50}},
        }))

    def test_by_name_with_recipe_and_prices(self):
        self.build()
        r = g.item_craft_info(item_name="Sword Reta")
        self.assertEqual(r["item_id"], 100)
        self.assertEqual(r["rarity"], "Exotic")
        self.assertTrue(r["craftable"])
        self.assertEqual(r["tp_buy_now"], g.format_coins(1000))   # menor oferta de venda
        self.assertEqual(r["tp_sell_now"], g.format_coins(900))   # maior oferta de compra
        self.assertEqual(r["buy_total_now"], g.format_coins(1000))
        self.assertEqual(r["craft"]["estimated_total_cost"], g.format_coins(100))
        self.assertTrue(r["wiki"].startswith(WIKI))
        # index written to disk and reused (only one call to the id list)
        self.assertTrue(os.path.exists(g._item_index_file()))
        g.item_craft_info(item_name="Sword Longa")
        chamadas_lista = sum(
            1 for c in self.router.calls if c[0] == f"{API}/items" and "ids" not in c[1]
        )
        self.assertEqual(chamadas_lista, 1)

    def test_ambiguous_name_lists_candidates(self):
        self.build()
        r = g.item_craft_info(item_name="Sword")
        self.assertIn("error", r)
        self.assertEqual(sorted(c["item_id"] for c in r["candidates"]), [103, 104])

    def test_unique_partial_name_resolves(self):
        self.build()
        r = g.item_craft_info(item_name="Longa")
        self.assertEqual(r["item_id"], 101)

    def test_no_recipe_points_to_wiki(self):
        self.build()
        r = g.item_craft_info(item_name="Runic Amulet")
        self.assertFalse(r["craftable"])
        self.assertEqual(r["vendor_value"], g.format_coins(123))
        self.assertIn("wiki", r)
        self.assertNotIn("craft", r)

    def test_by_id_without_name_and_validation(self):
        self.build()
        self.assertTrue(g.item_craft_info(item_id=100)["craftable"])
        self.assertIn("error", g.item_craft_info())                       # nada informado
        self.assertIn("error", g.item_craft_info(item_name="x", quantity=0))
        self.assertIn("error", g.item_craft_info(item_name="x", depth=9))

    def test_nonexistent_name(self):
        self.build()
        self.assertIn("error", g.item_craft_info(item_name="Zzz Nada Existe"))

    def test_corrupted_index_reloads(self):
        self.build()
        with open(g._item_index_file(), "w", encoding="utf-8") as f:
            f.write("{quebrado")
        self.assertEqual(g.item_craft_info(item_name="Sword Reta")["item_id"], 100)


LEGENDARIES = {
    1: {"id": 1, "name": "Legendary Staff", "type": "Weapon", "details": {"type": "Staff"}},
    2: {"id": 2, "name": "Legendary Sword", "type": "Weapon", "details": {"type": "Sword"}},
    3: {"id": 3, "name": "Legendary Helm", "type": "Armor", "details": {"type": "Helm", "weight_class": "Heavy"}},
}


class TestArmory(Base):
    def build(self):
        self.route("account/legendaryarmory", [{"id": 1, "count": 2}, {"id": 3, "count": 0}])
        self.route("legendaryarmory", [{"id": 1, "max_count": 9}, {"id": 2, "max_count": 9}, {"id": 3, "max_count": 9}])
        self.route("items", by_ids(LEGENDARIES))

    def test_have_and_missing_per_group(self):
        self.build()
        r = g.legendary_armory()
        self.assertEqual(r["have_total"], 1)
        self.assertEqual(r["total_considered"], 3)
        self.assertEqual(r["percent"], 33.3)
        self.assertEqual(r["missing_by_group"], {"Weapon: Sword": 1, "Armor: Helm (Heavy)": 1})
        self.assertEqual(r["have"], [{"item": "Legendary Staff", "group": "Weapon: Staff", "quantity": 2}])

    def test_filter_and_limit(self):
        self.build()
        r = g.legendary_armory(category="sword")
        self.assertEqual([f["item"] for f in r["missing"]], ["Legendary Sword"])
        r = g.legendary_armory(missing_limit=1)
        self.assertEqual(r["missing_omitted"], 1)

    def test_permission_error_becomes_message(self):
        self.route("account/legendaryarmory", FakeResp(403, {"text": "scope"}))
        self.route("legendaryarmory", [{"id": 1}])
        self.assertIn("unlocks", g.legendary_armory()["error"])


class TestDiagnostics(Base):
    def test_does_not_expose_key_and_shows_errors(self):
        self.route("build", {"id": 1})
        with self.assertRaises(g.GW2Error):
            g.gw2_get("route-that-does-not-exist", authenticated=False)
        r = g.diagnostics()
        self.assertTrue(r["key_configured"])
        self.assertTrue(r["public_api"]["ok"])
        self.assertNotIn(KEY, str(r))
        self.assertTrue(any("route-that-does-not-exist" in e["message"] for e in r["recent_errors"]))
        self.assertIn("python", r)

    def test_no_key_and_api_down(self):
        with mock.patch.dict(os.environ, {"GW2_API_KEY": ""}):
            r = g.diagnostics()
        self.assertFalse(r["key_configured"])
        self.assertFalse(r["public_api"]["ok"])  # route not mocked


class TestParallel(unittest.TestCase):
    def test_returns_result_or_message_per_task(self):
        def falha():
            raise g.GW2Error("quebrou")
        r = g._parallel({"a": lambda: 1, "b": falha})
        self.assertEqual(r, {"a": (True, 1), "b": (False, "quebrou")})

    def test_programming_bug_is_not_hidden(self):
        with self.assertRaises(ZeroDivisionError):
            g._parallel({"x": lambda: 1 / 0})


# ---------------------------------------------------------------------------
# v2.5: character_crafting, tp_listings, find_craftable
# ---------------------------------------------------------------------------

class TestCharacterCrafting(Base):
    def test_disciplines_per_character(self):
        self.route("characters", ["Seasky", "Freaza"])
        self.route("characters/Seasky/crafting", {"crafting": [
            {"discipline": "Huntsman", "rating": 500, "active": True},
            {"discipline": "Armorsmith", "rating": 75, "active": False},
        ]})
        self.route("characters/Freaza/crafting", {"crafting": [
            {"discipline": "Chef", "rating": 400, "active": True},
        ]})
        r = g.character_crafting()
        self.assertEqual(r["characters"]["Seasky"]["maxed_500"], ["Huntsman"])
        self.assertEqual(r["characters"]["Seasky"]["disciplines"][0]["rating"], 500)
        self.assertEqual(r["characters"]["Freaza"]["maxed_500"], [])

    def test_missing_character(self):
        self.route("characters", ["Seasky"])
        self.assertIn("error", g.character_crafting("Nada"))

    def test_error_on_one_character_does_not_break_others(self):
        self.route("characters", ["Seasky"])
        self.route("characters/Seasky/crafting", FakeResp(403, {"text": "nope"}))
        r = g.character_crafting()
        self.assertIn("Seasky", r["errors"])


class TestTpListings(Base):
    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)

    def build(self):
        def itens(params, headers):
            if "ids" not in params:
                return [19684]
            return [{"id": 19684, "name": "Glob of Ectoplasm"}]
        self.route("items", itens)
        self.route("commerce/listings/19684", {
            "id": 19684,
            "buys": [
                {"listing_count": 3, "unit_price": 9000, "quantity": 5},
                {"listing_count": 1, "unit_price": 10000, "quantity": 50},
            ],
            "sells": [
                {"listing_count": 4, "unit_price": 13000, "quantity": 20},
                {"listing_count": 2, "unit_price": 12000, "quantity": 9},
            ],
        })

    def test_depth_and_sorting(self):
        self.build()
        r = g.tp_listings(item_name="Glob of Ectoplasm", depth=1)
        self.assertEqual(r["id"], 19684)
        self.assertEqual(len(r["buys"]), 1)
        self.assertEqual(r["buys"][0]["price_copper"], 10000)  # maior lance
        self.assertEqual(r["sells"][0]["price_copper"], 12000)  # menor oferta
        self.assertEqual(r["spread_copper"], 2000)

    def test_nonexistent_name_and_invalid_depth(self):
        self.build()
        self.assertIn("error", g.tp_listings(item_name="zzz inexistente"))
        self.assertIn("error", g.tp_listings(item_id=19684, depth=0))
        self.assertIn("error", g.tp_listings())


class TestFindCraftable(Base):
    def setUp(self):
        super().setUp()
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)

    def build(self):
        import json as json_mod
        recipes = [
            {"id": 1, "output_item_id": 100, "output_item_count": 2,
             "disciplines": ["Chef"], "min_rating": 0,
             "ingredients": [{"item_id": 1, "count": 2}]},   # stock cobre
            {"id": 2, "output_item_id": 200, "output_item_count": 1,
             "disciplines": ["Chef"], "min_rating": 0,
             "ingredients": [{"item_id": 9, "count": 5}]},   # missing ingredient
            {"id": 3, "output_item_id": 300, "output_item_count": 1,
             "disciplines": ["Chef"], "min_rating": 0,
             "ingredients": [{"item_id": 1, "count": 1}]},   # account does not know it
        ]
        with open(os.path.join(self.pasta, "gw2_recipes_cache.json"), "w",
                  encoding="utf-8") as f:
            json_mod.dump(recipes, f)
        self.route("account/materials", [{"id": 1, "count": 10}])
        self.route("account/bank", [None, None])
        self.route("account/recipes", [1, 2])
        self.route("items", by_ids({
            100: {"id": 100, "name": "Bolo Teste"},
            200: {"id": 200, "name": "Torta Teste"},
        }))
        self.route("commerce/prices", by_ids({
            100: {"id": 100, "buys": {"unit_price": 500}, "sells": {"unit_price": 600}},
            200: {"id": 200, "buys": {"unit_price": 900}, "sells": {"unit_price": 1000}},
        }))

    def test_shows_only_stocked_and_sorts_by_value(self):
        self.build()
        r = g.find_craftable(limit=10)
        self.assertEqual(r["checked_recipes"], 3)
        self.assertEqual(r["craftable_count"], 1)
        self.assertEqual(r["items"][0]["item"], "Bolo Teste")
        self.assertEqual(r["items"][0]["output_count"], 2)
        self.assertEqual(r["items"][0]["value_copper"], 1200)  # 600 x 2

    def test_no_known_recipe_and_limits(self):
        self.build()
        self.route("account/recipes", FakeResp(403, {"text": "no unlocks"}))
        r = g.find_craftable()
        self.assertEqual(r["craftable_count"], 2)  # no filter: recipes 1 and 3
        self.assertTrue(any("known recipes" in w for w in r["warnings"]))
        self.assertIn("error", g.find_craftable(limit=0))

    def test_nothing_craftable(self):
        self.route("account/materials", [])
        self.route("account/bank", [])
        self.route("account/recipes", [1])
        with open(os.path.join(self.pasta, "gw2_recipes_cache.json"), "w",
                  encoding="utf-8") as f:
            import json as json_mod
            json_mod.dump([{"id": 1, "output_item_id": 100,
                            "ingredients": [{"item_id": 9, "count": 5}]}], f)
        r = g.find_craftable()
        self.assertEqual(r["craftable_count"], 0)


# ---------------------------------------------------------------------------
# v2.7: raid_progress
# ---------------------------------------------------------------------------

class TestRaids(Base):
    def build(self):
        self.route("raids", lambda p, h: [
            {"id": "forsaken_thicket", "wings": [
                {"id": "spirit_vale", "events": [
                    {"id": "vale_guardian", "type": "Boss"},
                    {"id": "spirit_woods", "type": "Checkpoint"},
                    {"id": "gorseval", "type": "Boss"}]},
                {"id": "salvation_pass", "events": [
                    {"id": "slothasor", "type": "Boss"}]},
            ]},
            {"id": "hall_of_chains", "wings": [
                {"id": "deathly_hallows", "events": [
                    {"id": "mursaat_overseer", "type": "Boss"}]},
            ]},
        ])
        self.route("account/raids", ["spirit_vale"])

    def test_wings_cleared_and_summary(self):
        self.build()
        r = g.raid_progress()
        self.assertEqual(r["wings_total"], 3)
        self.assertEqual(r["wings_cleared"], 1)
        thicket = r["raids"][0]
        self.assertEqual(thicket["raid"], "Forsaken Thicket")
        self.assertFalse(thicket["cleared"])  # 1 de 2 wings
        sv = thicket["wings"][0]
        self.assertTrue(sv["cleared"])
        # checkpoint does not count as a boss
        self.assertEqual(sv["bosses"], ["Vale Guardian", "Gorseval"])
        self.assertFalse(thicket["wings"][1]["cleared"])
        self.assertEqual(r["raids"][1]["wings"][0]["wing"], "Deathly Hallows")

    def test_without_permission_still_lists(self):
        self.build()
        self.route("account/raids", FakeResp(403, {"text": "no"}))
        r = g.raid_progress()
        self.assertEqual(r["wings_cleared"], 0)
        self.assertTrue(any("not readable" in w for w in r["warnings"]))


# ---------------------------------------------------------------------------
# v2.6: item_info (nome), recipes_using, craft_profit, bank_cleanup,
# legendary_route
# ---------------------------------------------------------------------------

class TestItemInfoByName(Base):
    """item_info by name: resolution via the item index in tempdir."""

    def setUp(self):
        super().setUp()
        import json as json_mod
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)
        with open(os.path.join(self.pasta, "gw2_items_index.json"), "w",
                  encoding="utf-8") as f:
            json_mod.dump({"mystic coin": [19976], "bolt": [30699, 30700]}, f)

    def test_resolve_exact_name(self):
        self.route("items/19976", {"id": 19976, "name": "Mystic Coin"})
        self.route("commerce/prices/19976", {
            "id": 19976, "buys": {"unit_price": 100}, "sells": {"unit_price": 200}})
        r = g.item_info(item_name="Mystic Coin")
        self.assertEqual(r["id"], 19976)
        self.assertEqual(r["tp_buy_now_copper"], 200)

    def test_ambiguous_name_lists_candidates(self):
        self.route("items", by_ids({30699: {"id": 30699, "name": "Bolt"},
                                    30700: {"id": 30700, "name": "Bolt II"}}))
        r = g.item_info(item_name="Bolt")
        self.assertIn("error", r)
        self.assertEqual(len(r["candidates"]), 2)

    def test_nonexistent_name(self):
        self.assertIn("error", g.item_info(item_name="Nada Aqui"))


class TestRecipesUsing(Base):
    def setUp(self):
        super().setUp()
        import json as json_mod
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)
        recipes = [
            {"id": 10, "output_item_id": 78425, "output_item_count": 1,
             "disciplines": ["Huntsman"], "min_rating": 500,
             "type": "LegendaryComponent",
             "ingredients": [{"item_id": 1, "count": 3}]},
            {"id": 11, "output_item_id": 100, "output_item_count": 2,
             "disciplines": ["Chef"], "min_rating": 0,
             "ingredients": [{"item_id": 1, "count": 1}, {"item_id": 2, "count": 5}]},
        ]
        with open(os.path.join(self.pasta, "gw2_recipes_cache.json"), "w",
                  encoding="utf-8") as f:
            json_mod.dump(recipes, f)
        self.route("items", by_ids({
            1: {"id": 1, "name": "T6 Dust"},
            100: {"id": 100, "name": "Bolo Teste"},
            78425: {"id": 78425, "name": "Tigris"},
        }))

    def test_legendary_first_and_quantities(self):
        r = g.recipes_using(item_id=1)
        self.assertEqual(r["item"], "T6 Dust")
        self.assertEqual(r["total_recipes"], 2)
        self.assertEqual(r["recipes"][0]["item"], "Tigris")
        self.assertEqual(r["recipes"][0]["type"], "LegendaryComponent")
        self.assertEqual(r["recipes"][0]["used_per_craft"], 3)
        self.assertEqual(r["recipes"][1]["item"], "Bolo Teste")
        self.assertEqual(r["recipes"][1]["used_per_craft"], 1)
        self.assertEqual(r["recipes"][1]["output_count"], 2)
        self.assertFalse(r["truncated"])

    def test_limit_truncates(self):
        r = g.recipes_using(item_id=1, limit=1)
        self.assertEqual(len(r["recipes"]), 1)
        self.assertTrue(r["truncated"])

    def test_unused_and_errors(self):
        r = g.recipes_using(item_id=999)
        self.assertEqual(r["total_recipes"], 0)
        self.assertEqual(r["recipes"], [])
        self.assertIn("error", g.recipes_using(limit=0))
        self.assertIn("error", g.recipes_using())


class TestCraftProfit(Base):
    def setUp(self):
        super().setUp()
        import json as json_mod
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)
        recipes = [
            {"id": 1, "output_item_id": 100, "output_item_count": 1,
             "disciplines": ["Chef"], "min_rating": 400,
             "ingredients": [{"item_id": 1, "count": 2}]},    # lucrativa
            {"id": 2, "output_item_id": 200, "output_item_count": 1,
             "disciplines": ["Chef"], "min_rating": 400,
             "ingredients": [{"item_id": 9, "count": 5}]},    # loss-making
            {"id": 3, "output_item_id": 300, "output_item_count": 1,
             "disciplines": ["Huntsman"], "min_rating": 500,
             "ingredients": [{"item_id": 1, "count": 1}]},    # insufficient level
        ]
        with open(os.path.join(self.pasta, "gw2_recipes_cache.json"), "w",
                  encoding="utf-8") as f:
            json_mod.dump(recipes, f)
        self.route("account/materials", [{"id": 1, "count": 10}])
        self.route("account/bank", [None])
        self.route("account/recipes", [1, 2, 3])
        self.route("characters", ["Seasky"])
        self.route("characters/Seasky/crafting", {"crafting": [
            {"discipline": "Chef", "rating": 500}]})
        self.route("items", by_ids({100: {"id": 100, "name": "Bolo Teste"}}))
        self.route("commerce/prices", by_ids({
            100: {"id": 100, "buys": {"unit_price": 500}, "sells": {"unit_price": 600}},
            200: {"id": 200, "buys": {"unit_price": 100}, "sells": {"unit_price": 150}},
            1: {"id": 1, "buys": {"unit_price": 40}, "sells": {"unit_price": 50}},
            9: {"id": 9, "buys": {"unit_price": 20}, "sells": {"unit_price": 500}},
        }))

    def test_real_profit_ranking(self):
        r = g.craft_profit()
        self.assertEqual(r["checked"], 2)  # 3 falls on the level filter
        self.assertEqual(r["profitable_count"], 1)
        row = r["profitable"][0]
        self.assertEqual(row["item"], "Bolo Teste")
        self.assertEqual(row["sale_net_copper"], 450)  # 500 x 0.90
        self.assertEqual(row["buy_cost_copper"], 0)    # estoque cobre
        self.assertEqual(row["stock_value_copper"], 72)  # 2 x 40 x 0.90
        self.assertEqual(row["profit_copper"], 378)
        self.assertTrue(any("discipline level" in w for w in r["warnings"]))

    def test_filter_by_discipline(self):
        r = g.craft_profit(discipline="Huntsman")
        self.assertEqual(r["profitable"], [])  # only Chef 500; Huntsman 500 does not

    def test_without_permission_uses_in_stock_recipes(self):
        self.route("account/recipes", FakeResp(403, {"text": "no unlocks"}))
        r = g.craft_profit()
        # fully in stock: 1 and 3; 3 falls on the level filter -> checked 1
        self.assertEqual(r["checked"], 1)
        self.assertEqual(r["profitable_count"], 1)
        self.assertTrue(any("fully in stock" in w for w in r["warnings"]))

    def test_invalid_limit(self):
        self.assertIn("error", g.craft_profit(limit=0))

    def test_failed_price_batch_generates_warning(self):
        self.route("commerce/prices", FakeResp(500, {"text": "boom"}))
        r = g.craft_profit()
        self.assertTrue(any("price records unreadable" in w for w in r["warnings"]))


class TestBankCleanup(Base):
    def setUp(self):
        super().setUp()
        import json as json_mod
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)

    def build(self):
        import json as json_mod
        # 600 is a legendary recipe ingredient; nobody uses 500
        with open(os.path.join(self.pasta, "gw2_recipes_cache.json"), "w",
                  encoding="utf-8") as f:
            json_mod.dump([
                {"id": 40, "output_item_id": 78425, "output_item_count": 1,
                 "disciplines": ["Huntsman"], "min_rating": 500,
                 "type": "LegendaryComponent",
                 "ingredients": [{"item_id": 600, "count": 1}]},
            ], f)
        self.route("account/bank", [{"id": 500, "count": 250},
                                   {"id": 600, "count": 10}, None])
        self.route("items", by_ids({
            500: {"id": 500, "name": "Old Junk", "vendor_value": 1},
            600: {"id": 600, "name": "Elder Spirit Spawn", "vendor_value": 0},
        }))
        self.route("commerce/prices", by_ids({
            500: {"id": 500, "buys": {"unit_price": 100}, "sells": {"unit_price": 120}},
            600: {"id": 600, "buys": {"unit_price": 10000}, "sells": {"unit_price": 12000}},
        }))

    def test_verdicts_and_order(self):
        self.build()
        r = g.bank_cleanup()
        self.assertEqual(r["slots_used"], 2)
        self.assertEqual([x["verdict"] for x in r["rows"]],
                         ["keep_crafting", "sell_tp"])
        self.assertEqual(r["rows"][0]["item"], "Elder Spirit Spawn")
        self.assertIn("legendary", r["rows"][0]["reason"])
        self.assertEqual(r["rows"][1]["full_stacks"], 1)  # 250 // 250
        self.assertEqual(r["total_sell_tp_now"], "2 gold, 25 silver, 0 copper")

    def test_filter_min_value_and_limit(self):
        self.build()
        r = g.bank_cleanup(min_value=50000, limit=1)
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["rows"][0]["item"], "Elder Spirit Spawn")
        self.assertEqual(r["hidden_below_min_value"], 1)

    def test_empty_bank(self):
        self.route("account/bank", [])
        r = g.bank_cleanup()
        self.assertEqual(r["slots_used"], 0)

    def test_name_failure_generates_warning_and_fallback(self):
        self.build()
        self.route("items", FakeResp(500, {"text": "boom"}))
        r = g.bank_cleanup()
        self.assertTrue(any("names unreadable" in w for w in r["warnings"]))
        self.assertTrue(any(x["item"].startswith("Item ") for x in r["rows"]))


class TestLegendaryRoute(Base):
    def setUp(self):
        super().setUp()
        import json as json_mod
        import shutil
        import tempfile
        self.pasta = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"GW2_DATA_DIR": self.pasta})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.pasta, True)
        with open(os.path.join(self.pasta, "gw2_items_index.json"), "w",
                  encoding="utf-8") as f:
            json_mod.dump({
                "the bifrost": [30698], "the legend": [30700],
                "gift of fortune": [19626], "gift of wood": [19622],
                "tigris": [78425],
            }, f)

    def build_gen2(self):
        """Tigris-like: LegendaryComponent with a recipe directly in the API."""
        self.route("account/materials", [])
        self.route("account/bank", [])
        self.route("account/legendaryarmory", [])
        self.route("characters", ["Seasky"])
        self.route("characters/Seasky/crafting", {"crafting": [
            {"discipline": "Huntsman", "rating": 500}]})
        self.route("recipes/search", lambda p, h: (
            [11803] if str(p.get("output")) == "78425" else []))
        self.route("recipes", by_ids({11803: {
            "id": 11803, "output_item_id": 78425, "output_item_count": 1,
            "disciplines": ["Huntsman"], "min_rating": 500,
            "type": "LegendaryComponent",
            "ingredients": [{"item_id": 78119, "count": 1},
                            {"item_id": 78342, "count": 1},
                            {"item_id": 78475, "count": 1},
                            {"item_id": 78642, "count": 1}]}}))
        self.route("items", by_ids({
            78425: {"id": 78425, "name": "Tigris"},
            78119: {"id": 78119, "name": "Lump of Aurillium"},
            78342: {"id": 78342, "name": "The Hunter"},
            78475: {"id": 78475, "name": "Chak Egg Sac"},
            78642: {"id": 78642, "name": "Spriggan Heart"},
        }))
        self.route("commerce/prices", by_ids({}))  # collection: no TP price

    def build_gen1(self):
        """Bifrost-like: no recipe in the API, falls back to the Wiki {{recipe}}."""
        self.route("account/materials", [])
        self.route("account/bank", [])
        self.route("account/legendaryarmory", [])
        self.route("recipes/search", lambda p, h: [])
        self.route("items", by_ids({
            30698: {"id": 30698, "name": "The Bifrost"},
            30700: {"id": 30700, "name": "The Legend"},
            19626: {"id": 19626, "name": "Gift of Fortune"},
            19622: {"id": 19622, "name": "Gift of Wood"},
        }))
        self.route("commerce/prices", by_ids({
            30700: {"id": 30700, "sells": {"unit_price": 500000},
                    "buys": {"unit_price": 400000}},
            19622: {"id": 19622, "sells": {"unit_price": 30000},
                    "buys": {"unit_price": 25000}},
        }))
        self.router.add(f"{WIKI}/wiki/The_Bifrost", FakeResp(200, None, text="""
{{Recipe
| source = [[Mystic Forge]]
| quantity = 1
| ingredient1 = 1 [[The Legend]]
| ingredient2 = 1 [[Gift of Fortune]]
}}
"""))
        self.router.add(f"{WIKI}/wiki/Gift_of_Fortune", FakeResp(200, None, text="""
{{Recipe
| quantity = 1
| ingredient1 = 1 [[Mysterious Relic]]
| ingredient2 = 1 [[Gift of Wood]]
}}
"""))

    def test_gen2_collection_components(self):
        self.build_gen2()
        r = g.legendary_route(item_id=78425)
        self.assertEqual(r["legendary"], "Tigris")
        self.assertFalse(r["already_owned"])
        self.assertEqual(r["craft_disciplines"], {"Huntsman": 500})
        self.assertEqual(r["steps"][0]["source"], "craft")
        self.assertTrue(r["steps"][0]["craftable"])
        self.assertEqual(r["steps"][0]["runs"], 1)
        filhos = r["steps"][1:]
        self.assertEqual(len(filhos), 4)
        self.assertTrue(all(s["source"] == "collection" for s in filhos))
        self.assertIn("wiki", filhos[0])
        self.assertEqual(r["est_buy_cost_copper"], 0)

    def test_already_in_armory_and_stock(self):
        self.build_gen2()
        self.route("account/legendaryarmory", [{"id": 78425, "count": 1}])
        self.route("account/materials", [{"id": 78425, "count": 1}])
        r = g.legendary_route(item_id=78425)
        self.assertTrue(r["already_owned"])
        self.assertEqual(r["in_armory"], 1)
        self.assertEqual(len(r["steps"]), 1)
        self.assertEqual(r["steps"][0]["source"], "stock")

    def test_gen1_wiki_recipe_with_unresolved_step(self):
        self.build_gen1()
        r = g.legendary_route(item_id=30698, depth=3)
        self.assertEqual(r["legendary"], "The Bifrost")
        self.assertEqual([s["source"] for s in r["steps"]],
                         ["craft_wiki", "buy", "craft_wiki", "unresolved", "buy"])
        self.assertEqual(r["steps"][1]["item"], "The Legend")
        self.assertEqual(r["steps"][1]["est_cost"], "50 gold, 0 silver, 0 copper")
        self.assertEqual(r["steps"][3]["item"], "Mysterious Relic")
        self.assertEqual(r["est_buy_cost_copper"], 530000)
        self.assertEqual(r["craft_disciplines"], {})

    def test_argument_errors(self):
        self.assertIn("error", g.legendary_route())
        self.assertIn("error", g.legendary_route(item_id=1, depth=0))
        self.assertIn("error", g.legendary_route(item_id=1, depth=6))


if __name__ == "__main__":
    unittest.main()
