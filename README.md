# GW2 Assistant 🎮

A **MCP** (Model Context Protocol) server for Guild Wars 2: it connects any
MCP-compatible agent (OpenCode, Claude Desktop, Cursor, Cline and others) to
your game account through the official API and answers real questions about
your progress: achievements, bank, economy, crafting, legendaries and more.

- **Read-only**: the server never writes anything to the game. It cannot buy,
  sell, move or spend anything.
- **40 tools** (v2.8): from a simple lookup ("how much gold is in my bank?")
  to a full step-by-step route to craft a legendary.
- **Real data always**: no guessing, everything comes from the official
  ArenaNet API and the game Wiki, on the spot.

---

## What you can do

### 📋 Account and characters
| Tool | What it does |
|---|---|
| `account_info` | Account name, access level |
| `account_wallet` | Every currency and wallet |
| `character_list` | Characters: race, profession, hours played |
| `character_crafting` | Crafting disciplines per character (up to 500) |
| `equipment` | Equipment review: rarity, stats, runes, sigils |
| `account_bank` | Bank items, grouped with names |
| `account_inventory` | Character bags, grouped by item |
| `materials_with_names` | Full material storage with quantities |
| `unlocks` | Unlock summary: skins, dyes, minis, mounts, titles |

### 🏆 Achievements and progress
| Tool | What it does |
|---|---|
| `achievement_details` | Full achievement card by ID (with your progress) |
| `achievement_points` | Total points and how far to the next 500-point milestone |
| `progress_guide` | Analysis of partial achievements: pending bits, priorities |
| `action_plan` | Action plan: identified bits, counters, what is missing |
| `missing_achievements` | What an achievement is really missing (crossing bits) |
| `pending_areas` | Unregistered areas of an exploration achievement |
| `find_area` | Locates an area on the official map (region, level, coordinates) |
| `legendary_armory` | Legendary armory progress: what you have, what is missing |

### ☀️ What to do today
| Tool | What it does |
|---|---|
| `what_to_do_today` | Daily summary in one call: vault, events, achievements, masteries |
| `wizards_vault` | Wizard's Vault: daily, weekly and special objectives |
| `daily_weekly_progress` | What you already did today/this week (bosses, raids, chests) |
| `event_timers` | Live event phases and upcoming schedules |
| `raid_progress` | Raid clears per wing and boss |
| `pvp_wvw` | PvP and WvW rank and stats |

### 💰 Economy and Trading Post
| Tool | What it does |
|---|---|
| `item_info` | Full item card by name or ID: price, type, wiki link |
| `item_prices` | Prices for many items at once (up to 200) |
| `tp_listings` | Order book depth: top bids and asks |
| `tp_transactions` | Your open orders compared to the market |
| `account_value` | Bank and materials value in gold, at current prices |
| `bank_cleanup` | What to SELL from the bank: stacks ranked by real value |

### 🔨 Crafting
| Tool | What it does |
|---|---|
| `item_craft_info` | Can you craft it? Recipe, materials and the verdict: craft, buy or vendor |
| `find_craftable` | What you can craft RIGHT NOW with what you already own |
| `recipes_using` | Which recipes use this item as an ingredient (reverse lookup) |
| `craft_profit` | Craft-to-sell ranking with real profit (recipe vs market) |
| `legendary_route` | Step-by-step route to craft a legendary from scratch |

### 📚 Wiki and utilities
| Tool | What it does |
|---|---|
| `search_wiki` | Search the official game Wiki with snippets |
| `wiki_details` | Content of a Wiki page |
| `save_snapshot` | Saves a progress snapshot (points, gold, masteries, hours) |
| `compare_progress` | Compares now against the snapshot from N days ago |
| `diagnostics` | Server status: version, key permissions (never the value) |

---

## Installation

### 1. Requirements
- Python **3.10+** (tested on 3.14)
- A GW2 API key (step 3)
- Any MCP-capable agent/client (the server is standard MCP over stdio)

### 2. Download and set up the environment

```bash
git clone https://github.com/YOUR_USER/gw2-assistant.git
cd gw2-assistant

# Create the virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\pip install requests tzdata mcp      # Windows
# source .venv/bin/activate && pip install requests tzdata mcp   # Linux/Mac
```

