#!/usr/bin/env python3
"""Flash a selected board with the bundled firmware images. No ESP-IDF installation needed."""
import argparse
from dataclasses import dataclass, field
import getpass
import hashlib
from http.client import HTTPException
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
import warnings
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
BAUD_RATES = (115200, 230400, 460800, 921600)
MAC = re.compile(r'\bMAC:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b', re.IGNORECASE)


@dataclass
class Settings:
    output: str
    ssid: str
    password: str = field(repr=False)
    server: str
    board: str
    port: int = 8765
    bclk: int = 26
    lrclk: int = 25
    dout: int = 22


def firmware_release():
    manifest = json.loads((ROOT / 'frontend/firmware/manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or not manifest.get('boards') or not manifest.get('firmware_version'):
        raise ValueError('Unsupported or empty firmware catalog.')
    return manifest


def firmware_catalog():
    return firmware_release()['boards']


def board_definition(board_id):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', board_id):
        raise ValueError('Invalid board identifier.')
    board = firmware_catalog().get(board_id)
    if not board or board.get('config_format') != 'sinkcfg1':
        raise ValueError('Choose a board with a supported configuration format.')
    return board


def choose_board():
    boards = firmware_catalog()
    print(f'\nFirmware: {firmware_release()["firmware_version"]}\nBoards:')
    choices = list(boards)
    for index, ident in enumerate(choices, 1):
        board = boards[ident]
        print(f'  {index}. {board["name"]}')
    while True:
        selected = prompt('Board number or identifier', '1' if len(choices) == 1 else None)
        ident = choices[int(selected)-1] if selected.isdigit() and 1 <= int(selected) <= len(choices) else selected
        if ident in boards:
            return ident, board_definition(ident)
        print('Choose a listed board.')


def validate_settings(settings):
    def control(value):
        return any(ord(char) < 32 for char in value)

    board = board_definition(settings.board)
    if settings.output not in board['outputs']:
        raise ValueError('Choose an output supported by the selected board.')
    if not settings.ssid or len(settings.ssid.encode('utf-8')) > 32 or control(settings.ssid):
        raise ValueError('Wi-Fi name must contain 1–32 UTF-8 bytes without control characters.')
    length = len(settings.password.encode('utf-8'))
    if control(settings.password) or length > 64 or (length and length < 8):
        raise ValueError('Use an open network or a Wi-Fi password of 8–64 bytes.')
    if length == 64 and not re.fullmatch(r'[a-fA-F0-9]{64}', settings.password):
        raise ValueError('A 64-byte Wi-Fi password must be a hexadecimal PSK.')
    try:
        address = ipaddress.IPv4Address(settings.server)
    except ipaddress.AddressValueError:
        raise ValueError('Enter the audio server’s LAN IPv4 address.') from None
    if str(address) in ('0.0.0.0', '255.255.255.255'):
        raise ValueError('Enter the audio server’s LAN IPv4 address.')
    if not isinstance(settings.port, int) or not 1 <= settings.port <= 65535:
        raise ValueError('The server TCP port must be between 1 and 65535.')
    pins = (settings.bclk, settings.lrclk, settings.dout)
    if len(set(pins)) != 3 or any(pin not in board['i2s']['output_pins'] for pin in pins):
        raise ValueError('Choose three distinct output GPIOs supported by the selected board.')


def configuration_image(settings):
    """Same partition layout and JSON fields as frontend/setup-config.js."""
    validate_settings(settings)
    payload = json.dumps({key: getattr(settings, key) for key in
                          ('ssid', 'password', 'server', 'port', 'bclk', 'lrclk', 'dout')},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > 2048:
        raise ValueError('Configuration is too large.')
    header = b'SINKCFG1' + struct.pack('<I', len(payload))
    size = board_definition(settings.board)['config_size']
    if len(header) + len(payload) > size:
        raise ValueError('Configuration exceeds the board’s setup partition.')
    return header + payload + b'\xff' * (size - len(header) - len(payload))


def bundled_firmware(board_id, output):
    board = board_definition(board_id)
    image = board['images'][output]
    if output not in board['outputs'] or image['url'] != f'assets/firmware/{board_id}/{output}.bin':
        raise ValueError('Unexpected firmware path in manifest.')
    path = ROOT / 'frontend/firmware' / board_id / f'{output}.bin'
    data = path.read_bytes()
    if (len(data) != image['size'] or board['flash']['offset'] + len(data) > board['config_offset'] or
            hashlib.sha256(data).hexdigest() != image['sha256']):
        raise ValueError('Firmware verification failed. Obtain a complete, matching release.')
    return path, firmware_release()['firmware_version']


def prompt(label, default=None):
    suffix = f' [{default}]' if default is not None else ''
    value = input(f'{label}{suffix}: ').strip()
    return value if value else (str(default) if default is not None else '')


def yes_no(label, default=True):
    while True:
        value = input(label + (' [Y/n]: ' if default else ' [y/N]: ')).strip().lower()
        if not value:
            return default
        if value in ('y', 'yes', 'n', 'no'):
            return value in ('y', 'yes')
        print('Enter y or n.')


def integer(label, default):
    while True:
        try:
            return int(prompt(label, default))
        except ValueError:
            print('Enter a whole number.')


def collect_settings(board_id, board):
    while True:
        output = prompt('Output (' + ' or '.join(board['outputs']) + ')', next(iter(board['outputs']))).lower()
        ssid = prompt('Wi-Fi network (2.4 GHz)')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                password = getpass.getpass('Wi-Fi password (empty for an open network): ')
            except getpass.GetPassWarning:
                raise RuntimeError('Run this script in an interactive terminal so the password can be hidden.') from None
        server = prompt('Audio server LAN IPv4 address')
        port = integer('Audio server TCP port', 8765)
        defaults = board['i2s']['default_pins']
        pins = tuple(defaults[key] for key in ('bclk', 'lrclk', 'dout'))
        if output == 'i2s':
            pins = tuple(integer(label, default) for label, default in
                         (('BCLK GPIO', defaults['bclk']), ('LRCLK GPIO', defaults['lrclk']), ('Data out GPIO', defaults['dout'])))
        settings = Settings(output, ssid, password, server, board_id, port, *pins)
        try:
            validate_settings(settings)
            return settings
        except ValueError as exc:
            print(f'Invalid settings: {exc}')
            if not yes_no('Enter settings again?'):
                raise SystemExit(1)


def available_ports():
    from serial.tools import list_ports
    return sorted((port for port in list_ports.comports()
                   if (port.description or '').strip().lower() not in ('', 'n/a') or
                   getattr(port, 'vid', None) is not None), key=lambda port: port.device)


def choose_port():
    ports = available_ports()
    print('\nSerial ports:')
    for index, port in enumerate(ports, 1):
        print(f'  {index}. {port.device}: {port.description}')
    if not ports:
        print('  No identified ports found. Connect the board or enter its device path manually.')
    while True:
        selected = prompt('Serial port number or device path', '1' if len(ports) == 1 else None)
        if selected.isdigit() and 1 <= int(selected) <= len(ports):
            return ports[int(selected) - 1].device
        if selected and not selected.isdigit():
            return selected
        print('Choose a listed number, or enter a port such as COM3 or /dev/ttyUSB0.')


def flash_command(port, baud, firmware, config, board):
    flash = board['flash']
    return [sys.executable, '-m', 'esptool', '--chip', board['esptool_chip'], '--port', port,
            '--baud', str(baud), '--before', 'default-reset', '--after', 'hard-reset',
            'write-flash', '--flash-mode', flash['mode'], '--flash-size', flash['size'], '--flash-freq', flash['frequency'],
            hex(flash['offset']), str(firmware), hex(board['config_offset']), str(config)]


def run_flasher(command):
    """Stream progress and retain only the chip MAC for the approval step."""
    ident = None
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf-8', errors='replace') as process:
        try:
            for line in process.stdout:
                print(line, end='', flush=True)
                match = MAC.search(line)
                if match:
                    ident = 'esp32-' + match.group(1).replace(':', '').lower()
            code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    if code:
        raise RuntimeError('Flashing failed. Close serial monitors, check the cable/port permissions, '
                           'and try a lower baud rate. Some boards need BOOT held while connecting.')
    return ident


def configure_device(port, baud, settings):
    board = board_definition(settings.board)
    # Only this data partition is written: application code and Bluetooth NVS stay intact.
    with tempfile.TemporaryDirectory(prefix='radio-configure-') as directory:
        config = Path(directory) / 'settings.bin'
        config.write_bytes(configuration_image(settings))
        config.chmod(0o600)
        return run_flasher([sys.executable, '-m', 'esptool', '--chip', board['esptool_chip'], '--port', port,
                            '--baud', str(baud), '--before', 'default-reset', '--after', 'hard-reset',
                            'write-flash', hex(board['config_offset']), str(config)])


def flash_device(port, baud, settings):
    firmware, version = bundled_firmware(settings.board, settings.output)
    print(f'\nBoard: {board_definition(settings.board)["name"]}\nFirmware: {version}\nOutput: {settings.output}\nSerial port: {port}\nBaud rate: {baud}')
    print(f'Wi-Fi network: {settings.ssid}\nAudio server: {settings.server}:{settings.port}')
    # TemporaryDirectory is private on POSIX; it also works with Windows serial tools.
    with tempfile.TemporaryDirectory(prefix='radio-flash-') as directory:
        config = Path(directory) / 'settings.bin'
        config.write_bytes(configuration_image(settings))
        config.chmod(0o600)
        ident = run_flasher(flash_command(port, baud, firmware, config, board_definition(settings.board)))
    print('\nFlash complete. The board has been reset.')
    return ident


def setup_base(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Setup URL must be an HTTP or HTTPS URL without embedded credentials.')
    if parsed.query or parsed.fragment:
        raise ValueError('Setup URL must not contain a query or fragment.')
    path = parsed.path.rstrip('/')
    if path.endswith('/setup'):
        path = path[:-6]
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


def api(base, path, body=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = Request(base + path, data=data,
                      headers={'Content-Type': 'application/json'} if data is not None else {})
    try:
        with urlopen(request, timeout=5) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc)
            message = detail.get('error', {}).get('message') or detail.get('detail') or f'HTTP {exc.code}'
        except (ValueError, AttributeError):
            message = f'HTTP {exc.code}'
        raise RuntimeError(str(message)) from None
    except HTTPException:
        raise RuntimeError('This port did not return a valid HTTP response.') from None



def resolve_setup(settings):
    candidate = f'http://{settings.server}:{settings.port}/setup'
    while True:
        try:
            base = setup_base(candidate)
            result = api(base, '/api/setup/devices')
            if not isinstance(result, dict) or not isinstance(result.get('devices'), list):
                raise ValueError('This address is not the audio server setup API.')
            return base
        except (OSError, ValueError, RuntimeError) as exc:
            print(f'Could not reach setup at {candidate}: {exc}')
            print('The audio TCP port and website port may differ.')
            candidate = prompt("Actual setup URL (or 'skip' to finish later)",
                               f'http://{settings.server}:8080/setup')
            if candidate.lower() == 'skip':
                return None


def prepare_approval(base, ident):
    status = api(base, f'/api/setup/devices/{ident}/status')
    if status['approved']:
        print('Returning this flashed sink to setup approval.')
        api(base, f'/api/devices/{ident}/unpair', {})


def approve_device(base, ident):
    print('Waiting for the board to join Wi-Fi and connect to the audio server…')
    deadline = time.monotonic() + 30
    while True:
        status = api(base, f'/api/setup/devices/{ident}/status')
        if status['approved']:
            print('This device is already approved. You can rename it in Administration.')
            return
        if status['online']:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('The device has not connected. Check Wi-Fi and the server address.')
        time.sleep(1)
    while True:
        name = prompt('Room or sink name')
        if 1 <= len(name) <= 64 and not any(ord(char) < 32 for char in name):
            break
        print('Enter 1–64 printable characters.')
    result = api(base, f'/api/setup/devices/{ident}/approve', {'display_name': name})
    print(f'Approved: {result["display_name"]}\nAdministration: {base}/admin')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baud', type=int, choices=BAUD_RATES, default=460800, help='Flash speed (default: 460800)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--flash-only', action='store_true', help='Skip CLI approval; approve later at /setup')
    mode.add_argument('--configure-only', action='store_true', help='Write settings only; preserve installed firmware, Bluetooth pairing and approval')
    parser.add_argument('--list-ports', action='store_true', help='List serial ports and exit')
    args = parser.parse_args(argv)
    if importlib.util.find_spec('esptool') is None:
        parser.exit(1, 'Install the flasher dependencies first: python -m pip install -r tools/cli-flasher/requirements.txt\n')
    if args.list_ports:
        for port in available_ports():
            print(f'{port.device}: {port.description}')
        return 0
    flashed = False
    setup_page = None
    setup_checked = False
    base = None
    try:
        if args.configure_only:
            print("Configure the installed firmware. Choose the output type already flashed on this board.")
        board_id, board = choose_board()
        settings = collect_settings(board_id, board)
        port = choose_port()
        while True:
            flashed = False
            if args.configure_only:
                ident = configure_device(port, args.baud, settings)
                flashed = True
                print('\nSettings written. The board has been reset; installed firmware and Bluetooth pairing are preserved.')
            else:
                ident = flash_device(port, args.baud, settings)
                flashed = True
            print('You can also continue approval on the server’s /setup webpage.')
            print('Press Ctrl+C to close this program; the completed flash will not be affected.')
            if not setup_checked:
                base = resolve_setup(settings)
                setup_checked = True
            setup_page = base + '/setup' if base else None
            if setup_page:
                print(f'Setup page: {setup_page}')
            if ident:
                print(f'Device identifier: {ident}')
            if ident and base and not args.configure_only:
                try:
                    prepare_approval(base, ident)
                except (OSError, ValueError, RuntimeError) as exc:
                    print(f'Could not prepare server approval: {exc}\nIf this board was already approved, unpair it in Admin before approving again.')
            elif not ident:
                print('The flasher did not report a device identifier. Review the device in Setup.')
            if not args.flash_only and ident and base and yes_no('Approve and name this device now?'):
                try:
                    approve_device(base, ident)
                except (OSError, ValueError, RuntimeError) as exc:
                    print(f'Settings written, but approval was not completed: {exc}\nContinue at {base}/setup')
            action = 'Configure' if args.configure_only else 'Flash'
            if not yes_no(f'{action} another device with the same network/output settings?', default=False):
                return 0
            port = choose_port()
    except (EOFError, KeyboardInterrupt):
        if flashed:
            completed = 'Configuration' if args.configure_only else 'Flashing'
            print(f'\nClosed. {completed} is complete; continue approval at ' + (setup_page or 'the server’s /setup webpage') + '.')
            return 0
        print('\nCancelled.', file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
