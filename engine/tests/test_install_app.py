"""`install-app.sh` must fail when it did not install: its failure paths, over fake tools.

The script shells out for everything that decides the outcome — pgrep, ps, xcodebuild, open — so
these checks put fakes for those first on PATH and leave `git`, `ditto`, `PlistBuddy`, `mv` and
`rm` real: the copy, the version read-back and the swap are worth exercising as themselves.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]

# Above any pid macOS will hand out, so it is a pid that is genuinely gone.
GONE_PID = "99999901"

# Stays alive through the SIGTERM it records in argv[1], so what "quit" means stays the fake pgrep's.
STUBBORN_PROCESS = (
    "import signal, sys, time; "
    "signal.signal(signal.SIGTERM, lambda *_: open(sys.argv[1], 'w').write('sigterm')); "
    "time.sleep(120)"
)

FAKE_XCODEBUILD = """#!/bin/bash
set -euo pipefail
version=""
derived=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -derivedDataPath) derived="$2"; shift 2 ;;
    CURRENT_PROJECT_VERSION=*) version="${1#CURRENT_PROJECT_VERSION=}"; shift ;;
    *) shift ;;
  esac
done
if [[ -z "$derived" ]]; then echo "fake xcodebuild: no -derivedDataPath" >&2; exit 1; fi
# Our parent is the install-app.sh shell, so this is the one place that can name a directory after
# the pid that run has: what an earlier run interrupted at the wrong moment would have left behind
# if its pid were reused.
for kind in previous installing; do
  if [[ -f "$LYREBIRD_TEST_STATE/seed-$kind" ]]; then
    seeded="$(cat "$LYREBIRD_TEST_STATE/seed-$kind")/.Lyrebird.app.$kind-$PPID"
    mkdir -p "$seeded/Lyrebird.app/Contents"
    echo 'rescued from an interrupted run' > "$seeded/Lyrebird.app/Contents/marker.txt"
    echo "$seeded" > "$LYREBIRD_TEST_STATE/seeded-$kind"
  fi
done
if [[ -f "$LYREBIRD_TEST_STATE/build-version" ]]; then
  version="$(cat "$LYREBIRD_TEST_STATE/build-version")"
fi
app="$derived/Build/Products/Release/Lyrebird.app"
mkdir -p "$app/Contents/MacOS"
echo 'fake executable' > "$app/Contents/MacOS/Lyrebird"
chmod 755 "$app/Contents/MacOS/Lyrebird"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.example.Lyrebird</string>
    <key>CFBundleShortVersionString</key>
    <string>0.0.0</string>
    <key>CFBundleVersion</key>
    <string>$version</string>
</dict>
</plist>
PLIST
"""

# `pgrep.out` names the pids that are running, `pgrep.calls` how many more times they are reported
# (which is how a process that does quit is spelled) and `pgrep.status` an error instead of an
# answer. Like the real thing, it exits 1 when it matched nothing.
FAKE_PGREP = """#!/bin/bash
state="$LYREBIRD_TEST_STATE"
if [[ -f "$state/pgrep.status" ]]; then exit "$(cat "$state/pgrep.status")"; fi
answers=1
if [[ -f "$state/pgrep.calls" ]]; then
  answers="$(cat "$state/pgrep.calls")"
  echo "$((answers - 1))" > "$state/pgrep.calls"
fi
if [[ -f "$state/pgrep.out" && "$answers" -gt 0 ]]; then
  cat "$state/pgrep.out"
  exit 0
fi
exit 1
"""

FAKE_PS = """#!/bin/bash
state="$LYREBIRD_TEST_STATE"
pid=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -p) pid="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [[ -f "$state/ps/$pid" ]]; then cat "$state/ps/$pid"; exit 0; fi
exit 1
"""

# `open-launches` holds the pid the launched app is to be found under, because the script checks
# that the pid is alive before believing in it.
FAKE_OPEN = """#!/bin/bash
state="$LYREBIRD_TEST_STATE"
echo "$1" >> "$state/open.log"
if [[ -f "$state/open-launches" ]]; then
  pid="$(cat "$state/open-launches")"
  executable="$1/Contents/MacOS/Lyrebird"
  # The launched process is reported under the path in `open-reports` when there is one: a real
  # process is not reported under the spelling that was passed to `open`.
  if [[ -f "$state/open-reports" ]]; then executable="$(cat "$state/open-reports")"; fi
  mkdir -p "$state/ps"
  echo "$executable" > "$state/ps/$pid"
  rm -f "$state/pgrep.calls"
  echo "$pid" > "$state/pgrep.out"
