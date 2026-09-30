# MPD integration

How StreamBridge drives MPD. For getting MPD itself running — audio backends,
service permissions, remote access — see [mpd-config.md](mpd-config.md); the two
are separate problems and this page is only about the first.

StreamBridge does not implement the MPD protocol. It drives your existing `mpc`
binary and MPD handles the audio. That is a deliberate choice: the MPD protocol
is stable and well specified, and every MPD client already speaks it, so
reimplementing it would add a second implementation to maintain and gain
nothing.

## What StreamBridge does to MPD

- Adds local HTTP URLs (`http://127.0.0.1:8787/stream/<id>`) to the running
  queue. This is a `load` of a generated playlist, not a database write.
- Writes that playlist into MPD's own `playlist_directory`, because MPD refuses
  to `load` from anywhere else.
- Injects title, artist, album and duration as `#EXTINF` tags in that same
  playlist. MPD will not accept tags on a URL add, so a playlist is the only
  supported way to make a URL queue entry show a real title.

**It never writes to the MPD database.** Your `music_directory` is not read
beyond MPD's own indexing, and not modified at all. Stopping StreamBridge
leaves nothing behind in MPD except queue entries, which stop resolving.

## Required MPD settings

Only two, both of which are already the default in a typical install. See
[`config/mpd.conf.example`](../config/mpd.conf.example) for the annotated file.

```ini
playlist_directory  "~/.local/share/mpd/playlists"
bind_to_address     "127.0.0.1"
```

### `playlist_directory`

StreamBridge must write a temporary `.m3u` there, and must be told where it is.
Leave `mpd.playlist_directory` unset in StreamBridge's config to let it
auto-detect, or set it explicitly:

```toml
[mpd]
playlist_directory = "~/.local/share/mpd/playlists"
```

The value must match MPD's exactly. If they disagree, queuing fails with
`MPD: access denied`, and [`docs/troubleshooting.md`](troubleshooting.md) has the
full diagnosis.

### `bind_to_address`

Keep MPD on loopback. Port 6600 is an **unauthenticated queue controller**: it
can play, clear and reorder anything, and it exposes your listening history.
This is true of any MPD install, not something StreamBridge changes, but
StreamBridge makes it more likely that MPD stays running.

## Audio output

A network output is what makes the URLs work. Any encoder MPD was built with
works; the packaged example uses `lame` at 192 kbps.

```ini
audio_output {
    type        "httpd"
    name        "StreamBridge output"
    encoder     "lame"
    port        "8000"
    bitrate     "192"
    format      "44100:16:2"
    mixer_type  "software"
    tags        "no"
}
```

`tags "no"` is deliberate: the metadata is already carried in the queue
entries, and a second copy in the stream confuses some clients.

## Diagnostics

```console
$ mpc status                 # what MPD thinks is playing
$ mpc playlist | head        # are the entries StreamBridge URLs?
$ streambridge doctor        # is StreamBridge's side healthy
```

A queued entry that shows a raw URL instead of a title means the `#EXTINF`
tags did not arrive, which in practice means MPD loaded the playlist but ignored
the tags. See [`ncmpcpp.md`](ncmpcpp.md) for the client side.

## Operational notes

- **Queue entries persist across restarts.** MPD saves its queue by default, so
  after a reboot the queue may hold URLs for a StreamBridge that is not running
  yet. They resolve once the service starts; if they do not, clear the queue
  (`streambridge clear`).
- **A track that will not play is usually a stale signature, not a bug.** A
  resolved media URL expires. StreamBridge retries once with a fresh URL; if
  both fail, re-add the track.
- **Do not set a static `volume` in the StreamBridge config expecting MPD to
  follow it.** MPD owns the volume; StreamBridge only remembers the last level
  it saw, to restore after a mute.