### 3. Generate your API key
1. Go to [account.arena.net → Applications](https://account.arena.net/applications)
2. **Create New API Key**, give it a name (e.g. "opencode")
3. Check the permissions: `account`, `wallet`, `progression`, `inventories`,
   `characters`, `unlocks`, `tradingpost`, `pvp`, `wvw`, `builds`, `guilds`
4. Copy the generated key

> The key exposes data from your account (characters, bank, progress). This
> project only reads, but still treat it like a password.

### 4. Connect it to your agent (MCP)

The server is **standard MCP over stdio**: it works with any MCP client. You
only need to provide two things: the `.venv` Python interpreter and the
`GW2_API_KEY` environment variable.

**Format used by most clients** (Claude Desktop, Cline and others, paste it
into the agent's own config file):

```json
{
    "mcpServers": {
        "gw2-assistant": {
            "command": "FULL/PATH/gw2-assistant/.venv/Scripts/python.exe",
            "args": ["FULL/PATH/gw2-assistant/gw2_server.py"],
            "env": {
                "GW2_API_KEY": "paste_your_key_here"
            }
        }
    }
}
```

Where that file lives in each agent:

| Agent | Config file |
|---|---|
| Claude Desktop | `claude_desktop_config.json` (help button → Config File) |
| Cursor | `.cursor/mcp.json` (project or global) |
| Cline | `cline_mcp_settings.json` |
| Others | look for "MCP" in settings: the `mcpServers` format above is the de-facto standard |

**OpenCode** uses a slightly different format, in the project's
`opencode.json` (or your global config):

```json
{
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
        "gw2-assistant": {
            "type": "local",
            "command": [
                "FULL/PATH/gw2-assistant/.venv/Scripts/python.exe",
                "FULL/PATH/gw2-assistant/gw2_server.py"
            ],
            "environment": {
                "GW2_API_KEY": "paste_your_key_here"
            },
            "enabled": true
        }
    }
}
```

> ⚠️ **This file contains your key.** This repository's `.gitignore` already
> blocks committing it, keep it that way.

Linux/Mac: use the venv interpreter
(`gw2-assistant/.venv/bin/python`) instead of the Windows path.

### 5. Reload and ask away
Reload your agent's MCP servers (in OpenCode: `/mcps` → disconnect/connect;
in Claude Desktop, restart the app). Done: ask anything about your account.

---

## Tests and verification

```bash
# Tests with mocked API: no network, no key, runs in < 1s (149 tests)
.venv\Scripts\python -m unittest test_gw2_server

# Live verifier: runs all 39 tools with your real key
# (set GW2_API_KEY in the terminal first)
.venv\Scripts\python verify_api.py
```

Changed `gw2_server.py`? Reload the MCP server in your agent (in OpenCode:
`/mcps` → disconnect/connect) to rerun it.

---

## Project files

| File | What it is |
|---|---|
| `gw2_server.py` | The MCP server (40 tools) |
| `test_gw2_server.py` | Tests with mocked API responses |
| `verify_api.py` | Live verifier using the real key |
| `AGENTS.md` | Internal development notes |
| `opencode.json` | Local config (contains the key, gitignored) |
| `gw2_items_index.json` | Name→id item cache (regenerates in 7 days) |
| `gw2_recipes_cache.json` | Game recipe cache (regenerates in 7 days) |
| `gw2_history.jsonl` | Progress snapshots (your data, gitignored) |

---

## Technical details

- **Transport**: stdio, the MCP standard, works with any MCP client (the
  client launches the Python process directly, no web server)
- **Dependencies**: `mcp`, `requests`, `tzdata`. Nothing else
- **Caches**: items and recipes are downloaded in bulk from the API and kept
  as JSON for 7 days; the very first call may take a few extra seconds
- **Language**: the API only has English names. Query in English
  ("Mithril Ingot", not "Mithril")
- **Honest limitations**: if a call fails, the server says what failed; it
  never invents data. TP prices are instant and do not guarantee profit

---

## License

[MIT](LICENSE) © 2026 Jean Faquim.

The code is free to use, modify and redistribute. Guild Wars 2, the API and
the Wiki remain property of ArenaNet and aren't covered by this license.
