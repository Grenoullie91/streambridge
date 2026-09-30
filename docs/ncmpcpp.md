# ncmpcpp setup

ncmpcpp is the terminal player UI. It talks to MPD, not to StreamBridge, so
it needs no special configuration beyond a sensible column layout.

## Install and run

```bash
sudo apt install ncmpcpp        # Debian / Ubuntu
sudo pacman -S ncmpcpp          # Arch
```

```bash
ncmpcpp
```

If MPD runs as a user service, nothing else is needed: ncmpcpp reads
`~/.config/ncmpcpp/config` and connects to `127.0.0.1:6600`.

## The one thing that matters: column layout

MPD populates **only** the `Name` field and the duration for stream queue
entries. Separate Artist, Album and AlbumArtist fields stay empty, even when
the generated playlist carries `#EXT-X-ALBUMARTIST` comments. That is MPD
0.23 behaviour, not a StreamBridge limitation.

So configure one title column and a duration, and leave Artist and Album out
of the list view. Otherwise you stare at empty columns.

`~/.config/ncmpcpp/config`:

```ini
ncmpcpp_title_format = "%a - %t"
```

That renders MPD's `Name` field, which StreamBridge fills with
`Artist - Title`.

## A working configuration

```ini
# ~/.config/ncmpcpp/config

# Host and port of MPD. Matches StreamBridge's [mpd] section.
host = "127.0.0.1"
port = 6600

# Nothing here is music: online tracks arrive as HTTP URLs in the queue.
mpd_music_dir = "~/.local/share/mpd/music"

# Show the full display name MPD received.
ncmpcpp_title_format = "%a - %t"

# Hide empty columns rather than showing blanks.
empty_item_line_format = "%t"
empty_playlist_format = "(empty)"

# Reasonable for a terminal that is not a TTY.
screen_view_mode = "small"
playlist_view_mode = "small"

# Colour, but not required. The defaults are readable on a dark terminal.
enable_color = true

# The visualizer needs a second MPD output; see docs/mpd-config.md.
# Without a null output defined, the spectrum display stays flat.
```

## Verify the connection

Inside ncmpcpp, the status line shows the connection. From a shell:

```bash
ncmpcpp -H 127.0.0.1
```

If it cannot connect, MPD is the problem, not ncmpcpp:

```bash
mpc status
systemctl --user status mpd
```

## Keys worth knowing

| Key | Action |
|---|---|
| `q` | Quit |
| `space` | Play/pause |
| `>` / `<` | Next / previous track |
| `s` | Stop |
| `+` / `-` | Volume up / down |
| `/` | Filter the current view |
| `y` / `p` | yank / paste within the playlist editor |
| `F1` | Help |

## What you should see

After a successful queue:

```
Artist One - First Song
   playing  #1  0:42/4:09 (17%)
```

If the title column shows `http://127.0.0.1:8787/stream/...` instead, MPD
received no metadata. The cause is almost always a wrong
`playlist_directory`; see [mpd-config.md](mpd-config.md).
