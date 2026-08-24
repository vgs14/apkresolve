#!/usr/bin/env python3
"""
apk_resolve.py — Recursively resolve Alpine package dependencies against
one or more APKINDEX repos and print the download URL for every package
needed (including transitive deps), rooted at dl-cdn.alpinelinux.org.

Resolves Alpine APK package dependencies
Copyright (C) 2026 Richard Bishop

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.



Usage:
    python3 apk_resolve.py PKG [PKG ...] \
        --version 3.20 --arch x86_64 --repo main --repo community

    # read package names from a file (one per line, '#' comments allowed):
    python3 apk_resolve.py --file wanted.txt --version 3.20 --arch x86_64

    # resolve AND download the .apk files, mirroring the repo layout:
    python3 apk_resolve.py --file wanted.txt --version 3.20 --arch x86_64 \
        --output-dir ./mirror

    # tell it what's already assumed to be installed on the target so install_if
    # triggers (e.g. seatd-openrc, dbus-openrc) resolve correctly even
    # though nothing in your world file mentions openrc directly:
    python3 apk_resolve.py --file wanted.txt --version 3.20 --arch x86_64 \
        --assume-installed openrc --output-dir ./mirror

    This creates:
        ./mirror/v3.20/main/x86_64/APKINDEX.tar.gz
        ./mirror/v3.20/main/x86_64/musl-1.2.5-r0.apk
        ./mirror/v3.20/community/x86_64/APKINDEX.tar.gz
        ./mirror/v3.20/community/x86_64/libfoo-2.0-r1.apk
        ...
    i.e. the same v{version}/{repo}/{arch}/ structure used on
    dl-cdn.alpinelinux.org/alpine/, just rooted at --output-dir instead of
    ".../alpine/". The APKINDEX.tar.gz for each repo/arch you queried is
    saved alongside the .apk files, so --output-dir ends up as a
    self-contained mirror apk(8) could point at directly.

Notes:
- Requires network access to dl-cdn.alpinelinux.org (fetches APKINDEX.tar.gz
  per repo/arch, caches them locally in ./apkindex_cache/).
- Handles virtual "provides" and so:libfoo.so.N style dependencies, choosing
  the provider apk itself would (highest provider_priority / k: field wins;
  an exact package name always wins over anything merely provided). When a
  virtual dependency has more than one candidate provider, a NOTE is printed
  to stderr showing what was chosen and the alternatives, so you can add a
  specific package name to your input list to force a different one.
- Handles install_if triggers (e.g. ssl-client, which isn't a normal
  `depend:` of anything but gets auto-installed by apk once its trigger
  packages/sonames are present in the closure).
- Ignores conflict deps (lines starting with "!").
- If a dependency has alternatives (rare, pipe-separated in some indexes),
  the first resolvable option is taken.
- Downloads are skipped if the destination file already exists with the
  correct size, so re-running is cheap/resumable.
- --assume-installed / --assume-installed-file let you tell the tool what
  is already installed on the target machine (e.g. 'openrc' from a base
  image). This matters because apk's install_if triggers (see above)
  evaluate against the *actual full installed set* on a real system, not
  just your world file — a package like 'seatd-openrc' only appears in a
  real `apk add` because openrc was already on the box, even though
  nothing in the world file mentions it. Assumed packages are resolved
  (with their own deps) purely to satisfy those checks; they are not
  included in the printed output or downloaded, since they're assumed to
  already exist on the target.
"""

import argparse
import io
import os
import re
import sys
import tarfile
import urllib.request

BASE_URL = "https://dl-cdn.alpinelinux.org/alpine"
CACHE_DIR = "apkindex_cache"

VERSION_SPLIT_RE = re.compile(r"[=<>~]")


def read_package_list_file(path):
    """Read a text file of package names, one per line, skipping blank
    lines and lines starting with '#' (leading whitespace is allowed
    before the '#'). Used for both --file and --assume-installed-file,
    since they share the exact same format."""
    names = []
    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue  # skip blank lines and comment lines
            names.append(line)
    return names


def strip_constraint(token: str) -> str:
    """Turn 'foo>=1.2.3' or 'so:libz.so.1' constraint forms into a bare name
    for lookup purposes. Sonames keep their 'so:' prefix since that's how
    they appear as provides."""
    # APKINDEX depend/provides tokens can carry a version constraint
    # (=, <, >, ~) glued directly onto the name, e.g. "musl>=1.2.3" or
    # "so:libssl.so.3=3". We only care about the name for lookups, so
    # split on the first constraint operator and keep the left half.
    return VERSION_SPLIT_RE.split(token, 1)[0]


