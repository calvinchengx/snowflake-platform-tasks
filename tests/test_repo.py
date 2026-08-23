"""Repo-boundary tests. No Docker, no emulator, no product.

Since the split this repository is a PLATFORM: it stands up the stack and runs
whatever product it is pointed at. The tests that read step code moved to
contoso-data-product-snowflake-tasks with the code they describe.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_pins() -> dict[str, str]:
    out = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_pins_are_immutable():
    pins = read_pins()
    assert "SNOWFLAKE_EMULATOR_VERSION" in pins
    mutable = {"latest", "stable", "main", "edge"}
    for k, v in pins.items():
        assert v.lower() not in mutable, f"{k}={v}"


def test_compose_reads_every_pin():
    composed = "".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "compose").glob("*.yml")
    )
    for k in read_pins():
        assert "${" + k in composed, k


def test_makefile_survives_cmd_exe():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    for bad in (" | ", " && ", " `", " rm "):
        for line in text.splitlines():
            if line.startswith("#") or line.startswith("ifeq") or line.startswith("  SHELL"):
                continue
            if ":" in line and not line.startswith("\t") and not line.startswith(" "):
                continue
            if line.startswith("\t"):
                assert bad not in line, f"cmd.exe-unsafe recipe: {line!r}"


def test_set_release_moves_only_the_emulator_pin():
    mod = load("set_release")
    text = "SNOWFLAKE_EMULATOR_VERSION=0.1.0\nOPENMETADATA_VERSION=1.13.2\n"
    new, moved = mod.set_version(text, "0.2.0")
    assert moved == {"SNOWFLAKE_EMULATOR_VERSION": "0.1.0"}
    assert "SNOWFLAKE_EMULATOR_VERSION=0.2.0" in new
    assert "OPENMETADATA_VERSION=1.13.2" in new


def test_the_platform_holds_no_product():
    """This repository used to contain its own product.

    Thirteen step modules -- ingest through govern -- sat in `platform/`, with
    the dbt profiles beside them. That made this cell's name a half-truth and
    made "a second product can use this platform unchanged" untestable, because
    there was no second thing to point it at.

    The split line is 00-family.md's: a platform holds no Contoso name and no
    product file. The steps live in contoso-data-product-snowflake-tasks.
    """
    assert not (ROOT / "platform").exists(), (
        "a platform/ directory is back -- the product's steps belong in the leaf"
    )
    for gone in ("gold/dbt_project.yml", "gold/profiles.yml", "silver/profiles.yml"):
        assert not (ROOT / gone).exists(), (
            f"{gone} is a product file: dbt runs from the product's directory, "
            f"so a copy here is one nothing reads and everything can diverge from"
        )
    # A DEFAULT NAMING CONTOSO would be the same coupling in one line. The
    # Makefile may name the VENDORS repo -- it consumes one -- but never a
    # product, and not in a variable name either: the stage the product writes
    # to is PRODUCT_STAGE, which says what it is rather than whose it is.
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for line in makefile.splitlines():
        code = line.split("#", 1)[0]
        if "contoso" in code.lower() and "contoso-sources" not in code:
            raise AssertionError(f"the Makefile names a product: {line.strip()!r}")

    # THE MAKEFILE WAS NOT ENOUGH in the Databricks split: a whole dbt PROJECT
    # survived it sitting in gold/, byte-identical to the product's copy and
    # naming `contoso_gold`. A dbt project is a product artefact -- it declares
    # models, materializations and a profile, which are the product's decisions.
    #
    # ./product is exempt: a dbt project appearing THERE is the product being
    # run, not the platform holding one.
    #
    # ./stages is exempt for the same reason one step further out: it is the
    # emulator's INTERNAL STAGE, gitignored, and the only route into it is the
    # driver's PUT. Now that silver and gold run as `EXECUTE DBT PROJECT`, the
    # step uploads the product's project there and a real `dbt_project.yml`
    # appears under `stages/silver_project/` on every run. That is the product
    # being RUN, held by the warehouse -- nothing there is authored here and
    # nothing there is committed.
    #
    # Exempted by name rather than by widening the glob, because the thing this
    # test exists to catch -- a project checked in under `gold/`, which is
    # exactly how one survived the split -- must still fail.
    exempt = {"product", "stages"}
    strays = [
        d.relative_to(ROOT).as_posix()
        for d in ROOT.rglob("dbt_project.yml")
        if d.relative_to(ROOT).parts[0] not in exempt and ".venv" not in d.parts
    ]
    assert not strays, f"a dbt project is still in the platform: {strays}"


def test_the_product_is_supplied_as_a_path():
    """PRODUCT is how the platform learns what to run, and it is a PATH.

    A name would mean this platform could only ever run one product, which is
    the property the split exists to remove.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^PRODUCT \?= \./product$", makefile, re.M), (
        "PRODUCT must default to the ./product mount point"
    )
    assert "--directory $(PRODUCT)" in makefile, (
        "steps must run in the product's own directory, so dbt uses the "
        "product's lock and outputs land there"
    )


