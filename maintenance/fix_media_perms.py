#!/usr/bin/env python3

import argparse
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from grp import getgrnam
from pwd import getpwnam
from shutil import which
from typing import Iterable, List, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()


BASE = os.getenv("RC_MEDIA_BASE", "/storage/media")
DEFAULT_TARGETS = [
    f"{BASE}/documentaries",
    f"{BASE}/movies",
    f"{BASE}/music",
    f"{BASE}/tvshows",
]
DEFAULT_EXCLUDE_NAMES = {"working", "usenet", "servarr"}

OWNER = os.getenv("RC_MEDIA_OWNER", "david")
GROUP = os.getenv("RC_MEDIA_GROUP", "media")
DIR_MODE = 0o2775
FILE_MODE = 0o0664


@dataclass
class Counters:
    roots: int = 0
    acl_strip_planned: int = 0
    acl_strip_executed: int = 0
    scanned_dirs: int = 0
    scanned_files: int = 0
    skipped_symlinks: int = 0
    changed_owner: int = 0
    changed_mode: int = 0
    skipped_missing: int = 0
    errors: int = 0


def _is_symlink(path: str) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _safe_lstat(path: str):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except PermissionError:
        return "PERM"


def _strip_acls(root: str, apply: bool) -> Optional[str]:
    """
    Remove all ACL entries and default ACLs.
      -b: remove extended ACL entries
      -k: remove default ACLs
    """
    if which("setfacl") is None:
        # In dry-run we can still show the intended behavior without failing immediately.
        # In apply mode, ACL stripping is a hard requirement (per project policy).
        return "setfacl not found (install package 'acl')"

    cmd = ["setfacl", "-R", "-b", "-k", root]
    if not apply:
        print("DRY-RUN:", " ".join(cmd))
        return None

    try:
        subprocess.run(cmd, check=True)
        return None
    except subprocess.CalledProcessError as e:
        return f"setfacl failed: {e}"


def _ensure_owner_group(
    path: str,
    st,
    uid: int,
    gid: int,
    apply: bool,
) -> Tuple[bool, Optional[str]]:
    if st == "PERM":
        return False, "permission denied (stat)"
    if st is None:
        return False, "missing"
    if st.st_uid == uid and st.st_gid == gid:
        return False, None

    if not apply:
        print(f"DRY-RUN: chown {uid}:{gid} {path}")
        return True, None

    try:
        os.chown(path, uid, gid)
        return True, None
    except PermissionError as e:
        return False, f"permission denied (chown): {e}"
    except OSError as e:
        return False, f"chown failed: {e}"


def _ensure_mode(
    path: str,
    st,
    desired_mode: int,
    apply: bool,
) -> Tuple[bool, Optional[str]]:
    if st == "PERM":
        return False, "permission denied (stat)"
    if st is None:
        return False, "missing"

    current = stat.S_IMODE(st.st_mode)
    # Keep execute bits if a file is already executable (avoid surprising removals)
    if stat.S_ISREG(st.st_mode):
        desired_mode = desired_mode | (current & 0o111)
    # Keep sticky bit on directories if present
    if stat.S_ISDIR(st.st_mode):
        desired_mode = desired_mode | (current & 0o1000)

    if current == desired_mode:
        return False, None

    if not apply:
        print(f"DRY-RUN: chmod {oct(desired_mode)} {path}  # was {oct(current)}")
        return True, None

    try:
        os.chmod(path, desired_mode)
        return True, None
    except PermissionError as e:
        return False, f"permission denied (chmod): {e}"
    except OSError as e:
        return False, f"chmod failed: {e}"


def _expand_roots(paths: List[str]) -> List[str]:
    if not paths:
        return DEFAULT_TARGETS[:]

    expanded: List[str] = []
    for p in paths:
        p = os.path.abspath(p)
        if p in ("/", ""):
            raise SystemExit("Refusing to run on '/'. Provide specific media paths.")
        # This script chowns/chmods recursively as root — restrict it to the
        # media tree so a stray argument (e.g. /etc) can't be clobbered.
        if p != BASE and not p.startswith(BASE.rstrip("/") + os.sep):
            raise SystemExit(f"Refusing to run outside {BASE} (RC_MEDIA_BASE): {p}")

        if p == BASE:
            try:
                for name in os.listdir(BASE):
                    if name in DEFAULT_EXCLUDE_NAMES:
                        continue
                    child = os.path.join(BASE, name)
                    if os.path.isdir(child):
                        expanded.append(child)
            except FileNotFoundError:
                expanded.append(p)
        else:
            expanded.append(p)
    return expanded


