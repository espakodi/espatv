# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
tdt_providers.py — Radio en Directo y TDT por Comunidad Autónoma.

Descarga y parsea las listas M3U de TDTChannels para ofrecer
radio española y canales TDT agrupados por comunidad autónoma.
"""
import re
import sys
import os
import json
import time
import hashlib
import core_settings

import xbmc
import xbmcgui
import xbmcplugin

try:
    import requests
except ImportError:
    import urllib.request
    requests = None

# Constantes
_RADIO_M3U_URL = "https://www.tdtchannels.com/lists/radio.m3u8"
_TDT_TV_JSON_URL = "https://www.tdtchannels.com/lists/tv.json"
_CACHE_TTL = 3600  # 1 hora

# Caché en memoria para evitar descargas repetidas durante la sesión
_radio_cache = {"ts": 0, "channels": None}
_tdt_region_cache = {"ts": 0, "ambits": None}

# Traducción de nombres de grupo del M3U a nombres legibles
_RADIO_GROUP_NAMES = {
    "Radio_Populares": "Populares (SER, COPE, Onda Cero...)",
    "Radio_Musicales": "Musicales (LOS40, Kiss FM, Rock FM...)",
    "Radio_Deportivas": "Deportivas",
    "Radio_Infantiles": "Infantiles",
}

# Regiones → nombres bonitos (para las que no coinciden con el nombre del grupo)
_REGION_NAMES = {
    "Radio_Andalucía": "Andalucía",
    "Radio_Aragón": "Aragón",
    "Radio_Asturias": "Asturias",
    "Radio_Canarias": "Canarias",
    "Radio_Cantabria": "Cantabria",
    "Radio_Castilla-La Mancha": "Castilla-La Mancha",
    "Radio_Castilla y León": "Castilla y León",
    "Radio_Cataluña": "Cataluña",
    "Radio_Ceuta": "Ceuta",
    "Radio_C. de Madrid": "Comunidad de Madrid",
    "Radio_C. Foral de Navarra": "Navarra",
    "Radio_C. Valenciana": "Comunidad Valenciana",
    "Radio_Extremadura": "Extremadura",
    "Radio_Galicia": "Galicia",
    "Radio_Illes Balears": "Illes Balears",
    "Radio_La Rioja": "La Rioja",
    "Radio_Melilla": "Melilla",
    "Radio_País Vasco": "País Vasco",
    "Radio_R. de Murcia": "Región de Murcia",
    "Radio_Int": "Internacional",
}


def _log(msg):
    xbmc.log("[EspaTV][tdt_providers] {0}".format(msg), xbmc.LOGINFO)


def _log_error(msg):
    xbmc.log("[EspaTV][tdt_providers] ERROR: {0}".format(msg), xbmc.LOGERROR)


def _handle():
    return int(sys.argv[1])


def _u(**kwargs):
    """Construye una URL de plugin con los parámetros dados."""
    from urllib.parse import urlencode
    base = sys.argv[0]
    return "{0}?{1}".format(base, urlencode(kwargs))


# Descarga HTTP
def _fetch(url, timeout=15):
    """Descarga una URL y devuelve el texto usando la cache de IPTV si esta activa."""
    ttl = core_settings.get_iptv_cache_ttl()
    cache_file = ""
    if core_settings.is_iptv_cache_active() and ttl > 0:
        cache_dir = core_settings.get_iptv_cache_dir()
        cache_key = hashlib.md5(url.encode('utf-8')).hexdigest()
        cache_file = os.path.join(cache_dir, "tdt_" + cache_key + ".txt")
        if os.path.exists(cache_file):
            try:
                import time
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if (time.time() - cached.get("ts", 0)) < ttl:
                    return cached.get("content", "")
            except Exception:
                pass

    if requests:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        data = r.text
    else:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")

    if core_settings.is_iptv_cache_active() and ttl > 0 and cache_file:
        try:
            import time
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({"ts": time.time(), "url": url, "content": data}, f, ensure_ascii=False)
        except Exception:
            pass

    return data


# Parser M3U
def parse_m3u(text):
    """Parsea contenido M3U y devuelve lista de dicts con name, url, logo, group."""
    lines = text.splitlines()
    channels = []
    cur_name, cur_logo, cur_group = "", "", ""
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            cur_name = line.split(",", 1)[-1].strip() if "," in line else "Canal"
            m = re.search(r'tvg-logo="([^"]*)"', line)
            cur_logo = m.group(1) if m else ""
            m2 = re.search(r'group-title="([^"]*)"', line)
            cur_group = m2.group(1) if m2 else ""
        elif line and not line.startswith("#"):
            channels.append({
                "name": cur_name or "Canal",
                "url": line,
                "logo": cur_logo,
                "group": cur_group,
            })
            cur_name, cur_logo, cur_group = "", "", ""
    return channels


# Radio: obtener datos
def _get_radio_channels():
    """Devuelve la lista de emisoras de radio (con caché en memoria)."""
    now = time.time()
    if _radio_cache["channels"] and (now - _radio_cache["ts"]) < _CACHE_TTL:
        return _radio_cache["channels"]
    text = _fetch(_RADIO_M3U_URL)
    channels = parse_m3u(text)
    _radio_cache["channels"] = channels
    _radio_cache["ts"] = now
    _log("Radio: descargadas {0} emisoras".format(len(channels)))
    return channels


def _radio_groups(channels):
    """Devuelve dict ordenado  { grupo: [emisoras] }."""
    from collections import OrderedDict
    groups = OrderedDict()
    for ch in channels:
        g = ch["group"] or "Sin grupo"
        groups.setdefault(g, []).append(ch)
    return groups


# Radio: menus Kodi
def radio_menu():
    """Menú principal de radio: lista de grupos."""
    h = _handle()
    try:
        xbmc.executebuiltin("ActivateWindow(busydialognocancel)")
        channels = _get_radio_channels()
        groups = _radio_groups(channels)
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
        if not groups:
            xbmcgui.Dialog().notification("Radio", "No se encontraron emisoras.",
                                          xbmcgui.NOTIFICATION_WARNING, 3000)
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
            return

        themed = []
        regional = []
        for g_key, g_list in groups.items():
            display = _RADIO_GROUP_NAMES.get(g_key)
            if display:
                themed.append((g_key, display, len(g_list)))
            else:
                region_name = _REGION_NAMES.get(g_key, g_key.replace("Radio_", ""))
                regional.append((g_key, region_name, len(g_list)))

        for g_key, display, count in themed:
            li = xbmcgui.ListItem(
                label="[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(display, count)
            )
            li.setArt({"icon": "DefaultMusicSongs.png"})
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="radio_group", group=g_key),
                listitem=li,
                isFolder=True,
            )

        li = xbmcgui.ListItem(label="[COLOR dimgray]── Emisoras por Comunidad Autónoma ──[/COLOR]")
        li.setProperty("IsPlayable", "false")
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)

        for g_key, display, count in sorted(regional, key=lambda x: x[1]):
            li = xbmcgui.ListItem(
                label="{0} [COLOR gray]({1})[/COLOR]".format(display, count)
            )
            li.setArt({"icon": "DefaultMusicSongs.png"})
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="radio_group", group=g_key),
                listitem=li,
                isFolder=True,
            )
        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
    except Exception as e:
        xbmc.executebuiltin("Dialog.Close(busydialognocancel)")
        _log_error("radio_menu: {0}".format(e))
        xbmcgui.Dialog().notification("Radio", "Error al cargar",
                                      xbmcgui.NOTIFICATION_ERROR, 3000)
        try:
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        except Exception:
            pass


def radio_group(group_name):
    """Lista emisoras de un grupo concreto."""
    h = _handle()
    try:
        channels = _get_radio_channels()
        filtered = [ch for ch in channels if ch["group"] == group_name]
        seen = set()
        unique = []
        for ch in filtered:
            if ch["name"] not in seen:
                seen.add(ch["name"])
                unique.append(ch)
        if not unique:
            xbmcgui.Dialog().notification("Radio", "Grupo vacío",
                                          xbmcgui.NOTIFICATION_WARNING, 3000)
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
            return
        xbmcplugin.setContent(h, "songs")
        for ch in unique:
            thumb = ch["logo"] or "DefaultMusicSongs.png"
            li = xbmcgui.ListItem(label=ch["name"])
            li.setArt({"icon": thumb, "thumb": thumb})
            li.setInfo("music", {
                "title": ch["name"],
                "comment": "Grupo: {0}".format(
                    _RADIO_GROUP_NAMES.get(group_name,
                        _REGION_NAMES.get(group_name, group_name))
                ),
            })
            li.setProperty("IsPlayable", "true")
            cm = [("Añadir a Favoritos", "RunPlugin({0})".format(
                _u(action="add_favorite", title=ch["name"],
                   fav_url=ch["url"], icon=thumb,
                   platform="radio", fav_action="play_radio",
                   params=json.dumps({}))
            ))]
            li.addContextMenuItems(cm)
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="play_radio", url=ch["url"], title=ch["name"]),
                listitem=li,
                isFolder=False,
            )
        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
    except Exception as e:
        _log_error("radio_group: {0}".format(e))
        xbmcgui.Dialog().notification("Radio", "Error al cargar",
                                      xbmcgui.NOTIFICATION_ERROR, 3000)
        try:
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        except Exception:
            pass


def play_radio(url, title=""):
    """Reproduce una emisora de radio."""
    h = _handle()
    try:
        _log("Reproduciendo radio: {0} → {1}".format(title, url))
        li = xbmcgui.ListItem(path=url)
        if title:
            li.setInfo("music", {"title": title})
        if ".m3u8" in url.lower():
            li.setMimeType("application/x-mpegURL")
        elif ".pls" in url.lower():
            li.setMimeType("audio/x-scpls")
        elif ".aac" in url.lower():
            li.setMimeType("audio/aac")
        elif ".mp3" in url.lower():
            li.setMimeType("audio/mpeg")
        li.setContentLookup(False)
        xbmcplugin.setResolvedUrl(h, True, li)
    except Exception as e:
        _log_error("play_radio: {0}".format(e))
        xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
        xbmcgui.Dialog().notification("Radio", "Error al reproducir",
                                      xbmcgui.NOTIFICATION_ERROR, 3000)


# TDT por Comunidad Autonoma
def _get_tdt_ambits():
    """Devuelve la lista de ámbitos TDT desde tv.json (con caché)."""
    now = time.time()
    if _tdt_region_cache["ambits"] and (now - _tdt_region_cache["ts"]) < _CACHE_TTL:
        return _tdt_region_cache["ambits"]
    text = _fetch(_TDT_TV_JSON_URL)
    data = json.loads(text)
    countries = data.get("countries", [])
    ambits = countries[0].get("ambits", []) if countries else []
    _tdt_region_cache["ambits"] = ambits
    _tdt_region_cache["ts"] = now
    _log("TDT: descargados {0} ámbitos".format(len(ambits)))
    return ambits




def tdt_by_region_menu():
    """Menú de Comunidades Autónomas con sus canales TDT."""
    h = _handle()
    dp = xbmcgui.DialogProgress()
    dp.create("TDT por Comunidad", "Descargando datos...")
    try:
        dp.update(30, "Conectando con TDTChannels...")
        ambits = _get_tdt_ambits()
        dp.close()
        if not ambits:
            xbmcgui.Dialog().ok("TDT", "No se pudieron cargar los datos.")
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
            return

        # Filtrar solo los ámbitos que son CCAA
        for ambit in ambits:
            name = ambit.get("name", "")
            num = len(ambit.get("channels", []))
            if not num:
                continue
            li = xbmcgui.ListItem(
                label="[B]{0}[/B] [COLOR gray]({1} canales)[/COLOR]".format(name, num)
            )
            li.setArt({"icon": "DefaultAddonPVRClient.png"})
            li.setInfo("video", {
                "plot": "Canales TDT de {0}\nTotal: {1}".format(name, num)
            })
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="tdt_region", region=name),
                listitem=li,
                isFolder=True,
            )
        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
    except Exception as e:
        dp.close()
        _log_error("tdt_by_region_menu: {0}".format(e))
        xbmcgui.Dialog().ok("TDT", "Error al cargar:\n{0}".format(e))
        try:
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        except Exception:
            pass


def tdt_region(region_name):
    """Lista canales TDT de una comunidad autónoma concreta."""
    h = _handle()
    try:
        ambits = _get_tdt_ambits()
        target = None
        for a in ambits:
            if a.get("name") == region_name:
                target = a
                break
        if not target:
            xbmcgui.Dialog().ok("TDT", "No se encontró la región: {0}".format(region_name))
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
            return

        xbmcplugin.setContent(h, "videos")
        for ch in target.get("channels", []):
            name = ch.get("name", "Canal")
            opts = ch.get("options", [])
            logo = ch.get("logo", "")
            # Buscar el primer stream disponible (m3u8 o mp4)
            stream_url = ""
            for opt in opts:
                fmt = opt.get("format", "")
                url = opt.get("url", "")
                if url and fmt.lower() in ("m3u8", "mp4", ""):
                    stream_url = url
                    break
            if not stream_url:
                continue
            thumb = logo or "DefaultAddonPVRClient.png"
            li = xbmcgui.ListItem(label=name)
            li.setArt({"icon": thumb, "thumb": thumb})
            li.setInfo("video", {
                "title": name,
                "plot": "Canal TDT — {0}\nRegión: {1}".format(name, region_name),
            })
            li.setProperty("IsPlayable", "true")
            cm = [
                ("Añadir a Favoritos", "RunPlugin({0})".format(
                _u(action="add_favorite", title=name,
                   fav_url=stream_url, icon=thumb,
                   platform="tdt", fav_action="play_tdt",
                   params=json.dumps({}))
                )),
                ("[COLOR red]Grabar este canal[/COLOR]", "RunPlugin({0})".format(
                _u(action="record_stream", url=stream_url, name=name)
                ))
            ]
            li.addContextMenuItems(cm)
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="play_tdt", url=stream_url, title=name),
                listitem=li,
                isFolder=False,
            )
        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
    except Exception as e:
        _log_error("tdt_region: {0}".format(e))
        xbmcgui.Dialog().ok("TDT", "Error: {0}".format(e))
        try:
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        except Exception:
            pass