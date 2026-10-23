#!/usr/bin/env bash

set -u
cd -- "$(dirname -- "$0")" || exit 1

ask_yes_no() {
    local prompt="$1"
    local default="${2:-no}"
    local choices="[y/N]"
    local answer
    if [[ "$default" == "yes" ]]; then
        choices="[Y/n]"
    fi
    if [[ ! -t 0 || ! -t 1 ]]; then
        echo "No terminal is available; skipping: $prompt"
        return 1
    fi
    read -r -p "$prompt $choices " answer || answer=""
    if [[ -z "$answer" ]]; then
        [[ "$default" == "yes" ]]
    else
        [[ "$answer" == [yY] || "$answer" == [yY][eE][sS] ]]
    fi
}

install_python() {
    if [[ "$(uname -s)" == "Darwin" ]]; then
        if command -v brew >/dev/null 2>&1; then
            brew install python
        else
            echo "Install Homebrew from https://brew.sh, then run this file again."
            return 1
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-pip
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed python python-pip
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y python3 python3-pip python3-virtualenv
    elif command -v apk >/dev/null 2>&1; then
        sudo apk add python3 py3-pip py3-virtualenv
    else
        echo "No supported package manager was found."
        echo "Install Python 3.9 or newer, then run this file again."
        return 1
    fi
}

install_pipx() {
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y pipx
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y pipx
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y pipx
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed python-pipx
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y python3-pipx
    elif command -v apk >/dev/null 2>&1; then
        sudo apk add pipx
    else
        echo "Your package manager does not have an automatic Pipx setup here."
        echo "You can continue with Python's built-in virtual environment instead."
        return 1
    fi
}

install_venv_support() {
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3-venv
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y python3
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed python
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y python3-virtualenv
    elif command -v apk >/dev/null 2>&1; then
        sudo apk add py3-virtualenv
    else
        echo "Your package manager does not have an automatic venv setup here."
        return 1
    fi
}

install_imagemagick() {
    if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
        brew install imagemagick
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y imagemagick
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y ImageMagick
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y ImageMagick
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed imagemagick
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y ImageMagick
    elif command -v apk >/dev/null 2>&1; then
        sudo apk add imagemagick
    else
        echo "No supported package manager was found for ImageMagick."
        return 1
    fi
}

imagemagick_available() {
    local candidate
    local version
    for candidate in magick convert; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && version=$("$candidate" -version 2>&1) \
            && [[ "$version" == *ImageMagick* ]]; then
            return 0
        fi
    done
    return 1
}

create_venv() {
    local output

    if [[ "$(uname -s)" == "Linux" ]] && command -v pipx >/dev/null 2>&1; then
        if pipx run --spec virtualenv virtualenv scripts/.venv; then
            return 0
        fi
        echo "Pipx could not create the environment. Trying Python's built-in venv."
    fi

    if output=$(python3 -m venv scripts/.venv 2>&1); then
        [[ -z "$output" ]] || printf '%s\n' "$output"
        return 0
    fi
    printf '%s\n' "$output"

    if [[ "$output" == *"ensurepip is not available"* ]] \
        && ask_yes_no "Install Python's venv support now?" yes; then
        if ! install_venv_support; then
            return 1
        fi
        if command -v virtualenv >/dev/null 2>&1; then
            virtualenv scripts/.venv
            return $?
        fi
        if output=$(python3 -m venv scripts/.venv 2>&1); then
            [[ -z "$output" ]] || printf '%s\n' "$output"
            return 0
        fi
        printf '%s\n' "$output"
    fi
    return 1
}

echo
echo "CarUtil setup"
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 was not found. CarUtil needs Python 3.9 or newer."
    if ! ask_yes_no "Install Python using your system package manager now?"; then
        echo "Install Python 3.9 or newer, then run carutil.sh again."
        read -r -p "Press Enter to close..." _
        exit 1
    fi
    if ! install_python; then
        read -r -p "Press Enter to close..." _
        exit 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Python was installed, but this terminal cannot find it yet."
        echo "Close this window, open a new terminal, and run carutil.sh again."
        read -r -p "Press Enter to close..." _
        exit 1
    fi
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "CarUtil needs Python 3.9 or newer. Update Python, then try again."
    read -r -p "Press Enter to close..." _
    exit 1
fi

if [[ "${CARUTIL_SKIP_UPDATE:-0}" != "1" && -f scripts/update.py ]]; then
    python3 scripts/update.py --auto
fi

if ! imagemagick_available; then
    echo "ImageMagick is optional; it converts the bundled brand logos."
    if ask_yes_no "Install ImageMagick now?" yes; then
        if install_imagemagick && imagemagick_available; then
            echo "ImageMagick is ready."
        else
            echo "ImageMagick could not be installed or was not found afterward."
            echo "CarUtil will continue without replacing the brand logo."
        fi
    else
        echo "Continuing without ImageMagick; the mod's logo or a placeholder will be used."
    fi
fi

if [[ "$(uname -s)" == "Linux" ]] && ! command -v pipx >/dev/null 2>&1; then
    echo "Pipx can create CarUtil's private Python environment."
    if ask_yes_no "Install Pipx with your system package manager?"; then
        if ! install_pipx || ! command -v pipx >/dev/null 2>&1; then
            echo "Pipx could not be set up. CarUtil will try Python's built-in venv."
        fi
    fi
fi

if [[ ! -x "scripts/.venv/bin/python" ]]; then
    echo "Creating CarUtil's private Python environment..."
    if ! create_venv || [[ ! -x "scripts/.venv/bin/python" ]]; then
        echo
        echo "Could not create the private environment."
        echo "Install your distribution's Python venv support, then run carutil.sh again."
        read -r -p "Press Enter to close..." _
        exit 1
    fi
fi

if ! scripts/.venv/bin/python -c 'import questionary, rich' >/dev/null 2>&1; then
    echo "CarUtil's enhanced menu packages are not installed."
    if ask_yes_no "Install them now?" yes; then
        if ! scripts/.venv/bin/python -m pip install -r scripts/requirements.txt; then
            echo
            echo "Package setup failed. Check your internet connection and try again."
            read -r -p "Press Enter to close..." _
            exit 1
        fi
    else
        echo "Continuing with the built-in numbered menus."
    fi
fi

echo
echo "Starting CarUtil..."
echo
scripts/.venv/bin/python scripts/carutil.py
status=$?
echo
read -r -p "Press Enter to close..." _
exit "$status"
