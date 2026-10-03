# Runs the plugin against a fake MMU (TCP, same protocol as the firmware)
# and minimal Klipper stand-ins.  python3 -m unittest discover tests
import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import chamois  # noqa: E402


class FakeMMU:
    def __init__(self, tools=4, legacy=False):
        self.tools = tools
        self.legacy = legacy
        self.initialized = False
        self.loaded = False
        self.selected = -1
        self.commands = []
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            conn, _ = self.server.accept()
            with conn:
                buf = b''
                while True:
                    data = conn.recv(1024)
                    if not data:
                        break
                    buf += data
                    while len(buf) >= 4:
                        length = struct.unpack('<H', buf[1:3])[0]
                        if len(buf) < 3 + length:
                            break
                        cmd, payload = buf[3], buf[4:3 + length]
                        buf = buf[3 + length:]
                        code, resp = self.handle(cmd, payload)
                        conn.sendall(struct.pack('<BHB', 0xAA, 1 + len(resp), code) + resp)

    def handle(self, cmd, payload):
        floats = struct.unpack('<%df' % (len(payload) // 4), payload) if cmd in (0xA9, 0xAA, 0xAC, 0xAD) else ()
        if cmd != 0xA0:
            self.commands.append((cmd, tuple(round(f, 3) for f in floats) or payload))
        if cmd == 0xA0:
            status = bytes([self.initialized, self.loaded, 0xFF if self.selected < 0 else self.selected])
            return 0, status + bytes(85)
        if cmd == 0x01:
            return 0, b''
        if cmd == 0x06:
            return 0, b'test'
        if cmd == 0xA6:
            self.initialized, self.loaded, self.selected = True, False, -1
            return 0, b''
        if cmd == 0xAF:
            if self.legacy:
                return 0xA2, b''
            return (0, b'') if self.initialized and self.selected >= 0 else (0xA3, b'')
        if cmd == 0xAB:
            idx = struct.unpack('<H', payload)[0]
            if not self.initialized or self.loaded or idx >= self.tools:
                return 0xA3, b''
            self.selected = idx
            return 0, b''
        if cmd == 0xA9:
            if self.loaded or self.selected < 0:
                return 0xA3, b''
            self.loaded = True
            return 0, b''
        if cmd == 0xAA:
            if not self.loaded:
                return 0xA3, b''
            self.loaded = False
            return 0, b''
        if cmd in (0xAC, 0xAD, 0xAE):
            time.sleep(0.05)
            return 0, b''
        if cmd == 0xA8:
            self.initialized = False
            return 0, b''
        return 0xA2, b''


class CommandError(Exception):
    pass


class FakeConfig:
    error = CommandError

    def __init__(self, printer, values):
        self.printer, self.values = printer, values

    def get_printer(self):
        return self.printer

    def _get(self, name, default, conv):
        if name in self.values:
            return conv(self.values[name])
        if default is KeyError:
            raise CommandError("missing " + name)
        return default

    def get(self, name, default=KeyError):
        return self._get(name, default, str)

    def getint(self, name, default=KeyError, **kw):
        return self._get(name, default, int)

    def getfloat(self, name, default=KeyError, **kw):
        return self._get(name, default, float)

    def getboolean(self, name, default=KeyError):
        return self._get(name, default, bool)


class FakeReactor:
    def monotonic(self):
        return time.monotonic()

    def pause(self, waketime):
        time.sleep(max(0., waketime - time.monotonic()))


class FakeHeater:
    can_extrude = True


class FakeExtruder:
    max_e_dist = 50.
    max_e_velocity = 100.

    def __init__(self):
        self.heater = FakeHeater()

    def get_heater(self):
        return self.heater


class FakeToolhead:
    def __init__(self):
        self.extruder = FakeExtruder()

    def wait_moves(self):
        pass

    def get_last_move_time(self):
        return time.monotonic() + 0.1

    def get_extruder(self):
        return self.extruder


class FakeMCU:
    def estimated_print_time(self, eventtime):
        return eventtime


class FakeGcmd:
    def __init__(self, gcode, params=None):
        self.gcode, self.params = gcode, params or {}

    def respond_info(self, msg):
        self.gcode.messages.append(msg)

    def error(self, msg):
        return CommandError(msg)

    def get_int(self, name, default=None, **kw):
        return int(self.params.get(name, default))

    def get_float(self, name, default=None, **kw):
        return float(self.params.get(name, default))


class FakeGcode:
    def __init__(self):
        self.gcode_handlers = {}
        self.scripts = []
        self.messages = []

    def register_command(self, name, func, desc=None):
        self.gcode_handlers[name] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)
        name = script.split()[0]
        if name in self.gcode_handlers and not name.startswith('T'):
            self.gcode_handlers[name](FakeGcmd(self))

    def respond_info(self, msg):
        self.messages.append(msg)

    def respond_raw(self, msg):
        self.messages.append(msg)


class FakePrinter:
    def __init__(self):
        self.reactor = FakeReactor()
        self.objects = {'gcode': FakeGcode(), 'toolhead': FakeToolhead(), 'mcu': FakeMCU()}
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=KeyError):
        if name in self.objects:
            return self.objects[name]
        if default is KeyError:
            raise KeyError(name)
        return default

    def register_event_handler(self, event, cb):
        self.handlers[event] = cb