fi
"""

# Real `mv`, except for the one move these checks are about: the staged bundle going into place.
# `mv-fails` refuses it; `mv-races` lets another install's bundle arrive at the destination first,
# in the window between the backup and the swap, and then does the move for real — which is how a
# `mv` that succeeds ends up putting our app inside somebody else's bundle.
FAKE_MV = """#!/bin/bash
state="$LYREBIRD_TEST_STATE"
staged_move=""
target=""
for arg in "$@"; do
  target="$arg"
  case "$arg" in
    *.Lyrebird.app.installing-*) staged_move="yes" ;;
  esac
done
if [[ -n "$staged_move" && -f "$state/mv-fails" ]]; then
  echo "fake mv: refusing" >&2
  exit 1
fi
if [[ -n "$staged_move" && -f "$state/mv-races" ]]; then
  mkdir -p "$target/Contents/MacOS"
  echo 'another install' > "$target/Contents/MacOS/Lyrebird"
  chmod 755 "$target/Contents/MacOS/Lyrebird"
  cat > "$target/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>CFBundleShortVersionString</key>
    <string>0.0.0</string>
    <key>CFBundleVersion</key>
    <string>$(cat "$state/mv-races")</string>
</dict>
</plist>
PLIST
fi
exec /bin/mv "$@"
"""

# Real `stat`, except for the one path named in `stat-fails-for`: a file that is there and cannot
# be stat'ed anyway, which is not something a readable path can be talked into.
FAKE_STAT = """#!/bin/bash
state="$LYREBIRD_TEST_STATE"
if [[ -f "$state/stat-fails-for" ]]; then
  refuse="$(cat "$state/stat-fails-for")"
  for arg in "$@"; do
    if [[ "$arg" == "$refuse" ]]; then echo "fake stat: refusing $arg" >&2; exit 1; fi
  done
fi
exec /usr/bin/stat "$@"
"""

# Real `ditto`, with the option of a copy that arrived missing a piece.
FAKE_DITTO = """#!/bin/bash
state="$LYREBIRD_TEST_STATE"
/usr/bin/ditto "$@"
if [[ -f "$state/ditto-drops-plist" ]]; then rm -f "$2/Contents/Info.plist"; fi
if [[ -f "$state/ditto-drops-executable" ]]; then rm -f "$2/Contents/MacOS/Lyrebird"; fi
"""


def write_executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def stubborn(tmp_path):
    """A real process standing in for the running copy the script is told to quit.

    It has to be real because the script settles existence with `kill -0`, and it survives the
    SIGTERM it is sent because these checks own it: what "quit" means here is the fake pgrep no
    longer reporting it. `.sigterm` is the file it writes when it is signalled, which is how a
    check sees that the copy was asked to go.
    """
    signalled = tmp_path / "sigterm-received"
    process = subprocess.Popen([sys.executable, "-c", STUBBORN_PROCESS, str(signalled)])
    yield SimpleNamespace(pid=str(process.pid), sigterm=signalled)
    process.kill()
    process.wait()


def eventually(path, seconds=5):
    """Wait for a file another process writes: the signal is delivered, not handed over."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def case_alias(path):
    """The same directory spelled in another case, or None where the filesystem tells them apart."""
    alias = path.parent / (path.name.upper() if path.name != path.name.upper() else path.name.lower())
    if alias == path or not alias.exists():
        return None
    return alias if os.path.samefile(alias, path) else None


