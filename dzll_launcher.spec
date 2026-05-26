Name:           dzll_launcher
Version:        0.2.0
Release:        0.beta%{?dist}
Summary:        DZLL is a launcher application for DayZ using SteamCMD to handle mods for modded servers

License:        MIT
URL:            https://dzllauncher.uk/
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  python3-build
BuildRequires:  python3-pytest

# BEGIN AUTO-DEPS
Requires:       python3-requests
Requires:       python3-pypresence
# END AUTO-DEPS

%description
DZLL is a launcher application for DayZ using SteamCMD to handle mods for modded servers.

%prep
%autosetup -n %{name}-%{version}

%build
%pyproject_wheel

%install
%pyproject_install

install -Dm644 dzll_launcher.desktop %{buildroot}%{_datadir}/applications/dzll_launcher.desktop
install -Dm644 src/dzll_launcher/images/icon.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/dzll_launcher.png
install -Dm644 LICENSE %{buildroot}%{_licensedir}/%{name}/LICENSE

%files
%license %{_licensedir}/%{name}/LICENSE
%{_bindir}/dzll_launcher
%{_datadir}/applications/dzll_launcher.desktop
%{_datadir}/icons/hicolor/256x256/apps/dzll_launcher.png
%{python3_sitelib}/dzll_launcher/
%{python3_sitelib}/dzll_launcher/*
%{python3_sitelib}/dzll_launcher-*.dist-info

%check
%pytest

%changelog
* Tue Mar 31 2026 Gareth Brown - 0.2.0-0.beta
- Initial build
