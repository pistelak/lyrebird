"""`lyrebird down`: the only recovery command, and every way it must refuse to claim success.

`down` observes, decides with `ownership.decide_down`, fences the watchdog, observes and decides
*again*, and only then writes. Almost every test here is a failure path, because that is where the
command's promise lives: a `down` that exits 0 has put the previous settings back, and one that
cannot has said so and kept the journal for the next attempt.
"""

import asyncio
import json
import os
import pathlib
import socket
import threading

import psutil
import pytest

import api
import cli
import config
import control
import ownership
import procs
import session
import store
import supervisor
from cli_doubles import (
    SILENT,
    FakeHealth,
    FakeNetwork,
    FakePsutil,
    answering,
    archived,
    body,
    ours,
    ours_off,
    record,
    world,
)
from ownership import Acquiring, Active, Pac, Restored, ServiceRef

WIFI = ServiceRef("Wi-Fi", "en0")
CORPORATE = Pac("http://proxy.example.com/corp.pac", True)
OTHER = Pac("http://proxy.example.net/other.pac", True)
EMPTY = Pac("", False)


def _active(table, *, port=None, baseline=EMPTY, service=WIFI):
    port = config.CONTROL_PORT if port is None else port
    proxy = table.spawn_ref("proxy", port)
    watchdog = table.spawn_ref("watchdog", port)
    return record(Active(proxy, watchdog), baseline=baseline, service=service), proxy, watchdog


# MARK: - Restoring


def test_down_finishes_a_restore_that_failed_half_way(profile, runner, monkeypatch):
    """`-setautoproxyurl` switches the PAC on as a side effect, so a restore interrupted between
    its two commands has already handed the URL back with the wrong flag. Read as "somebody else's
    PAC" that state was left wrong for good, because the record was then dropped."""
    table = FakePsutil()
    journal, proxy, watchdog = _active(table, baseline=CORPORATE)
    half_way = Pac(CORPORATE.url, False)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", half_way)}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == CORPORATE
    assert [call[1] for call in place.network.setters()] == ["-setautoproxystate"], "one command, not two"
    assert place.journal() == ownership.Absent()
    assert not place.table.alive(proxy.pid) and not place.table.alive(watchdog.pid)


def test_down_finishes_an_off_restore_and_releases_on_the_first_call(profile, runner, monkeypatch):
    """The residue an Off restore leaves — our own URL, disabled — *is* the satisfied state, and a
    read-back spelled "must be AT_TARGET" would call every successful Off restore a failure and
    keep the journal forever."""
    table = FakePsutil()
    journal, _, _ = _active(table)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == ours_off()
    assert place.journal() == ownership.Absent()


def test_down_restores_on_a_service_that_was_disabled_meanwhile(profile, runner, monkeypatch):
    """A `(*)` entry in the listing is a *disabled* network service: it is still listed and still
    holds its PAC. Dropped from the table, its device reads as gone and `down` archives a session
    whose service is right there."""
    table = FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)
    place.network.disabled.add("Wi-Fi")

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == CORPORATE
    assert place.archives() == []


def test_down_after_a_crash_between_read_back_and_checkpoint_restores_once_more_idempotently(
    profile, runner, monkeypatch
):
    """The crash plan-v6 names: the restore landed and the `Restored` record did not. The journal
    still says `Active`, so this run decides again on what it sees — the baseline, already there —
    and checkpoints it with zero writes rather than restoring over a PAC the user may have changed
    back in the meantime."""
    table = FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", CORPORATE)}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.network.setters() == [], "the obligation was already met; a write would be a second restore"
    assert place.journal() == ownership.Absent()


def test_down_switches_off_an_enabled_empty_url_over_an_empty_baseline(profile, runner, monkeypatch):
    """`networksetup` reports the URL and the flag independently, so `("", True)` is a reading it
    can give. Over an empty baseline it is one `state off` from the target — resumable, not a
    stranger's PAC to archive."""
    table = FakePsutil()
    journal, _, _ = _active(table)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", Pac("", True))}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == Pac("", False)
    assert [call[1] for call in place.network.setters()] == ["-setautoproxystate"]


