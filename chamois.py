# Chamois Klipper Plugin
from curses import raw
import threading
import socket
import struct
import time
import queue
from concurrent.futures import Future


class Chamois:

    __CMD_PING = 0x01
    __CMD_GET_STATUS = 0xA0
    __CMD_HOME = 0xA6
    __CMD_DISABLE = 0xA8
    __CMD_HALT = 0x02
    __CMD_LOAD = 0xA9
    __CMD_UNLOAD = 0xAA
    __CMD_SELECT_TOOL = 0xAB
    __CMD_RELEASE = 0xAE

    __RESPONSE_CODE_OK = 0x00

    def __init__(self, config):

        self._running = True
        self._job_queue = queue.Queue()
        self._thread = threading.Thread(target=self._main_thread)
        self._thread.start()

        self._initialized = 0
        self._loaded = 0
        self._selected_index = 0
        self._total_extruded_distance = 0
        self._number_of_tool_change = 0
        self._last_status_update = 0

        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.connect_timeout = config.getfloat('connect_timeout', 5.0)  # Default timeout for responses
        self.read_timeout = config.getfloat('read_timeout', 5.0)  # Timeout for command responses
        self.max_retries = config.getint('max_retries', 3)         # Maximum retries for command responses
        self.mmu_keepalive = config.getint('mmu_keepalive', 1)  # Keep connection alive

        self.number_of_toolhead = config.getint('number_of_toolhead', 4, minval=1, maxval=20)
        self.tcp_address = config.get('tcp_address', None)
        self.tcp_port = config.getint('tcp_port', 5433)

        if not self.tcp_address or not self.tcp_port:
            raise ValueError("TCP address and port must be specified in the configuration.")

        self.gcode.register_command('CHAMOIS_HOME', self.cmd_CHAMOIS_HOME, desc=self.cmd_CHAMOIS_HOME_help)
        self.gcode.register_command('CHAMOIS_DISABLE', self.cmd_CHAMOIS_DISABLE, desc=self.cmd_CHAMOIS_DISABLE_help)
        self.gcode.register_command('CHAMOIS_HALT', self.cmd_CHAMOIS_HALT, desc=self.cmd_CHAMOIS_HALT_help)

        # Register T0, T1, ... commands for tool changing
        for i in range(self.number_of_toolhead):
            self.gcode.register_command(f"T{i}", lambda gcmd, tool=i: self.cmd_CHAMOIS_TOOL_CHANGE(gcmd, tool),
                                        desc=f"Chamois: Unload, select, load, and release tool {i}")

    def _main_thread(self):
        while self._running:
            try:
                cmd, payload, future = self._job_queue.get(timeout=1)
                try:
                    response_code, payload = self._send_and_receive(cmd, payload)
                    self._update_status(forced=True)
                    if response_code == self.__RESPONSE_CODE_OK:
                        future.set_result(payload)
                    elif len(payload) == 0:
                        future.set_exception(RuntimeError(f"Command failed with response code: {hex(response_code)}"))
                    else:
                        future.set_exception(RuntimeError(
                            f"Command failed with response code: {hex(response_code)} error: {payload.decode('utf-8', 'ignore')}"))
                except Exception as e:
                    future.set_exception(e)
            except queue.Empty:
                continue
            finally:
                self._update_status()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        sock.connect((self.tcp_address, self.tcp_port))
        sock.settimeout(self.read_timeout)
        return sock

    def _send_and_receive(self, command_code: int, payload: bytes = b"") -> tuple[int, bytes]:
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                sock = self._connect()
                self._submit_command(sock, command_code, payload)
                return self._wait_for_response(sock)
            except Exception as e:
                retry_count += 1
                if retry_count >= self.max_retries:
                    raise e
            finally:
                if sock:
                    sock.close()

    def _submit_command(self, sock, command_code: int, payload: bytes = b""):
        length = 1 + len(payload)
        length_bytes = struct.pack('<H', length)
        request = bytes([0xAA]) + length_bytes + bytes([command_code]) + payload
        sock.sendall(request)

    def _wait_for_response(self, sock: socket.socket) -> tuple[int, bytes]:
        last_byte_time = time.time()
        received_bytes = bytearray()
        while last_byte_time - time.time() < self.read_timeout:
            byte = sock.recv(1)
            received_bytes.extend(byte)
            last_byte_time = time.time()

            # Response:  <0xAA:1><length:2><response_code:1><payload>
            while len(received_bytes) > 0 and received_bytes[0] != 0xAA:
                received_bytes.pop(0)

            if len(received_bytes) < 4:
                continue  # minimal response is 4 bytes

            length = int.from_bytes(received_bytes[1:3], 'little')

            if len(received_bytes) < length + 3:
                continue

            result_code = received_bytes[3]
            payload = received_bytes[4:4+length-1] if length > 1 else b''
            return (result_code, payload)

        raise TimeoutError(
            "Response timed out after {} seconds".format(self.read_timeout))

    def _update_status(self, forced=False):
        current_time = time.time()
        if current_time - self._last_status_update < self.mmu_keepalive and not forced:
            return

        response_code, payload = self._send_and_receive(self.__CMD_GET_STATUS)

        if response_code != self.__RESPONSE_CODE_OK:
            raise RuntimeError("Failed to get status from Chamois MMU, response code: {}".format(hex(response_code)))

        self._initialized = bool(payload[0])
        self._loaded = bool(payload[1])
        self._selected_index = payload[2]
        self._total_extruded_distance = int.from_bytes(payload[3:11], 'little')
        self._number_of_tool_change = int.from_bytes(payload[11:19], 'little')
        self._last_status_update = current_time

    def send_cmd_async(self, cmd, payload=b''):
        future = Future()
        self._job_queue.put((cmd, payload, future))
        return future

    def send_cmd(self, cmd, payload=b''):
        future = self.send_cmd_async(cmd, payload)
        while not future.done():
            time.sleep(0.1)

        if future.exception():
            raise future.exception()
        return future.result()

    def _wait_moves(self):
        self.printer.lookup_object('toolhead').wait_moves()

    def _park(self):
        if 'CHAMOIS_PARK' in self.gcode.gcode_handlers:
            self.gcode.run_script_from_command('CHAMOIS_PARK')
            self._wait_moves()

    def _unload(self):
        unload_future = self.send_cmd_async(self.__CMD_UNLOAD)
        if 'CHAMOIS_BEFORE_UNLOAD' in self.gcode.gcode_handlers:
            self.gcode.run_script_from_command('CHAMOIS_BEFORE_UNLOAD')
            self._wait_moves()

        while not unload_future.done():
            if 'CHAMOIS_ON_UNLOAD' in self.gcode.gcode_handlers:
                self.gcode.run_script_from_command('CHAMOIS_ON_UNLOAD')
                self._wait_moves()
            else:
                time.sleep(0.1)

        if unload_future.exception():
            raise unload_future.exception()

    def get_status(self, eventtime):
        return {
            'initialized': self._initialized,
            'loaded': self._loaded,
            'selected_index': self._selected_index,
            'total_extruded_distance': self._total_extruded_distance,
            'number_of_tool_change': self._number_of_tool_change,
        }

    def shutdown(self):
        self._running = False
        self._thread.join()

    #fmt: off
    cmd_CHAMOIS_HOME_help = "Initializes and homes the Chamois MMU"
    def cmd_CHAMOIS_HOME(self, gcmd):
        try:
            gcmd.respond_info("Chamois MMU Homing")
            self.send_cmd(self.__CMD_HOME)
            gcmd.respond_info("Chamois MMU is ready")
        except Exception as e:
            raise gcmd.error(f"Failed to home Chamois MMU: {str(e)}")

    cmd_CHAMOIS_HALT_help = "Restarts the Chamois MMU"
    def cmd_CHAMOIS_HALT(self, gcmd):
        try:
            gcmd.respond_info("Chamois MMU Halting")
            self.send_cmd(self.__CMD_HALT)
            gcmd.respond_info("Chamois MMU is halted")
        except Exception as e:
            raise gcmd.error(f"Failed to halt Chamois MMU: {str(e)}")

    cmd_CHAMOIS_DISABLE_help = "Disables the Chamois MMU"
    def cmd_CHAMOIS_DISABLE(self, gcmd):
        try:
            gcmd.respond_info("Disabling Chamois MMU")
            self.send_cmd(self.__CMD_PING)
            if self._loaded:
                self._park()
                self._unload()
            self.send_cmd(self.__CMD_DISABLE)
            gcmd.respond_info("Chamois MMU is disabled")
        except Exception as e:
            raise gcmd.error(f"Failed to disable Chamois MMU: {str(e)}")
    #fmt: on

    def cmd_CHAMOIS_TOOL_CHANGE(self, gcmd, index):
        if not (0 <= index < self.number_of_toolhead):
            raise gcmd.error(f"Invalid tool index: {index}. Must be between 0 and {self.number_of_toolhead - 1}.")

        try:
            gcmd.respond_info("Chamois MMU Tool Change")
            self.send_cmd(self.__CMD_PING)

            if not self._initialized:
                self.send_cmd(self.__CMD_HOME)

            self._park()
            if self._loaded:
                self._unload()

            self.send_cmd(self.__CMD_SELECT_TOOL, struct.pack('<H', index))
            load_future = self.send_cmd_async(self.__CMD_LOAD)
            while not load_future.done():
                if 'CHAMOIS_ON_LOAD' in self.gcode.gcode_handlers:
                    self.gcode.run_script_from_command('CHAMOIS_ON_LOAD')
                    self._wait_moves()
                else:
                    time.sleep(0.1)

            if load_future.exception():
                raise load_future.exception()

            self.send_cmd(self.__CMD_RELEASE)

            if 'CHAMOIS_AFTER_LOAD' in self.gcode.gcode_handlers:
                self.gcode.run_script_from_command('CHAMOIS_AFTER_LOAD')
                self._wait_moves()
            gcmd.respond_info(f"Tool change to index {index} completed successfully.")
        except Exception as e:
            raise gcmd.error(f"Tool change failed: {str(e)}")


def load_config(config):
    chamois = Chamois(config)
    config.get_printer().add_object("chamois", chamois)
    return chamois
