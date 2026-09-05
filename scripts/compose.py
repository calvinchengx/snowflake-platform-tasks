#!/usr/bin/env python3
"""Assemble compose files. Logic lives here so the Makefile survives cmd.exe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = "snowflake-tasks"
ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "compose" / ".generated"
FILES = ["compose/docker-compose.yml"]
if os.environ.get("GOVERNANCE", "1") == "1":
    FILES.append("compose/governance.yml")


def sources_dir() -> Path:
    """The contoso-sources checkout this stack pulls its vendors from.

    A SIBLING PATH, and the one place in this repository where that is right:
    the vendors are not a dependency, they are the world outside, mounted into
    containers as bytes rather than imported as code.
    """
    return Path(os.environ.get("SOURCES", ROOT.parent / "contoso-sources")).resolve()


def vendor_fragment() -> Path:
    """Generate the vendor compose fragment from the sources declaration.

    THIS CELL HAD NO SOURCE SYSTEMS AT ALL until now -- it seeded empty silver
    tables and built gold over them. The family's numbers are only comparable
    because every cell pulls the SAME bytes from the SAME pinned simulator, so a
    Snowflake cell without vendors could never produce evidence, only shapes.
    """
    src = sources_dir()
    decl = src / "sources.yaml"
    if not decl.exists():
        sys.exit(
            f"no vendor declaration at {decl}.\n\n"
            f"Clone calvinchengx/contoso-sources beside this repository, or set "
            f"SOURCES=/path/to/contoso-sources."
        )
    data = src / "_data"
    if not data.is_dir() or not any(data.iterdir()):
        sys.exit(
            f"{data} is empty -- the vendors have no bytes to serve.\n\n"
            f"Run `make sources` in {src} first. Without it mokapi does not\n"
            f"fail: it generates bodies from the OpenAPI schema and answers\n"
            f"every request 200, so this pipeline would land invented data."
        )
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / "sources.json"
    out.write_text(
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "sources.py"), str(decl), str(src)],
            check=True, capture_output=True, text=True,
        ).stdout,
        encoding="utf-8",
    )
    return out


def main() -> int:
    args = sys.argv[1:]
    # NAMED, NOT INFERRED. Without `-p`, docker derives the project name from
    # the directory holding the compose file -- which here is `compose/`, a name
    # this repository chose for tidiness and not as an identifier.
    #
    # `databricks-platform-jobs` keeps its compose file in a directory called
    # `compose/` too, so both stacks were project `compose` and either
    # platform's `make down` tore down the other's containers. Measured, not
    # imagined: bringing this stack down took `compose-databricks-1` with it,
    # and the only reason it was noticed is that `down` then refused to remove
    # the network with "Resource is still in use". Had the other run been
    # mid-flight it would have read as a container dying for no reason, with
    # nothing in that repository's logs to explain it.
    #
    # THE GENERIC NAME IS THE DEFECT, not the pair. Naming only one side fixes
    # this collision and leaves the next platform that keeps a `compose/`
    # directory to rediscover it. The three Airflow platforms in this family
    # already pass their own `-p`; the engine-native ones never did.
    #
    # NOTE ON UPGRADING: a stack started under the old name is invisible to this
    # one. Run `make down` on the previous version first, or remove the leftover
    # `compose`-project containers by hand.
    cmd = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "--env-file",
        "versions.env",
        "--profile",
        "governance",
    ]
    for f in FILES:
        cmd.extend(["-f", f])
    # The vendors come last, generated from contoso-sources at every
    # invocation, so a vendor added over there is stood up here without an edit.
    cmd.extend(["-f", str(vendor_fragment().relative_to(ROOT))])
    cmd.extend(args)
    env = os.environ.copy()
    env.setdefault("SNOWFLAKE_DATA", str(ROOT / "data"))
    Path(env["SNOWFLAKE_DATA"]).mkdir(parents=True, exist_ok=True)
    os.chmod(env["SNOWFLAKE_DATA"], 0o777)
    # The internal stage, host-side. 0777 for the same reason as data/: the
    # emulator runs as nonroot and has to write the files ingest puts here.
    env.setdefault("SNOWFLAKE_STAGES", str(ROOT / "stages"))
    Path(env["SNOWFLAKE_STAGES"]).mkdir(parents=True, exist_ok=True)
    os.chmod(env["SNOWFLAKE_STAGES"], 0o777)
    rc = subprocess.call(cmd, cwd=ROOT, env=env)
    if args and args[0] == "up":
        rc = wait_for_jobs(cmd[:-len(args)], env, rc)
        if rc != 0:
            dump_failure(cmd[:-len(args)], env)
    return rc


def wait_for_jobs(base: list[str], env: dict, rc: int) -> int:
    """`up --wait` starts the one-shot jobs. It does not wait for them to DO
    anything, and the next step needs them finished.

    Two services here are steps rather than servers -- `contoso-erp-seed`
    replays the vendor's history into its database, and `om-migrate` migrates
    OpenMetadata's schema. Compose declares both `restart: no`, which is how
    this function finds them without matching on a name.

    `--wait` gets them wrong in BOTH directions, and this repository has now
    been bitten by each:

      a job that has FINISHED is "not running", so `up --wait` failed on a
      stack that had come up correctly -- and in CI that stops the run before
      `make verify`, reporting the failure against a step that never executed;

      a job that is STILL RUNNING is "started", so `up` returned while the ERP
      seed was mid-replay and ingest read a database with nothing in it. That
      is the failure a first fix here made WORSE, by accepting `running` as
      good enough for a job whose whole purpose is to finish.

    So the wait is on completion: every `restart: no` service must have exited,
    and exited 0. Bounded, because a seed that never finishes is a fault to
    report rather than to hang on.
    """
    jobs = one_shot_services(base, env)
    if not jobs:
        return rc
    deadline = time.time() + 600.0
    while True:
        states = service_states(base, env)
        if states is None:
            return rc
        pending = [j for j in jobs if states.get(j, ("", 0))[0] != "exited"]
        failed = [f"{j}: exited {states[j][1]}" for j in jobs
                  if states.get(j, ("", 0))[0] == "exited" and states[j][1] != 0]
        if failed:
            print("compose: " + "; ".join(failed))
            return rc or 1
        if not pending:
            break
        if time.time() >= deadline:
            print(f"compose: still running after 600s: {', '.join(pending)}")
            return rc or 1
        time.sleep(2.0)

    # A service that has exited 0 is fine whether or not compose declares it
    # `restart: no`. om-migrate does not, and flagging it broken for finishing
    # its job was this function's first mistake in the other direction.
    broken = [f"{n}: {s} ({c})" for n, (s, c) in service_states(base, env).items()
              if s not in ("running", "restarting") and not (s == "exited" and c == 0)]
    if broken:
        print("compose: " + "; ".join(broken))
        return rc
    print(f"compose: up -- services running, jobs finished ({', '.join(sorted(jobs))})")
    return 0


def one_shot_services(base: list[str], env: dict) -> set[str]:
    """Services compose declares `restart: no` -- steps, not servers."""
    out = subprocess.run(base + ["config", "--format", "json"],
                         cwd=ROOT, env=env, capture_output=True, text=True,
                         check=False)
    if out.returncode != 0:
        return set()
    try:
        cfg = json.loads(out.stdout)
    except json.JSONDecodeError:
        return set()
    return {n for n, s in cfg.get("services", {}).items() if s.get("restart") == "no"}


def service_states(base: list[str], env: dict):
    """{service: (state, exit_code)} for everything compose knows about."""
    ps = subprocess.run(base + ["ps", "-a", "--format", "json"],
                        cwd=ROOT, env=env, capture_output=True, text=True,
                        check=False)
    if ps.returncode != 0 or not ps.stdout.strip():
        return None
    states = {}
    for line in ps.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            svc = json.loads(line)
        except json.JSONDecodeError:
            return None
        states[svc.get("Service", "?")] = (svc.get("State", ""), svc.get("ExitCode", 0))
    return states


def dump_failure(base: list[str], env: dict) -> None:
    """What the containers said on the way down.

    `compose up` resolves depends_on itself and reports only `dependency
    failed to start` -- WHICH container, and nothing about why it exited. That
    is how G48, a released emulator that did not boot in a sibling stack,
    survived a release and three CI runs without a single line of diagnosis.
    The logs exist at this moment and are gone as soon as anyone runs
    `make down`, which CI does in its cleanup step.

    `ps -a` first because it names which container died and with what code; the
    logs then say what it said on the way out. Both are bounded (`--tail`) so a
    noisy stack cannot bury the failure they exist to explain.

    check=False throughout, and no return value: this runs on a path that is
    already failing, and a diagnostic that can raise would replace the failure
    it was called to explain.
    """
    print("platform: the stack did not come up. what the containers said:",
          flush=True)
    subprocess.run(base + ["ps", "-a"], cwd=ROOT, env=env, check=False)
    subprocess.run(base + ["logs", "--no-color", "--tail=80"],
                   cwd=ROOT, env=env, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
