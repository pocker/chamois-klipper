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

# Copy the plugin to the Klipper directory
cp chamois.py "$KLIPPER_PATH/extras/"

# Restart Klipper service
sudo service klipper restart

echo "Chamois plugin installed successfully."