def test_restore_issues_exactly_the_commands_the_target_needs(profile, runner, monkeypatch):
    """As few `networksetup` commands as the target needs, and the *order* matters: setting the URL
    switches the PAC on, so a disabled target needs the flag afterwards and an enabled one needs it
    only when the read-back says the side effect did not happen."""
    cases = [
        (ours(), EMPTY, ["-setautoproxystate"]),
        (ours(), CORPORATE, ["-setautoproxyurl"]),
        (ours(), Pac(CORPORATE.url, False), ["-setautoproxyurl", "-setautoproxystate"]),
        (Pac(CORPORATE.url, False), CORPORATE, ["-setautoproxystate"]),
        (CORPORATE, CORPORATE, []),
    ]
    for found, baseline, expected in cases:
        table = FakePsutil()
        journal, _, _ = _active(table, baseline=baseline)
        place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", found)}, table=table)

        result = runner.invoke(cli.cli, ["down"])

        assert result.exit_code == 0, f"{found} → {baseline}: {result.output}"
        assert [call[1] for call in place.network.setters()] == expected, f"{found} → {baseline}"


# MARK: - When the PAC is not this session's to restore


def test_down_leaves_a_foreign_pac_and_archives_the_baseline_exit_1(profile, runner, monkeypatch):
    """A PAC somebody set by hand mid-session is not ours to overwrite, and the baseline behind it
    is not ours to forget: the record is archived, named, and the exit code says the previous
    settings were never put back."""
    table = FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", OTHER)}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.pac() == OTHER, "a PAC this session does not own is never written over"
    assert place.network.setters() == []
    archives = place.archives()
    assert len(archives) == 1 and str(archives[0]) in result.output
    kept = json.loads(archives[0].read_text())
    assert kept["baseline"] == {"url": CORPORATE.url, "enabled": True}


def test_down_after_restored_never_touches_the_pac_again(profile, runner, monkeypatch):
    """A `Restored` checkpoint is terminal. Whatever the PAC has become since — the user's own
    edit, another tool's — this session has no claim on it, and a second restore would put back a
    baseline that was given up."""
    table = FakePsutil()
    place = world(
        monkeypatch, record(Restored(None), baseline=CORPORATE), services={"Wi-Fi": ("en0", OTHER)}, table=table
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == OTHER
    assert place.network.setters() == []
    assert place.journal() == ownership.Absent()


def test_down_archives_service_gone_without_reading_a_pac(profile, runner, monkeypatch):
    """No service carries the recorded device, so there is nowhere to put the baseline back — and
    nothing to read a PAC off either. Reading one from whatever the route now carries would decide
    this session's fate on another service's settings."""
    table = FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE, service=ServiceRef("Thunderbolt Bridge", "bridge0"))
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.network.commands("-getautoproxyurl") == []
    assert len(place.archives()) == 1
    assert "service" in result.output


def test_down_preserves_when_the_service_table_cannot_be_read(profile, runner, monkeypatch):
    """`Gone` is a proof — no service carries the device — and a listing that could not be read is
    not that proof. Read as `Gone` it archives a session whose service is fine."""
    table = FakePsutil()
    journal, _, watchdog = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, table=table)
    place.network.fail_at(1)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.archives() == []
    assert isinstance(place.journal(), ownership.SessionRecord)
    assert place.table.alive(watchdog.pid), "nothing is fenced before the observation is understood"


def test_down_preserves_before_fencing_when_the_pac_is_unreadable(profile, runner, monkeypatch):
    """It used to stop the watchdog first and read the PAC afterwards, so a `networksetup` that
    failed for a moment cost a working session the process that would have restored it."""
    table = FakePsutil()
    journal, proxy, watchdog = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, table=table)
    place.network.die_at(2)  # the service table, then the PAC read

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.table.alive(watchdog.pid) and place.table.alive(proxy.pid)
    assert isinstance(place.journal(), ownership.SessionRecord)


