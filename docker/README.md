# Docker deployment

The image runs on Linux amd64. It includes the server, website, the board firmware catalog and images, FFmpeg,
go-librespot and the project's patched Shairport Sync receiver. ARM images are
not provided because the pinned Spotify receiver currently targets amd64.

## Build and run locally

From the repository root, with Docker Engine and the Compose plugin installed:

```sh
docker compose up -d --build
docker compose logs -f radio
```

Open `http://<host-LAN-IP>:8080/setup`. Configure ESP32 devices to connect to that
same host's LAN IPv4 address on TCP 8765. Container health is checked through
`/api/health`. To run the same startup check used before GHCR publishing:

```sh
python3 docker/smoke_test.py esp32-wifi-sink:local
python3 docker/smoke_test.py esp32-wifi-sink:local --setup-https
```

This uses a temporary container and volume, verifies the website, firmware hashes,
application UID and D-Bus authentication, then removes its test resources.

The supplied configuration uses host networking. Spotify and AirPlay advertise
receivers on the LAN, and WFAS listens for multicast discovery on
239.255.0.1:9091. Receiver ports are allocated dynamically, so mapping only the web
and audio ports through a bridge network is insufficient. Do not add `ports:`
when using host networking. Use a Linux host with multicast connectivity to the
senders. Docker Desktop networking is not a supported deployment target.

The container starts its own D-Bus and Avahi, then launches the app as an
unprivileged UID/GID. It does not require systemd, privileged mode, audio hardware,
host D-Bus sockets or USB devices. The `/run` and `/tmp` mounts are temporary;
application files are read-only. The root supervisor is required at startup to
set the data directory ownership and launch discovery daemons; do not override
Compose's user setting with `user:`.

On hosts with an existing mDNS daemon, Avahi may log a warning about another
stack. Both must permit sharing UDP 5353 and the LAN must allow multicast. The
container does not reconfigure the host daemon. If discovery fails, check host
firewall rules and interface selection rather than disabling host services.

## Configuration and data

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `PUID` | `10001` | Application user ID; must be nonzero. |
| `PGID` | `10001` | Application group ID; must be nonzero. |
| `SINK_HTTP_PORT` | `8080` | HTTP website/API listener on the host. |
| `SINK_TCP_PORT` | `8765` | ESP32 audio/control listener on the host. |
| `SINK_BIND_ADDRESS` | `0.0.0.0` | Bind address for HTTP, optional HTTPS and audio TCP listeners. |
| `SINK_SPOTIFY_ENABLED` | `true` | Make the included Spotify receiver available. |
| `SINK_AIRPLAY_ENABLED` | `true` | Make AirPlay available and start private D-Bus/Avahi. |
| `SINK_SETUP_HTTPS_ENABLED` | `false` | Add a self-signed HTTPS listener for browser setup; HTTP stays available. |
| `SINK_SETUP_HTTPS_PORT` | `8443` | Optional HTTPS port; must differ from HTTP and audio TCP ports. |
| `SINK_SETUP_HTTPS_HOSTS` | automatic | Comma-separated LAN IPs/hostnames used to open setup; included in the certificate. Localhost is always included. |
| `SINK_MDNS_INTERFACES` | all | Optional comma-separated host interface names for Avahi, such as `eno1`. Does not restrict Spotify or WFAS discovery. |

Add optional environment variables under the service's `environment:` section.
The local Compose file accepts `PUID`, `PGID`, `SINK_HTTP_PORT`, `SINK_TCP_PORT`,
`SINK_SPOTIFY_ENABLED` and `SINK_AIRPLAY_ENABLED` from a shell or Compose `.env`
file. The registry example sets those values directly in YAML. Both files accept
the three `SINK_SETUP_HTTPS_*` variables from a shell or Compose `.env` file.
Per-sink input switches remain in Admin settings; disabling a mode globally makes
it unavailable for all sinks.

To disable Spotify and AirPlay globally, add these values to a Compose `.env`
file beside the local Compose file, or set them directly in the registry
example's `environment:` section:

