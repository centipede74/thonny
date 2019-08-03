from thonny.common import (
    InputSubmission,
    InterruptCommand,
    EOFCommand,
    parse_message,
    ToplevelCommand,
    ToplevelResponse,
    InlineCommand,
    InlineResponse,
    UserError,
    serialize_message,
    BackendEvent,
    ValueInfo,
)
import sys
import logging
import traceback
import queue
from thonny.plugins.micropython.connection import (
    ConnectionClosedException,
    ConnectionFailedException,
)
from textwrap import dedent
import ast
import re
from queue import Queue, Empty
import threading
import os
import time
from thonny.misc_utils import find_volumes_by_name
import jedi
import io
import tokenize
from thonny.running import EXPECTED_TERMINATION_CODE
import binascii

# See https://github.com/dhylands/rshell/blob/master/rshell/main.py
# for UART_BUFFER_SIZE vs USB_BUFFER_SIZE
# ampy uses 32 bytes: https://github.com/pycampers/ampy/blob/master/ampy/files.py
# I'm not worrying so much, because reader thread reads continuously
# and writer (SerialConnection) has it's own blocks and delays
BUFFER_SIZE = 512

BAUDRATE = 115200
ENCODING = "utf-8"

# Commands
RAW_MODE_CMD = b"\x01"
NORMAL_MODE_CMD = b"\x02"
INTERRUPT_CMD = b"\x03"
SOFT_REBOOT_CMD = b"\x04"

# Output tokens
THONNY_MSG_START = b"\x02"
THONNY_MSG_END = b"\x04"
EOT = b"\x04"
NORMAL_PROMPT = b">>> "
LF = b"\n"
OK = b"OK"

# first prompt when switching to raw mode (or after soft reboot in raw mode)
# Looks like it's not translatable in CP
# https://github.com/adafruit/circuitpython/blob/master/locale/circuitpython.pot
FIRST_RAW_PROMPT = b"raw REPL; CTRL-B to exit\r\n>"

RAW_PROMPT = b">"

BLOCK_CLOSERS = re.compile(
    b"|".join(map(re.escape, [LF, EOT, THONNY_MSG_START, NORMAL_PROMPT, FIRST_RAW_PROMPT]))
)


logger = logging.getLogger("thonny.micropython.backend")


def debug(msg):
    return
    print(msg, file=sys.stderr)