class ChamoisTest(unittest.TestCase):
    def setUp(self, legacy=False):
        self.mmu = FakeMMU(legacy=legacy)
        self.printer = FakePrinter()
        self.gcode = self.printer.objects['gcode']
        self.gcode.register_command('CHAMOIS_FORM_TIP', lambda g: None)
        self.gcode.register_command('CHAMOIS_PARK', lambda g: None)
        self.plugin = chamois.load_config(FakeConfig(self.printer, {
            'tcp_address': '127.0.0.1', 'tcp_port': self.mmu.port, 'toolhead_load_distance': 60}))
        self.printer.handlers['klippy:connect']()
        self.printer.handlers['klippy:ready']()

    def tearDown(self):
        self.printer.handlers['klippy:disconnect']()
        self.mmu.server.close()

    def run_tool(self, index):
        self.gcode.scripts.clear()
        self.mmu.commands.clear()
        self.gcode.gcode_handlers['T%d' % index](FakeGcmd(self.gcode))

    def test_first_load_homes_and_syncs(self):
        self.run_tool(1)
        self.assertEqual(self.mmu.commands, [
            (0xAF, b''),            # firmware probe
            (0xA6, b''),            # home
            (0xAB, struct.pack('<H', 1)),
            (0xA9, (-20.0,)),       # fast load, stop 20mm short
            (0xAC, (50.0, 15.0)),   # sync load
            (0xAE, b''),            # release
        ])
        self.assertEqual(self.gcode.scripts, [
            'SAVE_GCODE_STATE NAME=_chamois_toolchange', 'CHAMOIS_PARK', 'M83',
            'G1 E49.0000 F900.0', 'G1 E1.0000 F900.0',
            'G1 E49.0000 F600.0', 'G1 E11.0000 F600.0',
            'RESTORE_GCODE_STATE NAME=_chamois_toolchange MOVE=1 MOVE_SPEED=100.0'])
        self.assertTrue(self.plugin.get_status(0)['loaded'])

    def test_swap_forms_tip_and_unloads_in_sync(self):
        self.run_tool(1)
        self.run_tool(2)
        self.assertEqual(self.mmu.commands, [
            (0xAF, b''),            # engage for sync unload
            (0xAD, (80.0, 15.0)),
            (0xAA, b''),            # fast unload
            (0xAB, struct.pack('<H', 2)),
            (0xA9, (-20.0,)),
            (0xAC, (50.0, 15.0)),
            (0xAE, b''),
        ])
        self.assertIn('CHAMOIS_FORM_TIP', self.gcode.scripts)
        self.assertLess(self.gcode.scripts.index('CHAMOIS_FORM_TIP'), self.gcode.scripts.index('G1 E-49.0000 F900.0'))
        self.assertEqual(self.plugin.get_status(0)['selected_index'], 2)

    def test_same_tool_is_noop(self):
        self.run_tool(3)
        self.run_tool(3)
        self.assertEqual(self.mmu.commands, [])

    def test_cold_extruder_raises(self):
        self.printer.objects['toolhead'].extruder.heater.can_extrude = False
        with self.assertRaises(CommandError):
            self.run_tool(0)
        self.assertIn('RESTORE_GCODE_STATE NAME=_chamois_toolchange MOVE=0', self.gcode.scripts)
        self.assertFalse(self.plugin.get_status(0)['busy'])


class LegacyFirmwareTest(ChamoisTest):
    def setUp(self):
        ChamoisTest.setUp(self, legacy=True)

    def test_first_load_homes_and_syncs(self):
        with self.assertRaisesRegex(CommandError, 'too old'):
            self.run_tool(1)
        self.assertEqual(self.mmu.commands, [(0xAF, b'')])

    test_swap_forms_tip_and_unloads_in_sync = None
    test_same_tool_is_noop = None
    test_cold_extruder_raises = None


if __name__ == '__main__':
    unittest.main()
