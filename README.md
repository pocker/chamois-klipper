# Chamois Klipper Plugin 🦎

## Overview
The Chamois Klipper plugin is an extra module designed to enhance the functionality of the Klipper firmware by integrating with the Chamois Multi-Material Unit (MMU). This plugin facilitates the management of tool changes and filament loading/unloading processes. 🛠️🎛️

> ℹ️ The plugin will automatically register `T0`, `T1`, `T2`, ... `Tn` commands and orchestrate the tool change process for you.

## Installation Instructions 🚀

1. **Prerequisites** ⚙️
   - Ensure that Python 3 is installed on your system. You can check this by running:
     ```bash
     python3 --version
     ```
   - Make sure Klipper is installed. If you haven't installed Klipper yet, please follow the official Klipper installation guide.

2. **Clone the Repository** 📥
   - Clone the Chamois Klipper plugin repository to your local machine:
     ```bash
     git clone https://github.com/yourusername/chamois-klipper.git
     cd chamois-klipper
     ```

3. **Run the Installation Script** 🖥️
   - Execute the installation script to set up the plugin:
     ```bash
     ./install.sh
     ```

4. **Configuration** 📝
   - After installation, you may need to configure the plugin settings in your Klipper configuration file. Refer to the documentation for details on the available configuration options.
   - **Add the following to your `printer.cfg`:**
     ```ini
     [chamois]
     tcp_address: <MMU IP>
     ```
     Replace `<MMU IP>` with the actual IP address of your Chamois MMU device.

## Macros 🧩

The following macros are used to control and customize the Chamois MMU operation during its life cycle. Define these in your Klipper configuration to match your printer and workflow:

- **CHAMOIS_PARK**: 🅿️ Move the toolhead into the park position.
- **CHAMOIS_BEFORE_UNLOAD**: ⏪ Called before the MMU pulls out the filament. Use this phase for tip forming and retraction.
- **CHAMOIS_ON_UNLOAD**: 🔄 While the MMU is retracting, this macro is called continuously. You can retract a little bit to prevent jams.
- **CHAMOIS_ON_LOAD**: 🔄 Similarly to CHAMOIS_ON_UNLOAD, this will be called continuously during MMU load to ensure the filament is caught by the extruder.
- **CHAMOIS_AFTER_LOAD**: ✅ Called after the MMU has loaded the filament. This should ensure the filament is fully loaded to the hotend.

> ℹ️ These macros will be called automatically by the plugin at the appropriate points in the MMU life cycle if they are defined.

## Example Macro Configuration 📝

Below is an example of how you can define the recommended macros in your Klipper configuration:

```ini
[gcode_macro CHAMOIS_PARK]
gcode:
  G1 X233 Y233 F6000

[gcode_macro CHAMOIS_BEFORE_UNLOAD]
gcode:
  M83
  G1 E-0.5 F1800
  G1 E0.2 F300
  G1 E-5 F3600
  G1 E-10 F600
  G1 E-5 F300
  G1 E-100 F6000

[gcode_macro CHAMOIS_ON_UNLOAD]
gcode:
  M83
  G1 E-5 F6000

[gcode_macro CHAMOIS_ON_LOAD]
gcode:
  M83
  G1 E5 F6000

[gcode_macro CHAMOIS_AFTER_LOAD]
gcode:
  M83
  G1 E120 F6000
```

> 🎨 Adjust these macros as needed for your printer and filament requirements.

## Life Cycle 🔄

The Chamois MMU operates in the following life cycle:

- **HOME** 🏠 → **SELECT** 🎯 → **DISABLE** 🚫
- **HALT**: ♻️ Restarts the MMU. Ensure all filaments are in the start position before proceeding.

**SELECT Sequence:**

1. `PARK` 🅿️ — Park the current filament.
2. If filament is loaded:
    - `UNLOAD` ⏪ — Unload the filament (repeats until complete).
3. `TOOL SELECT` 🔢 — Select the desired tool/filament.
4. `LOAD` ➡️ — Load the new filament (repeats until complete).
5. `RELEASE` 🔓 — Disengage the extruder from the loaded filament.

**DISABLE Sequence:**

- If filament is loaded:
    - `UNLOAD` ⏪ — Unload the filament.
- Turn off motors. 📴

## Troubleshooting 🛠️
If you encounter any issues during installation or usage, please check the following:
- Ensure that the paths in the `install.sh` script are correct.
- Verify that the Klipper service is running properly.
- Check the logs for any error messages related to the Chamois plugin.

## License 📄
This project is licensed under the GNU GPLv3 license. Please see the LICENSE file for more details.