"""The pre-push private-details check: what it reads, which commits it looks at, what it recognises.

A file in any text encoding is scanned and never skipped, a push is judged by the
commits it would add (their files and their messages, even for a detail that a
later commit removed), and the patterns cover home paths on any drive, shared
folder paths and common secret formats.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_public", REPO / "scripts" / "check_public.py")
check_public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_public)

# Built from pieces so this file does not trip the check it tests.
HOME_PATH = "C:" + "\\" + "Users" + "\\" + "someone" + "\\" + "project"
ZERO = "0" * 40


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=" + "test" + "@" + "example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, check=True,
    )
    return done.stdout.decode("utf-8").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q")
    return folder


def commit(repo: Path, files: dict[str, bytes | None], message: str = "change") -> str:
    for name, content in files.items():
        path = repo / name
        if content is None:
            git(repo, "rm", "-q", name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        git(repo, "add", name)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def push(repo: Path, local: str, remote: str = ZERO, remote_name: str = "origin"):
    line = f"refs/heads/main {local} refs/heads/main {remote}\n"
    return check_public.main(["--pre-push", remote_name, "url"], root=repo, stdin=io.StringIO(line))


def test_a_file_in_any_text_encoding_is_scanned_and_never_skipped(repo, capsys):
    text = f"see {HOME_PATH} for details\n"
    commit(
        repo,
        {
            "utf16.txt": text.encode("utf-16"),
            "utf16_no_bom.txt": text.encode("utf-16-le"),
            "latin.txt": b"caf\xe9 " + text.encode("ascii"),
            "clean.txt": b"nothing here\n",
        },
    )
    assert check_public.main([], root=repo) == 1
    err = capsys.readouterr().err
    for name in ("utf16.txt", "utf16_no_bom.txt", "latin.txt"):
        assert name in err, name
    assert "clean.txt" not in err


def test_a_commit_message_with_a_private_path_stops_the_push(repo, capsys):
    first = commit(repo, {"a.txt": b"fine\n"}, "first")
    second = commit(repo, {"b.txt": b"fine\n"}, f"fix the build on {HOME_PATH}")
    assert push(repo, second, first) == 1
    err = capsys.readouterr().err
    assert "message" in err and second[:7] in err
    assert HOME_PATH not in err, "the report must not print the private text"


def test_a_clean_push_and_a_deletion_pass(repo):
    first = commit(repo, {"a.txt": b"fine\n"}, "first")
    second = commit(repo, {"b.txt": b"fine\n"}, "second")
    assert push(repo, second, first) == 0
    assert push(repo, ZERO, second) == 0
    assert check_public.main(["--pre-push", "origin", "url"], root=repo, stdin=io.StringIO("")) == 0


def test_a_detail_that_a_later_commit_removed_is_still_found_in_the_pushed_range(repo, capsys):
    base = commit(repo, {"a.txt": b"fine\n"}, "base")
    commit(repo, {"notes.txt": f"path {HOME_PATH}\n".encode()}, "add notes")
    tip = commit(repo, {"notes.txt": None}, "remove notes")
    assert push(repo, tip, base) == 1
    assert "notes.txt" in capsys.readouterr().err
    assert check_public.main([], root=repo) == 0, "the working tree alone looks clean"


def test_a_new_branch_is_judged_by_what_the_remote_does_not_already_have(repo):
    old = commit(repo, {"notes.txt": f"path {HOME_PATH}\n".encode()}, "already published")
    tip = commit(repo, {"b.txt": b"fine\n"}, "new work")
    assert push(repo, tip) == 1, "with nothing known about the remote, all history counts"
    git(repo, "update-ref", "refs/remotes/origin/main", old)
    assert push(repo, tip) == 0
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")


def private_samples() -> dict[str, str]:
    users = "Users"
    return {
        "other drive": "D:" + "\\" + users + "\\someone",
        "json escaped": "E:" + "\\\\" + users + "\\\\someone",
        "encoded": "C%3A%5C" + users + "%5Csomeone",
        "userprofile": "%" + "USERPROFILE" + "%" + "\\x",
        "shared folder": "\\\\" + "fileserver" + "\\" + "share",
        "aws key": "AKIA" + "ABCDEFGHIJKLMNOP",
        "github token": "github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "slack token": "xoxb" + "-1234567890-abcdefghij",
    }


def test_a_no_reply_address_passes_and_a_real_one_does_not(repo, capsys):
    at = "@"
    base = commit(repo, {"a.txt": b"fine\n"}, "base")
    trailer = commit(repo, {"b.txt": b"fine\n"}, f"work\n\nCo-Authored-By: Bot <noreply{at}example.com>\n"
                     f"Signed-off-by: Someone <12345+someone{at}users.noreply.github.com>")
    assert push(repo, trailer, base) == 0
    real = commit(repo, {"c.txt": b"fine\n"}, f"ask jane.doe{at}example.com, or reply{at}example.com")
    assert push(repo, real, trailer) == 1
    assert real[:7] in capsys.readouterr().err


def test_every_kind_of_private_detail_is_recognised(repo, capsys):
    samples = private_samples()
    commit(repo, {f"{index}.txt": f"value {text}\n".encode() for index, text in enumerate(samples.values())})
    assert check_public.main([], root=repo) == 1
    err = capsys.readouterr().err
    missed = [name for index, name in enumerate(samples) if f"{index}.txt" not in err]
    assert missed == []


def test_the_hook_passes_the_pushed_refs_on_to_the_check(repo, tmp_path):
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no POSIX shell to run the hook")
    (repo / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "check_public.py", repo / "scripts" / "check_public.py")
    shutil.copy(REPO / ".githooks" / "pre-push", repo / "pre-push")
    first = commit(repo, {"a.txt": b"fine\n"}, "first")
    second = commit(repo, {"b.txt": b"fine\n"}, f"oops {HOME_PATH}")
    done = subprocess.run(
        [shell, "pre-push", "origin", "url"],
        cwd=repo,
        input=f"refs/heads/main {second} refs/heads/main {first}\n".encode(),
        capture_output=True,
    )
    assert done.returncode == 1, done.stderr.decode("utf-8", errors="replace")
    assert b"message" in done.stderr


def test_local_contestants_files_are_ignored_by_git_and_the_example_is_not():
    def ignored(name: str) -> bool:
        done = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO, capture_output=True)
        return done.returncode == 0

    assert ignored("runner/contestants.json")
    assert ignored("runner/contestants.local.json")
    assert ignored("runner/contestants.mine.json")
    assert not ignored("runner/contestants.example.json")
