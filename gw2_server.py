"""MCP server for Guild Wars 2 account queries.

Environment variable:
    GW2_API_KEY  GW2 API key (required for account tools). Sent only via
    Authorization header; never printed, logged, nor exposed in errors.

Version 2.8: 40 read-only tools; achievement_summary folded into
progress_guide as its 'summary' block; v2.7: raid_progress added;
crafting_cost folded into item_craft_info as helper; v2.6: item_info +
recipes_using + craft_profit +
bank_cleanup + legendary_route; achievement_details joins account progress;
wiki {{recipe}} parsing for gen1 legendaries; compact responses, English code
and comments.
"""

import json
import math
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

try:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x
except ImportError:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP("Guild Wars 2 Assistant")

# ============================================================
# Application constants
# ============================================================

VERSION = "2.7"
API_BASE = "https://api.guildwars2.com/v2"
WIKI_BASE = "https://wiki.guildwars2.com"
USER_AGENT = f"GW2PersonalAssistant/{VERSION} (personal assistant; individual use)"
# The Wiki blocks generic User-Agents (nginx 403, never reaches MediaWiki):
# it needs a browser prefix + identification (tested live on 2026-10-14).
WIKI_UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {USER_AGENT}"
TIMEOUT = 30
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Cache TTLs (seconds)
NAME_TTL = 24 * 3600       # Item/achievement names change rarely
PRICE_TTL = 60              # Trading Post prices fluctuate quickly
WIKI_TTL = 600              # Wiki content is semi-static
UNLOCK_TTL = 120            # Account unlocks change on login
CATEGORY_TTL = 3600         # Achievement categories are stable
ITEM_INDEX_TTL = 7 * 24 * 3600  # Item name index (name -> id) on disk

# Trading Post fee constants
TP_LISTED_FEE = 0.15        # 5% listing + 10% sale
TP_IMMEDIATE_FEE = 0.10     # 10% sale only
MAX_FLOORS = 60             # Max continent floors to scan for area lookup
REQUIRED_SCOPES = (         # Scopes the server tools rely on
    "account", "wallet", "progression", "inventories", "characters",
    "unlocks", "tradingpost", "pvp", "wvw", "builds", "guilds",
)


# ============================================================
# Error tracking (last 20 errors for diagnostics)
# ============================================================

_recent_errors = deque(maxlen=20)


class GW2Error(Exception):
    """Safe-to-display error message (never includes the API key)."""

    def __init__(self, *args):
        super().__init__(*args)
        _recent_errors.append(
            (datetime.now(timezone.utc).isoformat(), str(self)[:300])
        )


# ============================================================
# Infrastructure: session, cache, HTTP requests
# ============================================================

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    """Retrieve a cached value by key, or None if missing/expired."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.time():
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key, value, ttl):
    """Store a value in the cache with a time-to-live (seconds)."""
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)
    return value


def get_api_key():
    """Return the GW2 API key from the environment, or raise if missing."""
    key = (os.environ.get("GW2_API_KEY") or "").strip()
    if not key:
        raise GW2Error(
            "GW2_API_KEY is not set in this environment. "
            "Set the environment variable and restart the MCP server "
            "(and the app that starts it)."
        )
    return key


def _retry_wait(response, attempt):
    """Calculate wait time before retry (max 10s, exponential backoff)."""
    try:
        return min(float(response.headers.get("Retry-After", "")), 10)
    except (TypeError, ValueError, AttributeError):
        return min(0.5 * (2 ** attempt), 5)


def _request(url, params=None, headers=None, attempts=3):
    """HTTP GET with retries and exponential backoff. Never leaks URL in errors."""
    last_error = None
    for n in range(attempts):
        try:
            response = _session.get(
                url, params=params, headers=headers, timeout=TIMEOUT
            )
        except requests.RequestException as err:
            last_error = type(err).__name__
            if n < attempts - 1:
                time.sleep(0.5 * (2 ** n))
            continue

        if response.status_code in RETRYABLE_STATUS and n < attempts - 1:
            time.sleep(_retry_wait(response, n))
            continue
        return response

    host = url.split("/")[2] if "//" in url else url
    raise GW2Error(f"Network failure accessing {host} ({last_error}).")


def _error_text(response):
    """Extract error message from API response body."""
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("text") or body.get("error") or body)[:300]
    except ValueError:
        pass  # non-JSON body: the .text fallback below covers it
    return (getattr(response, "text", "") or "")[:300].strip()


# Likely permission for each endpoint group (used only in error messages).
ENDPOINT_PERMISSIONS = [
    ("account/wizardsvault", "progression"),
    ("account/skins", "unlocks"),
    ("account/dyes", "unlocks"),
    ("account/minis", "unlocks"),
    ("account/mounts", "unlocks"),
    ("account/titles", "unlocks"),
    ("account/outfits", "unlocks"),
    ("account/gliders", "unlocks"),
    ("account/finishers", "unlocks"),
    ("account/mailcarriers", "unlocks"),
    ("account/novelties", "unlocks"),
    ("account/recipes", "unlocks"),
    ("account/legendaryarmory", "unlocks"),
    ("commerce/transactions", "tradingpost"),
    ("commerce/delivery", "tradingpost"),
    ("pvp", "pvp"),
    ("characters/", "characters"),
    ("account/wallet", "wallet"),
    ("account/materials", "inventories"),
    ("account/bank", "inventories"),
    ("account/inventory", "inventories"),
    ("account/achievements", "progression"),
    ("account/masteries", "progression"),
    ("account/mastery", "progression"),
    ("account/worldbosses", "progression"),
    ("account/raids", "progression"),
    ("account/dungeons", "progression"),
    ("account/mapchests", "progression"),
    ("account/dailycrafting", "progression"),
    ("characters", "characters"),
    ("account", "account"),
]


def _likely_permission(endpoint):
    """Return the likely required permission for an API endpoint."""
    base = endpoint.split("?")[0].strip("/")
    for prefix, permission in ENDPOINT_PERMISSIONS:
        if base.startswith(prefix):
            return permission
    return None


def gw2_get(endpoint, params=None, authenticated=True):
    """Make an authenticated GW2 API GET request and return JSON."""
    params = dict(params or {})
    headers = {}
    if authenticated:
        headers["Authorization"] = f"Bearer {get_api_key()}"

    endpoint = endpoint.lstrip("/")
    response = _request(
        f"{API_BASE}/{endpoint}", params=params, headers=headers
    )

    if response.status_code in (200, 206):
        try:
            return response.json()
        except ValueError:
            raise GW2Error(
                f"The API returned an invalid response at '{endpoint}'."
            )

    code = response.status_code
    detail = _error_text(response)

    if code == 401:
        raise GW2Error(
            "API key invalid or expired (HTTP 401). Generate a new "
            "key at account.arena.net and update GW2_API_KEY."
        )
    if code == 403:
        permission = _likely_permission(endpoint)
        hint = f" Likely permission: '{permission}'." if permission else ""
        raise GW2Error(
            f"Access denied to '{endpoint}' (HTTP 403).{hint} "
            f"Use diagnostics to check key permissions. {detail}"
        )
    if code == 404:
        raise GW2Error(f"Resource not found at '{endpoint}' (HTTP 404). {detail}")
    if code == 429:
        raise GW2Error("API rate limit reached (HTTP 429). Try again shortly.")
    raise GW2Error(f"The API returned HTTP {code} at '{endpoint}'. {detail}")


def resolve_names(endpoint, ids, lang="en", batch=200, warnings=None):
    """Resolve id -> API object in batches, with caching.

    Names are an enrichment: if a batch fails, those ids are skipped and
    the caller uses a fallback text ("Item 123"); pass a warnings list to
    also surface the failure.
    """
    unique = list(dict.fromkeys(i for i in ids if isinstance(i, int)))
    result = {}
    missing = []
    for ident in unique:
        value = _cache_get(("obj", endpoint, lang, ident))
        if value is not None:
            result[ident] = value
        else:
            missing.append(ident)

    for start in range(0, len(missing), batch):
        part = missing[start:start + batch]
        try:
            objects = gw2_get(
                endpoint,
                {"ids": ",".join(map(str, part)), "lang": lang},
                authenticated=False,
            )
        except GW2Error as err:
            if warnings is not None:
                warnings.append(f"{len(part)} {endpoint} names unreadable ({err}).")
            continue
        if not isinstance(objects, list):
            continue
        for obj in objects:
            if isinstance(obj, dict) and "id" in obj:
                _cache_set(("obj", endpoint, lang, obj["id"]), obj, NAME_TTL)
                result[obj["id"]] = obj
    return result


# ============================================================
# Utility functions
# ============================================================

def format_coins(copper):
    """Format copper amount as 'X gold, Y silver, Z copper'."""
    copper = int(copper)
    sign = "-" if copper < 0 else ""
    copper = abs(copper)
    gold, rest = divmod(copper, 10000)
    silver, copper = divmod(rest, 100)
    return f"{sign}{gold} gold, {silver} silver, {copper} copper"


def _wiki_link(title):
    """Build a wiki page URL from a title."""
    return WIKI_BASE + "/wiki/" + quote(title.replace(" ", "_"), safe="()'_-,:!")


def _int(value, default=0):
    """Safely convert to int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc_now():
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


# Mapping from achievement bit/reward type to API endpoint
TYPE_TO_ENDPOINT = {
    "Item": "items",
    "Minipet": "minis",
    "Skin": "skins",
    "Title": "titles",
}


def _reference_names(pairs):
    """pairs: iterable of (type, id). Returns {(type, id): name}."""
    ids_by_type = {}
    for type_, ident in pairs:
        if type_ in TYPE_TO_ENDPOINT and isinstance(ident, int):
            ids_by_type.setdefault(type_, []).append(ident)

    names = {}
    for type_, ids in ids_by_type.items():
        for ident, obj in resolve_names(TYPE_TO_ENDPOINT[type_], ids).items():
            name = obj.get("name")
            if name:
                names[(type_, ident)] = name
    return names


def _describe_bit(bit, names):
    """Return a human-readable description of an achievement bit."""
    type_ = bit.get("type", "Unknown")
    if type_ == "Text":
        return (bit.get("text") or "").strip()
    name = names.get((type_, bit.get("id")))
    if name:
        return name
    if "id" in bit:
        return f"{type_} {bit['id']}"
    return type_


def _format_rewards(detail, names):
    """Format achievement rewards with resolved names."""
    rewards = []
    for reward in detail.get("rewards") or []:
        if not isinstance(reward, dict):
            continue
        type_ = reward.get("type", "Unknown")
        line = {"type": type_}
        for field in ("id", "count", "region"):
            if field in reward:
                line[field] = reward[field]
        name = names.get((type_, reward.get("id")))
        if name:
            line["name"] = name
        rewards.append(line)
    return rewards


# ============================================================
# Achievements: shared logic
# ============================================================

def _parallel(tasks, max_workers=6):
    """Run independent API calls concurrently.

    tasks: {name: function with no arguments}. Returns
    {name: (True, result) | (False, error_message)}.
    """
    def run(func):
        try:
            return True, func()
        except GW2Error as err:
            return False, str(err)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {name: pool.submit(run, f) for name, f in tasks.items()}
        return {name: future.result() for name, future in futures.items()}


def _ids_in_categories():
    """IDs of achievements that appear in any current game category.
    Achievements outside any category are usually retired content."""
    key = ("ids_in_categories",)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = gw2_get("achievements/categories", {"ids": "all"}, authenticated=False)
    if not isinstance(data, list):
        raise GW2Error("Unexpected format in achievements/categories.")
    ids = set()
    for category in data:
        if not isinstance(category, dict):
            continue
        for a in category.get("achievements") or []:
            if isinstance(a, int):
                ids.add(a)
            elif isinstance(a, dict) and isinstance(a.get("id"), int):
                ids.add(a["id"])
    if not ids:
        raise GW2Error("The API did not return achievement categories.")
    return _cache_set(key, ids, CATEGORY_TTL)


def _account_progress():
    """Fetch raw achievement progress data for the account."""
    data = gw2_get("account/achievements")
    if not isinstance(data, list):
        raise GW2Error("The API returned an unexpected format in account/achievements.")
    return [d for d in data if isinstance(d, dict) and "id" in d]


def _is_partial(item):
    """Has numeric progress but hasn't reached the max or been completed."""
    if item.get("done"):
        return False
    current, maximum = item.get("current"), item.get("max")
    return (
        isinstance(current, (int, float))
        and isinstance(maximum, (int, float))
        and maximum > 0
        and 0 < current < maximum
    )


def _points(detail, item):
    """Returns (points_earned, points_pending) based on tiers."""
    tiers = detail.get("tiers") or []
    total = sum(_int(t.get("points")) for t in tiers if isinstance(t, dict))
    if item.get("done"):
        return total, 0
    current = item.get("current")
    if not isinstance(current, (int, float)):
        current = 0
    earned = sum(
        _int(t.get("points"))
        for t in tiers
        if isinstance(t, dict) and _int(t.get("count")) <= current
    )
    return earned, total - earned


def _next_tier(detail, current):
    """Return the next tier info (target, points, remaining) or None."""
    pending = [
        t for t in (detail.get("tiers") or [])
        if isinstance(t, dict) and _int(t.get("count")) > current
    ]
    if not pending:
        return None
    t = min(pending, key=lambda x: _int(x.get("count")))
    return {
        "target": _int(t.get("count")),
        "points": _int(t.get("points")),
        "remaining": _int(t.get("count")) - current,
    }


def _unregistered_bits(detail, item):
    """List of (index, bit) not registered in the account, or None if the API
    doesn't provide per-bit progress for this achievement."""
    bits = detail.get("bits")
    account_bits = item.get("bits")
    if not isinstance(bits, list) or not isinstance(account_bits, list):
        return None
    registered = set(account_bits)
    return [
        (i, bit) for i, bit in enumerate(bits)
        if i not in registered and isinstance(bit, dict)
    ]


SORT_OPTIONS = ("percent", "remaining", "points", "points_per_unit")


def _analyze_partials(limit, sort_by, hide_unavailable=False):
    """Analyze achievements with partial progress and return enriched data."""
    progress = _account_progress()
    by_id = {p["id"]: p for p in progress}

    candidates = []
    for item in progress:
        if not _is_partial(item):
            continue
        current, maximum = item["current"], item["max"]
        candidates.append({
            "id": item["id"],
            "current": current,
            "max": maximum,
            "remaining": maximum - current,
            "percent": round(current / maximum * 100, 1),
        })

    hidden, availability_warning = None, None
    if hide_unavailable and candidates:
        try:
            available = _ids_in_categories()
        except GW2Error as err:
            availability_warning = (
                f"Could not check achievement availability: {err}"
            )
        else:
            outside = [c for c in candidates if c["id"] not in available]
            candidates = [c for c in candidates if c["id"] in available]
            outside.sort(key=lambda c: -c["percent"])
            outside_names = resolve_names("achievements", [c["id"] for c in outside[:8]])
            hidden = {
                "total": len(outside),
                "examples": [
                    outside_names.get(c["id"], {}).get("name", f"Achievement {c['id']}")
                    for c in outside[:8]
                ],
            }

    details = {}
    if sort_by in ("points", "points_per_unit"):
        details = resolve_names("achievements", [c["id"] for c in candidates])
        for c in candidates:
            _, c["points_pending"] = _points(
                details.get(c["id"], {}), by_id[c["id"]]
            )

    keys = {
        "percent": lambda c: (-c["percent"], c["remaining"]),
        "remaining": lambda c: (c["remaining"], -c["percent"]),
        "points": lambda c: (-c.get("points_pending", 0), c["remaining"]),
        "points_per_unit": lambda c: (
            -(c.get("points_pending", 0) / c["remaining"]), c["remaining"]
        ),
    }
    candidates.sort(key=keys[sort_by])
    selected = candidates[:limit]

    details.update(resolve_names("achievements", [c["id"] for c in selected]))
    for c in selected:
        earned, pending = _points(details.get(c["id"], {}), by_id[c["id"]])
        c["points_earned"] = earned
        c["points_pending"] = pending

    prereq_ids = []
    for c in selected:
        prereq_ids.extend(details.get(c["id"], {}).get("prerequisites") or [])
    prereq_details = resolve_names("achievements", prereq_ids)

    return {
        "progress": progress,
        "by_id": by_id,
        "candidates": candidates,
        "selected": selected,
        "details": details,
        "prereq_details": prereq_details,
        "hidden": hidden,
        "availability_warning": availability_warning,
    }


def _selected_references(analysis, include_bits):
    """Collect (type, id) pairs from selected achievements' rewards and bits."""
    pairs = set()
    for c in analysis["selected"]:
        detail = analysis["details"].get(c["id"], {})
        for r in detail.get("rewards") or []:
            if isinstance(r, dict):
                pairs.add((r.get("type"), r.get("id")))
        if include_bits:
            pending = _unregistered_bits(detail, analysis["by_id"][c["id"]])
            for _, bit in pending or []:
                pairs.add((bit.get("type"), bit.get("id")))
    return pairs


