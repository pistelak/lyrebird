"""Which simulator the CA and the relaunch land on.

`booted` is what these tests exist to keep out of the simctl arguments: `simctl help` says that
with several devices booted it "will choose one of them", and says nothing about which. A CA
trusted on a device nobody chose reads exactly like a CA trusted on the right one — until the
app under test rejects the certificate on the device the runner is actually driving.
"""

import json

import cli
import config
import ownership
import session
import simulator as sim
from cli_doubles import (
    _PHONE,
    FakeHealth,
    FakeNetwork,
    FakePsutil,
    _fake_proxy,
    _status_network,
    _up_with_a_proxy,
    answering,
    fake_simctl,
    record,
    up_after,
    write_journal,
)

# The rest of the device family `cli_doubles` explains beside `_PHONE`; only this module needs
# a second device, or to pick one simctl subcommand out of the recorded calls.
_PAD = {"udid": "PAD-1", "name": "iPad Pro 13-inch", "state": "Booted", "isAvailable": True}


def simctl_calls(calls, subcommand):
    return [call for call in calls if subcommand in call]


def _generated_ca(monkeypatch, tmp_path):
    """A CA file on disk, so `trust_ca_in_sim` gets as far as shelling out."""
    cert = tmp_path / "mitmproxy-ca-cert.pem"
    cert.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(sim, "_ca_cert", lambda: cert)


def _up_on_a_device(profile, monkeypatch, tmp_path, devices, **simctl):
    """A fresh `up` for a profile that names an app, with the real CA trust and relaunch reaching a
    fake simctl. Nothing is stubbed between `up` and the simctl arguments the tests below read."""
    up_after(
        monkeypatch,
        profile,
        None,
        FakeNetwork(),
        FakePsutil(),
        stub_trust=False,
        bundle_id="com.example.Store",
    )
    _generated_ca(monkeypatch, tmp_path)
    return fake_simctl(monkeypatch, devices, **simctl)


def _journalled_simulator():
    journal = session.Session().read()
    return None if journal.simulator is None else {"udid": journal.simulator.udid, "name": journal.simulator.name}


def test_up_uses_the_one_booted_simulator_without_being_told(profile, runner, monkeypatch, tmp_path):
    """The convenient case stays convenient: one booted device needs no --simulator."""
    calls = _up_on_a_device(profile, monkeypatch, tmp_path, [_PHONE])

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    keychain = simctl_calls(calls, "keychain")
    assert len(keychain) == 1 and _PHONE["udid"] in keychain[0]
    assert "booted" not in keychain[0], "the device must be named by UDID, never left to simctl"


def test_up_refuses_a_simulator_that_is_not_booted(profile, runner, monkeypatch, tmp_path):
    """Absent and shut down are different problems with different answers, and neither answer is
    "trust it somewhere else"."""
    calls = _up_on_a_device(profile, monkeypatch, tmp_path, [{**_PAD, "state": "Shutdown"}])

    result = runner.invoke(cli.cli, ["up", "--simulator", _PAD["name"]])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "not booted" in result.output
    assert f"simctl boot {_PAD['udid']}" in result.output


