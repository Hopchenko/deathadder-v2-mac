"""Build:  ./build.sh  (runs py2app -> dist/DeathAdder V2.app, signs it, installs to /Applications)"""
import os

from setuptools import setup

# logo.png / icon.icns are optional and not in the repo; use them if you have them.
OPTIONAL = {"resources": ["logo.png"] if os.path.exists("logo.png") else [],
            "icon": {"iconfile": "icon.icns"} if os.path.exists("icon.icns") else {}}

setup(
    app=["app.py"],
    name="DeathAdder V2",
    options={"py2app": {
        "includes": ["razer_da_v2", "remap", "ambient", "hid", "_math_integer", "WebKit", "ServiceManagement"],
        "resources": ["ui.html", *OPTIONAL["resources"]],
        **OPTIONAL["icon"],
        "plist": {
            "CFBundleIdentifier": "local.razer.deathadder-v2",
            "CFBundleShortVersionString": "2.0",
            "LSUIElement": True,      # menu-bar only, no Dock icon
        },
    }},
)