def test_the_platform_declares_no_runtime_dependency():
    """What is left here reaches for nothing outside the standard library.

    The declarations went to the product with the code that imports them, and
    the thirteen advisories fixed in #9 went with them -- they all arrived
    through dbt-snowflake, and dbt now runs from the product's lock. Keeping
    them here would keep a vulnerable tree alive in a lockfile that installs
    nothing.
    """
    proj = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in proj
    for gone in ("snowflake-target", "contoso-data-product", "dbt-snowflake"):
        for line in proj.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert gone not in line, f"{gone} is the product's dependency, not this one"


def test_the_stage_the_warehouse_mounts_is_the_one_the_product_writes():
    """The coupling the split exposed, and the reason it is one variable.

    Ingest writes the vendors' bytes into the internal stage; the warehouse --
    a container this platform runs -- reads them back through COPY INTO. While
    both halves lived here they spelled it `<repo>/stages` and agreed by
    accident. If the mount and PRODUCT_STAGE ever name different directories,
    ingest writes where COPY INTO cannot look, and the symptom is an EMPTY
    BRONZE rather than an error.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    values = {}
    for var in ("PRODUCT_STAGE", "SNOWFLAKE_STAGES"):
        m = re.search(rf"^export {var} := (.+)$", makefile, re.M)
        assert m, f"{var} is not exported -- the product would fall back to its own guess"
        values[var] = m.group(1).strip()
    assert values["PRODUCT_STAGE"] == values["SNOWFLAKE_STAGES"], (
        f"the warehouse mounts {values['SNOWFLAKE_STAGES']} and the product "
        f"writes {values['PRODUCT_STAGE']}"
    )


def test_verify_checks_the_product_pin_before_running_a_step():
    """The two pins live in two repositories now, so the check runs where both exist.

    versions.env pins the emulator IMAGE; the product pins the client WHEEL.
    Nothing in either repository alone can see the pair -- `make verify` is the
    moment it exists, because the platform has been pointed at a product.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    body = makefile[makefile.index("verify:") :]
    recipe = [ln for ln in body.splitlines()[1:] if ln.startswith("\t")]
    assert recipe, "verify has no recipe"
    assert "check_product_pin.py" in recipe[0], (
        "the pin check must run BEFORE the first step, or the run reports a "
        "client/image mismatch as a failure four steps deep"
    )


