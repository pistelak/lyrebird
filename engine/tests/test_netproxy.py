"""macOS network-service and PAC parsing.

This is the module that rewrites the user's system proxy configuration. The seam is
`subprocess.run`, so everything above it — including `_run`'s translation of a non-zero exit into
`NetworkSetupError` — is exercised without touching the network or shelling out.
"""

import subprocess

import pytest

import netproxy
import ownership as own


def fake_run(monkeypatch, stdout="", returncode=0, stderr=""):
    """Replace the one subprocess seam. Records the argv of every call for assertions.

    Only the exit status is faked; turning a non-zero one into `NetworkSetupError` is left to the
    real `_run`, which is the point of patching this far down.
    """
    calls = []

    def _subprocess_run(args, *rest, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    return calls


# MARK: - pac_status parsing


def test_pac_status_reads_url_and_enabled(monkeypatch, hosts):
    """Verbatim, and nothing else: whether the URL is *ours* is relative to a port and a baseline,
    which `ownership.classify` decides — a `ours` field here could only be relative to this
    process's configured port, which is not the question `down` asks."""
    fake_run(monkeypatch, stdout=f"URL: {netproxy.pac_url()}\nEnabled: Yes\n")
    assert netproxy.pac_status("Wi-Fi") == own.Pac(url=netproxy.pac_url(), enabled=True)


def test_pac_status_raises_when_networksetup_fails(monkeypatch):
    """An unreadable PAC is not an absent one. Read as `("", False, False)`, a failed query made
    `down` say "not ours — left untouched" over a PAC it never saw."""
    fake_run(monkeypatch, stdout="", returncode=1, stderr="** Error: The parameters were not valid.")
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.pac_status("Wi-Fi")


def test_pac_status_treats_null_as_no_url(monkeypatch):
    """macOS prints `(null)` for an unset PAC. Read literally it would look like a foreign PAC,
    and `down` would decline to clear it."""
    fake_run(monkeypatch, stdout="URL: (null)\nEnabled: No\n")
    assert netproxy.pac_status("Wi-Fi") == own.Pac(url="", enabled=False)


# MARK: - intercepting()


def test_intercepting_requires_both_enabled_and_ours(monkeypatch):
    fake_run(monkeypatch, stdout=f"URL: {own.our_url(8088)}\nEnabled: No\n")
    assert netproxy.intercepting("Wi-Fi", 8088) is False

    fake_run(monkeypatch, stdout="URL: http://proxy.example.com/corp.pac\nEnabled: Yes\n")
    assert netproxy.intercepting("Wi-Fi", 8088) is False

    fake_run(monkeypatch, stdout=f"URL: {own.our_url(8088)}\nEnabled: Yes\n")
    assert netproxy.intercepting("Wi-Fi", 8088) is True


def test_intercepting_is_relative_to_the_port_it_was_asked_about(monkeypatch):
    """ "Ours" is a claim about one session. A PAC pointing at port 9099 is not this session's
    interception, and reading it as one is how `status` reported a stranger's proxy as its own."""
    fake_run(monkeypatch, stdout=f"URL: {own.our_url(9099)}\nEnabled: Yes\n")
    assert netproxy.intercepting("Wi-Fi", 8088) is False
    assert netproxy.intercepting("Wi-Fi", 9099) is True


# MARK: - active_service parsing

SERVICE_ORDER = """An asterisk (*) denotes that a network service is disabled.
(1) Wi-Fi
(Hardware Port: Wi-Fi, Device: en0)

(2) Thunderbolt Bridge
(Hardware Port: Thunderbolt Bridge, Device: bridge0)

"""


def test_active_service_maps_the_default_route_to_a_service_name(monkeypatch):
    outputs = iter(["   gateway: 192.0.2.1\n  interface: en0\n", SERVICE_ORDER])

    def _subprocess_run(args, *rest, **kwargs):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    assert netproxy.active_service() == own.ServiceRef(name="Wi-Fi", device="en0")


@pytest.mark.parametrize("returncode", [0, 1], ids=["as macOS does", "if a release ever exits 1"])
def test_active_service_is_none_without_a_default_route(monkeypatch, returncode):
    """What macOS actually does with Wi-Fi off: `route` exits 0, prints nothing, and says
    `not in table` on stderr. That message, and only that, is "no default route" — recognised
    before the exit status is judged, so a release that exits 1 for it would say the same."""
    fake_run(monkeypatch, stdout="", returncode=returncode, stderr="route: writing to routing socket: not in table\n")
    assert netproxy.active_service() is None


def test_active_service_is_none_when_no_service_matches(monkeypatch):
    outputs = iter(["  interface: utun9\n", SERVICE_ORDER])

    def _subprocess_run(args, *rest, **kwargs):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    assert netproxy.active_service() is None


def test_active_service_refuses_two_services_on_the_route_device(monkeypatch):
    """Which of them holds the PAC is not something this Mac can be asked. Answering with the
    first — as a `findall` over the listing did — records a session against a service whose PAC it
    may never have installed."""
    shared = SERVICE_ORDER.replace("Device: bridge0", "Device: en0")
    outputs = iter(["  interface: en0\n", shared])

    def _subprocess_run(args, *rest, **kwargs):
        return subprocess.CompletedProcess(args, 0, next(outputs), "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.active_service()


# MARK: - A command that never returns


def test_every_command_is_bounded(monkeypatch, hosts):
    """The bound is on `subprocess.run` itself, so it is asserted there. Without it a hung
    `networksetup` took its whole caller with it — including `/health`, which runs on the proxy's
    event loop and whose silence the CLI reads as a dead proxy."""
    timeouts = []

    def _subprocess_run(args, *rest, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        out = f"URL: {netproxy.pac_url()}\nEnabled: Yes\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    netproxy.pac_status("Wi-Fi")
    assert timeouts == [netproxy._COMMAND_TIMEOUT]


TIMES_OUT = {
    "pac_status": lambda: netproxy.pac_status("Wi-Fi"),
    "write_pac_url": lambda: netproxy.write_pac_url("Wi-Fi", "http://proxy.example.com/corp.pac"),
    "write_pac_state": lambda: netproxy.write_pac_state("Wi-Fi", True),
    "active_service": netproxy.active_service,
}


@pytest.mark.parametrize("name", list(TIMES_OUT))
def test_a_command_that_does_not_finish_is_raised_not_read_as_no_pac(monkeypatch, hosts, name):
    """A `networksetup` that never answered has said nothing about the PAC. Reported as empty
    output it would parse as "no PAC", which is the mistake `pac_status` exists to prevent — and
    the one that makes `down` delete the only record of what to put back."""

    def _subprocess_run(args, *rest, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    with pytest.raises(netproxy.NetworkSetupError) as raised:
        TIMES_OUT[name]()
    assert "did not finish within 5s" in str(raised.value)


# MARK: - The PAC we advertise


# MARK: - An answer that says nothing is not "no PAC" / "no route"


def test_a_failed_route_command_is_not_no_default_route(monkeypatch):
    """`route` exiting non-zero used to parse as "no default route" — the same None as Wi-Fi
    off — and `down`, finding no service, printed "nothing to stop" over a PAC it never read."""
    fake_run(monkeypatch, stdout="", returncode=1, stderr="route: writing to routing socket: Operation not permitted")
    with pytest.raises(netproxy.NetworkSetupError, match="route -n get default"):
        netproxy.active_service()


def test_a_route_answer_without_an_interface_is_raised(monkeypatch):
    """Exit 0 and no `interface:` line is only "no default route" when `route` says `not in
    table`; anything else is an answer this code does not understand, and None would be a guess."""
    fake_run(monkeypatch, stdout="", stderr="route: writing to routing socket: Invalid argument\n")
    with pytest.raises(netproxy.NetworkSetupError, match="without an interface"):
        netproxy.active_service()


@pytest.mark.parametrize("listing", [("", 1, "** Error: not permitted"), ("", 0, "")], ids=["failed", "empty"])
def test_a_service_listing_that_fails_or_lists_nothing_is_raised(monkeypatch, listing):
    """A Mac with a default route has at least one network service. A listing that fails, or that
    exits 0 with nothing in it, used to read as "no service carries this interface" — and `down`
    then had no service to restore on."""
    stdout, returncode, stderr = listing
    outputs = iter([("  interface: en0\n", 0, ""), (stdout, returncode, stderr)])

    def _subprocess_run(args, *rest, **kwargs):
        out, code, err = next(outputs)
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    with pytest.raises(netproxy.NetworkSetupError, match="listnetworkserviceorder"):
        netproxy.active_service()


@pytest.mark.parametrize("stdout", ["", "URL: http://proxy.example.com/corp.pac\n", "Enabled: Yes\n"])
def test_pac_status_raises_when_the_answer_has_no_url_or_enabled_line(monkeypatch, stdout):
    """`networksetup` exiting 0 without both lines is not a PAC that is absent and not ours: read
    as `("", False, False)`, `down` said "not ours — left untouched" and deleted the record, with
    Lyrebird's PAC still installed."""
    fake_run(monkeypatch, stdout=stdout)
    with pytest.raises(netproxy.NetworkSetupError, match="without a URL/Enabled line"):
        netproxy.pac_status("Wi-Fi")


@pytest.mark.parametrize(
    "stdout",
    ["URL: \nEnabled: Yes\n", "URL:\nEnabled: No\n"],
    ids=["value missing after a space", "value missing"],
)
def test_pac_status_raises_when_a_url_label_has_no_value(monkeypatch, stdout):
    """`URL: ` with nothing after it used to be read across the newline as `URL: Enabled:` — a
    foreign PAC — and `down` left the real one untouched."""
    fake_run(monkeypatch, stdout=stdout)
    with pytest.raises(netproxy.NetworkSetupError, match="without a URL/Enabled line"):
        netproxy.pac_status("Wi-Fi")


def test_pac_status_reads_each_field_from_its_own_whole_line(monkeypatch):
    """A colon is legal in a URL path, so `Enabled:No` can appear inside the PAC URL. Pins the
    anchoring: an `Enabled:` matched anywhere in the output would read the state off the URL and
    snapshot an enabled PAC as disabled. (Not a regression of the original parser, which looked
    for the literal `Enabled: Yes`; the anchored form is what keeps both fields on their lines.)"""
    fake_run(monkeypatch, stdout="URL: http://proxy.example.com/Enabled:No/corp.pac\nEnabled: Yes\n")
    status = netproxy.pac_status("Wi-Fi")
    assert status.url == "http://proxy.example.com/Enabled:No/corp.pac"
    assert status.enabled is True


def test_a_command_that_cannot_start_is_a_network_setup_error(monkeypatch):
    """A `networksetup` that could not be spawned is the same silence as one that hung, and it
    used to leave `_discover_service` — which catches only `NetworkSetupError` — to traceback out
    of `up`, `down` and `status --json`."""

    def _subprocess_run(args, *rest, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", args[0])

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    with pytest.raises(netproxy.NetworkSetupError, match="could not run `networksetup -getautoproxyurl Wi-Fi`"):
        netproxy.pac_status("Wi-Fi")


# MARK: - The by-device half: route, service table, resolution
#
# The PAC is set by *name* and the session records a *device*, because a name can be changed while
# a session holds the PAC. Everything below is about the two failing the same way: a listing that
# could not be parsed whole is not a service that is gone, and a service that is gone is a proof.

LISTING = """An asterisk (*) denotes that a network service is disabled.
(1) Wi-Fi
(Hardware Port: Wi-Fi, Device: en0)

(2) Thunderbolt Bridge
(Hardware Port: Thunderbolt Bridge, Device: bridge0)

(*) Old Ethernet
(Hardware Port: Ethernet, Device: en5)

"""


def answering(monkeypatch, output, returncode=0, stderr=""):
    """One `networksetup`/`route` answer for every call, with the kwargs recorded."""
    calls = []

    def _subprocess_run(args, *rest, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, output, stderr)

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    return calls


def test_service_table_reads_every_entry(monkeypatch):
    answering(monkeypatch, LISTING)
    assert netproxy.service_table() == [("Wi-Fi", "en0"), ("Thunderbolt Bridge", "bridge0"), ("Old Ethernet", "en5")]


def test_service_table_keeps_a_disabled_service(monkeypatch):
    """`(*)` means disabled, not absent: it is still listed and still holds its PAC, and dropping
    it would have `down` refuse a session whose service was right there."""
    answering(monkeypatch, LISTING)
    assert ("Old Ethernet", "en5") in netproxy.service_table()


@pytest.mark.parametrize(
    "output",
    [
        LISTING.replace("(Hardware Port: Ethernet, Device: en5)", "(Hardware Port: Ethernet, Devic"),
        LISTING.replace("(2) Thunderbolt Bridge", "2) Thunderbolt Bridge"),
        LISTING + "and one more thing\n",
        LISTING.replace("An asterisk (*) denotes that a network service is disabled.\n", ""),
        "",
    ],
    ids=["a truncated hardware line", "a mangled entry line", "a line after the last block", "no legend", "nothing"],
)
def test_service_table_rejects_a_partially_parsable_listing(monkeypatch, output):
    """A `findall` drops what it cannot match: one truncated entry for the journalled device read
    as gone, and `down` refused a session that had nothing wrong with it."""
    answering(monkeypatch, output)
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.service_table()


def test_route_device_reads_the_interface(monkeypatch):
    answering(monkeypatch, "   gateway: 192.0.2.1\n  interface: en0\n")
    assert netproxy.route_device() == "en0"


def test_route_device_is_none_only_for_a_route_that_is_not_in_the_table(monkeypatch):
    answering(monkeypatch, "", stderr="route: writing to routing socket: not in table\n")
    assert netproxy.route_device() is None
    answering(monkeypatch, "", returncode=1, stderr="route: bad address")
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.route_device()


# MARK: - The single-command writers


def test_write_pac_url_is_one_command(monkeypatch):
    """No read-back here: that belongs to the recipe, which performs it against a name it resolved
    once for the whole invocation."""
    calls = answering(monkeypatch, "")
    netproxy.write_pac_url("Wi-Fi", "http://proxy.example.com/corp.pac")
    assert calls[0][0] == ["networksetup", "-setautoproxyurl", "Wi-Fi", "http://proxy.example.com/corp.pac"]
    assert len(calls) == 1


@pytest.mark.parametrize(("on", "word"), [(True, "on"), (False, "off")])
def test_write_pac_state_is_one_command(monkeypatch, on, word):
    calls = answering(monkeypatch, "")
    netproxy.write_pac_state("Wi-Fi", on)
    assert calls[0][0] == ["networksetup", "-setautoproxystate", "Wi-Fi", word]
    assert len(calls) == 1


def test_a_write_that_exits_non_zero_is_a_network_setup_error(monkeypatch):
    answering(monkeypatch, "", returncode=1, stderr="** Error: The parameters were not valid.")
    with pytest.raises(netproxy.NetworkSetupError):
        netproxy.write_pac_state("Wi-Fi", True)


def test_a_command_with_undecodable_output_is_a_network_setup_error(monkeypatch):
    """`subprocess.run(text=True)` decodes strictly. Anything but a `NetworkSetupError` out of an
    observation escapes every caller's mapping and tracebacks out of the command that met it."""

    def _subprocess_run(args, *rest, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(netproxy.subprocess, "run", _subprocess_run)
    with pytest.raises(netproxy.NetworkSetupError, match="could not be decoded"):
        netproxy.pac_status("Wi-Fi")
