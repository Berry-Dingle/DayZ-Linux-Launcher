# Project Memory

This file records durable project knowledge that is not obvious from the
current source code.

Do not treat this file as authoritative when it conflicts with current code,
tests, configuration, or explicit user instructions.

Only add facts that have been verified or decisions that have been explicitly
made.

## Release packaging and distribution

See `DZLL_RELEASE_PLAYBOOK.md` for the sequential release procedure, example
commands, and fresh-verification checklist.

- DZLL v0.4.2 bundles Valve's official Steamworks SDK v1.65 Linux x86-64
  redistributable `sdk/redistributable_bin/linux64/libsteam_api.so`. Preserve
  Valve copyright and the SDK Access Agreement reference in NOTICE and package
  licensing metadata. Redistribution is conditional on the Agreement's scope;
  recheck current terms and eligibility for each release.
- Runtime loading requires the exact package path and filename
  `dzll_launcher/native/linux_x86_64/libsteam_api.so`. Python packaging tools
  can misclassify it as a CPython extension and rename it. Debian's established
  `debian/rules` explicitly reinstalls the original binary after pybuild,
  removes the `libsteam_api.cpython-*.so` alias after `dh_python3`, and excludes
  the library from `dh_strip` using `-Xlibsteam_api`.
- Arch `makepkg` strips native libraries by default; the verified release recipe
  uses `options=('!strip')`. The current Fedora spec overrides strip as well.
  Successful build output does not prove these rules worked: inspect extracted
  contents of every package and compare `libsteam_api.so` SHA-256 with the
  authoritative Valve source binary used for the build.
- GitHub's automatic source archives derive from the tagged tree and include
  tracked development material unless archive export rules exclude it. Local
  untracked reports do not enter those archives; tracked `devReports/` did enter
  v0.4.2-beta. A filtered sidecar archive does not fix GitHub's automatic tag
  downloads. Recommended future fix: precise `.gitattributes` `export-ignore`
  rules, committed before a future release tag and verified in GitHub archives.
  Excluded paths stay tracked in Git. Recheck Debian orig, Fedora SRPM, and Arch
  builds against the filtered source contents.
- Keep the Git tag, GitHub-generated source archives, manually attached release
  assets, and the GitHub Release record distinct during audits and remediation.
  The v0.4.2-beta release audit also corrected `README.md` in the worktree after
  the tag; that edit remains uncommitted and is absent from tagged source.
- Release environments used by the project include Fedora RPM/SRPM builds,
  Ubuntu/Debian DEB packaging on the ProDesk, and Arch packaging in the existing
  `dzll-arch` Distrobox. Confirm each environment and toolchain live; these host
  names are operational context, not portable paths.
- GitHub CLI release authentication is expected through `DZLL_GITHUB_TOKEN`
  supplied to `gh` as `GH_TOKEN`; the v0.4.2-beta audit found authentication
  unavailable. Check `gh auth status` without exposing the credential. Beta
  releases require explicit GitHub pre-release designation.