def launched_by(tmp_path):
    """Tell the fake `open` to make the launch appear, under a pid that is alive.

    The test process is that pid: the script only sends SIGTERM to copies it found *before* the
    swap, so nothing signals this one — it is only ever looked up.
    """
    (tmp_path / "state/open-launches").write_text(str(os.getpid()))


def checkout(tmp_path):
    """A checkout the script can run in: one commit (so the build number is 1) and fake tools."""
    root = tmp_path / "checkout"
    (root / "menubar/scripts").mkdir(parents=True)
    for name in ("install-app.sh", "verify-version.sh"):
        shutil.copyfile(REPO / "menubar/scripts" / name, root / "menubar/scripts" / name)
        (root / "menubar/scripts" / name).chmod(0o755)
    (root / "menubar/project.yml").write_text("name: Lyrebird\n")
    write_executable(root / ".build/tools/bin/xcodegen", "#!/bin/bash\nexit 0\n")

    fakes = tmp_path / "fake-bin"
    write_executable(fakes / "xcodebuild", FAKE_XCODEBUILD)
    write_executable(fakes / "pgrep", FAKE_PGREP)
    write_executable(fakes / "ps", FAKE_PS)
    write_executable(fakes / "open", FAKE_OPEN)
    (tmp_path / "state/ps").mkdir(parents=True)

    git = [
        "git",
        "-C",
        str(root),
        "-c",
        "user.email=dev@example.invalid",
        "-c",
        "user.name=Fixture",
        "-c",
        "commit.gpgsign=false",
        "-c",
        f"core.hooksPath={tmp_path}/no-hooks",
    ]
    subprocess.run(["git", "init", "-q", "--template=", str(root)], check=True, capture_output=True)
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True, capture_output=True)
    return root


def old_bundle(dest):
    """An install already in place, with a file the checks can look for after a failure.

    It carries a real executable because that is the file a running copy is now recognised by.
    """
    bundle = dest / "Lyrebird.app"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/marker.txt").write_text("previous install")
    write_executable(bundle / "Contents/MacOS/Lyrebird", "#!/bin/bash\nsleep 120\n")
    return bundle


def install(root, tmp_path, dest):
    state = tmp_path / "state"
    environment = {
        "PATH": f"{tmp_path / 'fake-bin'}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LYREBIRD_TEST_STATE": str(state),
        # Otherwise a launch that is never coming costs ten seconds per check.
        "LYREBIRD_INSTALL_LAUNCH_WAIT": "1",
    }
    return subprocess.run(
        ["bash", "menubar/scripts/install-app.sh", str(dest)],
        cwd=str(root),
        env=environment,
        capture_output=True,
        text=True,
    )


def build_version(bundle):
    return subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", "Print CFBundleVersion", str(bundle / "Contents/Info.plist")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def leftovers(dest):
    return sorted(p.name for p in Path(dest).iterdir() if p.name.startswith(".Lyrebird.app."))


def test_install_app_installs_the_built_bundle_and_launches_it(tmp_path):
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    launched_by(tmp_path)

    result = install(root, tmp_path, dest)

    assert result.returncode == 0, result.stderr
    assert build_version(dest / "Lyrebird.app") == "1"
    assert (tmp_path / "state/open.log").read_text().strip() == str(dest / "Lyrebird.app")
    # Staging, backup and the lock: a run that finished leaves the next one nothing to work around.
    assert leftovers(dest) == []