def test_down_preserves_when_a_setter_exits_0_and_changes_nothing(profile, runner, monkeypatch):
    """`networksetup` really does this. Without the read-back `down` reports the previous settings
    restored over a PAC still pointing at a port that is about to go quiet; with it, the obligation
    stays open and the journal is kept for the next attempt."""
    table = FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)
    place.network.inert_setters()

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.pac() == ours(), "nothing was applied"
    assert isinstance(place.journal(), ownership.SessionRecord), "the obligation is kept, not abandoned"


def test_restore_read_back_re_resolves_a_renamed_service(profile, runner, monkeypatch):
    """Every `networksetup` command in a recipe — the read-back included — resolves the recorded
    *device* afresh. Read back by the old name, a rename that handed that name to another device
    would have the restore confirmed by a service it never wrote to."""
    table = FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    place = world(
        monkeypatch,
        journal,
        services={"Wi-Fi": ("en0", ours()), "Spare": ("en1", CORPORATE)},
        table=table,
    )
    place.network.inert_setters()
    place.network.rename("Wi-Fi", "Office")
    place.network.rename("Spare", "Wi-Fi")

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1, result.output
    assert place.pac("Office") == ours(), "the setter was inert, and the read-back must not say otherwise"
    assert isinstance(place.journal(), ownership.SessionRecord)


# MARK: - The fence, and deciding again after it


def test_down_decides_again_after_fencing(profile, runner, monkeypatch):
    """The PAC can move while the watchdog is being stopped — the watchdog's own last repair, or a
    person. Acting on the first decision would restore over a state that no longer exists."""
    table = FakePsutil()
    journal, _, watchdog = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", CORPORATE)}, table=table)

    def repairs_on_its_way_out(spec):
        place.network.set_pac("Wi-Fi", ours())
        spec.gone = True

    place.table.processes[watchdog.pid].on_terminate = repairs_on_its_way_out

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.pac() == CORPORATE, "the second decision saw ours-enabled and restored it"


