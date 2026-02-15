# Miru CLI tools

CLI tools for [Miru](https://miru.re).

## Install (pip)

Install the CLI tools:

    pip install miru-tools

This will also install the matching version of `miru-core` (the Python bindings, `import miru`).

## UI language (global)

Persist a global UI language setting:

    miru --AUTO
    miru --TH
    miru --EN

Notes:

- `MIRU_UI_LANG` (env) overrides the persisted setting.
- The persisted file is stored at:
  - Windows: `%LOCALAPPDATA%\\miru\\Config\\ui_lang.txt`
  - macOS: `~/Library/Application Support/miru/Config/ui_lang.txt`
  - Linux: `~/.config/miru/Config/ui_lang.txt` (or `$XDG_CONFIG_HOME`)

## Android USB auto-install (`miru-server`)

When using `-U` (USB) on Android, if `miru-server` is not running, Miru can self-heal by downloading the right
`miru-server` from GitHub Releases and starting it via `adb`.

Requirements:

- `adb` available on PATH (or set `MIRU_ADB`).
- Root is recommended for full capability (Miru will try `su -c`, otherwise runs as shell user).

Controls / overrides:

- `MIRU_AUTO_INSTALL_SERVER=0` disables auto-install.
- `MIRU_ASSETS_BASE_URL` sets the release base URL containing `miru-assets-manifest.json` and binaries.
- `MIRU_ASSETS_CACHE_DIR` overrides the local download cache directory.
- `MIRU_ADB` overrides the `adb` executable path.
- `MIRU_ADB_SERIAL` / `ANDROID_SERIAL` selects the device if multiple are connected.
- `MIRU_SERVER_PORT` overrides the default port (27042).

### Installing Fish completions

Currently there is no mechanism to install Fish completions through the setup.py
script so if you want to have completions in Fish you will have to install it
manually. Unless you've changed your XDG_CONFIG_HOME location, you should just
copy the completion file into `~/.config/fish/completions` like so:

    cp completions/miru.fish ~/.config/fish/completions

### miru-itrace file format

File starts with a 4-byte magic: "ITRC"
https://github.com/frida/frida-tools/blob/1ea077fdb49440e5807cf25fae41e389e3d2bd4a/frida_tools/itracer.py#L365-L366

Then, following that, there are two different types of records, MESSAGE and
CHUNK. Each record starts with a big-endian uint32 specifying the type of
record, where 1 means MESSAGE, 2 means CHUNK.

#### MESSAGE

- `length`: uint32 (big-endian)
- `message`: JSON, UTF-8 encoded
- `data_size`: uint32 (big-endian)
- `data_values`: uint8[data_size]

Generated [here](https://github.com/frida/frida-tools/blob/1ea077fdb49440e5807cf25fae41e389e3d2bd4a/frida_tools/itracer.py#L451-L458).

There are three different kinds of MESSAGEs:

- ["itrace:start"](https://github.com/frida/frida-tools/blob/1ea077fdb49440e5807cf25fae41e389e3d2bd4a/agents/itracer/agent.ts#L68-L76):
  Signals that the trace is starting, providing the initial register values.
  Contains register names and sizes in the JSON portion, and register values in
  the data portion.
  Generated [here](https://github.com/frida/frida-itrace/blob/ad7780bde9e518e325d7aaf848e9a29e1a53b7d2/lib/backend.ts#L341-L359).
- "itrace:end": Signals that the endpoint was reached, when specifying a range
  with an end address included.
- "itrace:compile": Signals that a basic block was discovered, providing the
  schema of future CHUNKs pertaining to it.
  Generated [here](https://github.com/frida/frida-itrace/blob/ad7780bde9e518e325d7aaf848e9a29e1a53b7d2/lib/backend.ts#L277-L323)
  and by the [code](https://github.com/frida/frida-itrace/blob/ad7780bde9e518e325d7aaf848e9a29e1a53b7d2/lib/backend.ts#L398-L401)
  above it that computes the "writes" array.

  The "writes" array contains tuples (arrays) that look like this:

    (block_offset, cpu_ctx_offset)

  Where `block_offset` is how many bytes into the basic block the write happens,
  and `cpu_ctx_offset` is the index into the registers declared by
  "itrace:start".

#### CHUNK

- `size`: uint32 (big-endian)
- `data`: uint8[size]

Generated [here](https://github.com/frida/frida-tools/blob/1ea077fdb49440e5807cf25fae41e389e3d2bd4a/frida_tools/itracer.py#L464-L465).

The CHUNK records combine to a stream of raw register values at different parts
of the given basic block. Each record looks like this:

- `block_start_address`: uint64 (target-endian, i.e. little-endian on arm64)
- `link_register_value`: uint64 (target-endian)
- `block_register_values`: uint64[n], where n depends on the specific basic
  block. (See above docs on "itrace:compile" and its "writes" array.)
