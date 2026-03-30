# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
rtve_catalog.py — RTVE Play: canales en directo y catálogo.

Utiliza la API pública de RTVE
"""
import sys
import json
import time
import os
import hashlib
import core_settings
from urllib.parse import urlencode, quote

import xbmc
import xbmcgui
import xbmcplugin

try:
    import requests
except ImportError:
    import urllib.request
    requests = None

# Constantes
_API_BASE = "https://www.rtve.es/api"

# Canales en directo de RTVE — video IDs para ztnr.rtve.es
_RTVE_LIVE_CHANNELS = [
    {
        "name": "La 1",
        "video_id": "1688877",
        "logo": "https://img2.rtve.es/css/rtve.commons/rtve.header.footer/i/logoLa1.png",
        "plot": "Canal generalista de RTVE con informativos, series, entretenimiento y deportes.",
    },
    {
        "name": "La 2",
        "video_id": "1688885",
        "logo": "https://img2.rtve.es/css/rtve.commons/rtve.header.footer/i/logoLa2.png",
        "plot": "Canal cultural de RTVE con documentales, cine y programas de divulgación.",
    },
    {
        "name": "Canal 24 Horas",
        "video_id": "1694255",
        "logo": "https://img2.rtve.es/css/rtve.commons/rtve.header.footer/i/logo24h.png",
        "plot": "Canal de noticias 24 horas de RTVE.",
    },
    {
        "name": "Teledeporte",
        "video_id": "1712295",
        "logo": "https://img2.rtve.es/css/rtve.commons/rtve.header.footer/i/logoTdp.png",
        "plot": "Canal deportivo de RTVE.",
    },
    {
        "name": "Clan TVE",
        "video_id": "5466990",
        "logo": "https://img2.rtve.es/css/rtve.commons/rtve.header.footer/i/logoClan.png",
        "plot": "Canal infantil de RTVE.",
    },
]

# Categorías principales del catálogo RTVE (IDs de api.rtve.es/api/tematicas/)
_RTVE_CATEGORIES = [
    {"name": "Series", "id": "864", "icon": "DefaultTVShows.png"},
    {"name": "Cine", "id": "866", "icon": "DefaultMovies.png"},
    {"name": "Documentales", "id": "863", "icon": "DefaultMovies.png"},
    {"name": "Informativos", "id": "862", "icon": "DefaultAddonPVRClient.png"},
    {"name": "Entretenimiento", "id": "102610", "icon": "DefaultAddonPVRClient.png"},
    {"name": "Deportes", "id": "867", "icon": "DefaultAddonPVRClient.png"},
    {"name": "Infantiles", "id": "865", "icon": "DefaultAddonPVRClient.png"},
    {"name": "Cultura", "id": "40270", "icon": "DefaultAddonPVRClient.png"},
    {"name": "Música", "id": "102614", "icon": "DefaultMusicSongs.png"},
    {"name": "Cocina", "id": "102611", "icon": "DefaultAddonPVRClient.png"},
    {"name": "Concursos", "id": "102613", "icon": "DefaultAddonPVRClient.png"},
    {"name": "Humor", "id": "102615", "icon": "DefaultAddonPVRClient.png"},
]


# Helpers
def _log(msg):
    xbmc.log("[EspaTV][rtve] {0}".format(msg), xbmc.LOGINFO)


def _log_error(msg):
    xbmc.log("[EspaTV][rtve] ERROR: {0}".format(msg), xbmc.LOGERROR)


def _handle():
    return int(sys.argv[1])


def _u(**kwargs):
    base = sys.argv[0]
    return "{0}?{1}".format(base, urlencode(kwargs))


# HTTP
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/118.0.0.0 Safari/537.36",
}


def _fetch_json(url, timeout=15):
    """Descarga JSON desde una URL con headers, usando cache si está activa."""
    ttl = core_settings.get_iptv_cache_ttl()
    cache_file = ""
    if core_settings.is_iptv_cache_active() and ttl > 0:
        cache_dir = core_settings.get_iptv_cache_dir()
        cache_key = hashlib.md5(url.encode("utf-8")).hexdigest()
        cache_file = os.path.join(cache_dir, "rtvecat_" + cache_key + ".json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if (time.time() - cached.get("ts", 0)) < ttl:
                    return cached.get("content", {})
            except Exception:
                pass

    if requests:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    else:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

    if core_settings.is_iptv_cache_active() and ttl > 0 and cache_file:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "url": url, "content": data}, f, ensure_ascii=False)
        except Exception:
            pass

    return data


# Reproduccion DRM/Widevine
def _play_drm_stream(video_id, title=""):
    """Reproduce un stream RTVE con DRM/Widevine via inputstream.adaptive."""
    h = _handle()
    stream_url = "https://ztnr.rtve.es/ztnr/{0}.mpd".format(video_id)
    _log("DRM play: {0} → {1}".format(title, stream_url))

    # Obtener token Widevine
    license_url = ""
    try:
        token_url = "{0}/token/{1}".format(_API_BASE, video_id)
        token_data = _fetch_json(token_url)
        license_url = token_data.get("widevineURL", "")
        _log("Widevine URL: {0}".format(license_url))
    except Exception as e:
        _log_error("Token error: {0}".format(e))

    # Headers para el stream
    headers = {
        "User-Agent": _HEADERS["User-Agent"],
        "Referer": "https://www.rtve.es/",
        "Origin": "https://www.rtve.es",
        "Accept": "*/*",
    }
    headers_string = "&".join(
        ["{0}={1}".format(k, quote(v)) for k, v in headers.items()]
    )

    try:
        li = xbmcgui.ListItem(path=stream_url)
        if title:
            info_tag = li.getVideoInfoTag()
            info_tag.setMediaType("video")
            info_tag.setTitle(title)

        # inputstream.adaptive para DASH
        li.setProperty("inputstream", "inputstream.adaptive")
        li.setProperty("inputstream.adaptive.manifest_type", "mpd")
        li.setProperty("inputstream.adaptive.manifest_headers", headers_string)
        li.setProperty("inputstream.adaptive.stream_headers", headers_string)

        # DRM Widevine
        if license_url:
            li.setProperty("inputstream.adaptive.license_type",
                           "com.widevine.alpha")
            li.setProperty("inputstream.adaptive.license_key", license_url)

        li.setMimeType("application/dash+xml")
        li.setContentLookup(False)

        # Buffering
        li.setProperty("inputstream.adaptive.stream_selection_type", "adaptive")

        xbmcplugin.setResolvedUrl(h, True, li)
        _log("DRM stream iniciado para: {0}".format(title))

    except Exception as e:
        _log_error("DRM play error: {0}".format(e))
        xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
        xbmcgui.Dialog().notification(
            "RTVE Play", "Error al reproducir",
            xbmcgui.NOTIFICATION_ERROR, 3000)


# Menu principal RTVE
def main_menu():
    """Menú RTVE Play: en directo + catálogo."""
    h = _handle()

    # Canales en directo
    li = xbmcgui.ListItem(
        label="[B][COLOR red]RTVE en Directo[/COLOR][/B]"
    )
    li.setArt({"icon": "DefaultAddonPVRClient.png"})
    li.setInfo("video", {"plot": "Canales de RTVE en directo:\n"
                                 "La 1, La 2, 24h, Teledeporte, Clan"})
    xbmcplugin.addDirectoryItem(
        handle=h, url=_u(action="rtve_live"), listitem=li, isFolder=True
    )

    # Catálogo por categorías
    for cat in _RTVE_CATEGORIES:
        li = xbmcgui.ListItem(label=cat["name"])
        li.setArt({"icon": cat["icon"]})
        li.setInfo("video", {"plot": "Programas de RTVE: {0}".format(
            cat["name"])})
        xbmcplugin.addDirectoryItem(
            handle=h,
            url=_u(action="rtve_category", cat_id=cat["id"],
                   cat_name=cat["name"]),
            listitem=li,
            isFolder=True,
        )

    # Búsqueda
    li = xbmcgui.ListItem(
        label="[COLOR dodgerblue][B]Buscar en RTVE[/B][/COLOR]")
    li.setArt({"icon": "DefaultAddonWebSkin.png"})
    xbmcplugin.addDirectoryItem(
        handle=h, url=_u(action="rtve_search"), listitem=li, isFolder=True
    )

    xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())


# Canales en directo
def live_channels():
    """Lista los canales en directo de RTVE."""
    h = _handle()
    xbmcplugin.setContent(h, "videos")
    for ch in _RTVE_LIVE_CHANNELS:
        li = xbmcgui.ListItem(label="[B]{0}[/B]".format(ch["name"]))
        li.setArt({"icon": ch["logo"], "thumb": ch["logo"]})
        li.setInfo("video", {"title": ch["name"], "plot": ch["plot"]})
        li.setProperty("IsPlayable", "true")
        cm = [("Añadir a Favoritos", "RunPlugin({0})".format(
            _u(action="add_favorite", title=ch["name"],
               fav_url=ch["video_id"], icon=ch["logo"],
               platform="rtve", fav_action="play_rtve_live",
               params=json.dumps({}))
        ))]
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(
            handle=h,
            url=_u(action="play_rtve_live", url=ch["video_id"],
                   title=ch["name"]),
            listitem=li,
            isFolder=False,
        )
    xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())


def play_live(url, title=""):
    """Reproduce un canal en directo de RTVE via DRM."""
    _play_drm_stream(url, title)


# Catalogo por categorias
def category(cat_id, cat_name="", page=1):
    """Lista programas/vídeos de una categoría RTVE."""
    h = _handle()
    dp = xbmcgui.DialogProgress()
    dp.create("RTVE Play", "Cargando {0}...".format(cat_name or "categoría"))
    try:
        dp.update(20, "Cargando subcategorías...")
        # Intentar hijos (subcarpetas de programas)
        hijos_url = "https://api.rtve.es/api/tematicas/{0}/hijos.json?page={1}".format(
            cat_id, page)
        data = _fetch_json(hijos_url)
        hijos = data.get("page", {}).get("items", [])

        if hijos:
            dp.update(60, "Procesando {0} programas...".format(len(hijos)))
            dp.close()
            xbmcplugin.setContent(h, "tvshows")

            for item in hijos:
                item_id = str(item.get("id", ""))
                name = item.get("title", item.get("name", "Sin título"))
                img = ""
                desc = item.get("shortDescription", item.get("description", "")) or ""
                try:
                    prog_url = "https://www.rtve.es/api/programas/{0}".format(
                        item_id)
                    prog_data = _fetch_json(prog_url)
                    prog_info = prog_data.get("page", {}).get("items", [{}])[0]
                    img = (prog_info.get("imgPoster", "") or
                           prog_info.get("imgCol", "") or
                           prog_info.get("thumbnail", "") or
                           prog_info.get("imgBackground", "") or "")
                    if not desc:
                        desc = prog_info.get("shortDescription", prog_info.get("description", "")) or ""
                except Exception:
                    pass

                li = xbmcgui.ListItem(label=name)
                li.setArt({"thumb": img, "icon": img, "poster": img})
                li.setInfo("video", {"title": name, "plot": desc})
                rtve_web_url = "https://www.rtve.es/play/buscador/?query={0}".format(
                    quote(name))
                cm = [
                    ("Buscar en Dailymotion", "Container.Update({0})".format(
                        _u(action="lfr", q=name, ot=""))),
                    ("Ver en RTVE.es", "RunPlugin({0})".format(
                        _u(action="rtve_open_web", url=rtve_web_url, title=name))),
                ]
                li.addContextMenuItems(cm)
                xbmcplugin.addDirectoryItem(
                    handle=h,
                    url=_u(action="rtve_category", cat_id=item_id,
                           cat_name=name),
                    listitem=li,
                    isFolder=True,
                )

            total_pages = data.get("page", {}).get("totalPages", 1)
            current_page = data.get("page", {}).get("number", 1)

            if current_page > 1:
                li = xbmcgui.ListItem(
                    label="[COLOR yellow]← Página anterior ({0}/{1})[/COLOR]".format(
                        current_page - 1, total_pages))
                li.setArt({"icon": "DefaultFolder.png"})
                xbmcplugin.addDirectoryItem(
                    handle=h,
                    url=_u(action="rtve_category", cat_id=cat_id,
                           cat_name=cat_name, page=current_page - 1),
                    listitem=li, isFolder=True)

            if current_page < total_pages:
                li = xbmcgui.ListItem(
                    label="[COLOR limegreen]Página siguiente ({0}/{1})[/COLOR]".format(
                        current_page + 1, total_pages))
                li.setArt({"icon": "DefaultFolder.png"})
                xbmcplugin.addDirectoryItem(
                    handle=h,
                    url=_u(action="rtve_category", cat_id=cat_id,
                           cat_name=cat_name, page=current_page + 1),
                    listitem=li, isFolder=True)

            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
            return

        # Si no hay hijos, intentar vídeos directos
        dp.update(40, "Buscando vídeos...")
        videos_url = "https://api.rtve.es/api/tematicas/{0}/videos.json?page={1}".format(
            cat_id, page)
        data = _fetch_json(videos_url)
        items = data.get("page", {}).get("items", [])

        dp.update(70, "Procesando {0} vídeos...".format(len(items)))
        dp.close()

        if not items:
            xbmcgui.Dialog().notification(
                "RTVE Play", "No se encontraron vídeos",
                xbmcgui.NOTIFICATION_INFO, 3000)
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
            return

        xbmcplugin.setContent(h, "videos")
        for item in items:
            _add_video_item(h, item)

        total_pages = data.get("page", {}).get("totalPages", 1)
        current_page = data.get("page", {}).get("number", 1)

        if current_page > 1:
            li = xbmcgui.ListItem(
                label="[COLOR yellow]← Página anterior ({0}/{1})[/COLOR]".format(
                    current_page - 1, total_pages))
            li.setArt({"icon": "DefaultFolder.png"})
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="rtve_category", cat_id=cat_id,
                       cat_name=cat_name, page=current_page - 1),
                listitem=li, isFolder=True)

        if current_page < total_pages:
            li = xbmcgui.ListItem(
                label="[COLOR limegreen]Página siguiente ({0}/{1})[/COLOR]".format(
                    current_page + 1, total_pages))
            li.setArt({"icon": "DefaultFolder.png"})
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="rtve_category", cat_id=cat_id,
                       cat_name=cat_name, page=current_page + 1),
                listitem=li, isFolder=True)

        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())

    except Exception as e:
        dp.close()
        _log_error("category: {0}".format(e))
        xbmcgui.Dialog().ok("RTVE Play", "Error al cargar:\n{0}".format(e))
        try:
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        except Exception:
            pass


def _add_video_item(h, item):
    """Añade un vídeo del catálogo RTVE al directorio."""
    title = item.get("title", item.get("name", "Sin título"))
    desc = item.get("shortDescription", item.get("description", "")) or ""
    thumb = item.get("thumbnail", item.get("imageSEO", "")) or ""
    try:
        duration = int(item.get("duration", 0) or 0)
    except (ValueError, TypeError):
        duration = 0
    pub_date = item.get("publicationDate", "") or ""
    video_id = str(item.get("id", ""))

    li = xbmcgui.ListItem(label=title)
    li.setArt({"thumb": thumb, "icon": thumb, "poster": thumb})

    plot = desc
    if pub_date:
        plot += "\n\nPublicado: {0}".format(pub_date[:10])
    li.setInfo("video", {
        "title": title,
        "plot": plot,
        "duration": duration // 1000 if duration else 0,
    })
    li.setProperty("IsPlayable", "true")

    web_url = item.get("htmlShortUrl", item.get("htmlUrl", "")) or ""
    if not web_url and video_id:
        web_url = "https://www.rtve.es/v/{0}/".format(video_id)
    cm = [
        ("Buscar en Dailymotion", "Container.Update({0})".format(
            _u(action="lfr", q=title, ot=""))),
        ("Ver en RTVE.es", "RunPlugin({0})".format(
            _u(action="rtve_open_web", url=web_url, title=title))),
    ]
    if web_url:
        cm.append(("Copiar URL", "RunPlugin({0})".format(
            _u(action="copy_url", url=web_url))))
    li.addContextMenuItems(cm)

    xbmcplugin.addDirectoryItem(
        handle=h,
        url=_u(action="play_rtve_video", video_id=video_id, title=title),
        listitem=li,
        isFolder=False,
    )


def play_video(video_id, title=""):
    """Reproduce un vídeo del catálogo RTVE via DRM."""
    _play_drm_stream(video_id, title)


# Busqueda en RTVE
def search():
    """Buscar programas en RTVE."""
    h = _handle()
    kb = xbmc.Keyboard("", "Buscar en RTVE Play")
    kb.doModal()
    if not kb.isConfirmed() or not kb.getText().strip():
        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        return
    query = kb.getText().strip()
    dp = xbmcgui.DialogProgress()
    dp.create("RTVE Play", "Buscando '{0}'...".format(query))
    try:
        dp.update(30, "Consultando api.rtve.es...")
        url = ("https://api.rtve.es/api/search/contents"
               "?search={0}&page=1&size=30&context=tve"
               "&type=completo&tipology=video"
               "&isExpanded=true&isChild=true"
               "&useOntology=false").format(quote(query))
        data = _fetch_json(url)
        dp.update(70, "Procesando resultados...")
        dp.close()

        items = data.get("page", {}).get("items", [])
        if not items:
            xbmcgui.Dialog().notification(
                "RTVE Play",
                "Sin resultados para '{0}'".format(query),
                xbmcgui.NOTIFICATION_INFO, 3000)
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
            return

        xbmcplugin.setContent(h, "videos")
        for item in items:
            _add_video_item(h, item)
        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())

    except Exception as e:
        dp.close()
        _log_error("search: {0}".format(e))
        xbmcgui.Dialog().ok("RTVE Play",
                            "Error en la búsqueda:\n{0}".format(e))
        try:
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        except Exception:
            pass


def open_web(url, title=""):
    """Abre un vídeo de RTVE en el navegador (fallback)."""
    if not url:
        xbmcgui.Dialog().notification("RTVE Play", "URL no disponible",
                                      xbmcgui.NOTIFICATION_WARNING, 2000)
        return
    _log("Abriendo en navegador: {0}".format(url))
    import webbrowser
    try:
        webbrowser.open(url)
        xbmcgui.Dialog().notification(
            "RTVE Play",
            "Abriendo {0} en navegador...".format(title or "vídeo"),
            xbmcgui.NOTIFICATION_INFO, 3000)
    except Exception as e:
        _log_error("open_web: {0}".format(e))
        xbmcgui.Dialog().ok(
            "RTVE Play",
            "No se pudo abrir el navegador.\n\nURL: {0}".format(url))