"""explain-match, live and offline.

The live command asks the running proxy for its rules. `validate` and `explain-match --scenario`
answer the same questions from a scenario file instead, under their own mark below.
"""

import io
import json

import api
import cli
import config

_RULES = [
    {"id": "ovr_broad", "mode": "replace", "match": {"method": "GET", "path": "/api/items"}},
    {
        "id": "ovr_alpha",
        "mode": "replace",
        "match": {"method": "GET", "path": "/api/items", "query": {"kind": "alpha"}},
    },
    {"id": "ovr_other", "mode": "replace", "match": {"method": "GET", "path": "/api/orders"}},
    {"id": "ovr_off", "active": False, "mode": "replace", "match": {"path": "/api/items"}},
]


def test_explain_match_names_the_winner_and_what_it_shadowed(profile, runner, monkeypatch):
    """The over-match made visible: the broad rule answers alpha, beta and gamma alike, and nothing
    in the tool used to say so until it had already happened."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items?kind=alpha"])
    assert result.exit_code == 0
    assert "ovr_alpha" in result.output
    assert "also matched" in result.output and "ovr_broad" in result.output


def test_explain_match_reads_a_repeated_query_key_the_way_the_wire_does(profile, runner, monkeypatch):
    """mitmproxy's MultiDict returns the first value; `dict(parse_qsl(...))` kept the last. For
    `?kind=beta&kind=alpha` the proxy selects on beta while this command explained alpha — a
    selection it then swore would happen."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items?kind=beta&kind=alpha"])
    assert result.exit_code == 0
    assert "rule wants 'alpha', request has 'beta'" in result.output
    assert "ovr_alpha is selected" not in result.output


def test_explain_match_says_why_each_rule_missed(profile, runner, monkeypatch):
    monkeypatch.setattr(api, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items?kind=beta"])
    assert result.exit_code == 0
    assert "ovr_broad" in result.output
    assert "rule wants 'alpha', request has 'beta'" in result.output


def test_explain_match_exits_non_zero_when_nothing_is_selected(profile, runner, monkeypatch):
    """Usable as a check in a script: a rule you cannot select is a rule that will never fire."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "POST", "/api/nothing"])
    assert result.exit_code == 1
    assert "no active rule" in result.output


def test_explain_match_reports_an_inactive_rule_separately(profile, runner, monkeypatch):
    """It matches and still cannot answer. Listing it among the misses would be a lie; leaving it
    out entirely loses the answer to "why is my rule not firing?"."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/api/items"])
    assert "inactive" in result.output and "ovr_off" in result.output