def test_install_app_refuses_to_start_while_another_install_holds_the_lock(tmp_path):
    """Two runs against one directory interleave into a bundle inside a bundle: the second moves
    its app in between the first one's backup and swap, and the first `mv` then lands inside what
    arrived and exits 0, reporting a build that is not the one that launches. `mkdir` is atomic, so
    the second run refuses; it refuses before building, because a build it will not install is
    minutes of work for nothing."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    (dest / ".Lyrebird.app.install-lock").mkdir()

    result = install(root, tmp_path, dest)

    assert result.returncode == 1
    assert "another install-app is running against" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert not (root / "menubar/.build").exists()
    assert leftovers(dest) == [".Lyrebird.app.install-lock"]


def test_install_app_rejects_a_bundle_that_appeared_during_the_swap(tmp_path):
    """The lock keeps two of these runs apart, and this check covers what the lock cannot: it is
    one process, so nothing is racing — the fake `mv` plants another install's bundle at the
    destination and then performs the move for real, exactly as an interleaved run would have left
    it. `mv` moves our app *inside* the bundle that arrived and exits 0, so the staged copy still
    verifies while a different app is what would launch.

    The competitor carries build 1, the same number as ours, because a commit count is not an
    identity: verifying the version at the destination passes here, and only the inode says that
    the bundle now at $dest is not the one this run built. Nothing was installed, so the previous
    bundle stays in its backup and the launch never happens.
    """
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    old_bundle(dest)
    write_executable(tmp_path / "fake-bin/mv", FAKE_MV)
    (tmp_path / "state/mv-races").write_text("1")
    launched_by(tmp_path)

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "a different bundle is at" in result.stderr
    assert "the previous bundle is kept at" in result.stderr
    assert not (tmp_path / "state/open.log").exists()
    backups = [name for name in leftovers(dest) if name.startswith(".Lyrebird.app.previous-")]
    assert len(backups) == 1
    assert (dest / backups[0] / "Lyrebird.app/Contents/marker.txt").read_text() == "previous install"


def test_install_app_stops_when_the_installed_bundle_has_no_executable(tmp_path):
    """The sanity checks behind the inode: a copy that landed where it should but arrived without
    the binary is not something to launch and report as installed. The previous bundle is kept,
    because what is at the destination is not an install."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    old_bundle(dest)
    write_executable(tmp_path / "fake-bin/ditto", FAKE_DITTO)
    (tmp_path / "state/ditto-drops-executable").write_text("")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "no executable at Contents/MacOS/Lyrebird" in result.stderr
    assert "the previous bundle is kept at" in result.stderr
    assert not (tmp_path / "state/open.log").exists()
    backups = [name for name in leftovers(dest) if name.startswith(".Lyrebird.app.previous-")]
    assert len(backups) == 1
    assert (dest / backups[0] / "Lyrebird.app/Contents/marker.txt").read_text() == "previous install"


def test_install_app_stops_when_the_copy_arrives_without_its_plist(tmp_path):
    """A copy that stopped short is not an install, and the version read-back is where that shows.
    PlistBuddy answers a missing file with "File Doesn't Exist, Will Create" and exit 1, which
    under `set -e` ended the run with nothing said; the plist is checked for before it is read."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    write_executable(tmp_path / "fake-bin/ditto", FAKE_DITTO)
    (tmp_path / "state/ditto-drops-plist").write_text("")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "has no Contents/Info.plist" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_install_app_stops_when_pgrep_cannot_answer(tmp_path):
    """`pgrep` exits 1 for "no match" and 2 or more for an error, and `set -e` does not reach into
    the `$(...)` of a `for` header: a failed query used to produce the empty list, which reads as
    "nothing is running". The old copy then survived the swap and the launch check at the end was
    satisfied by that same old process, so a failed install reported success."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    (tmp_path / "state/pgrep.status").write_text("3")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "pgrep" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_install_app_stops_when_ps_cannot_read_a_live_process(tmp_path):
    """The same hole one call further in, and `ps` exiting 1 is not enough to close it: `ps` fails
    that way both for a pid that is gone and for a `ps` that could not run, so a transient failure
    read as "gone" left the old copy running through the swap, and a query that worked later
    satisfied the launch check. The pid here is this very test process — alive beyond doubt, and
    never signalled, because the script only quits copies whose path matches the bundle — and the
    fake `ps` has no answer for it, which is the failure spelled the way the real one spells it."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    (tmp_path / "state/pgrep.out").write_text(f"{os.getpid()}\n")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "ps -o comm=" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_install_app_tolerates_a_pid_that_ended_between_pgrep_and_ps(tmp_path):
    """The benign half of the case above: a process that really did end is an answer, not a
    failure, and an install must not stop for one. The pid is above anything macOS hands out, so
    `kill -0` settles it as gone and the `ps` that has nothing to say about it is never asked."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    (tmp_path / "state/pgrep.out").write_text(f"{GONE_PID}\n")
    launched_by(tmp_path)

    result = install(root, tmp_path, dest)

    assert result.returncode == 0, result.stderr
    assert build_version(dest / "Lyrebird.app") == "1"