def test_check_product_pin_refuses_a_client_from_another_release(tmp_path):
    """Checked against a disagreement, not just against the happy path."""
    script = ROOT / "scripts" / "check_product_pin.py"
    version = read_pins()["SNOWFLAKE_EMULATOR_VERSION"]
    good = (
        "snowflake-target = { url = "
        f'"https://github.com/calvinchengx/snowflake-emulator/releases/download/'
        f'v{version}/snowflake_target-0.1.0-py3-none-any.whl" }}\n'
    )
    stale = good.replace(f"/v{version}/", "/v0.0.1/")

    for name, content, expected in (
        ("agreeing", good, 0),
        ("stale", stale, 1),
        ("silent", "dependencies = []\n", 1),
    ):
        product = tmp_path / name
        product.mkdir()
        (product / "pyproject.toml").write_text(content, encoding="utf-8")
        (product / "uv.lock").write_text(content, encoding="utf-8")
        rc = subprocess.call(
            [sys.executable, str(script), str(product)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert rc == expected, f"{name} product: expected exit {expected}, got {rc}"


def test_the_acceptance_run_checks_out_the_product_it_runs():
    """`make verify` with no PRODUCT would run the empty ./product mount point.

    It would fail at the first step rather than quietly verifying nothing, but
    it would fail for a confusing reason.
    """
    wf = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    for repo in (
        "calvinchengx/contoso-data-product-snowflake-tasks",
        # compose.py hard-requires this checkout to materialise the vendors.
        "calvinchengx/contoso-sources",
    ):
        assert repo in wf, f"the acceptance run does not check out {repo}"
    assert "make verify PRODUCT=../contoso-data-product-snowflake-tasks" in wf


def test_the_acceptance_run_asserts_the_numbers_and_not_only_the_run():
    """A nightly that proves the pipeline RAN proves nothing about the answer.

    G50: across all seven platforms with an acceptance workflow, none compared
    a snapshot against an expected value. `make verify` writes
    product_snapshot.json and nothing read it back, so gold could have returned
    different money indefinitely behind a green tick.

    The core checkout must be PINNED. Left tracking main, this cell's
    expectations could move without a reviewed commit here -- a nightly that
    another repository can turn green.
    """
    wf = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    assert "scripts/assert_snapshot.py" in wf, (
        "the acceptance run never asserts the figures core publishes"
    )
    assert "product_snapshot.json" in wf, (
        "the assert step names no snapshot, so it checks nothing"
    )
    core = wf[wf.index("repository: calvinchengx/contoso-data-product\n") :]
    ref = core[: core.index("path:")]
    assert re.search(r"ref: [0-9a-f]{40}", ref), (
        "the contoso-data-product checkout is not pinned to a commit"
    )


def test_the_acceptance_run_adopts_every_file_the_bump_touches():
    """A half-adopted pin publishes a main that contradicts itself.

    The bump now changes ONE file. pyproject.toml and uv.lock carried the
    client wheel until the split and no longer do -- the wheel is the product's
    pin, which is why check_product_pin.py exists.
    """
    wf = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
    adopt = wf[wf.index("Adopt the version this run just verified") :]
    assert adopt.count("versions.env") >= 2, (
        "the adopt step must both TEST and COMMIT versions.env"
    )
    for gone in ("pyproject.toml", "uv.lock"):
        assert gone not in adopt, (
            f"the adopt step commits {gone}, which the bump no longer touches"
        )


def test_the_compose_project_is_named_rather_than_inferred():
    """Two platforms in this family were both project `compose`.

    Without `-p`, docker derives the project name from the directory holding the
    compose file. This repository keeps its at `compose/`, chosen for tidiness
    and not as an identifier -- and `databricks-platform-jobs` does the same, so
    both stacks were project `compose` and either platform's `make down` tore
    down the other's containers.

    Measured rather than imagined: bringing this stack down took
    `compose-databricks-1` with it, and it was noticed only because `down` then
    refused to remove the network with "Resource is still in use". Had the other
    run been mid-flight it would have read as a container dying for no reason,
    with nothing in that repository's logs to explain it.

    THE GENERIC NAME IS THE DEFECT, not the pair of them. A name derived from a
    directory collides with whatever else picks that directory name next, so
    this asserts that a name is CHOSEN -- and that it is not the inferred one.
    """
    src = (ROOT / "scripts" / "compose.py").read_text(encoding="utf-8")
    assert '"-p"' in src, (
        "the compose project name is inferred from the directory, so this stack "
        "shares a project with any other platform whose compose file sits in a "
        "directory of the same name"
    )
    m = re.search(r'^PROJECT = "([^"]+)"', src, re.M)
    assert m, "no PROJECT constant to pass to -p"
    assert m.group(1) != "compose", (
        "the project is named `compose`, which is the inferred name this exists "
        "to replace"
    )


def test_no_image_comes_from_a_registry_the_family_does_not_trust():
    """G44: OpenMetadata shipped from docker.getcollate.io and took this
    nightly down twice in one morning.

    That registry is backed by neither Docker Hub nor GHCR, and a pull failure
    there reads as a broken governance step rather than as somebody else's
    outage. The images are mirrored into ghcr.io/calvinchengx by
    `calvinchengx/emulators` (`mirrors.json`, `scripts/mirror_images.py`), which
    copies the manifest index and records the digest the registry serves.

    AN ALLOWLIST, NOT A BAN ON ONE NAME. Asserting `getcollate` is absent would
    pass the day somebody adds a different vendor registry, which is the same
    defect one name later. This asks the opposite question: every image must
    come from somewhere the family already depends on being up.

    A VALUE THAT IS ENTIRELY A VARIABLE IS RESOLVED, not skipped. `${X}` hides
    the host completely, so a check that ignored those would be a check with a
    hole exactly where an unreviewed image would sit.
    """
    trusted = {
        # The family's own, and the mirrors it keeps there.
        "ghcr.io",
        # Docker Hub, which is what a bare `name/image` resolves to.
        "docker.io",
        "mcr.microsoft.com",
    }

    env = {}
    for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    def host_of(ref: str) -> str | None:
        head = ref.split("/")[0]
        return head if ("." in head or ":" in head) else "docker.io"

    bad = []
    for path in sorted((ROOT / "compose").rglob("*.yml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            ref = stripped.split(":", 1)[1].strip()
            whole = re.fullmatch(r"\$\{(\w+)(?::[?-][^}]*)?\}", ref)
            if whole:
                name = whole.group(1)
                if name not in env:
                    bad.append(f"{path.name}:{n}: ${{{name}}} is not in versions.env, "
                               f"so nothing here can tell which registry it names")
                    continue
                ref = env[name]
            host = host_of(ref)
            if host not in trusted:
                bad.append(f"{path.name}:{n}: {host} is not a registry the family "
                           f"trusts to be up ({ref})")
    assert not bad, "untrusted registries:\n  " + "\n  ".join(bad)


def test_openmetadata_comes_from_the_mirror():
    """The allowlist above would also pass if OpenMetadata simply vanished.

    So this names the thing G44 is about: the catalog's two images, from the
    family's registry, by the tag versions.env pins.
    """
    gov = (ROOT / "compose" / "governance.yml").read_text(encoding="utf-8")
    images = [ln.strip() for ln in gov.splitlines() if ln.strip().startswith("image:")]
    for name in ("openmetadata-server", "openmetadata-postgresql"):
        assert any(f"ghcr.io/calvinchengx/{name}:" in i for i in images), (
            f"the governance stack does not pull {name} from the family's registry"
        )
    assert not any("getcollate" in i for i in images), (
        "an image still comes straight from the vendor registry"
    )


def test_the_committed_vendor_ports_match_what_the_generator_emits():
    """`vendor-ports.json` is the only committed record of these host ports.

    The vendor compose fragment is GENERATED at `make up` and gitignored, so
    nothing in any repository recorded which host ports it publishes: the
    family registry could not see them, and the check that refuses two members
    claiming one host port was blind to them. This file is what the hub reads;
    this test is what keeps it true.

    IT REFUSES RATHER THAN SKIPS when the declaration is missing. A skip here
    would be invisible in CI, and CI is the only place it matters -- the first
    version skipped, and this repository's test job did not check out
    contoso-sources at all, so the check would have passed by never running.
    The job now places it beside this one, the way the Fabric platform's
    already did.

    Regenerate with:
        uv run --no-project python scripts/sources.py \\
            ../contoso-sources/sources.yaml $(cd ../contoso-sources && pwd) \\
            --ports > vendor-ports.json
    """
    import json
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    sources = pathlib.Path(os.environ.get("SOURCES", root.parent / "contoso-sources"))
    assert (sources / "sources.yaml").is_file(), (
        f"no contoso-sources declaration at {sources}; this test generates the "
        "vendor fragment from it. Clone it beside this repository or set SOURCES."
    )

    out = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "sources.py"),
            str(sources / "sources.yaml"),
            str(sources),
            "--ports",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    emitted = json.loads(out.stdout)
    assert emitted, (
        "the generator published no host ports — this check would be vacuous"
    )
    committed = json.loads((root / "vendor-ports.json").read_text(encoding="utf-8"))
    assert committed == emitted, (
        "vendor-ports.json is stale; regenerate it (see this test's docstring)"
    )
# --- digest pins ---------------------------------------------------------------
#
# Docker IGNORES the tag in `repo:tag@sha256:...` — the digest decides, silently.
# A version bumped without its digest runs the OLD image under the NEW name.

def test_every_image_in_every_compose_file_is_fetched_by_digest():
    """Both files, and every `image:` line in them. Scoping this to a list
    would pass the day a service is added and forgotten, which is exactly when
    the pin is missing."""
    for path in sorted((ROOT / "compose").glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            assert "@${" in stripped and "_DIGEST" in stripped, (
                f"{path.name}: pulled by tag alone: {stripped}")
            assert ":-" not in stripped, f"{path.name}: a default version floats: {stripped}"


def test_one_version_feeding_two_images_has_a_digest_for_each():
    """OPENMETADATA_VERSION tags `openmetadata-postgresql` AND
    `openmetadata-server` — different images, different digests. A single
    OPENMETADATA_DIGEST would pin one and silently mis-pin the other."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from digests import PINS

    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    shared = [p for p, (_i, tag_var) in PINS.items() if tag_var == "OPENMETADATA_VERSION"]
    assert len(shared) == 2, shared
    seen = {re.search(rf"^{p}_DIGEST=(.+)$", text, re.M).group(1) for p in shared}
    assert len(seen) == 2, f"two images share one digest: {seen}"


def test_every_pin_has_a_version_and_a_digest():
    sys.path.insert(0, str(ROOT / "scripts"))
    from digests import PINS

    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    for prefix, (_image, tag_var) in PINS.items():
        assert re.search(rf"^{tag_var}=.+$", text, re.M), tag_var
        assert re.search(rf"^{prefix}_DIGEST=sha256:[0-9a-f]{{64}}$", text, re.M), prefix
