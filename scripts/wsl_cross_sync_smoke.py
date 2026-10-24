# 主站容器內執行：跨服 sync/snapshot 煙測
import json
import os
import sys
import urllib.request

os.chdir("/workspace")
sys.path.insert(0, "/workspace")
os.environ["ALIOTH_MOBILE_DEMO_DATA_DIR"] = "/workspace/.alioth-mobile-demo"

from services.api.app.api.v1.mobile_demo_remote import (  # noqa: E402
    check_executor_health,
    pull_run_from_executor,
    push_run_to_executor,
)

base = os.environ.get("ALIOTH_VERIFY_SANDBOX", "http://host.docker.internal:8765")
rid = os.environ["ALIOTH_VERIFY_RUN_ID"]

print("HEALTH", check_executor_health(base).get("ok"))
push_run_to_executor(rid, base)
print("SYNC_OK")
snap = json.loads(
    urllib.request.urlopen(base + f"/runs/{rid}/snapshot", timeout=20).read().decode()
)
print("SNAPSHOT", snap.get("ok"), (snap.get("run") or {}).get("status"))
print("PULL", pull_run_from_executor(rid, base))
