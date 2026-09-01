"""Stand up whatever vendors a sources repo declares.

THE PLATFORM OWNS THE MECHANISM, THE DECLARATION OWNS THE CONTENT. This file
knows how to run an OpenAPI simulator and a CDC stack; it does not know that
Contoso exists, how many vendors there are, or what any of them serve. Point it
at a different `sources.yaml` and it stands up those vendors instead.

That is not tidiness, it is the reason this platform's numbers can be compared
with the Fabric one's at all. Both consume `contoso-sources`, so both pull the
same vendor bytes from the same pinned simulator -- and gold agreeing across
two engines means something only if the inputs were identical. Hand-writing a
vendor block here would make this platform's data ITS OWN, and the comparison
would be measuring the fixtures rather than the runtimes.

Emits a compose fragment on stdout rather than starting anything itself, so the
services join the same project, network and lifecycle as the rest of the stack
and `make down` really does take everything with it.

PUBLISHED PORTS, unlike the Airflow platform's copy of this generator, which
only `expose`s them. There the consumer is a worker container on the compose
network; here every ingest step runs on the operator's host, so a vendor that
is not published is a vendor this platform cannot reach.
"""
from __future__ import annotations

import json
import pathlib
import sys

# The host side of each vendor's published port. HOST PORTS ARE THIS
# PLATFORM'S, not the vendor's -- the declaration names the port the vendor
# listens on inside its own container, and two platforms running side by side
# must not fight over the host. This block is 181xx/19094/55434, chosen to sit
# clear of fabric-platform-notebook-pipelines's 180xx/19092/55432.
HOST_BASE = 18290
ERP_DB_HOST_PORT = 55436
ERP_BROKER_HOST_PORT = 19096
ERP_CONNECT_HOST_PORT = 18293


