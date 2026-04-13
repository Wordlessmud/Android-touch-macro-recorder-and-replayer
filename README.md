# Android-touch-macro-recorder-and-replayer

A Windows-only, non-root Android touch macro recorder and replayer built around **scrcpy** for live preview and **adb shell input motionevent** for gesture injection.

This tool records gestures from a transparent overlay placed over the scrcpy window. It does **not** passively capture touches made directly on the phone screen.

## What it does

- Launches **scrcpy** in read-only mode for live device preview
- Records touch gestures from a transparent desktop overlay
- Saves recorded gestures as JSON macro files
- Replays saved macros with optional:
  - speed changes
  - coordinate jitter
  - timing jitter
  - fixed or random intervals between runs
  - repeat counts or infinite replay
  - auto-quit timer
- Supports named macro and profile save/load from the GUI
- Includes ADB Wi-Fi helpers for pairing and connecting
- Can auto-reconnect and restart replay after disconnects
- Scales coordinates when replaying on a device size different from the original recording

## Requirements

- **Windows**
- **Python 3.9+**
- **Tkinter** available in your Python install
- **scrcpy** installed, or a direct path to `scrcpy.exe`
- **adb** installed, or a direct path to `adb.exe`
- Android device with:
  - USB debugging enabled
  - authorization accepted on the device

## Important limitation

This recorder captures gestures you perform **inside the desktop overlay over the scrcpy window**.

It does **not** record physical taps or swipes you make directly on the Android device itself.

## How the tool works

1. `record-scrcpy` starts scrcpy with control disabled.
2. A transparent overlay is positioned over the scrcpy preview.
3. Your mouse actions on that overlay are recorded as Android-style touch events.
4. The macro is saved as JSON.
5. `replay` sends the saved motion events back to the device through ADB.

## Installation

### 1. Install Python

Use Python 3.9 or newer on Windows.

### 2. Install scrcpy

Install scrcpy and confirm you can run it from Command Prompt, or note the path to `scrcpy.exe`.

### 3. Install adb

Install Android SDK Platform-Tools and confirm `adb` is on your `PATH`, or note the path to `adb.exe`.

### 4. Save the script

Save the script as:

```text
scrcpy_touch_macro.py
```

## Path discovery

The script can find tools in several ways:

### scrcpy

- `--scrcpy C:\path\to\scrcpy.exe`
- `SCRCPY_PATH` environment variable
- `scrcpy` or `scrcpy.exe` on `PATH`
- `scrcpy.exe` in the current working directory
- `scrcpy\scrcpy.exe` in the current working directory

### adb

- `--adb C:\path\to\adb.exe`
- `ADB_PATH` environment variable
- `adb` on `PATH`
- `adb.exe` next to the scrcpy binary
- common Android SDK Platform-Tools directories

## Quick start

### Record a macro

```bash
python scrcpy_touch_macro.py record-scrcpy my_macro.json
```

### Replay a macro once

```bash
python scrcpy_touch_macro.py replay my_macro.json
```

### Replay a macro multiple times with a gap

```bash
python scrcpy_touch_macro.py replay my_macro.json --repeat-count 5 --interval-s 1.5
```

### Replay with jitter

```bash
python scrcpy_touch_macro.py replay my_macro.json --jitter-px 3 --timing-jitter-ms 20
```

## Commands

## `record-scrcpy`

Launch scrcpy in read-only mode and open the recording GUI.

### Usage

```bash
python scrcpy_touch_macro.py record-scrcpy OUTPUT_JSON [options]
```

### Arguments

- `output` — path to the output JSON macro file

### Options

