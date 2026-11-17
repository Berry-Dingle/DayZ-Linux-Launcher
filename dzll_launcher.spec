Name:           dzll_launcher
Version:        0.5.0
Release:        0.beta%{?dist}
Summary:        DZLL is a native Linux launcher for DayZ with Steam Client Workshop/UGC mod preparation

License:        LicenseRef-DZLL-Community-Source-1.0 AND LicenseRef-Valve-Steamworks-SDK-Access-Agreement
URL:            https://dzllauncher.uk/
Source0:        %{name}-0.5.0b0.tar.gz

BuildArch:      x86_64
ExclusiveArch:  x86_64
%global debug_package %{nil}
# Preserve the official redistributable byte-for-byte; RPM's default BRP strip
# otherwise changes its pinned SHA-256.
%global __strip /bin/true
# This package-private copy must not satisfy a system libsteam_api.so dependency.
%global __provides_exclude_from ^%{python3_sitelib}/dzll_launcher/native/linux_x86_64/libsteam_api[.]so$

BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  python3-build

# BEGIN AUTO-DEPS
Requires:       python3-gobject
Requires:       gtk4
Requires:       gdk-pixbuf2
Requires:       graphene
# END AUTO-DEPS
# Fedora 45 builds require GTK 4.24 or newer; Fedora 44 keeps its GTK 4.22 series.
%if 0%{?fedora} >= 45
Requires:       gtk4 >= 4.24
%endif

%description
DZLL is a native Linux launcher for DayZ with Steam Client Workshop/UGC mod
preparation. Native Steam is required;
Flatpak Steam is unsupported.

%prep
%autosetup -n %{name}-0.5.0b0

%build
%pyproject_wheel

%install
%pyproject_install

install -Dm644 com.bdingle.dzll.desktop %{buildroot}%{_datadir}/applications/com.bdingle.dzll.desktop
install -Dm644 src/dzll_launcher/images/com.bdingle.dzll.png %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/com.bdingle.dzll.png
install -Dm644 LICENSE %{buildroot}%{_licensedir}/%{name}/LICENSE
install -Dm644 src/dzll_launcher/a2s/LICENSE %{buildroot}%{_licensedir}/%{name}/python-a2s-LICENSE
install -Dm644 src/dzll_launcher/vendor/pypresence/LICENSE %{buildroot}%{_licensedir}/%{name}/pypresence-LICENSE
install -Dm644 src/dzll_launcher/native/linux_x86_64/NOTICE %{buildroot}%{_licensedir}/%{name}/steamworks-sdk-1.65-NOTICE

%files
%license %{_licensedir}/%{name}/LICENSE
%license %{_licensedir}/%{name}/python-a2s-LICENSE
%license %{_licensedir}/%{name}/pypresence-LICENSE
%license %{_licensedir}/%{name}/steamworks-sdk-1.65-NOTICE
%{_bindir}/dzll_launcher
%{_datadir}/applications/com.bdingle.dzll.desktop
%{_datadir}/icons/hicolor/512x512/apps/com.bdingle.dzll.png
%{python3_sitelib}/dzll_launcher
%{python3_sitelib}/dzll_launcher-*.dist-info

%check
PYTHONPATH=%{buildroot}%{python3_sitelib} %{python3} -c "import dzll_launcher"

%changelog
* Tue Nov 17 2026 Berry Dingle <dzll-launcher@tutamail.com> - 0.5.0-0.beta
- Refresh the rows actually visible in the server browser; add a status bar and news ticker
- Server Companion: verified restart cycles, time-change handling, learning data deletion
- Move Server Companion persistence and learning rebuilds off the GTK thread
- Require GTK 4.24 or newer on Fedora 45

* Wed Oct 14 2026 Gareth Brown - 0.4.2-0.beta
- Vendor Steamworks SDK 1.65 for native Steam UGC on x86-64
- Improve Steam UGC startup diagnostics and inventory ABI handling

* Fri Oct 09 2026 Gareth Brown - 0.4.1-0.beta
- Improve browser ordering, Companion presentation, and official-server UI

* Sat Aug 15 2026 Gareth Brown - 0.4.0-0.beta
- v0.4.0 beta metadata refresh

* Thu Jul 09 2026 Gareth Brown - 0.3.0-0.beta
- v0.3.0 beta metadata refresh

* Tue Mar 31 2026 Gareth Brown - 0.2.0-0.beta
- Initial build
