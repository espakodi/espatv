# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
import os
import json
import xbmc
import xbmcaddon
import xbmcvfs

# Paths
_PROFILE_PATH = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
if not os.path.exists(_PROFILE_PATH): os.makedirs(_PROFILE_PATH, exist_ok=True)


_DEBUG_FILE = os.path.join(_PROFILE_PATH, 'debug_state.json')
_RECENT_FILE = os.path.join(_PROFILE_PATH, 'recent_state.json')
_FAV_FILE = os.path.join(_PROFILE_PATH, 'favorites.json')
_DL_FILE = os.path.join(_PROFILE_PATH, 'downloads_history.json')
_DURATION_FILE = os.path.join(_PROFILE_PATH, 'min_duration.json')
_ADVANCED_SEARCH_FILE = os.path.join(_PROFILE_PATH, 'advanced_search_state.json')

def is_debug_active():
    if not os.path.exists(_DEBUG_FILE): return False
    try:
        with open(_DEBUG_FILE, 'r', encoding='utf-8') as o: return json.load(o).get('active', False)
    except Exception: return False

def set_debug_active(state):
    try:
        with open(_DEBUG_FILE, 'w', encoding='utf-8') as o: json.dump({'active': state}, o)
    except Exception: pass


_PRIORITY_FILE = os.path.join(_PROFILE_PATH, 'priority_state.json')

def is_prioritize_match_active():
    if not os.path.exists(_PRIORITY_FILE): return False
    try:
        with open(_PRIORITY_FILE, 'r', encoding='utf-8') as o: return json.load(o).get('active', False)
    except Exception: return False

def set_prioritize_match(state):
    try:
        with open(_PRIORITY_FILE, 'w', encoding='utf-8') as o: json.dump({'active': state}, o)
    except Exception: pass


def is_recent_active():
    if not os.path.exists(_RECENT_FILE): return False
    try:
        with open(_RECENT_FILE, 'r', encoding='utf-8') as o: return json.load(o).get('active', False)
    except Exception: return False

def set_recent_active(state):
    try:
        with open(_RECENT_FILE, 'w', encoding='utf-8') as o: json.dump({'active': state}, o)
    except Exception: pass


