# CLAUDE.md — Home Assistant Integration Standards

> **Usage**: copy this file verbatim into any HA integration repo. Claude Code will
> read it automatically and apply the conventions below consistently across all repos.

---

## Project type

This is a **Home Assistant custom integration** (HACS-compatible).
Language: Python 3.12+. No build step. Pure-Python, no Cython.

---

## Repository layout

```
<repo-root>/
├── custom_components/<domain>/   # Integration source
│   ├── __init__.py               # Entry-point: setup/unload, services, scheduling
│   ├── manifest.json             # HA metadata (domain, version, requirements)
│   ├── config_flow.py            # UI wizard (ConfigFlow + OptionsFlow)
│   ├── coordinator.py            # DataUpdateCoordinator subclass
│   ├── sensor.py                 # SensorEntity subclasses (one file per platform)
│   ├── entity.py                 # Shared base entity (DeviceInfo, attribution)
│   ├── const.py                  # All constants and defaults — no magic strings
│   ├── strings.json              # Localised UI strings (Italian default)
│   ├── translations/
│   │   └── en.json               # English translations
│   └── services.yaml             # HA service schema definitions
├── tests/
│   ├── conftest.py               # HA module stubs + shared fixtures
│   ├── test_<module>.py          # One test file per source module
│   └── __init__.py
├── .github/workflows/
│   ├── validate.yml              # Hassfest + HACS + lint + pytest (every push)
│   └── release.yml               # Auto-versioning + GitHub release (main / tags)
├── pyproject.toml                # pytest config + coverage settings
├── requirements_test.txt         # Test-only deps (pytest, pytest-asyncio, etc.)
├── hacs.json                     # HACS metadata
├── info.md                       # HACS info snippet
├── README.md                     # User-facing documentation
└── CLAUDE.md                     # This file
```

---

## Code conventions

### General
- Use `from __future__ import annotations` in every file.
- All public functions/classes need type annotations.
- No external runtime dependencies unless absolutely necessary (use stdlib).
- `const.py` is the single source of truth for constants; never hard-code strings
  or numbers inline in business-logic files.
- Module-level loggers: `_LOGGER = logging.getLogger(__name__)`.

### Home Assistant patterns
- One `DataUpdateCoordinator` subclass per config entry.
- Entities inherit from `CoordinatorEntity`; override `_handle_coordinator_update`.
- Use `RestoreEntity` for entities whose state must survive HA restarts.
- Sensor `native_value` returns `None` when data is unavailable (never raise).
- `available` property must be overridden whenever a sensor can become unavailable
  independent of the coordinator.
- Use `entry.options` (not `entry.data`) for all mutable user settings.
- `entry.data` stores only immutable fields (e.g. location coordinates).
- Register services idempotently in `async_setup_entry` — check before registering.
- Always call `hass.config_entries.async_forward_entry_setups` (not the deprecated
  singular form).

### Config flow
- Validate API connectivity in `async_step_user` before creating the entry.
- Use `selector.*` helpers for all form fields (never raw `vol.In`/`vol.Range`).
- `ConfigFlow.VERSION = 1`; bump only when a migration is needed.
- Store computed defaults from `hass.config.*` (e.g. home lat/lon) in `async_step_user`.
- `OptionsFlow.async_step_init` must handle the case where an option key is missing
  (use `opts.get(KEY, DEFAULT)` everywhere).

### Translations
- `strings.json` = default Italian strings.
- `translations/en.json` = English translation (always kept in sync).
- Every config flow field, entity name, and service field must have a translation key.
- Entity names use `_attr_has_entity_name = True` + `_attr_translation_key`.

---

## Testing

### Framework
- `pytest` + `pytest-asyncio` (asyncio_mode = "auto").
- No `pytest-homeassistant-custom-component` required — we stub HA modules in
  `tests/conftest.py` via `sys.modules` injection.
- Python 3.12 required (`type X = Y` syntax).

### Running tests
```bash
pip install -r requirements_test.txt
python3.12 -m pytest tests/ -v
```

### Coverage target
- Pure-Python modules (geo, parser, prediction, storage logic): aim for ≥90%.
- HA-bound modules (sensor, config_flow, coordinator): focus on state/attribute
  logic; skip HA lifecycle methods that need the full HA test harness.

### What to test
1. **Pure business logic** — haversine distance, CSV parsing, forecasting algorithm,
   clamping, gap interpolation.
2. **Sensor state / attributes** — `native_value`, `extra_state_attributes`,
   `available`, `unique_id` format, `translation_placeholders`.
3. **Edge cases** — empty data, missing coordinator data (`coordinator.data = None`),
   rank beyond available stations, malformed CSV rows.

### What NOT to test (without full HA infra)
- `async_setup_entry`, `async_unload_entry` lifecycle.
- Actual HTTP calls (mock at the `MimitApiClient` level if needed).
- HA Long-Term Statistics injection.

