# Chamois Klipper Plugin
#
# Talks to the Chamois MMU firmware over TCP and orchestrates tool changes:
#
#   park -> form tip -> sync unload (extruder + MMU) -> MMU fast unload
#        -> select -> MMU fast load -> sync load (MMU + extruder) -> release
#        -> extruder loads to the nozzle -> after-load macro (purge)
#
# All waiting happens through the Klipper reactor. Never call time.sleep()
# from a G-code handler: it freezes Klipper (heaters, MCU comms) and can
# shut the printer down.
import logging
import socket
import struct
import threading
import time
from concurrent.futures import Future
from queue import Queue, Empty


class ChamoisError(Exception):
    def __init__(self, msg, code=None):
        Exception.__init__(self, msg)
        self.code = code


class Chamois:

    _CMD_PING = 0x01
    _CMD_HALT = 0x02
    _CMD_GET_FIRMWARE_VERSION = 0x06
    _CMD_GET_STATUS = 0xA0
    _CMD_HOME = 0xA6
    _CMD_DISABLE = 0xA8
    _CMD_LOAD = 0xA9
    _CMD_UNLOAD = 0xAA
    _CMD_SELECT_TOOL = 0xAB
    _CMD_EXTRUDE = 0xAC
    _CMD_RETRACT = 0xAD
    _CMD_RELEASE = 0xAE
    _CMD_ENGAGE = 0xAF

    # Commands that may be safely re-sent if the response got lost
    _IDEMPOTENT = (_CMD_PING, _CMD_GET_STATUS, _CMD_GET_FIRMWARE_VERSION)

    _START_BYTE = 0xAA
    _RESPONSE_OK = 0x00
    _RESPONSE_NAMES = {
        0xA1: "invalid command payload",
        0xA2: "unknown command (is the MMU firmware up to date?)",
        0xA3: "command failed on the MMU (see MMU serial log)",
    }

    _NO_TOOL = 0xFF
    _STATE_NAME = "_chamois_toolchange"
    _LEGACY_MACROS = ("CHAMOIS_BEFORE_UNLOAD", "CHAMOIS_ON_UNLOAD", "CHAMOIS_ON_LOAD")

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.toolhead = None
        self.mcu = None

        # Connection
        self.tcp_address = config.get('tcp_address')
        self.tcp_port = config.getint('tcp_port', 5433)
        self.connect_timeout = config.getfloat('connect_timeout', 5.0, above=0.)
        self.read_timeout = config.getfloat('read_timeout', 60.0, above=0.)
        self.max_retries = config.getint('max_retries', 3, minval=1)
        self.mmu_keepalive = config.getfloat('mmu_keepalive', 10.0, above=0.)

        self.number_of_toolhead = config.getint('number_of_toolhead', 4, minval=1, maxval=20)

        # Macros
        self.park_macro = config.get('park_macro', 'CHAMOIS_PARK')
        self.form_tip_macro = config.get('form_tip_macro', 'CHAMOIS_FORM_TIP')
        self.after_load_macro = config.get('after_load_macro', 'CHAMOIS_AFTER_LOAD')

        # Filament path geometry, see README
        self.sync_speed = config.getfloat('sync_speed', 15.0, above=0.)
        self.load_sync_margin = config.getfloat('load_sync_margin', 20.0, minval=0.)
        self.load_sync_distance = config.getfloat('load_sync_distance', 50.0, above=0.)
        self.toolhead_load_distance = config.getfloat('toolhead_load_distance', 0., minval=0.)
        self.toolhead_load_speed = config.getfloat('toolhead_load_speed', 10.0, above=0.)
        self.toolhead_unload_distance = config.getfloat('toolhead_unload_distance', 80.0, minval=0.)
        self.sync_unload = config.getboolean('sync_unload', True)
        self.sync_latency = config.getfloat('sync_latency', 0.05, minval=0.)
        self.toolhead_sensor_name = config.get('toolhead_sensor', None)

        self.restore_position = config.getboolean('restore_position', True)
        self.restore_speed = config.getfloat('restore_speed', 100.0, above=0.)
        self.pause_on_error = config.getboolean('pause_on_error', True)

        if self.load_sync_distance <= self.load_sync_margin:
            raise config.error("chamois: load_sync_distance must be larger than load_sync_margin")

        # Last known MMU state (updated from the worker thread)
        self._initialized = False
        self._loaded = False
        self._selected_index = -1
        self._total_extruded_distance = 0
        self._number_of_tool_change = 0
        self._last_status_update = 0.
        self._firmware_version = None
        self._busy = False

        self._job_queue = Queue()
        self._running = True
        self._thread = threading.Thread(target=self._worker_thread, name="Chamois MMU Worker Thread")
        self._thread.daemon = True

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

        for name, desc in (
                ('CHAMOIS_HOME', self.cmd_CHAMOIS_HOME_help),
                ('CHAMOIS_DISABLE', self.cmd_CHAMOIS_DISABLE_help),
                ('CHAMOIS_HALT', self.cmd_CHAMOIS_HALT_help),
                ('CHAMOIS_STATUS', self.cmd_CHAMOIS_STATUS_help),
                ('CHAMOIS_LOAD', self.cmd_CHAMOIS_LOAD_help),
                ('CHAMOIS_UNLOAD', self.cmd_CHAMOIS_UNLOAD_help),
                ('CHAMOIS_MOVE', self.cmd_CHAMOIS_MOVE_help)):
            self.gcode.register_command(name, getattr(self, 'cmd_' + name), desc=desc)

        for i in range(self.number_of_toolhead):
            self.gcode.register_command(
                "T%d" % i, (lambda gcmd, tool=i: self.cmd_TOOL_CHANGE(gcmd, tool)),
                desc="Chamois: change to tool %d" % i)

    # ------------------------------------------------------------------
    # Klipper events

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.mcu = self.printer.lookup_object('mcu')

    def _handle_ready(self):
        self._thread.start()
        for name in self._LEGACY_MACROS:
            if self._has_command(name):
                logging.warning("chamois: macro %s is no longer used, see README (sync load/unload)", name)

    def _handle_disconnect(self):
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=2.)

    # ------------------------------------------------------------------
    # MMU communication (worker thread)

    def _worker_thread(self):
        while self._running:
            try:
                cmd, payload, future = self._job_queue.get(timeout=1.)
            except Empty:
                if time.time() - self._last_status_update > self.mmu_keepalive:
                    self._try_refresh_status()
                continue

            if not future.set_running_or_notify_cancel():
                continue
            try:
                code, response = self._send_and_receive(cmd, payload)
                if code != self._RESPONSE_OK:
                    raise ChamoisError("MMU command 0x%02X: %s" % (
                        cmd, self._RESPONSE_NAMES.get(code, "error 0x%02X" % code)), code)
                if cmd != self._CMD_GET_STATUS:
                    self._try_refresh_status()
                else:
                    self._parse_status(response)
                future.set_result(response)
            except Exception as e:
                logging.exception("chamois: command 0x%02X failed", cmd)
                self._try_refresh_status()
                future.set_exception(e)

    def _try_refresh_status(self):
        try:
            code, response = self._send_and_receive(self._CMD_GET_STATUS)
            if code == self._RESPONSE_OK:
                self._parse_status(response)
        except Exception as e:
            logging.warning("chamois: status update failed: %s", e)

    def _parse_status(self, payload):
        if len(payload) < 19:
            raise ChamoisError("Short status response from MMU")
        self._initialized = bool(payload[0])
        self._loaded = bool(payload[1])
        self._selected_index = -1 if payload[2] == self._NO_TOOL else payload[2]
        self._total_extruded_distance = int.from_bytes(payload[3:11], 'little')
        self._number_of_tool_change = int.from_bytes(payload[11:19], 'little')
        self._last_status_update = time.time()

    def _send_and_receive(self, cmd, payload=b""):
        # The firmware accepts a single TCP client, so use one short-lived
        # connection per command. Motion commands are only retried when the
        # connection could not be established - re-sending a LOAD whose
        # response got lost would move the filament twice.
        attempt = 0
        while True:
            attempt += 1
            try:
                sock = socket.create_connection((self.tcp_address, self.tcp_port), timeout=self.connect_timeout)
            except OSError:
                if attempt >= self.max_retries or not self._running:
                    raise
                time.sleep(0.5)
                continue
            try:
                with sock:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(0.5)
                    sock.sendall(struct.pack('<BHB', self._START_BYTE, 1 + len(payload), cmd) + payload)
                    return self._wait_for_response(sock)
            except (OSError, ChamoisError):
                if cmd not in self._IDEMPOTENT or attempt >= self.max_retries or not self._running:
                    raise

    def _wait_for_response(self, sock):
        # Response: <0xAA:1><length:2><response_code:1><payload:length-1>
        deadline = time.time() + self.read_timeout
        buf = bytearray()
        while time.time() < deadline:
            if not self._running:
                raise ChamoisError("Chamois plugin is shutting down")
            try:
                chunk = sock.recv(1024)
            except socket.timeout:
                continue
            if not chunk:
                raise ChamoisError("MMU closed the connection")
            buf.extend(chunk)

            start = buf.find(bytes([self._START_BYTE]))
            if start < 0:
                buf.clear()
                continue
            del buf[:start]
            if len(buf) < 4:
                continue
            length = int.from_bytes(buf[1:3], 'little')
            if len(buf) < 3 + length:
                continue
            return buf[3], bytes(buf[4:3 + length])
        raise ChamoisError("MMU did not answer within %.0fs" % self.read_timeout)

    # ------------------------------------------------------------------
    # MMU commands (reactor side)

    def _submit(self, cmd, payload=b''):
        future = Future()
        self._job_queue.put((cmd, payload, future))
        return future

    def _wait(self, future):
        while not future.done():
            self.reactor.pause(self.reactor.monotonic() + 0.05)
        return future.result()

    def _mmu(self, cmd, payload=b''):
        return self._wait(self._submit(cmd, payload))

    def _refresh_status(self):
        self._mmu(self._CMD_GET_STATUS)

    # ------------------------------------------------------------------
    # Printer helpers

    def _has_command(self, name):
        return bool(name) and name.upper() in self.gcode.gcode_handlers

    def _run(self, script):
        self.gcode.run_script_from_command(script)

    def _run_macro(self, name):
        if self._has_command(name):
            self._run(name)
            self.toolhead.wait_moves()
            return True
        return False

    def _check_extruder_hot(self):
        heater = self.toolhead.get_extruder().get_heater()
        if not heater.can_extrude:
            raise ChamoisError("Extruder is too cold to move filament (heat it up first)")

    def _queue_extruder_move(self, distance, speed):
        # Respect max_extrude_only_distance by splitting long moves
        extruder = self.toolhead.get_extruder()
        max_dist = max(1., getattr(extruder, 'max_e_dist', 50.) - 1.)
        speed = min(speed, getattr(extruder, 'max_e_velocity', speed))
        remaining = abs(distance)
        sign = 1. if distance >= 0 else -1.
        while remaining > 1e-6:
            step = min(remaining, max_dist)
            self._run("G1 E%.4f F%.1f" % (sign * step, speed * 60.))
            remaining -= step

    def _extruder_move(self, distance, speed):
        if distance:
            self._queue_extruder_move(distance, speed)
            self.toolhead.wait_moves()

    def _sync_move(self, distance):
        """Move filament with the toolhead extruder and the MMU at the same time."""
        # Find out when the toolhead will actually start moving and start the
        # MMU at the same moment, otherwise one drive fights the other.
        speed = min(self.sync_speed, getattr(self.toolhead.get_extruder(), 'max_e_velocity', self.sync_speed))
        start_print_time = self.toolhead.get_last_move_time()
        self._queue_extruder_move(distance, speed)
        now = self.reactor.monotonic()
        delay = start_print_time - self.mcu.estimated_print_time(now) - self.sync_latency
        if delay > 0:
            self.reactor.pause(now + delay)
        cmd = self._CMD_EXTRUDE if distance > 0 else self._CMD_RETRACT
        future = self._submit(cmd, struct.pack('<ff', abs(distance), speed))
        self.toolhead.wait_moves()
        self._wait(future)

    def _sensor_state(self):
        if not self.toolhead_sensor_name:
            return None
        sensor = self.printer.lookup_object("filament_switch_sensor %s" % self.toolhead_sensor_name, None)
        if sensor is None:
            sensor = self.printer.lookup_object("filament_motion_sensor %s" % self.toolhead_sensor_name, None)
        if sensor is None:
            raise ChamoisError("toolhead_sensor '%s' not found" % self.toolhead_sensor_name)
        return bool(sensor.get_status(self.reactor.monotonic())['filament_detected'])

    # ------------------------------------------------------------------
    # Tool change sequences

    def _check_firmware(self):
        # ENGAGE is new in the sync load/unload firmware. Before homing it is
        # rejected without moving anything: new firmware answers with a generic
        # error, old firmware does not know the command at all.
        try:
            self._mmu(self._CMD_ENGAGE)
        except ChamoisError as e:
            if e.code == 0xA2:
                raise ChamoisError("MMU firmware is too old for this plugin, please update chamois-mmu")
            if e.code is None:
                raise

    def _ensure_homed(self):
        self._refresh_status()
        if not self._initialized:
            self._check_firmware()
            self.gcode.respond_info("Chamois: homing MMU")
            self._mmu(self._CMD_HOME)

    def _unload(self):
        self._check_extruder_hot()
        self._run("M83")
        self._run_macro(self.form_tip_macro)

        # Pull the tip out of the toolhead. The MMU grips the filament and
        # pulls in sync, so the extruder never fights a locked MMU gear.
        if self.toolhead_unload_distance > 0:
            if self.sync_unload:
                self._mmu(self._CMD_ENGAGE)
                self._sync_move(-self.toolhead_unload_distance)
            else:
                self._extruder_move(-self.toolhead_unload_distance, self.toolhead_load_speed)

        if self._sensor_state():
            raise ChamoisError("Filament still detected in the toolhead after unload; "
                               "check tip forming / toolhead_unload_distance")

        # Fast unload of the bowden back to the MMU park position
        self._mmu(self._CMD_UNLOAD)

    def _load(self, index):
        self._check_extruder_hot()
        self._run("M83")
        self._mmu(self._CMD_SELECT_TOOL, struct.pack('<H', index))

        # Fast load, stop load_sync_margin before the extruder gears
        self._mmu(self._CMD_LOAD, struct.pack('<f', -self.load_sync_margin))

        # Let the extruder grab the filament while the MMU is still pushing
        self._sync_move(self.load_sync_distance)
        self._mmu(self._CMD_RELEASE)

        self._extruder_move(self.toolhead_load_distance, self.toolhead_load_speed)

        if self._sensor_state() is False:
            raise ChamoisError("Filament not detected in the toolhead after load")

        self._run_macro(self.after_load_macro)

    def _with_saved_state(self, gcmd, func, restore_move):
        if self.toolhead is None:
            raise gcmd.error("Chamois: printer not ready")
        if self._busy:
            raise gcmd.error("Chamois: another MMU operation is in progress")
        self._busy = True
        self._run("SAVE_GCODE_STATE NAME=%s" % self._STATE_NAME)
        try:
            func()
        except Exception as e:
            logging.exception("chamois: operation failed")
            # Leave the toolhead where it is (parked) so the problem can be fixed
            self._run("RESTORE_GCODE_STATE NAME=%s MOVE=0" % self._STATE_NAME)
            msg = "Chamois: %s" % (e,)
            if self.pause_on_error and self._is_printing() and self._has_command("PAUSE"):
                self.gcode.respond_raw("!! %s" % msg)
                self.gcode.respond_info("Chamois: print paused. Fix the filament (CHAMOIS_UNLOAD / "
                                        "CHAMOIS_LOAD TOOL=n) and RESUME.")
                self._run("PAUSE")
                return
            raise gcmd.error(msg)
        finally:
            self._busy = False
        self._run("RESTORE_GCODE_STATE NAME=%s MOVE=%d MOVE_SPEED=%.1f" % (
            self._STATE_NAME, 1 if restore_move else 0, self.restore_speed))

    def _is_printing(self):
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is None:
            return False
        return print_stats.get_status(self.reactor.monotonic()).get('state') == 'printing'

    def _validate_tool(self, gcmd, index):
        if not 0 <= index < self.number_of_toolhead:
            raise gcmd.error("Invalid tool index %d, must be 0..%d" % (index, self.number_of_toolhead - 1))

    # ------------------------------------------------------------------
    # Status

    def get_status(self, eventtime):
        return {
            'initialized': self._initialized,
            'loaded': self._loaded,
            'selected_index': self._selected_index,
            'total_extruded_distance': self._total_extruded_distance,
            'number_of_tool_change': self._number_of_tool_change,
            'busy': self._busy,
        }

    # ------------------------------------------------------------------
    # G-code commands

    cmd_CHAMOIS_HOME_help = "Home the Chamois MMU selector"
    def cmd_CHAMOIS_HOME(self, gcmd):
        try:
            gcmd.respond_info("Chamois MMU homing")
            self._mmu(self._CMD_HOME)
            gcmd.respond_info("Chamois MMU is ready")
        except Exception as e:
            raise gcmd.error("Failed to home Chamois MMU: %s" % (e,))

    cmd_CHAMOIS_HALT_help = "Restart the Chamois MMU (put all filaments back to the park position first)"
    def cmd_CHAMOIS_HALT(self, gcmd):
        try:
            self._mmu(self._CMD_HALT)
            gcmd.respond_info("Chamois MMU is restarting")
        except Exception as e:
            raise gcmd.error("Failed to restart Chamois MMU: %s" % (e,))

    cmd_CHAMOIS_DISABLE_help = "Unload the filament (if loaded) and disable the Chamois MMU motors"
    def cmd_CHAMOIS_DISABLE(self, gcmd):
        def run():
            self._refresh_status()
            if self._loaded:
                self._run_macro(self.park_macro)
                self._unload()
            self._mmu(self._CMD_DISABLE)
        self._with_saved_state(gcmd, run, restore_move=False)
        gcmd.respond_info("Chamois MMU is disabled")

    cmd_CHAMOIS_STATUS_help = "Report the Chamois MMU status"
    def cmd_CHAMOIS_STATUS(self, gcmd):
        try:
            if self._firmware_version is None:
                self._firmware_version = self._mmu(self._CMD_GET_FIRMWARE_VERSION).decode('utf-8', 'ignore')
            self._refresh_status()
        except Exception as e:
            raise gcmd.error("Chamois MMU not reachable: %s" % (e,))
        gcmd.respond_info("Chamois MMU firmware %s: %s" % (self._firmware_version, self.get_status(None)))

    cmd_CHAMOIS_UNLOAD_help = "Form the tip and unload the current filament back to the MMU"
    def cmd_CHAMOIS_UNLOAD(self, gcmd):
        def run():
            self._ensure_homed()
            if not self._loaded:
                gcmd.respond_info("Chamois: no filament loaded")
                return
            self._run_macro(self.park_macro)
            self._unload()
        self._with_saved_state(gcmd, run, restore_move=self.restore_position)

    cmd_CHAMOIS_LOAD_help = "Load filament TOOL=<n> to the nozzle (unloads the current one first)"
    def cmd_CHAMOIS_LOAD(self, gcmd):
        self.cmd_TOOL_CHANGE(gcmd, gcmd.get_int('TOOL', minval=0))

    cmd_CHAMOIS_MOVE_help = ("Move the selected filament with the MMU only: DISTANCE=<mm> [SPEED=<mm/s>]."
                             " For calibration and recovery")
    def cmd_CHAMOIS_MOVE(self, gcmd):
        distance = gcmd.get_float('DISTANCE')
        speed = gcmd.get_float('SPEED', 0., minval=0.)
        try:
            self._mmu(self._CMD_EXTRUDE, struct.pack('<ff', distance, speed))
        except Exception as e:
            raise gcmd.error("Chamois move failed: %s" % (e,))

    def cmd_TOOL_CHANGE(self, gcmd, index):
        self._validate_tool(gcmd, index)

        def run():
            self._ensure_homed()
            if self._loaded and self._selected_index == index:
                gcmd.respond_info("Chamois: tool %d already loaded" % index)
                return
            start = time.time()
            gcmd.respond_info("Chamois: changing to tool %d" % index)
            self._run_macro(self.park_macro)
            if self._loaded:
                self._unload()
            self._load(index)
            gcmd.respond_info("Chamois: tool %d loaded in %.1fs" % (index, time.time() - start))

        self._with_saved_state(gcmd, run, restore_move=self.restore_position)


def load_config(config):
    return Chamois(config)
