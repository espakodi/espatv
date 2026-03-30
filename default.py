# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
EspaTV - Buscador multimedia para Kodi.

Busca y reproduce contenido publico de Dailymotion y otras fuentes
legales. Gestiona historial, favoritos, categorias y listas IPTV.

Addon desarrollado por fullstackcurso y espakodi - RSDFA1labernt
Licencia: GPL-2.0-or-later (Si vas a usar este código, por favor, da crédito al desarrollador original e incluye esta licencia, 
ademas intenta contribuir al proyecto original si los cambios no son significativos para no fragmentar el desarrollo y la comunidad).

En el futuro habrán versiones para otros países hispanohablantes. Si quieres ayudar, ponte en contacto.

Este addon se ha hecho intentando que todo sea lo mas legal posible. Si crees que algo no lo es, ponte en contacto.
"""
import sys
import os
import shutil
import time
import urllib.parse
from urllib.parse import urljoin, quote
import xbmcgui
import xbmcplugin
import xbmc
import xbmcaddon
import xbmcvfs
import json
import difflib
import zipfile
import re
import core_settings
import category_manager
import ytdlp_resolver
import stats

try:
    import requests
except ImportError:
    xbmc.log("[EspaTV] requests module not available", xbmc.LOGERROR)
    sys.exit(1)

# Identidad del addon — no eliminar (GPL requiere mantener los avisos de autoria)
xbmc.log(
    "[EspaTV] Addon original por RubénSDFA1labernt — "
    "https://github.com/espakodi — GPL-2.0-or-later",
    xbmc.LOGINFO,
)

# Firma de build original — no eliminar
_ORIGIN = "espakodi-RSDFA1-v1"

_STATUS_URL = "https://raw.githubusercontent.com/espakodi/espatv/main/status.json"
_MESSAGES_URL = "https://raw.githubusercontent.com/espakodi/espatv/main/messages.json"

_DM_API_BASE = "https://api.dailymotion.com"
_DM_FIELDS = "id,title,thumbnail_720_url,thumbnail_360_url,thumbnail_url,duration,views_total,owner.screenname,owner.username"


def _dm_search(query, extra_params=None):
    """Busca videos en Dailymotion via su API publica."""
    params = {
        "search": query, "limit": 30, "language": "es",
        "fields": _DM_FIELDS,
    }
    if extra_params:
        params.update(extra_params)
    try:
        resp = requests.get(_DM_API_BASE + "/videos", params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("list", [])
    except Exception as exc:
        _log_error("DM search error: {0}".format(exc))
    return []


def _dm_resolve(vid):
    """Obtiene metadatos de un video de Dailymotion via API publica."""
    fields = _DM_FIELDS + ",qualities"
    try:
        resp = requests.get("{0}/video/{1}".format(_DM_API_BASE, vid),
                            params={"fields": fields}, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        _log_error("DM resolve error: {0}".format(exc))
    return None



_HEADERS_DM_DESKTOP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://www.dailymotion.com",
    "Referer": "https://www.dailymotion.com/"
}


def _fetch_hls_variants(master_url, headers=None):
    """Descarga un master playlist HLS y devuelve las variantes ordenadas por ancho de banda."""
    variants = []
    req_headers = headers if headers else {}
    try:
        r = requests.get(master_url, headers=req_headers, timeout=15)
        if r.status_code != 200:
            return variants
            
        lines = r.text.strip().splitlines()
        
        current_label = "unknown"
        current_bw = 0
        expecting_url = False
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('#EXT-X-STREAM-INF'):
                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                current_label = res_match.group(1) if res_match else "unknown"
                current_bw = int(bw_match.group(1)) if bw_match else 0
                expecting_url = True
            elif expecting_url and not line.startswith('#'):
                # Primera línea que no es tag ni comentario: es la URL de la variante
                stream_url = line
                if not stream_url.startswith('http'):
                    stream_url = urljoin(master_url, stream_url)
                variants.append({'label': current_label, 'url': stream_url, 'bandwidth': current_bw})
                expecting_url = False
                
        variants.sort(key=lambda x: x.get('bandwidth', 0), reverse=True)
    except Exception as exc:
        _log_error("HLS variants lookup error: {0}".format(exc))
    return variants


def _dm_play_direct(vid, max_quality=1080):
    """Resuelve un video de Dailymotion directamente via su API de metadatos.

    Extrae MP4 directo o selecciona la variante HLS adecuada.
    Devuelve dict con url, mime, subs, headers, o None si falla.
    """
    if not isinstance(vid, str) or not vid.strip():
        xbmc.log("[EspaTV] _dm_play_direct: ID de video invalido", xbmc.LOGWARNING)
        return None
        
    vid_safe = quote(vid.strip())
    
    try:
        meta_url = "https://www.dailymotion.com/player/metadata/video/{0}".format(vid_safe)
        r = requests.get(meta_url, headers=_HEADERS_DM_DESKTOP, timeout=15)
        
        if r.status_code != 200:
            xbmc.log("[EspaTV] _dm_play_direct error HTTP: {0}".format(r.status_code), xbmc.LOGWARNING)
            return None
            
        data = r.json()
        
        # Log GEO-Block/Errores específicos
        if data.get("error"):
            err_msg = data["error"].get("message", "Error API desconocido")
            xbmc.log("[EspaTV] Dailymotion API Error: {0}".format(err_msg), xbmc.LOGWARNING)
            return None

        qualities = data.get("qualities", {})
        subtitles = []
        subs_data = data.get("subtitles", {})
        
        # Extracción defensiva de subtítulos (la estructura varía entre vídeos)
        if isinstance(subs_data, dict):
            for lang, sub_list in subs_data.items():
                if isinstance(sub_list, list):
                    for sub in sub_list:
                        if isinstance(sub, dict) and sub.get("url"):
                            subtitles.append(sub.get("url"))

        # 1. Buscar MP4 directo (mejor compatibilidad en Android)
        best_mp4 = None
        best_res = 0
        for res_key, formats in qualities.items():
            if res_key == "auto":
                continue
            try:
                res_val = int(res_key)
            except (ValueError, TypeError):
                continue
                
            if res_val > max_quality:
                continue
                
            for fmt in formats:
                if isinstance(fmt, dict) and fmt.get("type") == "video/mp4" and res_val > best_res:
                    best_mp4 = fmt.get("url")
                    best_res = res_val
                    
        if best_mp4:
            xbmc.log("[EspaTV] _dm_play_direct: MP4 directo {0}p".format(best_res), xbmc.LOGINFO)
            return {"url": best_mp4, "mime": "video/mp4", "subs": subtitles, "headers": _HEADERS_DM_DESKTOP}

        # 2. Buscar HLS (fallback si no hay MP4 directo)
        hls_url = None
        if "auto" in qualities:
            for item in qualities["auto"]:
                if isinstance(item, dict) and item.get("type") == "application/x-mpegURL":
                    hls_url = item.get("url")
                    break
                    
        if hls_url:
            xbmc.log("[EspaTV] _dm_play_direct: parseando manifiesto HLS", xbmc.LOGINFO)
            # Pasar cabeceras al fetch del manifiesto para evitar 403 en CDN
            variants = _fetch_hls_variants(hls_url, headers=_HEADERS_DM_DESKTOP)
            
            if variants:
                filtered = []
                for v in variants:
                    label = v.get("label", "")
                    try:
                        height = int(label.split("x")[1].split(" ")[0]) if "x" in label else 0
                    except (ValueError, IndexError):
                        height = 0
                        
                    if height > 0 and height <= max_quality:
                        filtered.append(v)
                
                target = filtered[0] if filtered else variants[-1]
                xbmc.log("[EspaTV] _dm_play_direct: variante HLS seleccionada: {0}".format(target.get("label")), xbmc.LOGINFO)
                return {"url": target["url"], "mime": "application/x-mpegURL", "subs": subtitles, "headers": _HEADERS_DM_DESKTOP}

            # Sin variantes parseables, pasar la URL maestra directamente
            xbmc.log("[EspaTV] _dm_play_direct: sin variantes, usando master URL", xbmc.LOGWARNING)
            return {"url": hls_url, "mime": "application/x-mpegURL", "subs": subtitles, "headers": _HEADERS_DM_DESKTOP}

    except Exception as exc:
        xbmc.log("[EspaTV] _dm_play_direct error: {0}".format(exc), xbmc.LOGERROR)
    return None


def _dm_play(vid, dm_level=0, max_quality=1080):
    """Resuelve y reproduce un video de Dailymotion.

    Cascada: dm_gujal (extreme/basic) → fallback directo (MP4/HLS).
    """
    try:
        import dm_gujal

        if dm_level == 2:
            xbmc.log("[EspaTV] _dm_play: usando modo EXTREMO para {0}".format(vid), xbmc.LOGINFO)
            url, subs, mime = dm_gujal.play_dm_extreme(vid, max_quality=max_quality)
            if url:
                return {'url': url, 'subs': subs, 'mime': mime or 'application/x-mpegURL'}
        elif dm_level == 1:
            xbmc.log("[EspaTV] _dm_play: usando modo BASICO para {0}".format(vid), xbmc.LOGINFO)
            url, mime = dm_gujal.play_dm_basic(vid)
            if url:
                return {'url': url, 'mime': mime or 'application/x-mpegURL'}
        else:
            # Default (nivel 0): extreme primero, basic como fallback
            xbmc.log("[EspaTV] _dm_play: usando modo DEFAULT (extreme+fallback) para {0}".format(vid), xbmc.LOGINFO)
            url, subs, mime = dm_gujal.play_dm_extreme(vid, max_quality=max_quality)
            if url:
                xbmc.log("[EspaTV] _dm_play: extreme OK, mime={0}".format(mime), xbmc.LOGINFO)
                return {'url': url, 'subs': subs, 'mime': mime or 'application/x-mpegURL'}
            xbmc.log("[EspaTV] _dm_play: extreme falló, intentando basic...", xbmc.LOGINFO)
            url, mime = dm_gujal.play_dm_basic(vid)
            if url:
                xbmc.log("[EspaTV] _dm_play: basic OK, mime={0}".format(mime), xbmc.LOGINFO)
                return {'url': url, 'mime': mime or 'application/x-mpegURL'}
    except Exception as exc:
        _log_error("dm_gujal play error: {0}".format(exc))
        xbmc.log("[EspaTV] _dm_play EXCEPTION: {0}".format(exc), xbmc.LOGERROR)

    # Fallback directo: moderna extracción nativa
    xbmc.log("[EspaTV] _dm_play: intentando fallback directo para {0}".format(vid), xbmc.LOGINFO)
    result = _dm_play_direct(vid, max_quality=max_quality)
    if result:
        xbmc.log("[EspaTV] _dm_play: fallback directo OK", xbmc.LOGINFO)
        return result

    xbmc.log("[EspaTV] _dm_play: todos los métodos fallaron para {0}".format(vid), xbmc.LOGWARNING)
    return None


def _dm_search_channels(query):
    """Busca canales/usuarios en Dailymotion via API publica."""
    try:
        params = {"search": query, "limit": 20, "fields": "id,screenname,username,avatar_720_url"}
        resp = requests.get(_DM_API_BASE + "/users", params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("list", [])
    except Exception as exc:
        _log_error("DM channel search error: {0}".format(exc))
    return []


def _dm_list_user_playlists(user):
    """Lista las playlists de un usuario de Dailymotion via API publica."""
    try:
        params = {"limit": 50, "fields": "id,name,description,videos_total"}
        resp = requests.get("{0}/user/{1}/playlists".format(_DM_API_BASE, user),
                           params=params, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("list", [])
            for r in results:
                r["count"] = r.pop("videos_total", 0)
            return results
    except Exception as exc:
        _log_error("DM user playlists error: {0}".format(exc))
    return []


def _dm_view_playlist_api(pid):
    """Lista los videos de una playlist de Dailymotion via API publica."""
    try:
        params = {"limit": 50, "fields": _DM_FIELDS}
        resp = requests.get("{0}/playlist/{1}/videos".format(_DM_API_BASE, pid),
                           params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("list", [])
    except Exception as exc:
        _log_error("DM view playlist error: {0}".format(exc))
    return []


def _get_saved_playlists_file():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    return os.path.join(p, 'saved_playlists.json')


def _load_saved_playlists():
    path = _get_saved_playlists_file()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []


def _save_saved_playlists(data):
    path = _get_saved_playlists_file()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))


def _add_saved_playlist(pid, name, user, count=0):
    saved = _load_saved_playlists()
    for s in saved:
        if s.get('pid') == pid:
            return False
    saved.append({'pid': pid, 'name': name, 'user': user, 'count': count})
    _save_saved_playlists(saved)
    return True


def _remove_saved_playlist(pid):
    saved = _load_saved_playlists()
    saved = [s for s in saved if s.get('pid') != pid]
    _save_saved_playlists(saved)


def _dm_playlists_search_menu():
    """Busca canales de DM y muestra sus playlists."""
    kb = xbmc.Keyboard('', 'Buscar canal en Dailymotion')
    kb.doModal()
    if not kb.isConfirmed() or not kb.getText():
        return
    query = kb.getText()
    channels = _dm_search_channels(query)
    if not channels:
        xbmcgui.Dialog().notification("EspaTV", "No se encontraron canales", xbmcgui.NOTIFICATION_WARNING)
        return
    h = int(sys.argv[1])
    for ch in channels:
        username = ch.get('username', '')
        screenname = ch.get('screenname', username)
        avatar = ch.get('avatar_720_url', '')
        li = xbmcgui.ListItem(label=screenname)
        li.setArt({'icon': avatar, 'thumb': avatar})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="dm_user_playlists", user=username), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)


def _dm_user_playlists_menu(user):
    """Lista playlists de un usuario DM con opción de guardar."""
    playlists = _dm_list_user_playlists(user)
    if not playlists:
        xbmcgui.Dialog().notification("EspaTV", "No se encontraron playlists", xbmcgui.NOTIFICATION_WARNING)
        return
    h = int(sys.argv[1])
    saved_pids = {s['pid'] for s in _load_saved_playlists()}
    for pl in playlists:
        pid = pl.get('id', '')
        name = pl.get('name', 'Sin nombre')
        count = pl.get('count', 0)
        is_saved = pid in saved_pids
        prefix = "[COLOR green]★[/COLOR] " if is_saved else ""
        li = xbmcgui.ListItem(label="{0}{1} ({2} vídeos)".format(prefix, name, count))
        # Menú contextual: guardar o quitar
        ctx = []
        if is_saved:
            ctx.append(("Quitar de guardados", "RunPlugin({0})".format(_u(action="dm_unsave_playlist", pid=pid))))
        else:
            ctx.append(("Guardar playlist", "RunPlugin({0})".format(_u(action="dm_save_playlist", pid=pid, name=name, user=user, count=count))))
        li.addContextMenuItems(ctx)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="dm_view_playlist", pid=pid), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)


def _dm_saved_playlists_menu():
    """Muestra las playlists guardadas con opción de borrar."""
    saved = _load_saved_playlists()
    h = int(sys.argv[1])
    if not saved:
        li = xbmcgui.ListItem(label="[COLOR grey]No tienes playlists guardadas[/COLOR]")
        li.setInfo('video', {'plot':
            "Para guardar una playlist:\n"
            "1. Ve a Catálogo → Playlists de Dailymotion\n"
            "2. Busca un canal (ej: antena3, telecinco...)\n"
            "3. Entra en el canal y verás sus playlists\n"
            "4. Mantén pulsado sobre una playlist → Guardar playlist\n\n"
            "Para borrar una playlist guardada:\n"
            "Mantén pulsado sobre ella → Quitar de guardados"})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h)
        return
    for s in saved:
        pid = s.get('pid', '')
        name = s.get('name', 'Sin nombre')
        user = s.get('user', '')
        count = s.get('count', 0)
        li = xbmcgui.ListItem(label="{0} ({1} vídeos) — {2}".format(name, count, user))
        ctx = [("Quitar de guardados", "RunPlugin({0})".format(_u(action="dm_unsave_playlist", pid=pid)))]
        li.addContextMenuItems(ctx)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="dm_view_playlist", pid=pid), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)


def _dm_view_playlist_menu(pid):
    """Muestra los vídeos de una playlist de DM."""
    items = _dm_view_playlist_api(pid)
    if not items:
        xbmcgui.Dialog().notification("EspaTV", "Playlist vacía o error", xbmcgui.NOTIFICATION_WARNING)
        return
    h = int(sys.argv[1])
    for v in items:
        vid = v.get('id', '')
        title = v.get('title', 'Sin título')
        thumb = v.get('thumbnail_720_url') or v.get('thumbnail_360_url') or v.get('thumbnail_url', '')
        duration = v.get('duration', 0)
        owner = v.get('owner.screenname', '')
        li = xbmcgui.ListItem(label=title)
        li.setArt({'thumb': thumb, 'icon': thumb})
        li.setInfo('video', {'title': title, 'duration': duration, 'plot': "Canal: {0}".format(owner)})
        li.setProperty('IsPlayable', 'true')
        cm = []
        cm.append(("Descargar Video", "RunPlugin({0})".format(_u(action='download_video', vid=vid, title=title))))
        cm.append(("Abrir en navegador", "RunPlugin({0})".format(_u(action='dm_open_browser', url=vid))))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vid)))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="pv", vid=vid), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)


# --- YOUTUBE PLAYLISTS (guardadas por URL) ---

def _get_yt_playlists_file():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    return os.path.join(p, 'yt_playlists.json')


def _load_yt_playlists():
    path = _get_yt_playlists_file()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.loads(f.read())
        except Exception:
            pass
    return []


def _save_yt_playlists(data):
    path = _get_yt_playlists_file()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))


def _extract_yt_playlist_id(url):
    """Extrae el list ID de una URL de playlist de YouTube."""
    m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None


def _fetch_yt_playlist_title(list_id):
    """Intenta obtener el titulo de una playlist de YouTube via oEmbed."""
    try:
        url = "https://www.youtube.com/oembed?url=https://www.youtube.com/playlist?list={0}&format=json".format(list_id)
        r = requests.get(url, timeout=5, headers={"User-Agent": "Kodi/EspaTV"})
        if r.status_code == 200:
            data = r.json()
            title = data.get("title", "")
            if title:
                return title
    except Exception:
        pass
    # Fallback: scraping basico
    try:
        import urllib.request
        pl_url = "https://www.youtube.com/playlist?list={0}".format(list_id)
        req = urllib.request.Request(pl_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "es-ES,es;q=0.9"
        })
        html = urllib.request.urlopen(req, timeout=8).read(51200).decode("utf-8", errors="ignore")
        m = re.search(r'<title>([^<]+)</title>', html)
        if m:
            title = m.group(1).strip()
            if title.endswith(" - YouTube"):
                title = title[:-10].strip()
            if title:
                return title
    except Exception:
        pass
    return None


def _add_yt_playlist_from_url(url):
    """Valida la URL, extrae ID, busca titulo y guarda la playlist."""
    if not url:
        return False
    list_id = _extract_yt_playlist_id(url)
    if not list_id:
        xbmcgui.Dialog().ok("EspaTV", "No se pudo extraer el ID de la playlist.\n\nAsegúrate de pegar una URL válida de playlist de YouTube.\nEjemplo: https://www.youtube.com/playlist?list=PLxxxxxx")
        return False
    saved = _load_yt_playlists()
    for s in saved:
        if s.get('list_id') == list_id:
            xbmcgui.Dialog().notification("EspaTV", "Esta playlist ya está guardada", xbmcgui.NOTIFICATION_WARNING)
            return False
    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Obteniendo información de la playlist...")
    title = _fetch_yt_playlist_title(list_id)
    dp.close()
    if not title:
        kb = xbmc.Keyboard('', 'Nombre para esta playlist')
        kb.doModal()
        if not kb.isConfirmed() or not kb.getText().strip():
            title = "Playlist {0}".format(list_id[:12])
        else:
            title = kb.getText().strip()
    saved.append({
        'list_id': list_id,
        'name': title,
        'url': url.strip(),
        'ts': int(time.time()),
    })
    _save_yt_playlists(saved)
    xbmcgui.Dialog().notification("EspaTV", "Playlist guardada: {0}".format(title), xbmcgui.NOTIFICATION_INFO)
    return True


def _remove_yt_playlist(list_id):
    saved = _load_yt_playlists()
    saved = [s for s in saved if s.get('list_id') != list_id]
    _save_yt_playlists(saved)


def _yt_playlists_menu():
    """Menu principal de Playlists de YouTube."""
    h = int(sys.argv[1])

    li = xbmcgui.ListItem(label="[COLOR limegreen][B]Añadir playlist (teclado)[/B][/COLOR]")
    li.setArt({'icon': 'DefaultAddSource.png'})
    li.setInfo('video', {'plot': "Pega la URL de una playlist de YouTube usando el teclado de Kodi.\nEjemplo: https://www.youtube.com/playlist?list=PLxxxxxx"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="yt_playlist_add"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR lightskyblue][B]Añadir desde móvil/PC[/B][/COLOR]")
    li.setArt({'icon': 'DefaultNetwork.png'})
    li.setInfo('video', {'plot': "Abre un servidor temporal en tu red local.\nDesde el navegador de tu móvil o PC, pega la URL de la playlist de YouTube.\nMás cómodo para URLs largas."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="yt_playlist_add_remote"), listitem=li, isFolder=False)

    saved = _load_yt_playlists()
    if not saved:
        li = xbmcgui.ListItem(label="[COLOR grey]No tienes playlists de YouTube guardadas[/COLOR]")
        li.setInfo('video', {'plot':
            "Para guardar una playlist de YouTube:\n"
            "1. Copia la URL de la playlist en YouTube\n"
            "2. Usa 'Añadir playlist (teclado)' o 'Añadir desde móvil/PC'\n"
            "3. La playlist aparecerá aquí para acceder rápidamente\n\n"
            "Para eliminar una playlist guardada:\n"
            "Mantén pulsado sobre ella → Eliminar playlist"})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
    else:

        for s in saved:
            list_id = s.get('list_id', '')
            name = s.get('name', 'Sin nombre')
            li = xbmcgui.ListItem(label=name)
            li.setArt({'icon': 'DefaultVideoPlaylists.png'})
            li.setInfo('video', {'plot': "Playlist de YouTube\nID: {0}\n\nPulsa para abrir.".format(list_id)})
            ctx = [
                ("Eliminar playlist", "RunPlugin({0})".format(_u(action="yt_playlist_remove", list_id=list_id))),
            ]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="yt_playlist_open", list_id=list_id, name=name), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(h)


def _yt_playlist_add_action():
    """Accion: pegar URL de playlist de YouTube con el teclado."""
    kb = xbmc.Keyboard('', 'Pega la URL de la playlist de YouTube')
    kb.doModal()
    if not kb.isConfirmed():
        return
    url = kb.getText().strip()
    if not url:
        return
    if _add_yt_playlist_from_url(url):
        xbmc.executebuiltin("Container.Refresh")


def _yt_playlist_add_remote():
    """Accion: pegar URL de playlist de YouTube desde movil/PC."""
    import url_remote
    url = url_remote.receive_text("Pegar playlist de YouTube")
    if not url:
        return
    if _add_yt_playlist_from_url(url):
        xbmc.executebuiltin("Container.Refresh")


def _yt_playlist_open(list_id, name=""):
    """Abre una playlist de YouTube guardada con opciones."""
    if not list_id:
        return
    h = int(sys.argv[1])
    has_yt = _check_youtube_addon()
    is_android = xbmc.getCondVisibility("System.Platform.Android")

    if has_yt:
        li = xbmcgui.ListItem(label="[COLOR limegreen][B]Ver playlist[/B][/COLOR]")
        li.setArt({'icon': 'DefaultVideoPlaylists.png'})
        li.setInfo('video', {'plot': "Muestra todos los vídeos de la playlist.\nElige cuál reproducir."})
        xbmcplugin.addDirectoryItem(handle=h,
            url=_u(action="yt_playlist_list", list_id=list_id, name=name),
            listitem=li, isFolder=True)

        li = xbmcgui.ListItem(label="[COLOR limegreen]Reproducir todo[/COLOR]")
        li.setArt({'icon': 'DefaultVideo.png'})
        li.setInfo('video', {'plot': "Reproduce todos los vídeos de la playlist en cola.\nSin necesidad de API keys."})
        xbmcplugin.addDirectoryItem(handle=h,
            url=_u(action="yt_playlist_open_exec", list_id=list_id, name=name, method="addon"),
            listitem=li, isFolder=False)
    else:
        li = xbmcgui.ListItem(label="[COLOR red][B]Instalar addon YouTube (necesario)[/B][/COLOR]")
        li.setArt({'icon': 'DefaultAddonHelper.png'})
        li.setInfo('video', {'plot': "El addon YouTube de Kodi es necesario para abrir playlists aquí.\nPulsa para ver las instrucciones de instalación."})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="youtube_install"), listitem=li, isFolder=False)

    if is_android:
        li = xbmcgui.ListItem(label="[COLOR lightskyblue]Abrir en la app YouTube[/COLOR]")
        li.setArt({'icon': 'DefaultAddonProgram.png'})
        li.setInfo('video', {'plot': "Abre la playlist en la aplicación YouTube de Android."})
        xbmcplugin.addDirectoryItem(handle=h,
            url=_u(action="yt_playlist_open_exec", list_id=list_id, name=name, method="yt_app"),
            listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR orange]Abrir en el navegador[/COLOR]")
    li.setArt({'icon': 'DefaultNetwork.png'})
    li.setInfo('video', {'plot': "Abre la playlist en el navegador web."})
    xbmcplugin.addDirectoryItem(handle=h,
        url=_u(action="yt_playlist_open_exec", list_id=list_id, name=name, method="browser"),
        listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(h)


def _fetch_yt_playlist_videos(list_id):
    """Extrae los IDs y titulos de video de una playlist de YouTube de forma muy rápida y segura."""
    try:
        import urllib.request
        pl_url = "https://www.youtube.com/playlist?list={0}".format(list_id)
        req = urllib.request.Request(pl_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "es-ES,es;q=0.9"
        })
        # Leemos todo (suele ser ~1MB)
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
        
        results = []
        seen = set()
        
        # Buscar en ytInitialData o al menos en los videoId de playlistVideoRenderer
        chunks = html.split('"playlistVideoRenderer":')
        for chunk in chunks[1:]:
            v_idx = chunk.find('"videoId":"')
            if v_idx != -1:
                vid = chunk[v_idx+11 : v_idx+22]
                
                title = vid
                # El titulo puede estar a mas de 1000 caracteres debido a las multiples miniaturas
                t_idx = chunk.find('"title":{"runs":[{"text":"', v_idx)
                if t_idx != -1 and (t_idx - v_idx) < 3000:
                    t_start = t_idx + 26
                    t_end = chunk.find('"', t_start)
                    if t_end != -1:
                        # Limpiar titulo basico y unescaping simple
                        title = chunk[t_start:t_end].replace('\\"', '"').replace('\\/', '/').replace('\\\\', '\\')
                        
                # Alternative title format
                if title == vid:
                    t_idx2 = chunk.find('"title":{"simpleText":"', v_idx)
                    if t_idx2 != -1 and (t_idx2 - v_idx) < 3000:
                        t_start2 = t_idx2 + 23
                        t_end2 = chunk.find('"', t_start2)
                        if t_end2 != -1:
                            title = chunk[t_start2:t_end2].replace('\\"', '"').replace('\\/', '/').replace('\\\\', '\\')
                
                if vid and len(vid) == 11 and vid not in seen:
                    seen.add(vid)
                    results.append({"vid": vid, "title": title})
                    
        # Fallback de emergencia si YouTube cambia por completo la estructura
        if not results:
            # Buscar en el HTML general con regex muy basica
            pattern = re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_\-]{11})".{0,1500}?"title"\s*:\s*\{"runs"\s*:\s*\[\{"text"\s*:\s*"([^"]+)"')
            for match in pattern.finditer(html):
                v, t = match.groups()
                if v not in seen:
                    seen.add(v)
                    results.append({"vid": v, "title": t})
            
            # Si aún falla, devolver solo IDs
            if not results:
                video_ids = re.findall(r'"videoId"\s*:\s*"([A-Za-z0-9_\-]{11})"', html)
                for vid in video_ids:
                    if vid not in seen:
                        seen.add(vid)
                        results.append({"vid": vid, "title": vid})

        return results
    except Exception:
        return []


def _yt_playlist_list(list_id, name=""):
    """Muestra los videos de una playlist de YouTube como lista navegable."""
    if not list_id:
        return
    h = int(sys.argv[1])
    videos = _fetch_yt_playlist_videos(list_id)
    if not videos:
        li = xbmcgui.ListItem(label="[COLOR grey]No se pudieron obtener los vídeos[/COLOR]")
        li.setInfo('video', {'plot': "No se pudo cargar la playlist.\nPrueba a abrirla en el navegador."})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h)
        return
    for i, v in enumerate(videos):
        vid = v["vid"]
        title = v["title"]
        thumb = "https://i.ytimg.com/vi/{0}/hqdefault.jpg".format(vid)
        li = xbmcgui.ListItem(label="{0}. {1}".format(i + 1, title))
        li.setArt({"icon": "DefaultVideo.png", "thumb": thumb, "fanart": thumb})
        li.setInfo("video", {"plot": title, "title": title})
        play_url = _u(action="felicidad_play", yt_id=vid, name=title, ctype="video")
        xbmcplugin.addDirectoryItem(handle=h, url=play_url, listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)


def _yt_playlist_open_exec(list_id, name="", method="addon"):
    """Ejecuta la apertura de una playlist de YouTube segun el metodo elegido."""
    if not list_id:
        return
    web_url = "https://www.youtube.com/playlist?list={0}".format(list_id)
    if method == "addon":
        if not _check_youtube_addon():
            if not _youtube_install_prompt():
                return
        xbmcgui.Dialog().notification("EspaTV", "Cargando playlist...", xbmcgui.NOTIFICATION_INFO, 2000)
        videos = _fetch_yt_playlist_videos(list_id)
        if not videos:
            xbmcgui.Dialog().ok("EspaTV", "No se pudieron obtener los vídeos de la playlist.\n\nPrueba a abrirla en el navegador o en la app YouTube.")
            return
        playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        playlist.clear()
        for v in videos:
            url = "plugin://plugin.video.youtube/play/?video_id={0}".format(v["vid"])
            li = xbmcgui.ListItem(v["title"])
            playlist.add(url, li)
        xbmc.Player().play(playlist)
        xbmcgui.Dialog().notification("EspaTV", "{0} vídeos en cola".format(len(videos)), xbmcgui.NOTIFICATION_INFO, 2000)
    elif method == "yt_app":
        _felicidad_open_yt_app(web_url, name)
    elif method == "browser":
        _felicidad_open_browser(web_url, name)



def _l(m): xbmc.log("[EspaTV] {0}".format(m), xbmc.LOGINFO)
def _u(**k): return sys.argv[0] + "?" + urllib.parse.urlencode(k)
def _fix_img(t):
    return t

def _get_history_file():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    return os.path.join(p, 'search_history.json')


def _load_history():
    f = _get_history_file()
    if not os.path.exists(f): return []
    try:
        with open(f, 'r', encoding='utf-8') as o: return json.load(o)
    except Exception as e:
        xbmc.log("[EspaTV] Error loading history: {0}".format(e), xbmc.LOGERROR)
        return []

def _add_to_history(q):
    q = q.strip()
    if not q: return
    h = _load_history()
    if q in h: h.remove(q)
    h.insert(0, q)
    h = h[:50]
    try:
        with open(_get_history_file(), 'w', encoding='utf-8') as o: json.dump(h, o)
    except IOError: pass

def _show_history(skip_end=False):
    h = _load_history()
    addon = xbmcaddon.Addon()
    icon = addon.getAddonInfo('icon')


    li = xbmcgui.ListItem(label="[COLOR yellow][B][Categorías / Carpetas][/B][/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="cat_menu"), listitem=li, isFolder=True)
    


    
    if not h:
        li = xbmcgui.ListItem(label="[COLOR gray]No hay historial reciente[/COLOR]")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)
    else:
        li = xbmcgui.ListItem(label="[COLOR yellow][B]--- Historial DM ---[/B][/COLOR]")
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)
        for q in h:
            li = xbmcgui.ListItem(label=q)
            li.setArt({'thumb': icon, 'icon': 'DefaultFolder.png'})
            try:
                cm = [
                    ("Editar y buscar", "RunPlugin({0})".format(_u(action='edit_and_search', q=q, ot=icon))),
                    ("Añadir a Categoría...", "RunPlugin({0})".format(_u(action='cat_add_item_dialog', q=q))),
                    ("Eliminar de este historial", "RunPlugin({0}?action=remove_history_item&q={1})".format(sys.argv[0], urllib.parse.quote(q)))
                ]
                
                # Favoritos
                fav_params = json.dumps({'q': q, 'ot': icon})
                cm.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(_u(action='add_favorite', title=q, fav_url='lfr_' + q, icon=icon, platform='search', fav_action='lfr', params=fav_params))))
                
                # Buscar en torrents
                cm.append(("Buscar en webs de torrent", "RunPlugin({0})".format(_u(action='elementum_search', q=q))))
                li.addContextMenuItems(cm)
            except Exception as e:
                xbmc.log("[EspaTV] Error adding CM to history item: {0}".format(e), xbmc.LOGERROR)
            
            target_url = _u(action="lfr", q=q, ot=icon, nh=1)
            xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=target_url, listitem=li, isFolder=True)
        
        li = xbmcgui.ListItem(label="[COLOR blue][I]Nota: Puedes borrar elementos individuales con clic derecho[/I][/COLOR]")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)

        li = xbmcgui.ListItem(label="[COLOR red]Borrar Historial DM...[/COLOR]")
        li.setArt({'icon': 'DefaultIconError.png'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="clear_history"), listitem=li, isFolder=False)

    # --- Historial de YouTube ---
    yt_h = _load_yt_history()
    if yt_h:
        li = xbmcgui.ListItem(label="[COLOR red][B]--- Historial YouTube ---[/B][/COLOR]")
        li.setArt({'icon': 'DefaultMusicVideos.png'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)
        for q in yt_h:
            li = xbmcgui.ListItem(label="[COLOR red][YT][/COLOR] {0}".format(q))
            li.setArt({'icon': 'DefaultMusicVideos.png'})
            cm = [
                ("Eliminar del historial YT", "RunPlugin({0})".format(_u(action="remove_yt_history_item", q=q)))
            ]
            li.addContextMenuItems(cm)
            xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="yt_search_results", query=q), listitem=li, isFolder=True)

        li = xbmcgui.ListItem(label="[COLOR red]Borrar Historial YouTube...[/COLOR]")
        li.setArt({'icon': 'DefaultIconError.png'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="clear_yt_history"), listitem=li, isFolder=False)
        
    if not skip_end:
        xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _clear_history():
    h = _load_history()
    if not h: return
    
    opts = ["Borrar TODO el historial", "Elegir elementos a borrar", "[COLOR red]Cancelar[/COLOR]"]
    sel = xbmcgui.Dialog().select("Limpiar Historial de Búsquedas", opts)
    
    if sel == 0:
        if xbmcgui.Dialog().yesno("Confirmar", "¿Estás seguro de que quieres vaciar TODO el historial?"):
            with open(_get_history_file(), 'w', encoding='utf-8') as o: json.dump([], o)
            xbmcgui.Dialog().notification("EspaTV", "Historial vaciado", xbmcgui.NOTIFICATION_INFO)
            xbmc.executebuiltin("Container.Refresh")
    elif sel == 1:
        chosen = xbmcgui.Dialog().multiselect("Marca las búsquedas a eliminar", h)
        if chosen:
            new_h = [item for i, item in enumerate(h) if i not in chosen]
            with open(_get_history_file(), 'w', encoding='utf-8') as o: json.dump(new_h, o)
            xbmc.executebuiltin("Container.Refresh")

def _remove_history_item(q):
    h = _load_history()
    if q in h:
        h.remove(q)
        with open(_get_history_file(), 'w', encoding='utf-8') as o: json.dump(h, o)


        xbmc.executebuiltin("Container.Refresh")

def _load_spanish_channels():
    """Carga canales españoles desde canales_espana.json."""
    try:
        addon_path = xbmcaddon.Addon().getAddonInfo('path')
        path = os.path.join(addon_path, 'canales_espana.json')
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.loads(f.read())
    except Exception as e:
        xbmc.log("[EspaTV] Error cargando canales_espana.json: {0}".format(e), xbmc.LOGWARNING)
    return {}


def _spanish_section_menu(section):
    """Muestra los canales de una sección española."""
    data = _load_spanish_channels()
    sec = data.get(section)
    if not sec:
        xbmcgui.Dialog().notification("EspaTV", "Sección no encontrada", xbmcgui.NOTIFICATION_WARNING)
        return
    h = int(sys.argv[1])
    channels = sec.get('channels', [])
    for ch in channels:
        username = ch.get('username', '')
        name = ch.get('name', username)
        li = xbmcgui.ListItem(label=name)
        # Intentar obtener avatar de DM
        try:
            r = requests.get("{0}/user/{1}".format(_DM_API_BASE, username),
                           params={"fields": "avatar_720_url,screenname"}, timeout=5)
            if r.status_code == 200:
                info = r.json()
                avatar = info.get('avatar_720_url', '')
                if avatar:
                    li.setArt({'icon': avatar, 'thumb': avatar})
                real_name = info.get('screenname', name)
                li.setLabel(real_name)
            else:
                li.setArt({'icon': 'DefaultActor.png'})
                li.setLabel("{0} [COLOR grey](no disponible)[/COLOR]".format(name))
        except Exception:
            li.setArt({'icon': 'DefaultActor.png'})
        # Dos opciones: ver playlists o ver vídeos recientes
        ctx = []
        ctx.append(("Ver playlists del canal", "Container.Update({0})".format(_u(action="dm_user_playlists", user=username))))
        fav_params = json.dumps({"user": username, "name": name})
        ctx.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(_u(action='add_favorite', title=name, fav_url='spanish_channel_videos_{0}'.format(username), icon=li.getArt('icon'), platform='catalogo', fav_action='spanish_channel_videos', params=fav_params))))
        li.addContextMenuItems(ctx)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="spanish_channel_videos", user=username, name=name), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)


def _spanish_channel_videos(user, name=""):
    """Muestra los vídeos recientes de un canal DM español."""
    try:
        params = {"limit": 50, "fields": _DM_FIELDS, "sort": "recent"}
        resp = requests.get("{0}/user/{1}/videos".format(_DM_API_BASE, user),
                           params=params, timeout=15)
        if resp.status_code != 200:
            xbmcgui.Dialog().notification("EspaTV", "Canal no disponible", xbmcgui.NOTIFICATION_WARNING)
            return
        items = resp.json().get("list", [])
    except Exception as exc:
        xbmcgui.Dialog().notification("EspaTV", "Error: {0}".format(exc), xbmcgui.NOTIFICATION_ERROR)
        return
    if not items:
        xbmcgui.Dialog().notification("EspaTV", "Sin vídeos recientes", xbmcgui.NOTIFICATION_INFO)
        return
    h = int(sys.argv[1])
    # Añadir opción para ver playlists al inicio
    li_pl = xbmcgui.ListItem(label="[COLOR yellow]Ver playlists de {0}[/COLOR]".format(name or user))
    li_pl.setArt({'icon': 'DefaultVideoPlaylists.png'})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="dm_user_playlists", user=user), listitem=li_pl, isFolder=True)
    for v in items:
        vid = v.get('id', '')
        title = v.get('title', 'Sin título')
        thumb = v.get('thumbnail_720_url') or v.get('thumbnail_360_url') or v.get('thumbnail_url', '')
        duration = v.get('duration', 0)
        owner = v.get('owner.screenname', name)
        li = xbmcgui.ListItem(label=title)
        li.setArt({'thumb': thumb, 'icon': thumb})
        li.setInfo('video', {'title': title, 'duration': duration, 'plot': "Canal: {0}".format(owner)})
        li.setProperty('IsPlayable', 'true')
        cm = []
        cm.append(("Descargar Video", "RunPlugin({0})".format(_u(action='download_video', vid=vid, title=title))))
        cm.append(("Abrir en navegador", "RunPlugin({0})".format(_u(action='dm_open_browser', url=vid))))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vid)))))
        fav_params = json.dumps({"q": title, "ot": thumb})
        cm.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(_u(action='add_favorite', title=title, fav_url='lfr_{0}'.format(vid), icon=thumb, platform='dailymotion', fav_action='lfr', params=fav_params))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="pv", vid=vid), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)


def _catalog_menu():
    h = int(sys.argv[1])
    data = _load_spanish_channels()

    # Secciones españolas dinámicas desde JSON
    section_order = ['tv_nacional', 'noticias', 'deportes', 'motor', 'cine',
                     'series', 'infantil', 'musica', 'lifestyle', 'gaming',
                     'cultura', 'internacional_es']
    for key in section_order:
        sec = data.get(key)
        if sec:
            name = sec.get('name', key)
            icon = sec.get('icon', 'DefaultFolder.png')
            plot = sec.get('plot', '')
            count = len(sec.get('channels', []))
            li = xbmcgui.ListItem(label="{0} ({1})".format(name, count))
            li.setArt({'icon': icon})
            li.setInfo('video', {'plot': plot})
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="spanish_section", section=key), listitem=li, isFolder=True)
        if key == 'tv_nacional':
            fv_count = sum(len(v) for v in _FELICIDAD_VERANIEGA_YT.values())
            li = xbmcgui.ListItem(label="[COLOR khaki]Dosis de Felicidad Veraniega[/COLOR] ({0})".format(fv_count))
            li.setArt({'icon': 'DefaultMusicVideos.png'})
            li.setInfo('video', {'plot': "Contenido para el buen rollo veraniego.\nGrand Prix, Humor Amarillo, summer mixes y buenas vibraciones.\nRequiere addon YouTube."})
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="felicidad_menu"), listitem=li, isFolder=True)
            cc_count = sum(len(v) for v in _COCINA_ESPANOLA_YT.values())
            if cc_count > 0:
                li = xbmcgui.ListItem(label="[COLOR orange]Cocina Española[/COLOR] ({0})".format(cc_count))
                li.setArt({'icon': 'DefaultMusicVideos.png'})
                li.setInfo('video', {'plot': "Recetas y cocina española en YouTube.\nTortillas, paellas, tapas y mucho más."})
                xbmcplugin.addDirectoryItem(handle=h, url=_u(action="cocina_menu"), listitem=li, isFolder=True)
    # Buscar en Dailymotion
    li = xbmcgui.ListItem(label="[COLOR dodgerblue]Buscar canales y playlists[/COLOR]")
    li.setArt({'icon': 'DefaultAddonsSearch.png'})
    li.setInfo('video', {'plot': "Busca cualquier canal de Dailymotion por nombre.\nPuedes explorar sus vídeos, ver sus playlists y guardarlas en tu colección."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="dm_playlists_search"), listitem=li, isFolder=True)

    # Mis Playlists guardadas
    saved = _load_saved_playlists()
    count = len(saved)
    label_saved = "Mis Playlists guardadas ({0})".format(count) if count else "Mis Playlists guardadas"
    li = xbmcgui.ListItem(label=label_saved)
    li.setArt({'icon': 'DefaultPlaylist.png'})
    li.setInfo('video', {'plot': "Playlists de Dailymotion que has guardado.\nPuedes guardar playlists desde la búsqueda y borrarlas desde aquí."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="dm_saved_playlists"), listitem=li, isFolder=True)

    # YouTube en directo
    li = xbmcgui.ListItem(label="[B][COLOR red]YouTube[/COLOR][/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales de YouTube en directo en español.\n24h noticias, música, documentales y más."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="youtube_live_menu"), listitem=li, isFolder=True)

    # Playlists de YouTube
    yt_pl_saved = _load_yt_playlists()
    yt_pl_count = len(yt_pl_saved)
    yt_pl_label = "Playlists de YouTube ({0})".format(yt_pl_count) if yt_pl_count else "Playlists de YouTube"
    li = xbmcgui.ListItem(label="[B][COLOR red]{0}[/COLOR][/B]".format(yt_pl_label))
    li.setArt({'icon': 'DefaultVideoPlaylists.png'})
    li.setInfo('video', {'plot': "Gestiona tus playlists de YouTube.\nPega la URL de una playlist y accede a ella desde aquí.\nPuedes pegar desde el mando o desde tu móvil/PC."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="yt_playlists_menu"), listitem=li, isFolder=True)

    # Listas de Trakt.tv
    li = xbmcgui.ListItem(label="Listas de Trakt.tv")
    li.setArt({'icon': 'DefaultVideoPlaylists.png'})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="trakt_root"), listitem=li, isFolder=True)

    # Tendencias
    li = xbmcgui.ListItem(label="[COLOR gold]Tendencias[/COLOR]")
    li.setArt({'icon': 'DefaultMusicTop100.png'})
    li.setInfo('video', {'plot': "Vídeos trending en español de la última semana."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="trending"), listitem=li, isFolder=True)

    # Cámaras de España en Directo
    li = xbmcgui.ListItem(label="[COLOR deepskyblue]España en Vivo[/COLOR]")
    _cam_icon = os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources', 'media', 'webcam.png')
    li.setArt({'icon': _cam_icon})
    li.setInfo('video', {'plot': "Cámaras web en directo de playas, ciudades y montañas de España."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="webcams_menu"), listitem=li, isFolder=True)

    # Último Boletín de Noticias (Audio en segundo plano)
    li = xbmcgui.ListItem(label="[COLOR tomato]Último Boletín de Noticias[/COLOR]")
    _radio_icon = os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources', 'media', 'radio.png')
    li.setArt({'icon': _radio_icon})
    li.setInfo('video', {'plot': "Reproduce el audio del último informativo nacional de radio (Cadena SER / RNE / COPE).\nSe reproduce en segundo plano."})
    li.setProperty("IsPlayable", "true")
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="play_latest_news"), listitem=li, isFolder=False)



    # --- Buscador de torrents ---
    li = xbmcgui.ListItem(label="[COLOR yellow][B]Buscar torrents[/B][/COLOR]")
    li.setArt({'icon': 'DefaultAddonProgram.png'})
    li.setInfo('video', {'plot': "Busca películas y series en webs de torrent.\n\nEscribe el nombre de lo que buscas y se abrirán los resultados."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="elementum_search_prompt"), listitem=li, isFolder=False)

    # --- Anuncios de otros addons ---
    li = xbmcgui.ListItem(label="[COLOR orange][B]AtresDaily[/B] — Explora el catálogo de Atresplayer[/COLOR]")
    li.setArt({'icon': 'DefaultAddonVideo.png'})
    li.setInfo('video', {'plot': "AtresDaily es un addon para Kodi centrado en el catálogo de Atresplayer.\nBusca vídeos públicos disponibles en internet para cada contenido del catálogo.\n\nSi ya lo tienes instalado, se abrirá directamente."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="open_atresdaily"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR deepskyblue][B]EspaDaily[/B] — Explorador de TV Española[/COLOR]")
    li.setArt({'icon': 'DefaultAddonVideo.png'})
    li.setInfo('video', {'plot': "EspaDaily combina la navegación por catálogos de TV española con un buscador universal de vídeo.\nExplora la estructura de las principales plataformas de TV en España.\n\nSi ya lo tienes instalado, se abrirá directamente."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="open_espadaily"), listitem=li, isFolder=False)

    # Nota informativa
    li = xbmcgui.ListItem(label="[COLOR gray][I]Nota: La mayoría del contenido se obtiene automáticamente. Algunos videos se han añadido manualmente para dar coherencia a cada sección.[/I][/COLOR]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.setInfo('video', {'plot': "La mayoría del contenido mostrado se obtiene mediante búsquedas automáticas por categoría.\nAlgunos canales y videos han sido añadidos manualmente para dar sentido y coherencia a cada sección.\nSu presencia no implica afinidad, preferencia ni promoción por parte de los desarrolladores."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="info_note"), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(h)


def _my_recordings_menu():
    h = int(sys.argv[1])
    rec_path = xbmcaddon.Addon().getSetting("record_path")
    if not rec_path:
        xbmcgui.Dialog().notification("EspaTV", "Carpeta no configurada", xbmcgui.NOTIFICATION_WARNING)
        xbmcplugin.endOfDirectory(h)
        return

    # Las grabaciones PVR se guardan en la subcarpeta dedicada
    pvr_dir = os.path.join(rec_path, "Grabaciones_PVR")
    if not os.path.isdir(pvr_dir):
        li = xbmcgui.ListItem(label="No hay grabaciones guardadas actualmente")
        li.setProperty("IsPlayable", "false")
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h)
        return

    try:
        dirs, files = xbmcvfs.listdir(pvr_dir)
    except Exception:
        files = []

    xbmcplugin.setContent(h, "videos")
    has_videos = False

    # Solo archivos .ts (formato nativo del grabador PVR)
    for f in sorted(files, reverse=True):
        # Compatibilidad: xbmcvfs.listdir puede devolver bytes en algunas plataformas
        if isinstance(f, bytes):
            f = f.decode('utf-8', errors='ignore')
        if not f.lower().endswith('.ts'):
            continue
        has_videos = True
        file_path = os.path.join(pvr_dir, f)
        try:
            size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            s_str = "{0:.1f} MB".format(size / 1024.0 / 1024.0)
        except Exception:
            s_str = "Desconocido"

        li = xbmcgui.ListItem(label=f)
        li.setArt({'icon': 'DefaultVideo.png'})
        li.setInfo('video', {'plot': "Grabación PVR:\n{0}\n\nTamaño: {1}".format(f, s_str)})

        cm = [("Borrar grabación", "RunPlugin({0})".format(_u(action="delete_recording", file=file_path)))]
        li.addContextMenuItems(cm)

        xbmcplugin.addDirectoryItem(handle=h, url=file_path, listitem=li, isFolder=False)

    if not has_videos:
        li = xbmcgui.ListItem(label="No hay grabaciones guardadas actualmente")
        li.setProperty("IsPlayable", "false")
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(h)

# --- REMOTE CHECKS ---

def _check_validity():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    sf = os.path.join(p, 'status_check.json')

    now = time.time()
    st = {'last_check': 0, 'disabled': False}
    if os.path.exists(sf):
        try:
            with open(sf, 'r', encoding='utf-8') as f:
                st = json.load(f)
        except Exception: pass

    if st.get('disabled', False): return False

    if now - st.get('last_check', 0) > 604800:
        data = None
        try:
            r = requests.get(_STATUS_URL, timeout=10)
            if r.status_code == 200: data = r.json()
        except Exception: pass

        if data:
            if not data.get('allowed', True):
                st['disabled'] = True
                with open(sf, 'w', encoding='utf-8') as f: json.dump(st, f)
                return False

            min_v = data.get('min_version', '0.0.0')
            cur_v = xbmcaddon.Addon().getAddonInfo('version')
            def pv(v): return [int(x) for x in v.replace('v','').split('.') if x.isdigit()]

            if pv(cur_v) < pv(min_v):
                st['disabled'] = True
                with open(sf, 'w', encoding='utf-8') as f: json.dump(st, f)
                return False

            st['last_check'] = now
            st['disabled'] = False
            with open(sf, 'w', encoding='utf-8') as f: json.dump(st, f)

    return True

def _check_messages():
    data = None
    try:
        r = requests.get(_MESSAGES_URL, timeout=5)
        if r.status_code == 200: data = r.json()
    except Exception: pass

    if not data: return

    # Soporte para formato plano {"id":..} y formato array {"messages":[..]}
    if isinstance(data, dict) and "messages" in data:
        msgs = data["messages"]
        if not isinstance(msgs, list) or not msgs: return
        msg = msgs[0]  # Mostrar solo el ultimo/primer mensaje
    elif isinstance(data, dict) and "id" in data:
        msg = data  # Formato plano legacy
    else:
        return

    mid = msg.get("id")
    if not mid: return

    # repeat: cuantas veces mostrar. -1=siempre, 0=nunca, N=N veces. Default=1 (una vez)
    mrep = msg.get("repeat", 1)
    if mrep == 0: return

    # Cargar estado local de mensajes vistos
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    mf = os.path.join(p, 'messages_state.json')
    st = {}
    if os.path.exists(mf):
        try:
            with open(mf, 'r', encoding='utf-8') as f: st = json.load(f)
        except Exception: pass

    views = st.get(str(mid), 0)
    show_it = False
    if mrep == -1: show_it = True
    elif views < mrep: show_it = True

    if show_it:
        title = msg.get("title", msg.get("titulo", "Aviso EspaTV"))
        body = msg.get("text", msg.get("message", ""))
        xbmcgui.Dialog().ok(title, body)
        st[str(mid)] = views + 1
        with open(mf, 'w', encoding='utf-8') as f: json.dump(st, f)

def main_menu():
    try:
        _check_messages()
    except Exception:
        pass  # No bloquear el menu si falla la comprobacion de mensajes
    
    # Descarga en segundo plano de ascii.jpg para Universo EspaKodi
    def _bg_download_ascii():
        import time as _t
        _t.sleep(5)
        _profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
        if not os.path.exists(_profile): os.makedirs(_profile)
        _dst = os.path.join(_profile, 'ascii.jpg')
        if os.path.exists(_dst):
            return
        try:
            _url = 'https://raw.githubusercontent.com/fullstackcurso/Ruta-Fullstack/main/ascii.jpg'
            r = requests.get(_url, timeout=10)
            if r.status_code == 200 and len(r.content) > 1000:
                with open(_dst, 'wb') as f:
                    f.write(r.content)
                xbmc.log("[EspaTV] ascii.jpg downloaded OK", xbmc.LOGINFO)
        except Exception:
            pass
    import threading
    threading.Thread(target=_bg_download_ascii, daemon=True).start()

    # 1. Catálogo (Agrupado)
    li = xbmcgui.ListItem(label="Catálogo")
    li.setArt({'icon': 'DefaultFolder.png'})
    li.setInfo('video', {'plot': "Playlists de Dailymotion, listas de Trakt.tv, películas populares y tendencias."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="catalog_menu"), listitem=li, isFolder=True)


    # 2a. Buscar en YouTube
    li = xbmcgui.ListItem(label="[B][COLOR red]Buscar en YouTube[/COLOR][/B]")
    li.setArt({'icon': 'DefaultMusicVideos.png'})
    li.setInfo('video', {'plot': "Busca vídeos directamente en YouTube.\nLos resultados se reproducen aquí o se abren en el navegador."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="yt_search"), listitem=li, isFolder=True)

    # 2b. Buscar en Dailymotion
    li = xbmcgui.ListItem(label="[B][COLOR dodgerblue]Buscar en Dailymotion[/COLOR][/B]")
    li.setArt({'icon': 'DefaultAddonWebSkin.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="manual_search"), listitem=li, isFolder=True)

    # 3. Historial
    li = xbmcgui.ListItem(label="[COLOR aqua]Historial[/COLOR]")
    li.setArt({'icon': 'DefaultAddonRepository.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="history_menu"), listitem=li, isFolder=True)

    # 4. TV y Radio en Directo
    li = xbmcgui.ListItem(label="[COLOR limegreen][B]TV y Radio en Directo[/B][/COLOR]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "TDT Channels España, listas IPTV personalizadas y más."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="live_tv_menu"), listitem=li, isFolder=True)

    # Podcast
    li = xbmcgui.ListItem(label="[B][COLOR coral]Podcast[/COLOR][/B]")
    li.setArt({'icon': 'DefaultMusicSongs.png'})
    li.setInfo('video', {'plot': "Podcasts populares en español.\nEscucha los últimos episodios directamente."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="podcast_menu"), listitem=li, isFolder=True)

    # 4.5. Prensa (Titulares RSS)
    li = xbmcgui.ListItem(label="[B][COLOR pink]Prensa[/COLOR][/B]")
    li.setArt({'icon': 'DefaultPlaylist.png'})
    li.setInfo('video', {'plot': "Lee los titulares de última hora de los principales diarios y portales de noticias nacionales."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="press_menu"), listitem=li, isFolder=True)

    # El Tiempo
    li = xbmcgui.ListItem(label="[B][COLOR lightskyblue]El Tiempo[/COLOR][/B]")
    li.setArt({'icon': 'DefaultAddonWeather.png'})
    li.setInfo('video', {'plot': "Pronóstico meteorológico oficial de España (AEMET).\nAñade tus ubicaciones y consulta el tiempo de los próximos 7 días."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="aemet_menu"), listitem=li, isFolder=True)

    # Agenda Deportiva
    li = xbmcgui.ListItem(label="[B][COLOR orange]Agenda Deportiva TV[/COLOR][/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Programación deportiva de hoy en la TV española.\nFútbol, baloncesto, F1, tenis y más.\nDatos: Marca.com"})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="agenda_menu"), listitem=li, isFolder=True)

    # 5. Mis Favoritos
    li = xbmcgui.ListItem(label="[B][COLOR khaki]Mis Favoritos[/COLOR][/B]")
    li.setArt({'icon': 'DefaultSets.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="show_favorites"), listitem=li, isFolder=True)

    # 6. Mis Descargas
    li = xbmcgui.ListItem(label="[B][COLOR lightblue]Mis Descargas[/COLOR][/B]")
    li.setArt({'icon': 'DefaultHardDisk.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="show_downloads"), listitem=li, isFolder=True)

    # 7. Abrir URL
    li = xbmcgui.ListItem(label="[B][COLOR mediumpurple]Abrir URL[/COLOR][/B]")
    li.setArt({'icon': 'DefaultAddSource.png'})
    li.setInfo('video', {'plot': "Reproduce una URL pegada: Dailymotion, YouTube, m3u8, mp4, mpd..."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="open_url"), listitem=li, isFolder=True)

    # 10. OPCIONES
    li = xbmcgui.ListItem(label="[COLOR ivory]Opciones, mantenimiento y anti-errores[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="advanced_menu"), listitem=li, isFolder=True)

    # 8. Información
    cm_legal = []
    li = xbmcgui.ListItem(label="Información")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.addContextMenuItems(cm_legal)
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="info_menu"), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def info_menu():
    h = int(sys.argv[1])
    li = xbmcgui.ListItem(label="[COLOR skyblue]Universo EspaKodi[/COLOR]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="show_universo"), listitem=li, isFolder=False)

    if _is_debug_active():
        li = xbmcgui.ListItem(label="Hecho por RubénSDFA1labernt")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="sanitized_info"), listitem=li, isFolder=False)
    else:
        li = xbmcgui.ListItem(label="Información del Addon v{0}".format(xbmcaddon.Addon().getAddonInfo('version')))
        li.setArt({'icon': 'DefaultIconInfo.png'})
        li.addContextMenuItems([("Revocar aceptación legal", "RunPlugin({0})".format(_u(action="revoke_legal")))])
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="info"), listitem=li, isFolder=False)

    # Aviso si falta el repositorio oficial (sin él no hay actualizaciones)
    if not xbmc.getCondVisibility("System.HasAddon(repository.espatv)"):
        li = xbmcgui.ListItem(label="[B][COLOR orange][!] Activar Actualizaciones Automáticas[/COLOR][/B]")
        li.setArt({'icon': 'DefaultIconWarning.png'})
        li.setInfo('video', {'plot':
            "No tienes instalado el Repositorio Oficial de EspaTV.\n\n"
            "Sin él, Kodi NO puede actualizar el addon automáticamente.\n"
            "Si mañana se rompe un canal o se añade una función nueva, "
            "no recibirás el parche y el addon dejará de funcionar.\n\n"
            "Pulsa aquí para instalarlo en 1 clic."})
        xbmcplugin.addDirectoryItem(handle=h,
            url=_u(action="install_espatv_repo"), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(h)

# --- INSTALADOR REPOSITORIO ESPATV ---
_ESPATV_REPO_ID = "repository.espatv"
_ESPATV_REPO_API_URL = "https://api.github.com/repos/espakodi/espatv/contents/"

def _install_espatv_repo():
    """Ofrece instalar el repositorio oficial de EspaTV con 1 clic."""
    if xbmc.getCondVisibility("System.HasAddon({0})".format(_ESPATV_REPO_ID)):
        xbmcgui.Dialog().ok("EspaTV", "El repositorio ya está instalado.\nLas actualizaciones automáticas están activas.")
        return

    texto = (
        "Has instalado EspaTV manualmente desde un archivo ZIP.\n\n"
        "Sin el [B]Repositorio Oficial[/B], Kodi no puede comprobar "
        "si hay versiones nuevas del addon. Esto significa que:\n\n"
        "- No recibirás correcciones cuando un canal deje de funcionar\n"
        "- No recibirás funciones nuevas\n"
        "- Tendrías que reinstalar manualmente cada vez\n\n"
        "¿Quieres que EspaTV descargue e instale su repositorio ahora?"
    )
    if not xbmcgui.Dialog().yesno("Actualizaciones Automáticas", texto):
        return

    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Descargando repositorio oficial...")
    zip_path = None
    try:
        dp.update(5, "Buscando última versión del repositorio...")
        api_r = requests.get(_ESPATV_REPO_API_URL, timeout=15)
        if api_r.status_code != 200:
            raise Exception("No se pudo consultar GitHub (HTTP {0})".format(api_r.status_code))
        zip_url = None
        for item in api_r.json():
            name = item.get("name", "")
            if name.startswith("repository.espatv") and name.endswith(".zip"):
                zip_url = item.get("download_url")
                break
        if not zip_url:
            raise Exception("No se encontró el ZIP del repositorio en GitHub")

        dp.update(15, "Descargando desde GitHub...")
        r = requests.get(zip_url, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            raise Exception("Error HTTP {0}".format(r.status_code))
        if len(r.content) < 4 or r.content[:2] != b'PK':
            raise Exception("El archivo descargado no es un ZIP válido")
        if len(r.content) > 50 * 1024 * 1024:
            raise Exception("Tamaño sospechoso: {0}KB".format(len(r.content) // 1024))

        zip_path = os.path.join(
            xbmcvfs.translatePath("special://temp/"), "repository.espatv.zip")
        with open(zip_path, "wb") as f:
            f.write(r.content)

        dp.update(50, "Extrayendo repositorio...")
        addons_dir = xbmcvfs.translatePath("special://home/addons/")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for entry in zf.namelist():
                resolved = os.path.realpath(os.path.join(addons_dir, entry))
                if not resolved.startswith(os.path.realpath(addons_dir)):
                    raise Exception("ZIP contiene ruta sospechosa: {0}".format(entry))
            zf.extractall(addons_dir)

        dp.update(80, "Activando repositorio...")
        xbmc.executebuiltin("UpdateLocalAddons()")
        xbmc.sleep(2000)
        xbmc.executebuiltin("EnableAddon({0})".format(_ESPATV_REPO_ID))
        xbmc.sleep(1000)
        dp.close()
        xbmcgui.Dialog().ok("EspaTV",
            "El repositorio se ha instalado.\n\n"
            "Por seguridad, es recomendable ir al menú inicio de Kodi:\n"
            "[B]Add-ons → Instalar desde repositorio[/B]\n\n"
            "Una vez ahí comprueba que aparece [B]EspaTV Repository[/B] en la lista para "
            "asegurarte de que tendrás actualizaciones automáticas.")
        xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        dp.close()
        _log_error("Error instalando repo EspaTV: {0}".format(e))
        choice = xbmcgui.Dialog().yesno("Error de instalación",
            "No se pudo instalar automáticamente.\n\n"
            "Error: {0}\n\n"
            "Puede que necesites activar 'Orígenes desconocidos' "
            "en Ajustes → Sistema → Addons.\n\n"
            "¿Abrir Ajustes del Sistema?".format(e))
        if choice:
            xbmc.executebuiltin("ActivateWindow(systemsettings)")
    finally:
        if zip_path and os.path.exists(zip_path):
            try: os.remove(zip_path)
            except Exception: pass

def _history_menu():
    """Historial: muestra búsquedas directamente + enlace a visionado."""
    # Enlace a Historial de Visionado dentro del historial
    li = xbmcgui.ListItem(label="[B][COLOR mediumpurple]Historial de Visionado[/COLOR][/B]")
    li.setArt({'icon': 'DefaultRecentlyAddedEpisodes.png'})
    li.setInfo('video', {'plot': "Vídeos reproducidos recientemente desde Dailymotion."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="watch_history"), listitem=li, isFolder=True)

    # Mostrar directamente el historial de búsquedas (como hacía antes _show_history)
    _show_history(skip_end=True)
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def advanced_menu():

    # 1. Búsqueda Avanzada
    if core_settings.is_advanced_search_active():
        label = "Búsqueda DM: [COLOR green]AVANZADO (Submenú)[/COLOR]"
        plot = "Al pulsar 'Buscar manualmente', se abrirá un menú intermedio con opciones de búsqueda especializada (Cine, Exacta, etc.).\n\n[COLOR blue][I]Pulsa para volver a Búsqueda Directa[/I][/COLOR]"
    else:
        label = "Búsqueda DM: [COLOR grey]SIMPLE (Directa)[/COLOR]"
        plot = "Al pulsar 'Buscar manualmente', se abrirá directamente el teclado para escribir.\n\n[COLOR blue][I]Pulsa para activar el Menú Avanzado[/I][/COLOR]"
    
    li = xbmcgui.ListItem(label=label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_advanced_search"), listitem=li, isFolder=False)





    # 2. Debug
    if _is_debug_active():
        li = xbmcgui.ListItem(label="Modo Debug: [COLOR green]ACTIVADO[/COLOR]")
    else:
        li = xbmcgui.ListItem(label="Modo Debug: [COLOR red]DESACTIVADO[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Activa el registro detallado de errores.\n\nPuedes generar un archivo de log interno para solucionar problemas. Puede ralentizar el addon."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_debug"), listitem=li, isFolder=False)

    # 2b. Dev Mode (solo visible con Debug activo)
    if _is_debug_active():
        dev_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.dev_mode')
        is_dev = os.path.exists(dev_flag)
        if is_dev:
            dev_label = "   Modo Desarrollo: [COLOR green]ACTIVO[/COLOR]"
        else:
            dev_label = "   Modo Desarrollo: [COLOR grey]DESACTIVADO[/COLOR]"
        li = xbmcgui.ListItem(label=dev_label)
        li.setArt({'icon': 'DefaultAddonService.png'})
        li.setInfo('video', {'plot': "Protege los modulos internos del addon.\nCuando esta ACTIVO, el addon no se actualiza desde internet.\n\nDesactivar para recibir actualizaciones normales."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_dev_mode"), listitem=li, isFolder=False)

    # 2c. Log y Diagnóstico
    li = xbmcgui.ListItem(label="Log y Diagnóstico")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.setInfo('video', {'plot': "Herramientas para ver y analizar el log de Kodi y del addon.\nFiltrar errores, buscar entradas, ver tamaño, etc."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="log_menu"), listitem=li, isFolder=True)


        

    # 5. Priorizar por Ajuste (Match) vs Duración
    if core_settings.is_prioritize_match_active():
        label = "Priorizar DM: [COLOR green]AJUSTE AL CONTEXTO[/COLOR]"
        plot = "MODO PRECISIÓN: Los resultados se ordenan según cuánto se parecen las palabras clave al título buscado.\n\nIdeal si te aparecen vídeos largos que no tienen nada que ver con lo que buscas.\n\n[COLOR blue][I]Pulsa para priorizar por DURACIÓN (Estándar)[/I][/COLOR]"
    else:
        label = "Priorizar DM: [COLOR grey]DURACIÓN (ESTÁNDAR)[/COLOR]"
        plot = "MODO CLÁSICO: Se da mucha importancia a la duración del vídeo para encontrar capítulos completos.\n\nPuede causar que aparezcan vídeos largos que no coinciden bien con el nombre.\n\n[COLOR blue][I]Pulsa para priorizar por PRECISIÓN[/I][/COLOR]"
    
    li = xbmcgui.ListItem(label=label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_prioritize_match"), listitem=li, isFolder=False)

    # 6. Priorizar Contenido Reciente
    if core_settings.is_recent_active():
        label = "Filtro Temporal DM: [COLOR green]RECIENTES PRIMERO[/COLOR]"
        plot = "MODO NOTICIAS: Los vídeos se ordenan por FECHA DE SUBIDA.\n\nIdeal para ver informativos de hoy o programas diarios.\n\n[COLOR blue][I]Pulsa para desactivar[/I][/COLOR]"
    else:
        label = "Filtro Temporal DM: [COLOR grey]RELEVANCIA (POR DEFECTO)[/COLOR]"
        plot = "MODO MIXTO: El orden depende de cuánto se parezca el título, sin importar si el vídeo es de ayer o de hace meses.\n\n[COLOR blue][I]Pulsa para activar orden RECIENTE[/I][/COLOR]"
    
    li = xbmcgui.ListItem(label=label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_recent"), listitem=li, isFolder=False)

    # 7. Filtro Anti-Trailers (Minutos)
    min_m = core_settings.get_min_duration()
    if min_m > 0:
        label = f"Anti-Trailers DM: [COLOR green]SOLO +{min_m} MIN[/COLOR]"
        plot = f"MODO CAPÍTULO: Oculta automáticamente cualquier vídeo de menos de {min_m} minutos.\n\nIdeal para limpiar trailers, clips y avances de los resultados de búsqueda.\n\n[COLOR blue][I]Pulsa para cambiar o desactivar[/I][/COLOR]"
    else:
        label = "Anti-Trailers DM: [COLOR grey]DESACTIVADO[/COLOR]"
        plot = "MODO TODOS: Muestra todos los resultados encontrados, sin importar su duración.\n\n[COLOR blue][I]Pulsa para filtrar por duración mínima[/I][/COLOR]"
    
    li = xbmcgui.ListItem(label=label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="set_min_duration"), listitem=li, isFolder=False)

    # 8b. Copias de Seguridad
    li = xbmcgui.ListItem(label="Copias de Seguridad")
    li.setArt({'icon': 'DefaultAddonRepository.png'})
    li.setInfo('video', {'plot': "Crea, restaura e importa copias ZIP de todos los datos del addon.\nHistorial, favoritos, categorías, configuración y más."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="backups_menu"), listitem=li, isFolder=True)

    # 8c. Ruta de Descargas
    cfg = _load_dm_settings()
    d_path = cfg.get('download_path', '')
    dl_label = "Ruta de Descargas: [COLOR green]{0}[/COLOR]".format(d_path) if d_path else "Ruta de Descargas: [COLOR grey]NO CONFIGURADA[/COLOR]"
    li = xbmcgui.ListItem(label=dl_label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Selecciona la carpeta donde se guardarán los vídeos descargados."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="set_download_path"), listitem=li, isFolder=False)
    
    # 8d. Modo Anti-Errores Dailymotion (4 niveles)
    dm_level = cfg.get('dm_safe_level', 0)
    if dm_level == 0:
        dm_label = "Modo Anti-Errores DM: [COLOR grey]DESACTIVADO[/COLOR]"
    elif dm_level == 1:
        dm_label = "Modo Anti-Errores DM: [COLOR green]BÁSICO[/COLOR]"
    elif dm_level == 2:
        dm_label = "Modo Anti-Errores DM: [COLOR gold]EXTREMO[/COLOR]"
    else:
        dm_label = "Modo Anti-Errores DM: [COLOR cyan]YT-DLP[/COLOR]"
    dm_plot = (
        "MODO ANTI-ERRORES DAILYMOTION:\n\n"
        "[B]DESACTIVADO:[/B] Conexión normal de EspaTV.\n"
        "[B]BÁSICO:[/B] Simula teléfono móvil Android.\n"
        "[B]EXTREMO:[/B] Técnicas avanzadas (Subtítulos, Verificación de enlaces, etc).\n"
        "[B]YT-DLP:[/B] Usa yt-dlp. Recomendado en Windows donde el CDN bloquea Kodi.\n\n"
        "[I]Pulsa para cambiar el nivel.[/I]"
    )
    li = xbmcgui.ListItem(label=dm_label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': dm_plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="set_dm_safe_level"), listitem=li, isFolder=False)

    # 8e. Modo de Descarga
    dl_mode = cfg.get('dl_mode', 0)
    if dl_mode == 0:
        d_mode_txt = "[COLOR grey]DIRECTO (Normal)[/COLOR]"
    elif dl_mode == 1:
        d_mode_txt = "[COLOR green]SAFE MODE (Gujal)[/COLOR]"
    elif dl_mode == 2:
        d_mode_txt = "[COLOR gold]MODO EspaTV (HLS Process)[/COLOR]"
    elif dl_mode == 3:
        d_mode_txt = "[COLOR cyan]YT-DLP (Externo)[/COLOR]"
    else:
        d_mode_txt = "[COLOR magenta]ULTRA (Multi-hilo HLS+)[/COLOR]"
        
    dl_s_label = f"Modo de Descarga: {d_mode_txt}"
    dl_s_plot = (
        "MODO DE DESCARGA:\n\n"
        "[B]DIRECTO:[/B] Muestra menú para elegir calidad.\n"
        "[B]SAFE MODE:[/B] Descarga el mejor MP4 directo.\n"
        "[B]EspaTV:[/B] Descarga por segmentos HLS.\n"
        "[B]YT-DLP:[/B] Usa yt-dlp (mejor calidad). Requiere Python 3.10+.\n"
        "[B]ULTRA:[/B] Igual que YT-DLP (mejor calidad). Recomendado en PC."
    )
    li = xbmcgui.ListItem(label=dl_s_label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': dl_s_plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="set_dl_mode"), listitem=li, isFolder=False)

    # 8f. Mantenimiento yt-dlp (Solo PC)
    if xbmc.getCondVisibility("System.Platform.Windows | System.Platform.Linux | System.Platform.OSX"):
        li = xbmcgui.ListItem(label="Mantenimiento PC (yt-dlp)")
        li.setArt({'icon': 'DefaultAddonService.png'})
        li.setInfo('video', {'plot': "Muestra las instrucciones paso a paso para instalar yt-dlp en tu PC, o ejecuta un diagnóstico para comprobar si ya está instalado y funcionando correctamente."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="ytdlp_menu"), listitem=li, isFolder=False)

    # --- Caché IPTV ---
    iptv_cache_on = core_settings.is_iptv_cache_active()
    ttl_val = core_settings.get_iptv_cache_ttl()
    # buscar nombre legible del TTL actual
    ttl_name = "Desactivada"
    for v, n in core_settings._IPTV_CACHE_TTL_OPTIONS:
        if v == ttl_val: ttl_name = n; break

    if iptv_cache_on:
        c_label = "Caché IPTV/TDT: [COLOR green]ACTIVA ({0})[/COLOR]".format(ttl_name)
    else:
        c_label = "Caché IPTV/TDT: [COLOR grey]DESACTIVADA[/COLOR]"
    c_plot = (
        "CACHÉ IPTV / TDT:\n\n"
        "Guarda en disco los canales (M3U, TDT, etc) para que "
        "al pulsar ATRÁS se carguen al instante.\n\n"
        "[B]Estado:[/B] {0}\n"
        "[B]Duración:[/B] {1}\n\n"
        "[I]Pulsa para activar o desactivar.[/I]"
    ).format("Activa" if iptv_cache_on else "Desactivada", ttl_name)
    li = xbmcgui.ListItem(label=c_label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': c_plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_iptv_cache"), listitem=li, isFolder=False)

    if iptv_cache_on:
        ttl_label = "   Duración de caché IPTV: [COLOR green]{0}[/COLOR]".format(ttl_name)
        li = xbmcgui.ListItem(label=ttl_label)
        li.setArt({'icon': 'DefaultAddonService.png'})
        li.setInfo('video', {'plot': "Selecciona cuánto tiempo se mantienen guardados los canales.\n\n1 hora: Se actualizan rápido.\n12 horas: Equilibrio.\n1 día: Menos internet.\n1 semana: Ultra rápido.\n\n[I]Pulsa para cambiar.[/I]"})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="set_iptv_cache_ttl"), listitem=li, isFolder=False)

        li = xbmcgui.ListItem(label="   [COLOR orange]Restaurar caché por defecto[/COLOR]")
        li.setArt({'icon': 'DefaultAddonService.png'})
        li.setInfo('video', {'plot': "Desactiva la caché IPTV y elimina todos los archivos cacheados.\nEl addon volverá a descargar los datos en cada entrada, como venía de fábrica."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="reset_iptv_cache"), listitem=li, isFolder=False)

    # PVR Integracion Nativa
    import pvr_manager
    if pvr_manager.is_pvr_installed():
        li = xbmcgui.ListItem(label="Abrir Guía de Televisión Nativa (PVR)")
        li.setArt({'icon': 'DefaultAddonPVRClient.png'})
        li.setInfo('video', {'plot': "Salta instantáneamente a la sección 'TV' de Kodi para ver la parrilla de horarios."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="open_pvr"), listitem=li, isFolder=False)

        li = xbmcgui.ListItem(label="Forzar Auto-Configuración PVR")
        li.setArt({'icon': 'DefaultAddonPVRClient.png'})
        li.setInfo('video', {'plot': "Utiliza esto para forzar que EspaTV reinstale y sobreescriba tu guía si falla."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="setup_pvr"), listitem=li, isFolder=False)
    else:
        li = xbmcgui.ListItem(label="Auto-Configurar Guía de TV Nativa (PVR Grid)")
        li.setArt({'icon': 'DefaultAddonPVRClient.png'})
        li.setInfo('video', {'plot': "Esta opción vinculará la programación TDT con la sección nativa 'TV' del menú principal de Kodi.\n\nTe proporcionará una cuadrícula de horarios por horas (Grid) súper fluida. Reemplazará configuraciones PVR anteriores."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="setup_pvr"), listitem=li, isFolder=False)

    # 8. Reproductor TDT (InputStream)
    use_ia = xbmcaddon.Addon().getSetting('use_inputstream') == 'true'
    if use_ia:
        ia_label = "Motor TDT (En Directo): [COLOR green]INPUTSTREAM ADAPTIVE[/COLOR]"
        ia_plot = "REPRODUCTOR AVANZADO: Forzando Kodi a usar el motor InputStream para M3U8.\n\nMejora la reconexión de red y permite pausar el directo (TimeShift).\n\n[COLOR blue][I]Pulsa para desactivar y volver al Reproductor Nativo de Kodi.[/I][/COLOR]"
    else:
        ia_label = "Motor TDT (En Directo): [COLOR grey]NATIVO (Por defecto)[/COLOR]"
        ia_plot = "REPRODUCTOR ESTÁNDAR: Usando el motor de vídeo nativo interno de Kodi.\n\nEl directo no se puede pausar, pero es compatible con todos los sistemas operativos.\n\n[COLOR blue][I]Pulsa para activar InputStream (Requiere addon instalado).[/I][/COLOR]"
    li = xbmcgui.ListItem(label=ia_label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': ia_plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_inputstream"), listitem=li, isFolder=False)

    # 8b. Modo Anti-Errores IPTV
    anti_err = xbmcaddon.Addon().getSetting('iptv_anti_errors') == 'true'
    if anti_err:
        ae_label = "Modo Anti-Errores IPTV: [COLOR green]ACTIVADO[/COLOR]"
    else:
        ae_label = "Modo Anti-Errores IPTV: [COLOR grey]DESACTIVADO[/COLOR]"
    ae_plot = "Si un canal te da 'Playback failed', activa este modo para solucionarlo."
    li = xbmcgui.ListItem(label=ae_label)
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': ae_plot})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="toggle_iptv_anti_errors"), listitem=li, isFolder=False)

    # Ajustes PVR y Generales
    li = xbmcgui.ListItem(label="Ajustes de Grabaciones")
    li.setArt({'icon': 'DefaultAddonProgram.png'})
    li.setInfo('video', {'plot': "Configura la carpeta de destino para las grabaciones PVR de IPTV y TDT."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="open_settings"), listitem=li, isFolder=False)

    # Puerto del servidor remoto
    _cur_port = xbmcaddon.Addon().getSetting('remote_port') or '8089'
    li = xbmcgui.ListItem(label="Puerto servidor remoto: [COLOR deepskyblue]{0}[/COLOR]".format(_cur_port))
    li.setArt({'icon': 'DefaultNetwork.png'})
    li.setInfo('video', {'plot': "Puerto inicial del servidor HTTP local para enviar URLs o texto desde el móvil o PC.\nSi el puerto está ocupado, se prueban los 10 siguientes.\nPuerto actual: {0}".format(_cur_port)})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="config_remote_port"), listitem=li, isFolder=False)

    # 9. Borrar Cache
    li = xbmcgui.ListItem(label="[COLOR red]¡BORRAR TODA LA CACHÉ![/COLOR]")
    li.setArt({'icon': 'DefaultIconError.png'})
    li.setInfo('video', {'plot': "Elimina todos los datos temporales e imágenes guardadas.\nSOLUCIONA PROBLEMAS.\n\nÚtil si los menús muestran información antigua, imágenes rotas o comportamientos extraños."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="clear_cache"), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _toggle_inputstream():
    cur = xbmcaddon.Addon().getSetting('use_inputstream') == 'true'
    xbmcaddon.Addon().setSetting('use_inputstream', "false" if cur else "true")
    if not cur:
        try:
            xbmcaddon.Addon('inputstream.adaptive')
            xbmcgui.Dialog().notification("Reproductor", "InputStream Adaptive ACTIVADO", xbmcgui.NOTIFICATION_INFO)
        except Exception:
            xbmcgui.Dialog().ok("Aviso Importante", "Has activado usar InputStream Adaptive, pero parece que el componente oficial no está instalado.\n\nVe al repositorio oficial de Kodi -> Addons de Vídeo InputStream e instala InputStream Adaptive para que funcione, o vuelve a desactivar esta opción.")
    else:
        xbmcgui.Dialog().notification("Reproductor", "InputStream Adaptive DESACTIVADO", xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _toggle_iptv_anti_errors():
    cur = xbmcaddon.Addon().getSetting('iptv_anti_errors') == 'true'
    xbmcaddon.Addon().setSetting('iptv_anti_errors', "false" if cur else "true")
    if not cur:
        xbmcgui.Dialog().notification("Anti-Errores", "Modo Anti-Errores ACTIVADO", xbmcgui.NOTIFICATION_INFO)
    else:
        xbmcgui.Dialog().notification("Anti-Errores", "Modo Anti-Errores DESACTIVADO", xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _toggle_dev_mode():
    """Activa o desactiva el modo desarrollo creando/eliminando .dev_mode."""
    dev_flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.dev_mode')
    if os.path.exists(dev_flag):
        try:
            os.remove(dev_flag)
            xbmcgui.Dialog().notification("EspaTV", "Modo Desarrollo DESACTIVADO", xbmcgui.NOTIFICATION_INFO)
        except Exception as e:
            xbmcgui.Dialog().ok("Error", "No se pudo desactivar: {0}".format(e))
    else:
        try:
            with open(dev_flag, 'w') as f:
                f.write('dev')
            xbmcgui.Dialog().notification("EspaTV", "Modo Desarrollo ACTIVO", xbmcgui.NOTIFICATION_WARNING)
        except Exception as e:
            xbmcgui.Dialog().ok("Error", "No se pudo activar: {0}".format(e))
    xbmc.executebuiltin("Container.Refresh")

def _get_debug_state_file():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    return os.path.join(p, 'debug_state.json')

def _is_debug_active():
    f = _get_debug_state_file()
    if not os.path.exists(f): return False
    try:
        with open(f, 'r', encoding='utf-8') as o: return json.load(o).get('active', False)
    except Exception: return False

def _toggle_debug():
    new_state = not _is_debug_active()
    
    # Advertencia de seguridad SIEMPRE (tanto al activar como al desactivar)
    action_str = "activar" if new_state else "desactivar"
    if not xbmcgui.Dialog().yesno("ADVERTENCIA DE SEGURIDAD", 
        f"[COLOR red]ALERTA:[/COLOR] Vas a {action_str} el Modo Debug.\n\n"
        "Si no sabes lo que estás haciendo, esto podría causar errores o inestabilidad en el addon.\n"
        "¿Estás seguro de continuar?"):
        return


    log_to_file = False
    if new_state:
        if xbmcgui.Dialog().yesno("Generar Log de Errores", 
            "¿Quieres generar un archivo LOG en la raíz del addon con los errores del addon?\n\n"
            "Esto creará 'EspaTV_errors.log' en la carpeta del plugin."):
            log_to_file = True
            # Crear archivo con cabecera
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                log_file = os.path.join(base_dir, 'EspaTV_errors.log')
                ts = time.strftime('%Y-%m-%d %H:%M:%S')
                with open(log_file, 'a', encoding='utf-8') as lf:
                    lf.write(f"[{ts}] --- INICIO DE SESIÓN DE DEBUG ---\n")
            except Exception: pass
    else:
        # Desactivando: Borrar archivo de log
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            log_file = os.path.join(base_dir, 'EspaTV_errors.log')
            if os.path.exists(log_file):
                os.remove(log_file)
        except Exception: pass

    with open(_get_debug_state_file(), 'w', encoding='utf-8') as o: json.dump({'active': new_state, 'log_to_file': log_to_file}, o)
    
    msg = "ACTIVADO" if new_state else "DESACTIVADO"
    if log_to_file: msg += " (Con Log Propio)"
    xbmcgui.Dialog().notification("EspaTV", f"Modo Debug {msg}", xbmcgui.NOTIFICATION_INFO)
    # Forzamos viaje al menu principal rompiendo la cache con un timestamp
    xbmc.executebuiltin(f"Container.Update({sys.argv[0]}?reload={int(time.time())}, replace)")

def _log_error(msg):
    xbmc.log(f"[EspaTV ERROR] {msg}", xbmc.LOGERROR)
    try:
        f = _get_debug_state_file()
        if os.path.exists(f):
            with open(f, 'r', encoding='utf-8') as fh:
                st = json.load(fh)
            if st.get('active') and st.get('log_to_file'):
                base_dir = os.path.dirname(os.path.abspath(__file__))
                log_file = os.path.join(base_dir, 'EspaTV_errors.log')
                ts = time.strftime('%Y-%m-%d %H:%M:%S')
                with open(log_file, 'a', encoding='utf-8') as lf:
                    lf.write(f"[{ts}] {msg}\n")
    except Exception: pass

def _view_error_log():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = os.path.join(base_dir, 'EspaTV_errors.log')
    
    if not os.path.exists(log_file):
        xbmcgui.Dialog().ok("Error", "No existe el archivo de log.\nActiva el Modo Debug y genera errores primero.")
        return

    opts = ["Ver archivo completo", "Últimas 50 líneas", "Últimas 20 líneas", "Últimas 10 líneas"]
    sel = xbmcgui.Dialog().select("¿Qué deseas ver?", opts)
    if sel < 0: return
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        content = ""
        if not lines:
            content = "[El archivo está vacío]"
        else:
            if sel == 0: # Todo
                content = "".join(lines)
            elif sel == 1: # 50
                content = "".join(lines[-50:])
            elif sel == 2: # 20
                content = "".join(lines[-20:])
            elif sel == 3: # 10
                content = "".join(lines[-10:])
                
        xbmcgui.Dialog().textviewer("Log de Errores - EspaTV", content)
    except Exception as e:
        xbmcgui.Dialog().ok("Error Leyendo Log", str(e))



def _toggle_prioritize_match():
    new_state = not core_settings.is_prioritize_match_active()
    core_settings.set_prioritize_match(new_state)
    msg = "MODO PRECISIÓN ACTIVADO" if new_state else "MODO DURACIÓN (ESTÁNDAR)"
    xbmcgui.Dialog().notification("EspaTV", msg, xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _toggle_recent():
    new_state = not core_settings.is_recent_active()
    core_settings.set_recent_active(new_state)
    msg = "ORDEN RECIENTE ACTIVADO" if new_state else "ORDEN RELEVANCIA REINSTAURADO"
    xbmcgui.Dialog().notification("EspaTV", msg, xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _set_min_duration_filter():
    opts = ["Desactivado", "Más de 2 min", "Más de 5 min", "Más de 10 min", "Más de 20 min (Solo Full)"]
    vals = [0, 2, 5, 10, 20]
    sel = xbmcgui.Dialog().select("Ocultar vídeos más cortos que...", opts)
    if sel < 0: return
    core_settings.set_min_duration(vals[sel])
    xbmc.executebuiltin("Container.Refresh")





def _get_cache_dir():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    cd = os.path.join(p, 'api_cache')
    if not os.path.exists(cd): os.makedirs(cd)
    return cd

def _save_cache(key, data):
    try:
        f = os.path.join(_get_cache_dir(), "{0}.json".format(key))
        with open(f, 'w', encoding='utf-8') as o: json.dump(data, o)
    except Exception: pass

def _load_cache(key):
    f = os.path.join(_get_cache_dir(), "{0}.json".format(key))
    if os.path.exists(f):
        try:
            with open(f, 'r', encoding='utf-8') as o: return json.load(o)
        except Exception: pass
    return None

# --- Cache HTTP para IPTV ---
def _cached_http_get(url, timeout=15):
    """Descarga una URL con cache opcional basada en TTL.

    Si la cache IPTV esta activa y el contenido no ha expirado, lo sirve
    desde disco. En caso contrario descarga de red y almacena el resultado.
    """
    import hashlib
    ttl = core_settings.get_iptv_cache_ttl()

    cache_file = None
    if ttl > 0:
        cache_dir = core_settings.get_iptv_cache_dir()
        cache_key = hashlib.md5(url.encode('utf-8')).hexdigest()
        cache_file = os.path.join(cache_dir, "{0}.json".format(cache_key))
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if (time.time() - cached.get("ts", 0)) < ttl:
                    return cached.get("content", "")
            except Exception:
                pass

    r = requests.get(url, timeout=timeout)
    if r.status_code != 200:
        raise Exception("Error HTTP {0}".format(r.status_code))
    content = r.text

    if cache_file:
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({"ts": time.time(), "url": url, "content": content}, f, ensure_ascii=False)
        except Exception:
            pass

    return content


def _sanitized_info():
    t = (
        "[B]EspaTV[/B]\nContacto: t.me/rubensdfa1labernt\n"
        "GitHub: https://github.com/espakodi\n"
        "Telegram: https://t.me/espadaily\n"
        "Contacto: fullstackcurso.github.io/donaciones/#mensaje\n\n"
        
        "[B]AVISO LEGAL:[/B]\n\n"
        
        "[B]1. No Afiliación:[/B]\n"
        "Este proyecto es independiente. NO tiene ninguna afiliación ni vinculación con ninguna cadena de televisión ni entidad oficial.\n\n"
        
        "[B]2. Naturaleza Técnica:[/B]\n"
        "Este software actúa como un agregador de búsqueda. NO contiene, aloja ni sube contenido protegido. Muestra resultados que ya son accesibles públicamente en internet.\n\n"
        
        "[B]3. Responsabilidad del Usuario:[/B]\n"
        "El usuario es el único responsable de verificar la legalidad del acceso a los contenidos según las leyes de su país. Este addon se proporciona \"tal cual\", sin garantías de ningún tipo.\n"
        "EspaTV no se hace responsable del contenido alojado en servidores de terceros.\n\n"
        
        "[B]4. Naturaleza del proyecto:[/B]\n"
        "Es GRATUITO y sin ánimo de lucro.\n\n"
        
        "[B]5. Telemetría Anónima:[/B]\n"
        "Para mejorar el addon, se envía de forma anónima la plataforma, versión del addon y de Kodi. No se recopilan datos personales, IPs ni hábitos de uso.\n\n"
        
        "[B]Contacto y Retirada:[/B]\n"
        "Si es titular de derechos y considera que este software le perjudica, puede solicitar cambios a través de Telegram o GitHub."
    )
    xbmcgui.Dialog().textviewer("Información de EspaTV", t)

def _info():
    t = (
        "[B]EspaTV[/B]\n"
        "GitHub: https://github.com/espakodi\n"
        "Contacto: t.me/rubensdfa1labernt\n"
        "Canal de Telegram: https://t.me/espadaily\n"
        "Chat de Telegram: https://t.me/espakodi\n"
        "Contacto 2: fullstackcurso.github.io/donaciones/#mensaje\n\n"
        
        "[B]AVISO LEGAL:[/B]\n\n"
        
        "[B]1. No Afiliación:[/B]\n"
        "Este proyecto es independiente. NO tiene ninguna afiliación ni vinculación con ninguna cadena de televisión ni entidad oficial.\n\n"
        
        "[B]2. Naturaleza Técnica:[/B]\n"
        "Este software actúa como un agregador de búsqueda. NO contiene, aloja ni sube contenido protegido. Muestra resultados que ya son accesibles públicamente en internet.\n\n"
        
        "[B]3. Responsabilidad del Usuario:[/B]\n"
        "El usuario es el único responsable de verificar la legalidad del acceso a los contenidos según las leyes de su país. Este addon se proporciona \"tal cual\", sin garantías de ningún tipo.\n"
        "EspaTV no se hace responsable del contenido alojado en servidores de terceros.\n\n"
        
        "[B]4. Naturaleza del proyecto:[/B]\n"
        "Es GRATUITO y sin ánimo de lucro.\n\n"
        
        "[B]5. Telemetría Anónima:[/B]\n"
        "Para mejorar el addon, se envía de forma anónima la plataforma, versión del addon y de Kodi. No se recopilan datos personales, IPs ni hábitos de uso.\n\n"
        
        "[B]Contacto y Retirada:[/B]\n"
        "Si es titular de derechos y considera que este software le perjudica, puede solicitar cambios a través de Telegram o GitHub."
    )
    xbmcgui.Dialog().textviewer("Información de EspaTV", t)

def _revoke_legal():
    """Revoca la aceptación legal. El addon se cerrará y pedirá aceptar de nuevo."""
    ok = xbmcgui.Dialog().yesno(
        "Revocar aceptación",
        "¿Estás seguro de que deseas revocar la aceptación legal?\n\n"
        "El addon se cerrará y no podrás usarlo hasta que aceptes de nuevo."
    )
    if not ok:
        return
    _profile_dir = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    _accepted_file = os.path.join(_profile_dir, '.legal_accepted')
    try:
        if os.path.exists(_accepted_file):
            os.remove(_accepted_file)
    except Exception:
        pass
    xbmcgui.Dialog().notification("EspaTV", "Aceptación revocada", xbmcgui.NOTIFICATION_INFO, 3000)
    xbmc.executebuiltin("Action(Back)")
    xbmc.executebuiltin("Action(Back)")

def _web_info():
    t = (
        "https://github.com/espakodi\n\n"
        "------------------------------------------------\n\n"
        "[B]Visita mi GitHub para más proyectos como este.[/B]\n\n"
        "Allí encontrarás mis otros trabajos y actualizaciones.\n"
        "Cualquier apoyo o difusión de mis proyectos se agradece enormemente.\n\n"
    )
    xbmcgui.Dialog().textviewer("Más en...", t)

def _version_notes():
    t = (
        "[B]GUÍA DE USUARIO DE ESPATV[/B]\n\n"
        "------------------------------------------------\n"
        "[B]¿CÓMO FUNCIONA?[/B]\n"
        "EspaTV es un centro multimedia con TV en directo (TDT, IPTV, RTVE Play), catálogo de canales de Dailymotion, búsqueda en YouTube, radio y más. Al elegir un contenido, el addon [B]busca automáticamente[/B] enlaces públicos en internet que coincidan.\n\n"
        "------------------------------------------------\n"
        "[B]HERRAMIENTAS PRINCIPALES[/B]\n\n"
        "• [B]Mis Favoritos:[/B] Guarda programas o series para acceder rápido. Pulsa clic derecho en cualquier ítem y elige 'Añadir a Favoritos'.\n"
        "• [B]Historial:[/B] Guarda tus últimas 50 búsquedas automáticamente.\n"
        "• [B]Mis Descargas:[/B] Gestiona los vídeos que hayas guardado localmente.\n"
        "• [B]Backup:[/B] Exporta tu historial y favoritos a un archivo ZIP para llevarlos a otro Kodi.\n\n"
        "------------------------------------------------\n"
        "[B]GUÍA DE OPCIONES AVANZADAS[/B]\n\n"
        "• [B]Contexto Completo:[/B] Es la opción más importante. Define qué texto se busca en internet.\n"
        "   - [I]Desactivado:[/I] Busca solo el título del capítulo (Ej: 'Capítulo 1').\n"
        "   - [I]Nivel 1 (Recomendado):[/I] Busca Serie + Capítulo (Ej: 'Cuéntame Capítulo 1').\n"
        "   - [I]Nivel 2/MAX:[/I] Añade más datos de la ruta para búsquedas muy específicas.\n\n"
        "• [B]Priorizar Ajuste vs Duración:[/B]\n"
        "   - [I]Duración:[/I] Prefiere vídeos largos (evita clips cortos).\n"
        "   - [I]Ajuste:[/I] Prefiere títulos que coincidan exactamente, aunque sean cortos.\n\n"
        "• [B]Filtro Anti-Trailers:[/B] Oculta automáticamente vídeos de menos de X minutos.\n\n"
        "• [B]Filtro Temporal:[/B] Decide si prefieres ver primero los resultados más nuevos (Reciente) o los que mejor coinciden (Relevancia).\n\n"
        "• [B]Control de Caché:[/B]\n"
        "   - [I]Apagado:[/I] Siempre carga de internet (más lento, menos espacio).\n"
        "   - [I]Híbrido:[/I] Guarda los menús en disco para que navegar sea instantáneo.\n\n"
        "• [B]Otras Utilidades:[/B]\n"
        "   - [I]Refrescar:[/I] Recarga el addon si notas que algún menú no se actualiza.\n"
        "   - [I]Modo Debug:[/I] Actívalo solo si tienes problemas graves para generar un registro de errores.\n"
        "   - [I]Borrar Caché:[/I] Elimina datos temporales. Úsalo si tienes problemas de espacio o errores visuales.\n\n"
        "------------------------------------------------\n"
        "¡Que disfrutes de EspaTV!"
    )
    xbmcgui.Dialog().textviewer("Guía de EspaTV", t)
def _version_notes_v1():
    t = (
        "[B]NOTAS DE LA VERSIÓN 1.0[/B]\n\n"
        "Versión inicial de EspaTV.\n\n"
        "[B]Funciones incluidas:[/B]\n"
        "• TV en directo (TDT, IPTV, RTVE Play)\n"
        "• Catálogo de canales de Dailymotion\n"
        "• Búsqueda en YouTube\n"
        "• Radio y podcasts\n"
        "• Favoritos, historial y descargas\n"
        "• Búsqueda avanzada con filtros de calidad\n"
        "• Integración con Elementum para torrents\n"
    )
    xbmcgui.Dialog().textviewer("Notas v1.0", t)
def _manual_search():
    if core_settings.is_advanced_search_active():
        _advanced_search_menu()
        return

    kb = xbmc.Keyboard('', 'Introduce el nombre a buscar')
    kb.doModal()
    if kb.isConfirmed():
        q = kb.getText()
        if q:
            _add_to_history(q)
            _lfd(q, "", nh=True)

def _get_movies_list():
    return [
        "Avatar", "Vengadores: Endgame", "Avatar: El sentido del agua", "Titanic", "Star Wars: El despertar de la fuerza",
        "Vengadores: Infinity War", "Spider-Man: No Way Home", "Jurassic World", "El rey león", "Los Vengadores",
        "Fast & Furious 7", "Top Gun: Maverick", "Frozen II", "Barbie", "Vengadores: La era de Ultrón",
        "Super Mario Bros.: La película", "Black Panther", "Harry Potter y las Reliquias de la Muerte - Parte 2",
        "Star Wars: Los últimos Jedi", "Jurassic World: El reino caído", "Frozen", "La bella y la bestia",
        "Los Increíbles 2", "El destino de los furiosos", "Iron Man 3", "Minions", "Capitán América: Civil War",
        "Aquaman", "El Señor de los Anillos: El retorno del Rey", "Spider-Man: Lejos de casa", "Capitana Marvel",
        "Transformers: El lado oscuro de la luna", "Skyfall", "Transformers: La era de la extinción",
        "El caballero oscuro: La leyenda renace", "Joker", "Star Wars: El ascenso de Skywalker", "Toy Story 4",
        "Toy Story 3", "Piratas del Caribe: El cofre del hombre muerto", "El rey león (1994)", "Buscando a Dory",
        "Star Wars: La amenaza fantasma", "Alicia en el país de las maravillas", "Zootrópolis", "Harry Potter y la piedra filosofal",
        "Gru, mi villano favorito 3", "Buscando a Nemo", "Harry Potter y la Orden del Fénix", "Harry Potter y el misterio del príncipe",
        "El Señor de los Anillos: Las dos torres", "Shrek 2", "Bohemian Rhapsody", "El Señor de los Anillos: La comunidad del anillo",
        "Harry Potter y las Reliquias de la Muerte - Parte 1", "El Hobbit: Un viaje inesperado", "El caballero oscuro",
        "Jumanji: Bienvenidos a la jungla", "Harry Potter y el cáliz de fuego", "Spider-Man 3", "Transformers: La venganza de los caídos",
        "Spider-Man: Homecoming", "Ice Age 3: El origen de los dinosaurios", "Ice Age 4: La formación de los continentes",
        "El libro de la selva", "Batman v Superman: El amanecer de la justicia", "El Hobbit: La desolación de Smaug",
        "El Hobbit: La batalla de los cinco ejércitos", "Thor: Ragnarok", "Guardianes de la Galaxia Vol. 2",
        "Inside Out (Del revés)", "Venom", "Thor: Love and Thunder", "Deadpool 2", "Deadpool", "Star Wars: La venganza de los Sith",
        "Spider-Man", "Wonder Woman", "Independence Day", "Animales fantásticos y dónde encontrarlos", "Shrek Tercero",
        "Coco", "Jumanji: Siguiente nivel", "Harry Potter y la cámara secreta", "Star Wars: Una nueva esperanza",
        "ET El extraterrestre", "Misión Imposible: Fallout", "2012", "Indiana Jones y el reino de la calavera de cristal",
        "Fast & Furious 6", "Piratas del Caribe: En el fin del mundo", "El código Da Vinci", "X-Men: Días del futuro pasado",
        "Madagascar 3: De marcha por Europa", "Las crónicas de Narnia: El león, la bruja y el armario", "Man of Steel",
        "Monstruos University", "Matrix Reloaded", "Up", "Gravity", "Capitán América: El Soldado de Invierno",
        "La saga Crepúsculo: Amanecer - Parte 2", "La saga Crepúsculo: Amanecer - Parte 1", "La saga Crepúsculo: Luna nueva",
        "La saga Crepúsculo: Eclipse", "Forrest Gump", "El sexto sentido", "Interestelar", "Origen", "Gladiator",
        "Salvar al soldado Ryan", "La lista de Schindler", "El Padrino", "El Padrino Parte II", "Cadena perpetua",
        "Pulp Fiction", "El club de la lucha", "Matrix", "Goodfellas (Uno de los nuestros)", "Seven", "El silencio de los corderos",
        "La vida es bella", "Ciudad de Dios", "El viaje de Chihiro", "Parásitos", "Psicosis", "Casablanca",
        "El bueno, el feo y el malo", "12 hombres sin piedad", "La milla verde", "El gran dictador", "Cinema Paradiso",
        "Regreso al futuro", "Terminator 2: El juicio final", "Alien, el octavo pasajero", "Apocalypse Now",
        "Django desencadenado", "El resplandor", "WALL-E", "La princesa Mononoke", "Oldboy", "Amélie", "El laberinto del fauno"
    ]



def _get_top_movies_cache_path():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    return os.path.join(p, "top_movies_thumbs.json")

def _load_top_movies_cache():
    path = _get_top_movies_cache_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception: pass
    return {}

def _top_movies(page):
    try: page = int(page)
    except (ValueError, TypeError): page = 1
    per_page = 50
    movies = _get_movies_list()
    total = len(movies)
    start = (page - 1) * per_page
    end = start + per_page
    page_items = movies[start:end]

    addon = xbmcaddon.Addon()
    ai = addon.getAddonInfo('icon')
    af = addon.getAddonInfo('fanart')

    # --- SISTEMA DE CACHÉ DE IMÁGENES ---
    cache = _load_top_movies_cache()
    has_cache = len(cache) > 0

    # BOTONES DE GESTIÓN DE CARÁTULAS
    if not has_cache:
        # Solo mostramos el botón de GUARDAR permanentemente
        li = xbmcgui.ListItem(label="[COLOR yellow][B]>>> Descargar y GUARDAR Carátulas permanentemente <<<[/B][/COLOR]")
        li.setArt({'icon': 'DefaultAddonService.png'})
        li.setInfo('video', {'plot': "Esta opción buscará las fotos de TODA la lista (130+ pelis) y las guardará.\n\n[COLOR red][B]Aviso:[/B][/COLOR] Tardará un poco cada vez que entres, pero se verá de cine."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="cache_top_movies_covers"), listitem=li, isFolder=False)
    else:
        # Si ya hay cache, opción para borrarla
        li = xbmcgui.ListItem(label="[COLOR red][B]Borrar carátulas guardadas (Volver a carga rápida)[/B][/COLOR]")
        li.setArt({'icon': 'DefaultIconError.png'})
        li.setInfo('video', {'plot': "Elimina las fotos guardadas para que la lista vuelva a cargar al instante."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="clear_top_movies_cache"), listitem=li, isFolder=False)

    for m in page_items:
        li = xbmcgui.ListItem(label=m)
        
        thumb = ai
        if has_cache and m in cache:
            thumb = cache[m]

        li.setArt({'thumb': thumb, 'icon': thumb, 'poster': thumb, 'fanart': af})
        li.setInfo('video', {'title': m, 'plot': "Película legendaria: {0}".format(m)})
        
        # Context Menu
        cm = []
        cm.append(("Buscar en webs de torrent", "RunPlugin({0})".format(_u(action='elementum_search', q=m))))
        
        # Favoritos - Construir URL antes para evitar problemas con caracteres especiales
        fav_url = _u(action='add_favorite', title=m, fav_url=m, icon=thumb, platform='Legendarias', fav_action='lfr', params=json.dumps({'q': m}))
        cm.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(fav_url)))
        li.addContextMenuItems(cm)
        
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="lfr", q=m, ot=thumb, mode="movie"), listitem=li, isFolder=True)
    
    if end < total:
        li = xbmcgui.ListItem(label="Siguiente Página ({0}) >>".format(page + 1))
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="top_movies", page=page+1), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _cache_top_movies_covers():
    if not xbmcgui.Dialog().yesno("EspaTV", 
        "¿Quieres descargar las carátulas de las 130+ películas top?\n\n"
        "Esto hará que la sección se vea mucho mejor pero [COLOR yellow]tardará un poco más en cargar[/COLOR]."):
        return

    movies = _get_movies_list()
    cache = {}
    pDialog = xbmcgui.DialogProgress()
    pDialog.create("EspaTV", "Descargando carátulas Top...")
    
    total = len(movies)
    for i, m in enumerate(movies):
        if pDialog.iscanceled(): break
        
        percent = int((float(i) / total) * 100)
        pDialog.update(percent, "Buscando ({0}/{1}): {2}".format(i, total, m))
        
        try:
            cover = _dm_search("{0} movie poster".format(m))
            if cover:
                cache[m] = cover
        except Exception: pass
        
    pDialog.close()
    
    if cache:
        with open(_get_top_movies_cache_path(), 'w', encoding='utf-8') as f:
            json.dump(cache, f)
        xbmcgui.Dialog().notification("EspaTV", "Carátulas guardadas correctamente", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("Container.Refresh")

def _clear_top_movies_cache():
    path = _get_top_movies_cache_path()
    if os.path.exists(path):
        os.remove(path)
        xbmcgui.Dialog().notification("EspaTV", "Cache eliminada", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("Container.Refresh")

def _ls(cid, page=0):
    items = _dm_search(cid, page=page)
    if not items:
        if page == 0:
            xbmcgui.Dialog().ok("Error", "No se pudo obtener la lista de contenidos.")
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
        return

    _process_atres_items(items, cid, page=page)

def _process_atres_items(items, cid, page=0):
    addon = xbmcaddon.Addon(); ai = addon.getAddonInfo('icon'); af = addon.getAddonInfo('fanart')
    for i in items:
        try:
            tt = i.get("title", "Sin título"); im = i.get("image", {}); tr = im.get("pathHorizontal") or im.get("pathVertical"); th = _fix_img(tr)
            if not th:
                try: dr = _sr(tt); th = dr[0].get("thumbnail_720_url") or dr[0].get("thumbnail_360_url") if dr else ai
                except Exception: th = ai
            if not th: th = ai
            fa = th if th != ai else af
            lk = i.get("link", {}); au = lk.get("href")
            if not au: continue
            li = xbmcgui.ListItem(label=tt); li.setArt({'thumb': th, 'icon': th, 'fanart': fa})
            li.setInfo('video', {'title': tt, 'plot': i.get('description', '')})
            

            _add_fav_cm(li, tt, "atresplayer", "fs", {"au": au, "st": tt, "sth": th})
            

            xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="fs", au=au, st=tt, sth=th), listitem=li, isFolder=True)

        except Exception as e:
            _log_error(f"Error al procesar item del catálogo: {e}")
            continue

    # Paginacion: si hay items suficientes, probablemente hay mas paginas
    if len(items) >= 10:
        next_page = page + 1
        li = xbmcgui.ListItem(label="[COLOR cyan][B]>> Página Siguiente ({0})[/B][/COLOR]".format(next_page))
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="ls", cid=cid, page=next_page), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(int(sys.argv[1]), cacheToDisc=False)

def _clear_cache():
    if not xbmcgui.Dialog().yesno("EspaTV",
"¿Borrar TODA la caché?\n\nEsto eliminará datos temporales de canales y el historial de red. NO borrará tus listas ni tu configuración.\n¿Estás seguro?"):
        return

    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if os.path.exists(p):
        _protected = [
            "favorites.json",
            "custom_categories.json",
            "custom_iptv.json",
            "iptv_cache_settings.json",
            "dm_settings.json",
            "trakt_settings.json",
            "advanced_search_state.json",
            "downloads_history.json",
            "min_duration.json",
            "priority_state.json",
            "saved_playlists.json",
            "yt_playlists.json",
            "url_bookmarks.json",
        ]
        for f in os.listdir(p):
            if f.endswith(".json") and f not in _protected:
                try: os.remove(os.path.join(p, f))
                except Exception: pass
    
    # Vaciar cache de discos
    import core_settings
    core_settings.clear_iptv_cache_files()
    
    xbmcgui.Dialog().notification("EspaTV", "Caché borrada", xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _fs(au, st, sth="", pu=""):
    if pu and au == pu: _fb(st, sth); return
    d = _dm_search(au)
    if not d: _fb(st, sth); return
    ss = d.get("seasons", []); rw = d.get("rows", []); id_ = d.get("items", [])
    erh = None
    for ro in rw:
        if ro.get("type") == "EPISODE": erh = ro.get("href"); break
    if erh: _lfr(erh, st, sth); return
    if id_: _rel(id_, st, sth); return
    if len(ss) > 1:
        for s in ss:
            t = s.get("title", "Temporada"); sh = s.get("link", {}).get("href")
            if sh and sh != au:
                li = xbmcgui.ListItem(label=t); li.setArt({'thumb': sth, 'icon': sth})
                xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="fs", au=sh, st=st, sth=sth, pu=au), listitem=li, isFolder=True)
        xbmcplugin.endOfDirectory(int(sys.argv[1])); return
    elif len(ss) == 1:
        sh = ss[0].get("link", {}).get("href")
        if sh and sh != au: _fs(sh, st, sth, pu=au); return
    _fb(st, sth)

def _fb(st, sth):
    li = xbmcgui.ListItem(label=f"Buscar '{st}' en Internet..."); li.setArt({'thumb': sth, 'icon': sth})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="lfr", q=st, ot=sth), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _lfr(ru, st, sth=""):
    items = _dm_search(ru)
    if items: _rel(items, st, sth)

def _rel(items, st, sth=""):
    for i in items:
        tt = i.get("title", "Capítulo"); en = i.get("name", ""); dt = f"{en} - {tt}" if en else tt

        sq = f"{tt} {en}".strip() if en else tt
        im = i.get("image", {}); tr = im.get("pathHorizontal") or im.get("pathVertical"); th = _fix_img(tr) or sth
        li = xbmcgui.ListItem(label=dt); li.setArt({'thumb': th, 'icon': th, 'fanart': th})
        li.setInfo('video', {'title': dt, 'plot': i.get('description', '')})

        # Menú contextual: añadir a favoritos (episodio individual)
        _add_fav_cm(li, dt, "atresplayer", "lfr", {"q": sq, "ot": th})

        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="lfr", q=sq, ot=th), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _clean_title(t):
    # Si hay dos partes separadas por " : ", quedarse con la segunda (suele ser el titulo real)
    if " : " in t: parts = t.split(" : "); t = parts[1] if len(parts[1]) > len(parts[0]) else parts[0]
    return t.strip() # Devolver limpio

def _lfd(q, ot, mode="", extra_params=None, nh=None):
    cq = _clean_title(q)
    rs = []
    
    if mode == "movie":
        # Intentar varias combinaciones para encontrar contenido en español
        variations = [
            f"{cq} pelicula completa castellano",
            f"{cq} pelicula completa español",
            f"{cq} pelicula completa",
            f"{cq} completa español",
            f"{cq} castellano",
            cq # Como ultimo recurso, el titulo limpio a secas
        ]
        
        for v in variations:
            rs = _sr(v, extra_params)
            if rs: break # Si encontramos algo, nos quedamos con ello
            
    elif mode == "exact":
        # Busca exactamente lo que el usuario escribió
        rs = _sr(q, extra_params)

    elif mode == "keywords":
        # Busca solo por palabras clave de más de 3 letras
        kw = " ".join([w for w in re.findall(r'\w+', q) if len(w) > 3])
        if kw: rs = _sr(kw, extra_params)

    else:
        # Logica estandar para series/programas (o User, Long, Short)
        # Si es modo User/Long/Short, el 'q' puede ser el termino de busqueda normal, y 'extra_params' lleva el filtro.
        # Intentamos con titulo limpio primero
        rs = _sr(cq, extra_params) 
        if not rs and cq != q: rs = _sr(q, extra_params) # Intentar con el original si el limpio falla
    
    if not rs and mode not in ["exact", "keywords"]: 
        # Ultimo intento: buscar solo palabras clave (mayores de 3 letras)
        kw = " ".join([w for w in re.findall(r'\w+', q) if len(w) > 3])
        if kw: rs = _sr(kw, extra_params)
    
    # Botón de alternativas de búsqueda al inicio de resultados
    _has_alt_addons = (
        xbmc.getCondVisibility("System.HasAddon(plugin.video.elementum)") or
        xbmc.getCondVisibility("System.HasAddon(plugin.video.dailymotion_com)") or
        xbmc.getCondVisibility("System.HasAddon(plugin.video.youtube)")
    )
    if _has_alt_addons:
        _alt_label = "[COLOR yellow][B]¿No es lo que buscas?[/B][/COLOR]"
    else:
        _alt_label = "[COLOR yellow][B]Editar búsqueda y reintentar[/B][/COLOR]"
    li_alt = xbmcgui.ListItem(label=_alt_label)
    li_alt.setArt({'icon': 'DefaultAddonsSearch.png', 'thumb': 'DefaultAddonsSearch.png'})
    li_alt.setInfo('video', {'plot': "Busca en otros addons instalados o edita tu búsqueda."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="search_alt_prompt", q=cq, ot=ot), listitem=li_alt, isFolder=False)

    if not rs: 
        xbmcgui.Dialog().notification("EspaTV", "No se encontraron resultados", xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
        return
    
    sr = _gsr(cq, rs, mode)[:25] # Aumentamos un poco el limite para tener mas donde ordenar
    
    if not sr: 

        if xbmcgui.Dialog().yesno("EspaTV", "No se encontraron coincidencias relevantes.\n\n¿Quieres editar las palabras clave y probar otra vez?"):
             kb = xbmc.Keyboard(q, 'Editar Búsqueda')
             kb.doModal()
             if kb.isConfirmed():
                 nq = kb.getText()
                 if nq:
                     xbmc.executebuiltin(f"Container.Update({_u(action='lfr', q=nq, ot=ot)})")
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
        return
    

    
    min_dur = core_settings.get_min_duration() * 60
    for sc, v in sr:
        vt = v.get("title", "Video"); vi = v.get("id"); ow = v.get("owner.username", "Unknown"); du = v.get("duration", 0)
        try: du = int(du)
        except (ValueError, TypeError): du = 0
        
        # FILTRO ESTRICTO DE DURACIÓN
        if min_dur > 0 and du < min_dur: continue
        dth = v.get("thumbnail_url") or v.get("thumbnail_360_url") or ot
        
        # En modo pelicula mostramos el titulo completo del video encontrado
        if mode == "movie":
             lb = vt
        else:
             lb = f"{vt} ({int(sc*100)}% match)"

        li = xbmcgui.ListItem(label=lb); li.setArt({'thumb': dth, 'icon': dth})
        li.setInfo('video', {'title': vt, 'plot': f"Subido por: {ow}\nDuración: {du}s", 'duration': du})
        li.setProperty("IsPlayable", "true")
        
        # MENU CONTEXTUAL PARA DESCARGAR Y ABRIR EN NAVEGADOR
        cm = []
        cm.append(("Buscar en webs de torrent", f"RunPlugin({_u(action='elementum_search', q=vt)})"))
        cm.append(("Descargar Video", f"RunPlugin({_u(action='download_video', vid=vi, title=vt)})"))
        cm.append(("Abrir en navegador", f"RunPlugin({_u(action='dm_open_browser', url=vi)})"))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vi)))))
        li.addContextMenuItems(cm)

        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="pv", vid=vi), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _pv(vid):
    """Gestor principal de reproducción de vídeos Dailymotion."""
    try:
        dm_cfg = _load_dm_settings()
        dm_level = dm_cfg.get('dm_safe_level', 0)
        max_quality = dm_cfg.get('dm_max_quality', 1080)

        # Metadata opcional (no bloquea la reproducción si falla)
        d = _dm_resolve(vid)
        v_title = d.get('title', vid) if d else vid
        v_thumb = (d.get('thumbnail_url') or d.get('thumbnail_360_url', '')) if d else ''
        v_duration = d.get('duration', 0) if d else 0

        def _record_history():
            try: _add_watch_entry(vid, v_title, thumb=v_thumb, duration=v_duration)
            except Exception: pass

        _is_windows = xbmc.getCondVisibility("System.Platform.Windows")
        
        # Solo usar YT-DLP si el usuario lo seleccionó explícitamente (Nivel 3)
        use_ytdlp = (dm_level == 3)

        if not _is_windows and not use_ytdlp:
            # En Linux/Android/etc: dm_gujal funciona directamente (CDN no bloquea)
            result = _dm_play(vid, dm_level=dm_level, max_quality=max_quality)
            if result and result.get('url'):
                _record_history()
                stream_url = result['url']
                mime_type = result.get('mime', '')
                is_hls = 'mpegURL' in mime_type

                # Codificar headers para inputstream.adaptive / pipe
                headers_dict = result.get('headers')
                header_str = ""
                if headers_dict:
                    header_str = "&".join(
                        "{0}={1}".format(k, urllib.parse.quote(str(v)))
                        for k, v in headers_dict.items()
                    )

                if not is_hls and header_str:
                    stream_url = "{0}|{1}".format(stream_url, header_str)

                li = xbmcgui.ListItem(path=stream_url)
                li.setProperty('IsPlayable', 'true')
                if mime_type:
                    li.setMimeType(mime_type)
                if is_hls and header_str:
                    li.setProperty("inputstream", "inputstream.adaptive")
                    li.setProperty("inputstream.adaptive.manifest_headers", header_str)
                    li.setProperty("inputstream.adaptive.stream_headers", header_str)
                li.setContentLookup(False)
                subs = result.get('subs')
                if subs and isinstance(subs, list):
                    li.setSubtitles(subs)
                xbmcplugin.setResolvedUrl(int(sys.argv[1]), True, listitem=li)
                return

            # Fallback para no-Windows: plugin.video.dailymotion_com
            if xbmc.getCondVisibility("System.HasAddon(plugin.video.dailymotion_com)"):
                xbmc.log("[EspaTV] _pv: Intentando plugin.video.dailymotion_com...", xbmc.LOGINFO)
                dm_plugin_url = "plugin://plugin.video.dailymotion_com/?url={0}&mode=playVideo".format(vid)
                xbmc.executebuiltin('PlayMedia("{0}")'.format(dm_plugin_url))
                try:
                    xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())
                except Exception: pass
                return

            xbmcgui.Dialog().notification("EspaTV", "No se encontró stream válido", xbmcgui.NOTIFICATION_ERROR)
            try:
                xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())
            except Exception: pass

        else:
            # WINDOWS / YT-DLP: El CDN de Dailymotion bloquea m3u8 desde Windows (403)
            # Cascada: yt-dlp → navegador → addon DM → dm_gujal last resort
            _dm_ok = False
            dm_web_url = f"https://www.dailymotion.com/video/{vid}"
            
            # Método 1: yt-dlp
            xbmcgui.Dialog().notification("EspaTV", "Resolviendo con yt-dlp...", xbmcgui.NOTIFICATION_INFO, 2000)
            stream_url = ytdlp_resolver.resolve(dm_web_url)
            if stream_url:
                _record_history()
                li = xbmcgui.ListItem(path=stream_url)
                li.setProperty('IsPlayable', 'true')
                li.setContentLookup(False)
                try:
                    xbmcplugin.setResolvedUrl(int(sys.argv[1]), True, li)
                except Exception:
                    xbmc.Player().play(stream_url, li)
                _dm_ok = True

            # Método 2: Abrir Navegador
            if not _dm_ok:
                if xbmcgui.Dialog().yesno(
                    "Error Kodi/Windows",
                    "Dailymotion suele bloquear Kodi en plataformas Windows devolviendo errores 403 HTTP.\n\n"
                    "No se pudo resolver el enlace nativamente.\n¿Quieres abrir este episodio en el Navegador Web?\n\n"
                    "[I](Si eliges 'No' forzaremos los últimos fallbacks internos)[/I]",
                    yeslabel="Navegador Web", nolabel="Forzar Kodi"
                ):
                    try:
                        import webbrowser
                        webbrowser.open(dm_web_url)
                        xbmcgui.Dialog().notification("EspaTV", "Enlace enviado al navegador", xbmcgui.NOTIFICATION_INFO)
                    except Exception:
                        xbmcgui.Dialog().ok("EspaTV", f"Abre la siguiente URL en tu navegador de internet:\n\n{dm_web_url}")
                    _record_history() # Se cuenta como visto porque lo abrieron externamente
                    _dm_ok = True
            
            # Método 3: Intento directo via dm_gujal como último recurso
            if not _dm_ok:
                try:
                    result = _dm_play(vid, dm_level=dm_level, max_quality=max_quality)
                    if result and result.get('url'):
                        _record_history()
                        stream_url = result['url']
                        mime_type = result.get('mime', '')
                        is_hls = 'mpegURL' in mime_type

                        headers_dict = result.get('headers')
                        header_str = ""
                        if headers_dict:
                            header_str = "&".join(
                                "{0}={1}".format(k, urllib.parse.quote(str(v)))
                                for k, v in headers_dict.items()
                            )

                        if not is_hls and header_str:
                            stream_url = "{0}|{1}".format(stream_url, header_str)

                        li = xbmcgui.ListItem(path=stream_url)
                        if mime_type: li.setMimeType(mime_type)
                        if is_hls and header_str:
                            li.setProperty("inputstream", "inputstream.adaptive")
                            li.setProperty("inputstream.adaptive.manifest_headers", header_str)
                            li.setProperty("inputstream.adaptive.stream_headers", header_str)
                        li.setContentLookup(False)
                        subs = result.get('subs')
                        if subs and isinstance(subs, list): li.setSubtitles(subs)
                        xbmcplugin.setResolvedUrl(int(sys.argv[1]), True, listitem=li)
                        
                        import time as _t; _t.sleep(2)  # Dar tiempo a ver si falla el reproductor
                        if xbmc.Player().isPlaying(): _dm_ok = True
                except Exception:
                    pass

            # Método 4: Addon_DM_Gujal
            if not _dm_ok and xbmc.getCondVisibility("System.HasAddon(plugin.video.dailymotion_com)"):
                try:
                    dm_plugin_url = "plugin://plugin.video.dailymotion_com/?url={0}&mode=playVideo".format(vid)
                    xbmc.executebuiltin('PlayMedia("{0}")'.format(dm_plugin_url))
                    import time as _t; _t.sleep(3)
                    if xbmc.Player().isPlaying():
                        _record_history()
                        _dm_ok = True
                except Exception:
                    pass
            
            # Limpiar resolución si todo falla, para no dejar Kodi pensando infinito
            if not _dm_ok:
                try:
                    xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())
                except Exception:
                    pass

    except Exception as e:
        _l(f"Play Error: {e}")
        xbmcgui.Dialog().ok("Error", str(e))

def _download_video(vid, title):
    if not vid: return
    
    try:
        # El player/metadata devuelve qualities; la API publica ya no lo hace
        meta_url = "https://www.dailymotion.com/player/metadata/video/{0}".format(vid)
        r = requests.get(meta_url, headers=_HEADERS_DM_DESKTOP, timeout=12)
        if r.status_code != 200:
            xbmcgui.Dialog().ok("EspaTV", "No se pudo resolver el video para descarga.\nCodigo: {0}".format(r.status_code))
            return
        d = r.json()
        if not d:
            xbmcgui.Dialog().ok("EspaTV", "No se pudo resolver el video para descarga.")
            return
        qs = d.get("qualities") or {}
        
        all_links = []
        
        # 1. Buscar enlaces MP4 directos
        for res_key, formats in qs.items():
            if res_key == "auto": continue
            for f in formats:
                u = f.get("url")
                if not u: continue
                if f.get("type") == "video/mp4":
                    res_val = int(res_key) if res_key.isdigit() else 0
                    all_links.append({'label': f"{res_key}p (Descarga Directa MP4)", 'url': u, 'res': res_val, 'type': 'mp4'})
        
        # 2. Buscar HLS (M3U8) como fallback
        m3u8_master = ""
        if "auto" in qs:
            for s in qs["auto"]:
                if s.get("type") == "application/x-mpegURL":
                    m3u8_master = s.get("url", "")
                    vs = _fetch_hls_variants(m3u8_master, headers=_HEADERS_DM_DESKTOP)
                    for v in vs:
                        rs_str = v['label'].split(' ')[0]
                        res_val = int(rs_str.split('x')[1]) if 'x' in rs_str else 0
                        all_links.append({'label': f"{rs_str} (HLS por Segmentos .TS)", 'url': v['url'], 'res': res_val, 'type': 'hls'})
                    if m3u8_master:
                        all_links.append({'label': "ULTRA (Mejor calidad via yt-dlp)", 'url': m3u8_master, 'res': 99999, 'type': 'ultra'})

        # Ordenar por resolucion
        all_links.sort(key=lambda x: x['res'], reverse=True)

        if not all_links:
            xbmcgui.Dialog().ok("EspaTV", "No se han encontrado enlaces de descarga para este contenido.\n\nEl servidor puede estar bloqueando el acceso directo a este video.")
            return

        # Leer modo de descarga configurado
        dm_cfg = _load_dm_settings()
        dl_mode = dm_cfg.get('dl_mode', 0)

        # Modo 0 (DIRECTO): Mostrar menú de selección (comportamiento actual)
        if dl_mode == 0:
            lbs = [l['label'] for l in all_links]
            sel = xbmcgui.Dialog().select("Selecciona Calidad de Descarga", lbs)
            if sel < 0: return
            target = all_links[sel]
        # Modo 1 (SAFE MODE): Mejor MP4 directo disponible
        elif dl_mode == 1:
            mp4s = [l for l in all_links if l['type'] == 'mp4']
            if mp4s:
                target = mp4s[0]
            else:
                xbmcgui.Dialog().ok("EspaTV", "No hay enlaces MP4 directos.\nCambia a modo ULTRA en ajustes.")
                return
        # Modo 2 (EspaTV): Mejor HLS disponible
        elif dl_mode == 2:
            hls_links = [l for l in all_links if l['type'] == 'hls']
            if hls_links:
                target = hls_links[0]
            else:
                xbmcgui.Dialog().ok("EspaTV", "No hay enlaces HLS disponibles.\nCambia a modo ULTRA en ajustes.")
                return
        # Modo 3 (YT-DLP) y 4 (ULTRA): Usar yt-dlp
        else:
            ultra = [l for l in all_links if l['type'] == 'ultra']
            if ultra:
                target = ultra[0]
            else:
                mp4s = [l for l in all_links if l['type'] == 'mp4']
                target = mp4s[0] if mp4s else all_links[0]
        
        # Pedir directorio de destino
        path = xbmcgui.Dialog().browse(3, 'Selecciona dónde guardar el video', 'video', '', False, False, '')
        if not path: return
        
        # Confirmar HLS si aplica
        if target['type'] == 'hls':
            if not xbmcgui.Dialog().yesno("Descarga HLS", "Este video no tiene enlace directo.\nSe bajará por segmentos y se unirán al final.\n\nEs un proceso lento. ¿Continuar?"):
                return

        # Preparar nombres de archivo
        safe_title = "".join([c for c in title if c.isalnum() or c in ' -_']).strip()
        if not safe_title: safe_title = vid
        ext = ".mp4" if target['type'] in ('mp4', 'ultra') else ".ts"
        dest = os.path.join(path, f"{safe_title}{ext}")
        

        if target['type'] == 'mp4':
            _do_download_direct(target['url'], dest, title)
        elif target['type'] == 'ultra':
            _do_download_ultra(vid, dest, title)
        else:
            _do_download_hls(target['url'], dest, title)
            
    except Exception as e:
        _log_error(f"Download Error video {vid}: {str(e)}")
        xbmcgui.Dialog().ok("EspaTV Error", f"Error al preparar descarga: {str(e)}")

def _do_download_direct(url, dest, title):
    dp = xbmcgui.DialogProgress()
    dp.create("Descargando MP4", f"Conectando: {title}")
    tmp_dest = dest + ".tmp"
    try:
        clean_url = url.split('|')[0] if '|' in url else url
        dm_h = {"User-Agent": "Mozilla/5.0 (Linux; Android 7.1.1; Pixel Build/NMF26O) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.91 Mobile Safari/537.36", "Origin": "https://www.dailymotion.com", "Referer": "https://www.dailymotion.com/"}
        r = requests.get(clean_url, headers=dm_h, stream=True, timeout=30)
        total = int(r.headers.get('content-length', 0))
        done = 0
        with open(tmp_dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024*256):
                if dp.iscanceled(): break
                f.write(chunk)
                done += len(chunk)
                if total > 0:
                    percent = int(done * 100 / total)
                    msg = f"Descargado: {done/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB"
                    dp.update(percent, msg)
        cancelled = dp.iscanceled()
        dp.close()
        if not cancelled:
            os.rename(tmp_dest, dest)
            core_settings.log_download(title, dest, vid)
            xbmcgui.Dialog().ok("¡Éxito!", f"Video guardado correctamente en:\n{dest}")
        elif os.path.exists(tmp_dest):
            os.remove(tmp_dest)
    except Exception as e:
        if 'dp' in locals(): dp.close()
        if os.path.exists(tmp_dest): os.remove(tmp_dest)
        _log_error(f"Error Direct DL: {str(e)}")
        xbmcgui.Dialog().ok("Error", f"Error en transferencia: {str(e)}")

def _do_download_hls(playlist_url, dest, title):
    dp = xbmcgui.DialogProgress()
    dp.create("Descargando HLS", "Obteniendo segmentos...")
    dm_h = {"User-Agent": "Mozilla/5.0 (Linux; Android 7.1.1; Pixel Build/NMF26O) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.91 Mobile Safari/537.36", "Origin": "https://www.dailymotion.com", "Referer": "https://www.dailymotion.com/"}
    clean_url = playlist_url.split('|')[0] if '|' in playlist_url else playlist_url
    try:
        if '?' in clean_url:
            base_url = clean_url.split('?')[0].rsplit('/', 1)[0] + '/'
            params = '?' + clean_url.split('?')[1]
        else:
            base_url = clean_url.rsplit('/', 1)[0] + '/'
            params = ''

        m3u8_content = requests.get(clean_url, headers=dm_h, timeout=15).text
        segments = [l for l in m3u8_content.splitlines() if l and not l.startswith('#')]
        total_seg = len(segments)
        
        if total_seg == 0:
            xbmcgui.Dialog().ok("Error", "La lista de reproducción está vacía."); return

        tmp_dest = dest + ".tmp"
        with open(tmp_dest, 'wb') as f_out:
            for idx, seg_name in enumerate(segments):
                if dp.iscanceled(): break
                
                if seg_name.startswith('http'):
                    seg_url = seg_name
                else:
                    seg_url = base_url + seg_name + params
                
                success = False
                for attempt in range(3):
                    try:
                        seg_data = requests.get(seg_url, headers=dm_h, timeout=12).content
                        f_out.write(seg_data)
                        success = True
                        break
                    except Exception: continue
                
                if not success: raise Exception(f"Fallo crítico en segmento {idx+1}")
                
                percent = int((idx + 1) * 100 / total_seg)
                msg = f"Segmento {idx+1}/{total_seg} - {title}"
                dp.update(percent, msg)
        
        cancelled = dp.iscanceled()
        dp.close()
        if not cancelled:
            os.rename(tmp_dest, dest)
            core_settings.log_download(title, dest, "HLS")
            xbmcgui.Dialog().ok("¡Completado!", f"El video HLS se ha unido y guardado en:\n{dest}")
        elif os.path.exists(tmp_dest):
            os.remove(tmp_dest)
    except Exception as e:
        if 'dp' in locals(): dp.close()
        if os.path.exists(tmp_dest): os.remove(tmp_dest)
        _log_error(f"HLS Merge Error: {str(e)}")
        xbmcgui.Dialog().ok("Error Union HLS", f"Fallo: {str(e)}")

def _get_ytdlp_pkg_path():
    """Devuelve la ruta de paquetes yt-dlp en addon_data."""
    profile = xbmcvfs.translatePath(
        xbmcaddon.Addon().getAddonInfo('profile')
    )
    pkg_path = os.path.join(profile, "packages")
    if not os.path.exists(pkg_path):
        os.makedirs(pkg_path)
    return pkg_path


def _ensure_ytdlp():
    """Descarga yt-dlp wheel desde PyPI si no existe en addon_data/packages."""
    import io
    pkg_path = _get_ytdlp_pkg_path()
    ytdlp_dir = os.path.join(pkg_path, "yt_dlp")
    if os.path.isdir(ytdlp_dir):
        return True

    try:
        pypi_r = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=15)
        if pypi_r.status_code != 200:
            return False
        whl_url = None
        for u in pypi_r.json().get("urls", []):
            if u["filename"].endswith(".whl") and "py3-none-any" in u["filename"]:
                whl_url = u["url"]
                break
        if not whl_url:
            return False
        whl_r = requests.get(whl_url, timeout=120)
        if whl_r.status_code != 200:
            return False
        whl_data = io.BytesIO(whl_r.content)
        with zipfile.ZipFile(whl_data) as zf:
            for member in zf.namelist():
                if member.startswith("yt_dlp/"):
                    zf.extract(member, pkg_path)
        return os.path.isdir(ytdlp_dir)
    except Exception as e:
        _log_error(f"Error descargando yt-dlp: {str(e)}")
    return False


def _find_system_python():
    """Busca un Python del sistema >= 3.10 que pueda ejecutar yt-dlp."""
    import subprocess
    no_win = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    for py in ["python", "python3", "py"]:
        try:
            r = subprocess.run(
                [py, "-c", "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}'); assert v >= (3,10)"],
                capture_output=True, text=True, timeout=5,
                creationflags=no_win
            )
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def _write_dl_helper(pkg_path, vid, dest, progress_file):
    """Genera un script Python que descarga con yt-dlp y escribe progreso."""
    helper_path = os.path.join(pkg_path, "_dl_helper.py")
    script = f'''
import sys, os, json
sys.path.insert(0, {repr(pkg_path)})
import yt_dlp

progress_file = {repr(progress_file)}

def hook(d):
    info = {{"status": d.get("status", ""), "percent": 0, "text": ""}}
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        speed = d.get("speed") or 0
        if total > 0:
            info["percent"] = int(done * 100 / total)
            mb_d = done / (1024*1024)
            mb_t = total / (1024*1024)
            sp = speed / (1024*1024) if speed else 0
            info["text"] = f"{{mb_d:.1f}}MB / {{mb_t:.1f}}MB ({{sp:.1f}} MB/s)"
        elif done > 0:
            info["percent"] = 50
            info["text"] = f"Descargando: {{done/(1024*1024):.1f}}MB"
    elif d["status"] == "finished":
        info["percent"] = 95
        info["text"] = "Finalizando..."
    with open(progress_file, "w") as f:
        json.dump(info, f)

opts = {{
    "format": "best[ext=mp4]/best",
    "outtmpl": {repr(dest)},
    "quiet": True,
    "no_warnings": True,
    "progress_hooks": [hook],
    "noprogress": True,
}}
url = "https://www.dailymotion.com/video/{vid}"
try:
    ydl = yt_dlp.YoutubeDL(opts)
    ydl.download([url])
    with open(progress_file, "w") as f:
        json.dump({{"status": "done", "percent": 100, "text": "Completado"}}, f)
except Exception as e:
    with open(progress_file, "w") as f:
        json.dump({{"status": "error", "percent": 0, "text": str(e)[:300]}}, f)
'''
    with open(helper_path, 'w', encoding='utf-8') as f:
        f.write(script)
    return helper_path


def _do_download_ultra(vid, dest, title):
    """Modo ULTRA: Descarga via yt-dlp (helper script + system Python)."""
    import subprocess
    import threading

    # Android/dispositivos sin Python del sistema: informar
    if xbmc.getCondVisibility("System.Platform.Android"):
        xbmcgui.Dialog().ok(
            "EspaTV",
            "Las descargas de Dailymotion no están\n"
            "disponibles en Android.\n\n"
            "Dailymotion bloquea descargas directas.\n"
            "Usa un PC con Python 3.10+ instalado."
        )
        return

    try:
        if xbmcvfs.exists(dest):
            if not xbmcgui.Dialog().yesno("EspaTV", "El archivo ya existe. ¿Sobrescribir?"): return
            xbmcvfs.delete(dest)

        dp = xbmcgui.DialogProgress()
        dp.create("Descargando (Modo ULTRA)", "Preparando...")

        if not dest.endswith(".mp4"):
            dest = os.path.splitext(dest)[0] + ".mp4"

        # 1. Buscar Python del sistema >= 3.10
        dp.update(2, "Buscando Python del sistema...")
        py_cmd = _find_system_python()
        if not py_cmd:
            dp.close()
            xbmcgui.Dialog().ok(
                "EspaTV",
                "Se necesita Python 3.10+ instalado en el sistema.\n\n"
                "Descárgalo de python.org e instálalo\n"
                "marcando 'Add to PATH'.\n\n"
                "Después reinicia Kodi."
            )
            return

        # 2. Descargar yt-dlp si no existe
        pkg_path = _get_ytdlp_pkg_path()
        ytdlp_dir = os.path.join(pkg_path, "yt_dlp")
        if not os.path.isdir(ytdlp_dir):
            dp.update(5, "Descargando componente yt-dlp (~3MB)...")
            if not _ensure_ytdlp():
                dp.close()
                xbmcgui.Dialog().ok("EspaTV", "Error descargando yt-dlp.\nComprueba tu conexión a internet.")
                return

        # 3. Generar y ejecutar script auxiliar
        dp.update(10, "Iniciando descarga...")
        progress_file = os.path.join(pkg_path, "_dl_progress.json")
        if os.path.exists(progress_file):
            os.remove(progress_file)

        helper_path = _write_dl_helper(pkg_path, vid, dest, progress_file)
        no_win = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

        proc = subprocess.Popen(
            [py_cmd, helper_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=no_win
        )

        # 4. Polling de progreso
        cancelled = False
        while proc.poll() is None:
            if dp.iscanceled():
                cancelled = True
                proc.kill()
                break
            if os.path.exists(progress_file):
                try:
                    with open(progress_file, 'r') as f:
                        info = json.loads(f.read())
                    dp.update(info.get("percent", 0), info.get("text", "Descargando..."))
                except Exception:
                    pass
            time.sleep(0.5)

        dp.close()

        # Limpiar script auxiliar
        for tmp in [helper_path, progress_file]:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except Exception: pass

        if cancelled:
            for ext in ["", ".part"]:
                p = dest + ext
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            return

        # Leer resultado final
        if proc.returncode == 0 and os.path.exists(dest):
            core_settings.log_download(title, dest, "MP4 (ULTRA)")
            xbmcgui.Dialog().ok("EspaTV", "¡Descarga ULTRA completada!")
        else:
            stderr = ""
            try:
                stderr = proc.stderr.read().decode('utf-8', errors='replace')[:200]
            except Exception:
                pass
            raise Exception(f"yt-dlp falló (código {proc.returncode}): {stderr}")

    except Exception as e:
        if 'dp' in locals(): dp.close()
        for ext in [".part", ".tmp"]:
            p = dest + ext
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        _log_error(f"Ultra Error: {str(e)}")
        xbmcgui.Dialog().ok("Error Ultra", f"Fallo en descarga Ultra:\n{str(e)}")

def _sr(q, extra_params=None):
    p = {}
    if core_settings.is_recent_active():
        p["sort"] = "recent"
    if extra_params:
        p.update(extra_params)
    return _dm_search(q, p if p else None)

def _gsr(q, rs, mode=""):
    if not rs: return []
    sc = []; ql = q.lower().strip(); qn = re.findall(r'(\d+)', ql)
    for r in rs:
        t = r.get("title", "").strip(); tl = t.lower()
        if not t: continue
        

        rt = difflib.SequenceMatcher(None, ql, tl).ratio(); s = rt
        
        # FILTRO ESTRICTO: Si no contiene las palabras clave de la busqueda, penalizar fuertemente
        # (Esto evita que salgan resultados random como 'La Monja' buscando 'Top Gun')
        q_words = [w for w in ql.split() if len(w) > 3] # Palabras importantes (>3 letras)
        if q_words:
            matches = sum(1 for w in q_words if w in tl)
            if matches == 0: 
                s -= 1.0 # Penalizacion masiva si no coincide ninguna palabra clave
            elif matches < len(q_words):
                s -= 0.1 # Pequeña penalizacion si faltan palabras

        if ql in tl: s += 0.35 # Match contenido exacto
        

        words_q = set(ql.split())
        words_t = set(tl.split())
        common = words_q.intersection(words_t)
        if len(common) > 0: s += (len(common) / len(words_q)) * 0.2
        

        tn = re.findall(r'(\d+)', tl)
        if qn and tn:
            if qn[-1] in tn: s += 0.4
            if set(qn).issubset(set(tn)): s += 0.1
            

        # IMPORTANTE: Solo aplicamos bonus de duracion si el titulo coincide razonablemente bien (s > 0.3)
        # Esto evita que 'La Monja' (larga) gane a 'Iron Man 3' (corto) si el titulo no se parece.
        du = r.get("duration", 0)
        try: du = int(du)
        except (ValueError, TypeError): du = 0
        
        if s > 0.3: # Solo si hay match de titulo decente
            if not core_settings.is_prioritize_match_active():
                # Modo ESTÁNDAR: Fuerte bonus por duración
                if du > 600: s += 0.6    # Gran bonus si coincide titulo y es larga
                elif du > 300: s += 0.2
            else:
                # Modo PRECISIÓN: Bonus por duración mínimo
                if du > 600: s += 0.05
            
            if du < 180: s -= 0.1  # Penalizacion leve por ser muy corto (trailer)
        else:
            # Si el titulo no coincide bien, la duracion NO ayuda.
            pass
        

        if any(w in tl for w in ["español", "castellano", "spanish"]):
            s += 0.15

        # Filtrado especifico de peliculas
        if mode == "movie":
            # Penalizar contenido que no sea pelicula
            if any(w in tl for w in ["trailer", "avance", "entrevista", "making", "detras", "clip", "promo"]):
                s -= 0.5
            # Bonus por indicadores de pelicula
            if any(w in tl for w in ["pelicula", "completa", "movie", "film"]):
                s += 0.2

        s = min(s, 1.0)
        sc.append((s, r))
    sc.sort(key=lambda x: x[0], reverse=True); return sc


def _show_yt_fav_news():
    """Busca vídeos recientes de los canales YouTube en favoritos."""
    favs = core_settings.get_favorites()
    channels = []
    for f in favs:
        if f.get('action') != 'felicidad_play':
            continue
        params = f.get('params', {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                continue
        if params.get('ctype') == 'channel' and params.get('yt_id'):
            channels.append({'yt_id': params['yt_id'], 'name': params.get('name', 'Canal')})

    if not channels:
        xbmcgui.Dialog().ok("EspaTV", "No tienes canales de YouTube en favoritos.\n\nAñade canales desde las secciones de YouTube.")
        return

    pdp = xbmcgui.DialogProgress()
    pdp.create("Novedades YouTube", "Buscando vídeos recientes...")

    all_videos = []
    total = len(channels)
    for i, ch in enumerate(channels):
        if pdp.iscanceled():
            break
        pdp.update(int((i / max(total, 1)) * 100), "Analizando: [B]{0}[/B]".format(ch['name']))
        try:
            url = "https://www.youtube.com/channel/{0}/videos".format(ch['yt_id'])
            r = requests.get(url, headers={"Accept-Language": "es"}, timeout=12)
            if r.status_code != 200:
                continue
            data = _yt_parse_initial_data(r.text)
            if not data:
                continue
            # Extraer de la pestaña de vídeos del canal
            vids = []
            try:
                tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
                for tab in tabs:
                    tr = tab.get("tabRenderer", {})
                    if tr.get("selected"):
                        contents = tr.get("content", {}).get("richGridRenderer", {}).get("contents", [])
                        for item in contents:
                            ri = item.get("richItemRenderer", {}).get("content", {}).get("videoRenderer")
                            if not ri:
                                continue
                            vid = ri.get("videoId", "")
                            if not vid:
                                continue
                            title = ""
                            runs = ri.get("title", {}).get("runs", [])
                            if runs:
                                title = runs[0].get("text", "")
                            dur = ri.get("lengthText", {}).get("simpleText", "")
                            views = ri.get("viewCountText", {}).get("simpleText", "")
                            if not views:
                                views = ri.get("shortViewCountText", {}).get("simpleText", "")
                            published = ri.get("publishedTimeText", {}).get("simpleText", "")
                            if title and vid:
                                vids.append({
                                    "vid": vid, "title": title, "duration": dur,
                                    "channel": ch['name'], "views": views,
                                    "published": published
                                })
                            if len(vids) >= 5:
                                break
                        break
            except (KeyError, TypeError):
                pass
            all_videos.extend(vids)
        except Exception:
            continue

    pdp.close()

    if not all_videos:
        xbmcgui.Dialog().ok("Novedades YouTube", "No se encontraron vídeos recientes en tus canales favoritos.")
        return

    h = int(sys.argv[1])
    xbmcplugin.setContent(h, 'videos')
    for v in all_videos:
        vid = v["vid"]
        title = v["title"]
        li = xbmcgui.ListItem(label="[COLOR cyan][{0}][/COLOR] {1}".format(v["channel"], title))
        thumb = 'https://i.ytimg.com/vi/{0}/hqdefault.jpg'.format(vid)
        li.setArt({'icon': 'DefaultMusicVideos.png', 'thumb': thumb})
        plot_lines = []
        if v.get("channel"):
            plot_lines.append("Canal: {0}".format(v["channel"]))
        if v.get("duration"):
            plot_lines.append("Duración: {0}".format(v["duration"]))
        if v.get("views"):
            plot_lines.append("Vistas: {0}".format(v["views"]))
        if v.get("published"):
            plot_lines.append("Subido: {0}".format(v["published"]))
        plot = "\n".join(plot_lines) if plot_lines else title
        li.setInfo('video', {'plot': plot, 'title': title})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="felicidad_play", yt_id=vid, name=title, ctype="video"), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)


def _show_fav_news():
    """Agregador: Busca contenido reciente de los favoritos con margen dinámico."""
    favs = core_settings.get_favorites()
    if not favs:
        xbmcgui.Dialog().notification("EspaTV", "No tienes favoritos guardados", xbmcgui.NOTIFICATION_WARNING)
        return
        
    # 1. Preguntar margen
    opts = [
        "Últimas 24 horas",
        "Últimas 48 horas (Recomendado)",
        "Última semana (7 días)",
        "Último mes (30 días - Lento)"
    ]
    sel = xbmcgui.Dialog().select("Margen de búsqueda de Novedades", opts)
    if sel < 0: return # Cancelado
    
    margins = [86400, 172800, 604800, 2592000]
    time_limit = int(time.time()) - margins[sel]
    sel_label = opts[sel]

    pdp = xbmcgui.DialogProgress()
    pdp.create("Novedades Favoritos DM", "Buscando: {0}...".format(sel_label))
    
    all_results = []
    
    queries = []
    seen = set()
    for f in favs:
        q = None
        if f.get('action') == 'lfr':
            q = f.get('params', {}).get('q')
        
        if q and q.lower() not in seen:
            queries.append(q)
            seen.add(q.lower())
            
    if not queries:
        pdp.close()
        xbmcgui.Dialog().ok("Mi Periódico", "No hay series o búsquedas en favoritos para analizar.")
        return

    total = len(queries)
    for i, q in enumerate(queries):
        if pdp.iscanceled(): break
        pdp.update(int((i / max(total, 1)) * 100), f"Analizando: [B]{q}[/B]")
        
        rs = _sr(q, {"created_after": time_limit})
        if rs:
            sr = _gsr(q, rs, mode="keywords") 
            for score, v in sr:
                if score > 0.35: # Filtro de calidad para evitar basura
                    vid = v.get("id")
                    if not any(r[1].get("id") == vid for r in all_results):
                        all_results.append((score, v, q))

    pdp.close()
    
    if not all_results:
        xbmcgui.Dialog().ok("Novedades Favoritos DM", "No se han encontrado novedades en '{0}' para tus favoritos.".format(sel_label))
        return

    # Mostrar resultados consolidados
    h = int(sys.argv[1])
    all_results.sort(key=lambda x: x[0], reverse=True)
    xbmcplugin.setContent(h, 'videos')
    
    for score, v, origin in all_results:
        vt = v.get("title", "Video"); vi = v.get("id"); ow = v.get("owner.username", "Unknown"); du = v.get("duration", 0)
        try: du = int(du)
        except (ValueError, TypeError): du = 0
        dth = v.get("thumbnail_url") or v.get("thumbnail_360_url")
        
        label = f"[COLOR yellow][{origin}][/COLOR] {vt}"
        li = xbmcgui.ListItem(label=label)
        li.setArt({'thumb': dth, 'icon': dth})
        li.setInfo('video', {'title': vt, 'plot': f"Favorito: {origin}\nSubido por: {ow}\nDuración: {du}s", 'duration': du})
        li.setProperty("IsPlayable", "true")
        
        cm = []
        cm.append(("Buscar en webs de torrent", f"RunPlugin({_u(action='elementum_search', q=vt)})"))
        cm.append(("Descargar Video", f"RunPlugin({_u(action='download_video', vid=vi, title=vt)})"))
        cm.append(("Abrir en navegador", f"RunPlugin({_u(action='dm_open_browser', url=vi)})"))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vi)))))
        li.addContextMenuItems(cm)

        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="pv", vid=vi), listitem=li, isFolder=False)
        
    xbmcplugin.endOfDirectory(h)

def _show_favorites():
    favs = core_settings.get_favorites()
    

    _FLOWFAV_REPO_ID = 'repository.flowfavmanager'
    _FLOWFAV_ADDON_ID = 'plugin.program.flowfavmanager'
    try:
        xbmcaddon.Addon(_FLOWFAV_ADDON_ID)
        li_flow = xbmcgui.ListItem(label="[COLOR violet][B]Flow FavManager[/B][/COLOR]")
        li_flow.setArt({'icon': 'DefaultAddonProgram.png'})
        li_flow.setInfo('video', {'plot': 'Abre el editor avanzado de favoritos de Kodi.\nOrganiza, personaliza colores, iconos, formatos, crea secciones, perfiles y copias de seguridad.'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="open_flowfav"), listitem=li_flow, isFolder=False)
    except Exception:
        li_flow = xbmcgui.ListItem(label="[COLOR gray]Flow FavManager (No instalado)[/COLOR]")
        li_flow.setArt({'icon': 'DefaultAddonProgram.png'})
        li_flow.setInfo('video', {'plot': 'Gestor avanzado de favoritos para Kodi.\nPulsa para instalar o ver instrucciones.'})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="flowfav_install"), listitem=li_flow, isFolder=False)


    cats = category_manager.load_cats()
    if cats:
         li_c = xbmcgui.ListItem(label=f"[COLOR yellow][B]Mis Categorías ({len(cats)})[/B][/COLOR]")
         li_c.setArt({'icon': 'DefaultAddonService.png'})
         li_c.setInfo('video', {'plot': "Accede a tus carpetas y listas personalizadas ('Infantil', 'Noticias', etc).\n\nPuedes crear nuevas categorías desde el menú principal."})
         xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="cat_menu"), listitem=li_c, isFolder=True)
    

    if favs:
         li_n = xbmcgui.ListItem(label="[COLOR yellow][B]Novedades Favoritos DM[/B][/COLOR]")
         li_n.setArt({'icon': 'DefaultAddonService.png'})
         li_n.setInfo('video', {'plot': "Analiza automáticamente tus favoritos y busca contenido subido recientemente.\n\nPodrás elegir el margen de tiempo (24h, 48h, 7 días...).\n\nIdeal para ver qué hay de nuevo sin ir sección por sección."})
         xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="fav_news"), listitem=li_n, isFolder=True)

         li_yt = xbmcgui.ListItem(label="[COLOR cyan][B]Novedades YouTube de Favoritos[/B][/COLOR]")
         li_yt.setArt({'icon': 'DefaultMusicVideos.png'})
         li_yt.setInfo('video', {'plot': "Busca vídeos recientes en los canales de YouTube que tengas en favoritos.\n\nMuestra los 5 últimos vídeos de cada canal."})
         xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="yt_fav_news"), listitem=li_yt, isFolder=True)


    if not favs:
        if not cats: # Only show 'empty' if no cats either
             li = xbmcgui.ListItem(label="No tienes favoritos guardados")
             li.setArt({'icon': 'DefaultIconInfo.png'})
             li.setInfo('video', {'plot': "Haz clic derecho en cualquier programa o serie y selecciona 'Añadir a Mis Favoritos' para guardarlo aquí."})
             xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)
    else:
        for f in favs:
            li = xbmcgui.ListItem(label=f['title'])
            li.setArt({'icon': f['icon'], 'thumb': f['icon']})
        
            cm = []
            cm.append(("Buscar en webs de torrent", f"RunPlugin({_u(action='elementum_search', q=f['title'])})"))



            cm.append(("Editar nombre y buscar", f"RunPlugin({_u(action='edit_and_search', q=f['title'], ot=f['icon'])})"))

            cm.append(("Renombrar...", f"RunPlugin({_u(action='fav_rename', fav_url=f['url'], old_title=f['title'])})"))
            cm.append(("Mover arriba", f"RunPlugin({_u(action='fav_move_up', fav_url=f['url'])})"))
            cm.append(("Mover abajo", f"RunPlugin({_u(action='fav_move_down', fav_url=f['url'])})"))
            

            # Usar 'cat_move_from_favs' para MOVER (borra de favs y añade a cat) en lugar de solo añadir.
            cm.append(("Mover a Categoría...", f"RunPlugin({_u(action='cat_move_from_favs', fav_url=f['url'], q=f['title'])})"))
            

            cm.append(("Eliminar de Favoritos", f"RunPlugin({_u(action='remove_favorite', fav_url=f['url'])})"))
            
            li.addContextMenuItems(cm)
            
            # Reconstruir la URL con la acción y parámetros guardados
            # URL que se abre al hacer clic en el favorito:
            u_params = f"action={f['action']}"
            for k,v in f.get('params', {}).items():
                u_params += f"&{k}={urllib.parse.quote(str(v))}"
            

            if f['action'] == 'lfr' and '&nh=' not in u_params:
                u_params += "&nh=1"
            
            non_folder_actions = ('play_podcast', 'play_tdt', 'pv')
            is_folder = f['action'] not in non_folder_actions
            if f['action'] == 'felicidad_play':
                fav_ctype = f.get('params', {}).get('ctype', 'channel')
                is_folder = (fav_ctype == 'video')
            xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=sys.argv[0]+"?"+u_params, listitem=li, isFolder=is_folder)


    li = xbmcgui.ListItem(label="[COLOR cyan]Exportar Favoritos...[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Guarda tus favoritos principales en un archivo JSON para compartirlos."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="fav_export"), listitem=li, isFolder=False)
    
    li = xbmcgui.ListItem(label="[COLOR cyan]Importar Favoritos...[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Carga favoritos desde un archivo JSON."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="fav_import"), listitem=li, isFolder=False)



    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _show_downloads():
    # Acceso directo a descargas de Elementum (solo si está instalado)
    if xbmc.getCondVisibility("System.HasAddon(plugin.video.elementum)"):
        li = xbmcgui.ListItem(label="[B][COLOR cyan]Ver Descargas de Elementum (Torrents)[/COLOR][/B]")
        li.setArt({'icon': 'DefaultAddonProgram.png'})
        li.setInfo('video', {'plot': "Abre la sección de descargas/torrents de Elementum para ver el progreso y archivos descargados."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="plugin://plugin.video.elementum/torrents/", listitem=li, isFolder=True)
    else:
        li = xbmcgui.ListItem(label="[COLOR grey]Elementum no instalado[/COLOR]")
        li.setArt({'icon': 'DefaultIconWarning.png'})
        li.setInfo('video', {'plot': "Para ver descargas de torrents, instala el addon Elementum desde su repositorio oficial.\n\nElementum permite buscar y descargar torrents directamente desde Kodi."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)


    dls = core_settings.get_downloads()
    if not dls:
        li = xbmcgui.ListItem(label="No hay historial de descargas")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        li.setInfo('video', {'plot': "Cuando descargues un vídeo con éxito, aparecerá aquí.\nPuedes descargar vídeos desde los resultados de búsqueda pulsando clic derecho."})
        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)
    else:
        for d in dls:
            path = d['path']
            if not os.path.exists(path):
                label = f"[COLOR grey][ELIMINADO][/COLOR] {d['title']}"
                is_valid = False
            else:
                label = f"{d['title']} ({d['date']})"
                is_valid = True
                
            li = xbmcgui.ListItem(label=label)
            li.setArt({'icon': 'DefaultVideo.png'})
            li.setInfo('video', {'title': d['title'], 'plot': f"Archivo: {path}\nDescargado el: {d['date']}"})
            
            cm = [("Quitar del historial", f"RunPlugin({_u(action='remove_download', dl_path=path)})")]
            if is_valid:
                 li.setProperty("IsPlayable", "true")
                 cm.append(("[COLOR red]ELIMINAR ARCHIVO DEL DISCO[/COLOR]", f"RunPlugin({_u(action='delete_download_file', dl_path=path, title=d['title'])})"))
             
                 u = path
            else:
                 u = ""
                 
            li.addContextMenuItems(cm)
            xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=u, listitem=li, isFolder=False if is_valid else True)
        
    # Nota legal de descargas
    li = xbmcgui.ListItem(label="[COLOR grey][I]Aviso: Descargar vídeos puede infringir los Términos de Servicio de las plataformas y las leyes de propiedad intelectual. No se incluye descarga de YouTube por restricciones legales y por la agresividad de Google contra estas prácticas. El usuario es el único responsable del uso que haga de esta función.[/I][/COLOR]")
    li.setArt({'icon': 'DefaultIconWarning.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _add_fav_cm(li, title, platform, action, params):
    # Prepare URL for favorites (re-entrant call)
    # Mejorar la unicidad del ID para evitar duplicados falsos
    id_val = params.get('cid') or params.get('pid') or params.get('url') or params.get('au') or params.get('q')
    if not id_val:
        # Fallback critico: Usar hash de params si no hay ID claro
        id_val = str(hash(json.dumps(params, sort_keys=True)))
    fav_url = f"{action}_{id_val}"
    params_json = json.dumps(params)
    li.addContextMenuItems([
        ("Añadir a Mis Favoritos", f"RunPlugin({_u(action='add_favorite', title=title, fav_url=fav_url, icon=li.getArt('icon'), platform=platform, fav_action=action, params=params_json)})")
    ])

def _delete_download_file(path, title):
    if xbmcgui.Dialog().yesno("Eliminar Archivo", f"¿Estás seguro de que quieres eliminar físicamente el archivo?\n\n[B]{title}[/B]\n\nEsta acción eliminará el archivo de tu disco duro para siempre."):
        try:
            if os.path.exists(path):
                os.remove(path)
                core_settings.remove_download(path)
                xbmcgui.Dialog().notification("EspaTV", "Archivo eliminado satisfactoriamente", xbmcgui.NOTIFICATION_INFO)
                xbmc.executebuiltin("Container.Refresh")
            else:
                xbmcgui.Dialog().ok("EspaTV", "El archivo ya no existe en esa ruta.")
                core_settings.remove_download(path)
                xbmc.executebuiltin("Container.Refresh")
        except Exception as e:
            xbmcgui.Dialog().ok("EspaTV Error", f"No se pudo eliminar: {str(e)}")


def _import_backup():
    f = xbmcgui.Dialog().browse(1, 'Seleccionar Backup', 'files', '.zip', False, False, '')
    if not f: return

    file_labels = {
        'settings.json': 'Configuración General',
        'favorites.json': 'Favoritos',
        'search_history.json': 'Historial de Búsquedas DM',
        'yt_search_history.json': 'Historial de Búsquedas YouTube',
        'custom_categories.json': 'Categorías Personalizadas',
        'dm_settings.json': 'Ajustes Dailymotion',
        'watch_history.json': 'Historial de Visionado',
        'custom_iptv.json': 'Listas IPTV Personalizadas',
        'url_history.json': 'Historial de URLs',
        'url_bookmarks.json': 'Marcadores de URLs',
        'saved_playlists.json': 'Playlists DM Guardadas',
        'yt_playlists.json': 'Playlists YouTube Guardadas',
        'downloads_history.json': 'Historial de Descargas',
        'trakt_settings.json': 'Configuración Trakt',
    }
    mergeable = ['favorites.json', 'search_history.json', 'yt_search_history.json',
                 'watch_history.json', 'url_history.json', 'url_bookmarks.json', 'custom_iptv.json',
                 'saved_playlists.json', 'yt_playlists.json', 'downloads_history.json']
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))

    try:
        with zipfile.ZipFile(f, 'r') as zf:
            available = zf.namelist()
        known = [fz for fz in available if fz in file_labels]
    
        cache_files = [fz for fz in available if fz.startswith("cat_cache_") and fz.endswith(".json")]
        if cache_files:
            known.append('__cache__')
            file_labels['__cache__'] = 'Caché de Categorías ({0})'.format(len(cache_files))
    
        if not known:
            xbmcgui.Dialog().ok("EspaTV", "Este archivo no contiene datos reconocidos.")
            return

        options = [file_labels.get(fz, fz) for fz in known]
        selected = xbmcgui.Dialog().multiselect("¿Qué deseas importar?", options,
                                                preselect=list(range(len(options))))
        if selected is None or len(selected) == 0: return

        files_to_restore = [known[i] for i in selected]
        restored = 0
        with zipfile.ZipFile(f, 'r') as zf:
            for fz in files_to_restore:
                if fz == '__cache__':
                    for cf in cache_files:
                        data = zf.read(cf)
                        with open(os.path.join(p, cf), 'wb') as f_out:
                            f_out.write(data)
                        restored += 1
                    continue
                dest_path = os.path.join(p, fz)
                if fz in mergeable and os.path.exists(dest_path):
                    choice = xbmcgui.Dialog().select(file_labels[fz],
                        ["Sustituir (borrar actual)", "Añadir (fusionar con actual)", "Omitir"])
                    if choice == 2 or choice == -1: continue
                    elif choice == 1:
                        try:
                            with open(dest_path, 'r', encoding='utf-8') as fh: current = json.load(fh)
                            with zf.open(fz) as zfile: backup = json.load(zfile)
                            if isinstance(current, list) and isinstance(backup, list):
                                merged = current + [x for x in backup if x not in current]
                                with open(dest_path, 'w', encoding='utf-8') as fh: json.dump(merged, fh, ensure_ascii=False)
                                restored += 1; continue
                            elif isinstance(current, dict) and isinstance(backup, dict):
                                current.update(backup)
                                with open(dest_path, 'w', encoding='utf-8') as fh: json.dump(current, fh, ensure_ascii=False)
                                restored += 1; continue
                        except Exception: pass
                resolved = os.path.realpath(os.path.join(p, fz))
                if not resolved.startswith(os.path.realpath(p)):
                    continue
                zf.extract(fz, p)
                restored += 1

        xbmcgui.Dialog().ok("Importación Completa", "Se restauraron {0} elementos.".format(restored))
        xbmc.executebuiltin("Container.Refresh")

    except Exception as e:
        xbmcgui.Dialog().ok("Error", "Fallo al importar:\n{0}".format(e))



def _export_favorites():
    """Exporta solo la lista de favoritos principales (favorites.json)"""
    favs = core_settings.get_favorites()
    if not favs:
        xbmcgui.Dialog().notification("EspaTV", "No hay favoritos para exportar", xbmcgui.NOTIFICATION_INFO)
        return
        
    ts = time.strftime("%Y%m%d_%H%M")
    default_name = f"EspaTV_favoritos_{ts}.json"
    
    d = xbmcgui.Dialog().browse(3, 'Guardar Favoritos', 'files', '', False, False, default_name)
    if not d: return
    
    if os.path.isdir(d):
        d = os.path.join(d, default_name)
    elif not d.lower().endswith(".json"):
        d += ".json"
        
    try:
        with open(d, 'w', encoding='utf-8') as f:
            json.dump(favs, f, ensure_ascii=False, indent=2)
        
        xbmcgui.Dialog().ok("Exportar Favoritos", f"Guardado correctamente:\n{os.path.basename(d)}\n\nElementos: {len(favs)}")
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))

def _import_favorites():
    """Importa lista de favoritos desde JSON"""
    f = xbmcgui.Dialog().browse(1, 'Seleccionar archivo de favoritos', 'files', '.json', False, False, '')
    if not f: return
    
    try:
        with open(f, 'r', encoding='utf-8') as fp:
            new_favs = json.load(fp)
        
        if not isinstance(new_favs, list):
            xbmcgui.Dialog().ok("Error", "El archivo no es una lista válida de favoritos.")
            return


        if new_favs and 'url' not in new_favs[0]:
            xbmcgui.Dialog().ok("Error", "El formato del JSON no parece ser de favoritos de EspaTV.")
            return

        opts = ["Fusionar (Añadir nuevos)", "Reemplazar lista completa"]
        sel = xbmcgui.Dialog().select("¿Cómo importar?", opts)
        if sel < 0: return
        
        if sel == 0:
    
            added = 0
            for item in new_favs:
                if core_settings.add_favorite(item['title'], item['url'], item['icon'], item['platform'], item['action'], item['params']):
                    added += 1
            xbmcgui.Dialog().notification("EspaTV", f"{added} favoritos añadidos", xbmcgui.NOTIFICATION_INFO)
            
        else:
            # Sobreescribir manualmente ya que core_settings no expone set_favorites

            profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
            fav_file = os.path.join(profile, 'favorites.json')
            with open(fav_file, 'w', encoding='utf-8') as out:
                json.dump(new_favs, out)
            xbmcgui.Dialog().notification("EspaTV", "Lista reemplazada", xbmcgui.NOTIFICATION_INFO)
            
        xbmc.executebuiltin("Container.Refresh")
            
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))


        
def _toggle_advanced_search():
    new_state = not core_settings.is_advanced_search_active()
    core_settings.set_advanced_search_active(new_state)
    msg = "MENÚ AVANZADO ACTIVADO" if new_state else "BÚSQUEDA DIRECTA (SIMPLE)"
    xbmcgui.Dialog().notification("EspaTV", msg, xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _advanced_search_menu():
    # === BÚSQUEDAS BÁSICAS ===
    li = xbmcgui.ListItem(label="[B]— BÚSQUEDAS BÁSICAS —[/B]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Búsqueda Estándar (Por Título)")
    li.setArt({'icon': 'DefaultAddonWebSkin.png'})
    _add_fav_cm(li, "Búsqueda Estándar", "search", "execute_adv_search", {"mode": ""})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode=""), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Búsqueda de Películas (Modo Cine)")
    li.setArt({'icon': 'DefaultMovies.png'})
    li.setInfo('video', {'plot': "Optimiza la búsqueda para encontrar películas completas en castellano.\nPrueba variantes como 'pelicula completa', 'castellano', etc."})
    _add_fav_cm(li, "Búsqueda de Películas", "search", "execute_adv_search", {"mode": "movie"})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="movie"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Búsqueda de Series (Temporada + Capítulo)")
    li.setArt({'icon': 'DefaultTVShows.png'})
    li.setInfo('video', {'plot': "Introduce el nombre de la serie y el addon te pedirá número de temporada y capítulo.\nConstruye automáticamente la mejor query posible."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="series"), listitem=li, isFolder=True)

    # === FILTROS DE DURACIÓN ===
    li = xbmcgui.ListItem(label="[B]— FILTROS DE DURACIÓN —[/B]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Búsqueda de Vídeos Largos (+20 min)")
    li.setArt({'icon': 'DefaultVideo.png'})
    li.setInfo('video', {'plot': "Filtra los resultados para mostrar solo vídeos de más de 20 minutos.\nIdeal para encontrar episodios completos o películas."})
    _add_fav_cm(li, "Vídeos Largos (+20 min)", "search", "execute_adv_search", {"mode": "long"})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="long"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Búsqueda de Vídeos Cortos (-10 min)")
    li.setArt({'icon': 'DefaultVideo.png'})
    li.setInfo('video', {'plot': "Filtra los resultados para mostrar solo vídeos de menos de 10 minutos.\nIdeal para buscar clips, canciones o sketches."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="short"), listitem=li, isFolder=True)

    # === FILTROS TEMPORALES ===
    li = xbmcgui.ListItem(label="[B]— FILTROS TEMPORALES —[/B]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Contenido de Hoy (Últimas 24h)")
    li.setArt({'icon': 'DefaultFolder.png'})
    li.setInfo('video', {'plot': "Muestra solo vídeos subidos en las últimas 24 horas.\nPerfecto para informativos y programas diarios."})
    _add_fav_cm(li, "Contenido de Hoy (24h)", "search", "execute_adv_search", {"mode": "recent_24h"})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="recent_24h"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Contenido de Esta Semana")
    li.setArt({'icon': 'DefaultFolder.png'})
    li.setInfo('video', {'plot': "Muestra solo vídeos subidos en los últimos 7 días."})
    _add_fav_cm(li, "Contenido de esta Semana", "search", "execute_adv_search", {"mode": "recent_week"})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="recent_week"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Contenido de Este Mes")
    li.setArt({'icon': 'DefaultFolder.png'})
    li.setInfo('video', {'plot': "Muestra solo vídeos subidos en los últimos 30 días."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="recent_month"), listitem=li, isFolder=True)

    # === FILTROS DE CALIDAD E IDIOMA ===
    li = xbmcgui.ListItem(label="[B]— CALIDAD E IDIOMA —[/B]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Solo HD (Alta Definición)")
    li.setArt({'icon': 'DefaultVideo.png'})
    li.setInfo('video', {'plot': "Filtra para mostrar únicamente vídeos en HD (720p o superior)."})
    _add_fav_cm(li, "Solo HD", "search", "execute_adv_search", {"mode": "hd"})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="hd"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Solo Contenido en Español")
    li.setArt({'icon': 'DefaultFolder.png'})
    li.setInfo('video', {'plot': "Filtra para mostrar solo vídeos con idioma español configurado."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="spanish"), listitem=li, isFolder=True)

    # === FILTROS AVANZADOS ===
    li = xbmcgui.ListItem(label="[B]— FILTROS AVANZADOS —[/B]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Búsqueda por Usuario / Canal")
    li.setArt({'icon': 'DefaultFolder.png'})
    li.setInfo('video', {'plot': "Busca contenido subido ÚNICAMENTE por un usuario específico (ej: 'rtve', 'atresplayer').\n\nPuedes filtrar por texto dentro del canal."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="user"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Búsqueda Inversa (Excluir palabras)")
    li.setArt({'icon': 'DefaultIconError.png'})
    li.setInfo('video', {'plot': "Busca X pero EXCLUYENDO resultados que contengan ciertas palabras.\nÚtil para evitar 'trailer', 'making of', 'clip', etc."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="exclude"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Búsqueda Exacta (Sin limpieza)")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Busca EXACTAMENTE lo que escribas, sin intentar limpiar el título ni quitar paréntesis."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="exact"), listitem=li, isFolder=True)
    
    li = xbmcgui.ListItem(label="Búsqueda por Palabras Clave")
    li.setArt({'icon': 'DefaultAddonWebSkin.png'})
    li.setInfo('video', {'plot': "Extrae solo las palabras importantes (más de 3 letras) y busca por ellas. Útil si el título oficial es muy largo o complejo."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="keywords"), listitem=li, isFolder=True)

    # === UTILIDADES ===
    li = xbmcgui.ListItem(label="[B]— UTILIDADES —[/B]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url="", listitem=li, isFolder=False)
    
    li = xbmcgui.ListItem(label="Repetir Última Búsqueda (con filtros)")
    li.setArt({'icon': 'DefaultAddonRepository.png'})
    li.setInfo('video', {'plot': "Toma la última búsqueda de tu historial y te permite ejecutarla con cualquiera de los filtros anteriores."})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="execute_adv_search", mode="repeat_last"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="Búsqueda Múltiple (varios términos)")
    li.setArt({'icon': 'DefaultAddonWebSkin.png'})
    li.setInfo('video', {'plot': "Busca varios términos separados por comas en Dailymotion.\nEjemplo: 'saber y ganar, pasapalabra, el hormiguero'"})
    xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="multi_search"), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(int(sys.argv[1]))

def _execute_adv_search(mode):
    ep = None
    exclude_words = None
    q = ""
    

    
    # --- BUSQUEDA POR USUARIO ---
    if mode == "user":
        kb = xbmc.Keyboard('', 'Introduce el ID de Usuario/Canal')
        kb.doModal()
        if not kb.isConfirmed(): return
        user = kb.getText()
        if not user: return
        ep = {"owner": user}
        

        kb = xbmc.Keyboard('', 'Buscar en canal de {0} (Opcional, vacío = últimos vídeos)'.format(user))
        kb.doModal()
        if kb.isConfirmed():
            q = kb.getText()
        # Si vacio, buscamos lo ultimo de ese usuario
        if q: _add_to_history(q)
        _lfd(q if q else " ", "", mode=mode, extra_params=ep, nh=True)
        return
    
    # --- SERIES SEARCH (ask for name, season, episode) ---
    if mode == "series":
        kb = xbmc.Keyboard('', 'Nombre de la Serie')
        kb.doModal()
        if not kb.isConfirmed(): return
        series_name = kb.getText()
        if not series_name: return
        

        kb = xbmc.Keyboard('', 'Número de Temporada (vacío = cualquiera)')
        kb.doModal()
        if not kb.isConfirmed(): return
        season = kb.getText()
        
        # Pedir capítulo
        kb = xbmc.Keyboard('', 'Número de Capítulo (vacío = cualquiera)')
        kb.doModal()
        if not kb.isConfirmed(): return
        episode = kb.getText()
        

        q = series_name
        if season:
            q += f" T{season}" # Format: T1, T2, etc.
        if episode:
            q += f" Capitulo {episode}"
            # Intentar formatos alternativos
        
        _add_to_history(q)
        _lfd(q, "", mode="", extra_params=None, nh=True)
        return
    
    # --- EXCLUDE SEARCH (ask for search + words to exclude) ---
    if mode == "exclude":
        kb = xbmc.Keyboard('', '¿Qué quieres buscar?')
        kb.doModal()
        if not kb.isConfirmed(): return
        q = kb.getText()
        if not q: return
        
        kb = xbmc.Keyboard('trailer, clip, making of', 'Palabras a EXCLUIR (separadas por coma)')
        kb.doModal()
        if not kb.isConfirmed(): return
        exclude_text = kb.getText()
        if exclude_text:
            exclude_words = [w.strip().lower() for w in exclude_text.split(",") if w.strip()]
        
        _add_to_history(q)
        _lfd_with_exclusions(q, "", exclude_words)
        return
    
    # --- REPEAT LAST (take from history) ---
    if mode == "repeat_last":
        h = _load_history()
        if not h:
            xbmcgui.Dialog().notification("EspaTV", "No hay historial", xbmcgui.NOTIFICATION_WARNING)
            return
        

        filters = [
            ("Sin filtros (Estándar)", "", None),
            ("Modo Cine (Películas)", "movie", None),
            ("Solo +20 min", "", {"longer_than": 20}),
            ("Solo -10 min", "", {"shorter_than": 10}),
            ("Solo HD", "", {"hd": 1}),
            ("Solo Español", "", {"language": "es"}),
            ("Últimas 24h", "", {"created_after": int(time.time()) - 86400}),
            ("Última Semana", "", {"created_after": int(time.time()) - 604800}),
        ]
        
        opts = [f[0] for f in filters]
        sel = xbmcgui.Dialog().select(f"Repetir: '{h[0]}' con filtro...", opts)
        if sel < 0: return
        
        chosen_mode, chosen_ep = filters[sel][1], filters[sel][2]
        _lfd(h[0], "", mode=chosen_mode, extra_params=chosen_ep, nh=True)
        return
    

    title = 'Búsqueda Avanzada'
    kb = xbmc.Keyboard('', title)
    kb.doModal()
    if not kb.isConfirmed(): return
    q = kb.getText()
    if not q: return
    
    _add_to_history(q)
    

    if mode == "long":
        ep = {"longer_than": 20}
    elif mode == "short":
        ep = {"shorter_than": 10}
    elif mode == "recent_24h":
        ep = {"created_after": int(time.time()) - 86400, "sort": "recent"}
    elif mode == "recent_week":
        ep = {"created_after": int(time.time()) - 604800, "sort": "recent"}
    elif mode == "recent_month":
        ep = {"created_after": int(time.time()) - 2592000, "sort": "recent"}
    elif mode == "hd":
        ep = {"hd": 1}
    elif mode == "spanish":
        ep = {"language": "es"}
    
    ep = {} if ep is None else ep
    _lfd(q, "", mode=mode, extra_params=ep, nh=True)

def _lfd_with_exclusions(q, ot, exclude_words):
    """Special version of _lfd that filters out results containing excluded words."""
    cq = _clean_title(q)
    rs = _sr(cq)
    if not rs and cq != q: rs = _sr(q)
    
    if not rs:
        xbmcgui.Dialog().notification("EspaTV", "No se encontraron resultados", xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
        return
    
    # Filter out excluded words
    if exclude_words:
        filtered = []
        for r in rs:
            title_lower = r.get("title", "").lower()
            if not any(ex in title_lower for ex in exclude_words):
                filtered.append(r)
        rs = filtered
    
    if not rs:
        xbmcgui.Dialog().notification("EspaTV", "Todos los resultados fueron excluidos", xbmcgui.NOTIFICATION_WARNING)
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
        return
    
    sr = _gsr(cq, rs, "")[:25]
    
    if not sr:
        xbmcgui.Dialog().notification("EspaTV", "Sin coincidencias relevantes", xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.endOfDirectory(int(sys.argv[1]))
        return
    
    min_dur = core_settings.get_min_duration() * 60
    for sc, v in sr:
        vt = v.get("title", "Video"); vi = v.get("id"); ow = v.get("owner.username", "Unknown"); du = v.get("duration", 0)
        try: du = int(du)
        except (ValueError, TypeError): du = 0
        
        if min_dur > 0 and du < min_dur: continue
        dth = v.get("thumbnail_url") or v.get("thumbnail_360_url") or ot
        
        lb = f"{vt} ({int(sc*100)}% match)"

        li = xbmcgui.ListItem(label=lb); li.setArt({'thumb': dth, 'icon': dth})
        li.setInfo('video', {'title': vt, 'plot': f"Subido por: {ow}\nDuración: {du}s", 'duration': du})
        li.setProperty("IsPlayable", "true")
        cm = []
        cm.append(("Descargar Video", f"RunPlugin({_u(action='download_video', vid=vi, title=vt)})"))
        cm.append(("Abrir en navegador", f"RunPlugin({_u(action='dm_open_browser', url=vi)})"))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vi)))))
        li.addContextMenuItems(cm)

        xbmcplugin.addDirectoryItem(handle=int(sys.argv[1]), url=_u(action="pv", vid=vi), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(int(sys.argv[1]))





_TDT_JSON_URL = "https://www.tdtchannels.com/lists/tv.json"
_TDT_M3U_URL = "https://www.tdtchannels.com/lists/tv.m3u8"
_CUSTOM_IPTV_FILE = "custom_iptv.json"
_tdt_json_cache = {"data": None}

def _load_custom_iptv():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    fp = os.path.join(p, _CUSTOM_IPTV_FILE)
    if not os.path.exists(fp): return []
    try:
        with open(fp, 'r', encoding='utf-8') as f: return json.load(f)
    except (json.JSONDecodeError, IOError): return []

def _save_custom_iptv(lists):
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    fp = os.path.join(p, _CUSTOM_IPTV_FILE)
    try:
        with open(fp, 'w', encoding='utf-8') as f: json.dump(lists, f, ensure_ascii=False)
    except IOError: pass

def _parse_m3u_content(text):
    lines = text.splitlines()
    channels = []
    cur_name, cur_logo, cur_group = "", "", ""
    cur_ua, cur_ref = "", ""
    cur_tvg_id = ""
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            cur_name = line.split(",", 1)[-1].strip() if "," in line else "Canal"
            m = re.search(r'tvg-logo="([^"]*)"', line)
            cur_logo = m.group(1) if m else ""
            m_id = re.search(r'tvg-id="([^"]*)"', line)
            cur_tvg_id = m_id.group(1) if m_id else ""
            m2 = re.search(r'group-title="([^"]*)"', line)
            cur_group = m2.group(1) if m2 else ""
        elif line.upper().startswith("#EXTVLCOPT:HTTP-USER-AGENT="):
            cur_ua = line.split("=", 1)[1].strip()
        elif line.upper().startswith("#EXTVLCOPT:HTTP-REFERRER="):
            cur_ref = line.split("=", 1)[1].strip()
        elif line.upper().startswith("#EXTVLCOPT:HTTP-REFERER="):
            cur_ref = line.split("=", 1)[1].strip()
        elif line and not line.startswith("#"):
            entry = {"name": cur_name or "Canal", "url": line, "logo": cur_logo, "group": cur_group}
            if cur_tvg_id: entry["tvg_id"] = cur_tvg_id
            if cur_ua: entry["ua"] = cur_ua
            if cur_ref: entry["ref"] = cur_ref
            channels.append(entry)
            cur_name, cur_logo, cur_group = "", "", ""
            cur_ua, cur_ref = "", ""
            cur_tvg_id = ""
    return channels

def _render_m3u_channels(channels, source_label=""):
    h = int(sys.argv[1])
    xbmcplugin.setContent(h, 'videos')
    epg = _fetch_epg() if any(ch.get("tvg_id") for ch in channels) else {}
    for ch in channels:
        label = ch["name"]
        tvg_id = ch.get("tvg_id", "")
        epg_info = epg.get(tvg_id) if tvg_id else None
        if epg_info and epg_info.get("title"):
            epg_label = epg_info["title"]
            t1 = _format_epg_time(epg_info.get("start", ""))
            t2 = _format_epg_time(epg_info.get("stop", ""))
            time_str = "  ({0}-{1})".format(t1, t2) if t1 and t2 else ""
            label += "  [COLOR skyblue]Ahora: {0}{1}[/COLOR]".format(epg_label, time_str)
        if ch["group"]:
            label = "[COLOR gray][{0}][/COLOR] {1}".format(ch["group"], label)
        li = xbmcgui.ListItem(label=label)
        thumb = ch["logo"] or "DefaultAddonPVRClient.png"
        li.setArt({'icon': thumb, 'thumb': thumb})
        plot = "Canal: {0}".format(ch["name"])
        if epg_info and epg_info.get("title"):
            plot += "\n\nAhora: {0}".format(epg_info["title"])
            if epg_info.get("desc"): plot += "\n{0}".format(epg_info["desc"])
        if ch["group"]: plot += "\nGrupo: {0}".format(ch["group"])
        if source_label: plot += "\n\nFuente: {0}".format(source_label)
        li.setInfo('video', {'title': ch["name"], 'plot': plot})
        li.setProperty("IsPlayable", "true")
        cm = [
            ("Añadir a Favoritos", "RunPlugin({0})".format(
                _u(action="add_favorite", title=ch["name"], fav_url=ch["url"], icon=thumb, platform="tdt", fav_action="play_tdt", params=json.dumps({}))
            )),
            ("[COLOR red]Grabar este canal[/COLOR]", "RunPlugin({0})".format(
                _u(action="record_stream", url=ch["url"], name=ch["name"])
            ))
        ]
        li.addContextMenuItems(cm)
        play_args = {"action": "play_tdt", "url": ch["url"], "title": ch["name"]}
        if ch.get("ua"): play_args["ua"] = ch["ua"]
        if ch.get("ref"): play_args["ref"] = ch["ref"]
        xbmcplugin.addDirectoryItem(handle=h, url=_u(**play_args), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())

# --- EPG (Guía de programación) ---
_EPG_URL = "https://www.tdtchannels.com/epg/TV.xml.gz"
_epg_cache = {"data": None, "ts": 0}

def _fetch_epg():
    now = time.time()
    if _epg_cache["data"] and (now - _epg_cache["ts"]) < 7200:
        return _epg_cache["data"]
    try:
        import gzip
        import io
        import xml.etree.ElementTree as ET
        from datetime import datetime
        import hashlib
        import os
        
        ttl = core_settings.get_iptv_cache_ttl()
        if ttl <= 0: ttl = 7200 # La EPG siempre tiene caché mínima de 2h
        
        c_file = ""
        programmes = None
        if core_settings.is_iptv_cache_active():
            c_dir = core_settings.get_iptv_cache_dir()
            c_key = hashlib.md5(_EPG_URL.encode('utf-8')).hexdigest()
            c_file = os.path.join(c_dir, 'epg_' + c_key + '.json')
            if os.path.exists(c_file):
                try:
                    with open(c_file, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                    if (now - meta.get('ts', 0)) < ttl:
                        programmes = meta.get('data', {})
                except Exception:
                    pass

        if not programmes:
            r = requests.get(_EPG_URL, timeout=20)
            if r.status_code != 200:
                return {}
            xml_data = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read()
            root = ET.fromstring(xml_data)
            now_dt = datetime.utcnow()
            programmes = {}
            for prog in root.findall("programme"):
                ch_id = prog.get("channel", "")
                start_s = prog.get("start", "")
                stop_s = prog.get("stop", "")
                if not ch_id or not start_s or not stop_s:
                    continue
                try:
                    start_dt = datetime.strptime(start_s[:14], "%Y%m%d%H%M%S")
                    stop_dt = datetime.strptime(stop_s[:14], "%Y%m%d%H%M%S")
                except ValueError:
                    continue
                if start_dt <= now_dt < stop_dt:
                    title_el = prog.find("title")
                    title_text = title_el.text if title_el is not None and title_el.text else ""
                    desc_el = prog.find("desc")
                    desc_text = desc_el.text if desc_el is not None and desc_el.text else ""
                    programmes[ch_id] = {"title": title_text, "desc": desc_text,
                                         "start": start_s[:14], "stop": stop_s[:14]}
            
            if core_settings.is_iptv_cache_active() and c_file:
                try:
                    with open(c_file, 'w', encoding='utf-8') as f:
                        json.dump({'ts': now, 'data': programmes}, f, ensure_ascii=False)
                except Exception:
                    pass

        _epg_cache["data"] = programmes
        _epg_cache["ts"] = now
        return programmes
    except Exception as e:
        _log_error("EPG fetch error: {0}".format(e))
        return {}

def _format_epg_time(t):
    if len(t) >= 12:
        try:
            from datetime import datetime, timedelta
            utc_dt = datetime.strptime(t[:14] if len(t) >= 14 else t[:12] + "00", "%Y%m%d%H%M%S")
            local_offset = -time.timezone if not time.daylight else -time.altzone
            local_dt = utc_dt + timedelta(seconds=local_offset)
            return "{0:02d}:{1:02d}".format(local_dt.hour, local_dt.minute)
        except Exception:
            return "{0}:{1}".format(t[8:10], t[10:12])
    return ""

# --- SLYGUY ADDON INSTALLER ---
_SLYGUY_REPO_ID = "repository.slyguy"
_SLYGUY_REPO_ZIP = "https://k.slyguy.xyz/repository.slyguy.zip"

def _slyguy_install(addon_id, addon_name):
    """Flujo inteligente: abrir si instalado, instalar si repo disponible, o guiar."""
    try:
        xbmcaddon.Addon(addon_id)
        xbmcgui.Dialog().notification("EspaTV", "Abriendo {0}...".format(addon_name), xbmcgui.NOTIFICATION_INFO, 2000)
        xbmc.executebuiltin("RunAddon({0})".format(addon_id))
        return
    except Exception:
        pass
    try:
        xbmcaddon.Addon(_SLYGUY_REPO_ID)
        if xbmcgui.Dialog().yesno("EspaTV", "{0} no está instalado pero el repositorio SlyGuy sí.\n\n¿Instalar {0} ahora?".format(addon_name)):
            xbmc.executebuiltin("InstallAddon({0})".format(addon_id))
            xbmcgui.Dialog().ok("EspaTV", "Se ha solicitado la instalación de {0}.\n\nSi Kodi te pide confirmación, acepta.\nDespués vuelve y pulsa de nuevo para abrirlo.".format(addon_name))
        return
    except Exception:
        pass
    opts = ["Instalar repositorio automáticamente", "Ver instrucciones manuales"]
    sel = xbmcgui.Dialog().select("Se necesita el repo SlyGuy para {0}".format(addon_name), opts)
    if sel == 0:
        _auto_install_slyguy_repo(addon_id, addon_name)
    elif sel == 1:
        _show_slyguy_instructions(addon_name)

def _auto_install_slyguy_repo(addon_id, addon_name):
    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Descargando repositorio SlyGuy...")
    zip_path = None
    try:
        dp.update(10, "Descargando repositorio...")
        r = requests.get(_SLYGUY_REPO_ZIP, timeout=30)
        if r.status_code != 200: raise Exception("Error HTTP {0}".format(r.status_code))
        if len(r.content) < 4 or r.content[:2] != b'PK':
            raise Exception("El archivo descargado no es un ZIP válido")
        if len(r.content) > 50 * 1024 * 1024:
            raise Exception("Tamaño sospechoso: {0}KB".format(len(r.content) // 1024))
        zip_path = os.path.join(xbmcvfs.translatePath("special://temp/"), "repository.slyguy.zip")
        with open(zip_path, "wb") as f: f.write(r.content)
        dp.update(50, "Extrayendo repositorio...")
        addons_dir = xbmcvfs.translatePath("special://home/addons/")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for entry in zf.namelist():
                resolved = os.path.realpath(os.path.join(addons_dir, entry))
                if not resolved.startswith(os.path.realpath(addons_dir)):
                    raise Exception("ZIP contiene ruta sospechosa: {0}".format(entry))
            zf.extractall(addons_dir)
        dp.update(80, "Actualizando addons locales...")
        xbmc.executebuiltin("UpdateLocalAddons()")
        xbmc.sleep(2000)
        xbmc.executebuiltin("EnableAddon({0})".format(_SLYGUY_REPO_ID))
        xbmc.sleep(1000)
        dp.update(90, "Instalando {0}...".format(addon_name))
        xbmc.executebuiltin("InstallAddon({0})".format(addon_id))
        dp.close()
        xbmcgui.Dialog().notification("EspaTV", "Repo SlyGuy instalado. Instalando {0}...".format(addon_name), xbmcgui.NOTIFICATION_INFO, 5000)
    except Exception as e:
        dp.close()
        _log_error("Error instalando repo SlyGuy: {0}".format(e))
        choice = xbmcgui.Dialog().yesno("Error de instalación",
            "No se pudo instalar automáticamente.\n\nError: {0}\n\nPuede que necesites activar 'Orígenes desconocidos' en Ajustes.\n¿Abrir Ajustes del Sistema?".format(e))
        if choice: xbmc.executebuiltin("ActivateWindow(systemsettings)")
    finally:
        if zip_path and os.path.exists(zip_path):
            try: os.remove(zip_path)
            except Exception: pass

def _show_slyguy_instructions(addon_name):
    t = (
        "[B]Cómo instalar {0}[/B]\n\n"
        "1. Ve a [B]Ajustes → Sistema → Addons[/B]\n"
        "   Activa [B]Orígenes desconocidos[/B]\n\n"
        "2. Ve a [B]Ajustes → Administrador de archivos[/B]\n"
        "   Añade fuente: [B]https://k.slyguy.xyz[/B]\n"
        "   Nombre: [B]slyguy[/B]\n\n"
        "3. Ve a [B]Addons → Instalar desde ZIP[/B]\n"
        "   Selecciona [B]slyguy → repository.slyguy.zip[/B]\n\n"
        "4. Ve a [B]Addons → Instalar desde repositorio[/B]\n"
        "   SlyGuy → Video addons → [B]{0}[/B]\n\n"
        "Tras instalarlo, vuelve a TV en Directo y pulsa de nuevo."
    ).format(addon_name)
    xbmcgui.Dialog().textviewer("Instalar {0}".format(addon_name), t)

def _open_atresdaily():
    """Abre AtresDaily si instalado, o muestra instrucciones de descarga."""
    try:
        xbmcaddon.Addon("plugin.video.atresdaily")
        xbmc.executebuiltin("RunAddon(plugin.video.atresdaily)")
        return
    except Exception:
        pass
    choice = xbmcgui.Dialog().yesno("EspaTV",
        "AtresDaily no está instalado.\n\n"
        "Es un addon especializado centrado en Atresplayer.\n"
        "Disponible en:\nhttps://github.com/fullstackcurso\n\n"
        "¿Ver instrucciones de instalación?")
    if choice:
        xbmcgui.Dialog().textviewer("Instalar AtresDaily",
            "[B]Cómo instalar AtresDaily[/B]\n\n"
            "1. Descarga el ZIP desde:\n"
            "   [B]https://github.com/fullstackcurso[/B]\n\n"
            "2. En Kodi, ve a:\n"
            "   [B]Addons → Instalar desde ZIP[/B]\n\n"
            "3. Selecciona el archivo ZIP descargado\n\n"
            "4. Vuelve al Catálogo de EspaTV y pulsa AtresDaily")

def live_tv_menu():
    h = int(sys.argv[1])
    li = xbmcgui.ListItem(label="[B]TDT Channels España[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Lista completa de canales TDT españoles.\nFuente: tdtchannels.com\n\nIncluye nacionales, autonómicos, temáticos y más."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="tdt_channels_json"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="TDT Channels España (M3U)")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Lista M3U alternativa."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="tdt_channels"), listitem=li, isFolder=True)


    li = xbmcgui.ListItem(label="[B][COLOR skyblue]Radio en Directo[/COLOR][/B]")
    li.setArt({'icon': 'DefaultMusicSongs.png'})
    li.setInfo('video', {'plot': "Emisoras de radio españolas en directo.\nPopulares, musicales, deportivas, autonómicas y más.\nFuente: tdtchannels.com"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="radio_menu"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B][COLOR red]RTVE Play[/COLOR][/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales de RTVE en directo (La 1, La 2, 24h, Teledeporte, Clan).\nCatálogo de programas, series y documentales de RTVE Play."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="rtve_menu"), listitem=li, isFolder=True)
    
    # Mostrar carpeta de grabaciones si existen archivos
    rec_path = xbmcaddon.Addon().getSetting("record_path")
    if rec_path and xbmcvfs.exists(rec_path):
        try:
            dirs, files = xbmcvfs.listdir(rec_path)
            if any(f.endswith('.ts') or f.endswith('.mp4') for f in files):
                li = xbmcgui.ListItem(label="[B][COLOR gold]Mis Grabaciones[/COLOR][/B]")
                li.setArt({'icon': 'DefaultVideo.png'})
                li.setInfo('video', {'plot': "Archivo de grabaciones de TV en directo.\nProgramas descargados localmente."})
                xbmcplugin.addDirectoryItem(handle=h, url=_u(action="my_recordings_menu"), listitem=li, isFolder=True)
        except Exception:
            pass

    # --- Listas IPTV gratuitas y legales ---
    li = xbmcgui.ListItem(label="[B]IPTV-org España[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Directorio comunitario con 300+ canales españoles verificados.\nIncluye nacionales, autonómicos, temáticos, deportes, música y más.\nFuente: github.com/iptv-org/iptv"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://iptv-org.github.io/iptv/countries/es.m3u"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B]Cine y Películas FAST[/B]")
    li.setArt({'icon': 'DefaultMovies.png'})
    li.setInfo('video', {'plot': "Canales FAST gratuitos de cine y películas.\nIncluye Rakuten TV, Pluto TV, Atrescine, Cine Western y más.\nContenido con publicidad."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://iptv-org.github.io/iptv/categories/movies.m3u"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B]Canales Internacionales en Español[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales internacionales disponibles en España.\nFrance 24, DW, euronews, RT y más en español.\nFuente: iptv-org"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://iptv-org.github.io/iptv/languages/spa.m3u"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B]Free-TV IPTV[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Lista IPTV gratuita con canales de todo el mundo.\nIncluye canales en abierto verificados.\nFuente: github.com/Free-TV/IPTV"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="freetv_menu"), listitem=li, isFolder=True)

    # --- Pluto TV y Samsung TV Plus (SlyGuy) ---
    li = xbmcgui.ListItem(label="[B]Pluto TV[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "TV gratuita con canales temáticos (cine, series, noticias...).\nRequiere addon SlyGuy (se instala automáticamente)."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="slyguy_install", addon_id="slyguy.pluto.tv.provider", title="Pluto TV"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[B]Samsung TV Plus[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales de TV gratuitos de Samsung.\nRequiere addon SlyGuy (se instala automáticamente)."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="slyguy_install", addon_id="slyguy.samsung.tv.plus", title="Samsung TV Plus"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[B]Roku Channel[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales de TV gratuitos de Roku.\nPelículas, series y TV en vivo sin suscripción.\nRequiere addon SlyGuy (se instala automáticamente)."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="slyguy_install", addon_id="slyguy.roku", title="Roku Channel"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[B]Plex Live TV[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Decenas de canales lineales gratuitos de cine, noticias y entretenimiento.\nRequiere el módulo de SlyGuy (se instala automáticamente)."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="slyguy_install", addon_id="slyguy.plex.live", title="Plex Live TV"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[B]Rakuten TV[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Más de 90 canales gratuitos con películas, series y documentales (FAST).\nFuente: iptv-org"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://raw.githubusercontent.com/iptv-org/iptv/master/streams/es_rakuten.m3u"), listitem=li, isFolder=True)

    import pvr_manager
    if pvr_manager.is_pvr_installed():
        li_p = xbmcgui.ListItem(label="Abrir Guía de Televisión Nativa (PVR)")
        li_p.setArt({'icon': 'DefaultAddonPVRClient.png'})
        li_p.setInfo('video', {'plot': "Salta instantáneamente a la sección 'TV' de Kodi para ver la parrilla de horarios interactiva de TDT."})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="open_pvr"), listitem=li_p, isFolder=False)

    # --- Listas IPTV personalizadas ---
    custom_lists = _load_custom_iptv()
    for lst in custom_lists:
        name = lst.get("name", "Lista")
        url = lst.get("url", "")
        li = xbmcgui.ListItem(label="[B]{0}[/B]".format(name))
        li.setArt({'icon': 'DefaultAddonPVRClient.png'})
        li.setInfo('video', {'plot': "Lista personalizada\nURL: {0}".format(url)})
        cm = [("Eliminar esta lista", "RunPlugin({0})".format(_u(action="delete_custom_iptv", url=url)))]
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url=url), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="Añadir IPTV M3U")
    li.setArt({'icon': 'DefaultAddSource.png'})
    li.setInfo('video', {'plot': "Pega la URL de una nueva fuente de canales (M3U/M3U8) para añadirla."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="add_custom_iptv"), listitem=li, isFolder=False)
    
    xbmcplugin.endOfDirectory(h)

def tdt_channels_json():
    h = int(sys.argv[1])
    dp = xbmcgui.DialogProgress()
    dp.create("TDT Channels", "Descargando lista actualizada...")
    try:
        dp.update(30, "Conectando con tdtchannels.com...")
        content = _cached_http_get(_TDT_JSON_URL, timeout=15)
        dp.update(70, "Procesando canales...")
        data = json.loads(content)
        dp.close()
        countries = data.get("countries", [])
        if not countries:
            xbmcgui.Dialog().ok("TDT Channels", "No se encontraron datos.")
            xbmcplugin.endOfDirectory(h); return
        _tdt_json_cache["data"] = countries[0].get("ambits", [])
        
        # Boton PVR rapido
        import pvr_manager
        if pvr_manager.is_pvr_installed():
            li_pvr = xbmcgui.ListItem(label="Abrir la Parrilla Completa (PVR)")
            li_pvr.setArt({'icon': 'DefaultAddonPVRClient.png'})
            li_pvr.setInfo('video', {'plot': "Salta automáticamente a la cuadrícula de televisión de Kodi."})
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="open_pvr"), listitem=li_pvr, isFolder=False)
        else:
            li_pvr = xbmcgui.ListItem(label="Ver en Parrilla Nativa Completa (PVR)")
            li_pvr.setArt({'icon': 'DefaultAddonPVRClient.png'})
            li_pvr.setInfo('video', {'plot': "Auto-configura Kodi para mostrar estos canales en una cuadrícula horaria visual."})
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="setup_pvr"), listitem=li_pvr, isFolder=False)

        for ambit in _tdt_json_cache["data"]:
            name = ambit.get("name", "Sin nombre")
            num = len(ambit.get("channels", []))
            li = xbmcgui.ListItem(label="[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(name, num))
            li.setArt({'icon': 'DefaultAddonPVRClient.png'})
            li.setInfo('video', {'plot': "Categoría: {0}\nCanales: {1}".format(name, num)})
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="tdt_json_ambit", url=name), listitem=li, isFolder=True)
        xbmcplugin.endOfDirectory(h)
    except Exception as e:
        dp.close()
        _log_error("Error TDT JSON: {0}".format(e))
        xbmcgui.Dialog().ok("TDT Channels", "No se pudo cargar:\n{0}".format(e))
        try: xbmcplugin.endOfDirectory(h)
        except Exception: pass

def tdt_json_ambit(ambit_name):
    h = int(sys.argv[1])
    ambits = _tdt_json_cache.get("data")
    if not ambits:
        try:
            content = _cached_http_get(_TDT_JSON_URL, timeout=15)
            data = json.loads(content)
            countries = data.get("countries", [])
            if countries:
                ambits = countries[0].get("ambits", [])
                _tdt_json_cache["data"] = ambits
        except Exception:
            xbmcgui.Dialog().ok("TDT Channels", "No se pudo cargar la lista.")
            xbmcplugin.endOfDirectory(h); return
    if not ambits:
        xbmcplugin.endOfDirectory(h); return
    target = None
    for a in ambits:
        if a.get("name") == ambit_name:
            target = a; break
    if not target:
        xbmcplugin.endOfDirectory(h); return
    channels = []
    for ch in target.get("channels", []):
        name = ch.get("name", "Canal")
        opts = ch.get("options", [])
        logo = ch.get("logo", "")
        for opt in opts:
            fmt = opt.get("format", "")
            url = opt.get("url", "")
            if url and fmt.lower() in ("m3u8", "mp4", ""):
                channels.append({"name": name, "url": url, "logo": logo, "group": ambit_name})
                break
    if channels:
        _render_m3u_channels(channels, "TDT Channels - {0}".format(ambit_name))
    else:
        xbmcgui.Dialog().ok("TDT Channels", "No se encontraron streams para {0}.".format(ambit_name))
        xbmcplugin.endOfDirectory(h)

def tdt_channels():
    h = int(sys.argv[1])
    dp = xbmcgui.DialogProgress()
    dp.create("TDT Channels", "Descargando lista M3U...")
    try:
        dp.update(30, "Conectando...")
        content = _cached_http_get(_TDT_M3U_URL, timeout=15)
        dp.update(70, "Procesando canales...")
        channels = _parse_m3u_content(content)
        dp.close()
        if not channels:
            xbmcgui.Dialog().ok("TDT Channels", "No se encontraron canales.")
            xbmcplugin.endOfDirectory(h); return
        _render_m3u_channels(channels, "TDT Channels España")
    except Exception as e:
        dp.close()
        _log_error("Error TDT M3U: {0}".format(e))
        xbmcgui.Dialog().ok("TDT Channels", "No se pudo cargar:\n{0}".format(e))
        try: xbmcplugin.endOfDirectory(h)
        except Exception: pass

def play_tdt(url, title="", ua="", ref=""):
    h = int(sys.argv[1])
    try:
        play_url = url
        is_m3u8 = '.m3u8' in url.lower()

        # Comprobar si el usuario quiere usar InputStream Adaptive
        use_ia = False
        if is_m3u8:
            import xbmcaddon
            if xbmcaddon.Addon().getSetting('use_inputstream') == 'true':
                # Verificar que el addon esta realmente instalado
                try:
                    xbmcaddon.Addon('inputstream.adaptive')
                    use_ia = True
                except Exception:
                    xbmc.log("[EspaTV] InputStream Adaptive activado en ajustes pero no instalado. Usando reproductor normal.", xbmc.LOGWARNING)

        # Modo Anti-Errores: inyectar UA de navegador si no hay uno propio
        import xbmcaddon as _xa
        anti_err = _xa.Addon().getSetting('iptv_anti_errors') == 'true'
        if anti_err and not ua:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        # Build URL with headers pipe for Kodi player if needed
        # InputStream Adaptive NO soporta el formato pipe (|User-Agent=...)
        if (ua or ref) and not use_ia:
            parts = []
            if ua: parts.append("User-Agent={0}".format(requests.utils.quote(ua)))
            if ref: parts.append("Referer={0}".format(requests.utils.quote(ref)))
            play_url = "{0}|{1}".format(url, "&".join(parts))

        li = xbmcgui.ListItem(path=play_url)
        if title: li.setInfo('video', {'title': title})
        
        if is_m3u8:
            li.setMimeType('application/x-mpegURL')

        if use_ia:
            xbmc.log("[EspaTV] Reproduciendo {0} con InputStream Adaptive".format(title), xbmc.LOGINFO)
            # Kodi 19+ / Kodi 18
            li.setProperty('inputstream', 'inputstream.adaptive')
            li.setProperty('inputstreamaddon', 'inputstream.adaptive')
            li.setProperty('inputstream.adaptive.manifest_type', 'hls')
            li.setProperty('inputstream.adaptive.manifest_update_parameter', 'full')
            
            # Pasar headers a InputStream Adaptive por su via nativa
            hdr_parts = []
            if ua:
                hdr_parts.append("User-Agent={0}".format(ua))
            if ref: 
                hdr_parts.append("Referer={0}".format(ref))
            if hdr_parts:
                li.setProperty('inputstream.adaptive.stream_headers', "&".join(hdr_parts))

        li.setContentLookup(False)
        xbmcplugin.setResolvedUrl(h, True, li)
    except Exception as e:
        _log_error("Error TDT play: {0}".format(e))
        xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())

def add_custom_iptv():
    kb = xbmc.Keyboard('', 'URL de la lista M3U/M3U8')
    kb.doModal()
    if not kb.isConfirmed(): return
    url = kb.getText().strip()
    if not url: return
    if not url.startswith(('http://', 'https://')):
        xbmcgui.Dialog().ok("EspaTV", "La URL debe empezar por http:// o https://"); return
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            xbmcgui.Dialog().ok("EspaTV", "No se pudo acceder a la URL.\nError HTTP {0}".format(r.status_code)); return
        channels = _parse_m3u_content(r.text)
        if not channels:
            xbmcgui.Dialog().ok("EspaTV", "La URL no contiene canales válidos en formato M3U."); return
    except Exception as e:
        xbmcgui.Dialog().ok("EspaTV", "Error al verificar la URL:\n{0}".format(e)); return
    kb2 = xbmc.Keyboard('', 'Nombre para la lista ({0} canales encontrados)'.format(len(channels)))
    kb2.doModal()
    if not kb2.isConfirmed() or not kb2.getText().strip(): return
    name = kb2.getText().strip()
    lists = _load_custom_iptv()
    if any(l.get("url") == url for l in lists):
        xbmcgui.Dialog().notification("EspaTV", "Esta lista ya está añadida", xbmcgui.NOTIFICATION_WARNING); return
    lists.append({"name": name, "url": url})
    _save_custom_iptv(lists)
    xbmcgui.Dialog().notification("EspaTV", "Lista '{0}' añadida".format(name), xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def view_custom_iptv(url):
    h = int(sys.argv[1])
    dp = xbmcgui.DialogProgress()
    dp.create("Lista IPTV", "Cargando canales...")
    try:
        dp.update(30, "Descargando...")
        content = _cached_http_get(url, timeout=15)
        dp.update(70, "Procesando canales...")
        channels = _parse_m3u_content(content)
        dp.close()
        if not channels:
            xbmcgui.Dialog().ok("Lista IPTV", "No se encontraron canales.")
            xbmcplugin.endOfDirectory(h); return
        _render_m3u_channels(channels, "Lista personalizada")
    except Exception as e:
        dp.close()
        _log_error("Error lista IPTV: {0}".format(e))
        xbmcgui.Dialog().ok("Lista IPTV", "No se pudo cargar:\n{0}".format(e))
        try: xbmcplugin.endOfDirectory(h)
        except Exception: pass

def freetv_menu():
    h = int(sys.argv[1])
    li = xbmcgui.ListItem(label="[B]Free-TV IPTV[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Lista IPTV gratuita con canales de todo el mundo.\nIncluye canales en abierto verificados.\nFuente: github.com/Free-TV/IPTV"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B]España (iptv-org)[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales españoles.\nFuente: iptv-org/iptv"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://raw.githubusercontent.com/iptv-org/iptv/master/streams/es.m3u"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B]España Regional[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales regionales españoles.\nFuente: iptv-org/iptv"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://raw.githubusercontent.com/iptv-org/iptv/master/streams/es_45.95.78.m3u"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B]Pluto TV (iptv-org)[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales Pluto TV España.\nFuente: iptv-org/iptv"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://raw.githubusercontent.com/iptv-org/iptv/master/streams/es_pluto.m3u"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[B]Samsung TV Plus (iptv-org)[/B]")
    li.setArt({'icon': 'DefaultAddonPVRClient.png'})
    li.setInfo('video', {'plot': "Canales Samsung TV Plus España.\nFuente: iptv-org/iptv"})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_custom_iptv", url="https://raw.githubusercontent.com/iptv-org/iptv/master/streams/es_samsung.m3u"), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(h)

def delete_custom_iptv(url):
    lists = _load_custom_iptv()
    name = next((l.get("name", "Lista") for l in lists if l.get("url") == url), "Lista")
    if xbmcgui.Dialog().yesno("EspaTV", "¿Eliminar la lista '{0}'?".format(name)):
        new_lists = [l for l in lists if l.get("url") != url]
        _save_custom_iptv(new_lists)
        xbmcgui.Dialog().notification("EspaTV", "Lista eliminada", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("Container.Refresh")


# --- YOUTUBE EN DIRECTO ---
# --- FELICIDAD VERANIEGA (YouTube) ---
from youtube_data import FELICIDAD_VERANIEGA_YT as _FELICIDAD_VERANIEGA_YT
from youtube_data import COCINA_ESPANOLA_YT as _COCINA_ESPANOLA_YT
from youtube_data import WEBCAMS_ES as _WEBCAMS_ES

_WATCHED_YT_FILE = os.path.join(core_settings._PROFILE_PATH, 'watched_yt.json')


def _load_watched_yt():
    if not os.path.exists(_WATCHED_YT_FILE):
        return set()
    try:
        with open(_WATCHED_YT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _save_watched_yt(ids):
    try:
        with open(_WATCHED_YT_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(ids), f)
    except Exception:
        pass


def _toggle_watched_yt(yt_id):
    if not yt_id:
        return
    ids = _load_watched_yt()
    if yt_id in ids:
        ids.discard(yt_id)
        xbmcgui.Dialog().notification(
            "EspaTV", "Desmarcado como visto",
            xbmcgui.NOTIFICATION_INFO, 1500,
        )
    else:
        ids.add(yt_id)
        xbmcgui.Dialog().notification(
            "EspaTV", "Marcado como visto",
            xbmcgui.NOTIFICATION_INFO, 1500,
        )
    _save_watched_yt(ids)
    xbmc.executebuiltin("Container.Refresh")


def _yt_queue_add(yt_id, name=""):
    if not yt_id:
        return
    if not _check_youtube_addon():
        _youtube_install_prompt()
        return
    url = "plugin://plugin.video.youtube/play/?video_id={0}".format(yt_id)
    li = xbmcgui.ListItem(name or yt_id)
    li.setArt({"thumb": "https://i.ytimg.com/vi/{0}/hqdefault.jpg".format(yt_id)})
    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    if not xbmc.Player().isPlaying():
        playlist.clear()
        playlist.add(url, li)
        xbmc.Player().play(playlist)
        xbmcgui.Dialog().notification(
            "EspaTV", "Reproduciendo — pulsa Atrás para añadir más",
            xbmcgui.NOTIFICATION_INFO, 3000,
        )
    else:
        playlist.add(url, li)
        xbmcgui.Dialog().notification(
            "EspaTV",
            "Añadido a la cola ({0} vídeos)".format(playlist.size()),
            xbmcgui.NOTIFICATION_INFO, 2000,
        )

def _felicidad_menu():
    h = int(sys.argv[1])
    if not _check_youtube_addon():
        li = xbmcgui.ListItem(label="[COLOR red][B]Instalar addon YouTube (necesario)[/B][/COLOR]")
        li.setArt({'icon': 'DefaultAddonHelper.png'})
        li.setInfo('video', {'plot': "El addon YouTube de Kodi es necesario para ver este contenido.\nPulsa aqui para ver las instrucciones."})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="youtube_install"), listitem=li, isFolder=False)
    for cat_name, channels in _FELICIDAD_VERANIEGA_YT.items():
        li = xbmcgui.ListItem(label="[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(cat_name, len(channels)))
        li.setArt({'icon': 'DefaultMusicVideos.png'})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="felicidad_list", cat=cat_name), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)

def _felicidad_list(cat_name):
    h = int(sys.argv[1])
    channels = _FELICIDAD_VERANIEGA_YT.get(cat_name, [])
    if not channels:
        xbmcplugin.endOfDirectory(h); return
    watched = _load_watched_yt()
    for ch in channels:
        name = ch.get("name", "Canal")
        ctype = ch.get("type", "channel")
        desc = ch.get("desc", "")
        yt_id = ch.get("yt_id", "")
        burl = ch.get("url", "")
        is_watched = ctype == "video" and yt_id in watched
        if ctype == "browser":
            label = "[COLOR orange]{0}[/COLOR]".format(name)
            plot = "{0}\nSe abre en el navegador.".format(desc)
        elif ctype == "video":
            if is_watched:
                label = "[COLOR gray](Visto) {0}[/COLOR]".format(name)
            else:
                label = "[COLOR lime]{0}[/COLOR]".format(name)
            plot = "{0}\nVideo individual.".format(desc)
        elif ctype == "playlist":
            label = "[COLOR skyblue]{0}[/COLOR]".format(name)
            plot = "{0}\nPlaylist de YouTube.".format(desc)
        else:
            label = "[B]{0}[/B]".format(name)
            plot = "{0}\nCanal de YouTube.".format(desc)
        li = xbmcgui.ListItem(label=label)
        art = {'icon': 'DefaultMusicVideos.png'}
        if ctype == "video" and yt_id:
            thumb = "https://i.ytimg.com/vi/{0}/hqdefault.jpg".format(yt_id)
            art['thumb'] = thumb
            art['icon'] = thumb
        li.setArt(art)
        li.setInfo('video', {'title': name, 'plot': plot})
        fav_params = json.dumps({"yt_id": yt_id, "name": name, "ctype": ctype, "burl": burl})
        cm = []
        if ctype == "video" and yt_id:
            if is_watched:
                cm.append(("Desmarcar como visto", "RunPlugin({0})".format(
                    _u(action='yt_toggle_watched', yt_id=yt_id))))
            else:
                cm.append(("Marcar como visto", "RunPlugin({0})".format(
                    _u(action='yt_toggle_watched', yt_id=yt_id))))
            cm.append(("Añadir a cola de reproducción", "RunPlugin({0})".format(
                _u(action='yt_queue_add', yt_id=yt_id, name=name))))
        cm.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(
            _u(action='add_favorite', title=name, fav_url='yt_{0}'.format(yt_id),
               icon='DefaultMusicVideos.png', platform='youtube',
               fav_action='felicidad_play', params=fav_params))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="felicidad_play", yt_id=yt_id, name=name, ctype=ctype, burl=burl), listitem=li, isFolder=(ctype == "video"))
    xbmcplugin.endOfDirectory(h)

def _felicidad_open_browser(url, name=""):
    url = url.replace('"', '').replace("'", '') if url else ""
    try:
        if xbmc.getCondVisibility("System.Platform.Android"):
            xbmc.executebuiltin('StartAndroidActivity("","android.intent.action.VIEW","text/html","{0}")'.format(url))
        else:
            import webbrowser
            webbrowser.open(url)
        xbmcgui.Dialog().notification("EspaTV", "Abriendo en el navegador...", xbmcgui.NOTIFICATION_INFO, 2000)
    except Exception:
        xbmcgui.Dialog().ok("Abrir en navegador", "No se pudo abrir automaticamente.\n\nAbre esta URL:\n{0}".format(url))

def _felicidad_open_yt_app(url, name=""):
    url = url.replace('"', '').replace("'", '') if url else ""
    try:
        xbmc.executebuiltin('StartAndroidActivity("com.google.android.youtube","android.intent.action.VIEW","","{0}")'.format(url))
        xbmcgui.Dialog().notification("EspaTV", "Abriendo app YouTube...", xbmcgui.NOTIFICATION_INFO, 2000)
    except Exception:
        xbmcgui.Dialog().ok("App YouTube", "No se pudo abrir la app YouTube.\nAsegurate de que esta instalada.")

# Buscar en YouTube

_YT_SP_DURATION = [
    ("Cualquier duración", ""),
    ("Cortos (< 4 min)",   "EgIYAQ=="),
    ("Medios (4-20 min)",  "EgIYAw=="),
    ("Largos (> 20 min)",  "EgIYAg=="),
]
_YT_SP_DATE = [
    ("Cualquier fecha", ""),
    ("Última hora",  "EgIIAQ=="),
    ("Hoy",             "EgIIAg=="),
    ("Esta semana",     "EgIIAw=="),
    ("Este mes",        "EgIIBA=="),
    ("Este año",   "EgIIBQ=="),
]
_YT_SP_SORT = [
    ("Relevancia",        ""),
    ("Fecha de subida",   "CAISAhAB"),
    ("Número de visitas", "CAMSAhAB"),
    ("Valoración",  "CAESAhAB"),
]


def _yt_search():
    opts = ["Búsqueda normal", "Buscar con filtros", "Buscar canal de YouTube", "Escribir desde movil/PC"]
    sel = xbmcgui.Dialog().select("Búsqueda en YouTube", opts)
    if sel < 0:
        return

    if sel == 2:
        _yt_channel_search()
        return

    if sel == 3:
        import url_remote
        q = url_remote.receive_text("Buscar en YouTube")
        if not q:
            return
    else:
        kb = xbmc.Keyboard('', 'Buscar en YouTube')
        kb.doModal()
        if not kb.isConfirmed():
            return
        q = kb.getText().strip()
        if not q:
            return

    sp = ""
    if sel == 1:
        dur_labels = [x[0] for x in _YT_SP_DURATION]
        d = xbmcgui.Dialog().select("Filtrar por duración", dur_labels)
        if d > 0:
            sp = _YT_SP_DURATION[d][1]

        date_labels = [x[0] for x in _YT_SP_DATE]
        dt = xbmcgui.Dialog().select("Filtrar por fecha", date_labels)
        if dt > 0:
            sp = _YT_SP_DATE[dt][1]

        sort_labels = [x[0] for x in _YT_SP_SORT]
        s = xbmcgui.Dialog().select("Ordenar por", sort_labels)
        if s > 0:
            sp = _YT_SP_SORT[s][1]

    _add_yt_to_history(q)
    _yt_search_results(q, sp)


def _yt_channel_search():
    kb = xbmc.Keyboard('', 'Nombre del canal de YouTube')
    kb.doModal()
    if not kb.isConfirmed():
        return
    q = kb.getText().strip()
    if not q:
        return
    import urllib.request, urllib.parse
    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Buscando canales: {0}...".format(q))
    try:
        encoded = urllib.parse.quote_plus(q)
        url = "https://www.youtube.com/results?search_query={0}&sp=EgIQAg%3D%3D".format(encoded)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9"
        })
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
        dp.update(60)
        data = _yt_parse_initial_data(html)
        dp.close()
    except Exception as e:
        dp.close()
        xbmcgui.Dialog().ok("EspaTV", "Error al buscar canales:\n{0}".format(str(e)))
        return

    if not data:
        xbmcgui.Dialog().ok("EspaTV", "No se pudo obtener datos de YouTube.")
        return

    channels = []
    try:
        sections = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]
        for section in sections:
            isr = section.get("itemSectionRenderer")
            if not isr:
                continue
            for item in isr.get("contents", []):
                cr = item.get("channelRenderer")
                if cr:
                    ch_id = cr.get("channelId", "")
                    ch_title = cr.get("title", {}).get("simpleText", "")
                    ch_subs = cr.get("videoCountText", {}).get("simpleText", "")
                    ch_desc = ""
                    desc_runs = cr.get("descriptionSnippet", {}).get("runs", [])
                    for r in desc_runs:
                        ch_desc += r.get("text", "")
                    ch_thumb = ""
                    thumbs = cr.get("thumbnail", {}).get("thumbnails", [])
                    if thumbs:
                        ch_thumb = thumbs[-1].get("url", "")
                        if ch_thumb.startswith("//"):
                            ch_thumb = "https:" + ch_thumb
                    channels.append({
                        "id": ch_id, "title": ch_title, "subs": ch_subs,
                        "desc": ch_desc, "thumb": ch_thumb
                    })
    except (KeyError, TypeError):
        pass

    if not channels:
        xbmcgui.Dialog().ok("EspaTV", "No se encontraron canales para:\n{0}".format(q))
        return

    h = int(sys.argv[1])
    for ch in channels:
        label = ch["title"]
        if ch["subs"]:
            label += " [COLOR gray]({0})[/COLOR]".format(ch["subs"])
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': ch["thumb"] or 'DefaultActor.png', 'thumb': ch["thumb"] or 'DefaultActor.png'})
        plot = ch["desc"][:300] if ch["desc"] else "Canal de YouTube"
        li.setInfo('video', {'title': ch["title"], 'plot': plot})
        play_url = _u(action="felicidad_play", yt_id=ch["id"], name=ch["title"], ctype="channel")
        fav_params = json.dumps({"yt_id": ch["id"], "name": ch["title"], "ctype": "channel", "burl": ""})
        li.addContextMenuItems([("Añadir a Mis Favoritos", "RunPlugin({0})".format(
            _u(action='add_favorite', title=ch["title"], fav_url='yt_ch_{0}'.format(ch["id"]),
               icon=ch["thumb"], platform='youtube', fav_action='felicidad_play', params=fav_params)
        ))])
        xbmcplugin.addDirectoryItem(handle=h, url=play_url, listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)


def _get_yt_history_file():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p):
        os.makedirs(p)
    return os.path.join(p, 'yt_search_history.json')


def _load_yt_history():
    f = _get_yt_history_file()
    if not os.path.exists(f):
        return []
    try:
        with open(f, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except (ValueError, IOError):
        return []


def _add_yt_to_history(q):
    q = q.strip()
    if not q:
        return
    hist = _load_yt_history()
    if q in hist:
        hist.remove(q)
    hist.insert(0, q)
    hist = hist[:30]
    try:
        with open(_get_yt_history_file(), 'w', encoding='utf-8') as fh:
            json.dump(hist, fh)
    except IOError:
        pass


def _yt_parse_initial_data(html):
    m = re.search(r'var ytInitialData\s*=\s*(\{.*?\});\s*</script>', html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def _yt_extract_videos(data):
    results = []
    try:
        sections = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]
    except (KeyError, TypeError):
        return results
    for section in sections:
        isr = section.get("itemSectionRenderer")
        if not isr:
            continue
        for item in isr.get("contents", []):
            vr = item.get("videoRenderer")
            if not vr:
                continue
            vid = vr.get("videoId", "")
            if not vid:
                continue
            title = ""
            runs = vr.get("title", {}).get("runs", [])
            if runs:
                title = runs[0].get("text", "")
            dur = vr.get("lengthText", {}).get("simpleText", "")
            channel = ""
            chan_runs = vr.get("ownerText", {}).get("runs", [])
            if chan_runs:
                channel = chan_runs[0].get("text", "")
            views = vr.get("viewCountText", {}).get("simpleText", "")
            if not views:
                views = vr.get("shortViewCountText", {}).get("simpleText", "")
            published = vr.get("publishedTimeText", {}).get("simpleText", "")
            results.append({
                "vid": vid,
                "title": title or "Video",
                "duration": dur,
                "channel": channel,
                "views": views,
                "published": published,
            })
    return results


def _yt_search_results(query, sp=""):
    import urllib.request, urllib.parse
    h = int(sys.argv[1])
    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Buscando en YouTube: {0}...".format(query))
    try:
        encoded = urllib.parse.quote_plus(query)
        url = "https://www.youtube.com/results?search_query={0}".format(encoded)
        if sp:
            url += "&sp={0}".format(urllib.parse.quote(sp, safe=''))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9"
        })
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
        dp.update(60)

        data = _yt_parse_initial_data(html)
        if data:
            results = _yt_extract_videos(data)
        else:
            results = _yt_fallback_parse(html)
        dp.close()
    except Exception as e:
        dp.close()
        _log_error("Error buscando en YouTube: {0}".format(e))
        xbmcgui.Dialog().ok("EspaTV", "Error al buscar en YouTube:\n{0}".format(str(e)))
        return

    if not results:
        xbmcgui.Dialog().ok("EspaTV", "No se encontraron resultados para:\n{0}".format(query))
        return

    xbmcplugin.setContent(h, 'videos')
    for r in results:
        vid = r["vid"]
        title = r["title"]
        dur = r["duration"]
        channel = r["channel"]
        views = r["views"]

        li = xbmcgui.ListItem(label=title)
        thumb = 'https://i.ytimg.com/vi/{0}/hqdefault.jpg'.format(vid)
        li.setArt({'icon': 'DefaultMusicVideos.png', 'thumb': thumb})

        published = r.get("published", "")
        plot_lines = []
        if channel:
            plot_lines.append("Canal: {0}".format(channel))
        if dur:
            plot_lines.append("Duración: {0}".format(dur))
        if views:
            plot_lines.append("Vistas: {0}".format(views))
        if published:
            plot_lines.append("Subido: {0}".format(published))
        plot = "\n".join(plot_lines) if plot_lines else title
        li.setInfo('video', {'plot': plot, 'title': title})

        fav_params = json.dumps({"yt_id": vid, "name": title, "ctype": "video", "burl": ""})
        li.addContextMenuItems([("Añadir a Mis Favoritos", "RunPlugin({0})".format(
            _u(action='add_favorite', title=title, fav_url='yt_{0}'.format(vid),
               icon=thumb, platform='youtube', fav_action='felicidad_play', params=fav_params)
        ))])
        play_url = _u(action="felicidad_play", yt_id=vid, name=title, ctype="video")
        xbmcplugin.addDirectoryItem(handle=h, url=play_url, listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[COLOR yellow][B]Buscar otra cosa en YouTube...[/B][/COLOR]")
    li.setArt({'icon': 'DefaultAddonsSearch.png'})
    li.setInfo('video', {'plot': 'YouTube muestra hasta 20 resultados por búsqueda. Si necesitas más, prueba con términos más específicos.'})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="yt_search"), listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(h)


def _yt_fallback_parse(html):
    results = []
    seen = set()
    pattern = re.compile(r'"videoRenderer":\{"videoId":"([^"]{11})".*?"title":\{"runs":\[\{"text":"([^"]+)"\}')
    for m in pattern.finditer(html):
        vid, title = m.group(1), m.group(2)
        if vid not in seen:
            seen.add(vid)
            results.append({"vid": vid, "title": title, "duration": "", "channel": "", "views": ""})
        if len(results) >= 20:
            break
    return results


def _felicidad_play(yt_id="", name="", ctype="channel", burl=""):
    if ctype == "browser":
        if xbmc.getCondVisibility("System.Platform.Android"):
            opts = ["Abrir en la app YouTube", "Abrir en el navegador"]
            sel = xbmcgui.Dialog().select("{0}".format(name or "YouTube"), opts)
            if sel < 0: return
            if sel == 0:
                _felicidad_open_yt_app(burl or "https://www.youtube.com", name)
            else:
                _felicidad_open_browser(burl or "https://www.youtube.com", name)
        else:
            _felicidad_open_browser(burl or "https://www.youtube.com", name)
        return
    if ctype == "video":
        h = int(sys.argv[1])
        yt_thumb = "https://i.ytimg.com/vi/{0}/hqdefault.jpg".format(yt_id)
        is_android = xbmc.getCondVisibility("System.Platform.Android")
        is_pc = not is_android
        li = xbmcgui.ListItem(label="[COLOR limegreen][B]Reproducir aquí[/B][/COLOR]")
        li.setArt({"icon": "DefaultVideo.png", "thumb": yt_thumb})
        li.setInfo("video", {"plot": "Reproduce con el addon YouTube de Kodi."})
        xbmcplugin.addDirectoryItem(handle=h,
            url=_u(action="felicidad_play_exec", yt_id=yt_id, name=name, method="addon"),
            listitem=li, isFolder=False)
        if is_android:
            li = xbmcgui.ListItem(label="[COLOR lightskyblue]Abrir en la app YouTube[/COLOR]")
            li.setArt({"icon": "DefaultAddonProgram.png", "thumb": yt_thumb})
            li.setInfo("video", {"plot": "Abre en la aplicación YouTube de Android."})
            xbmcplugin.addDirectoryItem(handle=h,
                url=_u(action="felicidad_play_exec", yt_id=yt_id, name=name, method="yt_app"),
                listitem=li, isFolder=False)
        li = xbmcgui.ListItem(label="[COLOR orange]Abrir en el navegador[/COLOR]")
        li.setArt({"icon": "DefaultNetwork.png", "thumb": yt_thumb})
        li.setInfo("video", {"plot": "Abre la página de YouTube en el navegador."})
        xbmcplugin.addDirectoryItem(handle=h,
            url=_u(action="felicidad_play_exec", yt_id=yt_id, name=name, method="browser"),
            listitem=li, isFolder=False)
        if is_pc:
            li = xbmcgui.ListItem(label="[COLOR mediumpurple]Reproducir con yt-dlp[/COLOR]")
            li.setArt({"icon": "DefaultAddonVideo.png", "thumb": yt_thumb})
            li.setInfo("video", {"plot": "Resuelve el vídeo con yt-dlp y lo reproduce."})
            xbmcplugin.addDirectoryItem(handle=h,
                url=_u(action="felicidad_play_exec", yt_id=yt_id, name=name, method="ytdlp"),
                listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h)
        return
    # --- Canales y playlists: mantener diálogo ---
    yt_url_map = {
        "channel": ("plugin://plugin.video.youtube/channel/{0}/", "https://www.youtube.com/channel/{0}"),
        "playlist": ("plugin://plugin.video.youtube/playlist/{0}/", "https://www.youtube.com/playlist?list={0}"),
    }
    addon_tpl, web_tpl = yt_url_map.get(ctype, yt_url_map["channel"])
    is_android = xbmc.getCondVisibility("System.Platform.Android")
    if is_android:
        opts = ["Abrir aquí", "Abrir en la app YouTube", "Abrir en el navegador"]
    else:
        opts = ["Abrir aquí", "Abrir en el navegador"]
    sel = xbmcgui.Dialog().select("{0}".format(name or "YouTube"), opts)
    if sel < 0:
        return
    if sel == 0:
        if not _check_youtube_addon():
            if not _youtube_install_prompt():
                return
        xbmc.executebuiltin("ActivateWindow(Videos,{0})".format(addon_tpl.format(yt_id)))
    elif is_android and sel == 1:
        _felicidad_open_yt_app(web_tpl.format(yt_id), name)
    else:
        _felicidad_open_browser(web_tpl.format(yt_id), name)


def _felicidad_play_exec(yt_id="", name="", method="addon"):
    if not yt_id:
        return
    yt_web = "https://www.youtube.com/watch?v={0}".format(yt_id)
    if method == "addon":
        if not _check_youtube_addon():
            if not _youtube_install_prompt():
                return
        yt_thumb = "https://i.ytimg.com/vi/{0}/hqdefault.jpg".format(yt_id)
        try:
            _add_watch_entry(yt_id, name or yt_id, thumb=yt_thumb, duration=0)
        except Exception:
            pass
        plugin_url = "plugin://plugin.video.youtube/play/?video_id={0}".format(yt_id)
        xbmc.executebuiltin("PlayMedia({0})".format(plugin_url))
    elif method == "yt_app":
        _felicidad_open_yt_app(yt_web, name)
    elif method == "browser":
        _felicidad_open_browser(yt_web, name)
    elif method == "ytdlp":
        _felicidad_play_ytdlp(yt_id, name)


def _felicidad_play_ytdlp(yt_id, name=""):
    """Resuelve y reproduce un vídeo de YouTube usando yt-dlp (solo PC)."""
    yt_web = "https://www.youtube.com/watch?v={0}".format(yt_id)
    xbmcgui.Dialog().notification(
        "EspaTV", "Resolviendo con yt-dlp...",
        xbmcgui.NOTIFICATION_INFO, 2000,
    )
    stream_url = ytdlp_resolver.resolve(yt_web)
    if stream_url:
        li = xbmcgui.ListItem(path=stream_url)
        li.setProperty("IsPlayable", "true")
        li.setContentLookup(False)
        xbmc.Player().play(stream_url, li)
        yt_thumb = 'https://i.ytimg.com/vi/{0}/hqdefault.jpg'.format(yt_id)
        try:
            _add_watch_entry(yt_id, name or yt_id, thumb=yt_thumb, duration=0)
        except Exception:
            pass
        return
    xbmcgui.Dialog().ok(
        "EspaTV",
        "yt-dlp no pudo resolver el vídeo.\n\n"
        "Asegúrate de tener yt-dlp instalado:\npip install yt-dlp",
    )

# --- COCINA ESPAÑOLA ---
def _cocina_menu():
    h = int(sys.argv[1])
    if not _COCINA_ESPANOLA_YT:
        xbmcgui.Dialog().ok("Cocina Española", "Aún no hay contenido. ¡Próximamente!")
        return
    for cat_name, channels in _COCINA_ESPANOLA_YT.items():
        li = xbmcgui.ListItem(label="[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(cat_name, len(channels)))
        li.setArt({'icon': 'DefaultMusicVideos.png'})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="cocina_list", cat=cat_name), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)

def _cocina_list(cat_name):
    h = int(sys.argv[1])
    channels = _COCINA_ESPANOLA_YT.get(cat_name, [])
    if not channels:
        xbmcplugin.endOfDirectory(h); return
    watched = _load_watched_yt()
    for ch in channels:
        name = ch.get("name", "Canal")
        ctype = ch.get("type", "channel")
        desc = ch.get("desc", "")
        yt_id = ch.get("yt_id", "")
        burl = ch.get("url", "")
        is_watched = ctype == "video" and yt_id in watched
        if ctype == "browser":
            label = "[COLOR orange]{0}[/COLOR]".format(name)
            plot = "{0}\nSe abre en el navegador.".format(desc)
        elif ctype == "video":
            if is_watched:
                label = "[COLOR gray](Visto) {0}[/COLOR]".format(name)
            else:
                label = "[COLOR lime]{0}[/COLOR]".format(name)
            plot = "{0}\nVideo individual.".format(desc)
        elif ctype == "playlist":
            label = "[COLOR skyblue]{0}[/COLOR]".format(name)
            plot = "{0}\nPlaylist de YouTube.".format(desc)
        else:
            label = "[B]{0}[/B]".format(name)
            plot = "{0}\nCanal de YouTube.".format(desc)
        li = xbmcgui.ListItem(label=label)
        art = {'icon': 'DefaultMusicVideos.png'}
        if ctype == "video" and yt_id:
            thumb = "https://i.ytimg.com/vi/{0}/hqdefault.jpg".format(yt_id)
            art['thumb'] = thumb
            art['icon'] = thumb
        li.setArt(art)
        li.setInfo('video', {'title': name, 'plot': plot})
        fav_params = json.dumps({"yt_id": yt_id, "name": name, "ctype": ctype, "burl": burl})
        cm = []
        if ctype == "video" and yt_id:
            if is_watched:
                cm.append(("Desmarcar como visto", "RunPlugin({0})".format(
                    _u(action='yt_toggle_watched', yt_id=yt_id))))
            else:
                cm.append(("Marcar como visto", "RunPlugin({0})".format(
                    _u(action='yt_toggle_watched', yt_id=yt_id))))
            cm.append(("Añadir a cola de reproducción", "RunPlugin({0})".format(
                _u(action='yt_queue_add', yt_id=yt_id, name=name))))
        cm.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(
            _u(action='add_favorite', title=name, fav_url='yt_{0}'.format(yt_id),
               icon='DefaultMusicVideos.png', platform='cocina',
               fav_action='felicidad_play', params=fav_params))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="felicidad_play", yt_id=yt_id, name=name, ctype=ctype, burl=burl), listitem=li, isFolder=(ctype == "video"))
    xbmcplugin.endOfDirectory(h)

_YT_LIVE_CHANNELS = {
    "Noticias 24h": [
        {"name": "RTVE 24h", "yt_id": "UCfHptOfCGvKbHdFhfjJnWpA"},
        {"name": "euronews en español", "yt_id": "UCjwHpMgZoLPasxBswgFshKg"},
        {"name": "France 24 Español", "yt_id": "UCUdOoVWuWmgo1wByzcsyKDQ"},
        {"name": "DW Español", "yt_id": "UCT2VMLy-aMTdfnCjTmYgUbg"},
        {"name": "Televisión de Galicia", "yt_id": "UC7dxTqnuQI6IxCZMmJG_MOw"},
    ],
    "Música": [
        {"name": "Lo-fi Girl", "yt_id": "UCSJ4gkVC6NrvII8umztf0A"},
        {"name": "Steezy Beats", "yt_id": "UCwNw0w-CorPkMNy5vRxatZg"},
        {"name": "Chillhop Music", "yt_id": "UCOxqgCwgOqC2lMqC5PYz_Dg"},
    ],
    "Naturaleza y Documentales": [
        {"name": "Explore.org Live Cams", "yt_id": "UCOhMiSLNDD0D7xTuHCYgEhw"},
    ],
}

def _check_youtube_addon():
    """Comprueba si el addon de YouTube está instalado. Devuelve True si sí."""
    try:
        xbmcaddon.Addon("plugin.video.youtube")
        return True
    except Exception:
        return False



def _youtube_install_prompt():
    """
    Subrutina interactiva con Thread Lock para la gestion de dependencias dinamicas (plugin.video.youtube).
    Regresa False si aborta, y es Safe-Bailing para evitar Kodi timeouts concurrentes.
    """
    import xbmc, xbmcgui
    import traceback
    

    dialog = xbmcgui.Dialog()
    
    try:
        # Peticion imperativa de interaccion del usuario
        if dialog.yesno("Dependencia Requerida en EspaTV", 
                        "Motor oficial de YouTube detectado ausente.\n\n"
                        "¿Autorizas la instalación automatizada nativa en este momento?",
                        nolabel="Cancelar", yeslabel="Sí, Instalar"):
            
            xbmc.log("[EspaTV] Dependencies: Aprobacion concedida. Lanzando Builtin Event.", xbmc.LOGINFO)
            
            # Ejecucion protegida del Sandbox de C++ Kodi
            xbmc.executebuiltin("InstallAddon(plugin.video.youtube)")
            
            # Notificacion asíncrona sutil
            dialog.notification("EspaTV", "Instalando módulo central por defecto...", xbmcgui.NOTIFICATION_INFO, 2500)
            

            return False
        else:
            xbmc.log("[EspaTV] Dependencies: Evaluacion completada, instalador denegado.", xbmc.LOGINFO)
            dialog.notification("EspaTV", "Instalación manual requerida (YouTube cancelado).", xbmcgui.NOTIFICATION_WARNING)
            return False
            
    except Exception as script_error:
        tb = traceback.format_exc()
        xbmc.log("[EspaTV] EXCEPCION CRITICA en Dependency Installer: {0}\n\t{1}".format(str(script_error), str(tb)), xbmc.LOGERROR)
        dialog.notification("EspaTV Error", "Fallo al llamar la API del sistema", xbmcgui.NOTIFICATION_ERROR)
        return False




def _webcams_menu():
    h = int(sys.argv[1])
    _cam_icon = os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources', 'media', 'webcam.png')
    for cat_name, items in _WEBCAMS_ES.items():
        if isinstance(items, dict):
            total_items = sum(len(sub) for sub in items.values())
        else:
            total_items = len(items)
        li = xbmcgui.ListItem(label="[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(cat_name, total_items))
        li.setArt({'icon': _cam_icon})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="webcams_list", cat=cat_name), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)

def _webcams_list(cat_name, subcat_name=""):
    h = int(sys.argv[1])
    node = _WEBCAMS_ES.get(cat_name, [])
    _cam_icon = os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources', 'media', 'webcam.png')

    if isinstance(node, dict) and not subcat_name:
        for sub_name, sub_items in node.items():
            li = xbmcgui.ListItem(label="[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(sub_name, len(sub_items)))
            li.setArt({'icon': _cam_icon})
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="webcams_list", cat=cat_name, subcat=sub_name), listitem=li, isFolder=True)
        xbmcplugin.endOfDirectory(h)
        return

    items = []
    if isinstance(node, dict) and subcat_name:
        items = node.get(subcat_name, [])
    elif isinstance(node, list):
        items = node

    if not items:
        xbmcplugin.endOfDirectory(h)
        return
    for item in items:
        name = item.get("name", "Cámara")
        desc = item.get("desc", name)
        stream_url = item.get("url", "")
        yt_id = item.get("yt_id", "")
        li = xbmcgui.ListItem(label="[B][COLOR red]●[/COLOR] {0}[/B]".format(name))
        li.setArt({'icon': _cam_icon, 'thumb': _cam_icon})
        li.setInfo('video', {'title': name, 'plot': "{0}\nPulsa para ver en directo.".format(desc)})
        li.setProperty("IsPlayable", "true")
        
        if yt_id:
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="play_youtube_live", yt_id=yt_id, title=name), listitem=li, isFolder=False)
        else:
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="play_webcam", stream_url=stream_url, title=name), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)

def _play_webcam(stream_url, title=""):
    h = int(sys.argv[1])
    if not stream_url:
        xbmcgui.Dialog().notification("EspaTV", "Cámara no disponible.", xbmcgui.NOTIFICATION_WARNING)
        xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
        return
    xbmc.log("[EspaTV] Reproduciendo webcam: {0}".format(stream_url), xbmc.LOGINFO)
    li = xbmcgui.ListItem(path=stream_url)
    li.setInfo('video', {'title': title})
    li.setMimeType('application/vnd.apple.mpegurl')
    li.setContentLookup(False)
    xbmcplugin.setResolvedUrl(h, True, li)

def _play_dash(stream_url, title=""):
    h = int(sys.argv[1])
    if not stream_url:
        xbmcgui.Dialog().notification("EspaTV", "Streaming DASH no disponible.", xbmcgui.NOTIFICATION_WARNING)
        xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
        return
    xbmc.log("[EspaTV] Reproduciendo flujo DASH: {0}".format(stream_url), xbmc.LOGINFO)
    li = xbmcgui.ListItem(path=stream_url)
    li.setInfo('video', {'title': title})
    li.setProperty('inputstream', 'inputstream.adaptive')
    li.setProperty('inputstream.adaptive.manifest_type', 'mpd')
    li.setMimeType('application/dash+xml')
    li.setContentLookup(False)
    xbmcplugin.setResolvedUrl(h, True, li)


def _youtube_live_menu():
    h = int(sys.argv[1])
    if not _check_youtube_addon():
        li = xbmcgui.ListItem(label="[COLOR red][B]Instalar addon YouTube (necesario)[/B][/COLOR]")
        li.setArt({'icon': 'DefaultAddonHelper.png'})
        li.setInfo('video', {'plot': "El addon YouTube de Kodi es necesario para reproducir estos canales.\nPulsa aquí para ver las instrucciones de instalación."})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="youtube_install"), listitem=li, isFolder=False)
    for cat_name, channels in _YT_LIVE_CHANNELS.items():
        li = xbmcgui.ListItem(label="[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(cat_name, len(channels)))
        li.setArt({'icon': 'DefaultAddonPVRClient.png'})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="youtube_live_list", cat=cat_name), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)

def _youtube_live_list(cat_name):
    h = int(sys.argv[1])
    channels = _YT_LIVE_CHANNELS.get(cat_name, [])
    if not channels:
        xbmcplugin.endOfDirectory(h); return
    has_yt = _check_youtube_addon()
    for ch in channels:
        name = ch.get("name", "Canal")
        yt_id = ch.get("yt_id", "")
        li = xbmcgui.ListItem(label="[B]{0}[/B]".format(name))
        li.setArt({'icon': 'DefaultAddonPVRClient.png', 'thumb': 'DefaultAddonPVRClient.png'})
        if has_yt:
            li.setInfo('video', {'title': name, 'plot': "YouTube Live: {0}\nPulsa para ver en directo.".format(name)})
        else:
            li.setInfo('video', {'title': name, 'plot': "YouTube Live: {0}\n[COLOR red]Necesitas instalar el addon YouTube.[/COLOR]".format(name)})
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="play_youtube_live", yt_id=yt_id, title=name), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)

def _play_youtube_live(yt_id, title=""):
    h = int(sys.argv[1])
    if not _check_youtube_addon():
        _youtube_install_prompt()
        xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
        return
    yt_url = "plugin://plugin.video.youtube/play/?channel_id={0}&live=1".format(yt_id)
    xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
    xbmc.executebuiltin("PlayMedia({0})".format(yt_url))

def _play_latest_news():
    h = int(sys.argv[1])
    url = "https://fapi-top.prisasd.com/podcast/playser/hora_14/itunestfp/podcast.xml"
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        r.encoding = 'utf-8'
        root = ET.fromstring(r.text)
        item = root.find('.//item')
        if item is None:
            xbmcgui.Dialog().notification("EspaTV", "No hay boletines disponibles.", xbmcgui.NOTIFICATION_WARNING)
            xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
            return
        enclosure = item.find('enclosure')
        if enclosure is None or not enclosure.get('url'):
            xbmcgui.Dialog().notification("EspaTV", "Boletín sin audio disponible.", xbmcgui.NOTIFICATION_WARNING)
            xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())
            return
        audio_url = enclosure.get('url')
        title_el = item.find('title')
        title = title_el.text if title_el is not None and title_el.text else "Boletín Informativo"
        xbmc.log("[EspaTV] Reproduciendo RSS Noticias: {0}".format(audio_url), xbmc.LOGINFO)
        xbmcgui.Dialog().notification("EspaTV", "Reproduciendo: {0}".format(title), xbmcgui.NOTIFICATION_INFO, 3000)
        li = xbmcgui.ListItem(path=audio_url)
        li.setInfo(type="Music", infoLabels={"Title": title, "Artist": "SER Noticias"})
        xbmcplugin.setResolvedUrl(h, True, li)
        return
    except Exception as exc:
        _log_error("Error obteniendo boletín de noticias: {0}".format(exc))
        xbmcgui.Dialog().notification("EspaTV", "Error obteniendo el boletín de noticias.", xbmcgui.NOTIFICATION_ERROR)
    xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())

# --- PRENSA (Titulares RSS) ---
_PRENSA_FEEDS = [
    {"name": "El País", "url": "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada"},
    {"name": "El Mundo", "url": "https://e00-elmundo.uecdn.es/elmundo/rss/portada.xml"},
    {"name": "ABC", "url": "https://www.abc.es/rss/2.0/portada/"},
    {"name": "El Confidencial", "url": "https://rss.elconfidencial.com/espana/"},
    {"name": "20 Minutos", "url": "https://www.20minutos.es/rss"},
    {"name": "elDiario.es", "url": "https://www.eldiario.es/rss/"},
    {"name": "Europa Press", "url": "https://www.europapress.es/rss/rss.aspx"},
    {"name": "RTVE Noticias", "url": "https://www.rtve.es/rss/temas_noticias.xml"},
    {"name": "Marca", "url": "https://e00-marca.uecdn.es/rss/portada.xml"},
    {"name": "Diario AS", "url": "https://feeds.as.com/mrss-s/pages/as/site/as.com/portada"},
    {"name": "Mundo Deportivo", "url": "https://www.mundodeportivo.com/rss/home.xml"},
]

def _press_menu():
    h = int(sys.argv[1])
    _news_icon = os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources', 'media', 'news.png')
    for feed in _PRENSA_FEEDS:
        li = xbmcgui.ListItem(label="[B]{0}[/B]".format(feed['name']))
        li.setArt({'icon': _news_icon})
        li.setInfo('video', {'plot': "Explora los últimos titulares de {0}.".format(feed['name'])})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="press_list", raw_url=feed['url'], name=feed['name']), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h)

def _press_list(feed_url, feed_name):
    h = int(sys.argv[1])
    try:
        import xml.etree.ElementTree as ET
        import re as _re
        r = requests.get(feed_url, timeout=10)
        r.raise_for_status()
        r.encoding = 'utf-8'
        root = ET.fromstring(r.text)
        items = root.findall('.//item')
        if not items:
            items = root.findall('.//{http://www.w3.org/2005/Atom}entry')

        for item in items:
            # Title (protegido contra .text = None)
            t_elem = item.find('title')
            if t_elem is None:
                t_elem = item.find('{http://www.w3.org/2005/Atom}title')
            title = (t_elem.text or "") if t_elem is not None else "Sin Titular"
            title = title.replace("<![CDATA[", "").replace("]]>", "").strip()
            if not title:
                title = "Sin Titular"

            # Description (protegido contra .text = None)
            d_elem = item.find('description')
            if d_elem is None:
                d_elem = item.find('{http://www.w3.org/2005/Atom}summary')
            desc = (d_elem.text or "") if d_elem is not None else ""
            desc = desc.replace("<![CDATA[", "").replace("]]>", "").strip()
            desc = _re.sub(r'<[^>]+>', '', desc)

            # Image
            _news_icon = os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources', 'media', 'news.png')
            thumb = _news_icon
            media_content = item.find('{http://search.yahoo.com/mrss/}content')
            if media_content is not None and media_content.get('url'):
                thumb = media_content.get('url')
            else:
                media_thumb = item.find('{http://search.yahoo.com/mrss/}thumbnail')
                if media_thumb is not None and media_thumb.get('url'):
                    thumb = media_thumb.get('url')
                else:
                    enclosure = item.find('enclosure')
                    if enclosure is not None and enclosure.get('type', '').startswith('image'):
                        thumb = enclosure.get('url')

            li = xbmcgui.ListItem(label=title)
            li.setArt({'icon': thumb, 'thumb': thumb})
            li.setInfo('video', {'title': title, 'plot': desc})

            # Truncar desc para URL (Kodi limita ~2048 chars en URLs de plugin)
            safe_desc = desc[:500] if len(desc) > 500 else desc
            xbmcplugin.addDirectoryItem(
                handle=h,
                url=_u(action="press_read", title=title, desc=safe_desc),
                listitem=li, isFolder=False)

    except Exception as exc:
        _log_error("Error cargando prensa ({0}): {1}".format(feed_url, exc))
        xbmcgui.Dialog().notification("EspaTV", "Error leyendo el feed.", xbmcgui.NOTIFICATION_ERROR)

    xbmcplugin.endOfDirectory(h)

def _press_read(title, desc):
    xbmcgui.Dialog().textviewer(title, desc)

# --- PODCAST ---
_PODCAST_FEEDS = [
    {"name": "El Larguero (Cadena SER)", "url": "https://fapi-top.prisasd.com/podcast/playser/el_larguero/itunestfp/podcast.xml", "icon": "DefaultMusicSongs.png", "desc": "Manu Carreño con lo mejor del deporte cada noche."},
    {"name": "Hora 25 (Cadena SER)", "url": "https://fapi-top.prisasd.com/podcast/playser/hora_25/itunestfp/podcast.xml", "icon": "DefaultMusicSongs.png", "desc": "Àngels Barceló con las noticias del día."},
    {"name": "Herrera en COPE", "url": "https://www.cope.es/api/es/programas/herrera-en-cope/audios/rss.xml", "icon": "DefaultMusicSongs.png", "desc": "Carlos Herrera en las mañanas de COPE."},
    {"name": "La Vida Moderna (SER)", "url": "https://fapi-top.prisasd.com/podcast/playser/la_vida_moderna/itunestfp/podcast.xml", "icon": "DefaultMusicSongs.png", "desc": "Humor y actualidad."},
    {"name": "Todopoderosos", "url": "http://feeds.feedburner.com/todopoderosos", "icon": "DefaultMusicSongs.png", "desc": "Podcast de humor con Berto Romero y más."},
    {"name": "Wild Project", "url": "https://thewildproject.libsyn.com/rss", "icon": "DefaultMusicSongs.png", "desc": "Jordi Wild entrevista a personalidades."},
    {"name": "Entiende tu mente", "url": "http://www.spreaker.com/show/2630773/episodes/feed", "icon": "DefaultMusicSongs.png", "desc": "Podcast de psicología y desarrollo personal."},
    {"name": "Nadie Sabe Nada (SER)", "url": "https://fapi-top.prisasd.com/podcast/playser/nadie_sabe_nada/itunestfp/podcast.xml", "icon": "DefaultMusicSongs.png", "desc": "Buenafuente y Berto Romero improvisando cada sábado."},
    {"name": "Criminopatía", "url": "https://feeds.megaphone.fm/ASCCT6415418901", "icon": "DefaultMusicSongs.png", "desc": "True crime en español. Casos reales narrados en detalle."},
    {"name": "El Partidazo de COPE", "url": "https://www.cope.es/api/es/programas/el-partidazo-de-cope/audios/rss.xml", "icon": "DefaultMusicSongs.png", "desc": "Juanma Castaño con la mejor actualidad deportiva nocturna."},
    {"name": "Tiempo de Juego (COPE)", "url": "https://www.cope.es/api/es/programas/tiempo-de-juego/audios/rss.xml", "icon": "DefaultMusicSongs.png", "desc": "Paco González y el mejor fútbol en directo."},
    {"name": "A Vivir (Cadena SER)", "url": "https://fapi-top.prisasd.com/podcast/playser/a_vivir_que_son_dos_dias/itunestfp/podcast.xml", "icon": "DefaultMusicSongs.png", "desc": "Javier del Pino cada fin de semana en la SER."},
    {"name": "La Ventana (Cadena SER)", "url": "https://fapi-top.prisasd.com/podcast/playser/la_ventana/itunestfp/podcast.xml", "icon": "DefaultMusicSongs.png", "desc": "Carles Francino cada tarde en Cadena SER."},
    {"name": "Estirando el Chicle", "url": "https://www.omnycontent.com/d/playlist/2446592a-b80e-4d28-a4fd-ae4c0140ac11/42658bb8-4afa-4449-9c7a-aea9010e5b53/5942eb19-73a6-4772-8093-aea9010e5b61/podcast.rss", "icon": "DefaultMusicSongs.png", "desc": "Carolina Iglesias y Victoria Martín rajan de todo."},
    {"name": "La Ruina", "url": "https://anchor.fm/s/a4e7fe88/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Ignasi Taltavull y Tomàs Fuentes y las peores anécdotas."},
    {"name": "La Órbita de Endor (LODE)", "url": "https://feeds.ivoox.com/feed_fg_f113302_filtro_1.xml", "icon": "DefaultMusicSongs.png", "desc": "El podcast friki por excelencia. Cine, series y cómics."},
    {"name": "Lo Que Tú Digas con Álex Fidalgo", "url": "https://anchor.fm/s/101d1e078/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Charlas sin guion, sin filtros y sin censuras."},
    {"name": "Aquí Hay Dragones", "url": "https://feeds.ivoox.com/feed_fg_f1576365_filtro_1.xml", "icon": "DefaultMusicSongs.png", "desc": "Historia y humor con Rodrigo Cortés, Cansado, Gómez-Jurado y Arturo."},
    {"name": "SER Historia", "url": "https://fapi-top.prisasd.com/podcast/playser/ser_historia/itunestfp/podcast.xml", "icon": "DefaultMusicSongs.png", "desc": "Nacho Ares nos acerca los grandes misterios de la historia."},
    {"name": "La Escóbula de la Brújula", "url": "https://www.omnycontent.com/d/playlist/2446592a-b80e-4d28-a4fd-ae4c0140ac11/9afe58f0-d402-4e05-a5ce-aead00a0ba96/fd25a142-cbf1-4f2b-9146-aead00a0bab7/podcast.rss", "icon": "DefaultMusicSongs.png", "desc": "Historia, misterio y leyendas con Jesús Callejo y equipo."},
    {"name": "Misterios y Cubatas", "url": "https://anchor.fm/s/10084e580/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Podcast de misterio, conspiraciones y muchas risas."},
    {"name": "El Podcast de Marian Rojas Estapé", "url": "https://feeds.simplecast.com/8YhIPHu1", "icon": "DefaultMusicSongs.png", "desc": "Salud mental, psicología y bienestar emocional."},
    {"name": "Poco se habla!", "url": "https://anchor.fm/s/f04e75c8/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Humor y salseo con Ana Brito y Xuso Jones."},
    {"name": "Saldremos mejores", "url": "https://www.omnycontent.com/d/playlist/2446592a-b80e-4d28-a4fd-ae4c0140ac11/172ad2c5-9214-4b21-85fe-aeaa01143b70/58c58f5a-82b3-4be2-aa54-aeaa01143b82/podcast.rss", "icon": "DefaultMusicSongs.png", "desc": "Actualidad y análisis social en tono de humor con Inés y Nerea."},
    {"name": "El Descampao", "url": "https://feeds.ivoox.com/feed_fg_f1268956_filtro_1.xml", "icon": "DefaultMusicSongs.png", "desc": "Documentales sonoros, cine, música e historia friki."},
    {"name": "Nude Project", "url": "https://anchor.fm/s/107d78054/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Entrevistas a emprendedores, artistas e ídolos jóvenes."},
    {"name": "Crims (CCMA)", "url": "https://dinamics.3cat.cat/public/podcast/catradio/xml/9/5/podprograma1859.xml", "icon": "DefaultMusicSongs.png", "desc": "El mítico true-crime narrado por Carles Porta."},
    {"name": "Tengo un Plan", "url": "https://anchor.fm/s/1007af750/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Sergio Beguería y Juan Domínguez: éxito y desarrollo personal."},
    {"name": "ROCA PROJECT", "url": "https://anchor.fm/s/ff4c5068/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Carlos Roca con entrevistas inspiracionales sin filtro."},
    {"name": "WORLDCAST", "url": "https://feeds.megaphone.fm/HOT7749589265", "icon": "DefaultMusicSongs.png", "desc": "Pedro Buerbaum conversando con los invitados más virales."},
    {"name": "Black Mango Podcast", "url": "https://anchor.fm/s/e0c735b8/podcast/rss", "icon": "DefaultMusicSongs.png", "desc": "Historia, misterios oscuros y casos reales."},
    {"name": "La ContraCrónica", "url": "https://feeds.ivoox.com/feed_fg_f1267769_filtro_1.xml", "icon": "DefaultMusicSongs.png", "desc": "Fernando Díaz Villanueva analiza la política, la economía y el mundo."},
    {"name": "A solas con... Vicky Martín Berrocal", "url": "https://www.omnycontent.com/d/playlist/2446592a-b80e-4d28-a4fd-ae4c0140ac11/d46d806d-6f59-403c-871f-b04400f4e432/52f42c98-c91e-469f-9579-b04400f56b06/podcast.rss", "icon": "DefaultMusicSongs.png", "desc": "Charlas íntimas e inspiradoras con invitadas mediáticas."},
    {"name": "Crónicas de la Calle Morgue", "url": "https://feeds.ivoox.com/feed_fg_f11436296_filtro_1.xml", "icon": "DefaultMusicSongs.png", "desc": "Relatos de crímenes reales llenos de detalle y crudeza."}
]

def _podcast_menu():
    h = int(sys.argv[1])
    for pod in _PODCAST_FEEDS:
        li = xbmcgui.ListItem(label="[B]{0}[/B]".format(pod["name"]))
        li.setArt({'icon': pod.get("icon", "DefaultMusicSongs.png")})
        li.setInfo('video', {'plot': pod.get("desc", "")})
        fav_params = json.dumps({"url": pod["url"], "title": pod["name"]})
        li.addContextMenuItems([("Añadir a Mis Favoritos", "RunPlugin({0})".format(_u(action='add_favorite', title=pod["name"], fav_url='podcast_{0}'.format(pod["name"]), icon=pod.get("icon", "DefaultMusicSongs.png"), platform='podcast', fav_action='podcast_feed', params=fav_params)))])
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="podcast_feed", url=pod["url"], title=pod["name"]), listitem=li, isFolder=True)
    xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())

def _podcast_feed(feed_url, feed_title=""):
    h = int(sys.argv[1])
    dp = xbmcgui.DialogProgress()
    dp.create("Podcast", "Cargando episodios...")
    try:
        import xml.etree.ElementTree as ET
        dp.update(30, "Descargando feed...")
        content = None
        c_file = ""
        ttl = core_settings.get_iptv_cache_ttl()
        if core_settings.is_iptv_cache_active() and ttl > 0:
            import hashlib, time, os
            c_dir = core_settings.get_iptv_cache_dir()
            c_key = hashlib.md5(feed_url.encode('utf-8')).hexdigest()
            c_file = os.path.join(c_dir, 'pod_' + c_key + '.xml')
            if os.path.exists(c_file):
                try:
                    with open(c_file, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                    if (time.time() - meta.get('ts', 0)) < ttl:
                        content = meta.get('data', '').encode('utf-8')
                except Exception:
                    pass

        if not content:
            r = requests.get(feed_url, timeout=15, headers={"User-Agent": "Kodi EspaTV"})
            if r.status_code != 200:
                dp.close()
                xbmcgui.Dialog().ok("Podcast", "Error al descargar el feed: HTTP {0}".format(r.status_code))
                xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active()); return
            content = r.content
            if core_settings.is_iptv_cache_active() and c_file:
                try:
                    import time
                    with open(c_file, 'w', encoding='utf-8') as f:
                        json.dump({'ts': time.time(), 'data': content.decode('utf-8', 'ignore')}, f)
                except Exception:
                    pass

        dp.update(70, "Procesando episodios...")
        root = ET.fromstring(content)
        channel = root.find("channel")
        if channel is None:
            dp.close()
            xbmcgui.Dialog().ok("Podcast", "Feed no válido.")
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active()); return
        items = channel.findall("item")
        dp.close()
        if not items:
            xbmcgui.Dialog().ok("Podcast", "No se encontraron episodios.")
            xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active()); return
        xbmcplugin.setContent(h, 'songs')
        for item in items[:50]:
            title_el = item.find("title")
            title = title_el.text.strip() if title_el is not None and title_el.text else "Episodio"
            desc_el = item.find("description")
            desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
            pub_el = item.find("pubDate")
            pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
            enclosure = item.find("enclosure")
            audio_url = enclosure.get("url", "") if enclosure is not None else ""
            if not audio_url:
                link_el = item.find("link")
                audio_url = link_el.text.strip() if link_el is not None and link_el.text else ""
            if not audio_url:
                continue
            # Thumbnail from itunes:image
            thumb = ""
            for el in item:
                if "image" in el.tag and el.get("href"):
                    thumb = el.get("href", "")
                    break
            label = title
            if pub_date:
                try:
                    short_date = pub_date.split(",")[1].strip()[:12] if "," in pub_date else pub_date[:16]
                    label = "[COLOR gray]{0}[/COLOR] {1}".format(short_date, title)
                except Exception:
                    pass
            li = xbmcgui.ListItem(label=label)
            li.setArt({'icon': thumb or 'DefaultMusicSongs.png', 'thumb': thumb or 'DefaultMusicSongs.png'})
            plot = ""
            if feed_title: plot += "Podcast: {0}\n".format(feed_title)
            if pub_date: plot += "Fecha: {0}\n".format(pub_date)
            if desc: plot += "\n{0}".format(desc[:500])
            li.setInfo('video', {'title': title, 'plot': plot})
            li.setProperty("IsPlayable", "true")
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="play_podcast", url=audio_url, title=title), listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
    except Exception as e:
        try: dp.close()
        except Exception: pass
        _log_error("Podcast feed error: {0}".format(e))
        xbmcgui.Dialog().ok("Podcast", "Error al cargar el feed:\n{0}".format(e))
        try: xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())
        except Exception: pass

def _play_podcast(url, title=""):
    h = int(sys.argv[1])
    try:
        li = xbmcgui.ListItem(path=url)
        if title: li.setInfo('video', {'title': title})
        li.setContentLookup(False)
        xbmcplugin.setResolvedUrl(h, True, li)
    except Exception as e:
        _log_error("Podcast play error: {0}".format(e))
        xbmcplugin.setResolvedUrl(h, False, xbmcgui.ListItem())


# --- LO MÁS VISTO ---
def _most_watched():
    h = int(sys.argv[1])
    history = _load_watch_history()
    if not history:
        li = xbmcgui.ListItem(label="[COLOR gray]No hay historial aún. Reproduce vídeos para ver tus más vistos.[/COLOR]")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h); return

    # Collect entries to show (top 20)
    entries_to_show = []
    for entry in history:
        if len(entries_to_show) >= 20: break
        vid = entry.get('vid', '')
        if not vid: continue
        entries_to_show.append(entry)

    # Detect entries with DM IDs as titles and batch-resolve from API
    ids_to_resolve = []
    for entry in entries_to_show:
        vt = entry.get('title', '')
        vid = entry.get('vid', '')
        if not vt or vt == vid or (len(vt) < 12 and ' ' not in vt):
            ids_to_resolve.append(vid)

    resolved = {}
    if ids_to_resolve:
        try:
            batch_ids = ",".join(ids_to_resolve[:20])
            r = requests.get("https://api.dailymotion.com/videos",
                params={"ids": batch_ids, "fields": "id,title,thumbnail_720_url,thumbnail_url,duration"},
                timeout=10)
            if r.status_code == 200:
                for v in r.json().get("list", []):
                    resolved[v.get("id", "")] = v
        except Exception:
            pass

    # Update history with resolved titles for future use
    if resolved:
        updated = False
        for entry in history:
            vid = entry.get('vid', '')
            if vid in resolved:
                info = resolved[vid]
                real_title = info.get("title", "")
                real_thumb = info.get("thumbnail_720_url") or info.get("thumbnail_url", "")
                real_dur = info.get("duration", 0)
                if real_title and (not entry.get('title') or entry['title'] == vid or (len(entry['title']) < 12 and ' ' not in entry['title'])):
                    entry['title'] = real_title
                    updated = True
                if real_thumb and not entry.get('thumb'):
                    entry['thumb'] = real_thumb
                    updated = True
                if real_dur and not entry.get('duration'):
                    entry['duration'] = real_dur
                    updated = True
        if updated:
            _save_watch_history(history)

    xbmcplugin.setContent(h, 'videos')
    addon = xbmcaddon.Addon()
    ai = addon.getAddonInfo('icon')
    for idx, entry in enumerate(entries_to_show):
        vid = entry.get('vid', '')
        vt = entry.get('title', 'Video')
        dth = entry.get('thumb', '') or ai
        du = entry.get('duration', 0)
        try: du = int(du)
        except (ValueError, TypeError): du = 0

        # Use resolved data if available
        if vid in resolved:
            info = resolved[vid]
            vt = info.get("title", vt) or vt
            dth = info.get("thumbnail_720_url") or info.get("thumbnail_url", "") or dth
            try: du = int(info.get("duration", du))
            except (ValueError, TypeError): pass

        label = "[COLOR gold]{0}.[/COLOR] {1}".format(idx + 1, vt)
        li = xbmcgui.ListItem(label=label)
        li.setArt({'thumb': dth, 'icon': dth})
        plot = "Acceso rápido a tus vídeos recientes."
        if du: plot += "\nDuración: {0}m {1}s".format(du // 60, du % 60)
        li.setInfo('video', {'title': vt, 'plot': plot, 'duration': du})
        li.setProperty("IsPlayable", "true")
        cm = []
        cm.append(("Descargar Video", "RunPlugin({0})".format(_u(action='download_video', vid=vid, title=vt))))
        cm.append(("Abrir en navegador", "RunPlugin({0})".format(_u(action='dm_open_browser', url=vid))))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vid)))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="pv", vid=vid), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR red][B]Limpiar lista de lo más visto[/B][/COLOR]")
    li.setArt({'icon': 'DefaultIconError.png'})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="clear_most_watched"), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(h)


_WATCH_HISTORY_FILE = "watch_history.json"
_MAX_WATCH_HISTORY = 100

def _load_watch_history():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    fp = os.path.join(p, _WATCH_HISTORY_FILE)
    if not os.path.exists(fp): return []
    try:
        with open(fp, 'r', encoding='utf-8') as f: return json.load(f)
    except (json.JSONDecodeError, IOError): return []

def _save_watch_history(history):
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    fp = os.path.join(p, _WATCH_HISTORY_FILE)
    try:
        with open(fp, 'w', encoding='utf-8') as f: json.dump(history, f, ensure_ascii=False)
    except IOError: pass

def _add_watch_entry(vid, title, thumb="", duration=0):
    if not vid: return
    history = _load_watch_history()
    history = [entry for entry in history if entry.get("vid") != vid]
    history.insert(0, {"vid": vid, "title": title, "thumb": thumb, "duration": duration, "watched_at": int(time.time())})
    history = history[:_MAX_WATCH_HISTORY]
    _save_watch_history(history)

def _get_watched_ids():
    return set(entry.get("vid", "") for entry in _load_watch_history())

def _view_watch_history():
    import datetime
    h = int(sys.argv[1])
    history = _load_watch_history()
    if history:
        li = xbmcgui.ListItem(label="[B][COLOR gold]Lo más visto[/COLOR][/B]")
        li.setArt({'icon': 'DefaultMusicTop100.png'})
        li.setInfo('video', {'plot': "Acceso rápido a tus 20 vídeos más recientes."})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="most_watched"), listitem=li, isFolder=True)
        li = xbmcgui.ListItem(label="[COLOR red][B]Borrar todo el historial de visionado[/B][/COLOR]")
        li.setArt({'icon': 'DefaultIconError.png'})
        li.setInfo('video', {'plot': "Tienes {0} vídeos en el historial.".format(len(history))})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="clear_watch_history"), listitem=li, isFolder=False)
    if not history:
        li = xbmcgui.ListItem(label="[COLOR gray]No has reproducido ningún vídeo aún[/COLOR]")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h); return
    xbmcplugin.setContent(h, 'videos')
    addon = xbmcaddon.Addon()
    ai = addon.getAddonInfo('icon')
    for entry in history:
        vid = entry.get('vid', '')
        vt = entry.get('title', 'Video')
        dth = entry.get('thumb', '') or ai
        du = entry.get('duration', 0)
        try: du = int(du)
        except (ValueError, TypeError): du = 0
        watched_at = entry.get('watched_at', 0)
        try:
            dt = datetime.datetime.fromtimestamp(watched_at)
            date_str = dt.strftime("%d/%m/%Y %H:%M")
        except Exception: date_str = ""
        label = "[COLOR lime][V][/COLOR] {0}".format(vt)
        li = xbmcgui.ListItem(label=label)
        li.setArt({'thumb': dth, 'icon': dth})
        plot = "Visto: {0}".format(date_str)
        if du: plot += "\nDuración: {0}m {1}s".format(du // 60, du % 60)
        li.setInfo('video', {'title': vt, 'plot': plot, 'duration': du})
        li.setProperty("IsPlayable", "true")
        cm = []
        if vid:
            cm.append(("Descargar Video", "RunPlugin({0})".format(_u(action='download_video', vid=vid, title=vt))))
            cm.append(("Eliminar del historial", "RunPlugin({0})".format(_u(action="del_watch_entry", url=vid))))
            cm.append(("Abrir en navegador", "RunPlugin({0})".format(_u(action="dm_open_browser", url=vid))))
            cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vid)))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="pv", vid=vid), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)

def _del_watch_entry(vid):
    if not vid: return
    history = _load_watch_history()
    history = [entry for entry in history if entry.get("vid") != vid]
    _save_watch_history(history)
    xbmc.executebuiltin("Container.Refresh")

def _clear_watch_history():
    if xbmcgui.Dialog().yesno("EspaTV", "¿Borrar todo el historial de visionado?"):
        _save_watch_history([])
        xbmc.executebuiltin("Container.Refresh")




def _show_trending():
    h = int(sys.argv[1])
    try:
        time_limit = int(time.time()) - 604800
        params = {
            "sort": "trending", "language": "es", "created_after": time_limit,
            "limit": 50,
            "fields": "id,title,thumbnail_720_url,thumbnail_url,duration,views_total,owner.screenname,owner.username"
        }
        resp = requests.get("https://api.dailymotion.com/videos", params=params, timeout=15)
        rs = resp.json().get("list", [])
        if not rs:
            xbmcgui.Dialog().ok("Tendencias", "No se han encontrado vídeos en tendencia.")
            xbmcplugin.endOfDirectory(h); return
        xbmcplugin.setContent(h, 'videos')
        watched_ids = _get_watched_ids()
        addon = xbmcaddon.Addon()
        ai = addon.getAddonInfo('icon')
        for i, v in enumerate(rs):
            vt = v.get("title", "Video")
            vi = v.get("id")
            ow = v.get("owner.screenname") or v.get("owner.username", "")
            du = 0
            try: du = int(v.get("duration", 0))
            except (ValueError, TypeError): pass
            vw = 0
            try: vw = int(v.get("views_total", 0))
            except (ValueError, TypeError): pass
            dth = v.get("thumbnail_720_url") or v.get("thumbnail_url") or ai
            is_watched = vi in watched_ids if vi else False
            watched_mark = "[COLOR lime][V][/COLOR] " if is_watched else ""
            label = "[COLOR orange]{0}.[/COLOR] {1}{2}".format(i + 1, watched_mark, vt)
            li = xbmcgui.ListItem(label=label)
            li.setArt({'thumb': dth, 'icon': dth})
            li.setInfo('video', {'title': vt, 'plot': "Canal: {0}\nVistas: {1:,}\nDuración: {2}m {3}s".format(ow, vw, du // 60, du % 60), 'duration': du})
            li.setProperty("IsPlayable", "true")
            cm = []
            if vi:
                cm.append(("Descargar Video", "RunPlugin({0})".format(_u(action="download_video", vid=vi, title=vt))))
                cm.append(("Abrir en navegador", "RunPlugin({0})".format(_u(action="dm_open_browser", url=vi))))
                cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vi)))))
                fav_params = json.dumps({"q": vt, "ot": dth})
                cm.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(_u(action='add_favorite', title=vt, fav_url='lfr_{0}'.format(vi), icon=dth, platform='tendencias', fav_action='lfr', params=fav_params))))
            li.addContextMenuItems(cm)
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="pv", vid=vi), listitem=li, isFolder=False)
        xbmcplugin.endOfDirectory(h)
    except Exception as e:
        xbmcgui.Dialog().ok("Error", "Error al cargar tendencias:\n{0}".format(str(e)))
        try: xbmcplugin.endOfDirectory(h)
        except Exception: pass




def _multi_search():
    kb = xbmc.Keyboard('', 'Términos separados por comas')
    kb.doModal()
    if not kb.isConfirmed(): return
    raw = kb.getText().strip()
    if not raw: return
    terms = [t.strip() for t in raw.split(',') if t.strip()]
    if not terms:
        xbmcgui.Dialog().notification("EspaTV", "No se encontraron términos válidos", xbmcgui.NOTIFICATION_WARNING)
        return
    if len(terms) > 10:
        terms = terms[:10]
        xbmcgui.Dialog().notification("EspaTV", "Limitado a 10 términos", xbmcgui.NOTIFICATION_INFO)
    h = int(sys.argv[1])
    dp = xbmcgui.DialogProgress()
    dp.create("Búsqueda Múltiple", "Buscando {0} términos...".format(len(terms)))
    all_results = []
    seen_ids = set()
    try:
        for idx, term in enumerate(terms):
            if dp.iscanceled(): break
            dp.update(int((idx / len(terms)) * 100), "Buscando: {0}".format(term))
            try:
                params = {"search": term, "sort": "relevance", "language": "es", "limit": 10,
                          "fields": "id,title,thumbnail_720_url,thumbnail_url,duration,views_total,owner.screenname,owner.username"}
                resp = requests.get("https://api.dailymotion.com/videos", params=params, timeout=15)
                for item in resp.json().get("list", []):
                    vid = item.get("id", "")
                    if vid and vid not in seen_ids:
                        seen_ids.add(vid)
                        all_results.append((term, item))
            except Exception: pass
    finally:
        dp.close()
    if not all_results:
        xbmcgui.Dialog().ok("Búsqueda Múltiple", "No se encontraron resultados para ningún término.")
        xbmcplugin.endOfDirectory(h); return
    addon = xbmcaddon.Addon()
    ai = addon.getAddonInfo('icon')
    for term, item in all_results:
        vid = item.get("id", "")
        title = item.get("title", "Sin título")
        thumb = item.get("thumbnail_720_url") or item.get("thumbnail_url", "") or ai
        dur = 0
        try: dur = int(item.get("duration", 0))
        except (ValueError, TypeError): pass
        owner = item.get("owner.screenname") or item.get("owner.username", "")
        views = 0
        try: views = int(item.get("views_total", 0))
        except (ValueError, TypeError): pass
        dur_min = dur // 60 if dur else 0
        label = "[B]{0}[/B] [COLOR gray]({1})[/COLOR]".format(title, term)
        if dur_min: label += " [COLOR cyan]{0}min[/COLOR]".format(dur_min)
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': thumb, 'thumb': thumb})
        li.setInfo('video', {'plot': "Canal: {0}\nBúsqueda: {1}\nDuración: {2}min\nVisitas: {3:,}".format(owner, term, dur_min, views), 'duration': dur})
        li.setProperty("IsPlayable", "true")
        cm = []
        cm.append(("Descargar Video", "RunPlugin({0})".format(_u(action='download_video', vid=vid, title=title))))
        cm.append(("Abrir en navegador", "RunPlugin({0})".format(_u(action='dm_open_browser', url=vid))))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vid)))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="pv", vid=vid), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)
    xbmcgui.Dialog().notification("Búsqueda Múltiple", "{0} resultados de {1} términos".format(len(all_results), len(terms)))




def _dm_open_browser(vid):
    vid = str(vid).replace('"', '').replace("'", '').strip() if vid else ""
    if not vid: return
    url = "https://www.dailymotion.com/video/{0}".format(vid)
    try:
        if xbmc.getCondVisibility("System.Platform.Android"):
            xbmc.executebuiltin('StartAndroidActivity("","android.intent.action.VIEW","","{0}")'.format(url))
        else:
            import webbrowser
            webbrowser.open(url)
        xbmcgui.Dialog().notification("EspaTV", "Abriendo en el navegador...", xbmcgui.NOTIFICATION_INFO, 2000)
    except Exception:
        xbmcgui.Dialog().ok("Abrir en navegador", "No se pudo abrir automáticamente.\n\nAbre esta URL:\n{0}".format(url))


def _copy_url(url):
    """Guarda una URL en el portapapeles interno de la sesión de Kodi."""
    url = str(url).strip() if url else ""
    if not url:
        return
    try:
        xbmcgui.Window(10000).setProperty("espatv.clipboard.url", url)
    except Exception:
        xbmc.log("EspaTV: Fallo al escribir en portapapeles (Window 10000 unavailable).", xbmc.LOGWARNING)
        return
    display = (url[:60] + "...") if len(url) > 60 else url
    xbmcgui.Dialog().notification(
        "EspaTV", "URL copiada: {0}".format(display),
        xbmcgui.NOTIFICATION_INFO, 2500
    )

def _sanitize_log_line(line):
    line = re.sub(r'(access_token=)[^&\s]+', lambda m: m.group(1) + '***', line)
    line = re.sub(r'(bearer=)[^&\s]+', lambda m: m.group(1) + '***', line)
    line = re.sub(r'(hdnts=)[^&\s]+', lambda m: m.group(1) + '***', line)
    line = re.sub(r'(token=)[^&\s]+', lambda m: m.group(1) + '***', line)
    line = re.sub(r'(Authorization:\s*Bearer\s+)\S+', lambda m: m.group(1) + '***', line, flags=re.IGNORECASE)
    return line

def _view_kodi_log():
    try:
        log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
        if not os.path.exists(log_path):
            xbmcgui.Dialog().ok("EspaTV", "No se encontró el archivo de log de Kodi.")
            return
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        last_lines = lines[-100:]
        espa_lines = [_sanitize_log_line(l.rstrip()) for l in last_lines if 'EspaTV' in l.lower() or 'espakodi' in l.lower()]
        if not espa_lines:
            espa_lines = [_sanitize_log_line(l.rstrip()) for l in last_lines[-30:]]
        text = "\n".join(espa_lines[-50:])
        xbmcgui.Dialog().textviewer("Log de Kodi (EspaTV)", text)
    except Exception as e:
        xbmcgui.Dialog().ok("Error", "No se pudo leer el log:\n{0}".format(e))

def _view_kodi_log_full():
    """Muestra las últimas N líneas del log de Kodi sin filtrar."""
    try:
        log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
        if not os.path.exists(log_path):
            xbmcgui.Dialog().ok("EspaTV", "No se encontró el archivo de log de Kodi.")
            return
        opts = ["Últimas 50 líneas", "Últimas 100 líneas", "Últimas 200 líneas"]
        counts = [50, 100, 200]
        sel = xbmcgui.Dialog().select("¿Cuántas líneas?", opts)
        if sel < 0: return
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        text = "".join([_sanitize_log_line(l) for l in lines[-counts[sel]:]])
        xbmcgui.Dialog().textviewer("Log de Kodi (completo)", text)
    except Exception as e:
        xbmcgui.Dialog().ok("Error", "No se pudo leer el log:\n{0}".format(e))

def _view_kodi_log_errors():
    """Muestra solo las líneas de error y warning del log."""
    try:
        log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
        if not os.path.exists(log_path):
            xbmcgui.Dialog().ok("EspaTV", "No se encontró el archivo de log de Kodi.")
            return
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        error_lines = [_sanitize_log_line(l.rstrip()) for l in lines[-500:]
                       if ' error ' in l.lower() or 'warning' in l.lower() or 'exception' in l.lower() or 'traceback' in l.lower()]
        if not error_lines:
            xbmcgui.Dialog().ok("EspaTV", "No se encontraron errores recientes en el log.")
            return
        text = "\n".join(error_lines[-80:])
        xbmcgui.Dialog().textviewer("Errores del Log de Kodi", text)
    except Exception as e:
        xbmcgui.Dialog().ok("Error", "No se pudo leer el log:\n{0}".format(e))

def _search_kodi_log():
    """Busca un texto en el log de Kodi."""
    kb = xbmc.Keyboard('', 'Buscar en el log de Kodi')
    kb.doModal()
    if not kb.isConfirmed() or not kb.getText().strip():
        return
    term = kb.getText().strip().lower()
    try:
        log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
        if not os.path.exists(log_path):
            xbmcgui.Dialog().ok("EspaTV", "No se encontró el archivo de log.")
            return
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        matches = [_sanitize_log_line(l.rstrip()) for l in lines if term in l.lower()]
        if not matches:
            xbmcgui.Dialog().ok("EspaTV", "No se encontró '{0}' en el log.".format(kb.getText().strip()))
            return
        header = "{0} resultados para '{1}'".format(len(matches), kb.getText().strip())
        text = "\n".join(matches[-80:])
        xbmcgui.Dialog().textviewer(header, text)
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))

def _log_info():
    """Muestra información sobre el log de Kodi y el log interno."""
    try:
        log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
        msg = "[B]Log de Kodi[/B]\n"
        if os.path.exists(log_path):
            size = os.path.getsize(log_path)
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                total_lines = sum(1 for _ in f)
            if size > 1048576:
                msg += "Tamaño: {0:.1f} MB\n".format(size / 1048576)
            else:
                msg += "Tamaño: {0:.1f} KB\n".format(size / 1024)
            msg += "Líneas: {0:,}\n".format(total_lines)
            msg += "Ruta: {0}\n".format(log_path)
        else:
            msg += "No encontrado\n"
        msg += "\n[B]Log Interno EspaTV[/B]\n"
        base_dir = os.path.dirname(os.path.abspath(__file__))
        err_log = os.path.join(base_dir, 'EspaTV_errors.log')
        if os.path.exists(err_log):
            esize = os.path.getsize(err_log)
            with open(err_log, 'r', encoding='utf-8', errors='replace') as f:
                elines = sum(1 for _ in f)
            msg += "Tamaño: {0:.1f} KB\n".format(esize / 1024)
            msg += "Líneas: {0:,}\n".format(elines)
            msg += "Debug: {0}".format("ACTIVADO" if _is_debug_active() else "DESACTIVADO")
        else:
            msg += "No existe (activa Debug para generarlo)"
        xbmcgui.Dialog().textviewer("Información de Logs", msg)
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))

def _export_log():
    """Exporta el log a un archivo TXT."""
    try:
        log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
        if not os.path.exists(log_path):
            xbmcgui.Dialog().ok("EspaTV", "No se encontró el archivo de log de Kodi.")
            return
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        opts = ["Solo entradas de EspaTV", "Log completo (últimas 500 líneas)", "Solo errores y warnings"]
        sel = xbmcgui.Dialog().select("¿Qué exportar?", opts)
        if sel < 0: return
        if sel == 0:
            filtered = [_sanitize_log_line(l) for l in lines if 'EspaTV' in l.lower() or 'espakodi' in l.lower()]
            suffix = "EspaTV"
        elif sel == 1:
            filtered = [_sanitize_log_line(l) for l in lines[-500:]]
            suffix = "completo"
        else:
            filtered = [_sanitize_log_line(l) for l in lines if ' error ' in l.lower() or 'warning' in l.lower() or 'exception' in l.lower() or 'traceback' in l.lower()]
            suffix = "errores"
        if not filtered:
            xbmcgui.Dialog().ok("EspaTV", "No hay entradas que exportar.")
            return
        d = xbmcgui.Dialog().browse(3, 'Guardar log exportado', 'files')
        if not d: return
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = "kodi_log_{0}_{1}.txt".format(suffix, ts)
        if os.path.isdir(d):
            dest = os.path.join(d, fname)
        else:
            dest = d if d.lower().endswith('.txt') else d + '.txt'
        with open(dest, 'w', encoding='utf-8') as f:
            f.write("Log exportado por EspaTV -- {0}\n".format(time.strftime('%d/%m/%Y %H:%M:%S')))
            f.write("Tipo: {0}\n".format(opts[sel]))
            f.write("Líneas: {0}\n".format(len(filtered)))
            f.write("=" * 60 + "\n\n")
            f.writelines(filtered)
        xbmcgui.Dialog().ok("Exportado", "Log guardado en:\n{0}\n\n{1} líneas exportadas.".format(dest, len(filtered)))
    except Exception as e:
        xbmcgui.Dialog().ok("Error", "No se pudo exportar:\n{0}".format(e))

def _clear_error_log():
    """Elimina el log interno de errores."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = os.path.join(base_dir, 'EspaTV_errors.log')
    if not os.path.exists(log_file):
        xbmcgui.Dialog().ok("EspaTV", "No existe el archivo de log interno.")
        return
    size_kb = os.path.getsize(log_file) / 1024
    if xbmcgui.Dialog().yesno("EspaTV", "¿Eliminar el log interno?\n\nTamaño: {0:.1f} KB".format(size_kb)):
        try:
            os.remove(log_file)
            xbmcgui.Dialog().notification("EspaTV", "Log interno eliminado", xbmcgui.NOTIFICATION_INFO)
            xbmc.executebuiltin("Container.Refresh")
        except Exception as e:
            xbmcgui.Dialog().ok("Error", str(e))

def _view_log_visual():
    """Visor de log visual: cada linea es un item con colores por severidad."""
    log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
    if not os.path.exists(log_path):
        xbmcgui.Dialog().ok("EspaTV", "No se encontró el archivo de log de Kodi.")
        return

    opts = ["Últimas 50 líneas", "Últimas 100 líneas", "Últimas 200 líneas", "Solo EspaTV", "Solo errores"]
    sel = xbmcgui.Dialog().select("Modo de vista", opts)
    if sel < 0: return

    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))
        return

    if sel == 0:
        lines = all_lines[-50:]
    elif sel == 1:
        lines = all_lines[-100:]
    elif sel == 2:
        lines = all_lines[-200:]
    elif sel == 3:
        lines = [l for l in all_lines if 'EspaTV' in l.lower() or 'espakodi' in l.lower()][-100:]
    else:
        lines = [l for l in all_lines if ' error ' in l.lower() or 'exception' in l.lower() or 'traceback' in l.lower()][-100:]

    if not lines:
        xbmcgui.Dialog().ok("EspaTV", "No hay entradas que mostrar.")
        return

    h = int(sys.argv[1])
    for i, raw in enumerate(lines):
        line = _sanitize_log_line(raw.rstrip())
        ll = line.lower()


        if ' error ' in ll or 'exception' in ll or 'traceback' in ll:
            sev = 'error'
        elif 'warning' in ll:
            sev = 'warning'
        elif 'EspaTV' in ll or 'espakodi' in ll:
            sev = 'espa'
        elif ' debug ' in ll:
            sev = 'debug'
        else:
            sev = 'info'

        # Extraer timestamp si existe (formato: 2026-03-16 23:05:26.421)
        ts_display = ""
        if len(line) > 23 and line[4] == '-' and line[10] == ' ':
            ts_display = line[11:19]  # HH:MM:SS
            body = line[23:].strip()
    
            if body.startswith("T:"):
                space_idx = body.find(' ')
                if space_idx > 0:
                    body = body[space_idx:].strip()
    
            for tag in ['info <general>:', 'error <general>:', 'warning <general>:', 'debug <general>:', 'notice <general>:']:
                if body.lower().startswith(tag):
                    body = body[len(tag):].strip()
                    break
        else:
            body = line


        if len(body) > 120:
            label_text = body[:117] + "..."
        else:
            label_text = body


        if sev == 'error':
            label = "[COLOR red][B]X[/B][/COLOR] "
            if ts_display:
                label += "[COLOR grey]{0}[/COLOR] ".format(ts_display)
            label += "[COLOR red]{0}[/COLOR]".format(label_text)
            icon = 'DefaultIconError.png'
        elif sev == 'warning':
            label = "[COLOR yellow][B]![/B][/COLOR] "
            if ts_display:
                label += "[COLOR grey]{0}[/COLOR] ".format(ts_display)
            label += "[COLOR yellow]{0}[/COLOR]".format(label_text)
            icon = 'DefaultIconWarning.png'
        elif sev == 'espa':
            label = "[COLOR limegreen][B]*[/B][/COLOR] "
            if ts_display:
                label += "[COLOR grey]{0}[/COLOR] ".format(ts_display)
            label += "[COLOR limegreen]{0}[/COLOR]".format(label_text)
            icon = 'DefaultIconInfo.png'
        elif sev == 'debug':
            label = "[COLOR grey]- "
            if ts_display:
                label += "{0} ".format(ts_display)
            label += "{0}[/COLOR]".format(label_text)
            icon = 'DefaultIconInfo.png'
        else:
            label = "[COLOR white]-[/COLOR] "
            if ts_display:
                label += "[COLOR grey]{0}[/COLOR] ".format(ts_display)
            label += "{0}".format(label_text)
            icon = 'DefaultIconInfo.png'

        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': icon})
        li.setInfo('video', {'plot': line})
        encoded = base64.b64encode(line.encode('utf-8')).decode('ascii')
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="show_log_line", data=encoded), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(h)

def _show_log_line(data):
    """Muestra una linea del log completa en un textviewer."""
    try:
        line = base64.b64decode(data).decode('utf-8')
        xbmcgui.Dialog().textviewer("Detalle del Log", line)
    except Exception:
        xbmcgui.Dialog().ok("Error", "No se pudo decodificar la entrada.")

def _log_menu():
    h = int(sys.argv[1])

    li = xbmcgui.ListItem(label="[COLOR limegreen]Log de EspaTV (filtrado)[/COLOR]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.setInfo('video', {'plot': "Muestra las últimas entradas del log de Kodi que mencionan EspaTV.\nTokens y datos sensibles se censuran automáticamente."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_kodi_log"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR magenta][B]Visor del Log[/B][/COLOR]")
    li.setArt({'icon': 'DefaultPicture.png'})
    li.setInfo('video', {'plot': "Muestra cada línea del log como un elemento visual individual.\nCodificado por colores según severidad:\n\n[COLOR red]X Error[/COLOR]\n[COLOR yellow]! Warning[/COLOR]\n[COLOR limegreen]* EspaTV[/COLOR]\n[COLOR white]- Info[/COLOR]\n[COLOR grey]- Debug[/COLOR]\n\nPermite hacer scroll por las entradas del log."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_log_visual"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="Log de Kodi (completo)")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.setInfo('video', {'plot': "Muestra las últimas líneas del log completo de Kodi, sin filtrar.\nPuedes elegir cuántas líneas ver."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_kodi_log_full"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR red]Solo Errores y Warnings[/COLOR]")
    li.setArt({'icon': 'DefaultIconError.png'})
    li.setInfo('video', {'plot': "Filtra el log para mostrar solo líneas de error, warning, exception y traceback."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_kodi_log_errors"), listitem=li, isFolder=False)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    err_log = os.path.join(base_dir, 'EspaTV_errors.log')
    if os.path.exists(err_log):
        esize = os.path.getsize(err_log) / 1024
        li = xbmcgui.ListItem(label="[COLOR gold]Log Interno ({0:.0f} KB)[/COLOR]".format(esize))
        li.setArt({'icon': 'DefaultIconInfo.png'})
        li.setInfo('video', {'plot': "Lee el archivo EspaTV_errors.log generado por el Modo Debug.\nPuedes elegir cuántas líneas ver."})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="view_error_log"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR cyan]Buscar en el Log[/COLOR]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.setInfo('video', {'plot': "Escribe un texto para buscar en todo el log de Kodi.\nMuestra todas las líneas que coinciden."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="search_kodi_log"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Exportar Log a archivo")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Guarda el log filtrado, completo o de errores en un archivo TXT.\nÚtil para compartir o enviar a soporte."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="export_log"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Información del Log")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.setInfo('video', {'plot': "Muestra el tamaño, número de líneas y ruta del log de Kodi y del log interno."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="log_info"), listitem=li, isFolder=False)

    if os.path.exists(err_log):
        li = xbmcgui.ListItem(label="[COLOR red]Borrar Log Interno[/COLOR]")
        li.setArt({'icon': 'DefaultIconError.png'})
        li.setInfo('video', {'plot': "Elimina el archivo EspaTV_errors.log para liberar espacio."})
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="clear_error_log"), listitem=li, isFolder=False)

    # LoioLog — Gestor de Logs avanzado
    if xbmc.getCondVisibility("System.HasAddon(plugin.program.loiolog)"):
        ll_label = "LoioLog: [COLOR green]INSTALADO[/COLOR]"
        ll_action = "open_loiolog"
    else:
        ll_label = "LoioLog: [COLOR grey]NO INSTALADO[/COLOR]"
        ll_action = "loiolog_install"
    li = xbmcgui.ListItem(label=ll_label)
    li.setArt({'icon': 'DefaultAddonProgram.png'})
    li.setInfo('video', {'plot': "LoioLog es un gestor avanzado de logs para Kodi.\nPermite ver, filtrar, buscar, analizar y exportar los logs del sistema.\n\nSi no está instalado, pulsa para descargarlo e instalarlo."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action=ll_action), listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(h)

def _open_flowfav():
    if xbmc.getCondVisibility("System.HasAddon(plugin.program.flowfavmanager)"):
        xbmc.executebuiltin("RunAddon(plugin.program.flowfavmanager)")
    else:
        xbmcgui.Dialog().ok("EspaTV",
            "Flow FavManager no está instalado.\n\n"
            "Descárgalo desde:\nhttps://github.com/loioloio/flowfav")

def _flowfav_install():
    """Instalación automática de Flow FavManager desde GitHub."""
    _FLOWFAV_API_URL = "https://api.github.com/repos/loioloio/flowfav/releases/latest"
    _FLOWFAV_ADDON_ID = "plugin.program.flowfavmanager"

    sel = xbmcgui.Dialog().select("Flow FavManager", [
        "Instalar automáticamente",
        "Ver instrucciones manuales"
    ])
    if sel < 0:
        return
    if sel == 1:
        xbmcgui.Dialog().ok("Instrucciónes de instalación",
            "1. Ves a https://github.com/loioloio/flowfav\n"
            "2. Descarga el archivo ZIP de la última release\n"
            "3. En Kodi: Ajustes > Addons > Instalar desde ZIP\n"
            "4. Selecciona el ZIP descargado")
        return

    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Descargando Flow FavManager...")
    zip_path = None
    try:
        dp.update(5, "Buscando última versión...")
        api_r = requests.get(_FLOWFAV_API_URL, timeout=15)
        if api_r.status_code != 200:
            raise Exception(f"No se pudo consultar GitHub (HTTP {api_r.status_code})")
        assets = api_r.json().get("assets", [])
        zip_url = None
        for asset in assets:
            name = asset.get("name", "")
            if "_ML" in name and name.endswith(".zip"):
                zip_url = asset.get("browser_download_url")
                break
        if not zip_url:
            raise Exception("No se encontró el ZIP en la última release")
        dp.update(10, "Descargando desde GitHub...")
        r = requests.get(zip_url, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}")
        if len(r.content) < 4 or r.content[:2] != b'PK':
            raise Exception("El archivo descargado no es un ZIP válido")
        zip_path = os.path.join(xbmcvfs.translatePath("special://temp/"), "flowfav.zip")
        with open(zip_path, "wb") as f:
            f.write(r.content)
        dp.update(50, "Extrayendo addon...")
        addons_dir = xbmcvfs.translatePath("special://home/addons/")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for entry in zf.namelist():
                resolved = os.path.realpath(os.path.join(addons_dir, entry))
                if not resolved.startswith(os.path.realpath(addons_dir)):
                    raise Exception(f"ZIP contiene ruta sospechosa: {entry}")
            zf.extractall(addons_dir)
        dp.update(80, "Actualizando addons locales...")
        xbmc.executebuiltin("UpdateLocalAddons()")
        xbmc.sleep(2000)
        xbmc.executebuiltin(f"EnableAddon({_FLOWFAV_ADDON_ID})")
        xbmc.sleep(1000)
        dp.close()
        xbmcgui.Dialog().notification("EspaTV", "Flow FavManager instalado correctamente", xbmcgui.NOTIFICATION_INFO, 5000)
        xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        dp.close()
        _log_error(f"Error instalando FlowFav: {e}")
        choice = xbmcgui.Dialog().yesno("Error de instalación",
            f"No se pudo instalar automáticamente.\n\nError: {e}\n\n"
            "Puede que necesites activar 'Orígenes desconocidos' en Ajustes.\n"
            "¿Abrir Ajustes del Sistema?")
        if choice:
            xbmc.executebuiltin("ActivateWindow(systemsettings)")
    finally:
        if zip_path and os.path.exists(zip_path):
            try: os.remove(zip_path)
            except Exception: pass

def _open_loiolog():
    if xbmc.getCondVisibility("System.HasAddon(plugin.program.loiolog)"):
        xbmc.executebuiltin("RunAddon(plugin.program.loiolog)")
    else:
        xbmcgui.Dialog().ok("EspaTV",
            "LoioLog no está instalado.\n\n"
            "Descárgalo desde:\nhttps://github.com/loioloio/loiolog")

def _loiolog_install():
    """Instalación automática de LoioLog desde GitHub."""
    _LOIOLOG_API_URL = "https://api.github.com/repos/loioloio/loiolog/releases/latest"
    _LOIOLOG_ADDON_ID = "plugin.program.loiolog"

    if xbmc.getCondVisibility("System.HasAddon(plugin.program.loiolog)"):
        sel = xbmcgui.Dialog().select("LoioLog", [
            "Abrir LoioLog",
            "Reinstalar desde GitHub"
        ])
        if sel == 0:
            xbmc.executebuiltin("RunAddon(plugin.program.loiolog)")
            return
        elif sel < 0:
            return
    else:
        sel = xbmcgui.Dialog().select("LoioLog — Gestor de Logs", [
            "Instalar automáticamente",
            "Ver instrucciones manuales"
        ])
        if sel < 0:
            return
        if sel == 1:
            xbmcgui.Dialog().ok("Instrucciones de instalación",
                "1. Ve a https://github.com/loioloio/loiolog\n"
                "2. Descarga el archivo ZIP de la última release\n"
                "3. En Kodi: Ajustes > Addons > Instalar desde ZIP\n"
                "4. Selecciona el ZIP descargado")
            return

    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Descargando LoioLog...")
    zip_path = None
    try:
        dp.update(5, "Buscando última versión...")
        api_r = requests.get(_LOIOLOG_API_URL, timeout=15)
        if api_r.status_code != 200:
            raise Exception(f"No se pudo consultar GitHub (HTTP {api_r.status_code})")
        assets = api_r.json().get("assets", [])
        zip_url = None
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(".zip"):
                zip_url = asset.get("browser_download_url")
                break
        if not zip_url:
            raise Exception("No se encontró el ZIP en la última release")
        dp.update(10, "Descargando desde GitHub...")
        r = requests.get(zip_url, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            raise Exception("HTTP {0}".format(r.status_code))
        if len(r.content) < 4 or r.content[:2] != b'PK':
            raise Exception("El archivo descargado no es un ZIP válido")
        zip_path = os.path.join(xbmcvfs.translatePath("special://temp/"), "loiolog.zip")
        with open(zip_path, "wb") as f:
            f.write(r.content)
        dp.update(50, "Extrayendo addon...")
        addons_dir = xbmcvfs.translatePath("special://home/addons/")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for entry in zf.namelist():
                resolved = os.path.realpath(os.path.join(addons_dir, entry))
                if not resolved.startswith(os.path.realpath(addons_dir)):
                    raise Exception(f"ZIP contiene ruta sospechosa: {entry}")
            zf.extractall(addons_dir)
        dp.update(80, "Actualizando addons locales...")
        xbmc.executebuiltin("UpdateLocalAddons()")
        xbmc.sleep(2000)
        xbmc.executebuiltin(f"EnableAddon({_LOIOLOG_ADDON_ID})")
        xbmc.sleep(1000)
        dp.close()
        xbmcgui.Dialog().notification("EspaTV", "LoioLog instalado correctamente", xbmcgui.NOTIFICATION_INFO, 5000)
        xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        dp.close()
        _log_error(f"Error instalando LoioLog: {e}")
        choice = xbmcgui.Dialog().yesno("Error de instalación",
            f"No se pudo instalar automáticamente.\n\nError: {e}\n\n"
            "Puede que necesites activar 'Orígenes desconocidos' en Ajustes.\n"
            "¿Abrir Ajustes del Sistema?")
        if choice:
            xbmc.executebuiltin("ActivateWindow(systemsettings)")
    finally:
        if zip_path and os.path.exists(zip_path):
            try: os.remove(zip_path)
            except Exception: pass

def _open_atresdaily():
    if xbmc.getCondVisibility("System.HasAddon(plugin.video.atresdaily)"):
        xbmc.executebuiltin("RunAddon(plugin.video.atresdaily)")
    else:
        xbmcgui.Dialog().ok("AtresDaily",
            "AtresDaily no está instalado.\n\n"
            "Para instalarlo:\n"
            "1. Ajustes > Explorador de archivos > Añadir fuente\n"
            "2. Escribe: https://fullstackcurso.github.io/atresdaily/\n"
            "3. Instalar desde ZIP > atresdaily > repository.atresdaily-1.0.1.zip\n"
            "4. Instalar desde repositorio > AtresDaily Repository")

def _open_espadaily():
    if xbmc.getCondVisibility("System.HasAddon(plugin.video.espadaily)"):
        xbmc.executebuiltin("RunAddon(plugin.video.espadaily)")
    else:
        xbmcgui.Dialog().ok("EspaDaily",
            "EspaDaily no está instalado.\n\n"
            "Para instalarlo:\n"
            "1. Ajustes > Explorador de archivos > Añadir fuente\n"
            "2. Escribe: https://fullstackcurso.github.io/espadaily/\n"
            "3. Instalar desde ZIP > espadaily > repository.espadaily-1.0.2.zip\n"
            "4. Instalar desde repositorio > EspaDaily Repository")

def _set_cache_config():
    cfg = _load_dm_settings()
    current = cfg.get('cache_expiry', 0)
    options = [
        "Apagado" + (" [ACTIVO]" if current == 0 else ""),
        "1 día" + (" [ACTIVO]" if current == 86400 else ""),
        "1 semana" + (" [ACTIVO]" if current == 604800 else ""),
        "1 mes" + (" [ACTIVO]" if current == 2592000 else ""),
        "Indefinido" + (" [ACTIVO]" if current == -1 else ""),
    ]
    vals = [0, 86400, 604800, 2592000, -1]
    sel = xbmcgui.Dialog().select("Duración de la caché", options)
    if sel < 0: return
    cfg['cache_expiry'] = vals[sel]
    _save_dm_settings(cfg)
    xbmcgui.Dialog().notification("EspaTV", "Caché: {0}".format(options[sel].replace(" [ACTIVO]", "")), xbmcgui.NOTIFICATION_INFO)

def _toggle_iptv_cache():
    current = core_settings.is_iptv_cache_active()
    new_state = not current
    core_settings.set_iptv_cache_active(new_state)
    if new_state:
        xbmcgui.Dialog().notification("EspaTV", "Caché IPTV ACTIVADA", xbmcgui.NOTIFICATION_INFO)
    else:
        core_settings.clear_iptv_cache_files()
        xbmcgui.Dialog().notification("EspaTV", "Caché IPTV DESACTIVADA", xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _set_iptv_cache_ttl_dialog():
    options = []
    current_ttl = core_settings.get_iptv_cache_ttl()
    for val, name in core_settings._IPTV_CACHE_TTL_OPTIONS:
        if val == 0:
            continue  # no mostrar "Desactivada", para eso está el toggle
        label = name
        if val == current_ttl:
            label += " [ACTIVO]"
        options.append((val, label))
    sel = xbmcgui.Dialog().select("Duración de caché IPTV", [o[1] for o in options])
    if sel < 0:
        return
    core_settings.set_iptv_cache_ttl(options[sel][0])
    core_settings.clear_iptv_cache_files()  # limpiar al cambiar TTL
    xbmcgui.Dialog().notification("EspaTV", "Caché: {0}".format(options[sel][1].replace(" [ACTIVO]", "")), xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def _reset_iptv_cache():
    if not xbmcgui.Dialog().yesno("EspaTV",
        "¿Restaurar la caché IPTV por defecto?\n\n"
        "Se desactivará la caché y se borrarán "
        "todos los archivos guardados.\n"
        "Los canales se descargarán de internet cada vez que entres, como venía de fábrica."):
        return
    core_settings.reset_iptv_cache_defaults()
    xbmcgui.Dialog().notification("EspaTV", "Caché restaurada por defecto", xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")


def _set_download_path():
    path = xbmcgui.Dialog().browse(0, 'Seleccionar carpeta de descargas', 'files')
    if not path: return
    cfg = _load_dm_settings()
    cfg['download_path'] = path
    _save_dm_settings(cfg)
    xbmcgui.Dialog().notification("EspaTV", "Ruta configurada", xbmcgui.NOTIFICATION_INFO)




def _get_backup_dir():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    d = os.path.join(p, 'backups')
    if not os.path.exists(d): os.makedirs(d)
    return d

def _create_backup_full():
    profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(profile): os.makedirs(profile)


    groups = [
        ("Favoritos", ['favorites.json']),
        ("Categorías Personalizadas", ['custom_categories.json']),
        ("Historial de Búsquedas DM", ['search_history.json']),
        ("Historial de Búsquedas YouTube", ['yt_search_history.json']),
        ("Historial de Visionado", ['watch_history.json']),
        ("Historial de URLs", ['url_history.json', 'url_bookmarks.json']),
        ("Playlists DM Guardadas", ['saved_playlists.json']),
        ("Playlists YouTube Guardadas", ['yt_playlists.json']),
        ("Historial de Descargas", ['downloads_history.json']),
        ("Configuración General", ['settings.json']),
        ("Ajustes Dailymotion", ['dm_settings.json']),
        ("Configuración Trakt", ['trakt_settings.json']),
        ("Listas IPTV Personalizadas", ['custom_iptv.json']),

    ]


    available_groups = []
    available_labels = []
    for label, files in groups:
        if files == '__cache__':
            has = any(f.startswith("cat_cache_") and f.endswith(".json") for f in os.listdir(profile))
        else:
            has = any(os.path.exists(os.path.join(profile, f)) for f in files)
        if has:
            available_groups.append((label, files))
            available_labels.append(label)

    if not available_labels:
        xbmcgui.Dialog().ok("Backup", "No se encontraron datos que respaldar.\n\nEl perfil del addon aún no tiene archivos guardados.")
        return


    selected = xbmcgui.Dialog().multiselect("¿Qué incluir en el backup?", available_labels,
                                            preselect=list(range(len(available_labels))))
    if selected is None or len(selected) == 0:
        return


    ts = time.strftime("%Y%m%d_%H%M%S")
    default_name = "EspaTV_backup_{0}.zip".format(ts)
    loc_opts = ["Guardar en carpeta del addon (por defecto)", "Elegir ubicación"]
    loc_sel = xbmcgui.Dialog().select("¿Dónde guardar?", loc_opts)
    if loc_sel < 0:
        return
    if loc_sel == 1:
        d = xbmcgui.Dialog().browse(3, 'Guardar Backup', 'files', '', False, False, default_name)
        if not d:
            return
        if os.path.isdir(d):
            target = os.path.join(d, default_name)
        elif not d.lower().endswith(".zip"):
            target = d + ".zip"
        else:
            target = d
    else:
        bdir = _get_backup_dir()
        target = os.path.join(bdir, default_name)

    dp = xbmcgui.DialogProgress()
    dp.create("Backup", "Creando copia de seguridad...")
    try:
        added = []
        chosen = [available_groups[i] for i in selected]

        dp.update(20, "Empaquetando datos...")
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
            for label, files in chosen:
                if files == '__cache__':
                    for cf in os.listdir(profile):
                        if cf.startswith("cat_cache_") and cf.endswith(".json"):
                            cfp = os.path.join(profile, cf)
                            if os.path.exists(cfp):
                                zf.write(cfp, cf)
                                added.append(cf)
                else:
                    for bf in files:
                        fp = os.path.join(profile, bf)
                        if os.path.exists(fp):
                            zf.write(fp, bf)
                            added.append(bf)

        dp.update(80, "Verificando integridad...")
        if not added:
            dp.close()
            if os.path.exists(target):
                os.remove(target)
            xbmcgui.Dialog().ok("Backup", "No se guardó ningún archivo.")
            return

        with zipfile.ZipFile(target, 'r') as zf:
            bad = zf.testzip()
        dp.close()

        size_kb = os.path.getsize(target) / 1024
        msg = "Backup creado correctamente.\n\n"
        msg += "Archivos: {0}\n".format(len(added))
        msg += "Tamaño: {0:.1f} KB\n\n".format(size_kb)
        msg += "Guardado en:\n{0}".format(target)
        if bad:
            msg += "\n\n[COLOR yellow]Advertencia: algunos archivos pueden estar dañados.[/COLOR]"
        xbmcgui.Dialog().ok("Backup Completo", msg)
        xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        dp.close()
        xbmcgui.Dialog().ok("Error", str(e))

def _list_backups():
    h = int(sys.argv[1])
    bdir = _get_backup_dir()
    try:
        files = sorted([f for f in os.listdir(bdir) if f.endswith('.zip')], reverse=True)
    except Exception:
        files = []
    if not files:
        li = xbmcgui.ListItem(label="[COLOR gray]No hay copias de seguridad[/COLOR]")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
    for f in files:
        li = xbmcgui.ListItem(label=f)
        li.setArt({'icon': 'DefaultAddonRepository.png'})
        cm = [("Eliminar backup", "RunPlugin({0})".format(_u(action="delete_single_backup", file=f)))]
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="restore_backup_selective", file=f), listitem=li, isFolder=False)
    xbmcplugin.endOfDirectory(h)

def _restore_backup_selective(fname):
    source = os.path.join(_get_backup_dir(), fname)
    profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    file_labels = {
        'settings.json': 'Configuración General',
        'favorites.json': 'Favoritos',
        'search_history.json': 'Historial de Búsquedas DM',
        'yt_search_history.json': 'Historial de Búsquedas YouTube',
        'custom_categories.json': 'Categorías Personalizadas',
        'dm_settings.json': 'Ajustes Dailymotion',
        'watch_history.json': 'Historial de Visionado',
        'custom_iptv.json': 'Listas IPTV Personalizadas',
        'url_history.json': 'Historial de URLs',
        'url_bookmarks.json': 'Marcadores de URLs',
        'saved_playlists.json': 'Playlists DM Guardadas',
        'yt_playlists.json': 'Playlists YouTube Guardadas',
        'downloads_history.json': 'Historial de Descargas',
        'trakt_settings.json': 'Configuración Trakt',
    }
    mergeable = ['favorites.json', 'search_history.json', 'yt_search_history.json',
                 'watch_history.json', 'url_history.json', 'url_bookmarks.json', 'custom_iptv.json',
                 'saved_playlists.json', 'yt_playlists.json', 'downloads_history.json']
    try:
        with zipfile.ZipFile(source, 'r') as zf:
            available = zf.namelist()
        known = [f for f in available if f in file_labels]
    
        cache_files = [f for f in available if f.startswith("cat_cache_") and f.endswith(".json")]
        if cache_files:
            known.append('__cache__')
            file_labels['__cache__'] = 'Caché de Categorías ({0})'.format(len(cache_files))
    

        if not known:
            xbmcgui.Dialog().ok("EspaTV", "Este backup no contiene datos reconocidos."); return
        options = [file_labels.get(f, f) for f in known]
        selected = xbmcgui.Dialog().multiselect("¿Qué quieres restaurar?", options)
        if selected is None or len(selected) == 0: return
        files_to_restore = [known[i] for i in selected]
        with zipfile.ZipFile(source, 'r') as zf:
            for fz in files_to_restore:

                if fz == '__cache__':
                    for cf in cache_files:
                        data = zf.read(cf)
                        with open(os.path.join(profile, cf), 'wb') as f_out:
                            f_out.write(data)
                    continue
                dest_path = os.path.join(profile, fz)
                if fz in mergeable and os.path.exists(dest_path):
                    choice = xbmcgui.Dialog().select(file_labels[fz], ["Sustituir (borrar actual)", "Añadir (fusionar con actual)", "Omitir"])
                    if choice == 2 or choice == -1: continue
                    elif choice == 1:
                        try:
                            with open(dest_path, 'r', encoding='utf-8') as f: current = json.load(f)
                            with zf.open(fz) as zfile: backup = json.load(zfile)
                            if isinstance(current, list) and isinstance(backup, list):
                                merged = current + [x for x in backup if x not in current]
                                with open(dest_path, 'w', encoding='utf-8') as f: json.dump(merged, f, ensure_ascii=False)
                                continue
                            elif isinstance(current, dict) and isinstance(backup, dict):
                                current.update(backup)
                                with open(dest_path, 'w', encoding='utf-8') as f: json.dump(current, f, ensure_ascii=False)
                                continue
                        except Exception: pass
                resolved = os.path.realpath(os.path.join(profile, fz))
                if not resolved.startswith(os.path.realpath(profile)):
                    continue
                zf.extract(fz, profile)
        xbmcgui.Dialog().notification("EspaTV", "Restauración completada", xbmcgui.NOTIFICATION_INFO)
    except Exception as e:
        xbmcgui.Dialog().ok("Error", "Fallo en restauración: {0}".format(e))

def _delete_single_backup(fname):
    path = os.path.join(_get_backup_dir(), fname)
    if xbmcgui.Dialog().yesno("EspaTV", "¿Eliminar '{0}'?".format(fname)):
        try:
            os.remove(path)
            xbmcgui.Dialog().notification("EspaTV", "Backup eliminado", xbmcgui.NOTIFICATION_INFO)
            xbmc.executebuiltin("Container.Refresh")
        except Exception as e:
            xbmcgui.Dialog().ok("Error", str(e))

def _export_favorites_txt():
    """Exporta la lista de favoritos a un archivo de texto plano legible."""
    favs = core_settings.get_favorites()
    if not favs:
        xbmcgui.Dialog().notification("EspaTV", "No hay favoritos para exportar", xbmcgui.NOTIFICATION_WARNING)
        return

    d = xbmcgui.Dialog().browse(3, 'Seleccionar carpeta destino', 'files')
    if not d: return

    dest = os.path.join(d, 'favoritos_EspaTV.txt')
    try:
        with open(dest, 'w', encoding='utf-8') as f:
            f.write("Favoritos EspaTV -- {0}\n".format(time.strftime('%d/%m/%Y %H:%M')))
            f.write("Total: {0} favoritos\n".format(len(favs)))
            f.write("=" * 50 + "\n\n")
            for fav in favs:
                title = fav.get('title', 'Sin titulo')
                url = fav.get('url', '')
                action = fav.get('action', '')
                f.write("{0}\n".format(title))
                if url:
                    f.write("  URL: {0}\n".format(url))
                if action:
                    f.write("  Tipo: {0}\n".format(action))
                f.write("\n")
        xbmcgui.Dialog().notification("EspaTV", "Exportados {0} favoritos a TXT".format(len(favs)))
    except Exception as e:
        xbmcgui.Dialog().ok("Error", "No se pudo exportar: {0}".format(e))

def _backups_menu():
    h = int(sys.argv[1])
    li = xbmcgui.ListItem(label="[COLOR limegreen][B]Crear Backup[/B][/COLOR]")
    li.setArt({'icon': 'DefaultAddonRepository.png'})
    li.setInfo('video', {'plot': "Crea una copia ZIP de tus datos.\nPuedes elegir qué incluir y dónde guardarla."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="create_backup_full"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Restaurar / Ver Backups")
    li.setArt({'icon': 'DefaultAddonRepository.png'})
    li.setInfo('video', {'plot': "Lista los backups creados. Permite restaurar selectivamente."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="list_backups"), listitem=li, isFolder=True)

    li = xbmcgui.ListItem(label="[COLOR gold]Importar Backup externo[/COLOR]")
    li.setArt({'icon': 'DefaultAddonRepository.png'})
    li.setInfo('video', {'plot': "Importa un archivo ZIP desde cualquier ubicación.\nPuedes elegir qué datos restaurar.\nÚtil para backups compartidos o de otro dispositivo."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="import_backup"), listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="[COLOR cyan]Exportar Favoritos a TXT[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Genera un archivo de texto plano con la lista de favoritos."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="export_favs_txt"), listitem=li, isFolder=False)



    xbmcplugin.endOfDirectory(h)




def _load_dm_settings():
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    sp = os.path.join(p, 'dm_settings.json')
    if not os.path.exists(sp):
        return {}
    try:
        with open(sp, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def _save_dm_settings(cfg):
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p):
        os.makedirs(p)
    sp = os.path.join(p, 'dm_settings.json')
    try:
        with open(sp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False)
    except IOError:
        pass

def _set_dm_safe_level():
    options = [
        "Desactivado (Conexión Normal)",
        "Básico (Móvil + Cookies)",
        "Extremo (Todas las técnicas de Gujal00)",
        "YT-DLP (TLS Spoofing - Recomendado en PC)"
    ]
    cfg = _load_dm_settings()
    current = cfg.get('dm_safe_level', 0)
    
    sel = xbmcgui.Dialog().select("Modo Anti-Errores Dailymotion", options, preselect=current)
    if sel >= 0:
        if sel == 3:
            try:
                import yt_dlp
                xbmc.log(f"[EspaTV] yt-dlp v{yt_dlp.version.__version__} disponible", xbmc.LOGINFO)
            except ImportError:
                xbmcgui.Dialog().ok("EspaTV", "yt-dlp no está instalado.\n\nInstálalo con:\npip install yt-dlp curl-cffi")
                return
        cfg['dm_safe_level'] = sel
        _save_dm_settings(cfg)
        xbmcgui.Dialog().notification("EspaTV", f"Modo Anti-Errores DM: {options[sel].split(' (')[0]}")
        xbmc.executebuiltin("Container.Refresh")

def _set_dl_mode():
    opts = [
        "Nivel 1: DIRECTO (Elegir calidad)", 
        "Nivel 2: SAFE MODE (Mejor MP4 directo)", 
        "Nivel 3: EspaTV (Descargar por HLS)",
        "Nivel 4: YT-DLP (Mejor calidad, Python 3.10+)",
        "Nivel 5: ULTRA (Mejor calidad, Python 3.10+)"
    ]
    cfg = _load_dm_settings()
    current = cfg.get('dl_mode', 0)
    idx = xbmcgui.Dialog().select("Modo de Descarga", opts, preselect=current)
    if idx >= 0:
        cfg['dl_mode'] = idx
        _save_dm_settings(cfg)
        xbmcgui.Dialog().notification("EspaTV", f"Modo descarga: Nivel {idx+1} guardado")
        xbmc.executebuiltin("Container.Refresh")

def _dm_playlists_search():
    kb = xbmc.Keyboard("", "Buscar canal de Dailymotion")
    kb.doModal()
    if not kb.isConfirmed():
        return
    query = kb.getText().strip()
    if not query:
        return
    channels = _dm_search_channels(query)
    if not channels:
        xbmcgui.Dialog().ok("EspaTV", "No se encontraron canales para: " + query)
        return
    h = int(sys.argv[1])
    for ch in channels:
        name = ch.get("screenname") or ch.get("username", "Canal")
        user = ch.get("username", "")
        li = xbmcgui.ListItem(label=name)
        li.setArt({'icon': 'DefaultActor.png'})
        li.setInfo('video', {'plot': "Usuario: " + user})
        xbmcplugin.addDirectoryItem(
            handle=h,
            url=_u(action="dm_user_playlists", user=user),
            listitem=li, isFolder=True,
        )
    xbmcplugin.endOfDirectory(h)

def _dm_user_playlists(user):
    if not user:
        return
    playlists = _dm_list_user_playlists(user)
    if not playlists:
        xbmcgui.Dialog().ok("EspaTV", "Este canal no tiene playlists.")
        return
    h = int(sys.argv[1])
    for pl in playlists:
        name = pl.get("name", "Playlist")
        count = pl.get("count", 0)
        label = "{0} ({1} videos)".format(name, count) if count else name
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': 'DefaultVideoPlaylists.png'})
        desc = pl.get("description", "")
        if desc:
            li.setInfo('video', {'plot': desc})
        xbmcplugin.addDirectoryItem(
            handle=h,
            url=_u(action="dm_view_playlist", pid=pl.get("id")),
            listitem=li, isFolder=True,
        )
    xbmcplugin.endOfDirectory(h)

def _dm_view_playlist(pid):
    if not pid:
        return
    videos = _dm_view_playlist_api(pid)
    if not videos:
        xbmcgui.Dialog().ok("EspaTV", "No se encontraron videos en esta playlist.")
        return
    h = int(sys.argv[1])
    for v in videos:
        vid = v.get("id")
        if not vid:
            continue
        tt = v.get("title", "Video")
        th = v.get("thumbnail_720_url", "")
        dur = v.get("duration", 0)
        owner = v.get("owner.screenname", "")
        li = xbmcgui.ListItem(label=tt)
        li.setArt({'thumb': th, 'icon': th})
        info = {'title': tt, 'duration': dur}
        if owner:
            info['plot'] = "Canal: " + owner
        li.setInfo('video', info)
        li.setProperty("IsPlayable", "true")
        cm = []
        cm.append(("Descargar Video", "RunPlugin({0})".format(_u(action='download_video', vid=vid, title=tt))))
        cm.append(("Abrir en navegador", "RunPlugin({0})".format(_u(action='dm_open_browser', url=vid))))
        cm.append(("Copiar URL", "RunPlugin({0})".format(_u(action='copy_url', url="https://www.dailymotion.com/video/" + str(vid)))))
        li.addContextMenuItems(cm)
        xbmcplugin.addDirectoryItem(
            handle=h,
            url=_u(action="pv", vid=vid),
            listitem=li, isFolder=False,
        )
    xbmcplugin.endOfDirectory(h)

def _search_external_prompt(query):
    if not query:
        return
    options = []
    actions = []


    if xbmc.getCondVisibility("System.HasAddon(plugin.video.dailymotion_com)"):
        options.append("Buscar en addon Dailymotion (Gujal)")
        actions.append(("dm_gujal_search", query))

    if xbmc.getCondVisibility("System.HasAddon(plugin.video.youtube)"):
        options.append("Buscar en YouTube")
        actions.append(("youtube_search", query))

    if not options:
        xbmcgui.Dialog().ok(
            "EspaTV",
            "No hay addons de terceros instalados.\n\n"
            "Addons compatibles:\n"
            u"  • Dailymotion (Gujal)\n"
            u"  • YouTube",
        )
        return

    sel = xbmcgui.Dialog().select(
        u"¿Dónde quieres buscar '{0}'?".format(query), options
    )
    if sel < 0:
        return

    action_name, q = actions[sel]
    if action_name == "dm_gujal_search":
        _search_dailymotion_gujal(q)
    elif action_name == "youtube_search":
        yt_url = "plugin://plugin.video.youtube/kodion/search/query/?q=" + urllib.parse.quote(q)
        xbmc.executebuiltin('Container.Update("{0}")'.format(yt_url))

def _search_dailymotion_gujal(query):
    if not query:
        return
    if not xbmc.getCondVisibility("System.HasAddon(plugin.video.dailymotion_com)"):
        xbmcgui.Dialog().ok("EspaTV", "El addon Dailymotion de Gujal no está instalado.")
        return
    url = "plugin://plugin.video.dailymotion_com/?action=search&query=" + urllib.parse.quote(query)
    xbmc.executebuiltin('Container.Update("{0}")'.format(url))

def _elementum_search_prompt():
    kb = xbmc.Keyboard("", "Buscar torrents en Elementum")
    kb.doModal()
    if kb.isConfirmed() and kb.getText().strip():
        _search_elementum(kb.getText().strip())


def _search_elementum(query):
    """Busca en Elementum con verificación de instalación."""
    if not query: return
    
    if not xbmc.getCondVisibility("System.HasAddon(plugin.video.elementum)"):
        xbmcgui.Dialog().ok("EspaTV", 
            "El addon Elementum no está instalado.\n\n"
            "Esta función requiere Elementum para buscar torrents en webs como 1337x, RARBG, etc.\n\n"
            "Puedes instalarlo desde el repositorio oficial de Kodi o desde GitHub.")
        return
    
    # Abrir búsqueda en Elementum
    elementum_url = "plugin://plugin.video.elementum/search?q={0}".format(urllib.parse.quote(query))
    xbmc.executebuiltin("Container.Update({0})".format(elementum_url))

def _search_youtube_from_results(query):
    """Búsqueda en YouTube desde resultados (sin API, por scraping)."""
    if not query: return
    kb = xbmc.Keyboard(query, 'Buscar en YouTube')
    kb.doModal()
    if kb.isConfirmed() and kb.getText():
        q = kb.getText().strip()
        if q:
            _add_yt_to_history(q)
            xbmc.executebuiltin(f"Container.Update({_u(action='yt_search_results', query=q)})")


def _search_alternatives_prompt(query, ot=''):
    """Menú de alternativas al pulsar '¿No es lo que buscas?'."""
    opciones = []
    acciones = []

    if xbmc.getCondVisibility("System.HasAddon(plugin.video.dailymotion_com)"):
        opciones.append("[COLOR deepskyblue]Buscar en addon Dailymotion (Gujal)[/COLOR]")
        acciones.append("dm_gujal")

    opciones.append("[COLOR red]Buscar en YouTube[/COLOR]")
    acciones.append("youtube")


    # Editar búsqueda siempre presente
    opciones.append("[COLOR yellow]Editar búsqueda y reintentar[/COLOR]")
    acciones.append("edit")

    # Si solo hay "editar", ir directo al teclado
    if len(acciones) == 1:
        kb = xbmc.Keyboard(query, 'Editar Búsqueda')
        kb.doModal()
        if kb.isConfirmed() and kb.getText():
            xbmc.executebuiltin(f"Container.Update({_u(action='lfr', q=kb.getText(), ot=ot)})")
        return

    sel = xbmcgui.Dialog().select(
        f"¿Dónde quieres buscar '{query}'?", opciones)
    if sel < 0:
        return
    accion = acciones[sel]
    if accion == "dm_gujal":
        _search_dailymotion_gujal(query)
    elif accion == "youtube":
        _search_youtube_from_results(query)
    elif accion == "edit":
        kb = xbmc.Keyboard(query, 'Editar Búsqueda')
        kb.doModal()
        if kb.isConfirmed() and kb.getText():
            xbmc.executebuiltin(f"Container.Update({_u(action='lfr', q=kb.getText(), ot=ot)})")


def _setup_pvr():
    import pvr_manager
    if not xbmcgui.Dialog().yesno(
        "Auto-Configuración PVR",
        "EspaTV va a vincular la lista TDT y la Guía (EPG) con la sección nativa de TV de Kodi para que puedas ver el Timeline de horas.\n\nAtención: Se sobrescribirán tus ajustes de 'PVR IPTV Simple Client'. ¿Continuar?"
    ):
        return

    dp = xbmcgui.DialogProgress()
    dp.create("EspaTV", "Configurando la Guía de Televisión...")
    dp.update(30, "Ajustando reproductor...")

    m3u_url = "https://www.tdtchannels.com/lists/tv.m3u8"
    epg_url = "https://www.tdtchannels.com/epg/TV.xml.gz"

    success = pvr_manager.check_and_setup_pvr(m3u_url, epg_url)

    dp.update(100, "¡Hecho!")
    dp.close()

    if success:
        if xbmcgui.Dialog().yesno("¡Éxito!", "Guía nativa configurada con éxito.\n\nNota: Si se queda al 0% un momento, es normal. Está procesando los datos por primera vez.\n\nVe a la pantalla principal de Kodi y busca la sección 'TV'.\n¿Saltar a la Parrilla ahora?"):
            xbmc.executebuiltin("ActivateWindow(TVGuide)")

def _show_ytdlp_instructions():
    """Muestra instrucciones de instalación de yt-dlp en un TextViewer."""
    text = (
        "Para ver videos de YouTube, Dailymotion y otras webs\n"
        "necesitas instalar dos programas gratuitos en tu PC.\n"
        "No te preocupes, es facil. Sigue estos pasos:\n\n"

        "============================================\n"
        "  PRIMERO: ABRIR LA TERMINAL\n"
        "============================================\n\n"
        "La terminal es una ventana donde escribes\n"
        "comandos. Para abrirla:\n\n"
        "  Windows:\n"
        "    1. Pulsa la tecla Windows del teclado\n"
        "       (la que tiene el logo de Windows)\n"
        "    2. Escribe:  cmd\n"
        "    3. Haz clic en 'Simbolo del sistema'\n"
        "    Se abrira una ventana negra. Ahi es donde\n"
        "    vas a escribir los comandos.\n\n"
        "    TRUCO: Para pegar un comando en esa ventana,\n"
        "    haz CLIC DERECHO con el raton (no Ctrl+V).\n\n"
        "  Mac:\n"
        "    1. Pulsa Cmd + Espacio a la vez\n"
        "    2. Escribe:  Terminal\n"
        "    3. Pulsa Enter\n\n"
        "  Linux:\n"
        "    Pulsa Ctrl + Alt + T a la vez\n\n"

        "============================================\n"
        "  PASO 1: INSTALAR PYTHON\n"
        "============================================\n\n"
        "Puede que ya lo tengas. Para comprobarlo,\n"
        "escribe en la terminal:\n\n"
        "   python --version\n\n"
        "Si aparece algo como 'Python 3.12.0', perfecto,\n"
        "ya lo tienes. Salta al Paso 2.\n\n"
        "Si da error, prueba con:\n"
        "   py --version\n\n"
        "Si tambien falla, necesitas instalarlo:\n\n"
        "   1. Ve a: https://www.python.org/downloads/\n"
        "   2. Descarga la version para tu sistema\n"
        "   3. Ejecuta el instalador\n\n"
        "   >>> MUY IMPORTANTE en Windows: <<<\n"
        "   En la primera pantalla del instalador,\n"
        "   MARCA la casilla que dice:\n"
        "       'Add Python to PATH'\n"
        "   Si no la marcas, nada funcionara.\n\n"
        "   4. Cierra la terminal y vuelve a abrirla\n"
        "      (para que detecte el Python nuevo)\n\n"

        "============================================\n"
        "  PASO 2: INSTALAR YT-DLP\n"
        "============================================\n\n"
        "Escribe (o pega) este comando en la terminal:\n\n"
        '   pip install -U "yt-dlp[default]"\n\n'
        ">>> IMPORTANTE: Escribe el comando TAL CUAL, <<<\n"
        ">>> con las comillas y el [default].          <<<\n"
        ">>> Sin eso, YouTube no funcionara.           <<<\n\n"
        "Si 'pip' da error, prueba con este otro:\n\n"
        '   python -m pip install -U "yt-dlp[default]"\n\n'
        "Espera a que termine (puede tardar un minuto).\n"
        "Cuando vuelva a aparecer el cursor parpadeante,\n"
        "ya esta listo.\n\n"

        "============================================\n"
        "  PASO 3: INSTALAR DENO (para YouTube)\n"
        "============================================\n\n"
        "YouTube necesita este programa adicional.\n"
        "Sin el, los videos de YouTube no funcionaran.\n\n"
        "  Windows:\n"
        "    Escribe en la terminal:\n"
        "    winget install DenoLand.Deno\n\n"
        "    Si 'winget' da error o no lo reconoce:\n"
        "    1. Ve a: https://github.com/denoland/deno/\n"
        "       releases/latest\n"
        "    2. Descarga el archivo que diga:\n"
        "       deno-x86_64-pc-windows-msvc.zip\n"
        "    3. Descomprime el ZIP\n"
        "    4. Mueve deno.exe a C:\\Windows\\\n"
        "       (o a cualquier carpeta que este en PATH)\n\n"
        "  Mac:\n"
        "    Escribe en la terminal:\n"
        "    curl -fsSL https://deno.land/install.sh | sh\n\n"
        "  Linux:\n"
        "    Escribe en la terminal:\n"
        "    curl -fsSL https://deno.land/install.sh | sh\n\n"
        "  Nota: Si ya tienes Node.js version 20 o\n"
        "  superior, tambien vale y no necesitas Deno.\n\n"
        "  Tras instalar Deno, CIERRA la terminal\n"
        "  y vuelve a abrirla para que lo detecte.\n\n"

        "============================================\n"
        "  PASO 4: COMPROBAR QUE TODO FUNCIONA\n"
        "============================================\n\n"
        "Escribe en la terminal:\n\n"
        "   python -m yt_dlp --version\n\n"
        "Si aparece una fecha (ej: 2026.03.13),\n"
        "todo esta correcto.\n\n"
        "Si da error, revisa los pasos anteriores.\n\n"
        ">>> ULTIMO PASO: Cierra Kodi completamente <<<\n"
        ">>> y vuelve a abrirlo.                    <<<\n\n"

        "============================================\n"
        "  PARA ACTUALIZAR EN EL FUTURO\n"
        "============================================\n\n"
        "Si YouTube deja de funcionar, abre la\n"
        "terminal y repite el comando del Paso 2:\n\n"
        '   pip install -U "yt-dlp[default]"\n\n'
        "Despues, cierra y abre Kodi.\n\n"
        "Se recomienda actualizar cada pocas semanas.\n\n"

        "============================================\n"
        "  AYUDA RAPIDA\n"
        "============================================\n\n"
        "Tambien puedes pulsar 'Comprobar estado'\n"
        "en los Ajustes de EspaTV para ver que\n"
        "tienes instalado y que te falta."
    )
    xbmcgui.Dialog().textviewer("EspaTV \u2014 Instalar yt-dlp", text)


def _check_ytdlp_status():
    """Comprueba el estado de yt-dlp y muestra un diagnóstico."""
    import subprocess
    lines = []
    creation_flags = 0x08000000 if xbmc.getCondVisibility("System.Platform.Windows") else 0

    # 1. Comprobar yt-dlp
    try:
        proc = subprocess.run(
            ["python", "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, timeout=15,
            creationflags=creation_flags,
        )
        if proc.returncode == 0:
            ver = proc.stdout.strip()
            lines.append("[OK] yt-dlp instalado: v{0}".format(ver))
        else:
            lines.append("[ERROR] yt-dlp encontrado pero devolvió error")
    except FileNotFoundError:
        lines.append("[ERROR] Python no encontrado en PATH")
    except subprocess.TimeoutExpired:
        lines.append("[AVISO] yt-dlp tardó demasiado en responder")
    except OSError as exc:
        lines.append("[ERROR] Error de sistema: {0}".format(exc))

    # 2. Comprobar Deno
    try:
        proc = subprocess.run(
            ["deno", "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=creation_flags,
        )
        if proc.returncode == 0:
            deno_ver = proc.stdout.strip().split("\n")[0]
            lines.append("[OK] Deno instalado: {0}".format(deno_ver))
        else:
            lines.append("[NO] Deno no disponible")
    except (FileNotFoundError, OSError):
        lines.append("[NO] Deno no encontrado en PATH")
    except subprocess.TimeoutExpired:
        lines.append("[AVISO] Deno tardó demasiado")

    # 3. Comprobar Node.js
    try:
        proc = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=creation_flags,
        )
        if proc.returncode == 0:
            node_ver = proc.stdout.strip()
            lines.append("[OK] Node.js instalado: {0}".format(node_ver))
        else:
            lines.append("[NO] Node.js no disponible")
    except (FileNotFoundError, OSError):
        lines.append("[NO] Node.js no encontrado en PATH")
    except subprocess.TimeoutExpired:
        lines.append("[AVISO] Node.js tardó demasiado")

    # 4. Comprobar yt-dlp-ejs
    try:
        proc = subprocess.run(
            ["python", "-c", "import yt_dlp_ejs"],
            capture_output=True, text=True, timeout=10,
            creationflags=creation_flags,
        )
        if proc.returncode == 0:
            lines.append("[OK] yt-dlp-ejs instalado")
        else:
            lines.append("[NO] yt-dlp-ejs no instalado")
            lines.append("     Instálalo: pip install -U \"yt-dlp[default]\"")
    except (FileNotFoundError, OSError):
        lines.append("[NO] No se pudo comprobar yt-dlp-ejs")
    except subprocess.TimeoutExpired:
        pass

    # Resumen
    has_ytdlp = any("[OK] yt-dlp instalado:" in l for l in lines)
    has_js = any("[OK] Deno" in l or "[OK] Node" in l for l in lines)
    has_ejs = any("[OK] yt-dlp-ejs" in l for l in lines)
    lines.append("")
    lines.append("===== RESUMEN =====")
    if has_ytdlp and has_js and has_ejs:
        lines.append("Todo OK. YouTube debería funcionar correctamente.")
    elif has_ytdlp and has_js and not has_ejs:
        lines.append("Falta yt-dlp-ejs. Ejecuta:")
        lines.append('   pip install -U "yt-dlp[default]"')
    elif has_ytdlp and not has_js:
        lines.append("Falta un runtime JS (Deno o Node.js).")
        lines.append("YouTube podría fallar sin él.")
        lines.append("Pulsa 'Instrucciones' en Ajustes para más info.")
    else:
        lines.append("yt-dlp no detectado. Pulsa 'Instrucciones' en Ajustes.")

    xbmcgui.Dialog().textviewer("EspaTV — Estado de yt-dlp", "\n".join(lines))


def _ytdlp_proactive_check():
    """Comprobación proactiva: devuelve True si yt-dlp parece funcional.

    Si no está disponible, ofrece al usuario ver las instrucciones.
    Llamar antes de intentar resolver un vídeo con ytdlp_resolver.
    """
    if xbmc.getCondVisibility("System.Platform.Android"):
        return False  # No aplica en Android

    import subprocess
    creation_flags = 0x08000000 if xbmc.getCondVisibility("System.Platform.Windows") else 0
    try:
        proc = subprocess.run(
            ["python", "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=creation_flags,
        )
        if proc.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    if xbmcgui.Dialog().yesno(
        "EspaTV",
        "No se encuentra yt-dlp en tu sistema, o no está en el PATH.\n"
        "Es necesario para reproducir este vídeo.\n\n"
        "¿Quieres ver las instrucciones de instalación?",
    ):
        _show_ytdlp_instructions()
    return False


def _copy_to_clipboard(text):
    """Copia texto al portapapeles del sistema operativo usando herramientas nativas."""
    import subprocess
    import shutil
    try:
        if xbmc.getCondVisibility("System.Platform.Windows"):
            p = subprocess.Popen(['clip'], stdin=subprocess.PIPE, text=True, encoding='utf-8')
            p.communicate(input=text)
            return True
        elif xbmc.getCondVisibility("System.Platform.OSX"):
            p = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE, text=True, encoding='utf-8')
            p.communicate(input=text)
            return True
        elif xbmc.getCondVisibility("System.Platform.Linux"):
            if shutil.which("xclip"):
                p = subprocess.Popen(['xclip', '-selection', 'clipboard'], stdin=subprocess.PIPE, text=True, encoding='utf-8')
                p.communicate(input=text)
                return True
            elif shutil.which("xsel"):
                p = subprocess.Popen(['xsel', '--clipboard', '--input'], stdin=subprocess.PIPE, text=True, encoding='utf-8')
                p.communicate(input=text)
                return True
    except Exception:
        pass

    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
        return True
    except Exception:
        pass
    return False


def _ytdlp_menu():
    """Menú principal de yt-dlp para instalación y diagnóstico."""
    opts = [
        "Instrucciones: Instalar / Actualizar yt-dlp",
        "Comprobar estado de yt-dlp",
        "Copiar comando de instalación al Portapapeles"
    ]
    sel = xbmcgui.Dialog().select("Mantenimiento yt-dlp", opts)
    if sel == 0:
        _show_ytdlp_instructions()
    elif sel == 1:
        _check_ytdlp_status()
    elif sel == 2:
        cmd_ytdlp = 'pip install -U "yt-dlp[default]"'
        if xbmc.getCondVisibility("System.Platform.Windows"):
            cmd_deno = 'winget install DenoLand.Deno'
        else:
            cmd_deno = 'curl -fsSL https://deno.land/install.sh | sh'
            
        full_text = cmd_ytdlp + "\n" + cmd_deno
        
        if _copy_to_clipboard(full_text):
            xbmcgui.Dialog().ok("Portapapeles", "Los comandos se han copiado con exito.\nAhora puedes pegarlos (clic derecho o Ctrl+V) en tu terminal.")
        else:
            xbmcgui.Dialog().ok("Error", "No se pudo copiar al portapapeles. Tendras que escribirlo a mano copiando las instrucciones.")


def router(ps):
    p = dict(urllib.parse.parse_qsl(ps)); a = p.get("action")



    if a == "ls": _ls(p.get("cid"), page=int(p.get("page", 0)))
    elif a == "fs": _fs(p.get("au"), p.get("st"), p.get("sth", ""), p.get("pu", ""))
    elif a == "lfr": _lfd(p.get("q"), p.get("ot"), p.get("mode", ""), nh=p.get("nh"))
    elif a == "pv": _pv(p.get("vid"))
    elif a == "catalog_menu": _catalog_menu()
    elif a == "youtube_live_menu": _youtube_live_menu()
    elif a == "webcams_menu": _webcams_menu()
    elif a == "webcams_list": _webcams_list(p.get("cat", ""), p.get("subcat", ""))
    elif a == "play_webcam": _play_webcam(p.get("stream_url", ""), p.get("title", ""))
    elif a == "play_dash": _play_dash(p.get("stream_url", ""), p.get("title", ""))
    elif a == "play_latest_news": _play_latest_news()
    elif a == "press_menu": _press_menu()
    elif a == "press_list": _press_list(p.get("raw_url", ""), p.get("name", ""))
    elif a == "press_read": _press_read(p.get("title", ""), p.get("desc", ""))
    elif a == "felicidad_menu": _felicidad_menu()
    elif a == "felicidad_list": _felicidad_list(p.get("cat", ""))
    elif a == "yt_search": _yt_search()
    elif a == "yt_search_results": _yt_search_results(p.get("query", ""))
    elif a == "remove_yt_history_item":
        q = p.get("q", "")
        if q:
            yh = _load_yt_history()
            yh = [x for x in yh if x != q]
            try:
                with open(_get_yt_history_file(), 'w', encoding='utf-8') as o: json.dump(yh, o)
            except IOError: pass
            xbmc.executebuiltin("Container.Refresh")
    elif a == "clear_yt_history":
        yh = _load_yt_history()
        if yh and xbmcgui.Dialog().yesno("Confirmar", "¿Borrar todo el historial de YouTube? ({0} elementos)".format(len(yh))):
            try:
                with open(_get_yt_history_file(), 'w', encoding='utf-8') as o: json.dump([], o)
            except IOError: pass
            xbmcgui.Dialog().notification("EspaTV", "Historial YouTube borrado", xbmcgui.NOTIFICATION_INFO)
            xbmc.executebuiltin("Container.Refresh")
    elif a == "yt_toggle_watched": _toggle_watched_yt(p.get("yt_id", ""))
    elif a == "yt_queue_add": _yt_queue_add(p.get("yt_id", ""), p.get("name", ""))
    elif a == "felicidad_play": _felicidad_play(p.get("yt_id", ""), p.get("name", ""), p.get("ctype", "channel"), p.get("burl", ""))
    elif a == "felicidad_play_exec": _felicidad_play_exec(p.get("yt_id", ""), p.get("name", ""), p.get("method", "addon"))
    elif a == "cocina_menu": _cocina_menu()
    elif a == "cocina_list": _cocina_list(p.get("cat", ""))
    elif a == "youtube_live_list": _youtube_live_list(p.get("cat", ""))
    elif a == "play_youtube_live": _play_youtube_live(p.get("yt_id", ""), p.get("title", ""))
    elif a == "youtube_install": _youtube_install_prompt()
    elif a == "yt_playlists_menu": _yt_playlists_menu()
    elif a == "yt_playlist_add": _yt_playlist_add_action()
    elif a == "yt_playlist_add_remote": _yt_playlist_add_remote()
    elif a == "yt_playlist_open": _yt_playlist_open(p.get("list_id", ""), p.get("name", ""))
    elif a == "yt_playlist_list": _yt_playlist_list(p.get("list_id", ""), p.get("name", ""))
    elif a == "yt_playlist_open_exec":
        import threading
        threading.Thread(target=_yt_playlist_open_exec, args=(p.get("list_id", ""), p.get("name", ""), p.get("method", "addon"))).start()
    elif a == "yt_playlist_remove":
        _remove_yt_playlist(p.get("list_id", ""))
        xbmcgui.Dialog().notification("EspaTV", "Playlist eliminada", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("Container.Refresh")
    elif a == "podcast_menu": _podcast_menu()
    elif a == "podcast_feed": _podcast_feed(p.get("url", ""), p.get("title", ""))
    elif a == "play_podcast": _play_podcast(p.get("url", ""), p.get("title", ""))
    elif a == "most_watched": _most_watched()
    elif a == "clear_most_watched":
        if xbmcgui.Dialog().yesno("Confirmar", "¿Limpiar toda la lista de lo más visto?"):
            _save_watch_history([])
            xbmcgui.Dialog().notification("EspaTV", "Lista limpiada", xbmcgui.NOTIFICATION_INFO)
            xbmc.executebuiltin("Container.Refresh")
    elif a == "spanish_section": _spanish_section_menu(p.get("section", ""))
    elif a == "spanish_channel_videos": _spanish_channel_videos(p.get("user", ""), p.get("name", ""))
    elif a == "dm_playlists_search": _dm_playlists_search_menu()
    elif a == "dm_user_playlists": _dm_user_playlists_menu(p.get("user", ""))
    elif a == "dm_saved_playlists": _dm_saved_playlists_menu()
    elif a == "dm_view_playlist": _dm_view_playlist_menu(p.get("pid", ""))
    elif a == "dm_save_playlist":
        if _add_saved_playlist(p.get("pid", ""), p.get("name", ""), p.get("user", ""), int(p.get("count", 0))):
            xbmcgui.Dialog().notification("EspaTV", "Playlist guardada", xbmcgui.NOTIFICATION_INFO)
        else:
            xbmcgui.Dialog().notification("EspaTV", "Ya estaba guardada", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("Container.Refresh")
    elif a == "dm_unsave_playlist":
        _remove_saved_playlist(p.get("pid", ""))
        xbmcgui.Dialog().notification("EspaTV", "Playlist eliminada", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("Container.Refresh")
    elif a == "info_menu": info_menu()
    elif a == "info": _info()
    elif a == "revoke_legal": _revoke_legal()
    elif a == "install_espatv_repo": _install_espatv_repo()
    elif a == "web_info": _web_info()
    elif a == "manual_search": _manual_search()
    elif a == "top_movies": _top_movies(p.get("page"))
    elif a == "advanced_menu": advanced_menu()
    elif a == "show_history": _show_history()
    elif a == "history_menu": _history_menu()
    elif a == "search_alt_prompt": _search_alternatives_prompt(p.get("q", ""), p.get("ot", ""))
    elif a == "clear_history": _clear_history()
    elif a == "remove_history_item": _remove_history_item(p.get("q"))
    elif a == "import_backup": _import_backup()
    elif a == "toggle_debug": _toggle_debug()

    elif a == "toggle_recent": _toggle_recent()
    elif a == "set_min_duration": _set_min_duration_filter()
    elif a == "toggle_prioritize_match": _toggle_prioritize_match()
    elif a == "show_favorites": _show_favorites()
    elif a == "add_favorite":
         try:
             params = json.loads(p.get("params", "{}"))
         except (ValueError, TypeError):
             # Si el JSON viene mal formado, extraemos solo la 'q' del título
             params = {'q': p.get("title", "")}
         added = core_settings.add_favorite(p.get("title"), p.get("fav_url"), p.get("icon"), p.get("platform"), p.get("fav_action"), params)
         if added:
             xbmcgui.Dialog().notification("EspaTV", "Añadido a Favoritos", xbmcgui.NOTIFICATION_INFO)
         else:
             xbmcgui.Dialog().notification("EspaTV", "Ya existe en Favoritos", xbmcgui.NOTIFICATION_WARNING)
    elif a == "remove_favorite":
         core_settings.remove_favorite(p.get("fav_url"))
         xbmc.executebuiltin("Container.Refresh")
    elif a == "fav_rename":
         kb = xbmc.Keyboard(p.get("old_title"), 'Renombrar Favorito')
         kb.doModal()
         if kb.isConfirmed():
             new_t = kb.getText()
             if new_t:
                 core_settings.rename_favorite(p.get("fav_url"), new_t)
                 xbmc.executebuiltin("Container.Refresh")
    elif a == "fav_move_up":
         if core_settings.move_favorite(p.get("fav_url"), "up"):
             xbmc.executebuiltin("Container.Refresh")
    elif a == "fav_move_down":
         if core_settings.move_favorite(p.get("fav_url"), "down"):
             xbmc.executebuiltin("Container.Refresh")
    elif a == "fav_news": _show_fav_news()
    elif a == "yt_fav_news": _show_yt_fav_news()
    elif a == "fav_export": _export_favorites()
    elif a == "fav_import": _import_favorites()
    elif a == "show_downloads": _show_downloads()
    elif a == "remove_download":
         core_settings.remove_download(p.get("dl_path"))
         xbmc.executebuiltin("Container.Refresh")
    elif a == "delete_download_file":
         _delete_download_file(p.get("dl_path"), p.get("title"))
    elif a == "clear_cache": _clear_cache()
    elif a == "config_remote_port":
        _addon = xbmcaddon.Addon()
        _old = _addon.getSetting('remote_port') or '8089'
        _new = xbmcgui.Dialog().input("Puerto del servidor remoto", defaultt=_old, type=xbmcgui.INPUT_NUMERIC)
        if _new:
            _port_num = int(_new)
            if 1024 <= _port_num <= 65535:
                _addon.setSetting('remote_port', str(_port_num))
                xbmcgui.Dialog().notification("EspaTV", "Puerto cambiado a {0}".format(_port_num), xbmcgui.NOTIFICATION_INFO, 2000)
                xbmc.executebuiltin("Container.Refresh")
            else:
                xbmcgui.Dialog().ok("EspaTV", "Puerto no válido.\nDebe estar entre 1024 y 65535.")
    elif a == "download_video": _download_video(p.get("vid"), p.get("title"))
    elif a == "toggle_advanced_search": _toggle_advanced_search()
    elif a == "advanced_search_menu": _advanced_search_menu()
    elif a == "setup_pvr": _setup_pvr()
    elif a == "open_pvr": xbmc.executebuiltin("ActivateWindow(TVGuide)")
    elif a == "execute_adv_search": _execute_adv_search(p.get("mode", ""))

    # --- CATEGORY MANAGER ROUTES ---
    elif a == "cat_menu": category_manager.main_menu()
    elif a == "cat_create": category_manager.create_category()
    elif a == "cat_delete": category_manager.delete_category(p.get("name"))
    elif a == "cat_rename": category_manager.rename_category(p.get("name"))
    elif a == "cat_view": category_manager.view_category(p.get("name"))
    elif a == "cat_add_item_dialog": category_manager.add_item_dialog(p.get("q"))
    elif a == "cat_remove_item": category_manager.remove_item(p.get("cat"), p.get("q"))
    elif a == "cat_move_item": category_manager.move_item(p.get("from_cat"), p.get("q"))
    elif a == "cat_export": category_manager.export_categories()
    elif a == "cat_import": category_manager.import_categories()
    elif a == "cat_move_from_favs": 
         # Acción compuesta: Añadir a categoría -> Si éxito -> Borrar de favoritos
         if category_manager.add_item_dialog(p.get("q")):
             removed = core_settings.remove_favorite(p.get("fav_url"))
             if not removed:
                 # Debug: Avisar si falla el borrado
                 xbmcgui.Dialog().notification("EspaTV", "Error: No se pudo quitar de favoritos", xbmcgui.NOTIFICATION_ERROR)
             else:
                 time.sleep(0.2) # Pequeña pausa para asegurar escritura
                 xbmc.executebuiltin("Container.Refresh")
    

    

    elif a == "sanitized_info": _sanitized_info()
    elif a == "version_notes": _version_notes()
    elif a == "version_notes_v1": _version_notes_v1()
    elif a == "view_error_log": _view_error_log()
    elif a == "cache_top_movies_covers": _cache_top_movies_covers()
    elif a == "clear_top_movies_cache": _clear_top_movies_cache()
    elif a == "edit_and_search":

        q = p.get("q", "")
        ot = p.get("ot", "")
        kb = xbmc.Keyboard(q, 'Editar y Buscar')
        kb.doModal()
        if kb.isConfirmed():
             nq = kb.getText()
             if nq:


                 xbmc.executebuiltin(f"Container.Update({_u(action='lfr', q=nq, ot=ot)})")


    # --- TRAKT ACTIONS ---
    elif a == "elementum_search_prompt": _elementum_search_prompt()
    elif a == "elementum_search": _search_elementum(p.get("q"))
    elif a == "menu_collections":
        import trakt_manager
        trakt_manager.menu_collections()
    elif a == "trakt_root":
        import trakt_manager
        trakt_manager.menu_trakt_root()
    elif a == "trakt_options":
        import trakt_manager
        trakt_manager.menu_options()
    elif a == "trakt_help_guide":
        import trakt_manager
        trakt_manager.help_guide()
    elif a == "trakt_set_key":
        import trakt_manager
        trakt_manager.set_api_key()
    elif a == "trakt_import":
        import trakt_manager
        trakt_manager.import_list()
    elif a == "trakt_view_list":
        import trakt_manager
        trakt_manager.view_list(p.get("list_id"), p.get("show_covers") == "1")
    elif a == "trakt_delete_list":
        import trakt_manager
        trakt_manager.delete_list(p.get("list_id"))
    elif a == "trakt_cache_covers":
        import trakt_manager
        trakt_manager.cache_covers(p.get("list_id"))
    elif a == "trakt_clear_cache":
        import trakt_manager
        trakt_manager.clear_cache(p.get("list_id"))




    # --- URL PLAYER ---
    elif a == "open_url":
        import url_player; url_player.open_url_dialog()
    elif a == "url_input":
        import url_player; url_player.url_input()
    elif a == "url_remote":
        import url_remote; url_remote.start_remote()
    elif a == "url_history":
        import url_player; url_player.url_history_menu()
    elif a == "url_history_clear":
        import url_player; url_player.history_clear()
    elif a == "url_history_remove":
        import url_player; url_player.history_remove(p.get("url"))
    elif a == "url_bookmarks":
        import url_player; url_player.url_bookmarks_menu()
    elif a == "url_bookmark_save_from_history":
        import url_player; url_player.bookmark_save_from_history(p.get("url"))
    elif a == "url_bookmark_rename":
        import url_player; url_player.bookmark_rename(p.get("url"))
    elif a == "url_bookmark_delete":
        import url_player; url_player.bookmark_delete(p.get("url"))
    elif a == "url_play":
        import url_player; url_player.play_url_action(p.get("url"))
    elif a == "url_scan":
        import url_player; url_player.scan_videos_dialog()

    # --- DM PLAYLISTS ---
    elif a == "dm_playlists_search": _dm_playlists_search()
    elif a == "dm_user_playlists": _dm_user_playlists(p.get("user"))
    elif a == "dm_view_playlist": _dm_view_playlist(p.get("pid"))

    # --- DM SETTINGS ---
    elif a == "set_dm_safe_level": _set_dm_safe_level()
    elif a == "set_dl_mode": _set_dl_mode()

    # --- SEARCH EXTERNAL ---
    elif a == "search_external_prompt": _search_external_prompt(p.get("q"))
    elif a == "dm_gujal_search": _search_dailymotion_gujal(p.get("q"))

    # --- TDT / TV EN DIRECTO ---
    elif a == "live_tv_menu": live_tv_menu()
    elif a == "slyguy_install": _slyguy_install(p.get("addon_id", ""), p.get("title", "Addon"))
    elif a == "tdt_channels_json": tdt_channels_json()
    elif a == "tdt_json_ambit": tdt_json_ambit(p.get("url", ""))
    elif a == "tdt_channels": tdt_channels()
    elif a == "play_tdt": play_tdt(p.get("url", ""), p.get("title", ""), p.get("ua", ""), p.get("ref", ""))
    elif a == "add_custom_iptv": add_custom_iptv()
    elif a == "freetv_menu": freetv_menu()
    elif a == "view_custom_iptv": view_custom_iptv(p.get("url", ""))
    elif a == "delete_custom_iptv": delete_custom_iptv(p.get("url", ""))

    # --- RADIO / TDT POR REGIÓN ---
    elif a == "radio_menu":
        import tdt_providers; tdt_providers.radio_menu()
    elif a == "radio_group":
        import tdt_providers; tdt_providers.radio_group(p.get("group", ""))
    elif a == "play_radio":
        import tdt_providers; tdt_providers.play_radio(p.get("url", ""), p.get("title", ""))


    # --- RTVE PLAY ---
    elif a == "rtve_menu":
        import rtve_catalog; rtve_catalog.main_menu()
    elif a == "rtve_live":
        import rtve_catalog; rtve_catalog.live_channels()
    elif a == "play_rtve_live":
        import rtve_catalog; rtve_catalog.play_live(p.get("url", ""), p.get("title", ""))
    elif a == "rtve_category":
        import rtve_catalog; rtve_catalog.category(p.get("cat_id", ""), p.get("cat_name", ""), int(p.get("page", "1")))
    elif a == "rtve_search":
        import rtve_catalog; rtve_catalog.search()
    elif a == "rtve_open_web":
        import rtve_catalog; rtve_catalog.open_web(p.get("url", ""), p.get("title", ""))
    elif a == "play_rtve_video":
        import rtve_catalog; rtve_catalog.play_video(p.get("video_id", ""), p.get("title", ""))

    # --- WATCH HISTORY ---
    elif a == "watch_history": _view_watch_history()
    elif a == "del_watch_entry": _del_watch_entry(p.get("url", ""))
    elif a == "clear_watch_history": _clear_watch_history()

    # --- PVR NATIVO ---
    elif a == "record_stream":
        import pvr_recorder_manager
        pvr_recorder_manager.start_recording_ui(p.get("url", ""), p.get("name", "Grabacion Automatica"))
    elif a == "stop_recording":
        import pvr_recorder_manager
        pvr_recorder_manager.stop_all_recordings()
    elif a == "my_recordings_menu":
        _my_recordings_menu()
    elif a == "delete_recording":
        file_path = p.get("file", "")
        if file_path and xbmcvfs.exists(file_path):
            if xbmcgui.Dialog().yesno("Borrar Grabación", "¿Seguro que quieres borrar este archivo de tu disco duro?\n\n{0}".format(os.path.basename(file_path))):
                xbmcvfs.delete(file_path)
                xbmc.executebuiltin("Container.Refresh")
    elif a == "open_settings":
        xbmcaddon.Addon().openSettings()

    # --- TRENDING / MULTI-SEARCH ---
    elif a == "trending": _show_trending()
    elif a == "multi_search": _multi_search()

    # --- UTILIDADES ---
    elif a == "dm_open_browser": _dm_open_browser(p.get("url", ""))
    elif a == "copy_url": _copy_url(p.get("url", ""))
    elif a == "url_play_clipboard":
        import url_player; url_player.play_clipboard()
    elif a == "log_menu": _log_menu()
    elif a == "view_kodi_log": _view_kodi_log()
    elif a == "view_kodi_log_full": _view_kodi_log_full()
    elif a == "view_kodi_log_errors": _view_kodi_log_errors()
    elif a == "search_kodi_log": _search_kodi_log()
    elif a == "log_info": _log_info()
    elif a == "export_log": _export_log()
    elif a == "view_log_visual": _view_log_visual()
    elif a == "show_log_line": _show_log_line(p.get("data", ""))
    elif a == "clear_error_log": _clear_error_log()
    elif a == "open_flowfav": _open_flowfav()
    elif a == "flowfav_install": _flowfav_install()
    elif a == "set_cache_config": _set_cache_config()
    elif a == "toggle_iptv_cache": _toggle_iptv_cache()
    elif a == "set_iptv_cache_ttl": _set_iptv_cache_ttl_dialog()
    elif a == "reset_iptv_cache": _reset_iptv_cache()
    elif a == "set_download_path": _set_download_path()
    elif a == "toggle_inputstream": _toggle_inputstream()
    elif a == "toggle_iptv_anti_errors": _toggle_iptv_anti_errors()
    elif a == "toggle_dev_mode": _toggle_dev_mode()

    # --- BACKUPS ---
    elif a == "backups_menu": _backups_menu()
    elif a == "create_backup_full": _create_backup_full()
    elif a == "list_backups": _list_backups()
    elif a == "restore_backup_selective": _restore_backup_selective(p.get("file", ""))
    elif a == "delete_single_backup": _delete_single_backup(p.get("file", ""))
    elif a == "slyguy_install": _slyguy_install(p.get("addon_id", ""), p.get("title", "Addon"))
    elif a == "export_favs_txt": _export_favorites_txt()
    elif a == "open_atresdaily": _open_atresdaily()
    elif a == "open_espadaily": _open_espadaily()
    elif a == "open_loiolog": _open_loiolog()
    elif a == "loiolog_install": _loiolog_install()

    # --- EL TIEMPO (AEMET) ---
    elif a == "aemet_menu":
        import aemet_weather
        aemet_weather.show_aemet_menu(int(sys.argv[1]), _u)
    elif a == "aemet_search":
        import aemet_weather
        aemet_weather.search_location_ui(int(sys.argv[1]), _u)
    elif a == "aemet_save":
        import aemet_weather
        aemet_weather.save_location(p.get("name", ""), p.get("code", ""))
    elif a == "aemet_remove":
        import aemet_weather
        aemet_weather.remove_location(p.get("code", ""))
        xbmc.executebuiltin("Container.Refresh")
    elif a == "aemet_forecast":
        import aemet_weather
        aemet_weather.show_forecast(int(sys.argv[1]), _u, p.get("code", ""), p.get("name", ""))
    elif a == "aemet_day_info":
        import aemet_weather
        aemet_weather.show_day_info(p.get("code", ""), p.get("name", ""), p.get("day_index", "0"))

    # --- AGENDA DEPORTIVA ---
    elif a == "agenda_menu":
        import espatv_agenda
        espatv_agenda.show_agenda_menu(int(sys.argv[1]), _u)
    elif a == "agenda_events":
        import espatv_agenda
        espatv_agenda.show_agenda_events(int(sys.argv[1]), _u, p.get("sport", ""))
    elif a == "agenda_event_options":
        import espatv_agenda
        espatv_agenda.show_event_options(int(sys.argv[1]), _u, p)
    elif a == "agenda_event_info":
        import espatv_agenda
        espatv_agenda.show_event_info(p)
    elif a == "agenda_futbolenlatv":
        import espatv_agenda
        espatv_agenda.show_futbolenlatv(int(sys.argv[1]), _u)

    # --- UNIVERSO ---
    elif a == "show_universo":
        import universo; universo.show()

    # --- YT-DLP MANTENIMIENTO PC ---
    elif a == "ytdlp_menu": _ytdlp_menu()
    elif a == "ytdlp_instructions": _show_ytdlp_instructions()
    elif a == "ytdlp_check": _check_ytdlp_status()

    else: 
        main_menu()

if __name__ == "__main__":
    # --- Aviso legal en primer arranque ---
    _profile_dir = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    _accepted_file = os.path.join(_profile_dir, '.legal_accepted')
    if not os.path.exists(_accepted_file):
        _aviso = (
            "1. Este addon es independiente y NO está afiliado a ninguna cadena de televisión.\n\n"
            "2. Funciona como agregador de búsqueda mostrando contenido públicamente accesible. "
            "No aloja ni sube contenido protegido.\n\n"
            "3. El usuario es responsable de verificar la legalidad del acceso a los contenidos "
            "según las leyes de su país.\n\n"
            "4. Los contenidos pueden dejar de estar disponibles en cualquier momento. "
            "Este addon se proporciona sin garantías de ningún tipo.\n\n"
            "5. Para mejorar el addon, se envía de forma anónima la plataforma, versión del addon "
            "y de Kodi. No se recopilan datos personales (IP ni hábitos de uso).\n\n"
            "Al usar EspaTV aceptas estas condiciones."
        )
        xbmcgui.Dialog().ok("EspaTV — Términos y Condiciones", _aviso)
        if not os.path.exists(_profile_dir):
            os.makedirs(_profile_dir)
        with open(_accepted_file, 'w') as _f:
            _f.write("accepted")

    stats.ping()

    # --- Comprobacion remota de validez (fail-open) ---
    try:
        if not _check_validity():
            xbmcgui.Dialog().ok("EspaTV", "La versión actual del addon está obsoleta o ha sido desactivada temporalmente.\n\nPor favor, actualiza el addon desde el repositorio oficial (GitHub).")
            sys.exit(0)
    except Exception:
        pass  # Sin internet o error → no bloquear al usuario

    try:
        router(sys.argv[2][1:])
    except Exception as e:
        import traceback
        _log_error("Unhandled exception: {0}\n{1}".format(e, traceback.format_exc()))
        xbmcgui.Dialog().ok("EspaTV - Error", "Ocurrió un error inesperado al procesar la ruta.\n\nConsulta el log de errores en Ajustes Avanzados.")