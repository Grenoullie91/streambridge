# MPD configuration

Complete, tested examples. Replace `USER` and adjust paths to your system.

> **This page is about getting MPD itself running**: audio backends, service
> permissions, remote access. For how StreamBridge *drives* MPD — the
> `playlist_directory` contract, how track metadata reaches the queue, and the
> audio output it needs — see [mpd.md](mpd.md).

## The packaged user service usually does not start

Distributions ship `/usr/lib/systemd/user/mpd.service` with
`ExecStart=/usr/bin/mpd --systemd`. Started without a config argument, MPD
reads `/etc/mpd.conf`, and that file belongs to `mpd:audio` with mode `0640`.
If you are not in the `audio` group you get:

```
exception: Failed to open '/etc/mpd.conf': Permission denied
```

**This is the most common reason a user-level MPD will not start.** It is a
permissions problem with the packaged unit, not an MPD problem.

### Option A: your own user override (recommended, no root needed)

`~/.config/systemd/user/mpd.service.d/override.conf`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/mpd --no-daemon /home/USER/.config/mpd/mpd.conf

ProtectSystem=full
ReadWritePaths=/home/USER/.cache/mpd /home/USER/.config/mpd /home/USER/.local/share/mpd
```

Together with `~/.config/mpd/mpd.conf` (below), then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now mpd
```

### Option B: join the `audio` group

```bash
sudo usermod -aG audio "$USER"
```

Then **log out and back in**. The packaged unit works unchanged afterwards.

---

## PipeWire

```ini
# ~/.config/mpd/mpd.conf

music_directory     "/home/USER/.local/share/mpd/music"
playlist_directory  "/home/USER/.config/mpd/playlists"
db_file             "/home/USER/.cache/mpd/database"
state_file          "/home/USER/.cache/mpd/state"
sticker_file        "/home/USER/.cache/mpd/sticker.sql"
pid_file            "/home/USER/.cache/mpd/pid"
log_file            "/home/USER/.cache/mpd/mpd.log"

bind_to_address     "127.0.0.1"
port                "6600"

audio_output {
    type        "pipewire"
    name        "PipeWire"
    remote      "/run/user/1000/pipewire-0"   # UID must match!
    format      "44100:16:2"
    mixer_type  "software"
}

# Visualizer feed for ncmpcpp
audio_output {
    type        "null"
    name        "Visualizer feed"
    mixer_type  "none"
    format      "44100:16:2"
}

decoder { plugin "ffmpeg" enabled "yes" }
```

### `remote`, not `server`

**MPD 0.23.14 rejects `server`, `description` and `auto_resample` when
`type "pipewire"` is set**, and refuses to start:

```
config: option 'server' on line 41 was not recognized
config: option 'description' on line 42 was not recognized
config: option 'auto_resample' on line 45 was not recognized
```

The valid options are `remote`, `format` and `mixer_type`. The socket path
goes in `remote`.

Find your UID once and hardcode it: `id -u`.

### Check the config before starting

```bash
mpd --stdout --no-daemon ~/.config/mpd/mpd.conf
```

No output apart from a harmless wildmidi notice means the config is valid.
Interrupt with `Ctrl+C`.

## ALSA (fallback)

```ini
audio_output {
    type        "alsa"
    name        "ALSA output"
    device      "hw:0,0"
    format      "44100:16:2"
    mixer_type  "software"
}
```

## PulseAudio

```ini
audio_output {
    type        "pulse"
    name        "PulseAudio"
    server      "unix:/run/user/1000/pulse/native"
    format      "44100:16:2"
    mixer_type  "software"
}
```

## Important: `playlist_directory`

MPD accepts `load` **only** from this directory, and it wants the playlist
name **without** the `.m3u` suffix. Both facts are verified against MPD 0.23.14
and mpc 0.35.

```bash
mkdir -p ~/.config/mpd/playlists
```

Then in `~/.config/streambridge/config.toml`:

```toml
[mpd]
playlist_directory = "~/playlists"
```

Without it, adding a track reports:

```
Error: MPD did not load the playlist: MPD error: Access denied
  Hint: The file must live in MPD's playlist_directory (currently: ...).
```

`mpc paths`, which would allow automatic detection, does not exist in mpc
0.35. So without the setting, StreamBridge falls back to its cache directory,
which MPD will normally reject.

### What MPD actually reads from an M3U

**Only the `Name` field** (the text after the EXTINF comma) plus the duration.
Separate tag fields (`Artist`, `Album`, `AlbumArtist`) stay empty for stream
queue entries: MPD 0.23 does not fill them, not even from `#EXT-X-*` comments.

That is why StreamBridge builds the full display name (`Artist - Title`) into
that single field, without duplicating the artist when the upstream title
already begins with it.

Configure **one** title column plus the duration in your client, rather than
waiting for Artist/Album columns that stay empty.

## HTTP streaming needs no `music_directory`

The remote files are **not** in `music_directory`. MPD requests

```
http://127.0.0.1:8787/stream/<video-id>
```

and StreamBridge delivers the bytes. `music_directory` only concerns your
local library.

## Remote access (optional)

```ini
bind_to_address    "127.0.0.1"
# bind_to_address  "0.0.0.0"     # LAN only, with a firewall
# password         "something"
```

Never `0.0.0.0` without a password and a firewall.

## Running MPD as a user service

```bash
systemctl --user enable --now mpd
```

This runs MPD without root and without system directories. Use matching paths
in `mpd.conf`.

## Diagnostics

```bash
mpc status                 # connection and state
mpc lsplaylists            # does MPD see the playlists?
mpc playlist               # queue contents
mpc clearerror             # read MPD's error state
tail -f ~/.cache/mpd/mpd.log
```

### MPD rejects a track

```
exception: Decoder plugin 'opus' is unavailable
```

Install `ffmpeg`. MPD decodes Opus only with ffmpeg present. Without ffmpeg,
StreamBridge automatically prefers M4A/AAC, which MPD decodes natively.

### MPD will not start

```
exception: Failed to open '/home/USER/.cache/mpd/state': No such file or directory
```

Create the directories:

```bash
mkdir -p ~/.cache/mpd ~/.config/mpd/playlists
```

## Metadata in ncmpcpp

After a successful `streambridge play`, ncmpcpp shows **Artist - Title** rather
than a URL. That works through a temporary `EXTM3U` file:

```
#EXTM3U
#EXTINF:249,Artist One - First Song
#EXT-X-ALBUMARTIST:Artist One
#EXT-X-ALBUM:Some Album
#EXT-X-TITLE:First Song
http://127.0.0.1:8787/stream/5NV6Rdv1a3I
```

The file is deleted again once the load completes.

If the title is missing in ncmpcpp, `playlist_directory` is wrong.