def test_install_app_refuses_to_replace_a_bundle_whose_process_will_not_quit(tmp_path, stubborn):
    """`ditto` over a bundle a live app is still reading is not an install, and the old code keeps
    running: two Lyrebirds in the menu bar, one of them the copy that was just replaced."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    (tmp_path / "state/pgrep.out").write_text(f"{stubborn.pid}\n")
    (tmp_path / f"state/ps/{stubborn.pid}").write_text(f"{bundle}/Contents/MacOS/Lyrebird\n")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "did not quit" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_install_app_finds_the_running_copy_through_a_symlinked_install_directory(tmp_path, stubborn):
    """`ps -o comm=` reports the real executable path, so a `dest` resolved with a `pwd` that keeps
    symlinks matched nothing: the running copy was filed as somebody else's Lyrebird, left alone,
    and the bundle was replaced under it. Here the copy does quit, and the proof that it was
    recognised as ours is that it was never reported as another Lyrebird."""
    root = checkout(tmp_path)
    real = tmp_path / "real-Applications"
    real.mkdir()
    alias = tmp_path / "aliased-Applications"
    alias.symlink_to(real)
    bundle = old_bundle(real)
    (tmp_path / "state/pgrep.out").write_text(f"{stubborn.pid}\n")
    (tmp_path / "state/pgrep.calls").write_text("2")  # the copy quits after being asked twice
    (tmp_path / f"state/ps/{stubborn.pid}").write_text(f"{bundle}/Contents/MacOS/Lyrebird\n")
    launched_by(tmp_path)

    result = install(root, tmp_path, alias)

    assert result.returncode == 0, result.stderr
    assert "left alone" not in result.stderr
    assert build_version(real / "Lyrebird.app") == "1"


def test_install_app_quits_the_running_copy_when_the_install_directory_is_spelled_in_another_case(tmp_path, stubborn):
    """macOS filesystems are case-insensitive and `pwd -P` hands back the case the caller typed, so
    `APP_INSTALL_DIR=/applications` and a process reported under `/Applications` never matched as
    strings: the running copy was filed as somebody else's, left running, and its bundle replaced
    under it. Paths are compared as files now, so the same directory spelled two ways is one
    directory. The copy is asked to quit — it writes down the SIGTERM — and the install completes.

    Both ends are checked: the copy running before the swap and the one that appears after `open`
    are reported under the original spelling while the script only ever holds the aliased one, so a
    string comparison at either end fails the run.
    """
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    aliased = case_alias(dest)
    if aliased is None:
        pytest.skip("tmp_path is on a case-sensitive filesystem, where there is no case alias")
    bundle = old_bundle(dest)
    (tmp_path / "state/pgrep.out").write_text(f"{stubborn.pid}\n")
    (tmp_path / "state/pgrep.calls").write_text("2")  # the copy quits after being asked twice
    (tmp_path / f"state/ps/{stubborn.pid}").write_text(f"{bundle}/Contents/MacOS/Lyrebird\n")
    launched_by(tmp_path)
    (tmp_path / "state/open-reports").write_text(f"{bundle}/Contents/MacOS/Lyrebird")

    result = install(root, tmp_path, aliased)

    assert result.returncode == 0, result.stderr
    assert eventually(stubborn.sigterm), "the running copy was never asked to quit"
    assert "left alone" not in result.stderr
    assert build_version(dest / "Lyrebird.app") == "1"


def test_install_app_stops_when_the_installed_executable_cannot_be_stated(tmp_path, stubborn):
    """An installed executable that is there but cannot be stat'ed is not "nothing is installed":
    reading it that way filed the running installed copy as another Lyrebird, left it running while
    its bundle was replaced under it, and let the post-launch check pass on that same old process.
    A readable file cannot be talked out of `stat`, so `stat` itself is faked for this one path; it
    is called by name for that reason. The running copy is reported by another spelling of the same
    file, which does stat, so nothing but the installed executable's own identity is missing — that
    is the shape in which the old reading let the copy survive. Nothing is signalled, because the
    run stops before the question of which copy to quit is asked."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    write_executable(tmp_path / "fake-bin/stat", FAKE_STAT)
    (tmp_path / "state/stat-fails-for").write_text(f"{bundle}/Contents/MacOS/Lyrebird")
    (tmp_path / "state/pgrep.out").write_text(f"{stubborn.pid}\n")
    (tmp_path / f"state/ps/{stubborn.pid}").write_text(f"{bundle}/Contents/./MacOS/Lyrebird\n")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert f"cannot stat {bundle}/Contents/MacOS/Lyrebird" in result.stderr
    assert "cannot tell which running Lyrebird is the installed one" in result.stderr
    assert not stubborn.sigterm.exists()
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_install_app_refuses_the_build_products_directory_spelled_in_another_case(tmp_path):
    """The same string comparison guarded the refusal: `.../Release` spelled `.../RELEASE` is the
    same directory, and installing into it means deleting the build to make room for a copy of
    itself."""
    root = checkout(tmp_path)
    products = root / "menubar/.build/Build/Products/Release"
    products.mkdir(parents=True)
    aliased = case_alias(products)
    if aliased is None:
        pytest.skip("tmp_path is on a case-sensitive filesystem, where there is no case alias")

    result = install(root, tmp_path, aliased)

    assert result.returncode == 2
    assert "refusing to replace the build with itself" in result.stderr
    assert (products / "Lyrebird.app/Contents/Info.plist").exists()