def _described_bits(detail, item, names):
    """Returns (named_bits, empty_entries_ignored) or (None, 0)."""
    pending = _unregistered_bits(detail, item)
    if pending is None:
        return None, 0
    bits, empty = [], 0
    for index, bit in pending:
        description = _describe_bit(bit, names)
        if description:
            bits.append({"index": index, "description": description})
        else:
            empty += 1
    return bits, empty


_COLOR_TAGS = re.compile(r"</?c(?:=[^>]*)?>")


def _strip_tags(text):
    """Remove API color markup like <c=@flavor>...</c>."""
    return _COLOR_TAGS.sub("", text or "").strip()


def _achievement_target(detail, item=None):
    """Get the target count for an achievement from tiers or item max."""
    counts = [
        _int(t.get("count")) for t in (detail.get("tiers") or [])
        if isinstance(t, dict)
    ]
    if counts:
        return max(counts)
    if item and isinstance(item.get("max"), (int, float)):
        return int(item["max"])
    return None


def _readable_requirement(detail, item=None):
    """The API leaves a double space where the requirement number should be
    ("Obtain  rank points"). Fill it with the achievement's final target."""
    text = _strip_tags(detail.get("requirement", ""))
    target = _achievement_target(detail, item)
    if target and re.search(r"\S {2,}\S", text):
        return re.sub(r"(?<=\S) {2,}(?=\S)", f" {target:,} ".replace(",", "."), text, count=1)
    return text


def _limit(lst, max_items):
    """Returns (truncated_list, omitted_count)."""
    if max_items is None or max_items <= 0 or len(lst) <= max_items:
        return lst, 0
    return lst[:max_items], len(lst) - max_items


# ============================================================
# Account tools
# ============================================================

@mcp.tool()
def account_info() -> dict:
    """Get account name, access level, and basic information."""
    return gw2_get("account")


@mcp.tool()
def account_wallet() -> list:
    """Get account wallet currencies. Requires wallet permission."""
    wallet = gw2_get("account/wallet")
    currencies = resolve_names(
        "currencies", [i.get("id") for i in wallet if isinstance(i, dict)]
    )
    result = []
    for item in wallet:
        ident = item["id"]
        line = {
            "id": ident,
            "currency": currencies.get(ident, {}).get("name", "Unknown"),
            "quantity": item.get("value", 0),
        }
        if ident == 1:  # Gold coin
            line["formatted_quantity"] = format_coins(line["quantity"])
        result.append(line)
    return result


@mcp.tool()
def materials_with_names(sort_by: str = "name", limit: int = 0) -> dict:
    """List account materials with names, quantities, and categories.
    sort_by: 'name' or 'quantity'. limit=0 returns all."""
    if sort_by not in ("name", "quantity"):
        return {"error": "sort_by must be 'name' or 'quantity'."}

    materials = [m for m in gw2_get("account/materials") if m.get("count", 0) > 0]
    names = resolve_names("items", [m["id"] for m in materials])

    lines = [
        {
            "id": m["id"],
            "name": names.get(m["id"], {}).get("name", f"Item {m['id']}"),
            "quantity": m["count"],
            "category": m.get("category"),
        }
        for m in materials
    ]
    if sort_by == "quantity":
        lines.sort(key=lambda x: (-x["quantity"], x["name"].lower()))
    else:
        lines.sort(key=lambda x: x["name"].lower())

    shown = lines[:limit] if limit and limit > 0 else lines
    return {
        "material_types": len(lines),
        "showing": len(shown),
        "sorted_by": sort_by,
        "materials": shown,
    }


@mcp.tool()
def account_bank(sort_by: str = "quantity", limit: int = 0) -> dict:
    """List account bank items (grouped by item, with names).
    Requires account + inventories permissions. sort_by: 'quantity' or 'name'."""
    if sort_by not in ("quantity", "name"):
        return {"error": "sort_by must be 'quantity' or 'name'."}

    bank = gw2_get("account/bank")
    occupied = [s for s in bank if isinstance(s, dict) and s.get("id")]
    totals = {}
    for slot in occupied:
        totals[slot["id"]] = totals.get(slot["id"], 0) + _int(slot.get("count"), 1)

    names = resolve_names("items", list(totals))
    lines = [
        {
            "id": ident,
            "name": names.get(ident, {}).get("name", f"Item {ident}"),
            "quantity": qty,
        }
        for ident, qty in totals.items()
    ]
    if sort_by == "name":
        lines.sort(key=lambda x: x["name"].lower())
    else:
        lines.sort(key=lambda x: (-x["quantity"], x["name"].lower()))

    shown = lines[:limit] if limit and limit > 0 else lines
    return {
        "slots_total": len(bank),
        "slots_occupied": len(occupied),
        "distinct_items": len(lines),
        "showing": len(shown),
        "items": shown,
    }


@mcp.tool()
def character_list() -> dict:
    """List account characters (race, profession, level, hours played).
    Requires account + characters permissions."""
    names = gw2_get("characters")
    characters, errors = [], {}
    for name in names:
        name = name if isinstance(name, str) else (name or {}).get("name")
        if not name:
            continue
        try:
            core = gw2_get(f"characters/{quote(name, safe='')}/core")
        except GW2Error as err:
            errors[name] = str(err)
            continue
        characters.append({
            "name": core.get("name", name),
            "race": core.get("race"),
            "profession": core.get("profession"),
            "level": core.get("level"),
            "hours_played": round(_int(core.get("age")) / 3600, 1),
            "deaths": core.get("deaths"),
            "created_at": core.get("created"),
        })
    characters.sort(key=lambda p: -(p["level"] or 0))
    result = {"count": len(characters), "characters": characters}
    if errors:
        result["errors"] = errors
    return result


@mcp.tool()
def character_crafting(character: str = "") -> dict:
    """Crafting disciplines per character (rating up to 500, active, maxed).
    Use to check who can craft what (e.g., Huntsman 500 for legendaries).
    character: name (empty = all). Requires characters permission."""
    names = _character_names(character)
    if not names:
        return {"error": f"Character '{character}' not found."}

    fetches = _parallel({
        name: (lambda n=name: gw2_get(f"characters/{quote(n, safe='')}/crafting"))
        for name in names
    })
    result, errors = {}, {}
    for name, (ok, value) in fetches.items():
        if not ok:
            errors[name] = value
            continue
        raw = value.get("crafting") if isinstance(value, dict) else value
        rows = []
        for d in raw or []:
            if not isinstance(d, dict):
                continue
            rows.append({
                "discipline": d.get("discipline"),
                "rating": _int(d.get("rating")),
                "active": bool(d.get("active")),
            })
        rows.sort(key=lambda r: -r["rating"])
        result[name] = {
            "maxed_500": [r["discipline"] for r in rows if r["rating"] >= 500],
            "disciplines": rows,
        }
    output = {"characters": result}
    if errors:
        output["errors"] = errors
    return output


@mcp.tool()
def account_inventory(character: str = "", item_limit: int = 0) -> dict:
    """Get character bag inventories, grouped by item with names.
    Requires account + characters + inventories permissions.
    character: filter by character name. item_limit=0 returns all items."""
    # /characters returns a list of NAMES (strings), not objects.
    names = [c if isinstance(c, str) else (c or {}).get("name") for c in gw2_get("characters")]
    names = [n for n in names if n]
    if character:
        names = [n for n in names if n.lower() == character.strip().lower()]
        if not names:
            return {"error": f"Character '{character}' not found."}

    raws, errors = {}, {}
    for name in names:
        try:
            raws[name] = gw2_get(f"characters/{quote(name, safe='')}/inventory")
        except GW2Error as err:
            errors[name] = str(err)

    per_character = {}
    all_ids = []
    for name, inventory in raws.items():
        totals, bags = {}, 0
        for bag in (inventory or {}).get("bags") or []:
            if not isinstance(bag, dict):
                continue  # empty bag slot
            bags += 1
            for slot in bag.get("inventory") or []:
                if isinstance(slot, dict) and slot.get("id"):
                    totals[slot["id"]] = totals.get(slot["id"], 0) + _int(slot.get("count"), 1)
        per_character[name] = (bags, totals)
        all_ids.extend(totals)

    item_names = resolve_names("items", all_ids)
    result = {}
    for name, (bags, totals) in per_character.items():
        lines = sorted(
            (
                {
                    "id": ident,
                    "name": item_names.get(ident, {}).get("name", f"Item {ident}"),
                    "quantity": qty,
                }
                for ident, qty in totals.items()
            ),
            key=lambda x: (-x["quantity"], x["name"].lower()),
        )
        shown = lines[:item_limit] if item_limit and item_limit > 0 else lines
        result[name] = {
            "bags": bags,
            "distinct_items": len(lines),
            "items": shown,
        }

    output = {"characters": result}
    if errors:
        output["errors"] = errors
    return output


@mcp.tool()
def daily_weekly_progress() -> dict:
    """Show what the account has done today/this week: world bosses, raids,
    dungeons, map chests, and daily crafting. Requires progression permission.
    Daily reset at 00:00 UTC (21:00 BRT); weekly on Monday, 07:30 UTC."""
    sections = {
        "world_bosses_today": "account/worldbosses",
        "raids_this_week": "account/raids",
        "dungeons_today": "account/dungeons",
        "map_chests_today": "account/mapchests",
        "daily_crafting_today": "account/dailycrafting",
    }
    result, errors = {}, {}
    for key, endpoint in sections.items():
        try:
            result[key] = gw2_get(endpoint)
        except GW2Error as err:
            errors[key] = str(err)
    output = {"queried_at_utc": _utc_now().isoformat(), **result}
    if errors:
        output["errors"] = errors
    return output


@mcp.tool()
def masteries() -> dict:
    """Show mastery points per region and unlocked mastery tracks.
    Requires progression permission."""
    points = gw2_get("account/mastery/points")
    account_tracks = gw2_get("account/masteries")
    details = resolve_names(
        "masteries", [t.get("id") for t in account_tracks if isinstance(t, dict)]
    )
    tracks = []
    for track in account_tracks:
        ident = track.get("id")
        detail = details.get(ident, {})
        tracks.append({
            "id": ident,
            "name": detail.get("name", f"Mastery {ident}"),
            "region": detail.get("region"),
            "unlocked_level_index": track.get("level"),
            "total_levels": len(detail.get("levels") or []) or None,
        })
    return {
        "points_by_region": (points or {}).get("totals"),
        "tracks": tracks,
        "note": "unlocked_level_index is the index reported by the API (starts at 0).",
    }


# ============================================================
# Achievement tools
# ============================================================

@mcp.tool()
def achievement_details(
    achievement_ids: list[int], include_bits: bool = False, include_progress: bool = True
) -> list:
    """Return names, descriptions, tiers, rewards, and achievement bits by ID.
    include_bits=True lists all bits (can be very long).
    include_progress=True joins YOUR progress per achievement (done/current/max)
    — the account-wide equivalent of filtering account achievements; set False
    to skip the extra call."""
    if not achievement_ids:
        return []

    ids = list(dict.fromkeys(achievement_ids))
    details = resolve_names("achievements", ids)

    # Account progress in one call; a failure (key without scope) does not kill the tool.
    progress_map, progress_error = {}, None
    if include_progress:
        try:
            progress_map = {p.get("id"): p for p in _account_progress()}
        except GW2Error as err:
            progress_error = str(err)

    pairs = set()
    for detail in details.values():
        for r in detail.get("rewards") or []:
            if isinstance(r, dict):
                pairs.add((r.get("type"), r.get("id")))
        if include_bits:
            for bit in detail.get("bits") or []:
                if isinstance(bit, dict):
                    pairs.add((bit.get("type"), bit.get("id")))
    names = _reference_names(pairs)

    results = []
    for ident in ids:
        detail = details.get(ident)
        if detail is None:
            results.append({"id": ident, "error": "Achievement not found."})
            continue
        line = {
            "id": ident,
            "name": detail.get("name"),
            "description": _strip_tags(detail.get("description", "")),
            "requirement": _readable_requirement(detail),
            "type": detail.get("type"),
            "flags": detail.get("flags", []),
            "tiers": detail.get("tiers", []),
            "rewards": _format_rewards(detail, names),
            "prerequisites": detail.get("prerequisites", []),
            "total_bits": len(detail.get("bits") or []),
        }
        if include_progress:
            if progress_error:
                line["progress_error"] = progress_error
            else:
                rec = progress_map.get(ident)
                line["progress"] = (
                    {
                        "done": bool(rec.get("done")),
                        "current": _int(rec.get("current")),
                        "max": _int(rec.get("max")),
                        "bits_unlocked": len(rec.get("bits") or []),
                    }
                    if rec is not None
                    else {"done": False, "current": 0, "max": 0, "note": "no record for this account"}
                )
        if include_bits:
            line["bits"] = [
                {"index": i, "description": d}
                for i, bit in enumerate(detail.get("bits") or [])
                if isinstance(bit, dict) and (d := _describe_bit(bit, names))
            ]
        results.append(line)
    return results


