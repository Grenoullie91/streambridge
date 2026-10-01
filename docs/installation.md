# Installation

## Install

```console
$ ./scripts/install.sh            # runtime only: the daemon and the CLI
$ ./scripts/install.sh --dev      # additionally pytest, ruff and mypy
$ ./scripts/uninstall.sh --yes    # remove it again
```

The dev extras are opt-in on purpose. A working install should not carry a
test framework, and the project has no runtime dependencies at all, so a
runtime-only install pulls in nothing beyond Python itself.

If you plan to work on StreamBridge rather than just run it, use `--dev` or
`make setup`: `scripts/verify.sh` and `make test` need pytest, and it will tell
you so rather than failing obscurely.

## Requirements

| Component | Needed for | Minimum |
|---|---|---|
| Python | everything | 3.11 (for `tomllib`) |
| yt-dlp | search, streaming | 2024.01.01 |
| MPD | playback | 0.21 |
| mpc | MPD control from the CLI | any recent version |

Optional: `ffmpeg` (exotic codecs), `ncmpcpp` (terminal UI).

Check what is present:

```console
$ streambridge doctor
```

## 1. Install MPD

### Debian / Ubuntu

```bash
sudo apt install mpd mpd-client
```

### Arch

```bash
sudo pacman -S mpd mpc
```

The packaged user service usually cannot read `/etc/mpd.conf`. See
[mpd-config.md](mpd-config.md) for the two ways around that; the user override
is the recommended one and needs no root.

## 2. Install yt-dlp

```bash
pipx install yt-dlp
# or
sudo apt install yt-dlp
```

yt-dlp is updated far more often than the distribution package, so `pipx` or a
self-updating install is preferable. Keep it current:

```bash
yt-dlp -U
```

An outdated extractor is the most common cause of "Requested format is not
available" and bot checks.

## 3. Install StreamBridge

```bash
pipx install streambridge
```

From a checkout, for development:

```bash
git clone https://github.com/Grenoullie91/streambridge
cd streambridge
pip install -e ".[dev]"
```

Verify:

```console
$ streambridge --version
$ streambridge-server --check
```

## 4. Start the server

Foreground, to see problems immediately:

```console
$ streambridge-server
2026-01-01T00:00:00 INFO    streambridge.server  streambridge-server 0.1.0 starting
2026-01-01T00:00:00 INFO    streambridge.server  listening on http://127.0.0.1:8787
```

As a user service:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/streambridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now streambridge
systemctl --user status streambridge
```

Follow the log:

```bash
journalctl --user -u streambridge -f
```

The packaged unit is hardened with `ProtectSystem=strict`, so you may need to
add your MPD `playlist_directory` to its `ReadWritePaths` line. See
[mpd-config.md](mpd-config.md).

## 5. Enable the service at boot

`systemctl --user enable --now` links it into `default.target`, which is what a
normal graphical session reaches. For a login that must have the bridge
without a desktop session:

```bash
sudo loginctl enable-linger "$USER"
```

## 6. Optional: a terminal player UI

```console
$ ncmpcpp
```

See [ncmpcpp.md](ncmpcpp.md). Configure one title column plus the duration: MPD
does not populate separate Artist and Album fields for stream entries.

## 7. Optional: shell completion

```bash
pipx install argcomplete
mkdir -p ~/.local/share/bash-completion/completions
argcomplete --shell bash > ~/.local/share/bash-completion/completions/streambridge
```

## Uninstall

```bash
systemctl --user disable --now streambridge
rm ~/.config/systemd/user/streambridge.service
pipx uninstall streambridge
rm -rf ~/.cache/streambridge
```

Nothing outside your home directory is modified. The MPD database and your
local library are never touched.

## Next

- [configuration.md](configuration.md) — every setting
- [mpd-config.md](mpd-config.md) — a working `mpd.conf`
- [ncmpcpp.md](ncmpcpp.md) — terminal player setup
- [troubleshooting.md](troubleshooting.md) — when something does not work
