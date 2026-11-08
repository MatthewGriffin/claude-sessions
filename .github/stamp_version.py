"""Write a release's version into the app: stamp_version.py v1.2.3 claude-monitor"""
import re
import sys

tag, path = sys.argv[1], sys.argv[2]
match = re.fullmatch(r"v(\d+\.\d+\.\d+)", tag)
if not match:
    sys.exit(f"Release tags look like v1.2.3, not {tag}")
with open(path, encoding="utf-8", newline="") as handle:
    source = handle.read()
stamped, count = re.subn(r'^APP_VERSION = "dev"$', f'APP_VERSION = "{match.group(1)}"', source, count=1, flags=re.M)
if count != 1:
    sys.exit("APP_VERSION = \"dev\" wasn't found")
with open(path, "w", encoding="utf-8", newline="") as handle:
    handle.write(stamped)
print(f"Version {match.group(1)}")