def test_install_app_stops_when_a_running_copys_executable_cannot_be_stated(tmp_path):
    """A live process whose executable cannot be stat'ed is the case where "not ours" must not be
    assumed: a Lyrebird whose bundle was deleted out from under it reports a path that no longer
    resolves, and filing it as somebody else's is how the copy in $dest gets replaced while it is
    still running. The pid is this test process, which is alive and is never signalled."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    (tmp_path / "state/pgrep.out").write_text(f"{os.getpid()}\n")
    (tmp_path / f"state/ps/{os.getpid()}").write_text(f"{tmp_path}/deleted/Lyrebird.app/Contents/MacOS/Lyrebird\n")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "cannot stat the executable" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_install_app_leaves_the_old_bundle_when_the_build_reports_the_wrong_version(tmp_path):
    """xcodebuild accepts build settings it does not use, so a bundle can come out stamped with a
    version nobody asked for. Installing it anyway puts a build in /Applications whose About box
    names a commit it was not built from."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    (tmp_path / "state/build-version").write_text("7")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "CFBundleVersion expected '1', got '7'" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_install_app_refuses_a_dest_that_is_a_symlink_to_the_build_products_directory(tmp_path):
    """Installing into the build products directory means deleting the build to make room for the
    copy of itself; the string comparison passed anything that spelled that directory differently."""
    root = checkout(tmp_path)
    products = root / "menubar/.build/Build/Products/Release"
    products.mkdir(parents=True)
    alias = tmp_path / "aliased-products"
    alias.symlink_to(products)

    result = install(root, tmp_path, alias)

    assert result.returncode == 2
    assert "refusing to replace the build with itself" in result.stderr
    assert (products / "Lyrebird.app/Contents/Info.plist").exists()