- `--scrcpy` — path to `scrcpy.exe`
- `--adb` — path to `adb.exe`
- `--serial` — specific ADB serial, including Wi-Fi targets like `192.168.1.25:5555`
- `--title` — unique scrcpy window title
- `--window-width` — initial scrcpy window width
- `--with-audio` — enable audio forwarding in scrcpy
- `--default-jitter-px` — default replay position jitter shown in the GUI
- `--default-timing-jitter-ms` — default replay timing jitter shown in the GUI
- `--default-replay-speed` — default replay speed shown in the GUI
- `--default-repeat-count` — default repeat count shown in the GUI
- `--default-interval-s` — default fixed interval between runs shown in the GUI
- `--default-interval-min-s` — default random interval minimum shown in the GUI
- `--default-interval-max-s` — default random interval maximum shown in the GUI
- `--default-infinite` — start the GUI with infinite replay enabled by default
- `--default-auto-quit-s` — default auto-quit timer shown in the GUI

### Example

```bash
python scrcpy_touch_macro.py record-scrcpy macros/scroll_feed.json --window-width 500 --title "ADB Macro Preview"
```

## `replay`

Replay a saved macro file through ADB.

### Usage

```bash
python scrcpy_touch_macro.py replay INPUT_JSON [options]
```

### Arguments

- `input` — path to a saved JSON macro file

### Options

- `--adb` — path to `adb.exe`
- `--serial` — specific ADB serial, including Wi-Fi targets like `192.168.1.25:5555`
- `--speed` — playback speed multiplier
- `--jitter-px` — random coordinate jitter in pixels, applied once per press
- `--timing-jitter-ms` — random delay jitter between events
- `--repeat-count` — number of times to replay the macro
- `--interval-s` — fixed delay between runs
- `--interval-min-s` — random interval minimum between runs
- `--interval-max-s` — random interval maximum between runs
- `--infinite` — replay until manually stopped or the auto-quit timer fires
- `--auto-quit-s` — stop after this many seconds
- `--seed` — RNG seed for repeatable jitter and interval behavior
- `--dry-run` — print the generated `input motionevent` commands instead of sending them

### Examples

Replay once:

```bash
python scrcpy_touch_macro.py replay macros/scroll_feed.json
```

Replay 20 times with a fixed interval:

```bash
python scrcpy_touch_macro.py replay macros/scroll_feed.json --repeat-count 20 --interval-s 2
```

Replay forever, but stop after 10 minutes:

```bash
python scrcpy_touch_macro.py replay macros/scroll_feed.json --infinite --auto-quit-s 600
```

Test the generated commands without touching the phone:

```bash
python scrcpy_touch_macro.py replay macros/scroll_feed.json --dry-run
```

## `wifi-pair`

Pair ADB with an Android 11+ device using Wireless debugging.

### Usage

```bash
python scrcpy_touch_macro.py wifi-pair PAIR_TARGET --code 123456 [--adb C:\path\to\adb.exe]
```

### Example

```bash
python scrcpy_touch_macro.py wifi-pair 192.168.1.25:37145 --code 123456
```

After pairing, if ADB does not auto-connect, use `wifi-connect` with the connect IP and port shown on the phone.

## `wifi-connect`

Connect ADB to a device over Wi-Fi.

### Usage

```bash
python scrcpy_touch_macro.py wifi-connect [options]
```

### Options

- `--adb` — path to `adb.exe`
- `--serial` — connected USB serial to use for `--enable-tcpip`, or a specific connected device
- `--ip` — target device IP address
- `--port` — TCP port, default `5555`
- `--enable-tcpip` — switch a currently USB-connected device into ADB-over-TCP/IP before connecting

### Examples

Connect directly to an already-known TCP/IP target:

```bash
python scrcpy_touch_macro.py wifi-connect --ip 192.168.1.25 --port 5555
```

Switch a USB-connected device to TCP/IP first, then connect:

```bash
python scrcpy_touch_macro.py wifi-connect --enable-tcpip --serial R58N1234567
```

The script prints the target serial you can reuse later:

```text
--serial 192.168.1.25:5555
```

## GUI overview

Running `record-scrcpy` opens:

