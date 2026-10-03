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
     git clone https://github.com/pocker/chamois-klipper.git
     cd chamois-klipper
     ```

3. **Run the Installation Script** 🖥️
   - Execute the installation script to set up the plugin:
     ```bash
     ./install.sh
     ```

4. **Configuration** 📝
   - Add the following to your `printer.cfg`:
     ```ini
     [include chamois_macros.cfg]

     [chamois]
     tcp_address: <MMU IP>
     # Calibrate these for your printer (see "Calibration" below)
     toolhead_unload_distance: 80
     toolhead_load_distance: 60
     ```
     Replace `<MMU IP>` with the actual IP address of your Chamois MMU device.
   - The plugin needs the matching `chamois-mmu` firmware (sync load/unload support). With old firmware
     the first tool change stops with "MMU firmware is too old".

## How a tool change works 🔄

```
T<n>
 ├─ CHAMOIS_PARK                 move the toolhead out of the way (optional macro)
 ├─ unload (if something is loaded)
 │   ├─ CHAMOIS_FORM_TIP         ramming, nozzle separation, cooling moves (chamois_macros.cfg)
 │   ├─ sync unload              extruder + MMU pull toolhead_unload_distance together
 │   └─ MMU fast unload          MMU pulls the filament back to its park position
 ├─ load
 │   ├─ MMU select + fast load   stops load_sync_margin before the extruder gears
 │   ├─ sync load                MMU + extruder push load_sync_distance together
 │   ├─ MMU release
 │   ├─ extruder                 pushes toolhead_load_distance to the nozzle
 │   └─ CHAMOIS_AFTER_LOAD       purge / prime (optional macro)
 └─ restore G-code state and move back to where the toolhead was
```

During the sync moves the MMU is started exactly when Klipper's extruder starts moving, so the two
drives never fight each other. Long extruder moves are split to respect `max_extrude_only_distance`.
If anything fails during a print, the print is paused (`pause_on_error`) and the toolhead stays parked.

## Configuration reference ⚙️

| Option | Default | Description |
|---|---|---|
| `tcp_address` | – | IP address of the MMU (required) |
| `tcp_port` | 5433 | TCP port of the MMU |
| `number_of_toolhead` | 4 | Number of `T<n>` commands to register |
| `toolhead_unload_distance` | 80 | Extruder + MMU retract after tip forming. Distance from the formed tip to the extruder gears **plus ~10mm** |
| `toolhead_load_distance` | 0 | Extruder-only move after the sync load: extruder gears → nozzle minus (`load_sync_distance` − `load_sync_margin`) |
| `toolhead_load_speed` | 10 | Speed (mm/s) of the extruder-only load move |
| `load_sync_margin` | 20 | The fast MMU load stops this many mm before the extruder gears |
| `load_sync_distance` | 50 | Distance moved by MMU and extruder together during load. Must be larger than `load_sync_margin` |
| `sync_speed` | 15 | Speed (mm/s) of the synchronized moves |
| `sync_unload` | True | Let the MMU pull together with the extruder during the toolhead unload |
| `sync_latency` | 0.05 | Seconds the MMU command is sent ahead of the extruder move (network latency) |
| `toolhead_sensor` | – | Name of a `filament_switch_sensor` in the toolhead, used to verify load/unload |
| `park_macro` | CHAMOIS_PARK | Macro run before the unload |
| `form_tip_macro` | CHAMOIS_FORM_TIP | Tip forming macro |
| `after_load_macro` | CHAMOIS_AFTER_LOAD | Macro run after the load (purge) |
| `restore_position` | True | Move back to the pre-park position after the tool change |
| `restore_speed` | 100 | Speed (mm/s) of that move |
| `pause_on_error` | True | Pause the print instead of aborting it when a tool change fails |
| `read_timeout` | 60 | Seconds to wait for an MMU command to finish |

## Commands 🧩

| Command | Description |
|---|---|
| `T0` … `Tn` | Tool change |
| `CHAMOIS_LOAD TOOL=<n>` | Same as `T<n>` |
| `CHAMOIS_UNLOAD` | Form the tip and unload the current filament to the MMU |
| `CHAMOIS_HOME` | Home the selector |
| `CHAMOIS_DISABLE` | Unload (if loaded) and turn the MMU motors off |
| `CHAMOIS_HALT` | Restart the MMU. Make sure all filaments are in the park position first |
| `CHAMOIS_STATUS` | Print firmware version and state |
| `CHAMOIS_MOVE DISTANCE=<mm> [SPEED=<mm/s>]` | Move the selected filament with the MMU only (calibration / recovery) |

The state is available to macros as `printer.chamois.loaded`, `printer.chamois.selected_index`, …

## Macros 📝

`chamois_macros.cfg` contains `CHAMOIS_FORM_TIP`, a PrusaSlicer/OrcaSlicer style tip forming
sequence. Its variables use the same names as the slicer's "single extruder multi material"
settings, so values from a working slicer profile can be copied. Tune it with
`SET_GCODE_VARIABLE MACRO=CHAMOIS_FORM_TIP VARIABLE=<name> VALUE=<value>` and make the result
permanent in the file. Good tips are thin, pointed and without strings or blobs; if you see a
blob, raise `cooling_moves` or lower `toolchange_temp`, if you see strings, raise `unloading_speed_start`.

> ⚠️ If your slicer already does tip forming / ramming on a wipe tower, disable one of them —
> set `form_tip_macro:` to an empty macro or turn off the slicer's ramming.

Optional macros you can define:

```ini
[gcode_macro CHAMOIS_PARK]
gcode:
  G91
  G1 Z2 F600          # small z-hop, undone by the position restore
  G90
  G1 X233 Y233 F6000

[gcode_macro CHAMOIS_AFTER_LOAD]
gcode:
  M83
  G1 E30 F300         # purge
```

`CHAMOIS_BEFORE_UNLOAD`, `CHAMOIS_ON_UNLOAD` and `CHAMOIS_ON_LOAD` from older versions are no longer
called — remove them (the extruder retraction they did is now `toolhead_unload_distance`, the
continuous extruder moves are replaced by the synchronized moves).

## Calibration 📐

1. **MMU hotend distance** (firmware config `hotend_distances`, per tool): distance the MMU pushes
   from the park position until the filament tip *touches the extruder gears*.
   Use `CHAMOIS_MOVE DISTANCE=...` with the selected filament to find it.
2. **`toolhead_unload_distance`**: after `CHAMOIS_FORM_TIP` the tip sits at
   `cooling_tube_position + cooling_tube_length/2` (or `parking_position`) above the nozzle. Measure
   from there to the extruder gears and add ~10mm.
3. **`toolhead_load_distance`**: distance from the extruder gears to the nozzle minus
   (`load_sync_distance` − `load_sync_margin`). A few mm too much is fine, it only purges.
4. Run `CHAMOIS_UNLOAD` and `T<n>` with the toolhead hot and check the tip shape.

## Troubleshooting 🛠️
If you encounter any issues during installation or usage, please check the following:
- Ensure that the paths in the `install.sh` script are correct.
- Verify that the Klipper service is running properly.
- Check the logs for any error messages related to the Chamois plugin.

## License 📄
This project is licensed under the GNU GPLv3 license. Please see the LICENSE file for more details.