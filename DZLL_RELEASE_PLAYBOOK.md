# DZLL Release Playbook

This is the release runbook for DayZ Linux Launcher. Follow it in order. Treat current source, packaging files, the release tag, release artifacts, and live platform behavior as authoritative. This document captures reusable procedures; version numbers, checksums, package tool versions, build hosts, and release asset names must be verified anew each release.

## 1. Release model and source of truth

A release has separate objects that must not be conflated:

1. A Git commit contains source and packaging changes.
2. A Git tag names a commit. GitHub automatic “Source code (zip)” and “Source code (tar.gz)” downloads are generated from the tagged tree.
3. Binary and source packages are built from a chosen source archive and build recipe.
4. A GitHub Release is a separate publication containing notes and manually attached assets. Publishing it does not create or repair a tag or the manually attached packages.

A beta tag (for example, a tag ending in `-beta`) must be published as a GitHub **pre-release**. A stable release must not be marked pre-release. Verify the exact version policy and current project convention before naming any tag; do not assume Python, Debian, RPM, Arch, and Git tag version strings are identical.

## 2. Preparation and version verification

1. Read `AGENTS.md` and `DZLL_MEMORY.md` at the start of the session, then inspect `README.md`, release notes, current packaging files, release scripts, and the latest pre-release report.
2. Record the working tree before any edits:

   ```sh
   git --no-pager status --short
   git --no-pager diff
   ```

   Preserve unrelated dirty and untracked files. Do not stage or delete them as cleanup.
3. Determine the intended version from the release request and compare every current version authority:

   ```sh
   rg -n 'version|Version|pkgver|pkgrel|v[0-9]' pyproject.toml setup.py debian/changelog dzll_launcher.spec packaging/arch README.md
   ```

   Inspect release-side finalized packaging recipes too. Known current convention from v0.4.2-beta: Python metadata `0.4.2b0`, Debian version `0.4.2~b0-1`, RPM `0.4.2` with beta release metadata, Arch `pkgver=0.4.2`, Git tag `v0.4.2-beta`. These are examples, not constants.
4. Confirm the requested work includes release publication before publishing. A request to audit or prepare is not publication authorization.

## 3. Branch, commit, and tag preflight

Before building or tagging, verify branch, HEAD, tags, and cleanliness against the intended release commit. Example read-only checks:

```sh
git --no-pager status --short
git --no-pager branch --show-current
git --no-pager log -1 --decorate --oneline
git --no-pager show --no-patch --format=fuller <release-commit>
git --no-pager rev-parse <release-tag>^{commit}
git --no-pager tag --points-at <release-commit>
```

Verify the release tag points to the exact intended commit and inspect the tag object if annotated. Do not create, move, delete, or force-update tags without explicit authorization. Do not use mutating Git commands in a session governed by the repository hard rules. A release tag pushed to GitHub is effectively immutable for release purposes; if its contents are wrong, prefer a new authorized correction version/tag.

## 4. Source archive hygiene

Inspect the *tracked tree*, not only the working directory. GitHub-generated source archives include tracked files and exclude untracked local files. `git status --short` identifies untracked work, while `git ls-tree` identifies what a tag tracks.

```sh
git --no-pager ls-tree -r --name-only <tag>
git --no-pager ls-tree -r --name-only <tag> | rg '(^|/)(AGENTS\.md|DZLL_MEMORY\.md|MEMORY\.md|devReports|\.codex|\.qwen|.*\.tmp)'
```

Also inspect the actual tag archive locally:

```sh
git archive --format=tar.gz --prefix=dzll-release/ <tag> > /tmp/dzll-tag.tar.gz
tar -tzf /tmp/dzll-tag.tar.gz
```

Search its file listing for internal reports, agent instructions, memory, credentials, logs, build outputs, local user data, caches, temporary files, and unrelated project material. Inspect likely sensitive files by tracked path and purpose; do not dump secrets into logs.

### Durable archive exclusion policy

For files that must remain tracked for project work but must not ship in GitHub source downloads, the recommended future solution is committed `.gitattributes` rules using Git's `export-ignore`, for example:

```gitattributes
/devReports/ export-ignore
/AGENTS.md export-ignore
/DZLL_MEMORY.md export-ignore
```

Choose precise patterns after reviewing every tracked path. Do not exclude files required to build or use the project. `export-ignore` changes archive export only: files remain tracked and visible in the repository. GitHub's automatic tag source archives honor these Git archive attributes. A separately generated filtered tarball alone is insufficient because GitHub will still expose its automatic source archives.

After a future authorized `.gitattributes` change is committed and included in the release tag, test both local `git archive <tag>` output and the actual GitHub auto-generated archive. Debian orig sources, Fedora source packages, and Arch source builds that consume the GitHub archive then receive the filtered file set; confirm each build still has its required inputs. Do not add these rules to an already published tag or alter `.gitattributes` without explicit scope authorization.

## 5. README and release notes

Compare `README.md` with tagged source behavior, supported platforms, current installation routes, and the assets actually planned for release. Confirm:

- native Steam is required; Flatpak Steam is unsupported; SteamCMD is not a backend or fallback;
- architecture and distro claims match actual package builds and supported source prerequisites;
- install commands, archive filenames, extraction directory names, and package filenames match the release;
- no unsupported package method or stale backend instructions remain;
- package verification guidance points to the correct checksum file.

Review `release-v<version>/RELEASE_NOTES.md` against the diff from the previous release and actual behavior. Separate verified fixes from unmeasured performance claims. Ensure compatibility and package availability statements match built artifacts. If README corrections are made after the tag, remember they do not appear in the tag or auto-generated source archives unless a new authorized source commit/tag is made.

## 6. Steamworks SDK 1.65 native library

DZLL v0.4.2 introduced the official Valve Steamworks SDK v1.65 Linux x86-64 redistributable `sdk/redistributable_bin/linux64/libsteam_api.so`. The project notice at `src/dzll_launcher/native/linux_x86_64/NOTICE` records origin, Valve copyright, architecture, source binary SHA-256, and the Steamworks SDK Access Agreement section 1.1(b). `debian/copyright` and RPM/Arch package license installation also preserve attribution and licensing references.

Redistribution is conditional: the cited agreement grants a nonexclusive, royalty-free, terminable, worldwide, nontransferable right to redistribute SDK redistributable files in object-code form along with Licensee Software, subject to its terms. Verify the current agreement and eligibility before each release. Retain the Valve notice and do not imply DZLL's own licence covers the Valve binary.

The runtime loader expects the exact filename `libsteam_api.so` at `dzll_launcher/native/linux_x86_64/libsteam_api.so`. Do not rename it, treat it as a Python extension, or rely on system library installation. For every package, extract the payload and compare the library SHA-256 to the authoritative Valve redistributable binary used for the build. The v0.4.2 notice provides a historical expected digest; obtain and verify the authoritative binary afresh rather than treating that digest as universal.

## 7. Fedora RPM and source RPM

Build on the established Fedora build environment after verifying its Fedora release and toolchain. The current spec is `dzll_launcher.spec`; inspect its source name, build requirements, dependencies, license files, architecture, strip/provides handling, and changelog.

A typical RPM build, with a prepared source archive staged under the active rpmbuild topdir, is:

```sh
rpmbuild -ba dzll_launcher.spec
```

Use the environment's configured `%{_topdir}` and its `SOURCES/` and `SPECS/` layout; do not assume a particular home path. Confirm the spec's `Source0` filename and `%prep` directory match the archive root. Inspect both resulting binary RPM and SRPM, not only the command exit status:

```sh
rpm -qpl <binary.rpm>
rpm -qp --scripts <binary.rpm>
rpm -qpl <source.rpm>
```

Extract the SRPM's source tarball and inspect it for excluded development files. Extract the binary RPM's native library and compare its digest to the verified SDK binary. The current spec disables RPM stripping with `%global __strip /bin/true` to preserve the official binary byte-for-byte, and excludes the package-private library from automatic provides. Validate these rules against current RPM tooling every release.

## 8. Ubuntu/Debian package on ProDesk