def get_favorites():
    if not os.path.exists(_FAV_FILE): return []
    try:
        with open(_FAV_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception: return []

def add_favorite(title, url, icon, platform, action, params=None):
    favs = get_favorites()

    if any(f['title'] == title and f['platform'] == platform for f in favs): return False
    
    favs.append({
        "title": title, "url": url, "icon": icon, 
        "platform": platform, "action": action, "params": params or {}
    })
    try:
        with open(_FAV_FILE, 'w', encoding='utf-8') as f: json.dump(favs, f)
        return True
    except Exception: return False

def remove_favorite(url):
    favs = get_favorites()
    new_favs = [f for f in favs if f['url'] != url]
    try:
        with open(_FAV_FILE, 'w', encoding='utf-8') as f: json.dump(new_favs, f)
    except Exception: pass
    return len(favs) != len(new_favs)

def rename_favorite(url, new_title):
    favs = get_favorites()
    changed = False
    for f in favs:
        if f['url'] == url:
            f['title'] = new_title
            changed = True
            break
    if changed:
        try:
            with open(_FAV_FILE, 'w', encoding='utf-8') as f: json.dump(favs, f)
        except Exception: pass
    return changed

def move_favorite(url, direction):
    favs = get_favorites()
    index = -1
    for i, f in enumerate(favs):
        if f['url'] == url:
            index = i
            break
            
    if index == -1: return False
    
    if direction == "up" and index > 0:
        favs[index], favs[index-1] = favs[index-1], favs[index]
        try:
            with open(_FAV_FILE, 'w', encoding='utf-8') as f: json.dump(favs, f)
        except Exception: pass
        return True
    elif direction == "down" and index < len(favs) - 1:
        favs[index], favs[index+1] = favs[index+1], favs[index]
        try:
            with open(_FAV_FILE, 'w', encoding='utf-8') as f: json.dump(favs, f)
        except Exception: pass
        return True
        
    return False


def get_downloads():
    if not os.path.exists(_DL_FILE): return []
    try:
        with open(_DL_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception: return []

def log_download(title, path, vid):
    dls = get_downloads()

    entry = {"title": title, "path": path, "vid": vid}
    import time
    entry["date"] = time.strftime("%d/%m/%Y %H:%M")
    dls.insert(0, entry)
    dls = dls[:50]
    try:
        with open(_DL_FILE, 'w', encoding='utf-8') as f: json.dump(dls, f)
    except Exception: pass

def remove_download(path):
    dls = get_downloads()
    new_dls = [d for d in dls if d['path'] != path]
    try:
        with open(_DL_FILE, 'w', encoding='utf-8') as f: json.dump(new_dls, f)
    except Exception: pass


def get_min_duration():
    if not os.path.exists(_DURATION_FILE): return 0
    try:
        with open(_DURATION_FILE, 'r', encoding='utf-8') as f: return json.load(f).get('minutes', 0)
    except Exception: return 0

def set_min_duration(minutes):
    try:
        with open(_DURATION_FILE, 'w', encoding='utf-8') as f: json.dump({'minutes': minutes}, f)
    except Exception: pass


def is_advanced_search_active():
    if not os.path.exists(_ADVANCED_SEARCH_FILE): return True
    try:
        with open(_ADVANCED_SEARCH_FILE, 'r', encoding='utf-8') as o: return json.load(o).get('active', True)
    except Exception: return True

def set_advanced_search_active(state):
    try:
        with open(_ADVANCED_SEARCH_FILE, 'w', encoding='utf-8') as o: json.dump({'active': state}, o)
    except Exception: pass


_TRAKT_SETTINGS_FILE = os.path.join(_PROFILE_PATH, 'trakt_settings.json')

def get_trakt_settings():
    if not os.path.exists(_TRAKT_SETTINGS_FILE): return {"api_key": "", "lists": []}
    try:
        with open(_TRAKT_SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception: return {"api_key": "", "lists": []}

def save_trakt_settings(data):
    try:
        with open(_TRAKT_SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(data, f)
    except Exception: pass

def get_trakt_api_key():
    return get_trakt_settings().get("api_key", "")

def set_trakt_api_key(key):
    data = get_trakt_settings()
    data["api_key"] = key
    save_trakt_settings(data)

def get_trakt_lists():
    return get_trakt_settings().get("lists", [])

def add_trakt_list(list_id, name, user, item_count=0):
    data = get_trakt_settings()
    lists = data.get("lists", [])

    for l in lists:
        if l['id'] == list_id:

            l['name'] = name
            l['user'] = user
            l['item_count'] = item_count
            save_trakt_settings(data)
            return
    
    lists.append({"id": list_id, "name": name, "user": user, "item_count": item_count})
    data["lists"] = lists
    save_trakt_settings(data)

def remove_trakt_list(list_id):
    data = get_trakt_settings()
    lists = data.get("lists", [])
    new_lists = [l for l in lists if l['id'] != list_id]
    data["lists"] = new_lists
    save_trakt_settings(data)


# --- Caché IPTV ---
_IPTV_CACHE_FILE = os.path.join(_PROFILE_PATH, 'iptv_cache_settings.json')
_IPTV_CACHE_DIR = os.path.join(_PROFILE_PATH, 'iptv_cache')

# Opciones de tiempo TTL en segundos:  0=inactivo, 3600=1h, 43200=12h, 86400=1dia, 604800=1semana
_IPTV_CACHE_TTL_OPTIONS = [
    (0, "Desactivada"),
    (3600, "1 hora"),
    (43200, "12 horas"),
    (86400, "1 día"),
    (604800, "1 semana"),
]

def _load_iptv_cache_cfg():
    if not os.path.exists(_IPTV_CACHE_FILE):
        return {"active": False, "ttl": 0}
    try:
        with open(_IPTV_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"active": False, "ttl": 0}

def _save_iptv_cache_cfg(cfg):
    try:
        with open(_IPTV_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False)
    except Exception:
        pass

def is_iptv_cache_active():
    return _load_iptv_cache_cfg().get("active", False)

def set_iptv_cache_active(state):
    cfg = _load_iptv_cache_cfg()
    cfg["active"] = state
    if state and cfg.get("ttl", 0) == 0:
        cfg["ttl"] = 43200  # default 12h al activar
    elif not state:
        # Si la desactivas, limpia la basura
        clear_iptv_cache_files()
    _save_iptv_cache_cfg(cfg)

def get_iptv_cache_ttl():
    cfg = _load_iptv_cache_cfg()
    if not cfg.get("active", False):
        return 0
    return cfg.get("ttl", 0)

def set_iptv_cache_ttl(ttl_seconds):
    cfg = _load_iptv_cache_cfg()
    cfg["ttl"] = ttl_seconds
    if ttl_seconds > 0:
        cfg["active"] = True
    _save_iptv_cache_cfg(cfg)

def reset_iptv_cache_defaults():
    """Restaura la configuracion de cache a los valores por defecto (desactivada)."""
    _save_iptv_cache_cfg({"active": False, "ttl": 0})
    clear_iptv_cache_files()

def clear_iptv_cache_files():
    """Elimina todos los archivos de cache IPTV del disco."""
    if os.path.exists(_IPTV_CACHE_DIR):
        for f in os.listdir(_IPTV_CACHE_DIR):
            try:
                os.remove(os.path.join(_IPTV_CACHE_DIR, f))
            except Exception:
                pass

def get_iptv_cache_dir():
    if not os.path.exists(_IPTV_CACHE_DIR):
        os.makedirs(_IPTV_CACHE_DIR, exist_ok=True)
    return _IPTV_CACHE_DIR