def test_up_refuses_a_simulator_that_does_not_exist(profile, runner, monkeypatch, tmp_path):
    """Naming a device that is not there must not quietly fall back to the booted one — that is
    the substituted default this codebase keeps having to remove."""
    calls = _up_on_a_device(profile, monkeypatch, tmp_path, [_PHONE])

    result = runner.invoke(cli.cli, ["up", "--simulator", "iPhone 4"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "keychain") == []
    assert "iPhone 4" in result.output
    assert _PHONE["udid"] not in result.output, "it must not offer the device it was not asked for"


def test_up_neither_trusts_nor_relaunches_when_the_simulator_is_ambiguous(profile, runner, monkeypatch, tmp_path):
    """Two booted devices and no choice made: `up` says so and exits non-zero, rather than letting
    simctl pick a device for the CA and pick again for the launch."""
    calls = _up_on_a_device(profile, monkeypatch, tmp_path, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["up"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == [] and simctl_calls(calls, "keychain") == []
    assert "NOT trusted" in result.output and "nothing was relaunched" in result.output
    assert session.Session().read() == ownership.Absent(), "a failed acquisition unwinds and releases"


def test_up_trusts_and_relaunches_on_the_simulator_it_was_given(profile, runner, monkeypatch, tmp_path):
    """Both device operations go to the named device — not to the other booted one, and not to
    `booted` — and `status` can say which device that was."""
    calls = _up_on_a_device(profile, monkeypatch, tmp_path, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["up", "--simulator", _PAD["udid"]])

    assert result.exit_code == 0, result.output
    assert [call[3] for call in simctl_calls(calls, "keychain")] == [_PAD["udid"]]
    assert [call[3] for call in simctl_calls(calls, "launch")] == [_PAD["udid"]]
    assert [call[3] for call in simctl_calls(calls, "terminate")] == [_PAD["udid"]]
    assert _journalled_simulator() == {"udid": _PAD["udid"], "name": _PAD["name"]}


def test_up_fails_when_the_ca_cannot_be_trusted_on_the_chosen_device(profile, runner, monkeypatch, tmp_path):
    """The device was resolved, so the failure is simctl's — and `up` is still not allowed to
    report interception it did not establish."""
    _up_on_a_device(profile, monkeypatch, tmp_path, [_PAD], keychain_status=1)

    result = runner.invoke(cli.cli, ["up", "--simulator", _PAD["name"]])

    assert result.exit_code == 1
    assert "keychain failed" in result.output


def test_up_fails_when_the_app_cannot_be_launched_on_the_chosen_device(profile, runner, monkeypatch, tmp_path):
    _up_on_a_device(profile, monkeypatch, tmp_path, [_PAD], launch_status=1)

    result = runner.invoke(cli.cli, ["up", "--simulator", _PAD["name"]])

    assert result.exit_code == 1
    assert f"com.example.Store is not installed in {_PAD['name']}" in result.output


def test_status_reports_the_simulator_the_last_up_used(profile, runner, monkeypatch):
    """Reported from the journal rather than by looking again: the question is which device this
    run trusted, and a fresh lookup would name whatever is booted now."""
    world = _status_network(monkeypatch)
    write_journal(record(world["proxy"], simulator=ownership.Simulator(udid=_PAD["udid"], name=_PAD["name"])))
    FakeHealth(sequence=[answering(world["proxy"].pid)]).install(monkeypatch)

    assert json.loads(runner.invoke(cli.cli, ["status", "--json"]).output)["simulator"] == {
        "udid": _PAD["udid"],
        "name": _PAD["name"],
    }
    plain = runner.invoke(cli.cli, ["status"])
    assert _PAD["udid"] in plain.output
    assert "not scoped to it" in plain.output, "device selection must not read as traffic isolation"


def test_up_still_selects_the_scenario_when_there_is_no_simulator_to_relaunch_on(profile, runner, monkeypatch):
    """`--use` and `--simulator` fail independently.

    A caller left to launch the app by hand still asked for that scenario, so the selection
    happens; the relaunch does not, because there is no device to relaunch on; and `up` exits 1
    about the device rather than reporting a run it did not set up.
    """
    state = _fake_proxy(monkeypatch)
    _up_with_a_proxy(profile, monkeypatch, state)
    fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["up", "--use", "orders-outage"])

    assert result.exit_code == 1
    assert state["events"] == [("activate", "orders-outage")]
    assert state["launched"] == [] and state["devices"] == []
    assert "simulators are booted" in result.output
    assert "nothing was relaunched" in result.output


# MARK: - `relaunch`: the app's button and the CLI take the same road


def _journal_bound_to_the_pad(profile):
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8"
    )
    config.reload_profile()
    write_journal(
        record(
            ownership.Ref(pid=4321, create_time=1000.5),
            simulator=ownership.Simulator(udid=_PAD["udid"], name=_PAD["name"]),
        )
    )


def test_relaunch_uses_the_device_up_recorded_not_whatever_is_booted(profile, runner, monkeypatch):
    """The menu-bar app's Relaunch runs this. It used to run `simctl launch booted`, which lets
    simctl choose between two booted devices — half the time the one that never got the CA."""
    _journal_bound_to_the_pad(profile)
    calls = fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 0, result.output
    assert [call[3] for call in simctl_calls(calls, "launch")] == [_PAD["udid"]]
    assert [call[3] for call in simctl_calls(calls, "terminate")] == [_PAD["udid"]]
    assert not any("booted" in call for call in calls), "simctl must never be left to choose"


def test_relaunch_refuses_when_the_recorded_device_has_since_shut_down(profile, runner, monkeypatch):
    """The recorded UDID is checked, not trusted: the CA lives on that device, so relaunching
    anywhere else would put the app in front of a certificate it does not trust."""
    _journal_bound_to_the_pad(profile)
    calls = fake_simctl(monkeypatch, [_PHONE, {**_PAD, "state": "Shutdown"}])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == [], "it must not relaunch on the other booted device"
    assert "not booted" in result.output and _PAD["udid"] in result.output


def test_relaunch_refuses_rather_than_pick_between_two_booted_devices(profile, runner, monkeypatch):
    """With no run recorded — no `up` yet, or one that failed to resolve a device — the rule is
    `up`'s own: the single candidate, or a refusal that lists them."""
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8"
    )
    calls = fake_simctl(monkeypatch, [_PHONE, _PAD])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == []
    assert "--simulator" in result.output


def test_relaunch_says_what_is_missing_when_no_bundle_id_can_be_found(profile, runner, monkeypatch):
    """`--relaunch`'s failure mode, in a command whose only job is the launch: it must not exit 0
    having launched nothing."""
    (profile / "profile.json").write_text('{"hosts": ["api.example.com"]}', encoding="utf-8")
    calls = fake_simctl(monkeypatch, [_PHONE])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == []
    assert "simBundleId" in result.output


def test_relaunch_reports_a_launch_simctl_refused(profile, runner, monkeypatch):
    calls = fake_simctl(monkeypatch, [_PHONE], launch_status=1)

    result = runner.invoke(cli.cli, ["relaunch", "com.example.Store"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") != []
    assert "com.example.Store is not installed" in result.output


def test_relaunch_refuses_over_a_journal_it_cannot_read(profile, runner, monkeypatch):
    """A corrupt journal read as "nothing recorded" resolved to whatever is booted — the device
    without the CA, half the time. Unreadable is refused with the file named."""
    (profile / "profile.json").write_text(
        '{"hosts": ["api.example.com"], "simBundleId": "com.example.Store"}', encoding="utf-8"
    )
    store = write_journal(None)
    store.journal_path.write_bytes(b"not json at all\xff")
    calls = fake_simctl(monkeypatch, [_PHONE])

    result = runner.invoke(cli.cli, ["relaunch"])

    assert result.exit_code == 1
    assert simctl_calls(calls, "launch") == []
    assert "cannot be read" in result.output and "--simulator" in result.output