def _walk_and_fix(root: str, uid: int, gid: int, apply: bool, counters: Counters) -> None:
    if not os.path.exists(root):
        counters.skipped_missing += 1
        print(f"[SKIP] Missing: {root}")
        return
    if not os.path.isdir(root):
        counters.skipped_missing += 1
        print(f"[SKIP] Not a directory: {root}")
        return

    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        # Only exclude by name if we're walking under BASE (e.g. user passed /storage/media)
        if os.path.abspath(current_root) == BASE:
            dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_NAMES]

        # Directory itself
        if _is_symlink(current_root):
            counters.skipped_symlinks += 1
        else:
            st = _safe_lstat(current_root)
            counters.scanned_dirs += 1

            changed, err = _ensure_owner_group(current_root, st, uid, gid, apply)
            if changed:
                counters.changed_owner += 1
            if err:
                counters.errors += 1
                print(f"[ERR] {current_root}: {err}")

            changed, err = _ensure_mode(current_root, st, DIR_MODE, apply)
            if changed:
                counters.changed_mode += 1
            if err:
                counters.errors += 1
                print(f"[ERR] {current_root}: {err}")

        # Files
        for name in filenames:
            p = os.path.join(current_root, name)
            if _is_symlink(p):
                counters.skipped_symlinks += 1
                continue

            st = _safe_lstat(p)
            counters.scanned_files += 1

            if st not in (None, "PERM") and not stat.S_ISREG(st.st_mode):
                continue

            changed, err = _ensure_owner_group(p, st, uid, gid, apply)
            if changed:
                counters.changed_owner += 1
            if err:
                counters.errors += 1
                print(f"[ERR] {p}: {err}")

            changed, err = _ensure_mode(p, st, FILE_MODE, apply)
            if changed:
                counters.changed_mode += 1
            if err:
                counters.errors += 1
                print(f"[ERR] {p}: {err}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Strip ACLs and enforce ownership/perms for Jellyfin media.\n\n"
            f"Defaults: owner/group={OWNER}:{GROUP}, dir={oct(DIR_MODE)}, file={oct(FILE_MODE)}"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help=(
            "One or more root paths.\n"
            f"If omitted, defaults to {BASE}/{{documentaries,movies,music,tvshows}}.\n"
            f"If you pass {BASE}, it will expand to children and skip: working, usenet, servarr."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run).")
    args = parser.parse_args()

    # If applying changes, automatically re-run under sudo/root.
    if args.apply and os.geteuid() != 0:
        if which("sudo") is None:
            raise SystemExit("This operation requires root, and 'sudo' was not found.")
        # sudo resets the environment, so carry the resolved settings across explicitly.
        env = [f"RC_MEDIA_BASE={BASE}", f"RC_MEDIA_OWNER={OWNER}", f"RC_MEDIA_GROUP={GROUP}"]
        os.execvp("sudo", ["sudo", "env", *env, sys.executable] + sys.argv)

    uid = getpwnam(OWNER).pw_uid
    gid = getgrnam(GROUP).gr_gid
    roots = _expand_roots(args.paths)

    counters = Counters(roots=len(roots))
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Mode: {mode}")
    print(f"Owner/group: {OWNER}:{GROUP}")
    print(f"Dir mode: {oct(DIR_MODE)}   File mode: {oct(FILE_MODE)}")
    print("-" * 80)

    for root in roots:
        print(f"\n== Root: {root}")
        err = _strip_acls(root, apply=args.apply)
        if err:
            counters.errors += 1
            print(f"[ERR] {root}: {err}")
            if "setfacl not found" in err and args.apply:
                raise SystemExit(err)
        else:
            if args.apply:
                counters.acl_strip_executed += 1
            else:
                counters.acl_strip_planned += 1

        _walk_and_fix(root, uid, gid, args.apply, counters)

    print("\n" + "-" * 80)
    print("Summary:")
    print(f"  roots             : {counters.roots}")
    if args.apply:
        print(f"  acl strip executed: {counters.acl_strip_executed}")
    else:
        print(f"  acl strip planned : {counters.acl_strip_planned}")
    print(f"  scanned dirs      : {counters.scanned_dirs}")
    print(f"  scanned files     : {counters.scanned_files}")
    print(f"  changed owner     : {counters.changed_owner}")
    print(f"  changed mode      : {counters.changed_mode}")
    print(f"  skipped symlinks  : {counters.skipped_symlinks}")
    print(f"  skipped missing   : {counters.skipped_missing}")
    print(f"  errors            : {counters.errors}")


if __name__ == "__main__":
    main()