class MicroPythonBackend:
    def __init__(self, connection, clean, api_stubs_path):
        self._connection = connection
        self._cwd = None
        self._interrupt_requested = False
        self._command_queue = Queue()  # populated by reader thread
        self._progress_times = {}

        self._api_stubs_path = api_stubs_path

        self._command_reading_thread = threading.Thread(target=self._read_commands, daemon=True)
        self._command_reading_thread.start()

        self._startup_time = time.time()
        self._ctrl_suggestion_given = False

        try:
            self._prepare(clean)
            self._mainloop()
        except ConnectionClosedException:
            self._on_connection_closed()
        except Exception:
            logger.exception("Crash in backend")
            traceback.print_exc()

    def _prepare(self, clean):
        if clean:
            self._interrupt_to_raw_prompt()
            self._clear_environment()
        else:
            self._process_until_initial_raw_prompt()

        self._cwd = self._fetch_cwd()
        self._welcome_text = self._fetch_welcome_text()
        self._builtin_modules = self._fetch_builtin_modules()
        self._builtins_info = self._fetch_builtins_info()

        self._send_ready_message()

    def _mainloop(self):
        while True:
            try:
                self._check_for_connection_errors()
                cmd = self._command_queue.get(timeout=0.1)
                if isinstance(cmd, InputSubmission):
                    self._submit_input(cmd.data)
                elif isinstance(cmd, EOFCommand):
                    self._soft_reboot(False)
                elif isinstance(cmd, InterruptCommand):
                    self._interrupt()
                else:
                    self.handle_command(cmd)
            except Empty:
                self._check_for_idle_events()
            except KeyboardInterrupt:
                self._interrupt()

    def _fetch_welcome_text(self):
        self._connection.write(NORMAL_MODE_CMD)
        welcome_text = self._connection.read_until(NORMAL_PROMPT).strip(b"\r\n >")
        if os.name != "nt":
            welcome_text = welcome_text.replace(b"\r\n", b"\n")

        # Go back to raw prompt
        self._connection.write(RAW_MODE_CMD)
        self._connection.read_until(FIRST_RAW_PROMPT)

        return welcome_text.decode(ENCODING)

    def _fetch_uname(self):
        res = self._evaluate("__module_os.uname()", prelude="import os as __module_os")
        return {
            "sysname": res[0],
            "nodename": res[1],
            "release": res[2],
            "version": res[3],
            "machine": res[4],
        }

    def _fetch_builtin_modules(self):
        out, err, _ = self._execute("help('modules')", capture_output=True)
        assert not err, "Error was: %r" % err

        modules_str_lines = out.strip().splitlines()

        last_line = modules_str_lines[-1].strip()
        if last_line.count(" ") > 0 and "  " not in last_line and "\t" not in last_line:
            # probably something like "plus any modules on the filesystem"
            # (can be in different languages)
            modules_str_lines = modules_str_lines[:-1]

        modules_str = (
            " ".join(modules_str_lines)
            .replace("/__init__", "")
            .replace("__main__", "")
            .replace("/", ".")
        )

        return modules_str.split()

    def _fetch_builtins_info(self):
        """
        for p in self._get_api_stubs_path():
            builtins_file = os.path.join(p, "__builtins__.py")
            if os.path.exists(builtins_file):
                return parse_api_information(builtins_file)
        """
        path = os.path.join(self._api_stubs_path, "builtins.py")
        if os.path.exists(path):
            return parse_api_information(path)
        else:
            return {}

    def _fetch_cwd(self):
        return self._evaluate(
            "__module_os.getcwd() if hasattr(__module_os, 'getcwd') else ''",
            prelude="import os as __module_os",
        )

    def _send_ready_message(self):
        self.send_message(ToplevelResponse(welcome_text=self._welcome_text, cwd=self._cwd))

    def _check_send_inline_progress(self, cmd, value, maximum, description=None):
        assert "id" in cmd
        prev_time = self._progress_times.get(cmd["id"], 0)
        if value != maximum and time.time() - prev_time < 0.2:
            # Don't notify too often
            return
        else:
            self._progress_times[cmd["id"]] = time.time()

        if description is None:
            description = cmd.get("description", "Working...")

        self.send_message(
            BackendEvent(
                event_type="InlineProgress",
                command_id=cmd["id"],
                value=value,
                maximum=maximum,
                description=description,
            )
        )

    def _interrupt(self):
        self._connection.write(INTERRUPT_CMD)

    def _check_for_interrupt(self):
        if self._interrupt_requested:
            self._interrupt_requested = False
            raise KeyboardInterrupt()

    def _interrupt_to_raw_prompt(self):
        # NB! Sometimes disconnecting and reconnecting (on macOS?)
        # too quickly causes anomalies. See CalliopeMiniProxy for more details

        discarded_bytes = b""

        for delay in [0.05, 0.5, 0.1, 2.0]:
            # Interrupt several times, because with some drivers first interrupts seem to vanish
            self._connection.reset_output_buffer()
            self._connection.write(INTERRUPT_CMD)
            self._connection.write(RAW_MODE_CMD)
            time.sleep(delay)
            discarded_bytes += self._connection.read_all()
            if discarded_bytes.endswith(FIRST_RAW_PROMPT) or discarded_bytes.endswith(b"\r\n>"):
                break
        else:
            raise TimeoutError("Can't get to raw prompt. Read bytes: " + str(discarded_bytes))

    def _soft_reboot(self, side_command):
        if side_command:
            self._interrupt_to_raw_prompt()

        # Need to go to normal mode. MP doesn't run user code in raw mode
        # (CP does, but it doesn't hurt to do it there as well)
        self._connection.write(NORMAL_MODE_CMD)
        self._connection.read_until(NORMAL_PROMPT)

        self._connection.write(SOFT_REBOOT_CMD)

        if not side_command:
            self._process_until_raw_prompt()
            self.send_message(ToplevelResponse(cwd=self._cwd))

    def _read_commands(self):
        "works in separate thread"

        while True:
            line = sys.stdin.readline()
            if line == "":
                logger.info("Read stdin EOF")
                sys.exit()
            cmd = parse_message(line)
            if isinstance(cmd, InterruptCommand):
                self._interrupt_requested = True
            else:
                self._command_queue.put(cmd)

    def handle_command(self, cmd):
        assert isinstance(cmd, (ToplevelCommand, InlineCommand))

        def create_error_response(**kw):
            if isinstance(cmd, ToplevelCommand):
                return ToplevelResponse(command_name=cmd.name, **kw)
            else:
                return InlineResponse(command_name=cmd.name, **kw)

        handler = getattr(self, "_cmd_" + cmd.name, None)

        if handler is None:
            response = create_error_response(error="Unknown command: " + cmd.name)
        else:
            try:
                response = handler(cmd)
            except SystemExit:
                # Must be caused by Thonny or plugins code
                if isinstance(cmd, ToplevelCommand):
                    traceback.print_exc()
                response = create_error_response(SystemExit=True)
            except UserError as e:
                sys.stderr.write(str(e) + "\n")
                response = create_error_response()
            except KeyboardInterrupt:
                response = create_error_response(error="Interrupted", interrupted=True)
            except Exception:
                _report_internal_error()
                response = create_error_response(context_info="other unhandled exception")

        if response is None:
            response = {}

        if response is False:
            # Command doesn't want to send any response
            return

        elif isinstance(response, dict):
            if isinstance(cmd, ToplevelCommand):
                response = ToplevelResponse(command_name=cmd.name, **response)
            elif isinstance(cmd, InlineCommand):
                response = InlineResponse(cmd.name, **response)

        if "id" in cmd and "command_id" not in response:
            response["command_id"] = cmd["id"]

        debug("cmd: " + str(cmd) + ", respin: " + str(response))
        self.send_message(response)

    def _submit_input(self, cdata: str) -> None:
        # TODO: what if there is a previous unused data waiting
        assert self._connection.outgoing_is_empty()

        assert cdata.endswith("\n")
        if not cdata.endswith("\r\n"):
            # submission is done with CRLF
            cdata = cdata[:-1] + "\r\n"

        bdata = cdata.encode(ENCODING)

        self._connection.write(bdata)
        # Try to consume the echo

        try:
            echo = self._connection.read(len(bdata))
        except queue.Empty:
            # leave it.
            logging.warning("Timeout when reading echo")
            return

        if echo != bdata:
            # because of autoreload? timing problems? interruption?
            # Leave it.
            logging.warning("Unexpected echo. Expected %s, got %s" % (bdata, echo))
            self._connection.unread(echo)

    def send_message(self, msg):
        if "cwd" not in msg:
            msg["cwd"] = self._cwd

        sys.stdout.write(serialize_message(msg) + "\n")
        sys.stdout.flush()

    def _send_output(self, data, stream_name):
        if not data:
            return
        data = self._transform_output(data)
        msg = BackendEvent(event_type="ProgramOutput", stream_name=stream_name, data=data)
        self.send_message(msg)

    def _transform_output(self, data):
        # Any keypress wouldn't work
        return data.replace(
            "Press any key to enter the REPL. Use CTRL-D to reload.",
            "Press Ctrl-C to enter the REPL. Use CTRL-D to reload.",
        )

    def _execute(self, script, capture_output=False):
        # self._ensure_raw_propmt()

        # send command
        self._connection.write(script.encode(ENCODING) + EOT)
        debug("Wrote " + script + "\n--------\n")

        # fetch command confirmation
        ok = self._connection.read(2)
        debug("GOTOK")
        assert ok == OK, "Expected OK, got %r, followed by %r" % (ok, self._connection.read_all())
        return self._process_until_raw_prompt(capture_output)

    def _execute_without_output(self, script):
        out, err, value = self._execute(script, capture_output=True)
        if out or err:

            print(
                "PROBLEM EXECUTING INTERNAL SCRIPT:\n" + out + err + "\nSCRIPT:\n" + script,
                file=sys.stderr,
            )
            raise RuntimeError("Failed MP script")

        return value

    def _execute_print_expr(self, expr, prelude="", cleanup="", capture_output=False):
        # assuming expr really contains an expression
        # separator is for separating side-effect output and printed value
        script = ""
        if prelude:
            script += prelude + "\n"
        script += "print(%r, repr(%s), sep='', end=%r)" % (
            THONNY_MSG_START.decode(),
            expr,
            THONNY_MSG_END.decode(),
        )

        # assuming cleanup doesn't cause output
        if cleanup:
            script += "\n" + cleanup

        return self._execute(script, capture_output)

    def _evaluate(self, expr, prelude="", cleanup=""):
        _, _, value_repr = self._execute_print_expr(expr, prelude, cleanup)
        debug("GOTVALUE")
        if value_repr is None:
            return None
        else:
            return ast.literal_eval(value_repr)

    def _process_until_initial_raw_prompt(self):
        self._connection.write(RAW_MODE_CMD)
        try:
            self._process_until_raw_prompt()
        except KeyboardInterrupt:
            self._interrupt()

    def _process_until_raw_prompt(self, capture_output=False):
        """
        Forwards output, extracts Thonny message, replaces normal prompts with raw prompts.
        
        This is executed when some code is running or just after requesting raw prompt.
        
        After submitting commands to the raw REPL, the output should be like
        {stdout}\x04\{stderr}\x04\n\>
        In the end of {stdout} there may be \x02{value-for-thonny}
        
        Interrupts will alter the execution, but from the response parsing
        perspective they don't matter as they look like any other exception.
        
        Things get complicated because of soft-reboots, which always end with
        regular prompt. Soft-reboots can occur because of Ctrl+D, machine.soft_reset()
        and even reset button (micro:bit).
        
        Because of soft-reboot we can't assume we'll find the terminating markers for 
        each command.
        
        Output produced by background threads (eg. in WiPy ESP32) cause even more difficulties, 
        because it becomes impossible to say whether we are at prompt and output
        is from another thread or the main thread is running.
        For now I'm ignoring these problems and assume all output comes from the main thread.
         
        """
        # TODO: experiment with Ctrl+C, Ctrl+D, reset
        eot_count = 0
        value = None
        done = False
        output = b""
        out = b""
        err = b""
        debug("proura")
        while not done:
            if (
                self._connection.num_bytes_received == 0
                and not self._ctrl_suggestion_given
                and time.time() - self._startup_time > 1.5
            ):
                self._send_output(
                    "\n"
                    + "Device is busy or does not respond. Your options:\n\n"
                    + "  - check the connection properties;\n"
                    + "  - make sure the device has suitable firmware;\n"
                    + "  - make sure the device is not in bootloader mode;\n"
                    + "  - wait until current work is complete;\n"
                    + "  - use Ctrl+C to interrupt current work.\n",
                    "stderr",
                )
                self._ctrl_suggestion_given = True

            # There may be an input submission waiting
            # and we can't progress without resolving it first
            self._check_for_side_commands()

            # Process input in chunks (max 1 parsing marker per chunk).
            # Prefer whole lines (to reduce the number of events),
            # but don't wait too long for eol.
            output += self._connection.soft_read_until(BLOCK_CLOSERS, timeout=0.05)
            stream_name = "stderr" if eot_count == 1 else "stdout"

            if output.endswith(THONNY_MSG_START):
                debug("MSGSTA: " + str(output))
                output = output[: -len(THONNY_MSG_START)]

                # Low chance of failure (eg. because of precisely timed reboot),
                # therefore it's safe to use big timeout
                temp = self._connection.soft_read_until(THONNY_MSG_END, timeout=3)
                if temp.endswith(THONNY_MSG_END):
                    value = temp[: -len(THONNY_MSG_END)]
                    debug("GOTVALUE: " + str(value))
                else:
                    # failure, restore everything to help diagnosis
                    output = output + THONNY_MSG_START + temp

            elif output.endswith(EOT):
                debug("EOT: " + str(output))
                output = output[: -len(EOT)]
                eot_count += 1
                if eot_count == 2:
                    # Normal completion of the command
                    # big chance of being at the raw prompt
                    temp = self._connection.soft_read_until(RAW_PROMPT, timeout=0.1)
                    if temp == RAW_PROMPT and self._connection.incoming_is_empty():
                        done = True
                    elif temp:
                        # Failure, temp needs to be parsed again
                        self._connection.unread(temp)

            elif output.endswith(FIRST_RAW_PROMPT) and self._connection.incoming_is_empty():
                debug("FIRAPRO: " + str(output))
                output = output[: -len(FIRST_RAW_PROMPT)]
                done = True

            elif (
                output.endswith(NORMAL_PROMPT)
                and self._connection.peek_incoming() == b"\r\n" + FIRST_RAW_PROMPT
            ):
                debug("NOPRO: " + str(output))
                output = output + self._connection.read_until(FIRST_RAW_PROMPT)
                # skip both normal and raw prompt together
                # (otherwise they get processed separately)
                output = output[: -len(NORMAL_PROMPT + b"\r\n" + FIRST_RAW_PROMPT)]
                done = True

            elif output.endswith(NORMAL_PROMPT) and self._connection.incoming_is_empty():
                debug("NOPRO2: " + str(output))
                output = output[: -len(NORMAL_PROMPT)]
                # switch to raw mode and continue
                self._connection.write(RAW_MODE_CMD)

            if output.endswith(FIRST_RAW_PROMPT[:-1]):
                # incomplete raw prompt, wait for more
                pass
            else:
                if capture_output:
                    if stream_name == "stdout":
                        out += output
                    else:
                        assert stream_name == "stderr"
                        err += output
                else:
                    # TODO: deal with partial UTF-8 chars
                    self._send_output(output.decode(ENCODING), stream_name)
                output = b""

        debug("doneproc")
        return (
            out.decode(ENCODING),
            err.decode(ENCODING),
            None if value is None else value.decode(ENCODING),
        )

    def _clear_environment(self):
        # TODO: Ctrl+D in raw repl is perfect for MicroPython
        # but on CircuitPython it runs main.py

        # TODO: which is better:
        # self._execute_async(dedent("""
        #    for name in globals():
        #        if not name.startswith("__"):
        #            del globals()[name]
        # """).strip())
        # or
        self._execute("globals().clear(); __name__ = '__main__'")

    def _check_for_side_commands(self):
        # TODO: do interrupts in reading thread
        # most likely the queue is empty
        if self._command_queue.empty():
            return

        postponed = []
        while not self._command_queue.empty():
            cmd = self._command_queue.get()
            if isinstance(cmd, InputSubmission):
                self._submit_input(cmd.data)
            elif isinstance(cmd, InterruptCommand):
                self._interrupt()
            elif isinstance(cmd, EOFCommand):
                self._soft_reboot(True)
            else:
                postponed.append(cmd)

        # put back postponed commands
        while postponed:
            self._command_queue.put(postponed.pop(0))

    def _check_for_idle_events(self):
        self._send_output(self._connection.read_all().decode(ENCODING, "replace"), "stdout")
        self._check_for_connection_errors()

    def _supports_directories(self):
        # NB! make sure self._cwd is queried first
        return self._cwd is not None

    def _cmd_interrupt(self, cmd):
        self._interrupt()

    def _cmd_cd(self, cmd):
        if len(cmd.args) == 1:
            if not self._supports_directories():
                raise UserError("This device doesn't have directories")

            path = cmd.args[0]
            self._execute("import os as __module_os; __module_os.chdir(%r)" % path)
            self._cwd = self._fetch_cwd()
            return {}
        else:
            raise UserError("%cd takes one parameter")

    def _cmd_Run(self, cmd):
        self._clear_environment()
        assert cmd.get("source")
        self._execute(cmd["source"])
        return {}

    def _cmd_execute_source(self, cmd):
        try:
            # Try to parse as expression
            ast.parse(cmd.source, mode="eval")
            # If it didn't fail then source is an expression
            _, _, value_repr = self._execute_print_expr(cmd.source)
            if value_repr is None:
                value_repr = repr(None)
            return {"value_info": ValueInfo(0, value_repr)}
        except SyntaxError:
            # source is a statement (or invalid syntax)
            self._execute(cmd.source)
            return {}

    def _cmd_get_globals(self, cmd):
        if cmd.module_name == "__main__":
            globs = self._evaluate(
                "{name : repr(value) for (name, value) in globals().items() if not name.startswith('__')}"
            )
        else:
            globs = self._evaluate(
                "{name : repr(getattr(__mod_for_globs, name)) in dir(__mod_for_globs) if not name.startswith('__')}",
                prelude="import %s as __mod_for_globs",
            )
        return {"module_name": cmd.module_name, "globals": globs}

    def _cmd_get_dirs_child_data(self, cmd):
        if "micro:bit" in self._welcome_text.lower():
            data = self._get_dirs_child_data_microbit(cmd)
            dir_separator = ""
        else:
            data = self._get_dirs_child_data_generic(cmd)
            dir_separator = "/"

        return {"node_id": cmd["node_id"], "dir_separator": dir_separator, "data": data}

    def _cmd_write_file(self, cmd):
        def generate_blocks(content_bytes, block_size):
            for i in range(0, len(content_bytes), block_size):
                yield content_bytes[i : i + block_size]

        self._write_file(generate_blocks(cmd["content_bytes"], BUFFER_SIZE), cmd["path"])

        return InlineResponse(
            command_name="write_file", path=cmd["path"], editor_id=cmd.get("editor_id")
        )

    def _internal_path_to_mounted_path(self, path):
        mount_path = self._get_fs_mount()
        if mount_path is None:
            return None

        flash_prefix = self._get_flash_prefix()
        if not path.startswith(flash_prefix):
            return None

        path_suffix = path[len(flash_prefix) :]

        return os.path.join(mount_path, os.path.normpath(path_suffix))

    def _cmd_read_file(self, cmd):
        try:
            content_bytes = b"".join(self._read_file(cmd["path"]))
            error = None
        except Exception as e:
            _report_internal_error()
            error = str(e)
            content_bytes = None

        return {"content_bytes": content_bytes, "path": cmd["path"], "error": error}

    def _cmd_download(self, cmd):
        total_size = 0
        completed_files_size = 0
        remote_files = self._list_remote_files_with_info(cmd["source_paths"])
        target_dir = cmd["target_dir"].rstrip("/").rstrip("\\")

        download_items = []
        for file in remote_files:
            total_size += file["size"]
            # compute filenames (and subdirs) in target_dir
            # relative to the context of the user selected items
            assert file["path"].startswith(file["original_context"])
            path_suffix = file["path"][len(file["original_context"]) :].strip("/").strip("\\")
            target_path = os.path.join(target_dir, os.path.normpath(path_suffix))
            download_items.append((file["path"], target_path, file["size"]))

        def notify(current_file_progress):
            self._check_send_inline_progress(
                cmd, completed_files_size + current_file_progress, total_size
            )

        # replace the indeterminate progressbar with determinate as soon as possible
        notify(0)

        for source, target, size in download_items:
            written_bytes = self._download_file(source, target, notify)
            assert written_bytes == size
            completed_files_size += size

    def _cmd_upload(self, cmd):
        total_size = 0
        completed_files_size = 0
        local_files = self._list_local_files_with_info(cmd["source_paths"])
        target_dir = cmd["target_dir"].rstrip("/")

        upload_items = []
        for file in local_files:
            total_size += file["size"]
            # compute filenames (and subdirs) in target_dir
            # relative to the context of the user selected items
            assert file["path"].startswith(file["original_context"])
            path_suffix = file["path"][len(file["original_context"]) :].strip("/").strip("\\")
            target_path = linux_join_path_parts(target_dir, to_linux_path(path_suffix))
            upload_items.append((file["path"], target_path, file["size"]))

        def notify(current_file_progress):
            self._check_send_inline_progress(
                cmd, completed_files_size + current_file_progress, total_size
            )

        # replace the indeterminate progressbar with determinate as soon as possible
        notify(0)

        for source, target, size in upload_items:
            written_bytes = self._upload_file(source, target, notify)
            assert written_bytes == size
            completed_files_size += size

    def _cmd_editor_autocomplete(self, cmd):
        # template for the response
        result = dict(source=cmd.source, row=cmd.row, column=cmd.column)

        try:
            script = jedi.Script(cmd.source, cmd.row, cmd.column, sys_path=[self._api_stubs_path])
            completions = script.completions()
            result["completions"] = self._filter_completions(completions)
        except Exception:
            result["error"] = "Autocomplete error"

        return result

    def _filter_completions(self, completions):
        # filter out completions not applicable to MicroPython
        result = []
        for completion in completions:
            if completion.name.startswith("__"):
                continue

            parent_name = completion.parent().name
            name = completion.name
            root = completion.full_name.split(".")[0]

            # jedi proposes names from CPython builtins
            if root in self._builtins_info and name not in self._builtins_info[root]:
                continue

            if parent_name == "builtins" and name not in self._builtins_info:
                continue

            result.append({"name": name, "complete": completion.complete})

        return result

    def _cmd_shell_autocomplete(self, cmd):
        source = cmd.source

        # TODO: combine dynamic results and jedi results
        if source.strip().startswith("import ") or source.strip().startswith("from "):
            # this needs the power of jedi
            response = {"source": cmd.source}

            try:
                # at the moment I'm assuming source is the code before cursor, not whole input
                lines = source.split("\n")
                script = jedi.Script(
                    source, len(lines), len(lines[-1]), sys_path=[self._api_stubs_path]
                )
                completions = script.completions()
                response["completions"] = self._filter_completions(completions)
            except Exception:
                traceback.print_exc()
                response["error"] = "Autocomplete error"

            return response
        else:
            # use live data
            match = re.search(
                r"(\w+\.)*(\w+)?$", source
            )  # https://github.com/takluyver/ubit_kernel/blob/master/ubit_kernel/kernel.py
            if match:
                prefix = match.group()
                if "." in prefix:
                    obj, prefix = prefix.rsplit(".", 1)
                    names = self._evaluate("dir(%s)" % obj)
                else:
                    names = self._evaluate("dir()")
            else:
                names = []
                prefix = ""

            completions = []
            for name in names:
                if name.startswith(prefix) and not name.startswith("__"):
                    completions.append({"name": name, "complete": name[len(prefix) :]})

            return {"completions": completions, "source": source}

    def _cmd_dump_api_info(self, cmd):
        "For use during development of the plug-in"

        self._execute(
            dedent(
                """
            def __get_object_atts(obj):
                result = []
                errors = []
                for name in dir(obj):
                    try:
                        val = getattr(obj, name)
                        result.append((name, repr(val), repr(type(val))))
                    except BaseException as e:
                        errors.append("Couldn't get attr '%s' from object '%r', Err: %r" % (name, obj, e))
                return (result, errors)
        """
            )
        )

        for module_name in sorted(self._fetch_builtin_modules()):
            if (
                not module_name.startswith("_")
                and not module_name.startswith("adafruit")
                # and not module_name == "builtins"
            ):
                file_name = os.path.join(
                    self._api_stubs_path, module_name.replace(".", "/") + ".py"
                )
                self._dump_module_stubs(module_name, file_name)

    def _dump_module_stubs(self, module_name, file_name):
        out, err, __ = self._execute("import {0}".format(module_name), capture_output=True)
        if out or err:
            print("FAILED IMPORTING MODULE:", module_name, "\nErr: " + out + err)
            return

        os.makedirs(os.path.dirname(file_name), exist_ok=True)
        with io.open(file_name, "w", encoding="utf-8", newline="\n") as fp:
            if module_name not in [
                "webrepl",
                "_webrepl",
                "gc",
                "http_client",
                "http_client_ssl",
                "http_server",
                "framebuf",
                "example_pub_button",
                "flashbdev",
            ]:
                self._dump_object_stubs(fp, module_name, "")

    def _dump_object_stubs(self, fp, object_expr, indent):
        if object_expr in [
            "docs.conf",
            "pulseio.PWMOut",
            "adafruit_hid",
            "upysh",
            # "webrepl",
            # "gc",
            # "http_client",
            # "http_server",
        ]:
            print("SKIPPING problematic name:", object_expr)
            return

        print("DUMPING", indent, object_expr)
        items, errors = self._evaluate("__get_object_atts({0})".format(object_expr))

        if errors:
            print("ERRORS", errors)

        for name, rep, typ in sorted(items, key=lambda x: x[0]):
            if name.startswith("__"):
                continue

            print("DUMPING", indent, object_expr, name)
            self._send_text_to_shell("  * " + name + " : " + typ, "stdout")

            if typ in ["<class 'function'>", "<class 'bound_method'>"]:
                fp.write(indent + "def " + name + "():\n")
                fp.write(indent + "    pass\n\n")
            elif typ in ["<class 'str'>", "<class 'int'>", "<class 'float'>"]:
                fp.write(indent + name + " = " + rep + "\n")
            elif typ == "<class 'type'>" and indent == "":
                # full expansion only on toplevel
                fp.write("\n")
                fp.write(indent + "class " + name + ":\n")  # What about superclass?
                fp.write(indent + "    ''\n")
                self._dump_object_stubs(fp, "{0}.{1}".format(object_expr, name), indent + "    ")
            else:
                # keep only the name
                fp.write(indent + name + " = None\n")

    def _read_file(self, path):
        # TODO: read from mount when possible
        # file_size = self._get_file_size(path)
        block_size = 512

        self._execute_without_output("__th_fp = open(%r, 'rb')" % path)
        if "binascii" in self._builtin_modules:
            self._execute_without_output("from binascii import hexlify as __temp_hexlify")

        while True:
            self._check_for_interrupt()
            if "binascii" in self._builtin_modules:
                block = binascii.unhexlify(
                    self._evaluate("__temp_hexlify(__th_fp.read(%s))" % block_size)
                )
            else:
                block = self._evaluate("__th_fp.read(%s)" % block_size)
            if block:
                yield block
            if len(block) < block_size:
                break

        self._execute_without_output("__th_fp.close(); del __th_fp")

    def _write_file(self, content_blocks, target_path, notifier=None):
        try:
            result = self._write_file_via_serial(content_blocks, target_path, notifier)
        except ReadOnlyFilesystemError:
            result = self._write_file_via_mount(content_blocks, target_path, notifier)

        self._sync_all_filesystems()
        return result

    def _write_file_via_mount(self, content_blocks, target_path, notifier=None):
        mounted_target_path = self._internal_path_to_mounted_path(target_path)
        with open(mounted_target_path, "wb") as f:
            bytes_written = 0
            for block in content_blocks:
                self._check_for_interrupt()
                bytes_written += f.write(block)
                f.flush()
                os.fsync(f)
                if notifier is not None:
                    notifier(bytes_written)

        return bytes_written

    def _write_file_via_serial(self, content_blocks, target_path, notifier=None):
        # prelude
        try:
            out, err, value = self._execute(
                dedent(
                    """
                __th_path = '{path}'
                __th_written = 0
                __th_f = open(__th_path, 'wb')
                """
                ).format(path=target_path),
                capture_output=True,
            )

            if "readonly" in err.replace("-", "").lower():
                raise ReadOnlyFilesystemError()

            # Define function to allow shorter write commands
            if "binascii" in self._builtin_modules:
                self._execute_without_output(
                    dedent(
                        """
                    from binascii import unhexlify as __th_unhex
                    def __W(x):
                        global __th_written
                        __th_written += __th_f.write(__th_unhex(x))
                """
                    )
                )
            else:
                self._execute_without_output(
                    dedent(
                        """
                    def __W(x):
                        global __th_written
                        __th_written += __th_f.write(x)
                """
                    )
                )

            bytes_sent = 0
            for block in content_blocks:
                self._check_for_interrupt()
                if "binascii" in self._builtin_modules:
                    script = "__W(%r)" % binascii.hexlify(block)
                else:
                    script = "__W(%r)" % block
                self._execute_without_output(script)
                bytes_sent += len(block)
                if notifier is not None:
                    notifier(bytes_sent)

            bytes_received = self._evaluate("__th_written")

            if bytes_received != bytes_sent:
                raise UserError(
                    "Expected %d written bytes but wrote %d" % (bytes_sent, bytes_received)
                )

        finally:
            # clean up
            self._execute(
                dedent(
                    """
                    try:
                        del __W
                        del __th_written
                        del __th_path
                        __th_f.close()
                        del __th_f
                        del __th_unhex
                    except:
                        pass
                """
                )
            )

        return bytes_sent

    def _sync_all_filesystems(self):
        self._execute_without_output(
            dedent(
                """
            try:
                from os import sync as __th_sync
                __th_sync()
                del __th_sync
            except ImportError:
                pass
        """
            )
        )

    def _list_local_files_with_info(self, paths):
        def rec_list_with_size(path):
            result = {}
            if os.path.isfile(path):
                result[path] = os.path.getsize(path)
            elif os.path.isdir(path):
                for name in os.listdir(path):
                    result.update(rec_list_with_size(os.path.join(path, name)))
            else:
                raise RuntimeError("Can't process " + path)

            return result

        result = []
        for requested_path in paths:
            sizes = rec_list_with_size(requested_path)
            for path in sizes:
                result.append(
                    {
                        "path": path,
                        "size": sizes[path],
                        "original_context": os.path.dirname(requested_path),
                    }
                )

        result.sort(key=lambda rec: rec["path"])
        return result

    def _list_remote_files_with_info(self, paths):
        # prepare universal functions
        self._execute_without_output(
            dedent(
                """
            try:
                from os import stat as __th_stat
                
                def __th_getsize(path):
                    return __th_stat(path)[6]
                
                def __th_isdir(path):
                    return __th_stat(path)[0] & 0o170000 == 0o040000
                    
            except ImportError:
                __th_stat = None
                # micro:bit
                from os import size as __th_getsize
                
                def __th_isdir(path):
                    return False
        """
            )
        )

        self._execute_without_output(
            dedent(
                """
            def __th_rec_list_with_size(path):
                result = {}
                if __th_isdir(path):
                    for name in os.listdir(path):
                        result.update(rec_list_with_size(path + "/" + name))
                else:
                    result[path] = __th_getsize(path)
    
                return result
        """
            )
        )

        result = []
        for requested_path in paths:
            sizes = self._evaluate("__th_rec_list_with_size(%r)" % requested_path)
            for path in sizes:
                result.append(
                    {
                        "path": path,
                        "size": sizes[path],
                        "original_context": os.path.dirname(requested_path),
                    }
                )

        result.sort(key=lambda rec: rec["path"])

        self._execute_without_output(
            dedent(
                """
            del __th_stat
            del __th_getsize
            del __th_isdir
            del __th_rec_list_with_size
        """
            )
        )
        return result

    def _get_file_size(self, path):
        if self._supports_directories():
            script = "__module_os.stat(%r)[6]"
        else:
            script = "os.stat(%r)[6]"

        return self._evaluate(script % path, prelude="import os as __module_os")

    def _makedirs(self, path):
        if path == "/":
            return

        mounted_path = self._internal_path_to_mounted_path(path)
        if mounted_path is None:
            self._makedirs_via_serial(path)
        else:
            os.makedirs(mounted_path, exist_ok=True)

    def _makedirs_via_serial(self, path):
        if path == "/":
            return
        path = path.rstrip("/")

        script = (
            dedent(
                """
            __th_path = %r
            import os as __module_os
            parts = __th_path.split('/')
            for i in range(2, len(parts)):
                path = "/".join(parts[:i])
                try:
                    __module_os.stat(path)
                except OSError:
                    # does not exist
                    __module_os.mkdir(path)
        """
            )
            % path
        )

        self._execute(script)

    def _upload_file(self, source, target, notifier):
        target_dir, target_base = linux_dirname_basename(target)
        self._makedirs(target_dir)

        def block_generator():
            with open(source, "rb") as source_fp:
                while True:
                    block = source_fp.read(1024)
                    if block:
                        yield block
                    else:
                        break

        return self._write_file(block_generator(), target, notifier=notifier)

    def _download_file(self, source, target, notifier=None):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        bytes_written = 0
        with open(target, "wb") as out_fp:
            for block in self._read_file(source):
                out_fp.write(block)
                os.fsync(out_fp)
                bytes_written += len(block)
                notifier(bytes_written)

        return bytes_written

    def _get_fs_mount_label(self):
        # This method is most likely required with CircuitPython,
        # so try its approach first
        # https://learn.adafruit.com/welcome-to-circuitpython/the-circuitpy-drive
        result = self._evaluate(
            "__th_result",
            prelude=dedent(
                """
            try:
                from storage import getmount as __th_getmount
                try:
                    __th_result = __th_getmount("/").label
                finally:
                    del __th_getmount
            except ImportError:
                __th_result = None 
            except OSError:
                __th_result = None 
        """
            ),
            cleanup="del __th_result",
        )

        if result is not None:
            return result

        if self._welcome_text is None:
            return None

        """
        # following is not reliable and probably not needed 
        markers_by_name = {"PYBFLASH": {"pyb"}, "CIRCUITPY": {"circuitpython"}}

        for name in markers_by_name:
            for marker in markers_by_name[name]:
                if marker.lower() in self._welcome_text.lower():
                    return name
        """

        return None

    def _get_flash_prefix(self):
        if not self._supports_directories():
            return ""
        elif (
            "LoBo" in self._welcome_text
            or "WiPy with ESP32" in self._welcome_text
            or "PYBLITE" in self._welcome_text
            or "PYBv" in self._welcome_text
            or "PYBOARD" in self._welcome_text.upper()
        ):
            return "/flash/"
        else:
            return "/"

    def _get_fs_mount(self):
        label = self._get_fs_mount_label()
        if label is None:
            return None
        else:
            candidates = find_volumes_by_name(
                self._get_fs_mount_label(),
                # querying A can be very slow
                skip_letters="A",
            )
            if len(candidates) == 0:
                raise RuntimeError("Could not find volume " + self._get_fs_mount_label())
            elif len(candidates) > 1:
                raise RuntimeError("Found several possible mount points: %s" % candidates)
            else:
                return candidates[0]

    def _get_dirs_child_data_microbit(self, cmd):
        """let it be here so micro:bit works with generic proxy as well"""

        assert cmd["paths"] == {""}, "Bad command: " + repr(cmd)
        file_sizes = self._evaluate(
            "{name : __module_os.size(name) for name in __module_os.listdir()}"
        )
        return {"": file_sizes}

    def _get_dirs_child_data_generic(self, cmd):
        return self._evaluate(
            "__th_result",
            prelude=dedent(
                """
                import os as __module_os
                # Init all vars, so that they can be deleted
                # even if the loop makes no iterations
                __th_result = {}
                __th_path = None
                __th_st = None 
                __th_children = None
                __th_name = None
                __th_real_path = None
                __th_full = None
                
                for __th_path in %(paths)r:
                    __th_real_path = __th_path or '/'
                    __th_children = {}
                    for __th_name in __module_os.listdir(__th_real_path):
                        if __th_name.startswith('.') or __th_name == "System Volume Information":
                            continue
                        __th_full = (__th_real_path + '/' + __th_name).replace("//", "/")
                        # print("processing", __th_full)
                        __th_st = __module_os.stat(__th_full)
                        if __th_st[0] & 0o170000 == 0o040000:
                            # directory
                            __th_children[__th_name] = None
                        else:
                            __th_children[__th_name] = __th_st[6]
                            
                    __th_result[__th_path] = __th_children                            
            """
            )
            % {"paths": cmd.paths},
            cleanup=dedent(
                """
                del __module_os
                del __th_st
                del __th_children
                del __th_name
                del __th_path
                del __th_full
            """
            ),
        )

    def _check_for_connection_errors(self):
        self._connection._check_for_error()

    def _on_connection_closed(self):
        self._send_output(
            "\n" + "Connection closed. Use 'Run → Stop / Restart' to reconnect." + "\n", "stderr"
        )
        sys.exit(EXPECTED_TERMINATION_CODE)


