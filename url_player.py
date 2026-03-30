# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
Reproductor de URLs Externas para EspaTV

Permite al usuario pegar una URL de vídeo y reproducirla en Kodi.
--
Inicia un mini servidor en la red local que sirve una pagina web
donde el usuario puede pegar una URL. 
Al enviar, Kodi lo recibe y actua.

Incluye controles de reproduccion y estado en tiempo real
mediante JSON-RPC de Kodi.

Idea original de RubenSDFA1labernt:
La implementación de un servidor HTTP local efímero que se levanta bajo demanda
para enviar URLs o texto a Kodi y se cierra automáticamente al terminar, sin dejar puertos abiertos ni obligar
al usuario a activar el control HTTP en los ajustes del sistema Kodi.

Si decides utilizar este código en tus propios desarrollos o te inspiras en esta idea original, 
es de agradecer que menciones este proyecto.

Detección inteligente:
  - Dailymotion (dailymotion.com / dai.ly)   -> dm_gujal
  - YouTube     (youtube.com / youtu.be)      -> plugin.video.youtube
  - Streams     (.m3u8 / .mp4 / .mpd / etc.)  -> reproducción directa
  - Torrents    (magnet: / .torrent)           -> plugin.video.elementum
  - AceStream   (acestream://)                -> script.module.horus / program.plexus
  - Otras webs                                -> SendToKodi / yt-dlp
"""
# noinspection PyUnresolvedReferences
import sys
import os
import json
import time
import re
import urllib.parse
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs
import ytdlp_resolver

_ADDON_ID = "plugin.video.espatv"
_profile_path = None
MAX_HISTORY = 20
MAX_BOOKMARKS = 50

_STREAM_EXTENSIONS = {
    ".m3u8", ".mp4", ".mkv", ".avi", ".ts", ".flv",
    ".mov", ".wmv", ".webm", ".mpd", ".m3u",
}

_ACESTREAM_RE = re.compile(r"^acestream://[0-9a-fA-F]{40}$")


def _get_profile():
    global _profile_path
    if _profile_path is None:
        _profile_path = xbmcvfs.translatePath(
            xbmcaddon.Addon().getAddonInfo("profile")
        )
        if not os.path.exists(_profile_path):
            os.makedirs(_profile_path)
    return _profile_path


def _history_path():
    return os.path.join(_get_profile(), "url_history.json")


def _bookmarks_path():
    return os.path.join(_get_profile(), "url_bookmarks.json")


def _load_json(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except IOError:
        pass


def parse_url_with_headers(raw_url):
    """Separa URL y headers en formato pipe.

    Entrada: "http://example.com/video.m3u8|User-Agent=xxx&Referer=yyy"
    Salida:  ("http://example.com/video.m3u8", {"User-Agent": "xxx", ...})
    """
    if not raw_url:
        return "", {}
    raw_url = raw_url.strip()
    if "|" not in raw_url:
        return raw_url, {}

    parts = raw_url.split("|", 1)
    url = parts[0].strip()
    headers_str = parts[1].strip()
    if not headers_str:
        return url, {}

    headers = {}
    for pair in headers_str.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            if key:
                headers[key] = value
    return url, headers


def is_direct_stream(url):
    """Determina si la URL apunta a un stream reproducible directamente."""
    try:
        parsed = urllib.parse.urlparse(url)
        _, ext = os.path.splitext(parsed.path.lower())
        return ext in _STREAM_EXTENSIONS
    except Exception:
        return False


def build_kodi_url(url, headers=None):
    """Reconstruye URL con headers en formato pipe de Kodi."""
    if not headers:
        return url
    parts = ["{0}={1}".format(k, v) for k, v in headers.items()]
    return url + "|" + "&".join(parts)


def detect_dailymotion_id(url):
    """Extrae el ID de un vídeo de Dailymotion o devuelve None."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        if host.endswith("dailymotion.com"):
            match = re.match(r"/video/([a-zA-Z0-9]+)", parsed.path)
            if match:
                return match.group(1)
            match = re.match(r"/embed/video/([a-zA-Z0-9]+)", parsed.path)
            if match:
                return match.group(1)
        elif host == "dai.ly":
            vid = parsed.path.strip("/")
            if vid:
                return vid.split("_")[0].split("?")[0]
    except Exception:
        pass
    return None


def detect_youtube_id(url):
    """Extrae el ID de un vídeo de YouTube o devuelve None."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        if host in ("youtube.com", "m.youtube.com"):
            if "/watch" in parsed.path:
                params = urllib.parse.parse_qs(parsed.query)
                vid_list = params.get("v", [])
                if vid_list:
                    return vid_list[0]
            elif "/shorts/" in parsed.path:
                match = re.match(r"/shorts/([a-zA-Z0-9_-]+)", parsed.path)
                if match:
                    return match.group(1)
            elif "/live/" in parsed.path:
                match = re.match(r"/live/([a-zA-Z0-9_-]+)", parsed.path)
                if match:
                    return match.group(1)
        elif host == "youtu.be":
            vid = parsed.path.strip("/")
            if vid:
                return vid
    except Exception:
        pass
    return None


def detect_url_type(raw_url):
    """Clasifica una URL y devuelve (tipo, id_o_url).
    Tipos: 'dailymotion', 'youtube', 'rtve', 'atresplayer', 'torrent',
           'acestream', 'direct_stream', 'web_page'
    """
    url, _ = parse_url_with_headers(raw_url)

    # Magnet links y ficheros .torrent
    if url.startswith("magnet:"):
        return "torrent", url
    try:
        parsed_path = urllib.parse.urlparse(url).path.lower()
        if parsed_path.endswith(".torrent"):
            return "torrent", url
    except Exception:
        pass

    # AceStream
    if _ACESTREAM_RE.match(url):
        return "acestream", url

    dm_id = detect_dailymotion_id(url)
    if dm_id:
        return "dailymotion", dm_id

    yt_id = detect_youtube_id(url)
    if yt_id:
        return "youtube", yt_id

    # RTVE Play
    rtve_match = re.match(
        r"https?://(?:www\.)?rtve\.es/(?:play/videos/.+?/|v/)(\d+)/?", url)
    if rtve_match:
        return "rtve", rtve_match.group(1)

    # A3pl._ayer
    if re.match(r"https?://(?:www\.)?atresplayer\.com/.+", url):
        return "atresplayer", url

    if is_direct_stream(url):
        return "direct_stream", raw_url

    return "web_page", url

_YT_RE = re.compile(
    r"(?:youtube\.com/watch\?.*?v=|youtu\.be/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})"
)
_DM_RE = re.compile(
    r"(?:dailymotion\.com/video/|dai\.ly/)([A-Za-z0-9]+)"
)
_VIMEO_RE = re.compile(r"vimeo\.com/(\d+)")
_OG_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
    r'|content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\'])',
    re.IGNORECASE,
)
_THUMB_TIMEOUT = 3


def _auto_thumb(url):
    """Devuelve la URL de miniatura para la URL dada, o cadena vacia.

    Soporta YouTube, Dailymotion (sin HTTP), Vimeo (oEmbed) y webs
    genericas (og:image). En caso de error devuelve cadena vacia.
    """
    if not url:
        return ""
    # YouTube — sin peticion HTTP
    m = _YT_RE.search(url)
    if m:
        return "https://img.youtube.com/vi/{0}/hqdefault.jpg".format(m.group(1))
    # Dailymotion — sin peticion HTTP
    m = _DM_RE.search(url)
    if m:
        return "https://www.dailymotion.com/thumbnail/video/{0}".format(m.group(1))
    # Vimeo — oEmbed API (sin autenticacion)
    m = _VIMEO_RE.search(url)
    if m:
        return _vimeo_thumb(url)
    # Webs genericas — intentar og:image
    if url.startswith("http"):
        return _og_thumb(url)
    return ""


def _vimeo_thumb(url):
    """Obtiene miniatura de Vimeo via oEmbed."""
    try:
        import requests
        r = requests.get(
            "https://vimeo.com/api/oembed.json",
            params={"url": url, "width": 480},
            timeout=_THUMB_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json().get("thumbnail_url", "")
    except Exception:
        pass
    return ""


def _og_thumb(url):
    """Extrae la meta tag og:image del HTML de una pagina web."""
    try:
        import requests
        r = requests.get(
            url,
            timeout=_THUMB_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
            stream=True,
        )
        try:
            if r.status_code != 200:
                return ""
            chunk = r.raw.read(8192).decode("utf-8", errors="ignore")
            m = _OG_RE.search(chunk)
            if m:
                return m.group(1) or m.group(2) or ""
        finally:
            r.close()
    except Exception:
        pass
    return ""


def _auto_label(url):
    """Genera un titulo legible para la URL dada.

    YouTube/DM/Vimeo: titulo real via oEmbed (sin autenticacion).
    Otras URLs: nombre derivado del path.
    """
    if not url:
        return ""
    try:
        import requests
        # YouTube
        m = _YT_RE.search(url)
        if m:
            r = requests.get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
                timeout=3,
            )
            if r.status_code == 200:
                return r.json().get("title", "")
        # Dailymotion
        m = _DM_RE.search(url)
        if m:
            r = requests.get(
                "https://www.dailymotion.com/services/oembed",
                params={"url": url, "format": "json"},
                timeout=3,
            )
            if r.status_code == 200:
                return r.json().get("title", "")
        # Vimeo
        m = _VIMEO_RE.search(url)
        if m:
            r = requests.get(
                "https://vimeo.com/api/oembed.json",
                params={"url": url},
                timeout=3,
            )
            if r.status_code == 200:
                return r.json().get("title", "")
    except Exception:
        pass
    # Fallback: limpiar path de la URL
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        if "." in path:
            path = path.rsplit(".", 1)[0]
        name = path.replace("-", " ").replace("_", " ")
        return name[:80] if name else ""
    except Exception:
        return ""


def _add_to_history(raw_url, label=None, thumb=None):
    history = _load_json(_history_path())
    # Reutilizar thumb y label de la entrada existente
    if not thumb or not label:
        for h in history:
            if h.get("url") == raw_url:
                if not thumb and h.get("thumb"):
                    thumb = h["thumb"]
                if not label and h.get("label") and h["label"] != raw_url:
                    label = h["label"]
                break
    history = [h for h in history if h.get("url") != raw_url]
    if not thumb:
        thumb = _auto_thumb(raw_url)
    if not label or label == raw_url:
        fetched = _auto_label(raw_url)
        label = fetched or raw_url
    entry = {
        "url": raw_url,
        "label": label,
        "date": time.strftime("%d/%m/%Y %H:%M"),
        "timestamp": int(time.time()),
    }
    if thumb:
        entry["thumb"] = thumb
    history.insert(0, entry)
    history = history[:MAX_HISTORY]
    _save_json(_history_path(), history)


def _get_history():
    return _load_json(_history_path())


def _clear_history():
    _save_json(_history_path(), [])


def _remove_from_history(url):
    history = _get_history()
    history = [h for h in history if h.get("url") != url]
    _save_json(_history_path(), history)


def _get_bookmarks():
    return _load_json(_bookmarks_path())


def _add_bookmark(name, url):
    bookmarks = _get_bookmarks()
    if any(b.get("url") == url for b in bookmarks):
        return False
    thumb = _auto_thumb(url)
    entry = {
        "name": name,
        "url": url,
        "date": time.strftime("%d/%m/%Y %H:%M"),
    }
    if thumb:
        entry["thumb"] = thumb
    bookmarks.append(entry)
    bookmarks = bookmarks[:MAX_BOOKMARKS]
    _save_json(_bookmarks_path(), bookmarks)
    return True


def _remove_bookmark(url):
    bookmarks = _get_bookmarks()
    new_bookmarks = [b for b in bookmarks if b.get("url") != url]
    _save_json(_bookmarks_path(), new_bookmarks)
    return len(bookmarks) != len(new_bookmarks)


def _rename_bookmark(url, new_name):
    bookmarks = _get_bookmarks()
    changed = False
    for b in bookmarks:
        if b.get("url") == url:
            b["name"] = new_name
            changed = True
            break
    if changed:
        _save_json(_bookmarks_path(), bookmarks)
    return changed


def _u(**kwargs):
    return sys.argv[0] + "?" + urllib.parse.urlencode(kwargs)


def open_url_dialog():
    """Menú principal de Abrir URL."""
    h = int(sys.argv[1])

    li = xbmcgui.ListItem(
        label="[COLOR limegreen][B]Pegar URL de vídeo[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultAddSource.png"})
    li.setInfo("video", {
        "plot": (
            "Pega una URL de vídeo para reproducirla.\n\n"
            "URLs soportadas:\n"
            "  • Dailymotion: dailymotion.com/video/xxx\n"
            "  • YouTube: youtube.com/watch?v=xxx\n"
            "  • Torrent / Magnet: .torrent, magnet:?\n"
            "  • AceStream: acestream://\n"
            "  • Stream directo: .m3u8, .mp4, .mkv, .mpd\n"
            "  • Con headers: URL|User-Agent=xxx&Referer=yyy\n"
            "  • Otras webs: resolución automática"
        ),
    })
    xbmcplugin.addDirectoryItem(
        handle=h, url=_u(action="url_input"), listitem=li, isFolder=False
    )

    try:
        clipboard_url = xbmcgui.Window(10000).getProperty("espatv.clipboard.url").strip()
    except Exception:
        clipboard_url = ""
    if clipboard_url:
        url_display = clipboard_url[:70] + "..." if len(clipboard_url) > 70 else clipboard_url
        li = xbmcgui.ListItem(
            label="[COLOR cyan][B]Reproducir URL copiada[/B][/COLOR]"
        )
        li.setArt({"icon": "DefaultAddonsUpdates.png"})
        li.setInfo("video", {
            "plot": (
                "URL en memoria:\n{0}\n\n"
                "Pulsa para reproducir directamente "
                "sin necesidad de escribir."
            ).format(url_display),
        })
        xbmcplugin.addDirectoryItem(
            handle=h, url=_u(action="url_play_clipboard"),
            listitem=li, isFolder=False
        )

    li = xbmcgui.ListItem(
        label="[COLOR lightsalmon][B]Enviar URL desde movil/PC[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultNetwork.png"})
    li.setInfo("video", {
        "plot": (
            "Abre un servidor temporal en la red local.\n\n"
            "Desde el navegador de tu movil o PC, "
            "podras pegar la URL sin necesidad de escribirla "
            "con el mando.\n\n"
            "Ideal para dispositivos sin teclado.\n\n"
            "Tambien disponible como addon independiente más completo:\n"
            "  • LoioLink - Enviar URLs y texto\n"
            "  • LoioMote - Control remoto web\n"
            "Ambos con API para integrar en otros addons."
        ),
    })
    xbmcplugin.addDirectoryItem(
        handle=h, url=_u(action="url_remote"), listitem=li, isFolder=False
    )

    li = xbmcgui.ListItem(
        label="[COLOR mediumpurple][B]Escanear vídeos de una web[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultAddonsSearch.png"})
    li.setInfo("video", {
        "plot": (
            "Pega la URL de cualquier página web y el addon "
            "detectará todos los vídeos disponibles.\n\n"
            "Muestra título, duración y tamaño de cada uno.\n\n"
            "En PC usa yt-dlp (más completo).\n"
            "En Android usa análisis HTML."
        ),
    })
    xbmcplugin.addDirectoryItem(
        handle=h, url=_u(action="url_scan"), listitem=li, isFolder=False
    )

    history = _get_history()
    if history:
        li = xbmcgui.ListItem(
            label="[COLOR cyan]Historial de URLs ({0})[/COLOR]".format(len(history))
        )
        li.setArt({"icon": "DefaultRecentlyAddedMovies.png"})
        xbmcplugin.addDirectoryItem(
            handle=h, url=_u(action="url_history"), listitem=li, isFolder=True
        )

    bookmarks = _get_bookmarks()
    if bookmarks:
        li = xbmcgui.ListItem(
            label="[COLOR khaki]Marcadores ({0})[/COLOR]".format(len(bookmarks))
        )
        li.setArt({"icon": "DefaultSets.png"})
        xbmcplugin.addDirectoryItem(
            handle=h, url=_u(action="url_bookmarks"), listitem=li, isFolder=True
        )

    xbmcplugin.endOfDirectory(h)


def _play_resolved_stream(stream_url, headers=None, protocol=""):
    """Reproduce una URL resuelta, configurando inputstream y headers.

    Args:
        stream_url: URL directa del stream.
        headers:    Dict de cabeceras HTTP de yt-dlp (opcional).
        protocol:   Protocolo reportado por yt-dlp (opcional).
    """
    li = xbmcgui.ListItem(path=stream_url)

    # Extraer solo el path real (ignorar query ?...)
    clean_path = urllib.parse.urlparse(stream_url).path.lower()

    # Filtrar headers: yt-dlp siempre incluye headers genéricos del navegador
    # (User-Agent, Accept, Accept-Language, Sec-Fetch-*). Solo inyectamos
    # headers que realmente importan para la autenticación del stream.
    _GENERIC_HEADERS = {
        "user-agent", "accept", "accept-language", "accept-encoding",
        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
        "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    }
    meaningful_headers = {}
    if headers:
        meaningful_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in _GENERIC_HEADERS
        }

    # Codificar headers para setProperty (formato key=value&key2=value2)
    header_str = ""
    if meaningful_headers:
        header_str = "&".join(
            "{0}={1}".format(k, urllib.parse.quote(str(v)))
            for k, v in meaningful_headers.items()
        )

    is_mpd = clean_path.endswith(".mpd") or "mpd" in protocol
    is_m3u8 = clean_path.endswith(".m3u8") or "m3u8" in protocol

    if is_mpd:
        li.setProperty("inputstream", "inputstream.adaptive")
        li.setProperty("inputstream.adaptive.manifest_type", "mpd")
        li.setMimeType("application/dash+xml")
        if header_str:
            li.setProperty("inputstream.adaptive.manifest_headers", header_str)
            li.setProperty("inputstream.adaptive.stream_headers", header_str)
    elif is_m3u8:
        li.setMimeType("application/x-mpegURL")
        if header_str:
            # Solo activar inputstream.adaptive si hay headers que inyectar
            li.setProperty("inputstream", "inputstream.adaptive")
            li.setProperty("inputstream.adaptive.manifest_headers", header_str)
            li.setProperty("inputstream.adaptive.stream_headers", header_str)
    elif header_str:
        # Para mp4/otros formatos directos, usar pipe (aun soportado)
        stream_url = "{0}|{1}".format(stream_url, header_str)
        li = xbmcgui.ListItem(path=stream_url)

    li.setProperty("IsPlayable", "true")
    li.setContentLookup(False)
    xbmc.Player().play(stream_url, li)
    

def _resolve_and_play(raw_url):
    """Detecta tipo de URL y reproduce con el método adecuado."""
    url_type, value = detect_url_type(raw_url)

    if url_type == "dailymotion":
        xbmcgui.Dialog().notification(
            "EspaTV", "Dailymotion: " + value,
            xbmcgui.NOTIFICATION_INFO, 2000,
        )
        played = False

        # Método 1: yt-dlp (mejor compatibilidad CDN en PC)
        if ytdlp_resolver.is_available():
            dm_url = "https://www.dailymotion.com/video/" + value
            info = ytdlp_resolver.resolve_full(dm_url)
            if info and info.get("url"):
                _play_resolved_stream(
                    info["url"],
                    headers=info.get("headers"),
                    protocol=info.get("protocol", ""),
                )
                played = True

        # Método 2: resolver interno del addon (_dm_play)
        if not played:
            try:
                import default
                result = default._dm_play(value)
                if result and result.get('url'):
                    stream_url = result['url']
                    
                    # Codificar headers para que Kodi los incluya en las peticiones al CDN
                    headers_dict = result.get('headers')
                    header_str = ""
                    if headers_dict:
                        header_str = "&".join(
                            "{0}={1}".format(k, urllib.parse.quote(str(v)))
                            for k, v in headers_dict.items()
                        )
                        
                    mime_type = result.get('mime', '')
                    is_hls = 'mpegURL' in mime_type
                    
                    if not is_hls and header_str:
                        stream_url = "{0}|{1}".format(stream_url, header_str)

                    li = xbmcgui.ListItem(path=stream_url)
                    if mime_type:
                        li.setMimeType(mime_type)
                        
                    if is_hls and header_str:
                        # inputstream.adaptive necesita los headers en sus properties
                        li.setProperty("inputstream", "inputstream.adaptive")
                        li.setProperty("inputstream.adaptive.manifest_headers", header_str)
                        li.setProperty("inputstream.adaptive.stream_headers", header_str)
                        
                    li.setContentLookup(False)
                    subs = result.get('subs')
                    if subs and isinstance(subs, list):
                        li.setSubtitles(subs)
                    li.setProperty("IsPlayable", "true")
                    xbmc.Player().play(stream_url, li)
                    played = True
            except Exception as e:
                xbmc.log(
                    "[EspaTV/url_player] _dm_play error: " + str(e),
                    xbmc.LOGWARNING,
                )

        # Método 3: navegador como último recurso
        if not played:
            dm_web = "https://www.dailymotion.com/video/" + value
            is_android = xbmc.getCondVisibility("System.Platform.Android")
            if is_android:
                opts = ["Abrir en el navegador", "Abrir en app externa"]
                sel = xbmcgui.Dialog().select("No se pudo resolver", opts)
                if sel < 0: return
                if sel == 1:
                    xbmc.executebuiltin(
                        'StartAndroidActivity("",'
                        '"android.intent.action.VIEW","",'
                        '"{0}")'.format(dm_web))
                else:
                    import webbrowser
                    webbrowser.open(dm_web)
            else:
                if xbmcgui.Dialog().yesno(
                    "EspaTV",
                    "No se pudo resolver el vídeo de Dailymotion.\n\n"
                    "¿Abrir en el navegador?"):
                    import webbrowser
                    webbrowser.open(dm_web)

    elif url_type == "youtube":
        yt_web = "https://www.youtube.com/watch?v=" + value
        is_android = xbmc.getCondVisibility("System.Platform.Android")

        # Construir menú de opciones estilo Felicidad
        opts = []
        actions = []

        # Addon YouTube (no pide API key para reproducir)
        if xbmc.getCondVisibility("System.HasAddon(plugin.video.youtube)"):
            opts.append("Reproducir aquí")
            actions.append("addon")
        # yt-dlp (PC)
        if ytdlp_resolver.is_available():
            opts.append("Reproducir con yt-dlp")
            actions.append("ytdlp")
        # App YouTube nativa (solo Android)
        if is_android:
            opts.append("Abrir en la app YouTube")
            actions.append("yt_app")
        # Navegador (siempre)
        opts.append("Abrir en el navegador")
        actions.append("browser")

        # Si solo hay una opción, ejecutar directamente
        if len(opts) == 1:
            sel = 0
        else:
            sel = xbmcgui.Dialog().select("YouTube", opts)
        if sel < 0:
            return

        act = actions[sel]
        if act == "ytdlp":
            xbmcgui.Dialog().notification(
                "EspaTV", "Resolviendo con yt-dlp...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            info = ytdlp_resolver.resolve_full(yt_web)
            if info and info.get("url"):
                _play_resolved_stream(
                    info["url"],
                    headers=info.get("headers"),
                    protocol=info.get("protocol", ""),
                )
            else:
                xbmcgui.Dialog().ok("EspaTV", "yt-dlp no pudo resolver el vídeo.")
        elif act == "addon":
            xbmc.executebuiltin(
                'PlayMedia("plugin://plugin.video.youtube/play/?video_id={0}")'.format(value))
        elif act == "yt_app":
            xbmc.executebuiltin(
                'StartAndroidActivity(com.google.android.youtube,'
                '"android.intent.action.VIEW","",'
                '"{0}")'.format(yt_web))
        elif act == "browser":
            import webbrowser
            webbrowser.open(yt_web)


    elif url_type == "direct_stream":
        url, headers = parse_url_with_headers(raw_url)
        kodi_url = build_kodi_url(url, headers)
        li = xbmcgui.ListItem(path=kodi_url)
        if ".mpd" in url.lower():
            li.setProperty("inputstream", "inputstream.adaptive")
            li.setProperty("inputstream.adaptive.manifest_type", "mpd")
        elif ".m3u8" in url.lower():
            li.setMimeType("application/x-mpegURL")
        if headers:
            header_str = "&".join("{0}={1}".format(k, v) for k, v in headers.items())
            li.setProperty("inputstream.adaptive.stream_headers", header_str)
        xbmc.Player().play(kodi_url, li)

    elif url_type == "torrent":
        has_elementum = xbmc.getCondVisibility(
            "System.HasAddon(plugin.video.elementum)"
        )
        if has_elementum:
            xbmcgui.Dialog().notification(
                "EspaTV", "Abriendo torrent en Elementum...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            elem_url = (
                "plugin://plugin.video.elementum/play?"
                + urllib.parse.urlencode({"uri": value})
            )
            xbmc.executebuiltin('PlayMedia("{0}")'.format(elem_url))
        else:
            xbmcgui.Dialog().ok(
                "EspaTV",
                "Para reproducir torrents necesitas instalar "
                "el addon Elementum.\n\n"
                "Descargalo desde su repositorio oficial en GitHub.",
            )

    elif url_type == "acestream":
        # Extraer hash del content ID
        ace_hash = value.replace("acestream://", "")
        has_horus = xbmc.getCondVisibility(
            "System.HasAddon(script.module.horus)"
        )
        has_plexus = xbmc.getCondVisibility(
            "System.HasAddon(program.plexus)"
        )
        if has_horus:
            xbmcgui.Dialog().notification(
                "EspaTV", "Abriendo AceStream con Horus...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            horus_url = (
                "plugin://script.module.horus/?"
                + urllib.parse.urlencode({"action": "play", "id": ace_hash})
            )
            xbmc.executebuiltin('PlayMedia("{0}")'.format(horus_url))
        elif has_plexus:
            xbmcgui.Dialog().notification(
                "EspaTV", "Abriendo AceStream con Plexus...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            plexus_url = (
                "plugin://program.plexus/?"
                + urllib.parse.urlencode({
                    "url": value, "mode": "1", "name": "AceStream"
                })
            )
            xbmc.executebuiltin('PlayMedia("{0}")'.format(plexus_url))
        else:
            xbmcgui.Dialog().ok(
                "EspaTV",
                "Para reproducir enlaces AceStream necesitas "
                "instalar uno de estos addons:\n\n"
                "  • Horus (script.module.horus)  [recomendado]\n"
                "  • Plexus (program.plexus)\n\n"
                "Ademas necesitas el motor AceStream instalado "
                "en el dispositivo.",
            )

    elif url_type == "rtve":
        xbmcgui.Dialog().notification(
            "EspaTV", "Reproduciendo vídeo...",
            xbmcgui.NOTIFICATION_INFO, 2000,
        )
        try:
            import rtve_catalog
            stream_url = "https://ztnr.rtve.es/ztnr/{0}.mpd".format(value)
            # Obtener token Widevine
            license_url = ""
            try:
                token_data = rtve_catalog._fetch_json(
                    "{0}/token/{1}".format(rtve_catalog._API_BASE, value))
                license_url = token_data.get("widevineURL", "")
            except Exception:
                pass

            headers = {
                "User-Agent": rtve_catalog._HEADERS["User-Agent"],
                "Referer": "https://www.rtve.es/",
                "Origin": "https://www.rtve.es",
            }
            headers_string = "&".join(
                ["{0}={1}".format(k, urllib.parse.quote(v)) for k, v in headers.items()])

            li = xbmcgui.ListItem(path=stream_url)
            li.setProperty("inputstream", "inputstream.adaptive")
            li.setProperty("inputstream.adaptive.manifest_type", "mpd")
            li.setProperty("inputstream.adaptive.manifest_headers", headers_string)
            li.setProperty("inputstream.adaptive.stream_headers", headers_string)
            if license_url:
                li.setProperty("inputstream.adaptive.license_type",
                               "com.widevine.alpha")
                li.setProperty("inputstream.adaptive.license_key", license_url)
            li.setMimeType("application/dash+xml")
            li.setContentLookup(False)
            xbmc.Player().play(stream_url, li)
        except Exception as e:
            xbmc.log(
                "[EspaTV/url_player] RTVE error: " + str(e),
                xbmc.LOGWARNING,
            )
            xbmcgui.Dialog().ok(
                "EspaTV",
                "Error al reproducir:\n{0}".format(str(e)),
            )

    elif url_type == "atresplayer":
        if ytdlp_resolver.is_available():
            xbmcgui.Dialog().notification(
                "EspaTV", "Resolviendo con yt-dlp...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            info = ytdlp_resolver.resolve_full(value)
            if info and info.get("url"):
                _play_resolved_stream(
                    info["url"],
                    headers=info.get("headers"),
                    protocol=info.get("protocol", ""),
                )
                return

        # Fallback: navegador/app
        is_android = xbmc.getCondVisibility("System.Platform.Android")
        if is_android:
            opts = ["Abrir en el navegador", "Abrir en app externa"]
            sel = xbmcgui.Dialog().select(
                "No se pudo reproducir (puede requerir login)", opts)
            if sel < 0: return
            if sel == 1:
                xbmc.executebuiltin(
                    'StartAndroidActivity("",'
                    '"android.intent.action.VIEW","",'
                    '"{0}")'.format(value))
            else:
                import webbrowser
                webbrowser.open(value)
        else:
            msg = ("No se pudo reproducir este contenido.\n\n"
                   "Es posible que requiera login o sea premium.\n\n"
                   "¿Abrir en el navegador?")
            if not ytdlp_resolver.is_available():
                msg = ("Se necesita yt-dlp para este contenido.\n\n"
                       "Instala con: pip install yt-dlp\n\n"
                       "¿Abrir en el navegador?")
            if xbmcgui.Dialog().yesno("EspaTV", msg):
                import webbrowser
                webbrowser.open(value)

    elif url_type == "web_page":
        # 1. yt-dlp (soporta cientos de webs de forma nativa)
        if ytdlp_resolver.is_available():
            xbmcgui.Dialog().notification(
                "EspaTV", "Resolviendo con yt-dlp...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            info = ytdlp_resolver.resolve_full(value)
            if info and info.get("url"):
                _play_resolved_stream(
                    info["url"],
                    headers=info.get("headers"),
                    protocol=info.get("protocol", ""),
                )
                return

        # 2. SendToKodi
        # Agradecimientos especiales a SendToKodi por su trabajo y servir de inspiración.
        has_stk = xbmc.getCondVisibility(
            "System.HasAddon(plugin.video.sendtokodi)"
        )
        if has_stk:
            xbmcgui.Dialog().notification(
                "EspaTV", "Resolviendo con SendToKodi...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            stk_url = (
                "plugin://plugin.video.sendtokodi/?"
                + urllib.parse.urlencode({"url": value})
            )
            xbmc.executebuiltin('PlayMedia("{0}")'.format(stk_url))
            return

        # 3. YouTube addon (último recurso)
        has_yt = xbmc.getCondVisibility(
            "System.HasAddon(plugin.video.youtube)"
        )
        if has_yt:
            xbmcgui.Dialog().notification(
                "EspaTV", "Intentando resolver...",
                xbmcgui.NOTIFICATION_INFO, 2000,
            )
            yt_url = (
                "plugin://plugin.video.youtube/uri2addon/?uri="
                + urllib.parse.quote(value)
            )
            xbmc.executebuiltin('PlayMedia("{0}")'.format(yt_url))
        else:
            # 4. Abrir en navegador / app nativa
            is_android = xbmc.getCondVisibility("System.Platform.Android")
            if is_android:
                opts = ["Abrir en el navegador", "Abrir en app externa"]
                sel = xbmcgui.Dialog().select("No se puede resolver aquí", opts)
                if sel < 0:
                    return
                if sel == 1:
                    xbmc.executebuiltin(
                        'StartAndroidActivity("",'
                        '"android.intent.action.VIEW","",'
                        '"{0}")'.format(value))
                else:
                    import webbrowser
                    webbrowser.open(value)
            else:
                if xbmcgui.Dialog().yesno(
                    "EspaTV",
                    "No se pudo resolver esta URL.\n\n"
                    "¿Abrir en el navegador?"):
                    import webbrowser
                    webbrowser.open(value)



def play_clipboard():
    """Reproduce directamente la URL del portapapeles de la sesión."""
    try:
        raw = xbmcgui.Window(10000).getProperty("espatv.clipboard.url").strip()
    except Exception:
        raw = ""

    if not raw:
        xbmcgui.Dialog().notification(
            "EspaTV", "No hay URL copiada en memoria",
            xbmcgui.NOTIFICATION_WARNING, 2000
        )
        return

    _add_to_history(raw)
    _resolve_and_play(raw)


def _get_download_dest(title, ext=".mp4"):
    """Solicita al usuario la carpeta de destino y devuelve la ruta final.

    Usa la ruta preconfigurada en los ajustes como punto de partida.
    Devuelve cadena vacia si el usuario cancela.
    """
    import default
    cfg = default._load_dm_settings()
    saved_path = cfg.get("download_path", "")

    dest_dir = xbmcgui.Dialog().browse(
        3, "Selecciona donde guardar", "video",
        "", False, False, saved_path,
    )
    if not dest_dir:
        return ""

    safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
    if not safe_title:
        safe_title = "espatv_download_{0}".format(int(time.time()))
    if len(safe_title) > 120:
        safe_title = safe_title[:120].rstrip()

    return os.path.join(dest_dir, "{0}{1}".format(safe_title, ext))


def _download_direct_url(url, dest, title, headers=None):
    """Descarga un archivo por HTTP con barra de progreso.

    Funciona con cualquier URL directa (MP4, MKV, etc.) sin
    depender de headers especificos de ninguna plataforma.
    """
    import requests
    dp = xbmcgui.DialogProgress()
    dp.create("Descargando", "Conectando: {0}".format(title))
    tmp = dest + ".tmp"
    try:
        req_headers = headers or {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        }
        r = requests.get(url, headers=req_headers, stream=True, timeout=30)
        if r.status_code >= 400:
            raise Exception(
                "El servidor respondio con codigo {0}".format(r.status_code))
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if dp.iscanceled():
                    break
                f.write(chunk)
                done += len(chunk)
                if total > 0:
                    pct = int(done * 100 / total)
                    mb_done = done / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    dp.update(pct, "{0:.1f} MB / {1:.1f} MB".format(
                        mb_done, mb_total))

        cancelled = dp.iscanceled()
        dp.close()
        if not cancelled:
            os.replace(tmp, dest)
            xbmcgui.Dialog().ok(
                "EspaTV", "Descarga completada:\n{0}".format(dest))
        elif os.path.exists(tmp):
            os.remove(tmp)
    except Exception as exc:
        try:
            dp.close()
        except Exception:
            pass
        if os.path.exists(tmp):
            os.remove(tmp)
        xbmc.log("[EspaTV/url_player] Error en descarga directa: {0}".format(
            exc), xbmc.LOGWARNING)
        xbmcgui.Dialog().ok("EspaTV", "Error en la descarga:\n{0}".format(
            str(exc)[:200]))


def _download_hls_stream(playlist_url, dest, title, headers=None):
    """Descarga un stream HLS concatenando sus segmentos .ts.

    A diferencia de default._do_download_hls, esta version acepta
    headers arbitrarios en vez de usar los de Dailymotion.
    """
    import requests
    dp = xbmcgui.DialogProgress()
    dp.create("Descargando HLS", "Obteniendo segmentos...")
    req_headers = headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    }
    clean_url = playlist_url.split("|")[0] if "|" in playlist_url else playlist_url
    tmp = dest + ".tmp"
    try:
        if "?" in clean_url:
            params = "?" + clean_url.split("?")[1]
        else:
            params = ""

        m3u8_text = requests.get(
            clean_url, headers=req_headers, timeout=15).text
        segments = [
            ln for ln in m3u8_text.splitlines()
            if ln and not ln.startswith("#")
        ]
        if not segments:
            dp.close()
            xbmcgui.Dialog().ok(
                "EspaTV", "La lista de reproduccion esta vacia.")
            return

        with open(tmp, "wb") as f_out:
            for idx, seg_name in enumerate(segments):
                if dp.iscanceled():
                    break

                if seg_name.startswith("http"):
                    seg_url = seg_name
                else:
                    import urllib.parse
                    # urljoin resuelve correctamente paths relativos o absolutos (con /)
                    seg_url = urllib.parse.urljoin(clean_url, seg_name)
                    # Añadir parametros de la playlist si el segmento no tiene propios
                    if params and "?" not in seg_url:
                        seg_url += params

                success = False
                for _attempt in range(3):
                    try:
                        seg_data = requests.get(
                            seg_url, headers=req_headers, timeout=12).content
                        f_out.write(seg_data)
                        success = True
                        break
                    except Exception:
                        continue

                if not success:
                    raise Exception(
                        "Fallo en segmento {0}/{1}".format(idx + 1, len(segments)))

                pct = int((idx + 1) * 100 / len(segments))
                dp.update(pct, "Segmento {0}/{1} - {2}".format(
                    idx + 1, len(segments), title))

        cancelled = dp.iscanceled()
        dp.close()
        if not cancelled:
            os.replace(tmp, dest)
            xbmcgui.Dialog().ok(
                "EspaTV", "Descarga HLS completada:\n{0}".format(dest))
        elif os.path.exists(tmp):
            os.remove(tmp)
    except Exception as exc:
        try:
            dp.close()
        except Exception:
            pass
        if os.path.exists(tmp):
            os.remove(tmp)
        xbmc.log("[EspaTV/url_player] Error HLS download: {0}".format(
            exc), xbmc.LOGWARNING)
        xbmcgui.Dialog().ok(
            "EspaTV", "Error en descarga HLS:\n{0}".format(str(exc)[:200]))


def _download_via_ytdlp(url, dest, title):
    """Descarga cualquier URL soportada por yt-dlp a disco.

    Genera un script auxiliar que ejecuta yt-dlp como libreria para
    evitar conflictos con el Python interno de Kodi.  Solo disponible
    en plataformas de escritorio con Python >= 3.10 en el PATH.
    """
    import default
    import subprocess

    if xbmc.getCondVisibility("System.Platform.Android"):
        xbmcgui.Dialog().ok(
            "EspaTV",
            "Las descargas con yt-dlp no estan\n"
            "disponibles en dispositivos Android.\n\n"
            "Usa un PC con Python 3.10+ instalado.")
        return

    dp = xbmcgui.DialogProgress()
    dp.create("Descarga con yt-dlp", "Preparando...")

    try:
        # Buscar Python del sistema
        dp.update(2, "Buscando Python del sistema...")
        py_cmd = default._find_system_python()
        if not py_cmd:
            dp.close()
            xbmcgui.Dialog().ok(
                "EspaTV",
                "Se necesita Python 3.10+ instalado.\n\n"
                "Descargalo de python.org e instalalo\n"
                "marcando 'Add to PATH'.\n\n"
                "Despues reinicia Kodi.")
            return

        # Preparar yt-dlp
        pkg_path = default._get_ytdlp_pkg_path()
        ytdlp_dir = os.path.join(pkg_path, "yt_dlp")
        if not os.path.isdir(ytdlp_dir):
            dp.update(5, "Descargando yt-dlp (~3 MB)...")
            if not default._ensure_ytdlp():
                dp.close()
                xbmcgui.Dialog().ok(
                    "EspaTV",
                    "Error al descargar yt-dlp.\n"
                    "Comprueba tu conexion a internet.")
                return

        # Asegurar extension .mp4
        if not dest.endswith(".mp4"):
            dest = os.path.splitext(dest)[0] + ".mp4"

        # Generar script auxiliar con URL generica
        import json as _json
        progress_file = os.path.join(pkg_path, "_dl_progress.json")
        if os.path.exists(progress_file):
            os.remove(progress_file)

        helper_path = os.path.join(pkg_path, "_dl_helper.py")
        script = (
            "import sys, os, json\n"
            "sys.path.insert(0, {pkg!r})\n"
            "import yt_dlp\n"
            "\n"
            "progress_file = {pf!r}\n"
            "\n"
            "def hook(d):\n"
            "    info = {{\"status\": d.get(\"status\", \"\"), "
            "\"percent\": 0, \"text\": \"\"}}\n"
            "    if d[\"status\"] == \"downloading\":\n"
            "        total = d.get(\"total_bytes\") or "
            "d.get(\"total_bytes_estimate\") or 0\n"
            "        done = d.get(\"downloaded_bytes\", 0)\n"
            "        speed = d.get(\"speed\") or 0\n"
            "        if total > 0:\n"
            "            info[\"percent\"] = int(done * 100 / total)\n"
            "            mb_d = done / (1024*1024)\n"
            "            mb_t = total / (1024*1024)\n"
            "            sp = speed / (1024*1024) if speed else 0\n"
            "            info[\"text\"] = "
            "f\"{{mb_d:.1f}}MB / {{mb_t:.1f}}MB ({{sp:.1f}} MB/s)\"\n"
            "        elif done > 0:\n"
            "            info[\"percent\"] = 50\n"
            "            info[\"text\"] = "
            "f\"Descargando: {{done/(1024*1024):.1f}}MB\"\n"
            "    elif d[\"status\"] == \"finished\":\n"
            "        info[\"percent\"] = 95\n"
            "        info[\"text\"] = \"Finalizando...\"\n"
            "    with open(progress_file, \"w\") as f:\n"
            "        json.dump(info, f)\n"
            "\n"
            "opts = {{\n"
            "    \"format\": \"best[ext=mp4]/best\",\n"
            "    \"outtmpl\": {dest!r},\n"
            "    \"quiet\": True,\n"
            "    \"no_warnings\": True,\n"
            "    \"progress_hooks\": [hook],\n"
            "    \"noprogress\": True,\n"
            "}}\n"
            "try:\n"
            "    ydl = yt_dlp.YoutubeDL(opts)\n"
            "    ydl.download([{url!r}])\n"
            "    with open(progress_file, \"w\") as f:\n"
            "        json.dump({{\"status\": \"done\", \"percent\": 100, "
            "\"text\": \"Completado\"}}, f)\n"
            "except Exception as e:\n"
            "    with open(progress_file, \"w\") as f:\n"
            "        json.dump({{\"status\": \"error\", \"percent\": 0, "
            "\"text\": str(e)[:300]}}, f)\n"
        ).format(pkg=pkg_path, pf=progress_file, dest=dest, url=url)

        with open(helper_path, "w", encoding="utf-8") as f:
            f.write(script)

        # Ejecutar descarga
        dp.update(10, "Iniciando descarga...")
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [py_cmd, helper_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=no_win,
        )

        # Polling
        cancelled = False
        while proc.poll() is None:
            if dp.iscanceled():
                cancelled = True
                proc.kill()
                break
            if os.path.exists(progress_file):
                try:
                    with open(progress_file, "r") as f:
                        info = _json.loads(f.read())
                    dp.update(
                        info.get("percent", 0),
                        info.get("text", "Descargando..."))
                except Exception:
                    pass
            time.sleep(0.5)

        dp.close()

        # Limpieza
        for tmp in [helper_path, progress_file]:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

        if cancelled:
            for ext in ["", ".part"]:
                p = dest + ext
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            return

        if proc.returncode == 0 and os.path.exists(dest):
            xbmcgui.Dialog().ok(
                "EspaTV", "Descarga completada:\n{0}".format(dest))
        else:
            stderr = ""
            try:
                stderr = proc.stderr.read().decode(
                    "utf-8", errors="replace")[:200]
            except Exception:
                pass
            xbmcgui.Dialog().ok(
                "EspaTV",
                "Error en la descarga:\n{0}".format(stderr or "Desconocido"))

    except Exception as exc:
        try:
            dp.close()
        except Exception:
            pass
        xbmc.log("[EspaTV/url_player] Error yt-dlp download: {0}".format(
            exc), xbmc.LOGWARNING)
        xbmcgui.Dialog().ok(
            "EspaTV", "Error al preparar la descarga:\n{0}".format(
                str(exc)[:200]))


def _resolve_and_download(raw_url):
    """Clasifica la URL y lanza la descarga adecuada."""
    url_type, value = detect_url_type(raw_url)

    if url_type == "dailymotion":
        import default
        label = _auto_label(raw_url) or value
        default._download_video(value, label)

    elif url_type in ("youtube", "web_page"):
        if not ytdlp_resolver.is_available():
            xbmcgui.Dialog().ok(
                "EspaTV",
                "La descarga de este contenido requiere yt-dlp.\n\n"
                "Solo esta disponible en PC (Windows/Linux/Mac)\n"
                "con Python 3.10+ instalado.")
            return
        label = _auto_label(raw_url) or "video"
        dest = _get_download_dest(label)
        if not dest:
            return
        _download_via_ytdlp(raw_url, dest, label)

    elif url_type == "direct_stream":
        url, headers = parse_url_with_headers(raw_url)
        label = _auto_label(raw_url) or "stream"

        clean_path = urllib.parse.urlparse(url).path.lower()
        if clean_path.endswith(".m3u8"):
            dest = _get_download_dest(label, ext=".ts")
            if not dest:
                return
            if not xbmcgui.Dialog().yesno(
                    "Descarga HLS",
                    "Este stream se descargara por segmentos.\n"
                    "El proceso puede ser lento. Continuar?"):
                return
            _download_hls_stream(url, dest, label, headers=headers or None)
        else:
            ext = os.path.splitext(clean_path)[1] or ".mp4"
            dest = _get_download_dest(label, ext=ext)
            if not dest:
                return
            h = headers if headers else None
            _download_direct_url(url, dest, label, headers=h)

    elif url_type == "rtve":
        if not ytdlp_resolver.is_available():
            xbmcgui.Dialog().ok(
                "EspaTV",
                "La descarga de RTVE requiere yt-dlp.\n\n"
                "Solo esta disponible en PC.")
            return
        label = _auto_label(raw_url) or "rtve"
        dest = _get_download_dest(label)
        if not dest:
            return
        _download_via_ytdlp(raw_url, dest, label)

    else:
        xbmcgui.Dialog().ok(
            "EspaTV",
            "Este tipo de contenido no se puede descargar.\n\n"
            "Tipos descargables: Dailymotion, YouTube,\n"
            "streams directos (.m3u8, .mp4) y paginas web.")


def url_input():
    """Teclado para pegar una URL y reproducirla."""
    kb = xbmc.Keyboard("", "Pegar URL (Dailymotion, YouTube, stream...)")
    kb.doModal()
    if not kb.isConfirmed():
        return

    raw = kb.getText().strip()
    if not raw:
        return

    valid_schemes = ("http://", "https://", "rtmp://", "rtsp://",
                     "magnet:?", "acestream://")
    if not raw.startswith(valid_schemes):
        xbmcgui.Dialog().ok(
            "EspaTV",
            "La URL no parece válida.\n\n"
            "Esquemas soportados: http, https, rtmp, rtsp, magnet, acestream",
        )
        return

    _add_to_history(raw)

    url_type, _ = detect_url_type(raw)
    type_labels = {
        "dailymotion": "Dailymotion",
        "youtube": "YouTube",
        "rtve": "Vídeo web",
        "atresplayer": "Vídeo web",
        "torrent": "Torrent",
        "acestream": "AceStream",
        "direct_stream": "Stream directo",
        "web_page": "Página web",
    }
    detected = type_labels.get(url_type, "Desconocido")
    url_display = raw[:50] + "..." if len(raw) > 50 else raw

    opts = [
        "Reproducir ahora ({0})".format(detected),
        "Guardar como marcador y reproducir",
        "Solo guardar como marcador",
    ]
    _DOWNLOADABLE = {"dailymotion", "youtube", "direct_stream", "web_page", "rtve"}
    if url_type in _DOWNLOADABLE:
        opts.append("Descargar ({0})".format(detected))

    sel = xbmcgui.Dialog().select(url_display, opts)

    if sel == 0:
        _resolve_and_play(raw)
    elif sel == 1:
        name_kb = xbmc.Keyboard("", "Nombre para el marcador")
        name_kb.doModal()
        if name_kb.isConfirmed() and name_kb.getText().strip():
            _add_bookmark(name_kb.getText().strip(), raw)
        _resolve_and_play(raw)
    elif sel == 2:
        name_kb = xbmc.Keyboard("", "Nombre para el marcador")
        name_kb.doModal()
        if name_kb.isConfirmed() and name_kb.getText().strip():
            if _add_bookmark(name_kb.getText().strip(), raw):
                xbmcgui.Dialog().notification(
                    "EspaTV", "Marcador guardado",
                    xbmcgui.NOTIFICATION_INFO,
                )
                xbmc.executebuiltin("Container.Refresh")
            else:
                xbmcgui.Dialog().notification(
                    "EspaTV", "Ya existe este marcador",
                    xbmcgui.NOTIFICATION_INFO,
                )
    elif sel == 3:
        _resolve_and_download(raw)


def url_history_menu():
    """Historial de URLs reproducidas."""
    h = int(sys.argv[1])
    history = _get_history()

    if not history:
        li = xbmcgui.ListItem(
            label="[COLOR gray]No hay URLs en el historial[/COLOR]"
        )
        li.setArt({"icon": "DefaultIconInfo.png"})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h)
        return

    li = xbmcgui.ListItem(
        label="[COLOR red][B]Borrar todo el historial de URLs[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultIconError.png"})
    xbmcplugin.addDirectoryItem(
        handle=h, url=_u(action="url_history_clear"), listitem=li, isFolder=False
    )

    for entry in history:
        raw_url = entry.get("url", "")
        label = entry.get("label", raw_url)
        date = entry.get("date", "")

        display = label[:80] + "..." if len(label) > 80 else label
        thumb = entry.get("thumb", "")
        li = xbmcgui.ListItem(label=display)
        li.setArt({
            "icon": thumb or "DefaultVideo.png",
            "thumb": thumb or "",
        })
        li.setInfo("video", {"plot": "URL: {0}\n\nFecha: {1}".format(raw_url, date)})

        cm = [
            (
                "Guardar como marcador",
                "RunPlugin({0})".format(
                    _u(action="url_bookmark_save_from_history", url=raw_url)
                ),
            ),
            (
                "Eliminar del historial",
                "RunPlugin({0})".format(
                    _u(action="url_history_remove", url=raw_url)
                ),
            ),
        ]
        li.addContextMenuItems(cm)

        play_url = _u(action="url_play", url=raw_url)
        xbmcplugin.addDirectoryItem(
            handle=h, url=play_url, listitem=li, isFolder=False
        )

    xbmcplugin.endOfDirectory(h)


def url_bookmarks_menu():
    """Marcadores de URLs guardados."""
    h = int(sys.argv[1])
    bookmarks = _get_bookmarks()

    if not bookmarks:
        li = xbmcgui.ListItem(
            label="[COLOR gray]No hay marcadores guardados[/COLOR]"
        )
        li.setArt({"icon": "DefaultIconInfo.png"})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h)
        return

    for bm in bookmarks:
        url = bm.get("url", "")
        name = bm.get("name", url)
        date = bm.get("date", "")

        li = xbmcgui.ListItem(label=name)
        thumb = bm.get("thumb", "")
        li.setArt({
            "icon": thumb or "DefaultVideo.png",
            "thumb": thumb or "",
        })
        li.setInfo("video", {"plot": "URL: {0}\n\nGuardado: {1}".format(url, date)})

        cm = [
            (
                "Renombrar",
                "RunPlugin({0})".format(_u(action="url_bookmark_rename", url=url)),
            ),
            (
                "Eliminar marcador",
                "RunPlugin({0})".format(_u(action="url_bookmark_delete", url=url)),
            ),
        ]
        li.addContextMenuItems(cm)

        play_url = _u(action="url_play", url=url)
        xbmcplugin.addDirectoryItem(
            handle=h, url=play_url, listitem=li, isFolder=False
        )

    xbmcplugin.endOfDirectory(h)


def bookmark_save_from_history(url):
    if not url:
        return
    kb = xbmc.Keyboard("", "Nombre para el marcador")
    kb.doModal()
    if kb.isConfirmed() and kb.getText().strip():
        if _add_bookmark(kb.getText().strip(), url):
            xbmcgui.Dialog().notification(
                "EspaTV", "Marcador guardado", xbmcgui.NOTIFICATION_INFO
            )
            xbmc.executebuiltin("Container.Refresh")
        else:
            xbmcgui.Dialog().notification(
                "EspaTV", "Ya existe este marcador", xbmcgui.NOTIFICATION_INFO
            )


def bookmark_rename(url):
    if not url:
        return
    bookmarks = _get_bookmarks()
    current_name = ""
    for b in bookmarks:
        if b.get("url") == url:
            current_name = b.get("name", "")
            break

    kb = xbmc.Keyboard(current_name, "Nuevo nombre")
    kb.doModal()
    if kb.isConfirmed() and kb.getText().strip():
        if _rename_bookmark(url, kb.getText().strip()):
            xbmc.executebuiltin("Container.Refresh")


def bookmark_delete(url):
    if not url:
        return
    if _remove_bookmark(url):
        xbmcgui.Dialog().notification(
            "EspaTV", "Marcador eliminado", xbmcgui.NOTIFICATION_INFO
        )
        xbmc.executebuiltin("Container.Refresh")


def history_remove(url):
    if not url:
        return
    _remove_from_history(url)
    xbmc.executebuiltin("Container.Refresh")


def history_clear():
    if xbmcgui.Dialog().yesno("EspaTV", "¿Borrar todo el historial de URLs?"):
        _clear_history()
        xbmc.executebuiltin("Container.Refresh")


def play_url_action(raw_url):
    """Reproduce una URL registrándola en el historial."""
    if not raw_url:
        return
    import threading
    threading.Thread(target=_add_to_history, args=(raw_url,), daemon=True).start()
    _resolve_and_play(raw_url)


def scan_videos_dialog():
    """Escanea una web en busca de vídeos y permite reproducirlos."""
    kb = xbmc.Keyboard("", "URL de la página a escanear")
    kb.doModal()
    if not kb.isConfirmed():
        return

    url = kb.getText().strip()
    if not url:
        return

    if not url.startswith(("http://", "https://")):
        xbmcgui.Dialog().ok(
            "EspaTV",
            "La URL debe empezar por http:// o https://",
        )
        return

    pdp = xbmcgui.DialogProgress()
    pdp.create("Escaneando vídeos", "Analizando {0}...".format(url[:60]))

    try:
        videos = _scan_with_best_engine(url)
    except Exception as exc:
        xbmc.log(
            "[EspaTV] Error escaneando {0}: {1}".format(url[:60], exc),
            xbmc.LOGWARNING,
        )
        videos = []
    finally:
        pdp.close()

    if not videos:
        xbmcgui.Dialog().ok(
            "EspaTV",
            "No se encontraron vídeos en esta página.\n\n"
            "Prueba con otra URL o pega directamente \n"
            "la URL del vídeo.",
        )
        return

    import html_scanner

    labels = []
    for v in videos:
        parts = [v.get("title") or v.get("url", "")[:60]]
        dur = v.get("duration")
        if dur:
            parts.append(html_scanner.format_duration(dur))
        size = v.get("filesize")
        if size:
            parts.append(html_scanner.format_size(size))
        ext = v.get("ext", "")
        if ext:
            parts.append(ext.upper())
        labels.append(" — ".join(parts))

    sel = xbmcgui.Dialog().select(
        "Vídeos encontrados ({0})".format(len(videos)), labels
    )
    if sel < 0:
        return

    chosen = videos[sel]
    chosen_url = chosen.get("url", "")
    if chosen_url:
        _add_to_history(chosen_url, label=chosen.get("title"))
        _resolve_and_play(chosen_url)


def _scan_with_best_engine(url):
    """Elige el motor adecuado según la plataforma y escanea."""
    # En PC, usar yt-dlp si está disponible
    if ytdlp_resolver.is_available():
        results = ytdlp_resolver.scan_videos(url)
        if results:
            return results

    # Fallback universal: escáner HTML
    import html_scanner
    return html_scanner.scan_page(url)