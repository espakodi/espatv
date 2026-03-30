# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
pvr_manager — Gestor del cliente PVR IPTV Simple de Kodi.

Automatiza la instalacion, configuracion y activacion del addon
pvr.iptvsimple para mostrar la parrilla EPG nativa desde EspaTV.

Compatibilidad:
    - Kodi 19 (Matrix) y 20 (Nexus): escritura dual via setSetting + XML.
    - Kodi 21 (Omega): escritura directa XML sobre instance-settings-*.xml.
"""
import json
import time

import xbmc
import xbmcaddon
import xbmcgui

_PVR_ADDON_ID = "pvr.iptvsimple"

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


#  Consultas de estado

def is_pvr_installed():
    """Comprueba si pvr.iptvsimple esta instalado (aunque este deshabilitado)."""
    try:
        xbmcaddon.Addon(_PVR_ADDON_ID)
        return True
    except RuntimeError:
        return False


#  JSON-RPC

def _execute_rpc(method, params=None):
    """Ejecuta un comando JSON-RPC contra el nucleo de Kodi."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        payload["params"] = params
    raw = xbmc.executeJSONRPC(json.dumps(payload))
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def enable_pvr():
    """Habilita el addon PVR por JSON-RPC."""
    resp = _execute_rpc(
        "Addons.SetAddonEnabled",
        {"addonid": _PVR_ADDON_ID, "enabled": True},
    )
    return resp.get("result") == "OK"


def _disable_pvr():
    """Deshabilita el addon PVR para poder modificar sus XML sin bloqueos."""
    _execute_rpc(
        "Addons.SetAddonEnabled",
        {"addonid": _PVR_ADDON_ID, "enabled": False},
    )


def trigger_pvr_reload():
    """Fuerza la recarga de la base de datos PVR."""
    resp = _execute_rpc("PVR.Reload")
    return resp.get("result") == "OK"


def _reset_pvr_database():
    """Reinicia la EPG y la base de datos del PVR para forzar reimportacion."""
    try:
        _execute_rpc(
            "Settings.SetSettingValue",
            {"setting": "pvrmanager.resetepg", "value": True},
        )
        _execute_rpc(
            "Settings.SetSettingValue",
            {"setting": "pvrmanager.resetdb", "value": True},
        )
    except Exception:
        pass


# Configuracion de settings del PVR

def _build_settings_map(m3u_url, epg_url):
    """Devuelve el diccionario de settings que deben inyectarse."""
    return {
        "m3uPathType":      "1",
        "m3uUrl":           m3u_url,
        "m3uCache":         "false",
        "epgPathType":      "1",
        "epgUrl":           epg_url,
        "epgCache":         "false",
        "catchupEnabled":   "false",
        "logoPathType":     "0",
        "defaultUserAgent": _CHROME_UA,
    }


def _apply_via_addon_api(m3u_url, epg_url):
    """Intenta aplicar settings usando la API clasica de Kodi (setSetting).

    Funciona en Kodi 18/19. En versiones posteriores puede fallar si el
    addon esta deshabilitado, lo cual es comportamiento esperado.
    """
    try:
        addon = xbmcaddon.Addon(_PVR_ADDON_ID)
    except RuntimeError:
        return

    cambios = False
    for key, val in _build_settings_map(m3u_url, epg_url).items():
        if addon.getSetting(key) != val:
            addon.setSetting(key, val)
            cambios = True
    return cambios


def _apply_via_xml(m3u_url, epg_url):
    """Inyecta settings directamente en los XML de pvr.iptvsimple.

    Kodi 20+ usa instance-settings-N.xml ademas de settings.xml.
    Escribir solo via setSetting provoca inconsistencias que cuelgan
    el PVR Manager al 0%.
    """
    import os
    import xml.etree.ElementTree as ET
    import xbmcvfs

    base = xbmcvfs.translatePath(
        "special://userdata/addon_data/" + _PVR_ADDON_ID
    )
    base = os.path.normpath(base)

    if not os.path.isdir(base):
        return False

    mapping = _build_settings_map(m3u_url, epg_url)
    cambios = False

    # Recopilar XMLs a procesar
    targets = []
    settings_xml = os.path.join(base, "settings.xml")
    if os.path.isfile(settings_xml):
        targets.append(settings_xml)
    for fn in os.listdir(base):
        if fn.startswith("instance-settings-") and fn.endswith(".xml"):
            targets.append(os.path.join(base, fn))

    for filepath in targets:
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()
        except ET.ParseError as exc:
            xbmc.log(
                "[EspaTV] XML corrupto en {}: {}".format(filepath, exc),
                xbmc.LOGERROR,
            )
            continue

        existing_ids = set()
        file_changed = False

        # Actualizar settings existentes
        for node in root.findall("setting"):
            sid = node.get("id")
            existing_ids.add(sid)
            if sid in mapping and (node.text or "") != mapping[sid]:
                node.text = mapping[sid]
                node.attrib.pop("default", None)
                file_changed = True

        # Insertar settings que faltan
        for sid, val in mapping.items():
            if sid not in existing_ids:
                ET.SubElement(root, "setting", id=sid).text = val
                file_changed = True

        if file_changed:
            tree.write(filepath, encoding="utf-8", xml_declaration=False)
            cambios = True

    return cambios


def configure_pvr_settings(m3u_url, epg_url):
    """Aplica la configuracion PVR usando ambos metodos (API + XML).

    Retorna (exito: bool, hubo_cambios: bool).
    """
    _apply_via_addon_api(m3u_url, epg_url)

    try:
        cambios = _apply_via_xml(m3u_url, epg_url)
        return True, bool(cambios)
    except Exception as exc:
        xbmc.log("[EspaTV] Error configurando PVR: " + str(exc), xbmc.LOGERROR)
        return False, False



#  Flujo principal


def check_and_setup_pvr(m3u_url, epg_url):
    """Configura el PVR de forma segura siguiendo este flujo:

    1. Verificar instalacion; si falta, redirigir al instalador nativo.
    2. Deshabilitar el addon para evitar bloqueos de escritura.
    3. Inyectar configuracion en los XML.
    4. Rehabilitar el addon.
    5. Reiniciar la base de datos EPG para forzar reimportacion limpia.
    """
    if not is_pvr_installed():
        xbmcgui.Dialog().ok(
            "EspaTV",
            "Para ver la parrilla de TV necesitas el modulo PVR IPTV Simple.\n"
            "Pulsa instalar en la siguiente pantalla.",
        )
        xbmc.executebuiltin("InstallAddon({})".format(_PVR_ADDON_ID))
        return False

    _disable_pvr()
    time.sleep(0.5)

    success, _cambios = configure_pvr_settings(m3u_url, epg_url)
    if not success:
        enable_pvr()
        return False

    enable_pvr()
    time.sleep(0.5)

    _reset_pvr_database()

    return True