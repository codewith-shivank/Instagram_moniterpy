import os
from pathlib import Path

os.environ.setdefault("DISCORD_TOKEN", "unit-test-token")
os.environ.setdefault(
    "STATE_FILE",
    str(Path(os.environ.get("TMPDIR", Path.cwd())) / "instagram-monitor-tests.db"),
)
