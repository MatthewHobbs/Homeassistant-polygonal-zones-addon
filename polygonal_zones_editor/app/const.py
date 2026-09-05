import os

OPTIONS_FILE = "/data/options.json"
DATA_FOLDER = "/data/polygonal_zones"
ZONES_FILE = os.path.join(DATA_FOLDER, "zones.json")

ALLOWED_IPS = ["172.30.32.2"]

MAX_SAVE_BYTES = 512 * 1024

# Schema version stamped onto the FeatureCollection on every write. Bump
# when the zones.json shape changes in a way the companion integration
# (or any other consumer) must detect. The value is informational today
# — readers are expected to ignore unknown top-level keys for forward
# compatibility; consumers that pin behaviour to a shape can branch on
# this. Paired with per-feature `properties.id` so automations have a
# stable binding handle across renames.
SCHEMA_VERSION = 1


# --- Tracker overlay -------------------------------------------------------
# The editor can plot live tracker positions over the map so a user can see
# *why* a zone misclassifies a device. That needs Home Assistant's state API,
# reached through the Supervisor proxy with the add-on's own SUPERVISOR_TOKEN
# (granted by `homeassistant_api: true` in config.yaml).
#
# Deliberately NOT a general state proxy: only entities the user has named in
# the `overlay_entities` option are ever requested or returned. The list is
# empty by default, so the add-on asks Home Assistant for nothing until the
# user opts in — the editor is LAN-reachable when allow_all_ips is on, and an
# unfiltered proxy there would expose every entity in the house.
SUPERVISOR_API = "http://supervisor/core/api"
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"

# Per-request budget for the whole overlay fetch. Home Assistant is on the
# same host, so this is a stall guard rather than a latency allowance.
OVERLAY_TIMEOUT_SECONDS = 5.0

# Ceiling on how many entities the overlay will fetch, whatever the option
# says. Each is a separate Supervisor call; an accidental 200-entity list
# would hammer core and stall the editor.
MAX_OVERLAY_ENTITIES = 25

# Coordinates are rounded before they leave this add-on. The editor draws
# zones at property scale, where ~11 m is already finer than any boundary a
# user can place by hand on a map; full float precision would put ~0.1 mm
# positions into a LAN-reachable response for no benefit.
OVERLAY_COORD_DECIMALS = 4
