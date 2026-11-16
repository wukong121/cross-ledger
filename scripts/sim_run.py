"""Run a shell snippet on the simulation host (az vm run-command) and optionally save a screenshot.

    cd app && uv run python ../scripts/sim_run.py 'rclone lsd overseas:' \
        --capture 20-rclone "title" --prompt 'rclone lsd overseas:'

The host has no inbound ports, so commands go through the Azure control plane.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from capture import render  # noqa: E402

RG = os.environ.get("SIM_RG", "rg-xl-sim-host")
VM = os.environ.get("SIM_VM", "vm-xl-sim")


def run_on_host(script: str) -> str:
    res = subprocess.run(
        [
            "az",
            "vm",
            "run-command",
            "invoke",
            "-g",
            RG,
            "-n",
            VM,
            "--command-id",
            "RunShellScript",
            "--scripts",
            f"export HOME=/root; cd /root; {script} 2>&1",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    msg = json.loads(res.stdout)["value"][0]["message"]
    out = msg.split("[stdout]\n", 1)[-1].split("\n[stderr]", 1)[0]
    # rclone timestamps are noise in a screenshot
    return re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ", "", out.rstrip(), flags=re.M)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--capture", nargs=2, metavar=("NAME", "TITLE"))
    ap.add_argument("--prompt", help="what to show after `$` (defaults to the script)")
    ap.add_argument("--width", type=int, default=170)
    a = ap.parse_args()
    out = run_on_host(a.script)
    print(out)
    if a.capture:
        render(a.capture[0], a.capture[1], f"[sim-host] {a.prompt or a.script}", out, width=a.width)


if __name__ == "__main__":
    main()