### Test file naming
`tests/test_<source_module>.py` → e.g. `test_geo.py`, `test_sensor.py`.

### Fixtures
All shared fixtures live in `tests/conftest.py`.
- `make_station(...)` — returns a `Station` dataclass.
- `make_enriched(...)` — returns an `EnrichedStation` with sensible defaults.
- `mock_coordinator` fixture — `MagicMock` with `.data = None` and `.ai_cache = {}`.
- `mock_config_entry` fixture — `MagicMock` with a realistic `.options` dict.

---

## CI/CD

### validate.yml — triggers on every push to known branches
Jobs (all independent, run in parallel):
1. **hassfest** — HA manifest + entity/platform validation.
2. **hacs** — HACS structure check (`continue-on-error: true` for first run).
3. **lint** — JSON syntax + YAML syntax + Python tests (pytest).

### release.yml — triggers on push to main/feature branches, tags, manual dispatch
Logic:
| Trigger | Tag | Type |
|---|---|---|
| push to `main` | `v{auto-patch}` | Stable release, latest |
| push to `claude/**`, `feature/**` etc. | `beta-{branch-slug}` | Pre-release, replaced on next push |
| tag `v1.2.3` | (existing tag) | Stable release |
| tag `v1.2.3-beta.1` | (existing tag) | Pre-release |
| `workflow_dispatch` | `v{input}` | Stable or pre-release |

After a stable release: `manifest.json` version is bumped and committed back to
`main` with `[skip ci]` to avoid infinite loops.

### Branch naming
- `main` — stable, always releasable.
- `claude/<description>-<id>` — AI-assisted feature branches.
- `feature/<description>` — manual feature branches.
- `fix/<description>` — bugfix branches.
- `release/<version>` — release preparation branches.

---

## Versioning

- `manifest.json` is the single source of version truth.
- Format: `MAJOR.MINOR.PATCH` (semver, no leading `v`).
- PATCH is auto-incremented by CI on every merge to `main`.
- MINOR bumps for new sensor types, new config options, new services.
- MAJOR bumps for breaking changes (config entry migration required).
- Pre-release suffixes: `-beta.N`, `-rc.N`.

---

## Documentation

### README.md structure
1. Short description + badge (HACS install, HA version, license).
2. Features list.
3. Data sources (license, update frequency).
4. Installation (HACS + manual).
5. Configuration (step-by-step, with screenshots or field descriptions).
6. Sensors reference (table: sensor name, state type, key attributes).
7. Services reference.
8. Lovelace examples.
9. Example automations.
10. Limitations / known issues.
11. Contributing / license.

### Changelog
Maintain `CHANGELOG.md` with one section per release version.
Format: `## [X.Y.Z] - YYYY-MM-DD` with `### Added / Changed / Fixed / Removed`.

---

## Common tasks for Claude

### Adding a new sensor type
1. Add `SENSOR_<NAME> = "<name>"` constant to `const.py`.
2. Create `class <Name>Sensor(CarburantiMimitEntity, SensorEntity)` in `sensor.py`.
   - Set `_attr_unique_id`, `_attr_translation_key`, `_attr_translation_placeholders`.
   - Override `native_value` and `extra_state_attributes`.
   - Override `available` if the sensor can be unavailable independently.
3. Add sensor to `async_setup_entry` in `sensor.py`.
4. Add translation keys in `strings.json` (Italian) and `translations/en.json` (English).
5. Add tests in `tests/test_sensor.py`.

### Adding a new config option
1. Add `CONF_<NAME>` and `DEFAULT_<NAME>` to `const.py`.
2. Add the field to both `async_step_advanced` (config flow) and `async_step_init`
   (options flow) in `config_flow.py` — using a `selector.*` helper.
3. Add to the options dict in both flow handlers.
4. Add translation keys in `strings.json` and `translations/en.json`.
5. Read the option in the relevant code with `entry.options.get(CONF_<NAME>, DEFAULT_<NAME>)`.

### Adding a new HA service
1. Define the handler in `services.py`.
2. Register it in `__init__.py → _async_register_services()` (idempotent check).
3. Add schema to `services.yaml`.
4. Add translation keys in `strings.json → services.*` and `translations/en.json`.

### Bumping the HA minimum version
Update `homeassistant` in `manifest.json` and document the reason in CHANGELOG.md.

---

## What Claude should NOT do
- Do not add `requirements` to `manifest.json` unless truly necessary (keep it empty
  for pure-Python integrations).
- Do not use `async_get_last_state` from `RestoreEntity` without the `RestoreEntity`
  mixin — it will silently fail.
- Do not call `hass.async_create_task` from a synchronous context.
- Do not import `homeassistant` at module level in test files — use the stubs in
  `conftest.py`.
- Do not commit secrets, API keys, or `.env` files.
- Do not create `README.md` or `CHANGELOG.md` unless explicitly asked.
- Do not push to `main` directly — always use a feature/claude branch.