```dotenv
SINK_SPOTIFY_ENABLED=false
SINK_AIRPLAY_ENABLED=false
```

Recreate the container with `docker compose up -d` (add
`-f compose.example.yaml` immediately after `docker compose` for the registry
example). Spotify will not start or advertise receivers. AirPlay will not start,
and neither will the container's D-Bus or Avahi processes. Both modes default to
`true` when the variables are omitted. Their binaries remain in the image.

WFAS runs inside the Python server. It does not advertise this server as a
receiver; it listens for sender discovery beacons on multicast
`239.255.0.1:9091`, then connects to a selected sender. It needs no Avahi or D-Bus,
but automatic sender discovery still requires multicast connectivity.

All persistent application data lives at `/data`: device approval and preferences,
favorites, and Spotify/AirPlay credentials. The default Compose file uses a named
volume. For a bind mount, create a private directory and mount it at
`/data`. Give the application UID/GID read/write access. Startup changes ownership
of the mount root only, not existing files or directory ACLs. Keep the same IDs
across updates and back up the entire directory while the container is stopped.

## Optional HTTPS for browser flashing

HTTPS is disabled by default. You can continue using your own reverse proxy, or
set these environment variables on the container:

```dotenv
SINK_SETUP_HTTPS_ENABLED=true
SINK_SETUP_HTTPS_HOSTS=192.168.1.20,radio.local
# Optional; defaults to 8443:
SINK_SETUP_HTTPS_PORT=8443
```

Replace the IP/name with the address you use in the browser, without a scheme,
path or port. When hosts are omitted, the server detects its hostname and IPs;
set them explicitly on hosts with multiple interfaces or when using a DNS alias.
Rebuild the image for a local deployment, then recreate the container. For a
published image, update to a release containing this feature and recreate it.

Open `https://<server>:8443/setup` in desktop Chrome or Edge. HTTP on 8080 and the
ESP32 audio port stay available. The HTTPS listener serves the same application,
so setup APIs and firmware downloads stay on the HTTPS origin and share the same
device state. HTTP is not redirected to HTTPS.

A self-signed certificate is generated in `/data/tls/setup.crt`, with its private
key at `/data/tls/setup.key`. Import the public certificate into the browser/OS
trust store on the computer doing the flashing, then reload the HTTPS setup page.
Self-signed certificates are not automatically trusted; merely bypassing a
certificate warning may still leave browser features unavailable. See
[Chrome certificate trust](https://www.chromium.org/Home/chromium-security/root-ca-policy/).
You can copy the public certificate out of the data volume or download it from
`http://<server>:8080/api/setup/certificate`; compare its SHA-256 fingerprint
with the one in the container logs before trusting it. Do not share the private key.

The certificate is reused across restarts and container replacement as long as
`/data` is preserved. It is replaced at startup if its configured hosts change,
it is invalid, or fewer than 30 days remain of its one-year validity. After a
replacement, trust the new public certificate on the flashing computer. Changing
`SINK_SETUP_HTTPS_ENABLED` back to `false` removes the HTTPS listener on restart
and leaves the saved certificate on disk. Approval still works over LAN HTTP;
USB belongs to the browser computer rather than the server.

## Run the published image

[compose.example.yaml](../compose.example.yaml) uses
`ghcr.io/skyslycer/esp32-wifi-sink:latest` with host networking and a persistent
Docker volume:

```sh
docker compose -f compose.example.yaml up -d
```

## Updates and restart

For a locally built image:

```sh
git pull --ff-only
docker compose up -d --build
```

For the published image:

```sh
docker compose -f compose.example.yaml pull
docker compose -f compose.example.yaml up -d
```

If you pin a version tag, update the image tag in the Compose file before
pulling. Preserve the data mount when updating. Recreating the container briefly
interrupts playback; ESP32 firmware is updated separately through `/setup` or the
CLI flasher.

`docker compose restart radio` restarts the container. The admin page's restart
button restarts the server process within the container. `docker compose down`
keeps the named data volume; `docker compose down -v` deletes it.
