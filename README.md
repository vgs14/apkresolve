# apk_resolve.py

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


Recursively resolve Alpine Linux package dependencies against the real
`APKINDEX` for a given Alpine version/architecture, print download URLs
(or package details), and optionally download the `.apk` files into a
local mirror with the same directory layout as `dl-cdn.alpinelinux.org`.

Unlike a naive "look up the `depends:` field" resolver, this script
mimics what `apk` itself actually does:

- **Virtual packages / sonames** (`so:libssl.so.3`, `ifupdown-any`, etc.)
  are resolved to a real package using `provider_priority`, the same
  tie-breaker `apk` uses when more than one package provides the same
  name.
- **`install_if` triggers** are handled — packages like `ssl-client` or
  `seatd-openrc` aren't a normal dependency of anything; `apk` installs
  them automatically once their trigger packages/sonames are present.
  This script runs that same fixpoint check.
- **Assumed-installed baselines** let you tell the script about packages
  already on your target image (e.g. `openrc` from a base image) so
  `install_if` triggers that depend on them still resolve correctly, even
  though your own package list never mentions them.

## Requirements

- Python 3
- Network access to `dl-cdn.alpinelinux.org`

## Basic usage

Resolve one or more packages by name and print `name / version / repo / URL`:

```bash
python3 apk_resolve.py curl bash --version 3.20 --arch x86_64
```

```
bash            5.2.26-r0   main    https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/bash-5.2.26-r0.apk
ca-certificates 20240705-r0 main    https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/ca-certificates-20240705-r0.apk
curl            8.9.0-r0    main    https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/curl-8.9.0-r0.apk
libcrypto3      3.3.1-r0    main    https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/libcrypto3-3.3.1-r0.apk
...
```

`--version` defaults to `3.20` and `--arch` defaults to `x86_64`, so
either can be omitted if that's what you want.

## Reading a package list from a file

Use `--file` instead of (or in addition to) listing packages on the
command line. One package per line; blank lines and `#` comments are
skipped:

```
# world.txt
alpine-base
cage
chromium

# desktop bits
mesa-dri-gallium
mesa-gl
seatd
eudev
dbus
ttf-dejavu
```

```bash
python3 apk_resolve.py --file world.txt --version 3.20 --arch x86_64
```

## URLs only

```bash
python3 apk_resolve.py curl --urls-only
```

```
https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/curl-8.9.0-r0.apk
https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/libcrypto3-3.3.1-r0.apk
...
```

Handy for piping straight into `xargs curl -O` or similar.

## Choosing repos

By default both `main` and `community` are searched. Pass `--repo` (it
can be repeated) to restrict or extend this:

```bash
python3 apk_resolve.py chromium --repo main --repo community --repo testing
```

## Downloading a local mirror

Pass `--output-dir` to actually download every resolved `.apk`, laid out
exactly like the real repo (`v{version}/{repo}/{arch}/...`), plus a copy
of each `APKINDEX.tar.gz` you queried:

```bash
python3 apk_resolve.py --file world.txt --version 3.20 --arch x86_64 \
    --output-dir ./mirror
```

```
mirror/
└── v3.20/
    ├── main/
    │   └── x86_64/
    │       ├── APKINDEX.tar.gz
    │       ├── musl-1.2.5-r0.apk
    │       └── ...
    └── community/
        └── x86_64/
            ├── APKINDEX.tar.gz
            ├── seatd-0.8.0-r0.apk
            └── ...
```

That layout means `./mirror` can be pointed at directly as an Alpine
repository (e.g. via `/etc/apk/repositories` on the target, or served
over HTTP).

Downloads are resumable/idempotent: if a file already exists locally with
the same size as the server's copy, it's skipped rather than
re-downloaded.

## Quiet mode

`--quiet` (or `-q`) suppresses the package/URL listing on stdout, along
with the `skip (exists)` download progress lines — useful for scripted,
silent mirror-building runs. Actual downloads and errors still print to
stderr:

