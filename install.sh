#!/bin/bash

# Set this to terminate on error.
set -e

KLIPPER_PATH="${HOME}/klipper"

# Check for Python installation
if ! command -v python3 &> /dev/null
then
    echo "Python3 is not installed. Please install Python3."
    exit 1
fi

# Check for Klipper installation
if [ ! -d "$KLIPPER_PATH" ]; then
    echo "Klipper is not installed. Please install Klipper."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${HOME}/printer_data/config"

# Copy the plugin to the Klipper directory
cp "$SCRIPT_DIR/chamois.py" "$KLIPPER_PATH/klippy/extras/"

# Install the macros once, so local tuning is not overwritten on update
if [ -d "$CONFIG_PATH" ] && [ ! -f "$CONFIG_PATH/chamois_macros.cfg" ]; then
    cp "$SCRIPT_DIR/chamois_macros.cfg" "$CONFIG_PATH/"
    echo "Installed chamois_macros.cfg to $CONFIG_PATH"
fi

# Restart Klipper service
sudo service klipper restart

echo "Chamois plugin installed successfully."