def test_explain_match_does_not_claim_a_patch_will_answer(profile, runner, monkeypatch):
    """Whether a patch answers depends on the upstream content type, which no dry run can know."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: [{"id": "p", "mode": "patch", "match": {"path": "/a"}}])
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/a"])
    assert result.exit_code == 0
    assert "is selected" in result.output
    assert "only if the upstream response is JSON" in result.output


def test_control_surfaces_the_apis_detail_not_just_its_slug(profile, runner, monkeypatch):
    """The API sends the sentence that names the problem and a slug for it. Printing the slug is
    how a supported matcher field ends up looking unsupported."""
    import urllib.error

    def raise_http(*_args, **_kwargs):
        body = io.BytesIO(json.dumps({"error": "invalid_payload", "detail": "match: unknown field 'kind'"}).encode())
        raise urllib.error.HTTPError("http://example.test", 400, "Bad Request", {}, body)

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    result = runner.invoke(cli.cli, ["override", "add", '{"mode":"replace"}'])
    assert result.exit_code == 1
    assert "unknown field 'kind'" in result.output


# MARK: - Offline inspection: `validate` and `explain-match --scenario`
#
# These commands exist because the alternative was starting a proxy, changing the Mac's network
# settings and walking through an app to discover a typo in a JSON file. So the thing worth pinning
# hardest is the negative: no proxy, no network, and nothing written.


def write_scenario(profile, name, payload):
    """A scenario file exactly as an operator would hand-write one."""
    path = profile / "scenarios" / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


_WHOLE = {
    "name": "whole",
    "overrides": [
        {"id": "ovr_orders", "mode": "replace", "status": 500, "match": {"method": "GET", "path": "/api/v1/orders/*"}},
    ],
}


_PARTIAL = {
    "name": "partial",
    "overrides": [
        {"id": "ovr_orders", "mode": "replace", "status": 500, "match": {"method": "GET", "path": "/api/v1/orders/*"}},
        {"id": "ovr_typo", "mode": "replace", "status": 200, "match": {"paths": "/api/v1/users"}},
        {"id": "ovr_orders", "mode": "replace", "status": 204, "match": {"path": "/api/v1/dupe"}},
    ],
}


def test_validate_accepts_a_scenario_that_loads_whole(profile, runner, offline):
    write_scenario(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whole"])
    assert result.exit_code == 0
    assert "1 rule(s)" in result.output


def test_validate_reports_malformed_json_and_exits_non_zero(profile, runner, offline):
    write_scenario(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["validate", "broken"])
    assert result.exit_code == 1
    assert "not loaded at all" in result.output
    assert "broken.json" in result.output


def test_validate_reports_a_partly_loaded_scenario_rule_by_rule(profile, runner, offline):
    """The failure the issue is about: startup keeps the rules it can read and carries on, so a
    scenario runs live and quietly missing the one rule the test depends on. "Something in this
    file is wrong" costs the same walk through the app this exists to avoid, so each dropped rule
    is named by index and by the field that dropped it — an unknown matcher field here, and a
    duplicate id, which cannot be kept because two rules sharing one share a cursor and an answer
    count and cannot be removed independently."""
    write_scenario(profile, "partial", _PARTIAL)
    result = runner.invoke(cli.cli, ["validate", "partial"])
    assert result.exit_code == 1
    assert "1 rule(s) kept, 2 dropped" in result.output
    assert "partial.json" in result.output
    assert "override[1]" in result.output and "'paths'" in result.output
    assert "override[2]" in result.output and "duplicate id" in result.output


def test_validate_refuses_an_unsupported_schema_version(profile, runner, offline):
    write_scenario(profile, "future", {"schemaVersion": 2, "name": "future", "overrides": []})
    result = runner.invoke(cli.cli, ["validate", "future"])
    assert result.exit_code == 1
    assert "schemaVersion" in result.output and "not loaded at all" in result.output


def test_validate_reports_a_file_that_points_at_itself(profile, runner, offline):
    """`Path.resolve()` raises RuntimeError on a symlink loop, and it is not an OSError. Uncaught,
    the command died on a traceback — with `--json`, before printing any of the diagnostics it
    promises, which is the one output a caller cannot recover from."""
    loop = profile / "scenarios" / "loop.json"
    loop.symlink_to(loop)
    text = runner.invoke(cli.cli, ["validate", "loop"])
    machine = runner.invoke(cli.cli, ["validate", "--json"])
    assert text.exit_code == 1 and "cannot resolve path" in text.output
    assert machine.exit_code == 1
    payload = json.loads(machine.output)
    assert any("loop.json" in problem for scenario in payload["scenarios"] for problem in scenario["problems"])


def test_validate_reports_a_delay_that_is_not_a_finite_number(profile, runner, offline):
    """`1e309` parses as `inf` and `int(inf)` raises OverflowError, which is not a ValidationError
    and not even a ValueError — so the rule that should have been one line of diagnostics took the
    whole command down instead."""
    write_scenario(
        profile,
        "wild",
        {
            "name": "wild",
            "overrides": [
                {"id": "ovr_slow", "mode": "replace", "status": 200, "delayMs": 1e309},
            ],
        },
    )
    result = runner.invoke(cli.cli, ["validate", "wild", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert any("override[0]" in problem and "finite" in problem for problem in payload["scenarios"][0]["problems"])


def test_explain_match_reports_a_file_that_points_at_itself(profile, runner, offline):
    loop = profile / "scenarios" / "loop.json"
    loop.symlink_to(loop)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "loop", "--json", "GET", "/a"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] is None
    assert any("cannot resolve path" in problem for problem in payload["problems"])


def test_validate_json_separates_not_loaded_from_loaded_with_problems(profile, runner, offline):
    write_scenario(profile, "broken", "{not json")
    write_scenario(profile, "partial", _PARTIAL)
    write_scenario(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    by_name = {scenario["name"]: scenario for scenario in payload["scenarios"]}
    assert by_name["whole"]["loaded"] is True and by_name["whole"]["ok"] is True
    assert by_name["whole"]["overrideCount"] == 1 and by_name["whole"]["problems"] == []
    assert by_name["partial"]["loaded"] is True and by_name["partial"]["ok"] is False
    assert by_name["partial"]["overrideCount"] == 1
    # null, not 0: a refused file has no rule count, and 0 reads as a scenario that loaded empty.
    assert by_name["broken"]["loaded"] is False and by_name["broken"]["overrideCount"] is None
    assert by_name["broken"]["problems"]


def test_validate_checks_every_scenario_when_none_is_named(profile, runner, offline):
    write_scenario(profile, "whole", _WHOLE)
    write_scenario(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["validate"])
    assert result.exit_code == 1
    assert "whole" in result.output and "broken" in result.output
    assert "2 scenario(s)" in result.output


def test_validate_refuses_a_scenario_that_does_not_exist(profile, runner, offline):
    """A typo and a scenario with no rules need different fixes. Reporting an empty scenario for a
    name nobody wrote is the same false success `--clone-from` was fixed for."""
    write_scenario(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whol"])
    assert result.exit_code == 1
    assert "no scenario 'whol'" in result.output
    assert "whole" in result.output, "the names that do exist are the useful half of the answer"


def test_validate_refuses_a_name_that_would_escape_the_profile(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "../../etc/passwd"])
    assert result.exit_code == 1
    assert "invalid scenario name" in result.output


def test_validate_fails_when_there_is_nothing_to_validate(profile, runner, offline):
    """A command named for checking scenarios that checked none has not validated anything. The
    usual cause is the wrong --profile, so exiting 0 would hide it behind a green tick."""
    result = runner.invoke(cli.cli, ["validate"])
    assert result.exit_code == 1
    assert "no scenario files" in result.output


def test_validate_json_stays_json_when_the_scenario_does_not_exist(profile, runner, offline):
    """A caller that pipes this into `jq` gets prose on exactly the failures it most needs to read.
    Every way the command can fail comes back in the documented shape."""
    write_scenario(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whol", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["scenarios"] == []
    assert any("no scenario 'whol'" in problem for problem in payload["problems"])


def test_validate_json_stays_json_when_there_is_nothing_to_validate(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["scenarios"] == []
    assert any("no scenario files" in problem for problem in payload["problems"])


def test_validate_json_stays_json_for_a_name_that_could_not_be_one(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "../../etc/passwd", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert any("invalid scenario name" in problem for problem in payload["problems"])


def test_validate_does_not_bless_a_scenario_symlinked_out_of_the_profile(profile, runner, offline, tmp_path):
    """Reported as a bulk-mode gap: `validate NAME` refused the escaping file by name while
    `validate` blessed the very same file found by the glob."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "outside", "overrides": []}), encoding="utf-8")
    (profile / "scenarios" / "sneaky.json").symlink_to(outside)
    bulk = runner.invoke(cli.cli, ["validate"])
    named = runner.invoke(cli.cli, ["validate", "sneaky"])
    assert bulk.exit_code == 1 and named.exit_code == 1, "one file, one verdict"
    assert "sneaky" in bulk.output