- a transparent recording overlay over the scrcpy window
- a separate control window with three tabs:
  - **Main**
  - **Library**
  - **ADB Wi-Fi**

### Main tab

The Main tab includes:

- Start/Stop Recording
- current recording status
- replay status
- run counter
- countdown
- auto-quit time remaining
- replay settings
- reconnect settings
- Save, Clear, Replay, Stop, and Quit actions

### Replay settings available in the GUI

- replay speed
- repeat count
- interval seconds
- infinite replay toggle
- random interval min/max
- auto-quit timer
- position jitter
- timing jitter

### Reconnect settings

- auto reconnect toggle
- retry delay
- status text for reconnect attempts

### Library tab

The Library tab supports:

- creating a new macro name
- saving named macros
- loading named macros
- saving profiles
- loading profiles
- refreshing saved item lists

### ADB Wi-Fi tab

The Wi-Fi tab supports:

- entering a Wireless debugging pair target and pairing code
- entering connect IP and port
- storing the current connected target
- enabling USB to TCP/IP handoff
- reconnecting the scrcpy view after transport changes

## Keyboard shortcuts

While the GUI is open:

- `Space` — start/stop recording
- `S` — save
- `C` — clear
- `P` — replay
- `Q` — quit

## Saved data locations

The script stores app state under:

```text
%USERPROFILE%\.scrcpy_touch_macro\
```

Inside that folder:

- `macros\` — named macro JSON files
- `profiles\` — saved profile JSON files

## Macro format

Saved macros are JSON files containing:

- a version number
- a `kind` field
- the original device size
- an ordered list of motion points

During replay, coordinates are scaled automatically if the current device resolution differs from the recorded one.

## Wi-Fi usage notes

- `record-scrcpy` and `replay` work over Wi-Fi when you pass `--serial IP:PORT`
- after changing from USB to Wi-Fi, unplug USB only after the active serial shows the Wi-Fi target
- the GUI includes reconnect helpers for both transport and preview recovery

## Troubleshooting

### `Could not find adb`

Install Android SDK Platform-Tools and either:

- add `adb` to your `PATH`
- set `ADB_PATH`
- pass `--adb C:\path\to\adb.exe`
- or point `--scrcpy` at a scrcpy folder that also contains `adb.exe`

### `Could not find scrcpy`

Install scrcpy and either:

- add it to your `PATH`
- set `SCRCPY_PATH`
- or pass `--scrcpy C:\path\to\scrcpy.exe`

### `No connected device found`

Check:

- USB debugging is enabled
- the device is authorized
- the cable and USB mode are correct
- `adb devices` shows the device as `device`

### `Could not find the scrcpy window`

Try:

- launching scrcpy manually once
- using a unique `--title`
- making sure no other window already uses the same title

### Replays miss the target area

This can happen if:

- the app UI changed since recording
- the device orientation changed
- the target app layout differs
- resolution scaling changes produce slightly different hit points

Use modest replay jitter only when small variance helps. Keep it at `0` when exact targeting matters.

## Notes

- The script is intended for **Windows only**.
- ADB output is decoded as UTF-8 with replacement to reduce Windows code-page decoding issues.
- scrcpy is launched with `--no-control`, so the phone is controlled by replayed ADB events rather than direct scrcpy input.

## Typical workflow

1. Connect the Android device over USB.
2. Start recording:

```bash
python scrcpy_touch_macro.py record-scrcpy my_macro.json
```

3. In the overlay, perform the gesture you want to automate.
4. Save the macro.
5. Replay it from the GUI or from the command line:

```bash
python scrcpy_touch_macro.py replay my_macro.json --repeat-count 10 --interval-s 1
```

## License

This project is licensed under the **GNU General Public License v3.0**.

You may copy, modify, and distribute this software under the terms of the GPL v3.0.  
This program is distributed in the hope that it will be useful, but **without any warranty**; without even the implied warranty of **merchantability** or **fitness for a particular purpose**.
