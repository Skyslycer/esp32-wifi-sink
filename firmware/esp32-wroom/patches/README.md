# Pinned ESP-IDF patches

The current firmware targets **ESP-IDF v6.1.1**, commit
`051e4c4eb20346e043cdd47bc388fde75ff8af63` (original ESP32 target).
Use a dedicated SDK checkout because these patches change SDK sources.

From a directory outside this repository, install the SDK and ESP32 toolchain:

```sh
git clone --branch v6.1.1 --depth 1 --recursive https://github.com/espressif/esp-idf.git esp-idf-v6.1.1
cd esp-idf-v6.1.1
./install.sh esp32
. ./export.sh
```

Then, from the project checkout root:

```sh
python3 firmware/esp32-wroom/tools/apply_idf_patch.py "$IDF_PATH"
```

The helper checks the exact SDK revision and preflights the entire ordered stack
in temporary copies before modifying the SDK. It accepts already-applied prefixes
and can be rerun. An unknown revision or conflicting source changes cause it to
stop before applying patches. Initialize the SDK submodules before running it.

## v6.1.1 stack

| Patch | Purpose |
| --- | --- |
| `lwip-nodelay-allocation` | Avoid full-MSS reservations for short TCP NODELAY writes while preserving contiguous transmit buffers. |
| `avrc-playback-status` | Expose playback-status updates needed by the firmware's AVRCP integration. |
| `bt-linker-paths` | Use relative linker-fragment paths so SDK paths containing spaces work. |
| `external-sbc-bound` | Limit the external SBC transmit queue to two packets and expose its counters. |
| `fixed-sbc-endpoint` | Permit the exact external-source profile (44.1 kHz, stereo, 16 blocks, 8 subbands, loudness, bitpool 52) through the new capability validator. Other source/sink checks remain intact. |

These patches were rebased against v6.1.1 in that order. The fixed-profile
exception avoids advertising SBC encoder formats the server does not produce;
without it the new mandatory-format check rejects endpoint registration and
Bluetooth remains uninitialized. Bluetooth uses only the external SBC codec. The SDK revision and
component manifest accept only ESP-IDF v6.1.1.

## Build dependencies

The firmware uses managed `espressif/cjson` 1.7.19 (with a committed dependency
lock) and PSA Crypto hashing with error handling. The first build needs Internet
access to download the managed component. Downloaded `managed_components/` files
are excluded from Git.

Bluetooth uses `sdkconfig.defaults`; PCM5102 uses
`SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.pcm5102"` with a separate build
directory and configuration file. All builds read network settings from the
dedicated `sink_setup` partition, written by the browser or CLI provisioning tools.

## Validation

Both output variants compile and link with the v6.1.1 ESP32 toolchain, without
embedded network credentials. Both fit the existing application partition.
Native sanitizer checks cover external SBC queue bounds, patch-prefix
preflight, endpoint capability validation, control dispatch (including hash
failures), JSON validation and TCP framing. Both web firmware images were rebuilt
and republished after this cleanup. Hardware playback was not retested as part
of this cleanup.
