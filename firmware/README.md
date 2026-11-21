# Firmware builds and publishing

Firmware projects live under `firmware/<board-id>/`. The directory name is the
board identifier used by the web and CLI flashers. Each project supplies a
`board.json` descriptor and its build tools.

Bundled images are stored in `frontend/firmware/<board-id>/`, with a shared
`frontend/firmware/manifest.json` catalog containing the shared firmware version, board output
images, SHA-256 hashes, chip and flashing settings. Both flashers use this catalog.

## Firmware version

[VERSION](VERSION) defines one firmware release version for **all boards**. It is
independent of the server's root `VERSION` file. Board projects include
[version.cmake](version.cmake) before ESP-IDF's `project.cmake`, which sets
`PROJECT_VER` and tracks the file so version changes trigger reconfiguration.
The sink reports the version embedded in its application descriptor. I2S output
applies volume changes as a 150 ms sample ramp after SBC decoding.

To release firmware, change `firmware/VERSION`, rebuild every board/output, and
publish the images. The publisher rejects builds whose embedded version differs
from that file. The catalog stores `firmware_version` once at the top level;
both flashers use it for every board. Publishing a new version starts a fresh
catalog to avoid labeling older board images with the new version. Publish all
board builds for the release before committing the catalog and images.

## Publishing web firmware

For ESP32-WROOM, activate the pinned ESP-IDF v6.1.1 environment and run from the
repository root:

```sh
bash firmware/esp32-wroom/tools/build_web_firmware.sh
```

The script applies the SDK patches, builds Bluetooth and PCM5102/I2S variants in
separate temporary directories, then publishes both images. The first build
downloads the managed cJSON dependency. Network credentials are provisioned
separately and are never part of the compiled firmware.

Commit `frontend/firmware/esp32-wroom/` and the updated catalog with the release.
The Docker image includes the complete firmware tree. See the sections below for
board metadata, manifest structure and publishing existing build directories.

## Build and flash manually

Use an **original ESP32**, such as an ESP32-WROOM board. The Bluetooth output
requires Bluetooth Classic A2DP. No PSRAM is required. Build and flash from a
computer with USB access to the board; the server container does not flash it.

1. Install ESP-IDF and activate its environment (`export.sh`). The bundled patch
   tool targets **ESP-IDF v6.1.1**, pinned to revision
   `051e4c4eb20346e043cdd47bc388fde75ff8af63`. Use a dedicated SDK checkout:
   the tool modifies its Bluetooth and network sources.
2. From the repository root, apply the SDK patches and build without credentials:

   ```sh
   python3 firmware/esp32-wroom/tools/apply_idf_patch.py "$IDF_PATH"
   cd firmware/esp32-wroom
   idf.py set-target esp32
   idf.py build
   idf.py -p /dev/ttyUSB0 flash
   ```

   Replace `/dev/ttyUSB0` with your board's serial port. Bluetooth output is the
   default. For a PCM5102 build, select the output in `idf.py menuconfig` under
   **Audio sink hardware** before building. Default I2S pins are BCLK **26**,
   LRCLK **25**, and DOUT **22**; provisioning can override these pins.
3. From the repository root, run the configuration-only tool with the Python
   environment installed in the [CLI setup instructions](../README.md#command-line-setup-linux-macos-and-windows):

   ```sh
   .flash-venv/bin/python tools/cli-flasher/flash.py --configure-only
   ```

   On Windows, use `.\.flash-venv\Scripts\python.exe` instead. Select the board
   and installed output type, then enter Wi-Fi credentials, server address/TCP port, any I2S
   pins, serial port and baud rate. The password prompt is hidden. The tool writes
   only the `sink_setup` partition at `0x310000` and resets the board; it keeps
   your custom firmware and Bluetooth pairing. Settings pass through a private
   temporary file that is removed afterward. No credentials are compiled into
   firmware or saved in the checkout.
4. After writing settings, `flash.py --configure-only` offers to approve and name
   the newly connected device. You can also complete approval on the server's
   `/setup` page and close the CLI with **Ctrl+C** after the settings write.
   Existing device approval is preserved. Then open `/admin` and configure its
   output. For Bluetooth, choose and connect the speaker. Select a source on the
   listening page.

Build only with the pinned ESP-IDF v6.1.1 checkout. Use a separate build directory
and configuration for each output. See [the SDK patch notes](esp32-wroom/patches/README.md).

To update firmware, pull the release, reapply the patch tool to the supported SDK,
rebuild and flash again. Normal `idf.py flash` leaves the setup partition intact;
run `flash.py --configure-only` to change Wi-Fi/server settings or after erasing flash. Use a
separate build configuration for each output type.

## Published files

```text
frontend/firmware/
  manifest.json
  esp32-wroom/
    bluetooth.bin
    i2s.bin
```

Commit the board images and `manifest.json` together. Docker includes the complete
`frontend/firmware/` tree; the web and CLI flashers read the same catalog.

The manifest has `schema_version: 1`, a shared `firmware_version`, and a `boards`
object keyed by board identifier. Each board entry contains its descriptor and
an `images` object keyed by output mode. Each image records its relative asset
URL, byte size and SHA-256 hash. Publishing a board replaces its catalog entry
and preserves other entries for the same firmware release.

## Board descriptor

[esp32-wroom/board.json](esp32-wroom/board.json) defines:

- `name`: the board label shown in both flashers.
- `chip` and `esptool_chip`: the browser loader's chip name and esptool target.
- `idf`: the SDK version used by this firmware project.
- `outputs`: output identifiers and their display names.
- `flash`: size, mode, frequency, merged image address and bootloader/partition
  table/application offsets.
- `config_format`, `config_offset` and `config_size`: configuration partition
  format, address and size. The `sinkcfg1` format contains the `SINKCFG1` header,
  a little-endian JSON length, the UTF-8 settings JSON and erased-byte padding.
- `i2s`: default pins and valid output GPIOs for setup validation.

Keep the descriptor's memory addresses consistent with the project's partition
table. The flashers use the selected board's settings for image selection,
configuration encoding and writes. The browser checks the connected chip before
writing; esptool checks the target supplied by the CLI.

## Publish existing builds

After building the output variants, use the shared publisher from the repository
root with the ESP-IDF Python environment active:

```sh
python firmware/tools/publish_web_firmware.py \
  --board esp32-wroom \
  --bluetooth /absolute/path/to/bluetooth-build \
  --i2s /absolute/path/to/i2s-build
```

Supply a build for each output declared by `board.json`. The publisher verifies
each output's project directory, SDK configuration, target and embedded version
against the shared `firmware/VERSION` file, then merges the bootloader, partition table and application binary
using the descriptor's offsets. It checks that the merged image ends before the
setup partition, computes hashes, and updates the catalog.

For manual builds and subsequent `idf.py flash` updates, the configuration stays
in the separate setup partition. Use
`python tools/cli-flasher/flash.py --configure-only` to provision settings without
replacing your build. Select the matching board and installed output mode.
