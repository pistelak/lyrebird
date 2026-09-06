"""explain-match, live and offline.

The live command asks the running proxy for its rules. `validate` and `explain-match --session`
answer the same questions from a session file instead, under their own mark below.
"""

import json

import api
import cli
import config

_RULES = [
    {"id": "ovr_broad", "mode": "replace", "match": {"method": "GET", "path": "/api/items"}},
    {"id": "ovr_alpha", "mode": "replace",
     "match": {"method": "GET", "path": "/api/items", "query": {"kind": "alpha"}}},
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
    monkeypatch.setattr(api, "_control",
                        lambda *a, **k: [{"id": "p", "mode": "patch", "match": {"path": "/a"}}])
    result = runner.invoke(cli.cli, ["explain-match", "GET", "/a"])
    assert result.exit_code == 0
    assert "is selected" in result.output
    assert "only if the upstream response is JSON" in result.output


def test_control_surfaces_the_apis_detail_not_just_its_slug(profile, runner, monkeypatch):
    """The API sends the sentence that names the problem and a slug for it. Printing the slug is
    how a supported matcher field ends up looking unsupported."""
    import urllib.error

    class _Body:
        @staticmethod
        def read():
            return json.dumps({"error": "invalid_payload",
                               "detail": "match: unknown field 'kind'"}).encode()

    def raise_http(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, _Body())  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    result = runner.invoke(cli.cli, ["override", "add", '{"mode":"replace"}'])
    assert result.exit_code == 1
    assert "unknown field 'kind'" in result.output


# MARK: - Offline inspection: `validate` and `explain-match --session`
#
# These commands exist because the alternative was starting a proxy, changing the Mac's network
# settings and walking through an app to discover a typo in a JSON file. So the thing worth pinning
# hardest is the negative: no proxy, no network, and nothing written.


def write_session(profile, name, payload):
    """A session file exactly as an operator would hand-write one."""
    path = profile / "sessions" / f"{name}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


_WHOLE = {"name": "whole", "overrides": [
    {"id": "ovr_orders", "mode": "replace", "status": 500,
     "match": {"method": "GET", "path": "/api/v1/orders/*"}},
]}


_PARTIAL = {"name": "partial", "overrides": [
    {"id": "ovr_orders", "mode": "replace", "status": 500,
     "match": {"method": "GET", "path": "/api/v1/orders/*"}},
    {"id": "ovr_typo", "mode": "replace", "status": 200, "match": {"paths": "/api/v1/users"}},
    {"id": "ovr_orders", "mode": "replace", "status": 204, "match": {"path": "/api/v1/dupe"}},
]}


def test_validate_accepts_a_session_that_loads_whole(profile, runner, offline):
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whole"])
    assert result.exit_code == 0
    assert "1 rule(s)" in result.output


def test_validate_reports_malformed_json_and_exits_non_zero(profile, runner, offline):
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["validate", "broken"])
    assert result.exit_code == 1
    assert "not loaded at all" in result.output
    assert "broken.json" in result.output


def test_validate_reports_a_partly_loaded_session_rule_by_rule(profile, runner, offline):
    """The failure the issue is about: startup keeps the rules it can read and carries on, so a
    scenario runs live and quietly missing the one rule the test depends on. "Something in this
    file is wrong" costs the same walk through the app this exists to avoid, so each dropped rule
    is named by index and by the field that dropped it — an unknown matcher field here, and a
    duplicate id, which cannot be kept because two rules sharing one share a cursor and an answer
    count and cannot be removed independently."""
    write_session(profile, "partial", _PARTIAL)
    result = runner.invoke(cli.cli, ["validate", "partial"])
    assert result.exit_code == 1
    assert "1 rule(s) kept, 2 dropped" in result.output
    assert "partial.json" in result.output
    assert "override[1]" in result.output and "'paths'" in result.output
    assert "override[2]" in result.output and "duplicate id" in result.output


def test_validate_refuses_an_unsupported_schema_version(profile, runner, offline):
    write_session(profile, "future", {"schemaVersion": 2, "name": "future", "overrides": []})
    result = runner.invoke(cli.cli, ["validate", "future"])
    assert result.exit_code == 1
    assert "schemaVersion" in result.output and "not loaded at all" in result.output