Build the Ubuntu/Debian package on the established ProDesk Debian/Ubuntu build environment. The machine name is an operational convention, not a portable path or guaranteed host; verify access, OS release, architecture, and build dependencies before use. Build only from the inspected release source tree/archive and current Debian packaging files (`debian/control`, `debian/changelog`, `debian/copyright`, `debian/rules`). Use the project's established ProDesk command sequence if available; otherwise verify current Debian packaging instructions and dependencies before running `dpkg-buildpackage`.

The current `debian/rules` contains essential native-library safeguards:

- `override_dh_auto_install` installs the official library explicitly at `/usr/lib/python3/dist-packages/dzll_launcher/native/linux_x86_64/libsteam_api.so` after pybuild installation.
- `override_dh_python3` removes the wrongly classified `libsteam_api.cpython-*.so` alias created by Python module scanning.
- `override_dh_strip` runs `dh_strip -Xlibsteam_api` to avoid modifying the official binary.

Do not remove or simplify these rules without reproducing and correcting the underlying failure. Inspect the built `.deb` with `dpkg-deb -c` and extract it with `dpkg-deb -x`; require the exact runtime filename, no `libsteam_api.cpython-*.so` replacement, required NOTICE/license files, and matching SDK SHA-256. Check for development files in the package payload.

## 9. Arch package in the `dzll-arch` Distrobox

The established workflow builds Arch from the Fedora desktop using the existing `dzll-arch` Distrobox. Verify it exists and is actually Arch before building:

```sh
distrobox enter dzll-arch
```

Run `makepkg -f` inside that container as the normal user, without `sudo`. Use a finalized PKGBUILD outside the tagged source tree to avoid a self-referential archive checksum. Pin the checksum of the actual GitHub tag source archive; never use `SKIP`, a previous release's hash, or an unmeasured placeholder. The release template is `packaging/arch/PKGBUILD.template`; compare it with the release-side finalized PKGBUILD.

The v0.4.2 recipe includes `options=('!strip')`, required because makepkg's default stripping altered the SDK binary. Verify the actual package:

```sh
pacman -Qlp <package.pkg.tar.zst>
pacman -Qip <package.pkg.tar.zst>
```

Extract with `bsdtar -xOf <package.pkg.tar.zst> <exact-library-path>` or unpack to a temporary directory, then check the library digest, runtime filename, NOTICE/license payload, and absence of internal development files. Keep the release package architecture `x86_64`; do not infer it from an older PKGBUILD that used `any`.

## 10. Source archive download and checksum

For a release built from GitHub's automatic tag archive, download that exact archive and verify its digest before using it as package input. Example pattern:

```sh
curl -fL <github-tag-archive-url> -o <tag-source.tar.gz>
sha256sum <tag-source.tar.gz>
```

Record the measured value only in the release-specific build recipe or checksum manifest, never as a timeless playbook constant. Inspect the downloaded archive file listing after checksum verification. Do not substitute a locally made archive if downstream recipes are pinned to the GitHub archive.

## 11. Consolidate assets and verify checksums

Use the release directory `release-v<version>/` for the final candidate assets. Copy—not move—the verified Fedora binary RPM, Fedora SRPM, Ubuntu/Debian DEB, and Arch x86_64 package into that directory. Include the reviewed release notes and intended source archive according to the release plan. Preserve build directories and existing reports.

Generate checksums from within that directory so entries are relative and portable:

```sh
cd release-v<version>
sha256sum <binary-rpm> <source-rpm> <deb> <arch-package> > SHA256SUMS
sha256sum -c SHA256SUMS
```

If the source archive is meant to be covered, include it explicitly. The checksum file cannot include its own digest. Verify that every listed filename exists and no stale or duplicate asset is present.

## 12. Final payload and archive inspection

Before publication, verify all package types and the source archive independently:

- Source archive: exact tag, README, build metadata, no excluded internal content, and sufficient files to build all packages.
- RPM: `rpm -qpl`; verify native library and licenses/notices.
- SRPM: `rpm -qpl`, extract source archive and spec, repeat source-content inspection.
- DEB: `dpkg-deb -c`; extract and verify native library, no renamed alias, and licenses/notices.
- Arch: `pacman -Qlp`; extract and verify native library, metadata, and licenses/notices.