def test_install_app_refuses_an_installed_bundle_that_is_a_symlink(tmp_path):
    """A canonical `dest` is not enough when the bundle inside it is itself a link: the running
    app's executable path is the link's target, so nothing matches, nothing is quit, and the link
    is replaced by a directory while the app it pointed at keeps running. There is no sensible
    install into a link, so the script says so rather than picking one meaning."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = old_bundle(elsewhere)
    (dest / "Lyrebird.app").symlink_to(target)

    result = install(root, tmp_path, dest)

    assert result.returncode == 2
    assert "is a symlink" in result.stderr
    assert (dest / "Lyrebird.app").is_symlink()
    assert (target / "Contents/marker.txt").read_text() == "previous install"


@pytest.mark.parametrize("kind", ["previous", "installing"])
def test_install_app_leaves_an_earlier_runs_directory_alone(tmp_path, kind):
    """Staging and backup used to be named after the pid, and a reused pid deleted the only copy an
    interrupted run had left. The directory is planted by the fake `xcodebuild`, whose parent is
    the running script: naming it after any other pid would pin nothing. There is an installed copy
    too, so this run allocates its own backup beside the one already sitting there."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    old_bundle(dest)
    (tmp_path / f"state/seed-{kind}").write_text(str(dest))
    launched_by(tmp_path)

    result = install(root, tmp_path, dest)

    assert result.returncode == 0, result.stderr
    assert build_version(dest / "Lyrebird.app") == "1"
    stranded = Path((tmp_path / f"state/seeded-{kind}").read_text().strip())
    assert (stranded / "Lyrebird.app/Contents/marker.txt").read_text().strip() == "rescued from an interrupted run"
    assert leftovers(dest) == [stranded.name]


def test_install_app_fails_when_the_installed_app_never_starts(tmp_path):
    """`open` returns 0 once the launch request is submitted, which says nothing about the app
    staying up: a bundle that crashes on launch used to install with a tick and no running app."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "was installed but no process is running from it" in result.stderr
    assert build_version(dest / "Lyrebird.app") == "1"


def test_install_app_resolves_a_relative_install_directory_against_the_invocation_directory(tmp_path):
    """The script `cd`s into menubar/ to build, so a relative APP_INSTALL_DIR resolved after that
    would replace a Lyrebird.app under menubar/ and exit 0 with the intended install untouched."""
    root = checkout(tmp_path)
    (root / "downstairs").mkdir()
    launched_by(tmp_path)

    result = install(root, tmp_path, "downstairs")

    assert result.returncode == 0, result.stderr
    assert build_version(root / "downstairs/Lyrebird.app") == "1"
    assert not (root / "menubar/downstairs").exists()


def test_install_app_puts_the_previous_bundle_back_when_the_swap_fails(tmp_path):
    """`rm -rf old && mv staged old` leaves nothing installed when that `mv` fails. The failure is
    induced with a fake `mv` because nothing on disk can talk a real one out of a rename within a
    directory; everything else in the swap — ditto, PlistBuddy, rm — is the real thing."""
    root = checkout(tmp_path)
    dest = tmp_path / "Applications"
    dest.mkdir()
    bundle = old_bundle(dest)
    write_executable(tmp_path / "fake-bin/mv", FAKE_MV)
    (tmp_path / "state/mv-fails").write_text("")

    result = install(root, tmp_path, dest)

    assert result.returncode != 0
    assert "was put back" in result.stderr
    assert (bundle / "Contents/marker.txt").read_text() == "previous install"
    assert leftovers(dest) == []


def test_check_shell_parses_every_listed_script_not_only_the_first(tmp_path):
    """`bash -n a b c` parses `a` and hands `b c` to it as positional arguments, so one invocation
    over the whole list reported success for scripts it never opened — every script but the first
    had gone unchecked. The target is driven for real with a two-file list whose second file does
    not parse, which the single-invocation recipe passed."""
    fine = tmp_path / "fine.sh"
    fine.write_text("#!/bin/bash\necho ok\n")
    broken = tmp_path / "broken.sh"
    broken.write_text("#!/bin/bash\nif true; then\n")

    # The premise: bash really does ignore the second file.
    assert subprocess.run(["bash", "-n", str(fine), str(broken)], capture_output=True).returncode == 0

    result = subprocess.run(
        ["make", "-C", str(REPO), "check-shell", f"SHELL_SCRIPTS={fine} {broken}"],
        capture_output=True,
        text=True,
        env={**os.environ, "MAKEFLAGS": ""},
    )

    assert result.returncode != 0
    assert "broken.sh" in result.stdout + result.stderr