class ExecutionError(Exception):
    pass


def _report_internal_error():
    print("PROBLEM WITH THONNY'S BACK-END:\n", file=sys.stderr)
    traceback.print_exc()


def parse_api_information(file_path):
    with tokenize.open(file_path) as fp:
        source = fp.read()

    tree = ast.parse(source)

    defs = {}

    # TODO: read also docstrings ?

    for toplevel_item in tree.body:
        if isinstance(toplevel_item, ast.ClassDef):
            class_name = toplevel_item.name
            member_names = []
            for item in toplevel_item.body:
                if isinstance(item, ast.FunctionDef):
                    member_names.append(item.name)
                elif isinstance(item, ast.Assign):
                    # TODO: check Python 3.4
                    "TODO: item.targets[0].id"

            defs[class_name] = member_names

    return defs


def linux_dirname_basename(path):
    if path == "/":
        return ("/", "")

    path = path.rstrip("/")
    return path.rsplit("/", maxsplit=1)


def linux_join_path_parts(left, right):
    return left.rstrip("/") + "/" + right.strip("/")


def to_linux_path(path):
    return path.replace("\\", "/")


class ReadOnlyFilesystemError(RuntimeError):
    pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", type=lambda s: True if s == "True" else False)
    parser.add_argument("--port", type=str)
    parser.add_argument("--url", type=str)
    parser.add_argument("--password", type=str)
    parser.add_argument("--api_stubs_path", type=str)
    args = parser.parse_args()

    port = None if args.port == "None" else args.port
    try:
        if port is None:
            # remain busy
            while True:
                time.sleep(1000)
        elif port == "webrepl":
            from thonny.plugins.micropython.webrepl_connection import WebReplConnection

            connection = WebReplConnection(args.url, args.password)
        else:
            from thonny.plugins.micropython.serial_connection import SerialConnection

            connection = SerialConnection(port, BAUDRATE)

        vm = MicroPythonBackend(connection, clean=args.clean, api_stubs_path=args.api_stubs_path)

    except ConnectionFailedException as e:
        text = "\n" + str(e) + "\n"
        msg = BackendEvent(event_type="ProgramOutput", stream_name="stderr", data=text)
        sys.stdout.write(serialize_message(msg) + "\n")
        sys.stdout.flush()