@mcp.tool()
def achievement_points() -> dict:
    """Estimate total achievement points and progress toward the next 500-point milestone.
    Requires account + progression permissions."""
    progress = _account_progress()
    details = resolve_names("achievements", [p["id"] for p in progress])

    total, missing_detail = 0, 0
    for item in progress:
        detail = details.get(item["id"])
        if detail is None:
            missing_detail += 1
            continue
        earned, _ = _points(detail, item)
        total += earned

    account = gw2_get("account")
    daily = _int(account.get("daily_ap"))
    monthly = _int(account.get("monthly_ap"))
    estimated_total = total + daily + monthly
    next_milestone = (estimated_total // 500 + 1) * 500

    return {
        "queried_at_utc": _utc_now().isoformat(),
        "achievement_points": total,
        "daily_points": daily,
        "monthly_points": monthly,
        "estimated_total": estimated_total,
        "next_milestone": next_milestone,
        "remaining_to_milestone": next_milestone - estimated_total,
        "records_missing_detail": missing_detail,
        "warning": (
            "Estimate based on achievement tiers; may diverge "
            "from in-game value (repeatable and hidden achievements)."
        ),
    }


@mcp.tool()
def progress_guide(
    limit: int = 15,
    sort_by: str = "percent",
    include_bits: bool = True,
    max_bits: int = 10,
    hide_unavailable: bool = True,
    compact: bool = True,
) -> dict:
    """Analyze achievements with partial progress: named pending bits, points
    per tier, prerequisites, rewards. summary: totals of ALL progress records
    (records/completed/in_progress/no_progress_recorded).
    sort_by: 'percent'|'remaining'|'points'|'points_per_unit'. max_bits: 0 = all.
    hide_unavailable: skip achievements outside current categories."""
    if not 1 <= limit <= 30:
        return {"error": "limit must be between 1 and 30."}
    if sort_by not in SORT_OPTIONS:
        return {"error": f"sort_by must be one of: {', '.join(SORT_OPTIONS)}."}

    analysis = _analyze_partials(limit, sort_by, hide_unavailable)
    names = _reference_names(_selected_references(analysis, include_bits))

    recommendations = []
    for c in analysis["selected"]:
        ident = c["id"]
        detail = analysis["details"].get(ident, {})
        item = analysis["by_id"][ident]
        name = detail.get("name", f"Achievement {ident}")

        if c["percent"] >= 80:
            priority = "High: investigate first"
        elif c["percent"] >= 50:
            priority = "Medium: assess remaining effort"
        else:
            priority = "Low: early progress"

        bits, empty = _described_bits(detail, item, names)

        prerequisites = []
        for prereq_id in detail.get("prerequisites") or []:
            prereq = analysis["prereq_details"].get(prereq_id, {})
            record = analysis["by_id"].get(prereq_id)
            if record is None:
                status = "No progress record in response"
            elif record.get("done"):
                status = "Completed"
            else:
                status = "Not completed"
            prerequisites.append({
                "id": prereq_id,
                "name": prereq.get("name", f"Achievement {prereq_id}"),
                "status": status,
                "wiki_link": _wiki_link(prereq["name"]) if prereq.get("name") else None,
            })

        line = {
            "id": ident,
            "name": name,
            "type": detail.get("type", "Unknown"),
            "flags": detail.get("flags", []),
            "progress": f"{c['current']:g} / {c['max']:g}",
            "percent": c["percent"],
            "remaining_units": c["remaining"],
            "points_earned": c["points_earned"],
            "points_pending": c["points_pending"],
            "next_tier": _next_tier(detail, c["current"]),
            "initial_priority": priority,
            "description": _strip_tags(detail.get("description", "")),
            "requirement": _readable_requirement(detail, item),
            "prerequisites": prerequisites,
            "rewards": _format_rewards(detail, names),
            "wiki_link": _wiki_link(name),
        }
        if bits is not None:
            line["unregistered_bits_total"] = len(bits)
            line["bits_match_remaining"] = len(bits) == c["remaining"]
            line["empty_entries_ignored"] = empty
            if include_bits:
                shown, omitted = _limit(bits, max_bits)
                line["unregistered_bits"] = shown
                line["bits_omitted"] = omitted
        recommendations.append(line)

    # Totals that used to be the achievement_summary tool (v2.8).
    progress = analysis["progress"]
    completed = sum(1 for p in progress if p.get("done"))
    in_progress = sum(1 for p in progress if _is_partial(p))
    summary = {
        "records": len(progress),
        "completed": completed,
        "in_progress": in_progress,
        "no_progress_recorded": len(progress) - completed - in_progress,
        "note": (
            "These are progress records returned by the API, "
            "not necessarily unique or non-repeatable achievements."
        ),
    }

    extras = {}
    if analysis["hidden"]:
        extras["hidden_unavailable"] = analysis["hidden"]
    if analysis["availability_warning"]:
        extras["availability_warning"] = analysis["availability_warning"]
    return {
        "objective": "Investigate partial achievements with additional details",
        "sorted_by": sort_by,
        "records_analyzed": len(analysis["progress"]),
        "summary": summary,
        "candidates_with_partial_progress": len(analysis["candidates"]),
        "recommendations_returned": len(recommendations),
        **extras,
        "recommendations": recommendations,
        "limitations": [
            "Percent does not estimate completion time.",
            "API may not provide detailed bits for all achievements.",
            "Unregistered bits are not necessarily confirmed pending.",
            "Prerequisites listed are from API details; may not represent all in-game conditions.",
            "Event and festival availability is not checked.",
            "Material costs and efficient routes are not calculated.",
        ],
    }


TEMPORARY_KEYWORDS = ("daily", "weekly", "monthly", "seasonal", "festival", "annual")


@mcp.tool()
def action_plan(
    limit: int = 15,
    sort_by: str = "percent",
    include_location: bool = False,
    max_bits: int = 5,
    hide_unavailable: bool = True,
    compact: bool = True,
) -> dict:
    """Plan from achievement progress: separates identified bits, counters,
    possibly temporary content, and cases needing investigation.
    sort_by: 'percent'|'remaining'|'points'|'points_per_unit'.
    include_location=True: locate pending areas via official API.
    max_bits: bits per achievement (0 = all). compact: bit names only."""
    if not 1 <= limit <= 30:
        return {"error": "limit must be between 1 and 30."}
    if sort_by not in SORT_OPTIONS:
        return {"error": f"sort_by must be one of: {', '.join(SORT_OPTIONS)}."}

    analysis = _analyze_partials(limit, sort_by, hide_unavailable)
    names = _reference_names(_selected_references(analysis, True))

    actions = []
    for c in analysis["selected"]:
        ident = c["id"]
        detail = analysis["details"].get(ident, {})
        item = analysis["by_id"][ident]
        name = detail.get("name", f"Achievement {ident}")
        bits, empty = _described_bits(detail, item, names)

        if bits:
            category = "bits_to_verify"
            next_action = (
                "Check the named bits in-game and confirm which are actually missing."
            )
        elif c["remaining"] <= 20:
            category = "few_units_goal"
            next_action = (
                "Investigate the exact requirement and confirm if it can be completed now."
            )
        else:
            category = "counter_or_goal"
            next_action = (
                "Find an efficient method to accumulate remaining progress; "
                "the API does not report time."
            )

        search_text = " ".join([
            str(detail.get("flags", [])),
            name,
            detail.get("requirement", "") or "",
            detail.get("description", "") or "",
        ]).lower()
        if any(p in search_text for p in TEMPORARY_KEYWORDS):
            category = "verify_availability"
            next_action = (
                "Confirm if the content (festival, event, or cycle) is available "
                "before planning completion."
            )

        shown, omitted = _limit(bits or [], max_bits)

        action = {
            "achievement_id": ident,
            "achievement": name,
            "category": category,
            "progress": f"{c['current']:g} / {c['max']:g}",
            "percent": c["percent"],
            "remaining_units": c["remaining"],
            "points_pending": c["points_pending"],
            "next_tier": _next_tier(detail, c["current"]),
            "bits_to_verify": shown,
            "bits_total": len(bits or []),
            "bits_omitted": omitted,
            "description": _strip_tags(detail.get("description", "")),
            "requirement": _readable_requirement(detail, item),
            "next_action": next_action,
            "wiki_link": _wiki_link(name),
        }
        if bits and include_location:
            action["location"] = _locate_bits(shown)
        if compact:
            action.pop("description", None)
            action["bits_to_verify"] = [e["description"] for e in shown]
            if not omitted:
                action.pop("bits_omitted", None)
            if not bits:
                action.pop("bits_to_verify", None)
                action.pop("bits_total", None)
        actions.append(action)

    by_category = {}
    for action in actions:
        by_category[action["category"]] = by_category.get(action["category"], 0) + 1

    extras = {}
    if analysis["hidden"]:
        extras["hidden_unavailable"] = analysis["hidden"]
    if analysis["availability_warning"]:
        extras["availability_warning"] = analysis["availability_warning"]
    return {
        "objective": "Initial action plan for partial achievements",
        **extras,
        "sorted_by": sort_by,
        "records_analyzed": len(analysis["progress"]),
        "candidates_with_partial_progress": len(analysis["candidates"]),
        "actions_returned": len(actions),
        "summary_by_category": by_category,
        "actions": actions,
        "warnings": [
            "Default sort is by percent, not actual time.",
            "Unregistered bits need in-game confirmation (use missing_achievements).",
            "Does not check routes, costs, or full prerequisites.",
        ],
    }


# ============================================================
# Area lookup (official API: /v2/continents)
# ============================================================

def _normalize(text):
    """Normalize text for comparison (lowercase, single spaces)."""
    return re.sub(r"\s+", " ", (text or "").replace("\u2019", "'").strip().lower())


def _split_area(text):
    """'Metrica Province: Calx's Hideout' -> ('metrica province', "calx's hideout")."""
    if ":" in text:
        map_name, sector = text.split(":", 1)
        return _normalize(map_name), _normalize(sector)
    return None, _normalize(text)


def _compact_floor(floor_id):
    """Fetch and cache a continent floor with sectors and maps."""
    key = ("floor", floor_id)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    floor = gw2_get(f"continents/1/floors/{floor_id}", {"lang": "en"}, authenticated=False)
    sectors, maps = [], []
    for region in (floor.get("regions") or {}).values():
        region_name = region.get("name", "")
        for map_obj in (region.get("maps") or {}).values():
            map_name = map_obj.get("name", "")
            maps.append({
                "map": map_name,
                "region": region_name,
                "min_level": map_obj.get("min_level"),
                "max_level": map_obj.get("max_level"),
                "floor": floor_id,
            })
            raw_sectors = map_obj.get("sectors") or {}
            sector_list = raw_sectors.values() if isinstance(raw_sectors, dict) else raw_sectors
            for sector in sector_list:
                if not isinstance(sector, dict):
                    continue
                sectors.append({
                    "sector": sector.get("name", ""),
                    "map": map_name,
                    "region": region_name,
                    "level": sector.get("level"),
                    "coord": sector.get("coord"),
                    "chat_link": sector.get("chat_link"),
                    "floor": floor_id,
                })
    return _cache_set(key, {"sectors": sectors, "maps": maps}, NAME_TTL)


def _find_areas(queries):
    """queries: list of (normalized_map|None, normalized_sector).
    Returns (results, visited_floors, failed_floors) — failed floors let the
    caller warn that a not-found answer may be partial."""
    results = {q: None for q in queries}
    visited_floors, failed = [], 0
    if not queries:
        return results, visited_floors, failed

    floors = gw2_get("continents/1/floors", authenticated=False)
    floors = [f for f in floors if isinstance(f, int)]
    order = sorted(floors, key=lambda x: (x != 1, x))[:MAX_FLOORS]

    for floor_id in order:
        try:
            floor = _compact_floor(floor_id)
        except GW2Error:
            failed += 1
            continue
        visited_floors.append(floor)
        for query in queries:
            if results[query] is not None:
                continue
            map_name, sector = query
            for s in floor["sectors"]:
                if _normalize(s["sector"]) == sector and (
                    map_name is None or _normalize(s["map"]) == map_name
                ):
                    results[query] = s
                    break
        if all(v is not None for v in results.values()):
            break
    return results, visited_floors, failed


def _format_location(s):
    """Format a sector location for output."""
    return {
        "map": s["map"],
        "region": s["region"],
        "sector": s["sector"],
        "level": s.get("level"),
        "continent_coord": s.get("coord"),
        "chat_link": s.get("chat_link"),
    }


def _locate_bits(bits):
    """Locate up to 40 bit descriptions on the continent map."""
    queries = {}
    for bit in bits[:40]:
        queries[bit["description"]] = _split_area(bit["description"])
    try:
        found, _, _ = _find_areas(list(set(queries.values())))
    except GW2Error as err:
        return {"error": str(err)}
    return {
        description: (_format_location(found[key]) if found.get(key) else None)
        for description, key in queries.items()
    }


@mcp.tool()
def find_area(name: str) -> dict:
    """Find an exploration area via the official API (map, region, level, continent coords).
    Accepts 'Sector' or 'Map: Sector' as shown in exploration achievements."""
    name = (name or "").strip()
    if not name:
        return {"error": "Provide an area name."}

    query = _split_area(name)
    try:
        found, floors, failed = _find_areas([query])
    except GW2Error as err:
        return {"query": name, "error": str(err)}

    location = found[query]
    if location:
        return {
            "query": name,
            "found": True,
            "location": _format_location(location),
            "note": "Continent map coordinates, not in-game map coordinates.",
        }

    target = _normalize(name)
    for floor in floors:
        for map_obj in floor["maps"]:
            if _normalize(map_obj["map"]) == target:
                return {"query": name, "found": True, "type": "map", "location": map_obj}

    out = {
        "query": name,
        "found": False,
        "hint": "Check English spelling (e.g., \"Metrica Province: Calx's Hideout\").",
    }
    if failed:
        out["warning"] = f"{failed} map floor(s) unreadable; the area may exist."
    return out


@mcp.tool()
def pending_areas(achievement_id: int, locate: bool = True) -> dict:
    """List unregistered bits for an achievement (e.g., areas missing from
    an exploration achievement) and, if possible, where they are."""
    progress = {p["id"]: p for p in _account_progress()}
    item = progress.get(achievement_id)
    if item is None:
        return {"error": f"No progress record for achievement {achievement_id}."}

    detail = resolve_names("achievements", [achievement_id]).get(achievement_id)
    if not detail:
        return {"error": f"Achievement {achievement_id} not found in API."}

    pending = _unregistered_bits(detail, item)
    if pending is None:
        return {
            "achievement": detail.get("name"),
            "error": "API does not report per-bit progress for this achievement.",
            "progress": f"{item.get('current')} / {item.get('max')}",
        }

    names = _reference_names((b.get("type"), b.get("id")) for _, b in pending)
    bits, empty = [], 0
    for index, bit in pending:
        description = _describe_bit(bit, names)
        if description:
            bits.append({"index": index, "description": description})
        else:
            empty += 1

    result = {
        "id": achievement_id,
        "achievement": detail.get("name"),
        "progress": f"{item.get('current')} / {item.get('max')}",
        "unregistered_bits_total": len(bits),
        "empty_entries_ignored": empty,
        "pending": [dict(e, location=None) for e in bits],
        "note": "Unregistered bits need in-game confirmation.",
    }
    if locate and bits:
        if len(bits) > 40:
            result["warning"] = "More than 40 pending bits; location disabled."
        else:
            locations = _locate_bits(bits)
            if "error" in locations and len(locations) == 1:
                result["location_warning"] = locations["error"]
            else:
                for p in result["pending"]:
                    p["location"] = locations.get(p["description"])
    return result


# ============================================================
# Prices and account value tools
# ============================================================

def _batch_prices(ids, warnings=None):
    """Fetch Trading Post prices in batches with caching.
    warnings (optional list): one line per unreadable batch — a missing price
    is not always 'no demand'; it can be a failed fetch."""
    unique = list(dict.fromkeys(ids))
    result, missing = {}, []
    for ident in unique:
        value = _cache_get(("price", ident))
        if value is not None:
            result[ident] = value
        else:
            missing.append(ident)

    for start in range(0, len(missing), 200):
        part = missing[start:start + 200]
        try:
            data = gw2_get(
                "commerce/prices", {"ids": ",".join(map(str, part))}, authenticated=False
            )
        except GW2Error as err:
            if warnings is not None:
                warnings.append(f"{len(part)} price records unreadable ({err}).")
            continue
        if not isinstance(data, list):
            continue
        for price in data:
            if isinstance(price, dict) and "id" in price:
                _cache_set(("price", price["id"]), price, PRICE_TTL)
                result[price["id"]] = price
    return result


def _unit_price(price, side):
    """Get unit price from a price object (buys or sells side)."""
    return _int((price.get(side) or {}).get("unit_price"))


@mcp.tool()
def item_info(item_name: str = "", item_id: int = 0) -> dict:
    """Full item card by English name or ID: type, rarity, level, description,
    vendor value, flags, Trading Post prices (buy/sell with order quantities)
    and Wiki link. Single tool for identifying an item and checking what it
    sells for; for the recipe and crafting cost use item_craft_info."""
    if not item_id and not item_name.strip():
        return {"error": "Provide item_id or item_name."}
    if not item_id:
        item_id, error = _resolve_item_input(0, item_name)
        if error:
            return error
    try:
        item = gw2_get(f"items/{item_id}", {"lang": "en"}, authenticated=False)
    except GW2Error as err:
        return {"id": item_id, "error": str(err)}

    out = {
        "id": item_id,
        "name": item.get("name"),
        "type": item.get("type"),
        "subtype": (item.get("details") or {}).get("type"),
        "rarity": item.get("rarity"),
        "level": _int(item.get("level")),
        "vendor_value": format_coins(_int(item.get("vendor_value"))),
        "vendor_value_copper": _int(item.get("vendor_value")),
        "chat_link": item.get("chat_link"),
        "wiki": _wiki_link(item.get("name", str(item_id))),
    }
    if item.get("description"):
        out["description"] = str(item["description"])[:300]
    if item.get("flags"):
        out["flags"] = item["flags"]

    try:
        prices = gw2_get(f"commerce/prices/{item_id}", authenticated=False)
    except GW2Error:
        out["no_tp_price"] = True
        return out
    bid, ask = _unit_price(prices, "buys"), _unit_price(prices, "sells")
    out["tp_buy_now"] = format_coins(ask)       # menor oferta de venda: o que se paga
    out["tp_sell_now"] = format_coins(bid)      # maior oferta: o que se recebe
    out["tp_buy_now_copper"] = ask
    out["tp_sell_now_copper"] = bid
    out["tp_buy_now_quantity"] = (prices.get("sells") or {}).get("quantity")
    out["tp_sell_now_quantity"] = (prices.get("buys") or {}).get("quantity")
    return out


@mcp.tool()
def item_prices(item_ids: list[int]) -> list:
    """Get names and prices for multiple items at once (up to 200 IDs)."""
    ids = list(dict.fromkeys(item_ids or []))[:200]
    if not ids:
        return []
    names = resolve_names("items", ids)
    prices = _batch_prices(ids)
    result = []
    for ident in ids:
        name = names.get(ident, {}).get("name", f"Item {ident}")
        price = prices.get(ident)
        if not price:
            result.append({"id": ident, "name": name, "no_tp_price": True})
            continue
        buy, sell = _unit_price(price, "buys"), _unit_price(price, "sells")
        result.append({
            "id": ident,
            "name": name,
            "buy_price_per_unit": format_coins(buy),
            "sell_price_per_unit": format_coins(sell),
            "buy_price_copper": buy,
            "sell_price_copper": sell,
        })
    return result


@mcp.tool()
def tp_listings(item_id: int = 0, item_name: str = "", depth: int = 5) -> dict:
    """Trading Post order book depth for one item: top bids and asks with
    quantity, listings, and price per level. Use before bulk buys (e.g., 250 T6).
    item_id or item_name (English). depth: price levels per side (1-20)."""
    if not 1 <= depth <= 20:
        return {"error": "depth must be between 1 and 20."}
    if not item_id and item_name:
        exact, candidates = _find_item(item_name)
        if not exact:
            return {"error": "Item not found by that name.", "candidates": candidates[:10]}
        item_id = exact[0]
    if not item_id:
        return {"error": "Provide item_id or item_name."}

    name = resolve_names("items", [item_id]).get(item_id, {}).get("name", f"Item {item_id}")
    try:
        data = gw2_get(f"commerce/listings/{item_id}", authenticated=False)
    except GW2Error as err:
        return {"id": item_id, "name": name, "error": str(err),
                "warning": "Item may not be listed on the Trading Post."}

    def side(rows, highest_first):
        rows = sorted(rows or [], key=lambda r: _int(r.get("unit_price")),
                      reverse=highest_first)[:depth]
        return [{
            "price": format_coins(_int(r.get("unit_price"))),
            "price_copper": _int(r.get("unit_price")),
            "quantity": _int(r.get("quantity")),
            "listings": _int(r.get("listing_count")),
        } for r in rows]

    buys, sells = side(data.get("buys"), True), side(data.get("sells"), False)
    spread = None
    if buys and sells:
        spread = sells[0]["price_copper"] - buys[0]["price_copper"]
    return {
        "id": item_id,
        "name": name,
        "buys": buys,   # buy orders (what buyers offer)
        "sells": sells,  # sell listings (what sellers ask)
        "spread_copper": spread,
        "spread": format_coins(spread) if spread is not None else None,
        "note": "Depth helps plan bulk buys: thin books move the price.",
    }


@mcp.tool()
def account_value(limit: int = 20, include_bank: bool = True) -> dict:
    """Estimate gold value of materials (and bank) at current Trading Post prices.
    Requires account, inventories, and (for wallet gold) wallet permissions."""
    if not 1 <= limit <= 100:
        return {"error": "limit must be between 1 and 100."}

    warnings = []
    stock = {}  # id -> {"materials": n, "bank": n}
    for m in gw2_get("account/materials"):
        if m.get("count", 0) > 0:
            stock.setdefault(m["id"], {"materials": 0, "bank": 0})["materials"] += m["count"]

    if include_bank:
        try:
            for slot in gw2_get("account/bank"):
                if isinstance(slot, dict) and slot.get("id"):
                    entry = stock.setdefault(slot["id"], {"materials": 0, "bank": 0})
                    entry["bank"] += _int(slot.get("count"), 1)
        except GW2Error as err:
            warnings.append(f"Bank not included: {err}")

    prices = _batch_prices(list(stock), warnings=warnings)

    lines, no_price = [], 0
    total_listed = total_immediate = 0
    for ident, qty in stock.items():
        price = prices.get(ident)
        if not price:
            no_price += 1
            continue
        total = qty["materials"] + qty["bank"]
        sell, buy = _unit_price(price, "sells"), _unit_price(price, "buys")
        listed_value = int(sell * total * (1 - TP_LISTED_FEE))
        immediate_value = int(buy * total * (1 - TP_IMMEDIATE_FEE))
        total_listed += listed_value
        total_immediate += immediate_value
        lines.append({
            "id": ident,
            "quantity": total,
            "sell_price_unit_copper": sell,
            "listed_net_value_copper": listed_value,
            "immediate_net_value_copper": immediate_value,
        })

    lines.sort(key=lambda x: -x["listed_net_value_copper"])
    top = lines[:limit]
    names = resolve_names("items", [x["id"] for x in top])
    for x in top:
        x["name"] = names.get(x["id"], {}).get("name", f"Item {x['id']}")
        x["listed_net_value"] = format_coins(x["listed_net_value_copper"])
        x["immediate_net_value"] = format_coins(x["immediate_net_value_copper"])

    wallet_gold = None
    try:
        for currency in gw2_get("account/wallet"):
            if currency.get("id") == 1:
                wallet_gold = format_coins(currency.get("value", 0))
    except GW2Error as err:
        warnings.append(f"Wallet not included: {err}")

    return {
        "queried_at_utc": _utc_now().isoformat(),
        "items_with_price": len(lines),
        "items_without_tp_price": no_price,
        "total_listed_net_value": format_coins(total_listed),
        "total_immediate_net_value": format_coins(total_immediate),
        "wallet_coins": wallet_gold,
        "top_values": top,
        "warnings": warnings + [
            "'Listed' = sell at current sell price, 15% total fee; "
            "'immediate' = sell to buy orders, 10% fee.",
            "Does not include character inventories, shared inventory, "
            "equipment, or items without TP price.",
            "Prices are instant and fees are approximate.",
        ],
    }


# ============================================================
# Wiki tools
# ============================================================

def _wiki_request(url, params):
    """Make a cached request to the GW2 Wiki API."""
    key = ("wiki", url, tuple(sorted((params or {}).items())))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    response = _request(
        url,
        params=params,
        headers={"Accept": "application/json", "User-Agent": WIKI_UA},
    )
    if response.status_code == 403:
        raise GW2Error(
            "Wiki blocked the request (HTTP 403). May be temporary "
            "automated access limitation."
        )
    if response.status_code == 404:
        raise GW2Error("Page not found on Wiki (HTTP 404).")
    if response.status_code != 200:
        raise GW2Error(f"Wiki returned HTTP {response.status_code}.")
    return _cache_set(key, response, WIKI_TTL)


@mcp.tool()
def search_wiki(term: str) -> dict:
    """Search the Guild Wars 2 Wiki and return relevant pages and snippets."""
    import html as html_lib

    term = (term or "").strip()
    if not term:
        return {"error": "Provide a search term."}

    try:
        response = _wiki_request(
            f"{WIKI_BASE}/api.php",
            {
                "action": "query",
                "list": "search",
                "srsearch": term,
                "srlimit": 8,
                "format": "json",
            },
        )
        results = response.json().get("query", {}).get("search", [])
    except GW2Error as err:
        return {
            "term": term,
            "error": str(err),
            "hint": "Try wiki_details with the exact page name.",
        }
    except ValueError:
        return {"term": term, "error": "Wiki returned an invalid response."}

    pages = []
    for page in results:
        title = page["title"]
        pages.append({
            "title": title,
            "url": _wiki_link(title),
            "snippet": html_lib.unescape(
                re.sub(r"<[^>]+>", "", page.get("snippet", ""))
            ),
        })
    return {"term": term, "results": pages, "wiki": WIKI_BASE + "/"}


def _clean_wikitext(text):
    """Remove wiki markup (comments, links) from raw wikitext."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    return text.strip()


def _raw_wikitext(title, follow_redirect=True):
    """Fetch raw wikitext for a wiki page, following redirects."""
    url = f"{WIKI_BASE}/wiki/" + quote(title.replace(" ", "_"), safe="()'_-,:!")
    text = _wiki_request(url, {"action": "raw"}).text or ""
    if follow_redirect and text.lstrip().upper().startswith("#REDIRECT"):
        target = re.search(r"\[\[([^\]|#]+)", text)
        if target:
            return _raw_wikitext(target.group(1).strip(), follow_redirect=False)
    return title, _clean_wikitext(text)


@mcp.tool()
def wiki_details(page: str) -> dict:
    """Get content from a Guild Wars 2 Wiki page.
    Returns explanatory text and page link for investigating locations,
    requirements, and achievement bits. Falls back to raw wikitext if API is blocked."""
    page = (page or "").strip()
    if not page:
        return {"error": "Provide a wiki page name."}
    if len(page) > 200:
        return {"error": "Page name must be 200 characters or less."}

    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "extracts|info",
        "explaintext": "1",
        "exsectionformat": "plain",
        "inprop": "url",
        "redirects": "1",
        "titles": page,
    }

    api_error = None
    try:
        data = _wiki_request(f"{WIKI_BASE}/api.php", params).json()
        pages = data.get("query", {}).get("pages", [])
        if not pages:
            return {"error": "Wiki returned no pages.", "searched_page": page}
        result = pages[0]
        if result.get("missing"):
            return {
                "error": "No page found with that name.",
                "searched_page": page,
                "suggestion": (
                    "Try the English name shown in the achievement "
                    "or use search_wiki to find the correct page."
                ),
            }
        title = result.get("title", page)
        text = (result.get("extract") or "").strip()
        return {
            "searched_page": page,
            "title_found": title,
            "url": result.get("fullurl", _wiki_link(title)),
            "source": "wiki_api",
            "text": text[:12000],
            "text_truncated": len(text) > 12000,
            "guidance": (
                "Use the text to identify location, access, requirements, and "
                "possible strategies. If the page does not explain the required "
                "bit, do not invent a solution."
            ),
        }
    except ValueError:
        api_error = "Wiki returned an invalid response."
    except GW2Error as err:
        api_error = str(err)

    # Fallback: raw wikitext of the page
    try:
        title, text = _raw_wikitext(page)
    except GW2Error as err:
        return {
            "error": "Could not query the Wiki.",
            "api_detail": api_error,
            "fallback_detail": str(err),
            "searched_page": page,
        }
    return {
        "searched_page": page,
        "title_found": title,
        "url": _wiki_link(title),
        "source": "raw_wikitext",
        "warning": (
            f"Wiki API failed ({api_error}); this is raw page text "
            "with template markup ({{...}}) not interpreted."
        ),
        "text": text[:12000],
        "text_truncated": len(text) > 12000,
    }


def _wiki_recipe(title):
    """Parse the {{Recipe}} template from a Wiki page: returns
    {"ingredients": [(name, qty)], "output_count": int} or None when the page
    has no recipe (Wiki down also becomes None; the caller warns)."""
    key = ("wiki_recipe", _normalize(title))
    cached = _cache_get(key)
    if cached is not None:
        return cached or None
    try:
        _, text = _raw_wikitext(title)
    except GW2Error:
        return None

    result = None
    # Template opens with {{Recipe|... and closes by itself; the block may have
    # nested templates, so we cut at the first isolated }}.
    match = re.search(r"\{\{[Rr]ecipe(?:\s*\n)?\s*\|(.*?)\n\}\}", text, re.S)
    if match:
        block = match.group(1)
        ingredients = []
        for m in re.finditer(r"\|\s*ingredient\d+\s*=\s*(\d+)\s+([^\n|]+)", block):
            qty = int(m.group(1))
            raw = m.group(2).strip()
            # [[A|B]] -> A (target = item name); [[A]] -> A
            link = re.match(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", raw)
            if link:
                raw = link.group(1).strip()
            if raw:
                ingredients.append((raw, qty))
        if ingredients:
            quantity = re.search(r"\|\s*quantity\s*=\s*(\d+)", block)
            result = {
                "ingredients": ingredients,
                "output_count": int(quantity.group(1)) if quantity else 1,
            }
    # {} (no recipe) is cached; None (Wiki failed) is not.
    _cache_set(key, result or {}, WIKI_TTL)
    return result


# ============================================================
# Event timers
# ============================================================

def _resolve_timezone(name):
    """Return (tzinfo, warning). Falls back to fixed UTC-03:00 for Sao Paulo without tzdata."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), None
    except Exception:
        if name == "America/Sao_Paulo":
            return (
                timezone(timedelta(hours=-3), "BRT"),
                "Timezone database (tzdata) unavailable; using fixed UTC-03:00 "
                "(Brazil has no DST since 2019). "
                "To fix: pip install tzdata.",
            )
        raise GW2Error(
            f"Timezone '{name}' not found. Install tzdata "
            "(pip install tzdata) or use another IANA name."
        )


def _fetch_timer_data():
    """Fetch event timer data from the GW2 Wiki."""
    response = _wiki_request(
        f"{WIKI_BASE}/wiki/Widget:Event_timer/data.json", {"action": "raw"}
    )
    try:
        return response.json()
    except ValueError:
        raise GW2Error("Timer data came in an invalid format.")


def _generate_phases(sequences, limit):
    """Apply partial sequence then repeat pattern, starting from 00:00 UTC.
    Times are minutes since midnight UTC."""
    if not isinstance(sequences, dict):
        return []
    partial = sequences.get("partial") or []
    pattern = sequences.get("pattern") or []
    if not isinstance(partial, list):
        partial = []
    if not isinstance(pattern, list):
        pattern = []

    phases = []
    cursor = 0

    def add_phases(phase_list):
        nonlocal cursor
        for item in phase_list:
            if not isinstance(item, dict):
                continue
            try:
                segment = str(int(item["r"]))
                duration = int(item["d"])
            except (KeyError, TypeError, ValueError):
                continue  # malformed segment: skip only it, the rest of the timer stays
            if duration <= 0:
                continue
            phases.append({"start": cursor, "end": cursor + duration, "segment": segment})
            cursor += duration

    add_phases(partial)
    if pattern:
        while cursor <= limit:
            before = cursor
            add_phases(pattern)
            if cursor <= before:
                break
    return phases


def _calculate_timers(data, now, horizon_minutes, tz, filter_=""):
    """Calculate active and upcoming event phases from timer data."""
    events = data.get("events", {}) if isinstance(data, dict) else {}
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_since_midnight = int((now - day_start).total_seconds() // 60)
    limit_minutes = minutes_since_midnight + horizon_minutes + 1
    window_end = now + timedelta(minutes=horizon_minutes)
    filter_norm = (filter_ or "").strip().lower()

    active, upcoming, seen = [], [], set()
    skipped = 0

    def phase_link(segment, phase_name):
        return _wiki_link(segment.get("link") or phase_name)

    for event_id, event in events.items():
        if not isinstance(event, dict):
            skipped += 1
            continue
        phases = _generate_phases(event.get("sequences"), limit_minutes)
        if not phases:
            skipped += 1
            continue

        segments = event.get("segments")
        segments = segments if isinstance(segments, dict) else {}
        event_name = event.get("name") or event_id
        category = event.get("category", "")

        for phase in phases:
            segment = segments.get(phase["segment"], {})
            if not isinstance(segment, dict):
                continue
            phase_name = segment.get("name", "")
            if not phase_name:
                continue
            if filter_norm and filter_norm not in " ".join(
                [event_name, category, phase_name]
            ).lower():
                continue

            start_dt = day_start + timedelta(minutes=phase["start"])
            end_dt = day_start + timedelta(minutes=phase["end"])
            key = (event_name, phase_name, phase["start"])
            if key in seen:
                continue

            if start_dt <= now < end_dt:
                seen.add(key)
                active.append({
                    "event": event_name,
                    "category": category,
                    "phase": phase_name,
                    "start_utc": start_dt.isoformat(),
                    "end_utc": end_dt.isoformat(),
                    "end_local": end_dt.astimezone(tz).isoformat(),
                    "wiki_link": phase_link(segment, phase_name),
                })
            elif now < start_dt <= window_end:
                seen.add(key)
                upcoming.append({
                    "event": event_name,
                    "category": category,
                    "phase": phase_name,
                    "start_utc": start_dt.isoformat(),
                    "start_local": start_dt.astimezone(tz).isoformat(),
                    "minutes_until_start": math.ceil((start_dt - now).total_seconds() / 60),
                    "wiki_link": phase_link(segment, phase_name),
                })

    active.sort(key=lambda x: (x["end_utc"], x["event"]))
    upcoming.sort(key=lambda x: (x["start_utc"], x["event"]))
    return active, upcoming, skipped


@mcp.tool()
def event_timers(
    horizon_minutes: int = 60,
    timezone_name: str = "America/Sao_Paulo",
    filter_: str = "",
) -> dict:
    """Calculate current phases and upcoming events from Wiki schedules.
    timezone_name: IANA timezone for display (default America/Sao_Paulo).
    filter_: optional text to filter by event, category, or phase."""
    if not 0 <= horizon_minutes <= 1440:
        return {"error": "horizon_minutes must be between 0 and 1440."}

    try:
        tz, tz_warning = _resolve_timezone(timezone_name)
        data = _fetch_timer_data()
    except GW2Error as err:
        return {"error": str(err)}

    now = _utc_now()
    active, upcoming, skipped = _calculate_timers(data, now, horizon_minutes, tz, filter_)

    result = {
        "queried_at_utc": now.isoformat(),
        "display_timezone": timezone_name,
        "horizon_minutes": horizon_minutes,
        "filter": filter_ or None,
        "active_phases": active,
        "upcoming_events": upcoming,
        "upcoming_count": len(upcoming),
        "events_without_usable_pattern": skipped,
        "source": f"{WIKI_BASE}/wiki/Widget:Event_timer/data.json",
        "warning": (
            "Schedules based on Wiki-published sequences. "
            "Do not confirm an event is live on the game server."
        ),
    }
    if tz_warning:
        result["timezone_warning"] = tz_warning
    return result


# ============================================================
# Unlocks and missing achievements
# ============================================================

UNLOCK_ENDPOINTS = {
    "skins": "account/skins",
    "dyes": "account/dyes",
    "minis": "account/minis",
    "mounts": "account/mounts/types",
    "mount_skins": "account/mounts/skins",
    "titles": "account/titles",
    "outfits": "account/outfits",
    "gliders": "account/gliders",
    "finishers": "account/finishers",
    "mailcarriers": "account/mailcarriers",
    "novelties": "account/novelties",
    "recipes": "account/recipes",
    "legendary_armory": "account/legendaryarmory",
}

# Public list endpoints with all existing IDs (to calculate the total).
PUBLIC_TOTALS = {
    "skins": "skins",
    "dyes": "colors",
    "minis": "minis",
    "mounts": "mounts/types",
    "mount_skins": "mounts/skins",
    "titles": "titles",
    "outfits": "outfits",
    "gliders": "gliders",
    "finishers": "finishers",
    "mailcarriers": "mailcarriers",
    "novelties": "novelties",
}


def _unlocked_ids(endpoint):
    """Set of unlocked IDs in the account (lists of IDs or objects)."""
    key = ("unlocked", endpoint)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = gw2_get(endpoint)
    if not isinstance(data, list):
        raise GW2Error(f"The API returned an unexpected format at {endpoint}.")
    ids = set()
    for d in data:
        if isinstance(d, dict) and "id" in d:
            ids.add(d["id"])
        elif isinstance(d, int):
            ids.add(d)
    return _cache_set(key, ids, UNLOCK_TTL)


def _total_in_game(public_endpoint):
    """Get the total count of items in the game from a public endpoint."""
    key = ("total_in_game", public_endpoint)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = gw2_get(public_endpoint, authenticated=False)
    if not isinstance(data, list):
        raise GW2Error(f"The API returned an unexpected format at {public_endpoint}.")
    return _cache_set(key, len(data), NAME_TTL)


@mcp.tool()
def unlocks(include_totals: bool = True) -> dict:
    """Summarize account unlocks: skins, dyes, minis, mounts, titles,
    outfits, gliders, finishers, and more. Requires unlocks permission.
    include_totals=True compares against the total in-game count."""
    tasks = {
        f"u:{name}": (lambda e=endpoint: len(_unlocked_ids(e)))
        for name, endpoint in UNLOCK_ENDPOINTS.items()
    }
    if include_totals:
        tasks.update({
            f"t:{name}": (lambda e=public: _total_in_game(e))
            for name, public in PUBLIC_TOTALS.items()
        })
    results = _parallel(tasks)

    categories, errors = {}, {}
    for name in UNLOCK_ENDPOINTS:
        ok, value = results[f"u:{name}"]
        if not ok:
            errors[name] = value
            continue
        line = {"unlocked": value}
        total = results.get(f"t:{name}")
        if total and total[0]:
            line["total_in_game"] = total[1]
            if total[1]:
                line["percent"] = round(value / total[1] * 100, 1)
        categories[name] = line
    output = {"categories": categories}
    if errors:
        output["errors"] = errors
    return output


UNLOCK_TYPE_MAP = {
    "Skin": "account/skins",
    "Minipet": "account/minis",
    "Title": "account/titles",
}


@mcp.tool()
def missing_achievements(achievement_id: int, max_bits: int = 40) -> dict:
    """Show what an achievement is actually missing by crossing bits with
    account unlocks (skins, minis, titles). Catches items already unlocked
    that the achievement hasn't registered yet.
    Requires progression and, for crossing, unlocks."""
    detail = resolve_names("achievements", [achievement_id]).get(achievement_id)
    if not detail:
        return {"error": f"Achievement {achievement_id} not found."}

    item = next(
        (p for p in _account_progress() if p["id"] == achievement_id),
        {"id": achievement_id, "done": False, "bits": []},
    )
    base = {
        "achievement_id": achievement_id,
        "achievement": detail.get("name"),
        "requirement": _readable_requirement(detail, item),
        "progress": (
            f"{item.get('current')} / {item.get('max')}"
            if item.get("max") is not None else None
        ),
        "wiki_link": _wiki_link(detail.get("name") or str(achievement_id)),
    }
    if item.get("done"):
        return {**base, "completed": True}

    bits = detail.get("bits")
    if not isinstance(bits, list) or not bits:
        return {
            **base,
            "warning": "This achievement has no individual bits in the API; "
                       "use the numeric progress above.",
        }

    registered = set(item.get("bits") or [])
    pending = [
        (i, b) for i, b in enumerate(bits) if i not in registered and isinstance(b, dict)
    ]
    names = _reference_names((b.get("type"), b.get("id")) for _, b in pending)

    unlocked, errors = {}, {}
    for type_, endpoint in UNLOCK_TYPE_MAP.items():
        if any(b.get("type") == type_ for _, b in pending):
            try:
                unlocked[type_] = _unlocked_ids(endpoint)
            except GW2Error as err:
                errors[type_] = str(err)

    missing, already_have, unchecked, empty = [], [], [], 0
    for index, bit in pending:
        description = _describe_bit(bit, names)
        if not description:
            empty += 1
            continue
        line = {"index": index, "type": bit.get("type"), "description": description}
        type_ = bit.get("type")
        if type_ in unlocked:
            (already_have if bit.get("id") in unlocked[type_] else missing).append(line)
        else:
            unchecked.append(line)

    missing_shown, missing_omitted = _limit(missing, max_bits)
    output = {
        **base,
        "bits_total": len(bits),
        "bits_registered": len(registered),
        "actually_missing": missing_shown,
        "actually_missing_total": len(missing),
        "unlocked_but_unregistered": already_have,
        "unchecked": _limit(unchecked, max_bits)[0],
        "unchecked_total": len(unchecked),
    }
    if missing_omitted:
        output["actually_missing_omitted"] = missing_omitted
    if empty:
        output["empty_entries_ignored"] = empty
    if errors:
        output["crossing_errors"] = errors
        output["warning"] = (
            "Without crossing, all unregistered bits show as "
            "'unchecked'. Check unlocks permission in diagnostics."
        )
    return output


# ============================================================
# Wizard's Vault and daily routine
# ============================================================

VAULT_SECTIONS = ("daily", "weekly", "special")


def _sum_by(lines, key, value):
    """Sum values grouped by a key."""
    result = {}
    for line in lines:
        result[line.get(key)] = result.get(line.get(key), 0) + line.get(value, 0)
    return result


def _vault_section(name, track=""):
    """Fetch and parse a Wizard's Vault section."""
    data = gw2_get(f"account/wizardsvault/{name}")
    if not isinstance(data, dict):
        raise GW2Error(f"Unexpected format in Wizard's Vault ({name}).")
    objectives = []
    for o in data.get("objectives") or []:
        if not isinstance(o, dict):
            continue
        current, total = _int(o.get("progress_current")), _int(o.get("progress_complete"))
        objectives.append({
            "id": o.get("id"),
            "title": o.get("title"),
            "track": o.get("track"),
            "acclaim": _int(o.get("acclaim")),
            "progress": f"{current} / {total}" if total else None,
            "completed": total > 0 and current >= total,
            "claimed": bool(o.get("claimed")),
        })
    if track:
        objectives = [
            o for o in objectives if (o["track"] or "").lower() == track.lower()
        ]
    pending = [o for o in objectives if not o["completed"]]
    claimable = [o for o in objectives if o["completed"] and not o["claimed"]]
    return {
        "meta": {
            "progress": f"{_int(data.get('meta_progress_current'))} / "
                         f"{_int(data.get('meta_progress_complete'))}",
            "astral_reward": _int(data.get("meta_reward_astral")),
            "claimed": bool(data.get("meta_reward_claimed")),
        },
        "objectives_total": len(objectives),
        "objectives_completed": len(objectives) - len(pending),
        "pending_acclaim": sum(o["acclaim"] for o in pending),
        "pending_acclaim_by_track": _sum_by(pending, "track", "acclaim"),
        "pending": pending,
        "claimable": claimable,
    }


@mcp.tool()
def wizards_vault(section: str = "", track: str = "") -> dict:
    """Show Wizard's Vault: daily, weekly, and special objectives, what's
    pending, and what can be claimed. Requires progression permission.
    section: 'daily', 'weekly', or 'special' (empty = all).
    track: 'PvE', 'PvP', or 'WvW' to filter objectives (empty = all)."""
    section = (section or "").strip().lower()
    if section and section not in VAULT_SECTIONS:
        return {"error": f"section must be one of {', '.join(VAULT_SECTIONS)} or empty."}
    result, errors = {}, {}
    for name in ([section] if section else VAULT_SECTIONS):
        try:
            result[name] = _vault_section(name, track)
        except GW2Error as err:
            errors[name] = str(err)
    output = {"queried_at_utc": _utc_now().isoformat(), **result}
    if errors:
        output["errors"] = errors
    return output


NOISY_EVENTS = ("day and night",)


def _without_noise(events, limit=6):
    """Filter out noisy events like 'day and night'."""
    useful = [
        e for e in events or []
        if not any(r in str(e.get("event", "")).lower() for r in NOISY_EVENTS)
    ]
    return useful[:limit]


def _objective_line(o):
    """Format a vault objective as a short text line."""
    counter = f"{o['progress']}, " if o.get("progress") else ""
    return f"[{o.get('track')}] {o.get('title')} ({counter}{o.get('acclaim')} acclaim)"


@mcp.tool()
def what_to_do_today(
    horizon_minutes: int = 30, timezone_name: str = "America/Sao_Paulo", track: str = ""
) -> dict:
    """Daily summary in one call: pending Wizard's Vault, what's been done today,
    upcoming events, best-value achievements, and spare mastery points.
    track: 'PvE', 'PvP', or 'WvW' to filter Vault objectives."""
    def vault(name):
        section = _vault_section(name, track)
        return {
            "pending": [_objective_line(o) for o in section["pending"]],
            "claimable": [o["title"] for o in section["claimable"]],
            "meta": section["meta"],
        }

    def mastery():
        points = (gw2_get("account/mastery/points") or {}).get("totals") or []
        spare = {
            p.get("region"): _int(p.get("earned")) - _int(p.get("spent"))
            for p in points if isinstance(p, dict)
        }
        return {r: n for r, n in spare.items() if n > 0}

    def achievements():
        analysis = _analyze_partials(5, "points_per_unit", hide_unavailable=True)
        return [
            {
                "id": c["id"],
                "name": analysis["details"].get(c["id"], {}).get("name", f"Achievement {c['id']}"),
                "progress": f"{c['current']:g} / {c['max']:g}",
                "remaining": c["remaining"],
                "points_pending": c["points_pending"],
            }
            for c in analysis["selected"]
        ]

    def events():
        timers = event_timers(horizon_minutes=horizon_minutes, timezone_name=timezone_name)
        if "error" in timers:
            raise GW2Error(timers["error"])
        return {
            "active": _without_noise(timers.get("active_phases")),
            "upcoming": _without_noise(timers.get("upcoming_events")),
        }

    r = _parallel({
        "vault_daily": lambda: vault("daily"),
        "vault_weekly": lambda: vault("weekly"),
        "done_today": daily_weekly_progress,
        "mastery": mastery,
        "achievements": achievements,
        "events": events,
    })

    output, errors = {"queried_at_utc": _utc_now().isoformat()}, {}
    output["wizards_vault"] = {
        name: r[f"vault_{name}"][1] for name in ("daily", "weekly") if r[f"vault_{name}"][0]
    }
    for name in ("daily", "weekly"):
        if not r[f"vault_{name}"][0]:
            errors[f"vault_{name}"] = r[f"vault_{name}"][1]

    ok, value = r["done_today"]
    if ok:
        done = {k: v for k, v in value.items() if k != "queried_at_utc" and v}
        output["done_today"] = done or "nothing recorded today"
    else:
        errors["done_today"] = value
    for key, dest in (
        ("mastery", "spare_mastery_points"),
        ("achievements", "suggested_achievements"),
        ("events", "events"),
    ):
        ok, value = r[key]
        if ok:
            output[dest] = value
        else:
            errors[key] = value
    if errors:
        output["errors"] = errors
    return output


# ============================================================
# Equipment tools
# ============================================================

RARITIES = ["Junk", "Basic", "Fine", "Masterwork", "Rare", "Exotic", "Ascended", "Legendary"]
GATHERING_SLOTS = {"Sickle", "Axe", "Pick", "FishingRod", "FishingBait", "FishingLure", "Sickle2"}
SLOTS_WITHOUT_RUNE = {
    "Backpack", "Accessory1", "Accessory2", "Amulet", "Ring1", "Ring2",
}

OPTIONAL_SLOTS = {"HelmAquatic", "WeaponAquaticA", "WeaponAquaticB", "Relic"}


def _character_names(character):
    """Get character names, optionally filtered by name."""
    names = [c if isinstance(c, str) else (c or {}).get("name") for c in gw2_get("characters")]
    names = [n for n in names if n]
    if not character:
        return names
    found = [n for n in names if n.lower() == character.strip().lower()]
    return found


def _item_stat_id(item_obj):
    """Get fixed stat ID (infix_upgrade) when no stats are selected."""
    return ((item_obj.get("details") or {}).get("infix_upgrade") or {}).get("id")


@mcp.tool()
def equipment(character: str = "", detailed: bool = False) -> dict:
    """Review equipment: rarity, stats, runes, sigils, infusions; flags pieces
    below Ascended or missing rune/sigil. Requires characters (builds sometimes).
    character: name (empty = all). detailed=True: each piece as object, not line."""
    names = _character_names(character)
    if not names:
        return {"error": f"Character '{character}' not found."}

    fetches = _parallel({
        name: (lambda n=name: gw2_get(f"characters/{quote(n, safe='')}/equipment"))
        for name in names
    })
    raws, errors = {}, {}
    for name, (ok, value) in fetches.items():
        if ok:
            raws[name] = (value or {}).get("equipment") or []
        else:
            errors[name] = value

    item_ids = []
    for pieces in raws.values():
        for p in pieces:
            if isinstance(p, dict):
                item_ids.append(p.get("id"))
                item_ids.extend(p.get("upgrades") or [])
                item_ids.extend(p.get("infusions") or [])
    items = resolve_names("items", item_ids)

    stat_ids = []
    for pieces in raws.values():
        for p in pieces:
            if not isinstance(p, dict):
                continue
            chosen = p.get("stats") if isinstance(p.get("stats"), dict) else {}
            stat_ids.append(chosen.get("id") or _item_stat_id(items.get(p.get("id"), {})))
    stat_names = resolve_names("itemstats", [i for i in stat_ids if i])

    def item_name(ident):
        return items.get(ident, {}).get("name", f"Item {ident}")

    result = {}
    for name, pieces in raws.items():
        lines, by_rarity = [], {}
        below, optional, no_rune = [], [], []
        for p in pieces:
            if not isinstance(p, dict):
                continue
            slot = p.get("slot")
            obj = items.get(p.get("id"), {})
            rarity = obj.get("rarity")
            chosen = p.get("stats") if isinstance(p.get("stats"), dict) else {}
            stat_id = chosen.get("id") or _item_stat_id(obj)
            stat = stat_names.get(stat_id, {}).get("name") if stat_id else None
            extras = [item_name(u) for u in p.get("upgrades") or []] + [
                item_name(i) for i in p.get("infusions") or []
            ]
            if detailed:
                lines.append({
                    "slot": slot, "item": item_name(p.get("id")), "rarity": rarity,
                    "level": obj.get("level"), "stats": stat,
                    "upgrades": [item_name(u) for u in p.get("upgrades") or []],
                    "infusions": [item_name(i) for i in p.get("infusions") or []],
                    "binding": p.get("binding"),
                })
            else:
                label = ", ".join(x for x in (rarity, stat) if x)
                line = f"{slot}: {item_name(p.get('id'))} [{label}]"
                lines.append(line + (" + " + ", ".join(extras) if extras else ""))
            if rarity:
                by_rarity[rarity] = by_rarity.get(rarity, 0) + 1
            if slot in GATHERING_SLOTS:
                continue
            not_ascended = (
                rarity in RARITIES
                and RARITIES.index(rarity) < RARITIES.index("Ascended")
            )
            no_upgrade = (
                obj.get("type") in ("Armor", "Weapon")
                and slot not in SLOTS_WITHOUT_RUNE
                and not p.get("upgrades")
            )
            piece = f"{slot}: {item_name(p.get('id'))}"
            with_rarity = f"{piece} ({rarity})"
            if slot in OPTIONAL_SLOTS:
                if not_ascended:
                    optional.append(with_rarity)
                continue
            if not_ascended:
                below.append(with_rarity)
            if no_upgrade:
                no_rune.append(piece)
        result[name] = {
            "pieces": lines,
            "count_by_rarity": by_rarity,
            "below_ascended": below,
            "optional_below_ascended": optional,
            "missing_rune_or_sigil": no_rune,
        }

    output = {"characters": result}
    if errors:
        output["errors"] = errors
    return output


# ---------------------------------------------------------------------------
# Trading Post
# ---------------------------------------------------------------------------

def _fetch_transactions(endpoint, limit):
    """Fetch transactions from a Trading Post endpoint."""
    data = gw2_get(endpoint)
    if not isinstance(data, list):
        raise GW2Error(f"Unexpected format at {endpoint}.")
    return [d for d in data if isinstance(d, dict)][:limit if limit > 0 else None]


@mcp.tool()
def tp_transactions(limit: int = 20) -> dict:
    """Show open Trading Post orders (buys and sells) compared to market,
    delivery box, and recent history. Requires tradingpost permission."""
    output, errors = {"queried_at_utc": _utc_now().isoformat()}, {}
    requests = {
        "active_buys": "commerce/transactions/current/buys",
        "active_sells": "commerce/transactions/current/sells",
        "history_buys": "commerce/transactions/history/buys",
        "history_sells": "commerce/transactions/history/sells",
    }
    responses = _parallel({
        key: (lambda e=endpoint: _fetch_transactions(e, limit))
        for key, endpoint in requests.items()
    })
    sections = {}
    for key, (ok, value) in responses.items():
        if ok:
            sections[key] = value
        else:
            errors[key] = value

    ids = [t.get("item_id") for lst in sections.values() for t in lst]
    names = resolve_names("items", ids)
    prices = _batch_prices([i for i in ids if isinstance(i, int)]) if ids else {}

    def base(t):
        ident = t.get("item_id")
        qty, price = _int(t.get("quantity")), _int(t.get("price"))
        return ident, qty, price, {
            "item_id": ident,
            "item": names.get(ident, {}).get("name", f"Item {ident}"),
            "quantity": qty,
            "unit_price": format_coins(price),
            "total": format_coins(price * qty),
            "created_at": t.get("created"),
        }

    for key in ("active_buys", "active_sells"):
        if key not in sections:
            continue
        lines, total = [], 0
        for t in sections[key]:
            ident, qty, price, line = base(t)
            market = prices.get(ident)
            if market:
                if key == "active_buys":
                    top = _unit_price(market, "buys")
                    line["highest_bid_now"] = format_coins(top)
                    line["outbid"] = price < top
                else:
                    floor = _unit_price(market, "sells")
                    line["lowest_ask_now"] = format_coins(floor)
                    line["outbid"] = price > floor
                    line["estimated_net"] = format_coins(
                        int(price * qty * (1 - TP_LISTED_FEE))
                    )
            total += price * qty
            lines.append(line)
        output[key] = {"orders": lines, "gross_total": format_coins(total)}

    for key in ("history_buys", "history_sells"):
        if key not in sections:
            continue
        lines, total = [], 0
        for t in sections[key]:
            _, qty, price, line = base(t)
            line["completed_at"] = t.get("purchased")
            total += price * qty
            lines.append(line)
        summary = {"transactions": lines, "gross_total": format_coins(total)}
        if key == "history_sells":
            summary["estimated_net"] = format_coins(int(total * (1 - TP_LISTED_FEE)))
        output[key] = summary

    try:
        delivery = gw2_get("commerce/delivery")
        delivery_items = (delivery or {}).get("items") or []
        delivery_names = resolve_names("items", [i.get("id") for i in delivery_items if isinstance(i, dict)])
        output["delivery_box"] = {
            "coins": format_coins(_int((delivery or {}).get("coins"))),
            "items": [
                {
                    "item": delivery_names.get(i.get("id"), {}).get("name", f"Item {i.get('id')}"),
                    "quantity": _int(i.get("count")),
                }
                for i in delivery_items if isinstance(i, dict)
            ],
        }
    except GW2Error as err:
        errors["delivery_box"] = str(err)

    if errors:
        output["errors"] = errors
    return output


# ---------------------------------------------------------------------------
# PvP and WvW
# ---------------------------------------------------------------------------

@mcp.tool()
def pvp_wvw() -> dict:
    """Show PvP rank and stats, and WvW rank.
    PvP requires pvp permission; WvW rank comes from account permission."""
    output, errors = {}, {}
    try:
        pvp = gw2_get("pvp/stats")
        aggregate = (pvp or {}).get("aggregate") or {}
        output["pvp"] = {
            "rank": (pvp or {}).get("pvp_rank"),
            "rank_points": (pvp or {}).get("pvp_rank_points"),
            "rollovers": (pvp or {}).get("pvp_rank_rollovers"),
            "wins": aggregate.get("wins"),
            "losses": aggregate.get("losses"),
            "desertions": aggregate.get("desertions"),
        }
    except GW2Error as err:
        errors["pvp"] = str(err)
    try:
        account = gw2_get("account")
        world = (account or {}).get("world")
        world_name = resolve_names("worlds", [world]).get(world, {}).get("name")
        output["wvw"] = {
            "rank": (account or {}).get("wvw_rank"),
            "world": world_name or world,
        }
    except GW2Error as err:
        errors["wvw"] = str(err)
    if errors:
        output["errors"] = errors
    return output


# ============================================================
# Group content (raids)
# ============================================================

def _pretty_id(raw):
    """'forsaken_thicket' -> 'Forsaken Thicket' (display formatting only)."""
    pequenas = {"of", "the", "and", "in", "to"}
    palavras = str(raw or "").replace("_", " ").split()
    return " ".join(
        p if (i and p.lower() in pequenas) else p.capitalize()
        for i, p in enumerate(palavras)
    ) or str(raw)


@mcp.tool()
def raid_progress() -> dict:
    """Raid clears for your account: each raid and wing with its bosses and
    whether you cleared it (account/raids). Strikes and fractals have NO
    progress endpoint in the API — use daily_weekly_progress and
    wizards_vault for those dailies."""
    warnings = []
    raids = gw2_get("raids", {"ids": "all"}, authenticated=False) or []
    try:
        cleared = {x for x in (gw2_get("account/raids") or []) if isinstance(x, str)}
    except GW2Error as err:
        cleared = set()
        warnings.append(f"Cleared status not readable ({err}); listing raids only.")

    rows, wings_total, wings_cleared = [], 0, 0
    for raid in raids:
        if not isinstance(raid, dict):
            continue
        rid = raid.get("id")
        wings = []
        for wing in raid.get("wings") or []:
            if not isinstance(wing, dict):
                continue
            wid = wing.get("id")
            bosses = [
                _pretty_id(e.get("id"))
                for e in wing.get("events") or []
                if isinstance(e, dict) and e.get("type") == "Boss"
            ]
            # account/raids returns wing ids; raid ids cover both shapes.
            done = wid in cleared or rid in cleared
            wings_total += 1
            wings_cleared += int(done)
            wings.append({"wing": _pretty_id(wid), "cleared": done, "bosses": bosses})
        rows.append({
            "raid": _pretty_id(rid),
            "cleared": bool(wings) and all(w["cleared"] for w in wings),
            "wings": wings,
        })

    return {
        "raids": rows,
        "wings_cleared": wings_cleared,
        "wings_total": wings_total,
        "warnings": warnings,
        "note": (
            "cleared = the wing appears in account/raids (at least one clear). "
            "Strikes and fractals have no progress endpoint; use "
            "daily_weekly_progress and wizards_vault for their dailies."
        ),
    }


# ============================================================
# Crafting cost calculation
# ============================================================

# Item index (name -> id): the API has no name search, so we build a local
# index once (one big request) and keep it on disk for a few days.

def _item_index_file():
    """Path of the item name index (disk cache)."""
    base = os.environ.get("GW2_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "gw2_items_index.json")


def _item_index():
    """{normalized name: [item ids]} for every game item (memory + disk cached)."""
    key = ("item_index",)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    index, ttl = {}, ITEM_INDEX_TTL
    path = _item_index_file()
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ITEM_INDEX_TTL:
            with open(path, encoding="utf-8") as f:
                index = json.load(f)
    except (OSError, ValueError):
        index = {}

    if not index:
        # /v2/items rejects ids=all: list all ids (one light call) and fetch
        # details in batches of 200, in parallel. One-time download per week.
        ids = [i for i in (gw2_get("items", authenticated=False) or []) if isinstance(i, int)]
        if not ids:
            raise GW2Error("Could not list item ids from the API.")
        tasks = {}
        for start in range(0, len(ids), 200):
            part = ids[start:start + 200]
            tasks[start] = (
                lambda part=part: gw2_get(
                    "items", {"ids": ",".join(map(str, part))}, authenticated=False
                )
            )
        got = 0
        for ok, objects in _parallel(tasks).values():
            if not ok or not isinstance(objects, list):
                continue
            got += len(objects)
            for it in objects:
                if isinstance(it, dict) and it.get("id") and it.get("name"):
                    index.setdefault(_normalize(it["name"]), []).append(it["id"])
        if not index:
            raise GW2Error("Could not build the item index (API returned nothing).")
        # Incomplete download (rate limit?): keep in memory only, retry soon
        complete = got >= len(ids)
        ttl = ITEM_INDEX_TTL if complete else WIKI_TTL
        if complete:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(index, f, ensure_ascii=False)
            except OSError:
                pass  # disk cache optional; memory cache still applies

    _cache_set(key, index, ttl)
    return index


def _find_item(query):
    """(exact_ids, candidate_ids) for an item name query.
    Candidates are the shortest names containing the text (refine if unsure)."""
    index = _item_index()
    q = _normalize(query)
    if not q:
        return [], []
    if q in index:
        return list(index[q]), []
    if len(q) < 3:  # short text would match thousands of items
        return [], []
    matches = sorted(
        ((len(name), ids) for name, ids in index.items() if q in name),
        key=lambda pair: pair[0],
    )
    return [], [ids[0] for _, ids in matches[:20]]


def _resolve_item_input(item_id, item_name):
    """(resolved_id, error_dict_or_None) para tools que aceitam id OU nome."""
    if item_id:
        return item_id, None
    if not item_name.strip():
        return 0, {"error": "Provide item_id or item_name."}
    exact, candidates = _find_item(item_name)
    pool = exact or candidates
    if not pool:
        return 0, {"error": f"No item found for '{item_name}'. Names are English."}
    if len(pool) == 1:
        return pool[0], None
    names = resolve_names("items", pool[:10])
    return 0, {
        "error": (
            f"More than one item matches '{item_name}'. "
            "Refine the name or use item_id."
        ),
        "candidates": [
            {"id": i, "name": names.get(i, {}).get("name", f"Item {i}")}
            for i in pool[:10]
        ],
    }


def _ingredients(recipe):
    """([(item_id, quantity)], non_item_ingredients_count)."""
    items, skipped = [], 0
    for ing in recipe.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        ident = ing.get("id", ing.get("item_id"))
        if ing.get("type") in (None, "Item") and isinstance(ident, int):
            items.append((ident, _int(ing.get("count"))))
        else:
            skipped += 1
    return items, skipped


def _recipe_for_item(item_id):
    """First recipe for an item that uses item ingredients."""
    key = ("recipe_for_item", item_id)
    cached = _cache_get(key)
    if cached is not None:
        return cached or None
    chosen, seen = None, []
    try:
        ids = gw2_get("recipes/search", {"output": item_id}, authenticated=False)
    except GW2Error:
        ids = []
    if isinstance(ids, list) and ids:
        recipes = resolve_names("recipes", ids[:6])
        for ident in ids[:6]:
            recipe = recipes.get(ident)
            if not recipe:
                continue
            seen.append(recipe)
            if _ingredients(recipe)[0]:
                chosen = recipe
                break
        if chosen is None and seen:
            chosen = {**seen[0], "_no_items": True}
    _cache_set(key, chosen or {}, NAME_TTL)
    return chosen


def _account_stock():
    """{item_id: quantity} summing materials and bank; warnings if missing permissions."""
    stock, warnings = {}, []
    for label, endpoint in (("materials", "account/materials"), ("bank", "account/bank")):
        try:
            for slot in gw2_get(endpoint) or []:
                if isinstance(slot, dict) and slot.get("id"):
                    stock[slot["id"]] = stock.get(slot["id"], 0) + _int(slot.get("count"), 1)
        except GW2Error as err:
            warnings.append(f"{label}: {err}")
    return stock, warnings


def _craft_node(item_id, qty, depth, stock, path):
    """Recursively calculate crafting cost for a node."""
    used = min(qty, stock.get(item_id, 0))
    stock[item_id] = stock.get(item_id, 0) - used
    missing = qty - used
    node = {"item_id": item_id, "required": qty, "from_stock": used, "to_get": missing,
            "mode": "stock" if missing == 0 else "buy", "cost": 0}
    if missing == 0:
        return node

    price = _batch_prices([item_id]).get(item_id)
    unit = _unit_price(price, "sells") if price else 0
    buy_cost = unit * missing if unit else None
    node["unit_price"] = unit or None
    node["cost"] = buy_cost

    recipe = _recipe_for_item(item_id) if depth > 0 and item_id not in path else None
    if recipe:
        output_per_recipe = max(_int(recipe.get("output_item_count"), 1), 1)
        runs = math.ceil(missing / output_per_recipe)
        copy = dict(stock)
        children, complete = [], True
        recipe_items, skipped = _ingredients(recipe)
        for ing_id, ing_qty in recipe_items:
            child = _craft_node(ing_id, ing_qty * runs, depth - 1, copy, path | {item_id})
            children.append(child)
            if child["cost"] is None:
                complete = False
        craft_cost = sum(f["cost"] or 0 for f in children) if complete else None
        if craft_cost is not None and (buy_cost is None or craft_cost < buy_cost):
            node.update({"mode": "craft", "cost": craft_cost, "recipes": runs, "ingredients": children})
            stock.clear()
            stock.update(copy)
            if skipped:
                node["non_item_ingredients_skipped"] = skipped
        else:
            node["cost_if_crafted"] = craft_cost
    return node


def _flatten_craft(node, purchases, stock_used, crafted):
    """Flatten the craft tree into purchases, stock used, and crafted items."""
    mode = node["mode"]
    if node.get("from_stock"):
        stock_used[node["item_id"]] = stock_used.get(node["item_id"], 0) + node["from_stock"]
    if mode == "craft":
        crafted[node["item_id"]] = crafted.get(node["item_id"], 0) + node.get("recipes", 0)
        for f in node["ingredients"]:
            _flatten_craft(f, purchases, stock_used, crafted)
    elif mode == "buy":
        current = purchases.setdefault(node["item_id"], {"quantity": 0, "cost": 0, "no_price": False})
        current["quantity"] += node["to_get"]
        if node["cost"] is None:
            current["no_price"] = True
        else:
            current["cost"] += node["cost"]


def _crafting_cost(
    item_id: int, quantity: int = 1, depth: int = 2, use_stock: bool = True
) -> dict:
    """Crafting cost with TP immediate-buy prices, deducting stock; each
    ingredient uses the cheaper of buying vs crafting. depth: 0-4.
    Coins, karma, and other non-item ingredients are not included.
    Helper of item_craft_info (the public tool since v2.7)."""
    if quantity < 1 or not 0 <= depth <= 4:
        return {"error": "quantity must be >= 1 and depth between 0 and 4."}
    recipe = _recipe_for_item(item_id)
    if not recipe:
        return {"error": f"No crafting recipe for item {item_id}."}
    if recipe.get("_no_items"):
        return {
            "error": "Found recipe does not use items as ingredients "
            "(e.g., only coins or guild upgrades).",
            "recipe_id": recipe.get("id"),
            "ingredients_seen": [
                {"type": i.get("type"), "id": i.get("id"), "quantity": i.get("count")}
                for i in recipe.get("ingredients") or [] if isinstance(i, dict)
            ],
        }

    stock, warnings = ({}, [])
    if use_stock:
        stock, warnings = _account_stock()
    # Root is always crafted; only sub-recipes compete with buying.
    output_per_recipe = max(_int(recipe.get("output_item_count"), 1), 1)
    runs = math.ceil(quantity / output_per_recipe)
    root_items, skipped = _ingredients(recipe)
    root_ingredients = [
        _craft_node(ing_id, ing_qty * runs, depth, stock, {item_id})
        for ing_id, ing_qty in root_items
    ]

    purchases, stock_used, crafted = {}, {}, {}
    for node in root_ingredients:
        _flatten_craft(node, purchases, stock_used, crafted)

    all_ids = set(purchases) | set(stock_used) | set(crafted) | {item_id}
    names = resolve_names("items", list(all_ids))

    def item_name(ident):
        return names.get(ident, {}).get("name", f"Item {ident}")

    purchase_list = sorted(
        (
            {
                "item": item_name(i),
                "item_id": i,
                "quantity": d["quantity"],
                "cost": format_coins(d["cost"]),
                "no_tp_price": d["no_price"],
            }
            for i, d in purchases.items()
        ),
        key=lambda x: x["item"].lower(),
    )
    total = sum(d["cost"] for d in purchases.values())
    output = {
        "item": item_name(item_id),
        "quantity": quantity,
        "recipe_id": recipe.get("id"),
        "recipes_to_run": runs,
        "estimated_total_cost": format_coins(total),
        "purchase_list": purchase_list,
        "from_stock": [
            {"item": item_name(i), "quantity": q} for i, q in sorted(stock_used.items())
        ],
        "sub_items_to_craft": [
            {"item": item_name(i), "recipes": q} for i, q in sorted(crafted.items())
        ],
        "warning": (
            "Immediate buy prices (lowest sell offer), no buy fees. "
            "Items without TP price (account-bound) are not included."
        ),
    }
    if any(d["no_price"] for d in purchases.values()):
        output["items_without_tp_price"] = [x["item"] for x in purchase_list if x["no_tp_price"]]
    if skipped:
        output["non_item_ingredients_skipped"] = skipped
    if warnings:
        output["stock_warnings"] = warnings
    return output


@mcp.tool()
def item_craft_info(
    item_name: str = "", item_id: int = 0, quantity: int = 1,
    depth: int = 2, use_stock: bool = True,
) -> dict:
    """Craft info for any game item: by item_id or item_name (English) → item
    data, TP prices, recipe chain with estimated cost, Wiki link. No API recipe
    (vendor, drop, collection): says so and points to the Wiki.
    quantity: how many. depth: sub-recipe levels (0-4). use_stock: deduct stock."""
    if quantity < 1 or not 0 <= depth <= 4:
        return {"error": "quantity must be >= 1 and depth between 0 and 4."}
    if not item_id and not item_name.strip():
        return {"error": "Provide item_id or item_name."}

    # Locate the item when only a name was given
    if not item_id:
        try:
            exact, candidates = _find_item(item_name)
        except GW2Error as err:
            return {"error": str(err)}
        if len(exact) == 1:
            item_id = exact[0]
        elif len(exact) > 1 or len(candidates) > 1:
            ids = exact or candidates
            names = resolve_names("items", ids)
            return {
                "error": f"More than one item matches '{item_name}'; "
                         "refine the name or use item_id.",
                "candidates": [
                    {"item_id": i, "item": names.get(i, {}).get("name", f"Item {i}")}
                    for i in ids
                ],
            }
        elif candidates:
            item_id = candidates[0]
        else:
            return {
                "error": f"No item found for '{item_name}'. "
                         "Names are English; check the spelling."
            }

    details = resolve_names("items", [item_id])
    item = details.get(item_id)
    if not item:
        return {"error": f"Item {item_id} not found in the API."}

    name = item.get("name", f"Item {item_id}")
    output = {
        "item_id": item_id,
        "item": name,
        "type": item.get("type"),
        "rarity": item.get("rarity"),
        "level": item.get("level"),
        "wiki": _wiki_link(name),
    }
    if _int(item.get("vendor_value")):
        output["vendor_value"] = format_coins(_int(item["vendor_value"]))

    # Trading Post prices (item may not be traded at all).
    # _now means "if you act now": buying takes the lowest ask (sells side),
    # selling takes the highest bid (buys side).
    prices = _batch_prices([item_id]).get(item_id)
    lowest_ask = _unit_price(prices, "sells") if prices else 0
    highest_bid = _unit_price(prices, "buys") if prices else 0
    if prices:
        output["tp_buy_now"] = format_coins(lowest_ask)
        output["tp_sell_now"] = format_coins(highest_bid)
    else:
        output["no_tp_price"] = True

    # Recipe and full cost
    recipe = _recipe_for_item(item_id)
    if not recipe:
        output["craftable"] = False
        output["note"] = (
            "No crafting recipe in the API for this item (vendor, drop, or "
            "achievement collection). Check the Wiki link for how to obtain it."
        )
        return output
    output["craftable"] = True
    output["recipe_id"] = recipe.get("id")
    if recipe.get("_no_items"):
        output["note"] = "Recipe uses only coins/karma (no item ingredients)."
        return output

    craft = _crafting_cost(item_id, quantity, depth, use_stock)
    if "error" in craft:
        output["error"] = craft["error"]
        return output
    if lowest_ask:
        output["buy_total_now"] = format_coins(lowest_ask * quantity)
    output["craft"] = craft
    output["note"] = (
        "Compare buy_total_now (buy from TP now) with "
        "craft['estimated_total_cost'] (crafting materials for the same amount)."
    )
    return output


# Recipe catalog: /v2/recipes rejects ids=all but returns complete objects in
# pages of 50. Downloaded in parallel once and kept on disk for a week.

def _all_recipes():
    """[recipe objects] for every game recipe (memory + disk cached)."""
    key = ("all_recipes",)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    recipes = []
    base = os.environ.get("GW2_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "gw2_recipes_cache.json")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ITEM_INDEX_TTL:
            with open(path, encoding="utf-8") as f:
                recipes = json.load(f)
    except (OSError, ValueError):
        recipes = []

    complete = True
    if not recipes:
        recipes, complete = [], False
        # The API reports the last valid page in the 400 error text.
        last_page = 399  # fallback if that message ever changes
        try:
            gw2_get("recipes", {"page": 9999}, authenticated=False)
        except GW2Error as err:
            found = re.search(r"page values 0 - (\d+)", str(err))
            if found:
                last_page = int(found.group(1))
        falhas = 0
        for start in range(0, last_page + 1, 10):
            pages = list(range(start, min(start + 10, last_page + 1)))
            got = _parallel({
                p: (lambda p=p: gw2_get("recipes", {"page": p}, authenticated=False))
                for p in pages
            }, max_workers=16)
            for p in pages:
                ok, value = got[p]
                if not ok or not isinstance(value, list):
                    falhas += 1
                    continue
                recipes.extend(
                    r for r in value
                    if isinstance(r, dict) and r.get("output_item_id")
                )
        complete = falhas == 0 and bool(recipes)
        if complete:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(recipes, f, ensure_ascii=False)
            except OSError:
                pass  # disk cache optional; memory cache still applies

    if not recipes:
        raise GW2Error("Could not download the recipe catalog from the API.")
    _cache_set(key, recipes, ITEM_INDEX_TTL if complete else WIKI_TTL)
    return recipes


@mcp.tool()
def find_craftable(limit: int = 15) -> dict:
    """What you can craft RIGHT NOW from bank + material storage: recipes whose
    item ingredients are fully in stock, ranked by output value at the TP.
    First run downloads the recipe catalog (~1 min, cached on disk for a week).
    limit: top items shown (1-50). Per-recipe check: shared ingredients are not
    deducted between recipes; coins/karma ingredients are ignored."""
    if not 1 <= limit <= 50:
        return {"error": "limit must be between 1 and 50."}

    recipes = _all_recipes()
    stock, warnings = _account_stock()

    known = None  # None = could not check
    try:
        known = set(gw2_get("account/recipes") or [])
    except GW2Error as err:
        warnings.append(f"known recipes not checked: {err}")

    craftable = []
    for recipe in recipes:
        items, _ = _ingredients(recipe)
        if not items:
            continue
        if known is not None and recipe.get("id") not in known:
            continue
        if all(stock.get(i, 0) >= c for i, c in items):
            craftable.append(recipe)

    if not craftable:
        return {
            "checked_recipes": len(recipes),
            "craftable_count": 0,
            "craftable": [],
            "note": "Nothing craftable right now with bank + materials.",
            "warnings": warnings,
        }

    out_ids = list(dict.fromkeys(r["output_item_id"] for r in craftable))
    prices = {}
    for start in range(0, len(out_ids), 200):
        prices.update(_batch_prices(out_ids[start:start + 200]))
    names = resolve_names("items", out_ids)

    rows = []
    for recipe in craftable:
        ident = recipe["output_item_id"]
        price = prices.get(ident)
        sell = _unit_price(price, "sells") if price else 0
        count = max(_int(recipe.get("output_item_count"), 1), 1)
        rows.append({
            "item": names.get(ident, {}).get("name", f"Item {ident}"),
            "id": ident,
            "output_count": count,
            "recipe_id": recipe.get("id"),
            "disciplines": recipe.get("disciplines") or [],
            "min_rating": _int(recipe.get("min_rating")),
            "est_value_each": format_coins(sell) if sell else None,
            "est_value_total": format_coins(sell * count) if sell else None,
            "value_copper": sell * count,
        })
    rows.sort(key=lambda r: (-r["value_copper"], r["item"]))
    return {
        "checked_recipes": len(recipes),
        "craftable_count": len(rows),
        "items": rows[:limit],
        "warnings": warnings,
        "note": (
            "Values are current TP sell prices (per-recipe check; shared "
            "ingredients may be counted in more than one recipe)."
        ),
    }


# ============================================================
# Reverse recipe index / craft economy / cleanup / legendary route
# ============================================================

def _usage_index():
    """{item_id: [recipe, ...]}: reverse ingredient->recipes index, built from
    the local catalog (in-memory cache, same 7-day TTL as the catalog)."""
    key = ("usage_index",)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    index = {}
    for recipe in _all_recipes():
        for ident, _ in _ingredients(recipe)[0]:
            index.setdefault(ident, []).append(recipe)
    _cache_set(key, index, ITEM_INDEX_TTL)
    return index


def _crafting_levels():
    """{discipline: best rating} across all characters (in-memory cache).
    Empty dict when the key cannot read characters: caller degrades."""
    key = ("crafting_levels",)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        names = [n for n in (gw2_get("characters") or []) if isinstance(n, str)]
    except GW2Error:
        return {}
    levels = {}
    fetches = _parallel({
        n: (lambda n=n: gw2_get(f"characters/{quote(n, safe='')}/crafting"))
        for n in names
    })
    for ok, value in fetches.values():
        if not ok or not isinstance(value, dict):
            continue
        for d in value.get("crafting") or []:
            if isinstance(d, dict) and d.get("discipline"):
                levels[d["discipline"]] = max(
                    levels.get(d["discipline"], 0), _int(d.get("rating"))
                )
    _cache_set(key, levels, NAME_TTL)
    return levels


@mcp.tool()
def recipes_using(item_name: str = "", item_id: int = 0, limit: int = 20) -> dict:
    """Which recipes USE this item as ingredient (reverse lookup on the full
    catalog): outputs, disciplines, rating, quantity per craft. Legendary
    components come FIRST — check before selling a material. limit: rows
    (1-100, default 20). Depth 1 only; for the full tree use legendary_route."""
    if not 1 <= limit <= 100:
        return {"error": "limit must be between 1 and 100."}
    if not item_id and not item_name.strip():
        return {"error": "Provide item_id or item_name."}
    if not item_id:
        item_id, error = _resolve_item_input(0, item_name)
        if error:
            return error

    hits = _usage_index().get(item_id) or []
    hits.sort(key=lambda r: (r.get("type") != "LegendaryComponent", r.get("output_item_id") or 0))
    shown = hits[:limit]
    out_ids = [r.get("output_item_id") for r in shown if r.get("output_item_id")]
    names = resolve_names("items", [item_id] + out_ids)

    rows = []
    for recipe in shown:
        out_id = recipe.get("output_item_id")
        qty = next((q for i, q in _ingredients(recipe)[0] if i == item_id), 0)
        rows.append({
            "item": names.get(out_id, {}).get("name", f"Item {out_id}"),
            "id": out_id,
            "output_count": max(_int(recipe.get("output_item_count"), 1), 1),
            "used_per_craft": qty,
            "disciplines": recipe.get("disciplines") or [],
            "min_rating": _int(recipe.get("min_rating")),
            "type": recipe.get("type"),
        })
    return {
        "item": names.get(item_id, {}).get("name", f"Item {item_id}"),
        "item_id": item_id,
        "total_recipes": len(hits),
        "recipes": rows,
        "truncated": len(hits) > limit,
        "note": (
            "Depth-1 recipes only. LegendaryComponent rows feed legendary "
            "weapons — keep the material unless you own them all."
        ),
    }


@mcp.tool()
def craft_profit(limit: int = 10, discipline: str = "") -> dict:
    """Craft-to-sell ranking (uses REAL data): for recipes you unlocked and can
    craft, profit = output sold now (highest bid minus 10% fee) − buying what
    you lack at the lowest ask − what your stocked materials would fetch if
    sold now. Positive profit = crafting beats selling the raw materials.
    Uses the local recipe catalog (first run downloads it, cached a week).
    discipline: filter (e.g. 'Huntsman'). limit: rows (1-50, default 10)."""
    if not 1 <= limit <= 50:
        return {"error": "limit must be between 1 and 50."}

    recipes = _all_recipes()
    stock, warnings = _account_stock()
    levels = _crafting_levels()

    # Unlocked recipes; without permission, fall back to fully in-stock ones.
    try:
        known = set(gw2_get("account/recipes") or [])
        candidates = [r for r in recipes if r.get("id") in known]
    except GW2Error as err:
        warnings.append(
            f"Unlocked recipes not readable ({err}); "
            "only recipes fully in stock were ranked."
        )
        candidates = [
            r for r in recipes
            if _ingredients(r)[0]
            and all(stock.get(i, 0) >= q for i, q in _ingredients(r)[0])
        ]
    if discipline:
        discipline = discipline.strip().capitalize()
        candidates = [r for r in candidates if discipline in (r.get("disciplines") or [])]
    if levels:
        before = len(candidates)
        candidates = [
            r for r in candidates
            if any(levels.get(d, 0) >= _int(r.get("min_rating")) for d in r.get("disciplines") or [])
        ]
        skipped = before - len(candidates)
        if skipped:
            warnings.append(f"{skipped} recipes skipped: no character has the needed discipline level.")
    else:
        warnings.append("Crafting levels unreadable; craftability was not filtered.")

    # Phase 1: price outputs only; keep those with standing bids (bid > 0).
    if len(candidates) > 3000:
        candidates = candidates[:3000]
        warnings.append("More than 3000 candidate recipes; only the first 3000 were priced.")
    out_ids = list(dict.fromkeys(r.get("output_item_id") for r in candidates if r.get("output_item_id")))
    prices = {}
    for start in range(0, len(out_ids), 200):
        prices.update(_batch_prices(out_ids[start:start + 200], warnings=warnings))
    sellable = [
        r for r in candidates
        if _unit_price(prices.get(r.get("output_item_id")) or {}, "buys") > 0
    ]
    if not sellable:
        return {
            "profitable": [], "checked": len(candidates), "warnings": warnings,
            "note": "No unlocked recipe has a standing buy order for its output.",
        }
    if len(sellable) > 400:
        sellable = sellable[:400]
        warnings.append("More than 400 sellable recipes; only the first 400 were costed.")

    # Phase 2: price every ingredient involved.
    ing_ids = list(dict.fromkeys(i for r in sellable for i, _ in _ingredients(r)[0]))
    for start in range(0, len(ing_ids), 200):
        prices.update(_batch_prices(ing_ids[start:start + 200], warnings=warnings))

    rows, coin_recipes, unpriced = [], 0, 0
    for recipe in sellable:
        items, skipped_ing = _ingredients(recipe)
        if skipped_ing:  # unknown karma/coin cost: not computed
            coin_recipes += 1
            continue
        out_id = recipe.get("output_item_id")
        count = max(_int(recipe.get("output_item_count"), 1), 1)
        bid = _unit_price(prices.get(out_id) or {}, "buys")
        sale_net = int(bid * count * (1 - TP_IMMEDIATE_FEE))
        buy_cost = stock_value = 0
        ok = True
        for iid, qty in items:
            price = prices.get(iid) or {}
            ask, mat_bid = _unit_price(price, "sells"), _unit_price(price, "buys")
            have = min(qty, stock.get(iid, 0))
            missing = qty - have
            if missing and not ask:  # ingredient with no ask: cannot be bought
                ok = False
                break
            buy_cost += missing * ask
            stock_value += int(have * mat_bid * (1 - TP_IMMEDIATE_FEE))
        if not ok:
            unpriced += 1
            continue
        profit = sale_net - buy_cost - stock_value
        rows.append({
            "output_item_id": out_id, "output_count": count,
            "disciplines": recipe.get("disciplines") or [],
            "min_rating": _int(recipe.get("min_rating")),
            "sale_net_copper": sale_net, "buy_cost_copper": buy_cost,
            "stock_value_copper": stock_value, "profit_copper": profit,
        })
    rows.sort(key=lambda r: -r["profit_copper"])
    profitable = [r for r in rows if r["profit_copper"] > 0][:limit]
    names = resolve_names("items", [r["output_item_id"] for r in profitable], warnings=warnings) if profitable else {}
    for r in profitable:
        r["item"] = names.get(r["output_item_id"], {}).get("name", f"Item {r['output_item_id']}")
        r["profit"] = format_coins(r["profit_copper"])
        r["sale_net"] = format_coins(r["sale_net_copper"])
        r["buy_cost"] = format_coins(r["buy_cost_copper"])
        r["stock_value"] = format_coins(r["stock_value_copper"])
    if coin_recipes:
        warnings.append(f"{coin_recipes} recipes skipped: coin/karma ingredients (cost unknown).")
    if unpriced:
        warnings.append(f"{unpriced} recipes skipped: an ingredient has no buy order right now.")
    return {
        "profitable": profitable,
        "checked": len(candidates),
        "priced": len(sellable),
        "profitable_count": sum(1 for r in rows if r["profit_copper"] > 0),
        "warnings": warnings,
        "note": (
            "profit = output sold to the highest bid (10% fee) − buying what you "
            "lack at the lowest ask − what your stocked materials would fetch "
            "sold now. Positive = crafting beats selling the raw materials."
        ),
    }


@mcp.tool()
def bank_cleanup(min_value: int = 10000, limit: int = 25) -> dict:
    """What to SELL from the bank (uses REAL data): stacks ranked by value with
    the Trading Post instant-sale value (highest bid minus 10% fee), vendor
    value, and a flag when the item is a recipe ingredient (legendary
    components highlighted). Verdicts: keep_crafting / sell_tp / vendor / keep.
    min_value: hide stacks below this best value (copper; default 1g, 0 = all).
    limit: rows (0 = all, cap 200). Bank only — material storage is separate.
    Uses the local recipe catalog (first run downloads it, cached a week)."""
    bank = gw2_get("account/bank") or []
    groups, slots_used = {}, 0
    for slot in bank:
        if isinstance(slot, dict) and slot.get("id"):
            slots_used += 1
            groups[slot["id"]] = groups.get(slot["id"], 0) + _int(slot.get("count"))
    if not groups:
        return {"slots_used": 0, "note": "Bank is empty."}

    usage = _usage_index()
    ids = list(groups)
    warnings = []
    items = resolve_names("items", ids, warnings=warnings)
    prices = {}
    for start in range(0, len(ids), 200):
        prices.update(_batch_prices(ids[start:start + 200], warnings=warnings))

    rows, below, total_sell = [], 0, 0
    for iid, qty in groups.items():
        data = items.get(iid) or {}
        price = prices.get(iid) or {}
        bid, ask = _unit_price(price, "buys"), _unit_price(price, "sells")
        vendor = _int(data.get("vendor_value")) * qty
        sell_now = int(bid * qty * (1 - TP_IMMEDIATE_FEE))
        listed = int(ask * qty * (1 - TP_LISTED_FEE))
        best = max(sell_now, vendor)
        if best < min_value:
            below += 1
            continue
        hits = usage.get(iid) or []
        leg = sum(1 for r in hits if r.get("type") == "LegendaryComponent")
        # Conservative verdict: never recommends selling a recipe ingredient.
        if leg:
            verdict, reason = "keep_crafting", f"legendary ingredient ({leg} legendary recipes)"
        elif hits:
            verdict, reason = "keep_crafting", f"ingredient of {len(hits)} recipes"
        elif bid and sell_now >= vendor:
            verdict, reason = "sell_tp", "no recipe uses it"
        elif vendor:
            verdict, reason = "vendor", "vendor pays more than TP right now"
        elif bid:
            verdict, reason = "sell_tp", "tradeable (vendor value is 0)"
        else:
            verdict, reason = "keep", "no TP demand and no vendor value"
        if verdict == "sell_tp":
            total_sell += sell_now
        rows.append({
            "item": data.get("name", f"Item {iid}"),
            "id": iid,
            "quantity": qty,
            "full_stacks": qty // 250,
            "sell_now": format_coins(sell_now),
            "listed": format_coins(listed),
            "vendor": format_coins(vendor),
            "best_value": format_coins(best),
            "best_value_copper": best,
            "verdict": verdict,
            "reason": reason,
        })
    rows.sort(key=lambda r: -r["best_value_copper"])
    limit = max(0, min(limit, 200))
    return {
        "slots_used": slots_used,
        "rows_shown": len(rows[:limit] if limit else rows),
        "hidden_below_min_value": below,
        "total_sell_tp_now": format_coins(total_sell),
        "rows": rows[:limit] if limit else rows,
        "warnings": warnings,
        "note": (
            "sell_now = highest bid × qty − 10% fee (immediate sale). "
            "Material storage is NOT included — use materials_with_names. "
            "Wiki-only recipes (gen1 legendaries) are not in this catalog — "
            "precursors may look unused."
        ),
    }


@mcp.tool()
def legendary_route(item_name: str = "", item_id: int = 0, depth: int = 3) -> dict:
    """Step-by-step route to CRAFT a legendary (uses REAL data): flat list in
    dependency order — craft steps (discipline/rating), buy steps (TP price
    now), stock steps (you already have it) and collection steps (untradeable,
    Wiki link). gen2/3 come from API recipes; gen1 falls back to the Wiki
    {{recipe}} template. Shows if you already own it (legendary armory).
    depth: tree levels (1-5, default 3). Karma/currency costs NOT included."""
    if not 1 <= depth <= 5:
        return {"error": "depth must be between 1 and 5."}
    if not item_id and not item_name.strip():
        return {"error": "Provide item_id or item_name."}
    if not item_id:
        item_id, error = _resolve_item_input(0, item_name)
        if error:
            return error

    stock, warnings = _account_stock()
    try:
        armory = gw2_get("account/legendaryarmory") or []
    except GW2Error as err:
        armory = []
        warnings.append(f"legendary armory not readable ({err})")
    in_armory = next(
        (_int(a.get("count")) for a in armory if isinstance(a, dict) and a.get("id") == item_id),
        0,
    )
    levels = _crafting_levels()

    steps, leaf_ids, truncated = [], set(), False

    def walk(iid, qty, level, path):
        nonlocal truncated
        if truncated or len(steps) > 150:
            truncated = True
            return
        have = min(qty, stock.get(iid, 0))
        stock[iid] = stock.get(iid, 0) - have
        missing = qty - have
        base = {"depth": level, "id": iid, "qty": qty, "have": have, "missing": missing}
        if missing == 0:
            steps.append({**base, "source": "stock"})
            return
        if iid in path:
            steps.append({**base, "source": "cycle", "note": "already listed above"})
            return
        if level > depth:
            steps.append({**base, "source": "max_depth", "note": "increase depth to expand"})
            leaf_ids.add(iid)
            return

        recipe = _recipe_for_item(iid)
        if recipe and _ingredients(recipe)[0]:
            count = max(_int(recipe.get("output_item_count"), 1), 1)
            runs = math.ceil(missing / count)
            disc = recipe.get("disciplines") or []
            rating = _int(recipe.get("min_rating"))
            node = {**base, "source": "craft", "runs": runs,
                    "disciplines": disc, "min_rating": rating}
            if levels:
                node["craftable"] = any(levels.get(d, 0) >= rating for d in disc)
            steps.append(node)
            for cid, cqty in _ingredients(recipe)[0]:
                walk(cid, cqty * runs, level + 1, path | {iid})
            return

        # No recipe in the API: try the Wiki {{recipe}} (gen1 and Mystic Forge).
        wiki = None
        if not recipe or recipe.get("_no_items"):
            page = (resolve_names("items", [iid]).get(iid) or {}).get("name") or str(iid)
            wiki = _wiki_recipe(page)
        if wiki and wiki.get("ingredients"):
            count = max(_int(wiki.get("output_count"), 1), 1)
            runs = math.ceil(missing / count)
            steps.append({**base, "source": "craft_wiki", "runs": runs,
                          "note": "Mystic Forge recipe from the Wiki (no API entry)"})
            for cname, cqty in wiki["ingredients"]:
                found, _cands = _find_item(cname)
                if found:
                    walk(found[0], cqty * runs, level + 1, path | {iid})
                else:
                    steps.append({"depth": level + 1, "id": 0, "item": cname,
                                  "qty": cqty * runs, "missing": cqty * runs,
                                  "source": "unresolved",
                                  "note": "name not resolved to an item id"})
            return

        # Leaf: buy on the TP or collection item.
        steps.append({**base, "source": "leaf"})
        leaf_ids.add(iid)

    walk(item_id, 1, 0, set())

    # Phase 2: batch names + leaf prices; source of each leaf.
    all_ids = [s["id"] for s in steps if s.get("id")]
    names = resolve_names("items", all_ids, warnings=warnings) if all_ids else {}
    prices = _batch_prices(list(leaf_ids), warnings=warnings) if leaf_ids else {}
    est_cost = 0
    for s in steps:
        iid = s.get("id")
        s["item"] = (names.get(iid) or {}).get("name", s.get("item") or f"Item {iid}")
        if s["source"] == "leaf":
            ask = _unit_price(prices.get(iid) or {}, "sells")
            if ask:
                s["source"] = "buy"
                s["unit_buy_now"] = format_coins(ask)
                s["est_cost"] = format_coins(ask * s["missing"])
                est_cost += ask * s["missing"]
            else:
                s["source"] = "collection"
                s["wiki"] = _wiki_link(s["item"])
                s["note"] = "not tradeable and no recipe — collection/wiki step"

    craft_disc = {}
    for s in steps:
        if s["source"] == "craft":
            for d in s.get("disciplines") or []:
                craft_disc[d] = max(craft_disc.get(d, 0), s.get("min_rating", 0))
    item_obj = names.get(item_id) or {}
    return {
        "legendary": item_obj.get("name", f"Item {item_id}"),
        "id": item_id,
        "wiki": _wiki_link(item_obj.get("name", str(item_id))),
        "in_armory": in_armory,
        "already_owned": in_armory > 0,
        "est_buy_cost": format_coins(est_cost),
        "est_buy_cost_copper": est_cost,
        "craft_disciplines": craft_disc,
        "steps": steps,
        "truncated": truncated,
        "warnings": warnings,
        "note": (
            "Flat list in dependency order (parent before children). buy prices "
            "are the current lowest ask; collection steps need the Wiki. "
            "Currency costs (karma, etc.) are NOT included."
        ),
    }


# ============================================================
# Progress history
# ============================================================

def _history_file():
    """Return the path to the progress history JSONL file."""
    base = os.environ.get("GW2_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "gw2_history.jsonl")


def _collect_snapshot():
    """Collect a snapshot of account progress."""
    def gold():
        wallet = gw2_get("account/wallet")
        return next(
            (_int(m.get("value")) // 10000 for m in wallet if isinstance(m, dict) and m.get("id") == 1), 0
        )

    def mastery():
        regions = (gw2_get("account/mastery/points") or {}).get("totals") or []
        regions = [r for r in regions if isinstance(r, dict)]
        return (sum(_int(r.get("earned")) for r in regions),
                sum(_int(r.get("spent")) for r in regions))

    def hours():
        characters = gw2_get("characters", {"ids": "all"})
        return round(sum(_int(c.get("age")) for c in characters if isinstance(c, dict)) / 3600, 1)

    r = _parallel({
        "points": lambda: achievement_points().get("estimated_total"),
        "completed": lambda: sum(1 for p in _account_progress() if p.get("done")),
        "gold": gold,
        "mastery": mastery,
        "hours": hours,
    })
    snap, errors = {"at_utc": _utc_now().isoformat()}, {}
    for key, field in (
        ("points", "achievement_points"),
        ("completed", "achievements_completed"),
        ("gold", "gold"),
        ("hours", "hours_played"),
    ):
        ok, value = r[key]
        if ok:
            snap[field] = value
        else:
            errors[field] = value
    ok, value = r["mastery"]
    if ok:
        snap["mastery_points_earned"], snap["mastery_points_spent"] = value
    else:
        errors["mastery"] = value
    if errors:
        snap["errors"] = errors
    return snap


def _read_history():
    """Read progress history from JSONL file."""
    path = _history_file()
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # corrupted history line: skip the line
            if isinstance(record, dict) and record.get("at_utc"):
                records.append(record)
    return records


@mcp.tool()
def save_snapshot() -> dict:
    """Save a progress snapshot (achievement points, gold, mastery, hours played)
    to a local file for later comparison with compare_progress.
    File is saved in GW2_DATA_DIR (default: server directory)."""
    snap = _collect_snapshot()
    path = _history_file()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    except OSError as err:
        return {"error": f"Could not write history ({type(err).__name__}).", "snapshot": snap}
    return {"saved_to": path, "snapshot": snap}


COMPARABLE_FIELDS = (
    "achievement_points", "achievements_completed", "gold",
    "mastery_points_earned", "mastery_points_spent", "hours_played",
)


@mcp.tool()
def compare_progress(days: int = 7) -> dict:
    """Compare current state with the snapshot closest to N days ago
    (use save_snapshot to create snapshots). Shows what changed."""
    if not 1 <= days <= 3650:
        return {"error": "days must be between 1 and 3650."}
    history = _read_history()
    if not history:
        return {"error": "No snapshots saved yet. Run save_snapshot first."}

    target = _utc_now() - timedelta(days=days)

    def when(record):
        return datetime.fromisoformat(record["at_utc"])

    previous = [r for r in history if when(r) <= target]
    base = previous[-1] if previous else history[0]
    current = _collect_snapshot()

    differences = {}
    for field in COMPARABLE_FIELDS:
        a, b = base.get(field), current.get(field)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            differences[field] = {"before": a, "now": b, "change": round(b - a, 1)}
    output = {
        "reference_snapshot": base["at_utc"],
        "now": current["at_utc"],
        "differences": differences,
    }
    if not previous:
        output["warning"] = (
            f"No snapshot with {days} days or more; used the oldest available."
        )
    return output


# ============================================================
# Legendary armory
# ============================================================

def _legendary_group(obj):
    """Determine the legendary group name for an item."""
    type_ = obj.get("type")
    d = obj.get("details") or {}
    if type_ == "Weapon":
        return f"Weapon: {d.get('type')}"
    if type_ == "Armor":
        return f"Armor: {d.get('type')} ({d.get('weight_class')})"
    if type_ == "Back":
        return "Back"
    if type_ == "Trinket":
        return f"Trinket: {d.get('type')}"
    return type_ or "Other"


@mcp.tool()
def legendary_armory(category: str = "", missing_limit: int = 40) -> dict:
    """Show legendary armory progress: what you have and what's missing,
    grouped by type (weapons, armor, back, trinkets). Requires unlocks.
    category: filter by group text (e.g., 'staff', 'heavy', 'back').
    missing_limit: max missing items listed (0 = all)."""
    r = _parallel({
        "account": lambda: gw2_get("account/legendaryarmory"),
        # Public list requires ids=all; without it the endpoint returns empty []
        "all": lambda: gw2_get("legendaryarmory", {"ids": "all"}, authenticated=False),
    })
    for key in ("account", "all"):
        if not r[key][0]:
            return {"error": r[key][1]}
    mine = {
        i["id"]: _int(i.get("count"), 1)
        for i in r["account"][1] if isinstance(i, dict) and "id" in i
    }
    existing = [i for i in r["all"][1] if isinstance(i, dict) and "id" in i]
    if not existing:
        return {"error": "API did not return legendary armory item list."}

    objects = resolve_names("items", [i["id"] for i in existing])

    def item_name(ident):
        return objects.get(ident, {}).get("name", f"Item {ident}")

    filter_ = (category or "").strip().lower()
    have, missing, groups = [], [], {}
    for i in existing:
        ident = i["id"]
        group = _legendary_group(objects.get(ident, {}))
        if filter_ and filter_ not in group.lower() and filter_ not in item_name(ident).lower():
            continue
        if mine.get(ident, 0) > 0:
            have.append({"item": item_name(ident), "group": group, "quantity": mine[ident]})
        else:
            missing.append({"item": item_name(ident), "group": group})
            groups[group] = groups.get(group, 0) + 1
    # items the account has that the public list doesn't include (rare)
    total = len(have) + len(missing)
    shown, omitted = _limit(sorted(missing, key=lambda x: (x["group"], x["item"])), missing_limit)
    output = {
        "have_total": len(have),
        "total_considered": total,
        "percent": round(len(have) / total * 100, 1) if total else None,
        "have": have,
        "missing_by_group": dict(sorted(groups.items())),
        "missing": shown,
    }
    if omitted:
        output["missing_omitted"] = omitted
    if filter_:
        output["filter"] = category
    return output


# ============================================================
# Diagnostics
# ============================================================

@mcp.tool()
def diagnostics(test_api: bool = True) -> dict:
    """Show server status: versions, key permissions/scopes (never the value),
    timezone, history file, cache, and recent errors. Use when something fails."""
    import platform
    from importlib import metadata

    def version(pkg):
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return None

    try:
        _, tz_warning = _resolve_timezone("America/Sao_Paulo")
    except GW2Error as err:
        tz_warning = str(err)

    path = _history_file()
    folder = os.path.dirname(path)
    output = {
        "server": VERSION,
        "python": platform.python_version(),
        "system": platform.platform(),
        "libraries": {p: version(p) for p in ("mcp", "requests", "tzdata")},
        "key_configured": bool((os.environ.get("GW2_API_KEY") or "").strip()),
        "timezone": tz_warning or "ok (America/Sao_Paulo)",
        "history": {
            "file": path,
            "exists": os.path.exists(path),
            "folder_writable": os.access(folder, os.W_OK),
        },
        "cache_entries": len(_cache),
        "recent_errors": [{"at_utc": t, "message": m} for t, m in list(_recent_errors)[-10:]],
    }
    # Key permissions (available and missing scopes)
    if output["key_configured"]:
        try:
            token = gw2_get("tokeninfo")
            perms = token.get("permissions", []) if isinstance(token, dict) else []
            output["key"] = {
                "name": token.get("name"),
                "type": token.get("type"),
                "permissions": perms,
                "missing_scopes": [s for s in REQUIRED_SCOPES if s not in perms],
            }
        except GW2Error as err:
            output["key"] = {"error": str(err)}
    if test_api:
        start = time.time()
        try:
            gw2_get("build", authenticated=False)
            output["public_api"] = {"ok": True, "ms": round((time.time() - start) * 1000)}
        except GW2Error as err:
            output["public_api"] = {"ok": False, "error": str(err)}
    return output


if __name__ == "__main__":
    mcp.run(transport="stdio")
