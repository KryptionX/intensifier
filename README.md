# Intensifier

Turn any image into a Slack-ready "intensifies" emoji GIF. Web UI or CLI. macOS + Linux.

Based on [vtbassmatt/intensifier](https://github.com/vtbassmatt/intensifier) by Matt Cooper (MIT), a shell script that does the classic shake. This is a ground-up rewrite with a UI, more effects and Slack size fitting.

## Install

```bash
git clone <this repo> intensifier && cd intensifier
./install.sh
```

`install.sh` installs the dependencies (Homebrew on macOS, or apt / dnf / pacman on Linux) and links
an `intensify` command into `~/.local/bin`. The command is a symlink back to the clone, so a `git pull` updates it.

```bash
./install.sh --no-deps     # just link the command
./install.sh --uninstall   # remove the command
intensify --doctor         # check what's installed
```

| Dependency | Why |
|---|---|
| python3 (3.8+) | the program itself, stdlib only, no pip |
| imagemagick (6 or 7) | **required**, all image work |
| gifsicle | recommended, extra optimisation pass, noticeably smaller GIFs |
| librsvg / webp | optional, SVG / WebP input |

Prefer not to install? Run it in place: `./intensify.py`.

## Use

```bash
intensify                              # web UI (opens your browser)
intensify cat.png                      # -> cat-intensifies.gif next to the input
intensify -p extreme --remove-bg *.png
intensify cat.png -i 9 --rotate 6 --hue 360 --delay 25 -s 128
intensify cat.png --suffix=-shook      # use = when the value starts with a dash
```

Presets: classic, mild, intense, extreme, earthquake, wobble, pulse, bounce, spin, party.
`intensify -h` lists every option.

The web UI runs on `127.0.0.1` only and refuses requests from other websites. Uploads live in a
temp folder that's deleted when you quit.

## Slack compliance

Slack wants square, transparent, under 128KB. Every render is checked for all three.
If the GIF is over the limit it is automatically shrunk (fewer colours, then fewer frames,
then smaller size) using the gentlest setting that fits, and the UI/CLI tells you what changed.
Use `--no-slack-fit` / untick the option to disable.

GIF transparency is 1-bit; "alpha cut" controls where semi-transparent edge pixels flip to opaque.
Photos with solid backgrounds: tick **Remove background** (flood-fills from the corners).

## Development

```bash
python3 -m unittest discover -s tests -v
```

The smoke tests render every preset and check each GIF is square, transparent and under the limit,
then exercise the CLI and the web server (including its cross-site request blocking).
CI runs them on macOS (ImageMagick 7) and Ubuntu (ImageMagick 6, with and without gifsicle).

| File | What |
|---|---|
| `intensify.py` | engine, CLI and local web server |
| `ui.html` | the web UI (vanilla JS, no build step) |
| `install.sh` | dependency install + `intensify` command link |
| `Brewfile` | Homebrew dependencies |
| `tests/` | stdlib-only smoke tests |

## License

[MIT](LICENSE). Includes the original copyright notice from vtbassmatt/intensifier.