def _load(path: pathlib.Path) -> dict:
    """Read sources.yaml without a YAML dependency.

    The declaration is a small, flat document, so a minimal reader is cheaper
    than adding PyYAML to a platform that otherwise needs none -- and it FAILS
    on anything it does not understand rather than guessing, because a silently
    skipped vendor would surface much later as an empty landing directory.
    """
    vendors: list[dict] = []
    current: dict | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() in ("vendors:",) or line.startswith("version:"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            current = {}
            vendors.append(current)
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        current[key.strip()] = value
    return {"vendors": vendors}


def fragment(decl: dict, sources_dir: str, pins: dict) -> dict:
    services: dict = {}
    http_index = 0
    for v in decl["vendors"]:
        name = v["name"].replace("_", "-")
        kind = v.get("kind")
        if kind == "openapi":
            host_port = HOST_BASE + http_index
            http_index += 1
            services[name] = {
                "image": f"mokapi/mokapi:{pins['MOKAPI_VERSION']}",
                # The dashboard retains every request AND its response body. For
                # a 95 MB export that is a multi-hundred-MB copy per call, so the
                # history is capped at one entry per API -- this flag exists
                # because a container was being OOM-killed mid-response.
                "command": ["--event-store-default-size=1",
                            f"/sources/{v['spec']}", f"/sources/{v['script']}"],
                # Go does not read the cgroup limit; without GOMEMLIMIT the heap
                # climbs past mem_limit and the container dies mid-response.
                # SIZED TO THIS VENDOR, from the declaration. These were
                # hardcoded 2GiB/4g for every vendor, which gave the 4 KB
                # reference feed the same budget as the 95 MB POS export --
                # wasteful rather than wrong, but it also meant contoso-sources
                # could state a vendor's budget and this platform would ignore
                # it. The Fabric platform reads them; so does this one now.
                "environment": {"GOMEMLIMIT": v.get("memory", "1GiB")},
                "mem_limit": v.get("mem_limit", "2g"),
                # A SEATBELT, added after this platform lost a vendor silently.
                # mokapi was OOM-killed serving the POS orders export (exit 137,
                # OOMKilled=true) and, with no restart policy, simply stayed
                # dead. Nothing noticed for 33 minutes, until the next ingest
                # met `Connection refused` on a vendor that had been up when the
                # run started.
                #
                # This is NOT the fix for the OOM itself -- GOMEMLIMIT above is
                # -- and a vendor that dies repeatedly should be investigated
                # rather than restarted forever. It is here so that a death is
                # survivable and visible, instead of a stack that looks up while
                # one service is gone.
                "restart": "unless-stopped",
                "volumes": [f"{sources_dir}:/sources:ro"],
                "ports": [f"{host_port}:{v['port']}"],
                # HEALTHY MEANS THE VENDOR ENFORCES ITS CREDENTIAL, not that
                # a port is open. Without its fixture mokapi does not fail: it
                # GENERATES bodies from the OpenAPI schema and answers every
                # request 200, wrong key included. A probe against `/` cannot
                # tell those apart, and one reporting "healthy" for a vendor
                # serving invented data is worse than none.
                #
                # The route comes from the declaration, because which path
                # enforces a credential is a fact about the vendor's API. wget
                # exits non-zero on 401, which is the healthy case here -- hence
                # the inverted test.
                "healthcheck": {
                    "test": ["CMD-SHELL",
                             (f"wget -q -O /dev/null "
                             f"--header='X-Api-Key: definitely-not-the-key' "
                             f"http://localhost:{v['port']}{v['health']} "
                             f"&& exit 1 || exit 0")],
                    "interval": "10s", "timeout": "5s", "retries": 5,
                } if v.get("health") else {
                    "test": ["CMD-SHELL",
                             (f"wget -q -O /dev/null http://localhost:{v['port']}/ "
                             f"|| test $? -ne 4")],
                    "interval": "10s", "timeout": "5s", "retries": 5,
                },
            }
        elif kind == "cdc":
            # THREE SERVICES, because a change stream needs all three and any
            # two of them is a snapshot wearing a stream's name. The database
            # holds the rows, Debezium reads its write-ahead log, and the broker
            # carries what Debezium produced. Standing up only Postgres would
            # serve rows -- possibly even the right count -- while testing
            # something else entirely.
            db, broker, connect = f"{name}-db", f"{name}-broker", f"{name}-connect"
            services[db] = {
                "image": f"postgres:{pins['POSTGRES_VERSION']}",
                # LOGICAL replication, and the slots to hold it. Debezium reads
                # the WAL; at the default `replica` level there is nothing in it
                # for a decoder to read and the connector attaches to silence.
                "command": ["postgres", "-c", "wal_level=logical",
                            "-c", "max_replication_slots=4", "-c", "max_wal_senders=4"],
                "environment": {"POSTGRES_USER": v.get("db_user", "contoso"),
                                "POSTGRES_PASSWORD": v.get("db_password", "contoso-erp-dev"),
                                "POSTGRES_DB": v.get("db_name", "erp")},
                "ports": [f"{ERP_DB_HOST_PORT}:5432"],
                "healthcheck": {
                    "test": ["CMD-SHELL",
                             f"pg_isready -U {v.get('db_user','contoso')} -d {v.get('db_name','erp')}"],
                    "interval": "5s", "timeout": "3s", "retries": 20},
                "volumes": [f"{sources_dir}:/sources:ro"],
            }
            services[broker] = {
                "image": f"docker.redpanda.com/redpandadata/redpanda:{pins['REDPANDA_VERSION']}",
                # TWO LISTENERS, and both are needed. A broker tells clients
                # where to reconnect, so a single `broker:9092` advertisement is
                # correct for Debezium (on the compose network) and unusable
                # from the host, which cannot resolve that name -- and the
                # ingest step that consumes this stream runs on the host. The
                # failure without it is librdkafka's `Host resolution failure`,
                # which names the symptom and not the listener.
                "command": ["redpanda", "start", "--mode=dev-container", "--smp=1",
                            ("--kafka-addr=INTERNAL://0.0.0.0:9092,"
                            f"EXTERNAL://0.0.0.0:{ERP_BROKER_HOST_PORT}"),
                            (f"--advertise-kafka-addr=INTERNAL://{broker}:9092,"
                            f"EXTERNAL://localhost:{ERP_BROKER_HOST_PORT}")],
                "ports": [f"{ERP_BROKER_HOST_PORT}:{ERP_BROKER_HOST_PORT}"],
                "healthcheck": {"test": ["CMD-SHELL", "rpk cluster health | grep -q 'Healthy:.*true'"],
                                "interval": "5s", "timeout": "5s", "retries": 30},
            }
            services[connect] = {
                "image": f"debezium/connect:{pins['DEBEZIUM_VERSION']}",
                "depends_on": {db: {"condition": "service_healthy"},
                               broker: {"condition": "service_healthy"}},
                "environment": {
                    "BOOTSTRAP_SERVERS": f"{broker}:9092",
                    "GROUP_ID": v["name"],
                    "CONFIG_STORAGE_TOPIC": "_connect_configs",
                    "OFFSET_STORAGE_TOPIC": "_connect_offsets",
                    "STATUS_STORAGE_TOPIC": "_connect_status",
                    # One partition each: ordering per key is what CDC
                    # guarantees, and more partitions would trade that away for
                    # throughput this does not need.
                    "CONFIG_STORAGE_REPLICATION_FACTOR": "1",
                    "OFFSET_STORAGE_REPLICATION_FACTOR": "1",
                    "STATUS_STORAGE_REPLICATION_FACTOR": "1"},
                "ports": [f"{ERP_CONNECT_HOST_PORT}:8083"],
                "healthcheck": {"test": ["CMD-SHELL", "curl -sf http://localhost:8083/connectors || exit 1"],
                                "interval": "10s", "timeout": "5s", "retries": 30},
            }
            if v.get("seed"):
                # THE SEEDER MAKES THE VENDOR EXIST -- it registers the Debezium
                # connector and then replays the fixture's history as DML, so
                # the stream is CAPTURED rather than described. It belongs to
                # the sources repo; the platform only runs it.
                services[f"{name}-seed"] = {
                    "image": f"python:{pins.get('PYTHON_VERSION', '3.12')}-slim",
                    "depends_on": {db: {"condition": "service_healthy"},
                                   connect: {"condition": "service_healthy"}},
                    "environment": {
                        "ERP_DSN": (f"host={db} port=5432 dbname={v.get('db_name','erp')} "
                                    f"user={v.get('db_user','contoso')} "
                                    f"password={v.get('db_password','contoso-erp-dev')}"),
                        "ERP_CONNECT_URL": f"http://{connect}:8083",
                        "ERP_DB_HOST": db,
                        "PYTHONUNBUFFERED": "1",
                    },
                    "volumes": [f"{sources_dir}:/sources:rw"],
                    "working_dir": "/sources",
                    # `restart: no` and a one-shot command: this is a step, not
                    # a service, and it must not loop if the replay fails.
                    #
                    # --frozen --no-sync is load-bearing. A bare `uv run`
                    # RE-SYNCS and prunes anything absent from the lock, which
                    # evicts the generators and psycopg installed moments
                    # earlier and then fails with ModuleNotFoundError for a
                    # package that was just there. And psycopg goes in AFTER
                    # fixtures.py, because that script calls `uv sync` itself.
                    "command": ["sh", "-c",
                                ("pip install --quiet uv && "
                                "uv sync --quiet && "
                                "uv run --frozen --no-sync python scripts/fixtures.py && "
                                "uv pip install --quiet 'psycopg[binary]' && "
                                "uv run --frozen --no-sync python scripts/seed_erp.py")],
                    "restart": "no",
                }
        else:
            raise SystemExit(
                f"platform: vendor {v['name']!r} declares kind={kind!r}, which this "
                f"platform does not know how to run. Add it here or fix the "
                f"declaration; guessing would stand up the wrong vendor.")
    return {"services": services}


def host_ports(frag: dict) -> dict:
    """{service: [host ports]} for everything this fragment publishes.

    DERIVED FROM THE FRAGMENT, not from a second table beside it. A function
    that re-listed the host ports here would be a copy free to drift from what
    compose is actually handed, which is the whole reason this is written down.

    It exists because the fragment is GENERATED at `make up` and gitignored, so
    nothing committed anywhere recorded these ports: the family registry could
    not see them, and neither could the check that refuses two members claiming
    one host port.
    """
    out: dict[str, list[int]] = {}
    for name, svc in frag.get("services", {}).items():
        for mapping in svc.get("ports", []):
            host = str(mapping).split(":")[0]
            if host.isdigit():
                out.setdefault(name, []).append(int(host))
    return {k: sorted(v) for k, v in sorted(out.items())}


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[3] == "--ports":
        sys.argv = sys.argv[:3]
        ports = True
    else:
        ports = False
    if len(sys.argv) != 3:
        sys.exit("usage: sources.py <path-to-sources.yaml> <sources-dir-abs>")
    decl = _load(pathlib.Path(sys.argv[1]))
    if not decl["vendors"]:
        sys.exit("platform: that sources.yaml declares no vendors")
    # Every image this platform starts on a product's behalf is pinned by the
    # SOURCES repo, never by versions.env here. A platform defaulting any of
    # them would be deciding what the vendor IS -- and two platforms on
    # different mokapis are not pulling from the same vendor even if the specs
    # match, which would quietly invalidate the cross-runtime comparison.
    versions = pathlib.Path(sys.argv[2]) / "versions.env"
    pins = dict(
        line.split("=", 1) for line in versions.read_text().splitlines()
        if "=" in line and not line.strip().startswith("#")
    ) if versions.exists() else {}
    pins = {k.strip(): val.strip() for k, val in pins.items()}
    needed = {"openapi": ["MOKAPI_VERSION"],
              "cdc": ["POSTGRES_VERSION", "REDPANDA_VERSION", "DEBEZIUM_VERSION"]}
    for v in decl["vendors"]:
        for key in needed.get(v.get("kind"), []):
            if key not in pins:
                sys.exit(f"platform: vendor {v['name']!r} is kind={v.get('kind')!r} but "
                         f"{versions} does not pin {key}; this platform will not guess it")
    frag = fragment(decl, sys.argv[2], pins)
    print(json.dumps(host_ports(frag) if ports else frag, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