def test_validate_reports_a_file_that_points_at_itself(profile, runner, offline):
    """`Path.resolve()` raises RuntimeError on a symlink loop, and it is not an OSError. Uncaught,
    the command died on a traceback — with `--json`, before printing any of the diagnostics it
    promises, which is the one output a caller cannot recover from."""
    loop = profile / "sessions" / "loop.json"
    loop.symlink_to(loop)
    text = runner.invoke(cli.cli, ["validate", "loop"])
    machine = runner.invoke(cli.cli, ["validate", "--json"])
    assert text.exit_code == 1 and "cannot resolve path" in text.output
    assert machine.exit_code == 1
    payload = json.loads(machine.output)
    assert any("loop.json" in problem
               for session in payload["sessions"] for problem in session["problems"])


def test_validate_reports_a_delay_that_is_not_a_finite_number(profile, runner, offline):
    """`1e309` parses as `inf` and `int(inf)` raises OverflowError, which is not a ValidationError
    and not even a ValueError — so the rule that should have been one line of diagnostics took the
    whole command down instead."""
    write_session(profile, "wild", {"name": "wild", "overrides": [
        {"id": "ovr_slow", "mode": "replace", "status": 200, "delayMs": 1e309},
    ]})
    result = runner.invoke(cli.cli, ["validate", "wild", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert any("override[0]" in problem and "finite" in problem
               for problem in payload["sessions"][0]["problems"])


def test_explain_match_reports_a_file_that_points_at_itself(profile, runner, offline):
    loop = profile / "sessions" / "loop.json"
    loop.symlink_to(loop)
    result = runner.invoke(cli.cli, ["explain-match", "--session", "loop", "--json", "GET", "/a"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] is None
    assert any("cannot resolve path" in problem for problem in payload["problems"])


def test_validate_json_separates_not_loaded_from_loaded_with_problems(profile, runner, offline):
    write_session(profile, "broken", "{not json")
    write_session(profile, "partial", _PARTIAL)
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    by_name = {session["name"]: session for session in payload["sessions"]}
    assert by_name["whole"]["loaded"] is True and by_name["whole"]["ok"] is True
    assert by_name["whole"]["overrideCount"] == 1 and by_name["whole"]["problems"] == []
    assert by_name["partial"]["loaded"] is True and by_name["partial"]["ok"] is False
    assert by_name["partial"]["overrideCount"] == 1
    # null, not 0: a refused file has no rule count, and 0 reads as a session that loaded empty.
    assert by_name["broken"]["loaded"] is False and by_name["broken"]["overrideCount"] is None
    assert by_name["broken"]["problems"]


def test_validate_checks_every_session_when_none_is_named(profile, runner, offline):
    write_session(profile, "whole", _WHOLE)
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["validate"])
    assert result.exit_code == 1
    assert "whole" in result.output and "broken" in result.output
    assert "2 session(s)" in result.output


def test_validate_refuses_a_session_that_does_not_exist(profile, runner, offline):
    """A typo and a scenario with no rules need different fixes. Reporting an empty session for a
    name nobody wrote is the same false success `--clone-from` was fixed for."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whol"])
    assert result.exit_code == 1
    assert "no session 'whol'" in result.output
    assert "whole" in result.output, "the names that do exist are the useful half of the answer"


def test_validate_refuses_a_name_that_would_escape_the_profile(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "../../etc/passwd"])
    assert result.exit_code == 1
    assert "invalid session name" in result.output


def test_validate_fails_when_there_is_nothing_to_validate(profile, runner, offline):
    """A command named for checking sessions that checked none has not validated anything. The
    usual cause is the wrong --profile, so exiting 0 would hide it behind a green tick."""
    result = runner.invoke(cli.cli, ["validate"])
    assert result.exit_code == 1
    assert "no session files" in result.output


def test_validate_json_stays_json_when_the_session_does_not_exist(profile, runner, offline):
    """A caller that pipes this into `jq` gets prose on exactly the failures it most needs to read.
    Every way the command can fail comes back in the documented shape."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["validate", "whol", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["sessions"] == []
    assert any("no session 'whol'" in problem for problem in payload["problems"])


def test_validate_json_stays_json_when_there_is_nothing_to_validate(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["sessions"] == []
    assert any("no session files" in problem for problem in payload["problems"])


def test_validate_json_stays_json_for_a_name_that_could_not_be_one(profile, runner, offline):
    result = runner.invoke(cli.cli, ["validate", "../../etc/passwd", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert any("invalid session name" in problem for problem in payload["problems"])


def test_validate_does_not_bless_a_session_symlinked_out_of_the_profile(profile, runner, offline,
                                                                       tmp_path):
    """Reported as a bulk-mode gap: `validate NAME` refused the escaping file by name while
    `validate` blessed the very same file found by the glob."""
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "outside", "overrides": []}), encoding="utf-8")
    (profile / "sessions" / "sneaky.json").symlink_to(outside)
    bulk = runner.invoke(cli.cli, ["validate"])
    named = runner.invoke(cli.cli, ["validate", "sneaky"])
    assert bulk.exit_code == 1 and named.exit_code == 1, "one file, one verdict"
    assert "sneaky" in bulk.output


def test_validate_writes_nothing_to_the_profile(profile, runner, offline):
    """Inspection must not rewrite the profile or change the active session — no synthesised
    `default.json`, no re-indented file, no active-session pointer."""
    path = write_session(profile, "whole", _WHOLE)
    original = path.read_bytes()
    before = sorted(p.name for p in (profile / "sessions").iterdir())
    assert runner.invoke(cli.cli, ["validate"]).exit_code == 0
    assert path.read_bytes() == original
    assert sorted(p.name for p in (profile / "sessions").iterdir()) == before
    assert not config.STATE_FILE.exists()


def test_explain_match_against_a_file_never_asks_the_proxy(profile, runner, offline):
    """The whole point: the answer comes from the file and the engine's own matching code, with no
    proxy running and nothing on the machine changed."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(
        cli.cli, ["explain-match", "--session", "whole", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 0
    assert "ovr_orders is selected" in result.output


def test_explain_match_against_a_file_uses_the_engines_own_matcher(profile, runner, offline):
    """Same reasons, same wording as the live command — a reimplementation would drift, and the
    command exists to be believed."""
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["explain-match", "--session", "whole", "GET", "/api/v1/users"])
    assert result.exit_code == 1
    assert "does not match" in result.output and "/api/v1/orders/*" in result.output


def test_explain_match_against_a_partly_loaded_file_exits_non_zero(profile, runner, offline):
    """A ranking computed without the rules the loader dropped answers "which rule wins" while
    hiding that the rule you asked about was never a candidate."""
    write_session(profile, "partial", _PARTIAL)
    result = runner.invoke(
        cli.cli, ["explain-match", "--session", "partial", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1, "the ranking does not cover the rules the file was written with"
    assert "ovr_orders is selected" in result.output
    assert "dropped at load" in result.output and "override[1]" in result.output


def test_explain_match_names_the_file_it_could_not_read(profile, runner, offline):
    """"Nothing would be selected" is the sentence an empty session produces, and this is not that:
    every rule in the file is absent, not out-ranked."""
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["explain-match", "--session", "broken", "GET", "/a"])
    assert result.exit_code == 1
    assert "broken.json" in result.output
    assert "no active rule would be selected" not in result.output


def test_explain_match_refuses_a_session_that_does_not_exist(profile, runner, offline):
    write_session(profile, "whole", _WHOLE)
    result = runner.invoke(cli.cli, ["explain-match", "--session", "nope", "GET", "/a"])
    assert result.exit_code == 1
    assert "no session 'nope'" in result.output


def test_explain_match_json_carries_the_diagnostics_for_a_file(profile, runner, offline):
    write_session(profile, "partial", _PARTIAL)
    result = runner.invoke(
        cli.cli, ["explain-match", "--session", "partial", "--json", "GET", "/api/v1/orders/42"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] == "ovr_orders"
    assert [c["id"] for c in payload["candidates"]] == ["ovr_orders"]
    assert any("override[1]" in problem for problem in payload["problems"])


def test_explain_match_json_for_an_unreadable_file_still_has_the_live_shape(profile, runner, offline):
    write_session(profile, "broken", "{not json")
    result = runner.invoke(cli.cli, ["explain-match", "--session", "broken", "--json", "GET", "/a"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["selected"] is None and payload["candidates"] == []
    assert payload["problems"]


def test_explain_match_against_the_proxy_does_not_claim_its_session_loaded_whole(profile, runner,
                                                                                monkeypatch):
    """`problems: []` on the live path would assert something this command never checked — the
    running session's load problems live in the proxy, not in a file it read."""
    monkeypatch.setattr(api, "_control", lambda *a, **k: _RULES)
    result = runner.invoke(cli.cli, ["explain-match", "--json", "GET", "/api/items?kind=alpha"])
    assert result.exit_code == 0
    assert "problems" not in json.loads(result.output)
