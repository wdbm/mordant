#!/usr/bin/env bash
# Copyright (C) 2026 William Breaden Madden
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail

usage(){
    printf '%s\n' \
        'Usage: ./install.sh --user [--prefix ABSOLUTE_DIRECTORY]' \
        '       sudo ./install.sh --system' \
        '' \
        'Installs the local Mordant sources without downloads.' \
        'See the installation section at README.md for the required system packages.'
}

fail(){
    printf 'mordant installer: %s\n' "$*" >&2
    exit 1
}

mode=''
custom_prefix=''
while (($#)); do
    case "$1" in
        --user|--system)
            [[ -z "${mode}" ]] || fail 'Choose exactly one of --user or --system.'
            mode="$1"
            shift
            ;;
        --prefix)
            (($# >= 2)) || fail '--prefix requires an absolute directory.'
            [[ -z "${custom_prefix}" ]] || fail 'Specify --prefix only once.'
            custom_prefix="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) fail "Unknown argument: $1" ;;
    esac
done
[[ -n "${mode}" ]] || { usage >&2; exit 2; }
if [[ "${mode}" == '--system' ]]; then
    [[ ${EUID} -eq 0 ]] || fail 'Run the system installation with sudo ./install.sh --system.'
    [[ -z "${custom_prefix}" ]] || fail '--prefix is supported only with --user.'
    install_prefix='/usr/local'
    environment_dir='/opt/mordant/venv'
else
    [[ ${EUID} -ne 0 ]] || fail 'Run --user as your regular user, without sudo.'
    install_prefix="${custom_prefix:-${HOME}/.local}"
    [[ "${install_prefix}" == /* ]] || fail '--prefix must be an absolute directory.'
    [[ "${install_prefix}" != *$'\n'* && "${install_prefix}" != *$'\r'* ]] || fail 'The installation path must not contain line breaks.'
    install_prefix="$(realpath -m -- "${install_prefix}")"
    [[ "${install_prefix}" != '/' ]] || fail 'The filesystem root cannot be an installation prefix.'
    environment_dir="${install_prefix}/share/mordant/venv"
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
app_id='io.github.wdbm.mordant'
launcher="${install_prefix}/bin/mordant"
applications_dir="${install_prefix}/share/applications"
services_dir="${install_prefix}/share/dbus-1/services"
icon_dir="${install_prefix}/share/icons/hicolor/scalable/apps"

[[ -f "${script_dir}/pyproject.toml" && -f "${script_dir}/README.md" ]] || fail 'Mordant sources are incomplete.'
for asset in "${app_id}.desktop.in" "${app_id}.service.in" "${app_id}.svg"; do
    [[ -f "${script_dir}/data/${asset}" ]] || fail "Missing data/${asset}."
done
for tool in check_dependencies.py render_install_assets.py; do
    [[ -f "${script_dir}/tools/${tool}" ]] || fail "Missing tools/${tool}."
done
for command in /usr/bin/python3 install ln desktop-file-validate update-desktop-database; do
    command -v "${command}" >/dev/null || fail "Missing ${command}; see README.md."
done
if [[ -e "${launcher}" || -L "${launcher}" ]]; then
    [[ -L "${launcher}" && "$(readlink -- "${launcher}")" == "${environment_dir}/bin/mordant" ]] || \
        fail "${launcher} already exists and is not this installer's launcher. Move it aside first."
fi
if [[ -e "${environment_dir}" && ! -f "${environment_dir}/.mordant-install" ]]; then
    fail "${environment_dir} already exists without a Mordant installer marker. Choose a different prefix or move it aside."
fi

# All dependency checks run before installation paths are created. -I ignores
# user `PYTHONPATH` and user-site packages when checking the distro interpreter.
/usr/bin/python3 -I "${script_dir}/tools/check_dependencies.py"

printf 'Installing Mordant into %s\n' "${environment_dir}"
install -d -- "${environment_dir}"
# The marker permits retrying if venv creation or package installation fails.
install -m644 /dev/null "${environment_dir}/.mordant-install"
/usr/bin/python3 -I -m venv --system-site-packages "${environment_dir}"
staging_dir="$(mktemp -d)"
trap 'rm -rf -- "${staging_dir}"' EXIT
# Build from a private copy so that stale or read-only build directories in the
# checkout do not interfere with the pip local wheel build.
install -d -- "${staging_dir}/mordant"
tar -C "${script_dir}" --exclude=build --exclude='*.egg-info' --exclude=__pycache__ \
    --exclude=.venv -cf - . | tar -C "${staging_dir}/mordant" -xf -
"${environment_dir}/bin/python" -I -m pip install \
    --disable-pip-version-check --no-cache-dir --no-index --no-build-isolation \
    --no-deps --upgrade --force-reinstall \
    "${staging_dir}/mordant"
"${environment_dir}/bin/python" -I -c \
    'from mordant_app import __VERSION__; from mordant_app.recent_directories import SaveSession; from mordant_app.transfers import TransferManager; print("Installed Mordant", __VERSION__)'

install -d -- "${install_prefix}/bin" "${applications_dir}" "${services_dir}" "${icon_dir}"
ln -sfn -- "${environment_dir}/bin/mordant" "${launcher}"
install -m644 -- "${script_dir}/data/${app_id}.svg" "${icon_dir}/${app_id}.svg"
/usr/bin/python3 -I "${script_dir}/tools/render_install_assets.py" \
    "${script_dir}/data" "${launcher}" "${applications_dir}" "${services_dir}" "${app_id}"
desktop-file-validate "${applications_dir}/${app_id}.desktop"
update-desktop-database "${applications_dir}"
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache --force --ignore-theme-index "${install_prefix}/share/icons/hicolor" || \
        printf '%s\n' 'The icon cache could not be refreshed; the SVG icon is installed.' >&2
fi
printf '\nInstalled. Launch with: %s\n' "${launcher}"
printf '%s\n' 'Configuration defaults to ~/.config/mordant (or ${XDG_CONFIG_HOME}/mordant).'
