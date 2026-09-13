"""The diagnosis a failed acceptance cleanup carries.

The module under test lives in `tests/acceptance/`, but this check cannot: that directory's
`conftest.py` marks everything it collects as `acceptance`, so a check placed beside the code would
be deselected from the fast suite and would only ever run on a machine with a simulator. Loaded by
path rather than by adding the directory to `sys.path`, so nothing else in the run sees it.

Every case here is a cleanup that already failed. The thing being pinned is that the failure still
says what happened: the command, its output, and the device's state — which nothing recorded
before, so a 405 (a state error) could not be read at all. The state is an observation and settles
no cause; it is context for the command that failed, and must never displace it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

import cli_doubles as doubles
import config
import netproxy
import ownership
import session

_ACCEPTANCE = Path(__file__).resolve().parent / "acceptance"


def _load(file: str, name: str | None = None):
    """Load a module out of `tests/acceptance/` by path.

    `simstate` is registered under its own name because the harness imports it by that name, as
    pytest's own collection of that directory does. The harness itself is *not* registered as
    `conftest`: pytest already holds the root `tests/conftest.py` under that name, and replacing it
    would take the whole suite's fixtures with it.
    """
    name = name or file
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ACCEPTANCE / f"{file}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


simstate = _load("simstate")

# Short and readable, like the other fixtures here: a real udid in the repository is a
# machine-specific value nothing can reproduce, and the privacy gate refuses it.
UDID = "SIM-1"


def _listing(*devices: dict) -> subprocess.CompletedProcess:
    payload = json.dumps({"devices": {"iOS-26-4": list(devices)}})
    return subprocess.CompletedProcess(["xcrun", "simctl", "list", "devices", "-j"], 0, payload, "")


@pytest.mark.parametrize("state", ["Booted", "Shutdown"])
def test_the_state_simctl_reports_is_the_state_named(state):
    described = simstate.describe_state(UDID, run=lambda timeout: _listing({"udid": UDID, "state": state}))
    assert state in described


def test_a_device_missing_from_the_listing_is_not_reported_as_a_state():
    """ "Shutdown" and "gone" are different facts, and a cleanup that could not find its device at
    all is the one most worth saying out loud."""
    described = simstate.describe_state(UDID, run=lambda timeout: _listing({"udid": "other", "state": "Booted"}))
    assert "is not in `simctl list devices`" in described
    assert "Shutdown" not in described


def test_a_refused_query_is_not_read_as_a_state():
    """`simctl` exiting non-zero says nothing about the device. Reporting that as a state would
    invent a fact, and reporting nothing would lose the reason."""
    refused = subprocess.CompletedProcess(["xcrun", "simctl"], 2, "", "simctl: no such device")
    described = simstate.describe_state(UDID, run=lambda timeout: refused)
    assert "state unknown" in described
    assert "no such device" in described


def test_unparseable_output_says_so_rather_than_raising():
    broken = subprocess.CompletedProcess(["xcrun", "simctl"], 0, "not json", "")
    described = simstate.describe_state(UDID, run=lambda timeout: broken)
    assert "did not parse" in described


def test_a_query_that_cannot_run_at_all_still_returns_a_phrase():
    """This runs inside a finalizer with cleanup still to do: an exception thrown out of it
    abandons the rest, which is how a run ends with the app installed and the simulator booted."""

    def explode(timeout):
        raise subprocess.TimeoutExpired(["xcrun", "simctl"], timeout)

    assert "could not be run" in simstate.describe_state(UDID, run=explode)


def test_the_command_that_failed_survives_a_diagnosis_that_also_fails():
    """The finding is the failed cleanup. The device state is context for it, so a snapshot that
    cannot be taken must not take the reason down with it."""

    def explode(timeout):
        raise OSError("xcrun is gone")

    failed = subprocess.CompletedProcess(
        ["xcrun", "simctl", "uninstall", UDID, "com.example.lyrebird-fixture"], 1, "", "code=405"
    )
    message = simstate.cleanup_failure("could not uninstall", failed, UDID, run=explode)
    assert "could not uninstall" in message
    assert "code=405" in message
    assert "exit: 1" in message
    assert "simctl uninstall" in message
    assert "could not be run" in message


def test_a_cleanup_whose_command_never_ran_says_that_much():
    message = simstate.cleanup_failure("could not shut it down", None, UDID, run=lambda timeout: _listing())
    assert "simctl did not run at all" in message
    assert "could not shut it down" in message


def test_a_malformed_device_entry_does_not_raise_out_of_the_finalizer():
    """A listing is whatever `simctl` happened to print. `null` in place of a device used to reach
    `.get` and raise, which in a finalizer means the failure being reported is replaced by this
    one and the rest of the cleanup never runs."""
    broken = subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": {"r": [None, 7, "x"]}}), "")
    assert "entries that are not devices" in simstate.describe_state(UDID, run=lambda timeout: broken)


def test_an_undecodable_answer_does_not_raise_out_of_the_finalizer():
    """`subprocess.run(text=True)` decodes, and decoding raises `UnicodeDecodeError` — a
    `ValueError`, so naming the process and OS errors alone did not cover it."""

    def undecodable(timeout):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    assert "device state unknown" in simstate.describe_state(UDID, run=undecodable)


def test_a_listing_shaped_wrongly_does_not_raise_out_of_the_finalizer():
    wrong = subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": "not a mapping"}), "")
    assert "device state unknown" in simstate.describe_state(UDID, run=lambda timeout: wrong)


@pytest.mark.parametrize(
    "query",
    [
        lambda timeout: subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": {"r": [None]}}), ""),
        lambda timeout: subprocess.CompletedProcess(["xcrun"], 0, "not json", ""),
        lambda timeout: (_ for _ in ()).throw(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")),
    ],
    ids=["malformed-entry", "unparseable", "undecodable"],
)
def test_the_failed_command_survives_every_way_the_diagnosis_can_go_wrong(query):
    """The finding is the cleanup that failed. However badly the state query goes, the command,
    its exit status and both its streams have to reach the reader."""
    failed = subprocess.CompletedProcess(
        ["xcrun", "simctl", "uninstall", UDID, "com.example.lyrebird-fixture"],
        1,
        "some stdout worth keeping",
        "error code=405):\nUnable to lookup in current state: Shutdown",
    )
    message = simstate.cleanup_failure("could not uninstall", failed, UDID, run=query)
    assert "could not uninstall" in message
    assert "simctl uninstall" in message
    assert "exit: 1" in message
    assert "some stdout worth keeping" in message
    assert failed.stderr in message, "the original stderr must survive whole, not just its last line"


def test_a_wall_of_simctl_output_cannot_push_the_failure_out_of_view():
    """Supplementary detail is capped; the failure it accompanies is not."""
    noisy = subprocess.CompletedProcess(["xcrun"], 2, "", "x" * 5000)
    message = simstate.cleanup_failure("could not shut it down", None, UDID, run=lambda timeout: noisy)
    assert "could not shut it down" in message
    assert "characters in all" in message
    assert len(message) < 1200


def test_the_state_query_is_given_a_bound_of_its_own():
    """It runs while a finalizer is already reporting a failure: a diagnosis that hangs turns a
    legible failure into no failure at all."""
    seen = []

    def record(timeout):
        seen.append(timeout)
        return _listing({"udid": UDID, "state": "Booted"})

    simstate.describe_state(UDID, run=record)
    assert seen == [simstate.STATE_TIMEOUT]
    assert 0 < simstate.STATE_TIMEOUT <= 60


def test_a_listing_it_could_not_read_is_not_reported_as_the_device_being_absent():
    """Skipping entries and then saying "not there" states a fact nobody established: a listing
    this cannot read says nothing about whether the device is in it. Two different answers."""
    unreadable = subprocess.CompletedProcess(["xcrun"], 0, json.dumps({"devices": {"r": [None, 7]}}), "")
    described = simstate.describe_state(UDID, run=lambda timeout: unreadable)
    assert "entries that are not devices" in described
    assert "is not in `simctl list devices` at all" not in described


@pytest.mark.parametrize(
    "query",
    [
        lambda timeout: subprocess.CompletedProcess(
            ["xcrun"], 0, json.dumps({"devices": {"r": [{"udid": "SIM-1", "state": "S" * 5000}]}}), ""
        ),
        lambda timeout: subprocess.CompletedProcess(["xcrun"], 2, "", "e" * 5000),
        lambda timeout: (_ for _ in ()).throw(OSError("o" * 5000)),
    ],
    ids=["state", "refusal", "exception"],
)
def test_every_supplemental_answer_is_bounded(query):
    """Not just the one that happened to be noticed first: a state, a refusal and an exception can
    each arrive with a wall of text behind it."""
    assert len(simstate.describe_state(UDID, run=query)) < 600


def test_an_interrupt_is_not_swallowed():
    """The suite turns SIGTERM and SIGHUP into `KeyboardInterrupt` so every finalizer runs. A
    diagnosis that ate one would strand the machine it was trying to hand back."""

    def interrupted(timeout):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        simstate.describe_state(UDID, run=interrupted)


def test_the_real_query_hands_its_bound_to_subprocess(monkeypatch):
    """The timeout has to reach `subprocess.run`, not merely exist as a constant."""
    seen = {}

    def capture(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, json.dumps({"devices": {}}), "")

    monkeypatch.setattr(simstate.subprocess, "run", capture)
    simstate.describe_state(UDID)
    assert seen["timeout"] == simstate.STATE_TIMEOUT


# MARK: - The finalizer's teardown and its independent fallback
#
# The acceptance harness's cleanup runs on a machine with a simulator, so its *outcomes* can only
# be pinned here — over the same in-memory doubles the CLI suite uses. What is under test is the
# harness itself: the owner gate, the retention rule, the fallback's row, and the two rules that
# keep it off a successor session's processes.

acceptance = _load("conftest", "acceptance_harness")


def _harness(monkeypatch, network, *, baseline=None, service=None):
    """The real `Harness`, wired to the doubles rather than to macOS.

    Its own verification reads go through the module-level `_run`, which is what a real cleanup
    uses to ask macOS directly — so that is the one seam replaced here, and everything above it is
    the harness's own code.
    """
    monkeypatch.setattr(acceptance, "_run", lambda args, timeout=600: network._run(list(args)))
    world = acceptance.Harness(
        udid="SIM-1",
        documents=Path("/tmp/lyrebird-tests/documents"),
        profile=config.PROFILE_DIR,
        state=config.STATE_ROOT,
        env={"LYREBIRD_CONTROL_PORT": str(config.CONTROL_PORT)},
    )
    world.service = service or ownership.ServiceRef("Wi-Fi", "en0")
    found = network.pac(world.service.name)
    world.baseline = acceptance.Pac(found.url, found.enabled) if baseline is None else baseline
    return world


def _place(monkeypatch, journal, *, services=None, table=None, health=None):
    return doubles.world(monkeypatch, journal, services=services, table=table, health=health)


def _active(table, *, baseline=None, owned_by=None, service=None):
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    watchdog = table.spawn_ref("watchdog", config.CONTROL_PORT)
    record = doubles.record(
        ownership.Active(proxy, watchdog),
        baseline=baseline or ownership.Pac("", False),
        owned_by=owned_by,
        service=service or doubles.WIFI,
    )
    return record, proxy, watchdog


CORPORATE = ownership.Pac("http://proxy.example.com/corp.pac", True)
OTHER = ownership.Pac("http://proxy.example.net/other.pac", True)


def test_acceptance_finalizer_restores_checkpoints_and_releases(profile, monkeypatch):
    """The ordinary case, so the failures below are read against a cleanup that works."""
    table = doubles.FakePsutil()
    journal, proxy, watchdog = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    problems = world._restore_the_network()

    assert problems == [], problems
    assert place.pac() == CORPORATE
    assert place.journal() == ownership.Absent()
    assert not place.table.alive(proxy.pid) and not place.table.alive(watchdog.pid)


def test_acceptance_finalizer_leaves_a_successor_session_alone(profile, monkeypatch):
    """Once this run's own session has been released, the journal at the fixed per-user path may
    be somebody else's — and an ordinary `down` would tear it down. Every attempt goes through the
    owner precondition, so a successor appearing before any of them is refused."""
    table = doubles.FakePsutil()
    stranger = ownership.Owner(control_port=9099, profile_fingerprint="ffffffffffff", state_root="/tmp/elsewhere")
    journal, proxy, watchdog = _active(table, baseline=CORPORATE, owned_by=stranger)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    problems = world._restore_the_network()

    assert any("not this run" in problem for problem in problems), problems
    assert place.network.setters() == [], "a successor's PAC is never written over"
    assert place.table.alive(proxy.pid) and place.table.alive(watchdog.pid)
    assert isinstance(place.journal(), ownership.SessionRecord)


def test_acceptance_finalizer_never_kills_a_successor_on_the_same_state_root(profile, monkeypatch):
    """Profiles sharing a state root share the confdir, so a successor started there under another
    profile matches this run's own `--set confdir=` token. The owner gate is what keeps the process
    cleanup off it."""
    table = doubles.FakePsutil()
    stranger = ownership.Owner(
        control_port=config.CONTROL_PORT, profile_fingerprint="ffffffffffff", state_root=str(config.STATE_ROOT)
    )
    journal, proxy, _ = _active(table, baseline=CORPORATE, owned_by=stranger)
    successor = table.spawn("proxy", config.CONTROL_PORT, confdir=str(config.STATE_ROOT / "mitmproxy"))
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    problems = world._restore_the_network()

    assert place.table.alive(successor), problems
    assert place.table.alive(proxy.pid)


def test_acceptance_finalizer_writes_nothing_without_proof_of_ownership(profile, monkeypatch):
    """`Unreadable` and `Archived(Unknown)` name nobody, and `Absent` is no authority to put a
    preflight snapshot back — after a release through `Archived` the baseline was given up."""
    for journal, corrupt in (
        (None, True),
        (doubles.archived("unreadable"), False),
        (None, False),
    ):
        table = doubles.FakePsutil()
        place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", OTHER)}, table=table)
        if corrupt:
            place.session.journal_path.write_bytes(b"not json at all\xff")
        world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

        problems = world._restore_the_network()

        assert place.network.setters() == [], problems
        assert place.pac() == OTHER


def test_acceptance_finalizer_writes_nothing_after_a_released_checkpoint(profile, monkeypatch):
    """A displacement already recorded and released: the PAC differs from the preflight snapshot,
    and putting the snapshot back would restore an obligation this run gave up."""
    table = doubles.FakePsutil()
    place = _place(monkeypatch, None, services={"Wi-Fi": ("en0", OTHER)}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    problems = world._restore_the_network()

    assert place.network.setters() == []
    assert any("nothing here is this run" in problem for problem in problems), problems


def test_acceptance_finalizer_reaches_the_fallback_when_down_session_raises(profile, monkeypatch):
    """A wiring error in the executor must not skip the cleanup and leave an owned `Active` routed
    at a dead proxy: the traceback is reported and the independent fallback runs."""
    table = doubles.FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    def boom(store, only_owner=None):
        raise RuntimeError("a wiring error in down_session")

    monkeypatch.setattr(acceptance.supervisor, "down_session", boom)

    problems = world._restore_the_network()

    assert any("a wiring error" in problem for problem in problems), problems
    assert place.pac() == CORPORATE, "the fallback put the baseline back"
    assert place.journal() == ownership.Absent()


def test_acceptance_finalizer_accounts_a_live_proxy_in_a_terminal_record_when_down_session_raises(profile, monkeypatch):
    """A terminal record accounts for the refs it names before release. Without it, both guarded
    attempts raising before their accounting would leave a live recorded proxy that the independent
    scan excludes by design."""
    table = doubles.FakePsutil()
    proxy = table.spawn_ref("proxy", config.CONTROL_PORT)
    place = _place(
        monkeypatch,
        doubles.record(ownership.Restored(proxy), baseline=CORPORATE),
        services={"Wi-Fi": ("en0", CORPORATE)},
        table=table,
    )
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    monkeypatch.setattr(
        acceptance.supervisor, "down_session", lambda store, only_owner=None: (_ for _ in ()).throw(RuntimeError("x"))
    )

    world._restore_the_network()

    assert not place.table.alive(proxy.pid), "the recorded proxy was stopped through its persisted ref"
    assert place.journal() == ownership.Absent()


def test_acceptance_finalizer_retains_a_terminal_record_it_cannot_make_durable(profile, monkeypatch):
    """`write(Restored)` may have replaced the journal and then failed its directory sync, and an
    unlink over that could let a crash resurrect the earlier `Active`."""
    table = doubles.FakePsutil()
    place = _place(
        monkeypatch,
        doubles.record(ownership.Restored(None), baseline=CORPORATE),
        services={"Wi-Fi": ("en0", CORPORATE)},
        table=table,
    )
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    monkeypatch.setattr(
        acceptance.supervisor, "down_session", lambda store, only_owner=None: (_ for _ in ()).throw(RuntimeError("x"))
    )
    failing = {"on": True}
    real = session.Session.barrier

    def barrier(self):
        if failing["on"]:
            raise OSError("fsync: input/output error")
        real(self)

    monkeypatch.setattr(session.Session, "barrier", barrier)

    problems = world._restore_the_network()

    assert any("durable" in problem for problem in problems), problems
    assert isinstance(place.journal(), ownership.SessionRecord), "retained while the barrier fails"

    failing["on"] = False
    world.cleaned = False
    world._restore_the_network()
    assert place.journal() == ownership.Absent(), "released once it succeeds"


def test_acceptance_finalizer_does_not_checkpoint_a_satisfied_target_behind_a_live_watchdog(profile, monkeypatch):
    """A satisfied target with a live watchdog is *not* done: the watchdog would repair its own
    residue, and the fence failing is what both teardown attempts preserved on."""
    table = doubles.FakePsutil()
    journal, _, watchdog = _active(table, baseline=CORPORATE)
    table.processes[watchdog.pid].dies = False
    table.processes[watchdog.pid].waits = [psutil.TimeoutExpired(1)] * 8
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", CORPORATE)}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    monkeypatch.setattr(
        acceptance.supervisor, "down_session", lambda store, only_owner=None: (_ for _ in ()).throw(RuntimeError("x"))
    )

    world._restore_the_network()

    kept = place.journal()
    assert isinstance(kept, ownership.SessionRecord) and isinstance(kept.phase, ownership.Active)


def test_acceptance_finalizer_leaves_the_proxy_running_after_a_post_fence_preserve(profile, monkeypatch):
    """A proxy stopped over an unreadable second read would leave an `Active` journal, a PAC
    possibly enabled at a dead port, and no watchdog."""
    table = doubles.FakePsutil()
    journal, proxy, watchdog = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    monkeypatch.setattr(
        acceptance.supervisor, "down_session", lambda store, only_owner=None: (_ for _ in ()).throw(RuntimeError("x"))
    )
    reads = {"n": 0}
    real = netproxy.pac_status

    def failing(name):
        reads["n"] += 1
        if reads["n"] > 1:  # the first observation lands; the one after the fence does not
            raise netproxy.NetworkSetupError("`networksetup -getautoproxyurl Wi-Fi` failed")
        return real(name)

    monkeypatch.setattr(netproxy, "pac_status", failing)

    world._restore_the_network()

    assert place.table.alive(proxy.pid), "nothing was signalled after the second look failed"
    assert not place.table.alive(watchdog.pid), "the fence itself happened"
    assert isinstance(place.journal().phase, ownership.Active), "the journal keeps its phase"


def test_acceptance_finalizer_never_writes_over_a_foreign_pac(profile, monkeypatch):
    """A foreign PAC is archived, never overwritten — and the recorded processes are stopped even
    when the archive cannot be written, because that evidence is what makes stopping them safe."""
    table = doubles.FakePsutil()
    journal, proxy, watchdog = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", OTHER)}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    monkeypatch.setattr(
        acceptance.supervisor, "down_session", lambda store, only_owner=None: (_ for _ in ()).throw(RuntimeError("x"))
    )
    monkeypatch.setattr(
        session.Session,
        "write",
        lambda self, record: (_ for _ in ()).throw(OSError("no space left on device")),
    )

    world._restore_the_network()

    assert place.pac() == OTHER, "the PAC this session does not own is untouched"
    assert place.network.setters() == []
    assert not place.table.alive(proxy.pid) and not place.table.alive(watchdog.pid)


def test_acceptance_finalizer_respects_a_pac_that_changed_between_attempts(profile, monkeypatch):
    """The teardown preserved, a person then changed the PAC, and the fallback sees what is there
    now — not what the first decision was about."""
    table = doubles.FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    def preserve_then_edit(store, only_owner=None):
        place.network.set_pac("Wi-Fi", OTHER)
        return acceptance.supervisor.DownOutcome(1, "none", False, None, None)

    monkeypatch.setattr(acceptance.supervisor, "down_session", preserve_then_edit)

    world._restore_the_network()

    assert place.pac() == OTHER
    assert place.network.setters() == []


def test_acceptance_finalizer_checkpoint_is_terminal(profile, monkeypatch):
    """Once the fallback has checkpointed, a later `down` over the same root must not restore
    again — the user enabling the baseline URL afterwards is their business."""
    table = doubles.FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    world._restore_the_network()
    assert place.journal() == ownership.Absent()

    place.network.set_pac("Wi-Fi", OTHER)
    outcome = acceptance.supervisor.down_session(session.Session(), only_owner=world.owner())

    assert outcome.exit_code == 0
    assert place.pac() == OTHER, "nothing was restored a second time"


def test_acceptance_finalizer_keeps_an_archived_outcome_across_a_retry(profile, monkeypatch):
    """The first non-`none` disposition is what this run did; a later `Absent` observation must
    never overwrite an earlier `archived` in the report."""
    table = doubles.FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", OTHER)}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    answers = iter(
        [
            acceptance.supervisor.DownOutcome(1, "archived", False, Path("/tmp/lyrebird-tests/archive/a.json"), None),
            acceptance.supervisor.DownOutcome(0, "none", True, None, None),
        ]
    )
    monkeypatch.setattr(acceptance.supervisor, "down_session", lambda store, only_owner=None: next(answers))

    problems = world._restore_the_network()

    assert any("archived" in problem and "a.json" in problem for problem in problems), problems


def test_acceptance_finalizer_retries_a_retained_restored_journal(profile, monkeypatch):
    """A `Restored(ref)` kept because the proxy would not die still needs accounting and
    unlinking, so the retry is driven by `released`, not by the disposition."""
    table = doubles.FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", CORPORATE)}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    attempts = []

    def down(store, only_owner=None):
        attempts.append(len(attempts))
        return acceptance.supervisor.DownOutcome(1, "restored", len(attempts) > 1, None, None)

    monkeypatch.setattr(acceptance.supervisor, "down_session", down)

    world._restore_the_network()

    assert len(attempts) == 2, "it retried while an owner-matching journal remained"


def test_acceptance_cleanup_follows_its_service_by_device_after_a_rename(profile, monkeypatch):
    """A rename that hands the old name to another device would otherwise make the harness read
    that other service and report a failed restore after production restored the right one."""
    table = doubles.FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = _place(
        monkeypatch,
        journal,
        services={"Wi-Fi": ("en0", doubles.ours()), "Spare": ("en1", OTHER)},
        table=table,
    )
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))
    place.network.rename("Wi-Fi", "Office")
    place.network.rename("Spare", "Wi-Fi")

    problems = world._restore_the_network()

    assert world.service_name() == "Office"
    assert problems == [], problems
    assert place.pac("Office") == CORPORATE


def test_acceptance_reports_but_does_not_restore_a_preflight_snapshot_that_differs_from_the_journal(
    profile, monkeypatch
):
    """The journal's baseline — observed under the lock at acquisition — is the only target
    anything writes. A user who reconfigured the PAC between the preflight snapshot and `up` has
    changed what "restored" means, and only the journal knows it."""
    table = doubles.FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    # The snapshot this harness took before `up`, which is *not* what `up` then observed.
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(OTHER.url, True))

    problems = world._restore_the_network()

    assert place.pac() == CORPORATE, "the journal's baseline is what came back"
    assert any("not restored" in problem for problem in problems), problems


def test_acceptance_finalizer_never_signals_a_journalled_pid_whose_identity_is_unknown(profile, monkeypatch):
    """After a clock step both teardown attempts correctly preserve on UNKNOWN, and this
    independent scan must not then kill the process through its fresh `Ref`."""
    table = doubles.FakePsutil()
    journal, proxy, _ = _active(table, baseline=CORPORATE)
    table.processes[proxy.pid].create_time += 100.0  # the clock stepped
    place = _place(monkeypatch, journal, services={"Wi-Fi": ("en0", doubles.ours())}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac(CORPORATE.url, True))

    world._restore_the_network()

    assert (proxy.pid, "terminate") not in place.table.signalled
    assert (proxy.pid, "kill") not in place.table.signalled


def test_acceptance_finalizer_never_signals_a_reused_proxy_pid(profile, monkeypatch):
    """Signalling is only ever through the `Ref` captured at discovery, so a proxy that exits
    during the grace wait and whose pid is reused receives nothing."""
    table = doubles.FakePsutil()
    place = _place(monkeypatch, None, services={"Wi-Fi": ("en0", ownership.Pac("", False))}, table=table)
    stray = table.spawn("proxy", config.CONTROL_PORT, confdir=str(config.STATE_ROOT / "mitmproxy"))
    captured = table.ref(stray)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac("", False))
    # The pid is reused by something else between the scan and the signal.
    table.processes[stray] = doubles.FakeProc(cmdline=["/usr/bin/vim", "notes.txt"], create_time=captured.create_time)

    problems = world._restore_the_network()

    assert table.signalled == [], problems
    assert table.processes[stray].cmdline == ["/usr/bin/vim", "notes.txt"]


def test_acceptance_finalizer_reports_a_scan_it_could_not_complete(profile, monkeypatch):
    """A `ps` that failed used to return `[]`, and the cleanup reported success over a surviving
    proxy. Incomplete is a claim about the scan, not about the machine."""
    table = doubles.FakePsutil(pids_error=PermissionError("operation not permitted"))
    place = _place(monkeypatch, None, services={"Wi-Fi": ("en0", ownership.Pac("", False))}, table=table)
    world = _harness(monkeypatch, place.network, baseline=acceptance.Pac("", False))

    problems = world._restore_the_network()

    assert any("read whole" in problem for problem in problems), problems
