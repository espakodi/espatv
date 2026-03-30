#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Genera el ZIP instalable de plugin.video.espatv para Kodi.
Solo incluye los archivos necesarios, sin cache, tests ni basura.

Uso: python create_release_zip.py
Resultado: plugin.video.espatv-X.Y.Z.zip en el directorio padre.
"""
import os
import re
import zipfile

ADDON_ID = "plugin.video.espatv"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Extensiones permitidas
ALLOWED_EXT = {".py", ".xml", ".json", ".png", ".jpg", ".txt", ".md"}

# Carpetas y archivos a excluir
EXCLUDE_DIRS = {
    "__pycache__", ".pytest_cache", "tests", ".git", ".vscode", ".idea",
    ".gemini", "__test_profile__", ".agents", "_agents", ".agent", "_agent",
}
EXCLUDE_FILES = {"create_release_zip.py", ".gitignore", ".antigravityignore"}
INCLUDE_FILES = {"LICENSE"}  # Archivos sin extension que se deben incluir
EXCLUDE_PREFIXES = ("test_",)
EXCLUDE_EXT_EXTRA = {".zip"}


def get_version():
    """Extrae la version de addon.xml."""
    addon_xml = os.path.join(SCRIPT_DIR, "addon.xml")
    with open(addon_xml, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r'version="(\d+\.\d+\.\d+)"', content)
    return match.group(1) if match else "0.0.0"


def collect_files():
    """Recoge los archivos a incluir en el ZIP."""
    files = []
    for root, dirs, filenames in os.walk(SCRIPT_DIR):
        # Filtrar directorios excluidos (modifica in-place para os.walk)
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

        for name in filenames:
            if name in EXCLUDE_FILES:
                continue
            if any(name.startswith(p) for p in EXCLUDE_PREFIXES):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in EXCLUDE_EXT_EXTRA:
                continue
            if name not in INCLUDE_FILES and ext not in ALLOWED_EXT:
                continue
            full_path = os.path.join(root, name)
            # Ruta relativa dentro del ZIP: plugin.video.espatv/...
            rel = os.path.relpath(full_path, os.path.dirname(SCRIPT_DIR))
            files.append((full_path, rel))
    return files


def main():
    version = get_version()
    zip_name = "{0}-{1}.zip".format(ADDON_ID, version)
    out_path = os.path.join(SCRIPT_DIR, zip_name)

    files = collect_files()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full_path, arc_name in sorted(files):
            zf.write(full_path, arc_name)

    # Resumen
    total_kb = os.path.getsize(out_path) / 1024
    print("=" * 50)
    print("  {0}  ({1:.0f} KB)".format(zip_name, total_kb))
    print("  Version: {0}".format(version))
    print("  Archivos: {0}".format(len(files)))
    print("  Ruta: {0}".format(out_path))
    print("=" * 50)
    print()
    for _, arc in sorted(files):
        print("  + {0}".format(arc))
    print()
    print("Listo.")


if __name__ == "__main__":
    main()