def fetch_apkindex(version: str, repo: str, arch: str, output_dir: str = None) -> str:
    """Fetch the APKINDEX (package metadata) for one repo/arch combination
    and return its decoded text content.

    Behaviour:
    - The raw APKINDEX text is cached locally under CACHE_DIR so re-running
      the script against the same version/repo/arch doesn't re-download it.
    - If `output_dir` is given, the *original* APKINDEX.tar.gz archive is
      also copied into `output_dir/v{version}/{repo}/{arch}/APKINDEX.tar.gz`
      (skipped if already present there), so --output-dir ends up as a
      self-contained local mirror apk(8) could point at directly.

    Returns the plain-text contents of the APKINDEX file (not the tar.gz).
    """
    # Local cache of the *decoded* index text, keyed by version/repo/arch.
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{version}_{repo}_{arch}_APKINDEX")

    raw_data = None  # holds the raw APKINDEX.tar.gz bytes, if we fetched them this run
    if not os.path.exists(cache_path):
        # Not cached yet: download the real archive from dl-cdn.
        url = f"{BASE_URL}/v{version}/{repo}/{arch}/APKINDEX.tar.gz"
        sys.stderr.write(f"fetching {url}\n")
        with urllib.request.urlopen(url) as resp:
            raw_data = resp.read()
        # APKINDEX.tar.gz contains a single file, "APKINDEX", with the
        # actual pipe-delimited package metadata we want to parse.
        tf = tarfile.open(fileobj=io.BytesIO(raw_data), mode="r:gz")
        member = tf.getmember("APKINDEX")
        text = tf.extractfile(member).read().decode("utf-8", errors="replace")
        # Cache the decoded text for next time.
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        # Already cached: just read the decoded text back from disk.
        with open(cache_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

    if output_dir:
        # Mirror mode: also place a copy of the raw APKINDEX.tar.gz inside
        # the output directory, at the same path convention used for the
        # .apk files (v{version}/{repo}/{arch}/...).
        dest = os.path.join(output_dir, f"v{version}", repo, arch, "APKINDEX.tar.gz")
        if not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if raw_data is None:
                # We served `text` from the local cache above, so we don't
                # have the raw archive bytes in memory — re-download just
                # the archive so we can place real bytes in the mirror.
                url = f"{BASE_URL}/v{version}/{repo}/{arch}/APKINDEX.tar.gz"
                sys.stderr.write(f"fetching {url} (for mirror)\n")
                with urllib.request.urlopen(url) as resp:
                    raw_data = resp.read()
            with open(dest, "wb") as f:
                f.write(raw_data)
            sys.stderr.write(f"saved: v{version}/{repo}/{arch}/APKINDEX.tar.gz\n")

    return text


def parse_apkindex(text: str, repo: str, arch: str, packages: dict, provides_map: dict,
                    provides_priority: dict = None, provides_candidates: dict = None):
    """Parse one APKINDEX blob, merging results into `packages` and
    `provides_map`. Later repos don't override earlier ones for the same
    package name (first match wins, mirroring apk's repo priority order).

    For virtual/provided names (including so:libfoo.so.N sonames), the
    provider is chosen the way apk itself does: an exact package name
    always wins; among multiple packages that `provide:` the same name,
    the one with the highest provider_priority (k: field, default 0)
    wins. `provides_candidates` (if given) records every candidate per
    name for diagnostics.
    """
    if provides_priority is None:
        provides_priority = {}
    if provides_candidates is None:
        provides_candidates = {}
    block = {}  # accumulates the "KEY:value" lines of the package block currently being read

    def flush(block):
        """Called at each blank line (APKINDEX separates package records
        with a blank line). Turns the accumulated `block` dict of raw
        single-letter fields into a package entry and registers it, plus
        updates the provides/priority maps."""
        if not block or "P" not in block:
            return  # empty or malformed block, nothing to do
        name = block["P"]                              # P: package name
        version = block.get("V", "")                    # V: version-release
        priority = int(block.get("k", "0") or "0")       # k: provider_priority (default 0)
        entry = {
            "name": name,
            "version": version,
            "repo": repo,
            "arch": block.get("A", arch),                                   # A: architecture
            "depends": block.get("D", "").split() if "D" in block else [],  # D: depends
            "provides": block.get("p", "").split() if "p" in block else [], # p: provides
            "install_if": block.get("i", "").split() if "i" in block else [],  # i: install_if
            "provider_priority": priority,
            "filename": f"{name}-{version}.apk",
        }
        if name not in packages:
            # First repo to define this package name wins (mirrors apk's
            # repo priority order: main before community, etc., based on
            # the order repos are passed in).
            packages[name] = entry

        # An exact package name always outranks anything merely "provided".
        # e.g. if some other package's `p:` line claims to provide "musl",
        # a real package literally named "musl" still wins.
        if provides_priority.get(name) != float("inf"):
            provides_map[name] = name
            provides_priority[name] = float("inf")

        # Register this package as a candidate provider for each name in
        # its `provides` list (virtual packages and so:libfoo.so.N sonames).
        # Among competing providers of the same name, the one with the
        # highest provider_priority wins — this is exactly how real apk
        # resolves ambiguous virtual dependencies.
        for p in entry["provides"]:
            pname = strip_constraint(p)
            provides_candidates.setdefault(pname, []).append((priority, name))
            current = provides_priority.get(pname)
            if current == float("inf"):
                continue  # a real package already owns this exact name; never override
            if current is None or priority > current:
                provides_map[pname] = name
                provides_priority[pname] = priority

    # APKINDEX format: each package is a block of "KEY:value" lines,
    # blocks are separated by a single blank line.
    for line in text.splitlines():
        if line == "":
            flush(block)
            block = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            block[k] = v
    flush(block)  # the file may not end with a trailing blank line


def condition_satisfied(token, resolved, provides_map):
    """Is the install_if condition token (a package name or so:soname,
    possibly with a version constraint) satisfied by the current resolved
    set?"""
    # An install_if condition is satisfied if the package/soname it names
    # (after stripping any version constraint, and resolving through the
    # provides map if it's virtual) is already in the resolved set.
    bare = strip_constraint(token)
    real_name = provides_map.get(bare, bare)
    return real_name in resolved


def resolve(names, packages, provides_map, provides_candidates=None, notify=True, preseed=None):
    """Recursively resolve `names` (a list of package/virtual-package
    tokens) against the parsed `packages`/`provides_map`, returning
    everything that would end up installed.

    Two resolution passes run in a loop until a fixpoint is reached:
      1. Forward dependency walk: follow each package's `depends` (D:)
         field, resolving virtual names/sonames through `provides_map`.
      2. install_if fixpoint: any package whose `install_if` conditions
         are now ALL satisfied by what's resolved so far gets pulled in
         too (this is how apk auto-installs things like ssl-client that
         nothing explicitly depends on).
    Because step 2 can add packages with their own `depends`, we loop
    back to step 1 until a full pass adds nothing new.

    `preseed`, if given, is a dict of package name -> entry (as returned
    by a previous call to `resolve`) to start the resolved set from. This
    is how "assume these packages are already installed on the target"
    baselines are implemented: seed `resolved` with the baseline's own
    fully-expanded resolve() result so install_if conditions and normal
    dependencies can see them as already satisfied, without walking their
    dependencies again. The caller is responsible for excluding baseline
    entries from final output if they don't want them re-reported/re-
    downloaded (see main()'s handling of --assume-installed).

    Returns (resolved, unresolved):
      resolved   - dict of package name -> package entry for everything
                   that should be installed (including any preseed
                   entries — the caller strips those back out if needed).
      unresolved - set of tokens that couldn't be matched to any package
                   or provided name at all (likely typos or packages in
                   a repo that wasn't queried).
    """
    resolved = dict(preseed) if preseed else {}  # pkg name -> entry
    unresolved = set()
    stack = list(names)  # work queue of tokens still needing resolution
    notified = set()     # tokens we've already printed an ambiguity NOTE for

    def note_ambiguous(bare, real_name):
        """Print a one-time stderr note when a virtual dependency had more
        than one candidate provider, so the user can see what was chosen
        and knows they can pin a specific package name instead."""
        if not notify or not provides_candidates or bare in notified:
            return
        candidates = provides_candidates.get(bare)
        if not candidates or len(candidates) < 2:
            return  # not actually ambiguous, nothing to report
        notified.add(bare)
        alts = ", ".join(f"{n} (priority {p})" for p, n in sorted(candidates, reverse=True))
        sys.stderr.write(
            f"NOTE: virtual dependency '{bare}' is provided by multiple packages: {alts}. "
            f"Chose '{real_name}'. Add the specific package name to your input list to force a different one.\n"
        )

    while True:
        # --- Pass 1: normal forward dependency walk (D: field) ---
        while stack:
            token = stack.pop()
            bare = strip_constraint(token)  # drop any version constraint for lookup

            # Resolve the token to a real package name: first try the
            # provides map (covers virtual packages, sonames, and real
            # package names too, since parse_apkindex registers those),
            # then fall back to a direct package-name match.
            real_name = provides_map.get(bare)
            if real_name is None and bare in packages:
                real_name = bare
            if real_name is None:
                unresolved.add(bare)  # nothing in the index provides this
                continue

            note_ambiguous(bare, real_name)

            if real_name in resolved:
                continue  # already handled, skip re-processing its deps

            entry = packages[real_name]
            resolved[real_name] = entry

            # Queue this package's own dependencies for resolution.
            for dep in entry["depends"]:
                dep = dep.strip()
                if not dep or dep.startswith("!"):
                    continue  # "!" lines are conflicts, not real dependencies
                stack.append(dep)

        # --- Pass 2: install_if fixpoint ---
        # Scan every known package (not just resolved ones) for any whose
        # install_if conditions are now fully satisfied by the resolved
        # set. This can itself expose new depends, so we loop back to
        # pass 1 if anything new was added.
        added_any = False
        for pkg_name, entry in packages.items():
            if pkg_name in resolved:
                continue
            conditions = entry.get("install_if")
            if not conditions:
                continue  # no install_if trigger for this package
            if all(condition_satisfied(c, resolved, provides_map) for c in conditions):
                resolved[pkg_name] = entry
                stack.extend(entry["depends"])  # its deps still need resolving
                added_any = True

        if not added_any:
            break  # fixpoint reached, nothing new in this iteration

    return resolved, unresolved


def build_url(entry, version):
    """Build the full dl-cdn.alpinelinux.org download URL for a resolved
    package entry, e.g.
    https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/musl-1.2.5-r0.apk
    """
    return f"{BASE_URL}/v{version}/{entry['repo']}/{entry['arch']}/{entry['filename']}"


def build_relpath(entry, version):
    """Path mirroring the repo layout, relative to an output root:
    v{version}/{repo}/{arch}/{filename}"""
    # Used both for local mirror downloads and for messages/logging, so
    # this stays a plain relative OS path (os.path.join handles the
    # platform-specific separator).
    return os.path.join(f"v{version}", entry["repo"], entry["arch"], entry["filename"])


def download_packages(resolved, version, output_dir, quiet=False):
    """Download every resolved package's .apk file into `output_dir`,
    mirroring the v{version}/{repo}/{arch}/ layout used by the real repo.

    Skips a file if it already exists locally with the same size as the
    remote copy (checked via a HEAD request), so re-running the script is
    cheap and safe to interrupt/resume.

    If `quiet` is True, the "skip (exists)" progress lines are suppressed
    (download and error messages are still printed, since those represent
    actual work/problems rather than routine no-ops).
    """
    total = len(resolved)
    for i, name in enumerate(sorted(resolved), 1):
        entry = resolved[name]
        url = build_url(entry, version)
        relpath = build_relpath(entry, version)
        dest = os.path.join(output_dir, relpath)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        if os.path.exists(dest):
            # File already present locally — check if it's already
            # complete by comparing sizes with the server before deciding
            # whether to skip or re-download.
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req) as resp:
                    remote_size = int(resp.headers.get("Content-Length", -1))
                if os.path.getsize(dest) == remote_size and remote_size >= 0:
                    if not quiet:
                        sys.stderr.write(f"[{i}/{total}] skip (exists): {relpath}\n")
                    continue
            except Exception:
                pass  # HEAD failed for some reason; fall through and re-download to be safe

        sys.stderr.write(f"[{i}/{total}] downloading: {relpath}\n")
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as e:
            # Don't let one failed package abort the whole run; report and
            # keep going so the rest of the mirror still gets built.
            sys.stderr.write(f"  ERROR downloading {url}: {e}\n")