For each binary package, require exact `libsteam_api.so` runtime path and byte-for-byte digest equality with the authoritative SDK redistributable. Search payloads for `devReports`, `AGENTS.md`, memory/instruction files, credentials, temporary files, logs, build caches, and unrelated artifacts. A successful build, checksum manifest, or source file list is not a substitute for extracted package inspection.

## 13. GitHub CLI authentication and publication

Do not print or log credentials. GitHub CLI accepts `GH_TOKEN`; supply the existing `DZLL_GITHUB_TOKEN` in the process environment without putting its value in command output or shell history. Check authentication safely:

```sh
GH_TOKEN="$DZLL_GITHUB_TOKEN" gh auth status
```

If this fails, stop publication and resolve the existing credential source through the authorized operator path. Do not request `gh auth login` when the task prohibits it. Never include a token in command arguments, reports, or logs.

Only publish when explicitly authorized and after every blocker is resolved. Example command shape (review syntax and release state first):

```sh
GH_TOKEN="$DZLL_GITHUB_TOKEN" gh release create <tag> \
  --title '<release title>' \
  --notes-file release-v<version>/RELEASE_NOTES.md \
  <release-v<version>/<asset-1> ...>
```

For a beta tag, add `--prerelease` explicitly. For a stable release, omit it and verify GitHub shows it as a full release. Do not use `--target` to silently create a different tag; tag creation and movement are separate authorized operations. If the release already exists, use the appropriate `gh release edit` or upload command only after checking current assets and user authorization.

## 14. Post-publication verification

After authorized publication:

1. Inspect release metadata and assets with `gh release view <tag>`; verify title, notes, pre-release status, and every expected asset.
2. Download each release asset to a fresh temporary directory and run `sha256sum -c SHA256SUMS`.
3. Download GitHub's automatic source archive for the tag and inspect it independently. Confirm archive exclusions apply to both tar and zip downloads.
4. Reinspect package payloads or compare their hashes with the pre-publication verified artifacts.
5. Record exact published URLs, asset names, and validation outcomes in the release report. Do not include secrets.

## 15. Known failures and established fixes

- **Tracked development reports appear in GitHub source archives:** untracked files are irrelevant to the archive. Use a committed, tested `.gitattributes` `export-ignore` policy for internal-only paths; a filtered sidecar tarball does not change GitHub's automatic archives.
- **Python packaging treats `libsteam_api.so` as an extension module:** Debian pybuild/dh-python can create a renamed `libsteam_api.cpython-*.so`. The current `debian/rules` explicitly reinstalls the original and removes the alias after `dh_python3`.
- **Debian strips the SDK library:** exclude it with `dh_strip -Xlibsteam_api` in `override_dh_strip` and compare extracted bytes.
- **Arch makepkg strips the SDK library:** use `options=('!strip')` in the release PKGBUILD and compare extracted bytes.
- **RPM build scripts can strip the SDK library:** current spec overrides `__strip`; verify on the current Fedora toolchain.
- **A package build succeeds but output is wrong:** inspect extracted payloads and compare the native binary checksum against the authoritative SDK source.
- **A source RPM can preserve a bad source archive:** inspect SRPM contents, not only the binary RPM.
- **GitHub authentication fails:** the v0.4.2-beta audit found no authenticated host despite the attempted `DZLL_GITHUB_TOKEN`/`GH_TOKEN` path. Diagnose credential availability without exposing it; do not publish until `gh auth status` succeeds.

## 16. Evidence and fresh verification boundary

Durable facts include the native library runtime filename, current Debian and Arch preservation mechanisms, and the need to exclude internal tracked paths from archives. Every release must freshly verify version metadata, branch/commit/tag identity, package architectures and dependencies, SDK agreement terms and source binary, build host/tool versions, archive exclusions, checksums, package contents, GitHub authentication, release state, and upload assets. Historical filenames, checksums, release IDs, and build paths are examples only.
