# PyInstaller recipe for the Utter app, one for all three platforms. Run from the repo root:
#   pyinstaller --noconfirm packaging/utter.spec
# Out comes dist/Utter/ (Linux, Windows: a folder with the program and everything it needs) or dist/Utter.app (macOS).
# The installers are then made from that by build-deb.sh, utter.iss (Windows) and hdiutil (macOS), see build.yml.
import os, sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from utter import VERSION  # noqa: E402

LINUX, MAC, WIN = sys.platform.startswith("linux"), sys.platform == "darwin", sys.platform == "win32"
here = lambda *p: os.path.join(ROOT, *p)  # noqa: E731

a = Analysis(
    [here("packaging", "utter-app.py")],
    pathex=[here("src")],
    datas=[(here("sounds", "start.wav"), "sounds"), (here("sounds", "stop.wav"), "sounds"),
           (here("data", "utter.svg"), "data"), (here("data", "utter-tray.svg"), "data")],
    # the platform modules are picked at run time; name this platform's so it is always collected
    hiddenimports=["utter.plat.linux" if LINUX else "utter.plat.mac" if MAC else "utter.plat.windows"],
    # Linux uses the system's ffmpeg (a .deb dependency), so the 165 MB bundled one stays out
    excludes=["tkinter", "unittest", "pydoc_data"] + (["utter.plat.mac", "utter.plat.windows", "utter.plat.desktop",
                                                        "imageio_ffmpeg", "sounddevice", "pynput"] if LINUX else []),
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="utter-app" if LINUX else "Utter",
    console=False,
    icon=here("data", "utter.png"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="Utter")

if MAC:
    app = BUNDLE(
        coll,
        name="Utter.app",
        icon=here("data", "utter.png"),
        bundle_identifier="ai.utter.app",
        version=VERSION,
        info_plist={
            "CFBundleName": "Utter",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # menu bar app: no Dock icon
            "LSMinimumSystemVersion": "11.0",
            "NSMicrophoneUsageDescription": "Utter listens while you hold the shortcut, to turn your speech into text.",
            "NSHighResolutionCapable": True,
        },
    )
