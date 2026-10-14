# Arch v0.4.2-beta release recipe

After the final release commit is tagged and the GitHub tag source archive is available, copy `PKGBUILD.template` to a release-side `PKGBUILD` outside the tagged source tree. Replace `REPLACE_WITH_V0_4_2_BETA_TAG_ARCHIVE_SHA256` with the measured SHA-256 of the GitHub `v0.4.2-beta` source archive. Do not use `SKIP` or the prior release hash.

On the Fedora desktop, run the established `dzll-arch` Arch Distrobox procedure and `makepkg -f` without sudo. Verify the resulting x86_64 package with `pacman -Qlp`, inspect its metadata and native library payload, and confirm the bundled ELF SHA-256 is `eb2dd015b84177cf4f4326fe578aab375fd8931bbbd719c7492420d9777007fe` before adding it to the release checksums.

Valve Steamworks SDK redistribution permission and required notices remain to be confirmed before public distribution.
