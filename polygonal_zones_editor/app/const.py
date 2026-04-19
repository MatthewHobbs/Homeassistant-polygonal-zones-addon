import os

OPTIONS_FILE = "/data/options.json"
DATA_FOLDER = "/data/polygonal_zones"
ZONES_FILE = os.path.join(DATA_FOLDER, "zones.json")

ALLOWED_IPS = ['172.30.32.2']

MAX_SAVE_BYTES = 512 * 1024

# Schema version stamped onto the FeatureCollection on every write. Bump when
# the zones.json shape changes in a way a consumer (the HA integration) must
# detect. The value is purely informational today — readers are expected to
# ignore unknown top-level keys for forward compatibility.
SCHEMA_VERSION = 1