def test_validate_writes_nothing_to_the_profile(profile, runner, offline):
    """Inspection must not rewrite the profile or change the active scenario — no synthesised
    `default.json`, no re-indented file, no active-scenario pointer."""
    path = write_scenario(profile, "whole", _WHOLE)
    original = path.read_bytes()
    before = sorted(p.name for p in (profile / "scenarios").iterdir())
    assert runner.invoke(cli.cli, ["validate"]).exit_code == 0
    assert path.read_bytes() == original
    assert sorted(p.name for p in (profile / "scenarios").iterdir()) == before
    assert not config.STATE_FILE.exists()


def test_explain_match_against_a_file_never_asks_the_proxy(profile, runner, offline):
    """The whole point: the answer comes from the file and the engine's own matching code, with no
    proxy running and nothing on the machine changed."""
    write_scenario(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "whole", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 0
    assert "ovr_orders is selected" in result.output


def test_explain_match_against_a_file_uses_the_engines_own_matcher(profile, runner, offline):
    """Same reasons, same wording as the live command — a reimplementation would drift, and the
    command exists to be believed."""
    write_scenario(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "whole", "GET", "/api/v1/users"])
    assert result.exit_code == 1
    assert "does not match" in result.output and "/api/v1/orders/*" in result.output


def test_explain_match_against_a_partly_loaded_file_exits_non_zero(profile, runner, offline):
    """A ranking computed without the rules the loader dropped answers "which rule wins" while
    hiding that the rule you asked about was never a candidate."""
    write_scenario(profile, "partial", _PARTIAL)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "partial", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1, "the ranking does not cover the rules the file was written with"
    assert "ovr_orders is selected" in result.output
    assert "dropped at load" in result.output and "override[1]" in result.output