def main():
    """CLI entry point: parse arguments, load the requested package
    name(s), fetch/parse the relevant APKINDEX files, resolve the full
    transitive dependency set, then print and/or download the results."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("packages", nargs="*", help="package names to resolve")
    ap.add_argument("--file", help="file with one package name per line")
    ap.add_argument("--version", default="3.20", help="Alpine version, e.g. 3.20")
    ap.add_argument("--arch", default="x86_64", help="architecture, e.g. x86_64, aarch64")
    ap.add_argument("--repo", action="append", default=None,
                     help="repo(s) to search, can repeat (default: main community)")
    ap.add_argument("--urls-only", action="store_true", help="print only URLs")
    ap.add_argument("--quiet", "-q", action="store_true",
                     help="suppress package/URL listing on stdout and 'skip (exists)' "
                          "download progress lines (downloads and errors still print to stderr)")
    ap.add_argument("--output-dir", help="if set, download resolved .apk files here, "
                                          "mirroring v{version}/{repo}/{arch}/ layout")
    ap.add_argument("--assume-installed", action="append", default=None,
                     help="package(s) to treat as already installed on the target (e.g. "
                          "'openrc'), so install_if triggers depending on them still fire "
                          "correctly. Can repeat. Assumed packages (and their own deps) are "
                          "resolved for satisfying dependencies/install_if but are NOT "
                          "included in the printed/downloaded output, since they're assumed "
                          "to already be present.")
    ap.add_argument("--assume-installed-file",
                     help="file with one 'assumed installed' package name per line "
                          "(same format as --file: blank lines and '#' comments skipped)")
    args = ap.parse_args()

    # Build the full wanted-package list from positional args plus,
    # optionally, a file (one package name per line).
    wanted = list(args.packages)
    if args.file:
        wanted += read_package_list_file(args.file)
    if not wanted:
        ap.error("no packages given (pass as args or --file)")

    # Build the "assume already installed" baseline list, if any, the same
    # way (positional-style --assume-installed args plus an optional file).
    assume_installed = list(args.assume_installed or [])
    if args.assume_installed_file:
        assume_installed += read_package_list_file(args.assume_installed_file)

    repos = args.repo or ["main", "community"]

    # Fetch and parse every requested repo/arch combination into a single
    # merged package index before resolving anything.
    packages = {}
    provides_map = {}
    provides_priority = {}
    provides_candidates = {}
    for repo in repos:
        text = fetch_apkindex(args.version, repo, args.arch, output_dir=args.output_dir)
        parse_apkindex(text, repo, args.arch, packages, provides_map,
                        provides_priority, provides_candidates)

    # If a baseline was given, fully resolve it first (dependencies +
    # install_if, exactly like a normal resolve) so it can be used to
    # seed the main resolve below. `notify=False` since we don't need
    # ambiguous-provider notes for packages that aren't even being
    # reported/downloaded.
    baseline_resolved = {}
    if assume_installed:
        baseline_resolved, baseline_unresolved = resolve(
            assume_installed, packages, provides_map, provides_candidates, notify=False
        )
        if baseline_unresolved:
            sys.stderr.write(
                f"WARNING: could not resolve --assume-installed packages: "
                f"{', '.join(sorted(baseline_unresolved))}\n"
            )

    # Recursively resolve the wanted packages against the merged index,
    # seeded with the baseline so install_if conditions referencing
    # baseline packages (e.g. 'openrc') are already satisfied.
    resolved, unresolved = resolve(
        wanted, packages, provides_map, provides_candidates, preseed=baseline_resolved
    )

    if unresolved:
        sys.stderr.write(f"WARNING: could not resolve: {', '.join(sorted(unresolved))}\n")

    # Strip baseline packages back out of the result: they're assumed to
    # already be on the target, so they shouldn't be reported or
    # downloaded as "new" — only genuinely new packages should be.
    if baseline_resolved:
        resolved = {name: entry for name, entry in resolved.items() if name not in baseline_resolved}

    # Print results (unless --quiet), one line per resolved package.
    for name in sorted(resolved):
        entry = resolved[name]
        url = build_url(entry, args.version)
        if args.quiet:
            continue
        if args.urls_only:
            print(url)
        else:
            print(f"{name}\t{entry['version']}\t{entry['repo']}\t{url}")

    # Optionally download every resolved .apk (and the APKINDEX.tar.gz
    # files, handled inside fetch_apkindex above) into a local mirror.
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        download_packages(resolved, args.version, args.output_dir, quiet=args.quiet)


if __name__ == "__main__":
    main()