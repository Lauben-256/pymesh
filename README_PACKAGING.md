# PyMesh Chat — Packaging Guide

Build a single distributable executable that requires **no Python installation** on the target machine.

---

## Requirements

Install PyInstaller on your build machine (the machine you run the build on must match the target OS):

```bash
pip install pyinstaller>=6.0
```

> **Important:** You must build on the same OS you want to distribute for.
> - Build on **macOS** → produces `dist/pymesh-chat` for macOS
> - Build on **Windows** → produces `dist/pymesh-chat.exe` for Windows
> - You cannot cross-compile (e.g. build a Windows .exe from macOS)

---

## Build

From the project root folder, run:

```bash
python build.py
```

That's it. The script will:
1. Check / install PyInstaller automatically
2. Clean any previous build artifacts
3. Run PyInstaller using `pymesh.spec`
4. Report the output binary path and size

---

## Output

| Platform | Binary location         | Typical size |
|----------|-------------------------|--------------|
| macOS    | `dist/pymesh-chat`      | ~15–25 MB    |
| Windows  | `dist/pymesh-chat.exe`  | ~15–25 MB    |

The binary is **fully self-contained** — just copy and run it, no Python or pip needed.

---

## Running the binary

**macOS** — first run only, allow execution:
```bash
chmod +x dist/pymesh-chat
./dist/pymesh-chat -a YourName -s your-session
```

If macOS Gatekeeper blocks it (unsigned binary), right-click → Open → Open anyway. Or:
```bash
xattr -dr com.apple.quarantine dist/pymesh-chat
```

**Windows:**
```
dist\pymesh-chat.exe -a YourName -s your-session
```

If Windows Defender SmartScreen warns you (unsigned binary), click "More info" → "Run anyway".

---

## Advanced options

### Universal macOS binary (Intel + Apple Silicon)

Edit `pymesh.spec`, find `target_arch=None` and change to:
```python
target_arch="universal2",
```
Then rebuild. The binary will run natively on both Intel and M-series Macs (doubles the size).

### Adding an icon

- **Windows:** Create a `icon.ico` file in the project root, then in `pymesh.spec` change `icon=None` to `icon="icon.ico"`
- **macOS:** Create a `icon.icns` file, change `icon=None` to `icon="icon.icns"`

### UPX compression

If [UPX](https://upx.github.io/) is installed and on your PATH, PyInstaller uses it automatically to compress the binary (typically reduces size by 30–50%).

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError` at runtime | Add the missing module to `hiddenimports` in `pymesh.spec` and rebuild |
| Binary blocked by Gatekeeper (macOS) | `xattr -dr com.apple.quarantine dist/pymesh-chat` |
| SmartScreen warning (Windows) | Click "More info" → "Run anyway" — expected for unsigned binaries |
| Large binary size | Install UPX: `brew install upx` (Mac) or download from upx.github.io (Windows) |
| `zeroconf` discovery not working | zeroconf needs network permissions; auto-discovery falls back gracefully, use `--connect` |