def test_explain_match_names_the_file_it_could_not_read(profile, runner, offline):
    """ "Nothing would be selected" is the sentence an empty scenario produces, and this is not that:
    every rule in the file is absent, not out-ranked."""
    write_scenario(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "broken", "GET", "/a"])
    assert result.exit_code == 1
    assert "broken.json" in result.output
    assert "no active rule would be selected" not in result.output


def test_explain_match_refuses_a_scenario_that_does_not_exist(profile, runner, offline):
    write_scenario(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "nope", "GET", "/a"])
    assert result.exit_code == 1
    assert "no scenario 'nope'" in result.output


def test_explain_match_json_carries_the_diagnostics_for_a_file(profile, runner, offline):
    write_scenario(profile, "partial", _PARTIAL)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "partial", "--json", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] == "ovr_orders"
    assert [c["id"] for c in payload["candidates"]] == ["ovr_orders"]
    assert any("override[1]" in problem for problem in payload["problems"])


def test_explain_match_json_for_an_unreadable_file_still_has_the_live_shape(profile, runner, offline):
    write_scenario(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "broken", "--json", "GET", "/a"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] is None and payload["candidates"] == []
    assert payload["problems"]


def test_explain_match_against_the_proxy_does_not_claim_its_scenario_loaded_whole(profile, runner, monkeypatch):
    """`problems: []` on the live path would assert something this command never checked — the
    running scenario's load problems live in the proxy, not in a file it read."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "--json", "GET", "/api/items?kind=alpha"])
    assert result.exit_code == 0
    assert "problems" not in json.loads(result.output)


# MARK: - The profile layout from before the rename
#
# `sessions/` became `scenarios/`, and an unrenamed profile still holds every scenario it ever had.
# The failure to prevent is the encouraging one: a green "0 scenario(s) load whole", or a "no
# scenario 'orders-outage'" that sends an operator to write the file again — for a profile in which
# it is sitting one directory away.


def make_legacy_profile(profile):
    """The `profile` fixture's directory, with its `scenarios/` renamed back to `sessions/`."""
    (profile / "scenarios").rename(profile / "sessions")
    return profile


def test_validate_refuses_a_legacy_sessions_layout(profile, runner, offline):
    legacy = make_legacy_profile(profile)
    result = runner.invoke(cli.cli, ["validate"])
    assert result.exit_code == 1
    assert f"mv {legacy / 'sessions'} {legacy / 'scenarios'}" in result.output
    assert "load whole" not in result.output
    assert not (legacy / "scenarios").exists()


def test_validate_json_stays_json_for_a_legacy_sessions_layout(profile, runner, offline):
    """The refusal reaches a parsing caller through the envelope `--json` promises, not as a bare
    sentence printed past it."""
    legacy = make_legacy_profile(profile)
    result = runner.invoke(cli.cli, ["validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["scenarios"] == []
    assert any(f"mv {legacy / 'sessions'}" in problem for problem in payload["problems"])


def test_validate_by_name_refuses_a_legacy_sessions_layout(profile, runner, offline):
    """Naming a scenario takes the other path through `_resolve_scenario`, and "no scenario X
    (found: none)" is exactly the wrong answer for a profile that has it under the old name."""
    legacy = make_legacy_profile(profile)
    result = runner.invoke(cli.cli, ["validate", "whole"])
    assert result.exit_code == 1
    assert f"mv {legacy / 'sessions'} {legacy / 'scenarios'}" in result.output
    assert "found: none" not in result.output


def test_explain_match_refuses_a_legacy_sessions_layout(profile, runner, offline):
    legacy = make_legacy_profile(profile)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "whole", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1
    assert f"mv {legacy / 'sessions'} {legacy / 'scenarios'}" in result.output


def test_explain_match_json_stays_json_for_a_legacy_sessions_layout(profile, runner, offline):
    legacy = make_legacy_profile(profile)
    result = runner.invoke(cli.cli, ["explain-match", "--scenario", "whole", "--json", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] is None and payload["candidates"] == []
    assert any(f"mv {legacy / 'sessions'}" in problem for problem in payload["problems"])
