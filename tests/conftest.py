"""Keep the test run away from the real dashboard data (users, settings, stats)."""
import os
import tempfile

os.environ.setdefault("HBLINK4_DASH_DATA_DIR", tempfile.mkdtemp(prefix="hblink4-dash-test-"))
