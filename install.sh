#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PadSquire installer, for Linux and macOS: tools for GameSir controllers.
#
#   curl -fsSL https://padsquire.com/install.sh | sh
#
# Or download it, read it, then run it:  sh install.sh
# To remove PadSquire:  sh install.sh --uninstall   (or: curl ... | sh -s -- --uninstall)
#
# What it does, and nothing else:
#   1. Makes a private Python environment for PadSquire in ~/.local/share/padsquire/venv and
#      installs PadSquire into it (from PyPI, or from GitHub if PyPI doesn't have it yet).
#   2. Links the padsquire command into ~/.local/bin.
#   3. Adds PadSquire to your app menu (~/.local/share/applications), or on macOS makes a small
#      PadSquire app in ~/Applications.
#   4. Linux only, and only if you say yes: installs the udev rules that let you use the controller
#      without sudo. This is the one step that needs your password. macOS needs no such step.
# Running it again updates PadSquire. Your settings live on the controller; firmware downloads and
# flash backups in ~/.local/share/padsquire are never touched.
#
# No warranty: you use PadSquire at your own risk. Settings can be changed back, but a firmware
# update can leave a controller or receiver unusable, and PadSquire's authors aren't responsible
# for a controller that is damaged or stops working. The GNU AGPL (sections 15 and 16 of the
# LICENSE file) says the same in full.

set -eu

REPO="https://github.com/Magniswerfer/padsquire"
# The version this installer installs. Each release writes its own number here (release.yml), so
# the installer from a release installs exactly that release, even in the minutes after it comes
# out, when pip and PyPI can still hand out the one before. Empty: the newest on PyPI.
VERSION=""
if [ -n "$VERSION" ]; then
    WANT="padsquire[gui]==$VERSION"
    SOURCE_ARCHIVE="$REPO/archive/refs/tags/v$VERSION.tar.gz"
else
    WANT="padsquire[gui]"
    SOURCE_ARCHIVE="$REPO/archive/refs/heads/main.tar.gz"
fi
SOURCE="${PADSQUIRE_SOURCE:-}"      # optional: a pip requirement or path to install instead
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/padsquire"
VENV="$DATA/venv"
WINDOW="${XDG_CACHE_HOME:-$HOME/.cache}/padsquire/window"   # the app window's own browser profile
BIN="$HOME/.local/bin"
RULES="/etc/udev/rules.d/70-gamesir.rules"
OS=$(uname -s)

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\nPadSquire installer: %s\n' "$*" >&2; exit 1; }

# Questions go to the terminal even when the script itself comes through a pipe (curl ... | sh).
ask() {
    if (: < /dev/tty) 2>/dev/null; then          # there is a terminal to ask
        printf '%s ' "$1" > /dev/tty
        read -r answer < /dev/tty || answer=""
    else
        return 1                     # nobody to ask: do only the steps that need no answer
    fi
    case "$answer" in [Yy]*) return 0 ;; [Nn]*) return 1 ;; "") [ "$2" = yes ] ;; *) return 1 ;; esac
}

install_hint() {   # the command that installs a distribution package, for messages
    if [ "$OS" = Darwin ]; then
        if [ "$1" = python3 ]; then say "brew install python (https://brew.sh), or the installer from https://www.python.org"
        else say "brew install $1 (https://brew.sh)"; fi
    elif command -v dnf >/dev/null 2>&1; then say "sudo dnf install $1"
    elif command -v apt >/dev/null 2>&1; then say "sudo apt install $1"
    elif command -v pacman >/dev/null 2>&1; then say "sudo pacman -S $1"
    elif command -v zypper >/dev/null 2>&1; then say "sudo zypper install $1"
    else say "your package manager's $1 package"; fi
}

find_python() {
    # On macOS also where Homebrew and python.org put it, in case the shell's PATH doesn't say;
    # the system's own python3 there is too old.
    for py in python3.14 python3.13 python3.12 python3.11 python3 \
              /opt/homebrew/bin/python3 /usr/local/bin/python3 \
              /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
        if command -v "$py" >/dev/null 2>&1 &&
           "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            command -v "$py"; return 0
        fi
    done
    return 1
}

install_app() {
    step "1. Installing PadSquire into its own Python environment"
    say "   Folder: $VENV (only PadSquire and its libraries go here; nothing system-wide)."
    PY=$(find_python) || fail "PadSquire needs Python 3.11 or newer. Install it with: $(install_hint python3)"
    if [ ! -x "$VENV/bin/python" ]; then
        mkdir -p "$DATA"
        "$PY" -m venv "$VENV" 2>/dev/null ||
            fail "couldn't make a Python environment. On Debian and Ubuntu that needs: $(install_hint python3-venv)"
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    if [ -n "$SOURCE" ]; then
        "$VENV/bin/python" -m pip install --quiet --upgrade "$SOURCE" || fail "pip couldn't install $SOURCE."
        say "   Installed from $SOURCE."
    # --no-cache-dir: pip keeps PyPI's list of versions for a few minutes, which can hide a new one
    elif "$VENV/bin/python" -m pip install --quiet --upgrade --no-cache-dir "$WANT" 2>/dev/null; then
        say "   Installed from PyPI."
    else
        say "   Not on PyPI yet: installing it from GitHub instead."
        "$VENV/bin/python" -m pip install --quiet --upgrade "padsquire[gui] @ $SOURCE_ARCHIVE" ||
            fail "pip couldn't install PadSquire (see the message above)."
    fi
    say "   $("$VENV/bin/padsquire" --version 2>/dev/null | sed 's/^/Version /')"
}