```bash
python3 apk_resolve.py --file world.txt --output-dir ./mirror --quiet
```

## Telling it what's already installed (`--assume-installed`)

This is the one that trips people up. `apk`'s `install_if` mechanism
(used for packages like `seatd-openrc`, `dbus-openrc`, `eudev-openrc`)
triggers based on the **entire set of packages actually installed on the
target machine** — not just the packages in your world file. A base
Alpine image typically already has `openrc` installed even though
nothing in your explicit package list mentions it, so on a real system
`apk add seatd dbus eudev` also silently pulls in `seatd-openrc`,
`dbus-openrc`, and `eudev-openrc`.

This script only sees what you give it, so by default it won't know
`openrc` is already there and will miss those `-openrc` packages too.
`--assume-installed` fixes that: it lets you name packages that should be
treated as already present on the target, purely so `install_if`
conditions (and normal dependency lookups) that reference them still
succeed. Assumed packages themselves (and their own dependencies) are
**not** included in the printed output or downloaded — they're assumed
to already be there.

```bash
python3 apk_resolve.py --file world.txt --version 3.20 --arch x86_64 \
    --assume-installed openrc --output-dir ./mirror
```

`--assume-installed` can be repeated, and there's a file-based
equivalent with the same blank-line/`#`-comment format as `--file`:

```bash
python3 apk_resolve.py --file world.txt \
    --assume-installed-file base-image-packages.txt \
    --output-dir ./mirror
```

```
# base-image-packages.txt
openrc
busybox
```

## Reading the diagnostic notes

When a dependency is a *virtual* package (like `ifupdown-any`, satisfied
by any of `ifupdown`, `ifupdown-ng`, or `busybox-ifupdown`), the script
picks the same provider `apk` would (highest `provider_priority`) and
prints a note to stderr so you can see the choice was made and override
it if you want a different provider:

```
NOTE: virtual dependency 'ifupdown-any' is provided by multiple packages: ifupdown (priority 10), ifupdown-ng (priority 5), busybox-ifupdown (priority 0). Chose 'ifupdown'. Add the specific package name to your input list to force a different one.
```

To force a different provider, just add its exact package name to your
package list — an explicit name always wins over provider-priority
resolution.

If a package name can't be matched to anything in the index at all
(typo, or it's in a repo you didn't query), you'll see:

```
WARNING: could not resolve: some-typo-pkgname
```

## Full option reference

| Flag | Description |
|---|---|
| `packages` | Package names to resolve (positional, space-separated) |
| `--file FILE` | File with one package name per line (blank/`#` lines skipped) |
| `--version VERSION` | Alpine version, e.g. `3.20` (default: `3.20`) |
| `--arch ARCH` | Architecture, e.g. `x86_64`, `aarch64` (default: `x86_64`) |
| `--repo REPO` | Repo(s) to search; repeatable (default: `main community`) |
| `--urls-only` | Print only the download URLs, nothing else |
| `--quiet`, `-q` | Suppress stdout listing and `skip (exists)` download lines |
| `--output-dir DIR` | Download resolved `.apk` files here, mirroring the real repo layout |
| `--assume-installed PKG` | Treat `PKG` as already installed on the target; repeatable |
| `--assume-installed-file FILE` | Same as above, one package per line |

## How it works, briefly

1. Downloads (and caches locally in `./apkindex_cache/`) the `APKINDEX`
   for each repo/arch you're querying.
2. Parses every package's `depends`, `provides`, and `install_if` fields.
3. Starting from your requested package list (plus any
   `--assume-installed` baseline), walks the dependency graph to a
   fixpoint: normal dependencies are followed recursively, virtual names
   are resolved via provider priority, and any package whose
   `install_if` conditions become fully satisfied is pulled in too —
   which can itself expose more dependencies, so the process repeats
   until nothing new is added.
4. Prints the results and/or downloads them, excluding anything that was
   only present because of `--assume-installed`.