def test_down_archives_after_fencing_when_the_pac_turned_foreign(profile, runner, monkeypatch):
    """The other half of the same rule: a PAC that became somebody else's during the fence is
    archived on the second decision, not restored on the first."""
    table = FakePsutil()
    journal, _, watchdog = _active(table, baseline=CORPORATE)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)
    place.table.processes[watchdog.pid].on_terminate = lambda spec: (
        place.network.set_pac("Wi-Fi", OTHER),
        setattr(spec, "gone", True),
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.pac() == OTHER
    assert len(place.archives()) == 1


def test_down_refuses_after_fencing_when_the_port_changed_hands_during_it(profile, runner, monkeypatch):
    """A stranger holding the port is not this session's proxy to stop, and the journal keeps its
    phase — the next `down` re-fences nothing, because the watchdog is already gone."""
    table = FakePsutil()
    journal, proxy, watchdog = _active(table, baseline=CORPORATE)
    answers = {"pid": proxy.pid}
    place = world(
        monkeypatch,
        journal,
        services={"Wi-Fi": ("en0", ours())},
        table=table,
        health=FakeHealth(sequence=[lambda port: answering(answers["pid"])]),
    )
    place.table.processes[watchdog.pid].on_terminate = lambda spec: (
        answers.update(pid=999_001),
        setattr(spec, "gone", True),
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert "999001" in result.output
    assert place.pac() == ours(), "nothing was restored out from under whoever holds the port"
    assert isinstance(place.journal(), ownership.SessionRecord)


def test_down_refuses_before_fencing_when_another_pid_answers_on_the_journals_port(profile, runner, monkeypatch):
    """A numeric pid is not an identity, and a proxy this session does not name is not one to
    restore around: `down` says whose port it is and changes nothing."""
    table = FakePsutil()
    journal, _, watchdog = _active(table, baseline=CORPORATE)
    place = world(
        monkeypatch,
        journal,
        services={"Wi-Fi": ("en0", ours())},
        table=table,
        health=FakeHealth(sequence=[answering(999_002)]),
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.network.setters() == []
    assert place.table.alive(watchdog.pid), "the refusal comes before the fence"


def test_decide_down_refuses_a_reused_pid_that_answers(profile, runner, monkeypatch):
    """The recorded pid answers, and the recorded process is provably gone: the number was reused
    by something else that happens to hold the port. Identity is marker, port and create time — all
    three — so this is a stranger, not the proxy."""
    table = FakePsutil()
    journal, proxy, _ = _active(table, baseline=CORPORATE)
    table.processes[proxy.pid].gone = True
    place = world(
        monkeypatch,
        journal,
        services={"Wi-Fi": ("en0", ours())},
        table=table,
        health=FakeHealth(sequence=[answering(proxy.pid)]),
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.network.setters() == []


# MARK: - The journal itself


def test_down_on_an_unreadable_journal_archives_the_bytes_and_sweeps(profile, runner, monkeypatch):
    """A journal this reader cannot decode may still be holding somebody's PAC, so its bytes are
    copied durably before anything replaces them — and what follows is the portless sweep, because
    nothing here knows a baseline or a port."""
    answers = {"live": True}
    place = world(
        monkeypatch,
        None,
        services={"Wi-Fi": ("en0", ours(9099))},
        health=FakeHealth(sequence=[lambda port: answering(4321) if answers["live"] else SILENT]),
    )
    place.session.journal_path.write_bytes(b"not json at all\xff")

    first = runner.invoke(cli.cli, ["down"])

    assert first.exit_code == 1
    archives = place.archives()
    assert len(archives) == 1 and archives[0].read_bytes() == b"not json at all\xff"
    assert isinstance(place.journal(), ownership.Archived), "a port that answers keeps the record"
    assert place.pac() == ours(9099)

    answers["live"] = False
    second = runner.invoke(cli.cli, ["down"])

    assert second.exit_code == 1, "an archived session never becomes a clean teardown"
    assert place.pac().enabled is False, "the sweep runs again from the replacement record"
    assert place.journal() == ownership.Absent(), "and the record is released once nothing is left"


def test_down_keeps_an_unreadable_journal_whose_bytes_it_cannot_copy(profile, runner, monkeypatch):
    """An archive holding an error string instead of the bytes is exactly the loss it exists to
    prevent, so a copy that failed leaves the file where it is."""
    place = world(monkeypatch, None)
    place.session.journal_path.write_bytes(b"not json at all\xff")

    def cannot_copy(self, since, reason):
        raise OSError("archive volume is full")

    monkeypatch.setattr(session.Session, "archive_file", cannot_copy)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.session.journal_path.read_bytes() == b"not json at all\xff"
    assert "can neither be read" in result.output


def test_down_archives_an_oversized_journal_without_loading_it(profile, runner, monkeypatch):
    """The journal `read()` refused may have been refused *for being oversize*, and reading it
    whole to archive it would cost `down` the memory the cap exists to bound."""
    place = world(monkeypatch, None)
    payload = b"x" * (session.JOURNAL_SIZE_CAP + 4096)
    place.session.journal_path.write_bytes(payload)

    def never(self, *args, **kwargs):
        raise AssertionError("the journal was slurped into memory")

    monkeypatch.setattr(pathlib.Path, "read_bytes", never)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    archives = place.archives()
    assert len(archives) == 1 and os.stat(archives[0]).st_size == len(payload)


def test_down_does_not_release_a_restored_record_it_could_not_make_durable(profile, runner, monkeypatch):
    """A `write` can fail after `tmp.replace` has made the record visible, so a checkpoint this run
    *found* on disk is one it has to sync itself. Unlinked over a visible-but-undurable `Restored`,
    a crash could resurrect the earlier `Active` and restore across a closed boundary."""
    table = FakePsutil()
    place = world(monkeypatch, record(Restored(None), baseline=CORPORATE), table=table)
    failing = {"on": True}
    real = session.Session.barrier

    def barrier(self):
        if failing["on"]:
            raise OSError("fsync: input/output error")
        real(self)

    monkeypatch.setattr(session.Session, "barrier", barrier)

    first = runner.invoke(cli.cli, ["down"])
    assert first.exit_code == 1
    assert isinstance(place.journal(), ownership.SessionRecord)

    failing["on"] = False
    assert runner.invoke(cli.cli, ["down"]).exit_code == 0
    assert place.journal() == ownership.Absent()


def test_down_does_not_sweep_over_an_archived_unknown_record_it_could_not_make_durable(profile, runner, monkeypatch):
    """The same rule for the portless row: a sweep and an unlink over a checkpoint that is visible
    and not yet on disk could let a crash bring the earlier session back."""
    place = world(monkeypatch, archived("unreadable"))

    def barrier(self):
        raise OSError("fsync: input/output error")

    monkeypatch.setattr(session.Session, "barrier", barrier)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert isinstance(place.journal(), ownership.Archived)
    assert place.network.setters() == []


def test_down_on_a_machine_with_no_session_root(profile, runner, monkeypatch):
    """A first-ever `down` on a clean machine must reach the absent-journal row, not `ENOENT` from
    the lock — which is why every actor creates the root before it locks."""
    table = FakePsutil()
    monkeypatch.setattr(procs, "psutil", table)
    FakeNetwork().install(monkeypatch)
    FakeHealth(table).install(monkeypatch)
    assert not session.Session().root.exists()

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert "nothing to stop" in result.output


# MARK: - `Acquiring(None)`: an `up` that recorded no proxy


def test_down_on_acquiring_none_exits_1_and_says_what_it_saw(profile, runner, monkeypatch):
    """One complete scan and one health read on the port; stop what is found; release. There is no
    second scan and no delay, because nothing was installed and there is no baseline to protect —
    but exit 0 would claim a teardown that never had anything to tear down."""
    table = FakePsutil()
    stray = table.spawn("proxy", config.CONTROL_PORT)
    place = world(monkeypatch, record(Acquiring(None)), table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert not place.table.alive(stray)
    assert place.journal() == ownership.Absent()
    assert place.network.setters() == [], "nothing was installed, so nothing is restored"


def test_down_keeps_acquiring_none_when_a_proxy_answers_that_no_record_names(profile, runner, monkeypatch):
    """Something holds the port and the journal cannot say it is ours. Released, the next `up`
    would acquire over it."""
    table = FakePsutil()
    place = world(
        monkeypatch,
        record(Acquiring(None)),
        table=table,
        health=FakeHealth(sequence=[answering(999_003)]),
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert isinstance(place.journal(), ownership.SessionRecord)


def test_down_keeps_the_journal_when_the_scan_is_incomplete(profile, runner, monkeypatch):
    """`Incomplete` is a claim about the *scan*, not about the machine. Read as "nothing found", an
    `AccessDenied` on one of this user's pids releases a journal over a session still running."""
    table = FakePsutil(pids_error=PermissionError("operation not permitted"))
    place = world(monkeypatch, record(Acquiring(None)), table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert isinstance(place.journal(), ownership.SessionRecord)


# MARK: - The portless sweep


def test_down_absent_exits_0_only_when_nothing_was_found_at_all(profile, runner, monkeypatch):
    place = world(monkeypatch, None)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert place.network.setters() == []


def test_down_sweeps_a_disabled_service_too(profile, runner, monkeypatch):
    """A disabled network service still holds a PAC, and `-listallnetworkservices` still lists it —
    with a `*`. Dropping those names leaves a Lyrebird PAC enabled on a service nobody swept."""
    place = world(
        monkeypatch,
        None,
        services={"Wi-Fi": ("en0", EMPTY), "Ethernet": ("en1", ours(9099))},
    )
    place.network.disabled.add("Ethernet")

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1, "something was found, so this is not a clean machine"
    assert place.pac("Ethernet").enabled is False


def test_down_sweep_does_not_switch_off_a_port_it_did_not_check(profile, runner, monkeypatch):
    """The health check and the write are two commands apart, and in between the PAC can become a
    *different* Lyrebird's. Switching it off on the strength of the earlier port's silence would
    take down a session that answers."""
    place = world(monkeypatch, None, services={"Wi-Fi": ("en0", ours(9099))})
    # The 9099 read, then the foreign edit, then `_switch_off`'s own re-read.
    place.network.write_at(3, "Wi-Fi", ours(9100))
    FakeHealth(sequence=[lambda port: SILENT if port == 9099 else answering(4321)]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.pac() == ours(9100), "the port that answers keeps its PAC"


def test_down_sweep_leaves_a_port_that_answers_null_alone(profile, runner, monkeypatch):
    """An HTTP 200 whose body is the literal `null` is a port that *answers*. Decoded through
    `json.loads` alone it was indistinguishable from a connection failure, and the sweep switched
    off a live session's PAC."""
    place = world(monkeypatch, None, services={"Wi-Fi": ("en0", ours(9099))})
    FakeHealth(sequence=[body(None)]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.pac() == ours(9099)


def test_down_sweep_leaves_a_port_that_answers_500_alone(profile, runner, monkeypatch):
    """A listener that says 500 is still a listener."""
    place = world(monkeypatch, None, services={"Wi-Fi": ("en0", ours(9099))})
    FakeHealth(sequence=[body({"error": "boom"}, status=500)]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.pac() == ours(9099)


def test_down_absent_exits_1_when_an_observed_pac_was_disabled_before_it_could_switch_it_off(
    profile, runner, monkeypatch
):
    """`found_any` is a fact about what was *observed*, not about what this run then did. Reset by
    a PAC that was gone again a moment later, `down` exits 0 over a machine it had just found
    evidence on."""
    place = world(monkeypatch, None, services={"Wi-Fi": ("en0", ours(9099))})
    place.network.write_at(3, "Wi-Fi", ours_off(9099))  # disabled between the read and the write

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.network.setters() == [], "there was nothing left to switch off"


def test_down_absent_reports_a_late_orphan_and_leaves_it_to_the_next_down(profile, runner, monkeypatch):
    """A marked process that appears between the first scan and the release scan is something that
    was found — so exit 1 — and it is *reported*, not chased.

    There is no second stop-and-scan pass: a process that appeared after the accounting can appear
    again after the pass that stopped it, and a command that kept trying would either loop or
    release the record anyway. `decide_release` keeps what it saw for the next `down`.
    """
    table = FakePsutil()
    world(monkeypatch, None, table=table)
    real_scan = procs.scan_marked
    calls = {"n": 0}
    late = {"pid": None}

    def scan(*, exclude=None):
        calls["n"] += 1
        if calls["n"] == 2:
            late["pid"] = table.spawn("proxy", 9099)
        return real_scan(exclude=exclude)

    monkeypatch.setattr(procs, "scan_marked", scan)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert table.alive(late["pid"]), "the orphan this run only observed is left for the next `down`"
    assert table.signalled == []
    assert "still running" in result.output


def test_down_absent_exits_1_over_a_legacy_runtime_record(profile, runner, monkeypatch):
    """A `runtime-*.json` from before this protocol is unresolved recovery evidence: it says a PAC
    may still be installed. The path is named and counted as a finding; its contents are not
    decoded, because a record written before this protocol can be neither trusted nor acted on and
    the file is right there for a person to read."""
    place = world(monkeypatch, None)
    config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
    (config.STATE_ROOT / "runtime-8088.json").write_text(
        json.dumps({"proxyPid": 1, "service": "Wi-Fi", "previousPac": {"url": CORPORATE.url, "enabled": True}}),
        encoding="utf-8",
    )

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1, "an unresolved pre-protocol record is a finding, not a clean machine"
    assert "runtime-8088.json" in result.output
    assert place.network.setters() == [], "a record from an older Lyrebird is named to the operator, not acted on"


def test_down_sweep_keeps_the_archive_when_a_service_cannot_be_read(profile, runner, monkeypatch):
    """ "I could not check every service" is not "there was nothing to find". The services already
    switched off stay off — each was its own read-back — and the record is kept."""
    place = world(
        monkeypatch,
        archived("unreadable"),
        services={"Wi-Fi": ("en0", EMPTY), "Ethernet": ("en1", EMPTY)},
    )
    place.network.fail_at(3)  # -listallnetworkservices, Wi-Fi's read, then Ethernet's

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert isinstance(place.journal(), ownership.Archived)


def test_down_sweep_release_needs_a_clean_sweep_not_a_port(profile, runner, monkeypatch):
    """The portless rows have no port to ask, so what they consult is the sweep. One Lyrebird PAC
    left enabled keeps the record, whatever any port would have said."""
    place = world(monkeypatch, archived("unreadable"), services={"Wi-Fi": ("en0", ours(9099))})
    FakeHealth(sequence=[answering(4321)]).install(monkeypatch)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert isinstance(place.journal(), ownership.Archived)
    assert place.pac() == ours(9099)


# MARK: - Accounting


def test_down_accounts_for_a_marked_orphan_that_reused_a_dead_journalled_pid(profile, runner, monkeypatch):
    """A journalled pid is excluded from the scan's orphans only *while* its persisted identity is
    alive or unknown. Once the recorded ref is proven dead, a marked process on that number is a
    different incarnation — and excluding it forever would keep the journal for good."""
    table = FakePsutil()
    journal, proxy, watchdog = _active(table, baseline=CORPORATE)
    table.processes[proxy.pid].gone = True
    # The number comes back, marked, on another port: an orphan like any other.
    table.spawn("proxy", 9099, pid=proxy.pid)
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 0, result.output
    assert not place.table.alive(proxy.pid)
    assert place.journal() == ownership.Absent()


def test_down_keeps_the_journal_when_the_proxy_will_not_stop(profile, runner, monkeypatch):
    """ "Restored" and "released" are two claims. The baseline is back, and a proxy that survived
    SIGKILL keeps the record for the next `down` — an unlink would leave nothing naming it."""
    table = FakePsutil()
    journal, proxy, _ = _active(table, baseline=CORPORATE)
    table.processes[proxy.pid].dies = False
    table.processes[proxy.pid].waits = [psutil.TimeoutExpired(1)] * 6
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)

    result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert place.pac() == CORPORATE, "the restore still happened"
    assert isinstance(place.journal(), ownership.SessionRecord)
    assert isinstance(place.journal().phase, Restored)


# MARK: - Reaching the journal's port, not this process's


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _RealControlServer:
    """A real control server on one port, in a thread, so a synchronous `down` can talk to it.

    The server binds `config.CONTROL_PORT` as it is when the thread starts; the test then points
    that global at the *other* port, which is what an invocation carrying another
    `LYREBIRD_CONTROL_PORT` looks like. `CONTROL_HOST_HEADER` stays this server's, because in
    production the guard runs inside the proxy process and that process's own header is the one it
    allows — see `control._allowed_hosts`.
    """

    def __init__(self, port):
        self.port = port
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        async def main():
            runner_ = await control.start(store.Store(), _meta_for(self.port))
            self.ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
            await runner_.cleanup()

        asyncio.run(main())

    def __enter__(self):
        self._thread.start()
        assert self.ready.wait(10), "the control server did not start"
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(10)


def test_down_from_another_control_port_reaches_the_journals_proxy(
    profile, runner, monkeypatch, _no_real_control_transport
):
    """`down` needs no port of its own: it asks the journal's.

    Read through the configured origin instead — or through the configured `Host`, which the
    control guard answers 421 to — a `down` invoked with another `LYREBIRD_CONTROL_PORT` reported
    a live proxy as gone and swept its PAC. So this drives the whole command over the real
    transport against a real control server on the journal's port, with the configured port a
    closed one, and asserts both the request that was sent and the answer it produced: a stranger
    answering on the journal's port is a refusal that names it, where a `down` that had asked the
    configured port would have found silence and restored.
    """
    journal_port, configured = _free_port(), _free_port()
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    config.reload_profile()
    monkeypatch.setattr(config, "CONTROL_PORT", journal_port)
    monkeypatch.setattr(config, "CONTROL_HOST_HEADER", f"127.0.0.1:{journal_port}")

    table = FakePsutil()
    # A recorded proxy that is provably gone, so what answers on that port is a stranger.
    proxy = table.spawn_ref("proxy", journal_port)
    watchdog = table.spawn_ref("watchdog", journal_port)
    table.processes[proxy.pid].gone = True
    journal = record(
        Active(proxy, watchdog),
        baseline=CORPORATE,
        owned_by=ownership.Owner(
            control_port=journal_port,
            profile_fingerprint=config.PROFILE_FINGERPRINT,
            state_root=str(config.STATE_ROOT),
        ),
    )
    real_fetch = api._fetch
    place = world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours(journal_port))}, table=table)
    monkeypatch.setattr(api, "_fetch", real_fetch)  # the real transport, not the suite's double

    asked = []

    def recording(request, timeout):
        asked.append((request.full_url, request.get_header("Host")))
        return _no_real_control_transport(request, timeout)

    monkeypatch.setattr(api, "_open", recording)

    with _RealControlServer(journal_port):
        # Only now: the server is bound, and this is the port the invocation was given.
        monkeypatch.setattr(config, "CONTROL_PORT", configured)
        result = runner.invoke(cli.cli, ["down"])

    assert result.exit_code == 1
    assert str(journal_port) in result.output and str(os.getpid()) in result.output
    assert place.pac() == ours(journal_port), "a port that answers keeps its PAC"
    assert isinstance(place.journal().phase, Active), "and its journal"
    assert asked, "the transport was used"
    for url, host in asked:
        assert f":{journal_port}/" in url and host == f"127.0.0.1:{journal_port}", (url, host)
        assert str(configured) not in url


async def _meta(  # noqa: D401 - the shape `control.start` expects
) -> dict:
    return {"proxyUp": True, "pid": os.getpid(), "profileFingerprint": config.PROFILE_FINGERPRINT}


def _meta_for(port):
    async def meta() -> dict:
        return {"proxyUp": True, "pid": os.getpid(), "controlPort": port}

    return meta


# MARK: - `down_session`, the in-process executor


def test_down_session_refuses_a_journal_that_is_not_this_owners(profile, runner, monkeypatch):
    """The owner precondition the acceptance finalizer runs under: an ordinary `down` tears down
    whichever session exists, which is right for the command and wrong for a cleanup that may be
    running after its own session was released."""
    table = FakePsutil()
    stranger = ownership.Owner(control_port=9099, profile_fingerprint="ffffffffffff", state_root="/tmp/elsewhere")
    journal, _, watchdog = _active(table, baseline=CORPORATE)
    place = world(
        monkeypatch,
        record(journal.phase, baseline=CORPORATE, owned_by=stranger),
        services={"Wi-Fi": ("en0", ours())},
        table=table,
    )

    outcome = supervisor.down_session(session.Session(), only_owner=supervisor._owner())

    assert outcome.exit_code == 1 and outcome.refused is not None
    assert outcome.disposition == "none" and outcome.released is False
    assert place.network.setters() == [] and place.table.alive(watchdog.pid)


@pytest.mark.parametrize(
    "journal",
    [None, archived("unreadable")],
    ids=["absent", "archived without an owner"],
)
def test_down_session_refuses_every_journal_that_carries_no_owner_evidence(profile, monkeypatch, journal):
    """`Unreadable` and `Archived(Unknown)` name nobody — the second by definition — so ownership
    is unproven and the sweep an ordinary `down` would run could stop a successor whose record
    merely failed to decode. `Absent` is the one exception: there is nothing to be wrong about."""
    place = world(monkeypatch, journal)
    if journal is None:
        place.session.journal_path.write_bytes(b"\xff not json")

    outcome = supervisor.down_session(session.Session(), only_owner=supervisor._owner())

    assert outcome.exit_code == 1 and outcome.refused is not None
    assert place.network.setters() == []


def test_down_session_reports_what_became_of_the_obligation(profile, monkeypatch):
    """The finalizer has to tell "restored" from "archived" from "neither yet", and whether the
    journal was released — a `Restored` release and an `Archived(displaced)` release both leave
    `Absent` behind, and only the caller that ran them can say which happened."""
    table = FakePsutil()
    journal, _, _ = _active(table, baseline=CORPORATE)
    world(monkeypatch, journal, services={"Wi-Fi": ("en0", ours())}, table=table)

    outcome = supervisor.down_session(session.Session(), only_owner=supervisor._owner())

    assert (outcome.exit_code, outcome.disposition, outcome.released) == (0, "restored", True)
    assert outcome.archive is None
