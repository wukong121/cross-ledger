"""Run a one-off command inside one of the solution's Container Apps Jobs (same identity, network and
image as the real job) and print its console output.

    uv run --project app python scripts/job_exec.py <transfer|collector|seed> -- <command ...>

Used for PoC evidence that can only be produced from inside the VNet (e.g. the private audit
account). The job template is copied and only the command is replaced, so env vars stay intact.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def az(*args: str, body: dict | None = None):
    cmd = ["az", *args, "-o", "json"]
    if body is not None:
        cmd += ["--body", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr)
    return json.loads(r.stdout or "null")


def main() -> int:
    if "--" not in sys.argv or len(sys.argv) < 4:
        print(__doc__)
        return 2
    sep = sys.argv.index("--")
    key, cmd = sys.argv[1], sys.argv[sep + 1 :]
    tf = json.loads(
        subprocess.run(
            ["terraform", f"-chdir={ROOT / 'infra'}", "output", "-json"], capture_output=True, text=True, check=True
        ).stdout
    )
    env = dict(ln.split("=", 1) for ln in (ROOT / ".xl.env").read_text().splitlines() if "=" in ln)
    rg, job = tf["resource_groups"]["value"]["gov"], tf["jobs"]["value"][key]
    jid = f"/subscriptions/{env['XL_SUBSCRIPTION_ID']}/resourceGroups/{rg}/providers/Microsoft.App/jobs/{job}"
    url = f"https://management.azure.com{jid}"
    c = az("rest", "--method", "get", "--url", f"{url}?api-version=2025-01-01")["properties"]["template"]["containers"][0]
    c["command"], _ = cmd, c.pop("args", None)
    ex = az("rest", "--method", "post", "--url", f"{url}/start?api-version=2025-01-01", body={"containers": [c]})
    name = ex["name"]
    while True:
        st = az("rest", "--method", "get", "--url", f"{url}/executions/{name}?api-version=2025-01-01")["properties"]["status"]
        if st in ("Succeeded", "Failed", "Stopped", "Degraded"):
            break
        time.sleep(10)
    # console logs reach Log Analytics a little later
    q = (
        f"ContainerAppConsoleLogs_CL | where ContainerGroupName_s startswith '{name}' "
        "| order by TimeGenerated asc, _timestamp_d asc | project Log_s"
    )
    for _ in range(30):
        r = subprocess.run(
            ["az", "monitor", "log-analytics", "query", "-w", env["XL_WORKSPACE_ID"], "--analytics-query", q,
             "--timespan", "PT2H", "-o", "tsv"],
            capture_output=True, text=True,
        )
        lines = [ln.split("\t")[0] for ln in r.stdout.splitlines() if ln.strip()]
        if lines:
            break
        time.sleep(20)
    print("\n".join(lines))
    return 0 if st == "Succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
