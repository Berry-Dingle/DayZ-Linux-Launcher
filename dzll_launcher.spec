Name:           dzll_launcher
Version:        0.3.1
Release:        0.beta%{?dist}
Summary:        DZLL is a native Linux launcher for DayZ with Steam Client Workshop mod handling and SteamCMD as an advanced fallback

License:        LicenseRef-DZLL-Community-Source-1.0
URL:            https://dzllauncher.uk/
Source0:        %{name}-0.3.1b0.tar.gz

BuildArch:      noarch

BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  python3-build

# BEGIN AUTO-DEPS
Requires:       python3-requests
Requires:       python3-gobject
Requires:       gtk4
Requires:       gdk-pixbuf2
Requires:       graphene
# END AUTO-DEPS

%description
DZLL is a native Linux launcher for DayZ with Steam Client Workshop mod
handling and SteamCMD as an advanced fallback. Native Steam is required;
Flatpak Steam is unsupported.

%prep
%autosetup -n %{name}-0.3.1b0

%build
%pyproject_wheel

%install
%pyproject_install

install -Dm644 com.bdingle.dzll.desktop %{buildroot}%{_datadir}/applications/com.bdingle.dzll.desktop
install -Dm644 src/dzll_launcher/images/com.bdingle.dzll.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/com.bdingle.dzll.png
install -Dm644 LICENSE %{buildroot}%{_licensedir}/%{name}/LICENSE
install -Dm644 src/dzll_launcher/a2s/LICENSE %{buildroot}%{_licensedir}/%{name}/python-a2s-LICENSE
install -Dm644 src/dzll_launcher/vendor/pypresence/LICENSE %{buildroot}%{_licensedir}/%{name}/pypresence-LICENSE

%files
%license %{_licensedir}/%{name}/LICENSE
%license %{_licensedir}/%{name}/python-a2s-LICENSE
%license %{_licensedir}/%{name}/pypresence-LICENSE
%{_bindir}/dzll_launcher
%{_datadir}/applications/com.bdingle.dzll.desktop
%{_datadir}/icons/hicolor/256x256/apps/com.bdingle.dzll.png
%{python3_sitelib}/dzll_launcher
%{python3_sitelib}/dzll_launcher-*.dist-info

%check
PYTHONPATH=%{buildroot}%{python3_sitelib} %{python3} -c "import dzll_launcher"

%changelog
* Tue Jul 14 2026 Gareth Brown - 0.3.1-0.beta
- v0.3.1 beta metadata refresh

* Thu Jul 09 2026 Gareth Brown - 0.3.0-0.beta
- v0.3.0 beta metadata refresh

* Tue Mar 31 2026 Gareth Brown - 0.2.0-0.beta
- Initial build