link_command() {
    step "2. Making the padsquire command available"
    mkdir -p "$BIN"
    ln -sf "$VENV/bin/padsquire" "$BIN/padsquire"
    say "   Linked $BIN/padsquire."
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) say "   $BIN isn't on your PATH yet, so typing just 'padsquire' won't work in a terminal."
           if [ "$OS" = Darwin ]; then
               say "   The PadSquire app works anyway. To fix the terminal, add this line to ~/.zprofile:"
           else
               say "   The app menu entry works anyway. To fix the terminal, add this line to ~/.profile:"
           fi
           say "     export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
}

add_menu_entry() {
    if [ "$OS" = Darwin ]; then
        step "3. Adding the PadSquire app to ~/Applications (Launchpad and Spotlight find it there)"
    else
        step "3. Adding PadSquire to your app menu"
    fi
    "$VENV/bin/padsquire" menu | sed 's/^/   /'
}

install_rules() {
    if [ "$OS" = Darwin ]; then
        step "4. Permission to use the controller"
        say "   Nothing to do: macOS's own driver lets PadSquire change settings without sudo."
        return 0
    fi
    step "4. Permission to use the controller (optional, needs your password)"
    if [ -f "$RULES" ] && cmp -s "$RULES" "$("$VENV/bin/padsquire" udev-rules --path)"; then
        say "   Already installed ($RULES)."; return 0
    fi
    say "   Linux only lets the administrator talk to a USB controller's settings until a udev rule"
    say "   allows it. PadSquire's rule lets whoever is logged in at this computer use GameSir"
    say "   devices (USB vendor 3537) and their update mode (JieLi, 4c4a). Nothing else."
    say "   It runs, as the administrator (sudo asks for your password):"
    say "     sudo install -m 644 $("$VENV/bin/padsquire" udev-rules --path) $RULES"
    say "     sudo udevadm control --reload && sudo udevadm trigger"
    say "   which copies the rule into /etc/udev/rules.d and applies it to devices already plugged in."
    say "   To undo it later: sudo rm $RULES"
    if ask "   Install the rule now? [y/N]" no; then
        sudo install -m 644 "$("$VENV/bin/padsquire" udev-rules --path)" "$RULES"
        sudo udevadm control --reload && sudo udevadm trigger
        say "   Done. Unplug the controller and plug it back in."
    else
        say "   Skipped. The app's Setup and help page shows the same steps when you need them."
    fi
}

uninstall() {
    step "Removing PadSquire"
    [ -x "$VENV/bin/padsquire" ] && "$VENV/bin/padsquire" menu --remove | sed 's/^/   /' || true
    [ -L "$BIN/padsquire" ] && rm -f "$BIN/padsquire" && say "   Removed $BIN/padsquire."
    [ -d "$VENV" ] && rm -rf "$VENV" && say "   Removed $VENV."
    [ -d "$WINDOW" ] && rm -rf "$WINDOW" && say "   Removed $WINDOW (the app window's browser profile)."
    for dir in "$HOME"/.var/app/*/cache/padsquire; do     # the same, for a Flatpak browser
        [ -d "$dir" ] && rm -rf "$dir" && say "   Removed $dir (the app window's browser profile)."
    done
    if [ "$OS" = Linux ] && [ -f "$RULES" ]; then
        if ask "   Also remove the udev rule ($RULES, needs your password)? [y/N]" no; then
            sudo rm -f "$RULES" && sudo udevadm control --reload && say "   Removed $RULES."
        fi
    fi
    say ""
    say "Kept: your firmware downloads and flash backups in $DATA."
    say "The backups hold your controller's firmware, pairing and calibration; delete them yourself"
    say "only if you're sure you don't need them."
}

main() {
    case "$OS" in Linux|Darwin) ;; *) fail "PadSquire is for Linux and macOS." ;; esac
    [ "$(id -u)" -ne 0 ] || fail "run this as yourself, not as root or with sudo. It asks for your password only for the one step that needs it."
    if [ "${1:-}" = "--uninstall" ]; then uninstall; return; fi
    say "PadSquire: tools for GameSir controllers. Unofficial and open source ($REPO)."
    say ""
    say "No warranty: you use PadSquire at your own risk. Settings can be changed back, but a"
    say "firmware update can leave a controller or receiver unusable. PadSquire makes a full backup"
    say "before any firmware write, but its authors aren't responsible for a controller that is"
    say "damaged or stops working. The licence (GNU AGPL, sections 15 and 16) says the same in full."
    install_app
    link_command
    add_menu_entry
    install_rules
    step "Ready"
    if [ "$OS" = Darwin ]; then
        say "   Open PadSquire from Launchpad or Spotlight, or run: padsquire gui"
        say "   Firmware updates ask for your password in macOS's own dialog; the app explains why."
        say "   Steam games work in green mode (Steam has its own driver); for others, switch to blue"
        say "   (hold Menu + View)."
    else
        say "   Open PadSquire from your app menu, or run: padsquire gui"
        say "   Firmware updates also need 7-Zip and the sg kernel module; the app's Setup and help page"
        say "   explains both when you get there."
    fi
    if ask "   Open PadSquire now? [Y/n]" yes; then
        nohup "$VENV/bin/padsquire" gui >/dev/null 2>&1 &
    fi
}

main "$@"
