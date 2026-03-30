# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
Agenda deportiva para EspaTV.

Extrae programacion deportiva de fuentes web (Marca, futbolenlatv.es)
mediante parsing con expresiones regulares sobre HTML estatico.
No requiere dependencias externas mas alla de la libreria estandar.

Arquitectura:
    descarga con cache (TTL 30 min) → parsing regex → renderizado Kodi
"""
import html as html_module
import os
import re
import time
import urllib.request

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

# Constantes

_LOG_TAG = "[EspaTV-Agenda]"
_TEMP_DIR = xbmcvfs.translatePath("special://temp/")
_CACHE_FILE = os.path.join(_TEMP_DIR, "espatv_agenda_marca.html")
_CACHE_TTL = 30 * 60  # 30 minutos

_MARCA_URL = "https://www.marca.com/programacion-tv.html"
_MARCA_ENCODING = "iso-8859-1"
_REQUEST_TIMEOUT = 15
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

_FUTBOLENLATV_URL = "https://www.futbolenlatv.es"
_FUTBOLENLATV_ENCODING = "utf-8"
_FUTBOLENLATV_CACHE = os.path.join(_TEMP_DIR, "espatv_agenda_futbolenlatv.html")

# Mapeo de deportes de Marca a categorias agrupadas.
# La clave es el texto exacto que aparece en <span class="dailyday"> del HTML.
_SPORT_GROUPS = {
    "Fútbol":       {"category": "futbol",      "label": "Fútbol",              "color": "limegreen"},
    "F. Sala":      {"category": "futbol",      "label": "Fútbol",              "color": "limegreen"},
    "Futsal":       {"category": "futbol",      "label": "Fútbol",              "color": "limegreen"},
    "NBA":          {"category": "baloncesto",  "label": "Baloncesto y NBA",    "color": "orange"},
    "Baloncesto":   {"category": "baloncesto",  "label": "Baloncesto y NBA",    "color": "orange"},
    "ACB":          {"category": "baloncesto",  "label": "Baloncesto y NBA",    "color": "orange"},
    "Euroliga":     {"category": "baloncesto",  "label": "Baloncesto y NBA",    "color": "orange"},
    "Fórmula 1":    {"category": "motor",       "label": "Motor",               "color": "red"},
    "MotoGP":       {"category": "motor",       "label": "Motor",               "color": "red"},
    "Motor":        {"category": "motor",       "label": "Motor",               "color": "red"},
    "Rally":        {"category": "motor",       "label": "Motor",               "color": "red"},
    "Tenis":        {"category": "tenis",       "label": "Tenis",               "color": "gold"},
    "Ciclismo":     {"category": "ciclismo",    "label": "Ciclismo",            "color": "deepskyblue"},
    "Golf":         {"category": "golf",        "label": "Golf",                "color": "mediumseagreen"},
    "Boxeo":        {"category": "boxeo",       "label": "Boxeo y MMA",         "color": "tomato"},
    "MMA":          {"category": "boxeo",       "label": "Boxeo y MMA",         "color": "tomato"},
    "Rugby":        {"category": "rugby",       "label": "Rugby",               "color": "peru"},
    "Natación":     {"category": "natacion",    "label": "Natación",            "color": "lightskyblue"},
    "Atletismo":    {"category": "atletismo",   "label": "Atletismo",           "color": "coral"},
    "Balonmano":    {"category": "balonmano",   "label": "Balonmano",           "color": "orchid"},
    "Waterpolo":    {"category": "waterpolo",   "label": "Waterpolo",           "color": "aquamarine"},
    "Voleibol":     {"category": "voleibol",    "label": "Voleibol",            "color": "khaki"},
    "Padel":        {"category": "padel",       "label": "Pádel",               "color": "springgreen"},
    "Pádel":        {"category": "padel",       "label": "Pádel",               "color": "springgreen"},
    "Hockey":       {"category": "hockey",      "label": "Hockey",              "color": "wheat"},
    "Vela":         {"category": "vela",        "label": "Vela",                "color": "cadetblue"},
    "Esgrima":      {"category": "otros",       "label": "Otros Deportes",      "color": "silver"},
    "Gimnasia":     {"category": "otros",       "label": "Otros Deportes",      "color": "silver"},
    "Judo":         {"category": "otros",       "label": "Otros Deportes",      "color": "silver"},
    "Piragüismo":   {"category": "otros",       "label": "Otros Deportes",      "color": "silver"},
    "Esquí":        {"category": "otros",       "label": "Otros Deportes",      "color": "silver"},
}

_DEFAULT_GROUP = {"category": "otros", "label": "Otros Deportes", "color": "silver"}

# Orden de presentacion en el menu de categorias.
_CATEGORY_ORDER = [
    "futbol", "baloncesto", "motor", "tenis", "ciclismo",
    "golf", "boxeo", "rugby", "natacion", "atletismo",
    "balonmano", "waterpolo", "voleibol", "padel",
    "hockey", "vela", "otros",
]


# Logging


def _log(msg, level=xbmc.LOGINFO):
    xbmc.log("{tag} {msg}".format(tag=_LOG_TAG, msg=msg), level)


def _log_err(msg):
    _log(msg, xbmc.LOGERROR)


# Cache generica

def _cache_valid(path):
    """Comprueba si un fichero de cache existe y esta dentro de su TTL."""
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < _CACHE_TTL


def _cache_read(path):
    """Lee un fichero de cache. Devuelve cadena o None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception as exc:
        _log_err("Error leyendo cache {0}: {1}".format(path, exc))
    return None


def _cache_write(path, content):
    """Escribe contenido en un fichero de cache."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception as exc:
        _log_err("Error escribiendo cache {0}: {1}".format(path, exc))


def _fetch_url(url, encoding, cache_path):
    """Descarga una URL con cache y fallback a cache expirada."""
    if _cache_valid(cache_path):
        cached = _cache_read(cache_path)
        if cached:
            _log("Cache valida: {0}".format(os.path.basename(cache_path)))
            return cached

    try:
        _log("Descargando: {0}".format(url))
        req = urllib.request.Request(url, headers=_REQUEST_HEADERS)
        resp = urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT)
        raw = resp.read()
        html = raw.decode(encoding)
        _cache_write(cache_path, html)
        return html
    except Exception as exc:
        _log_err("Error descargando {0}: {1}".format(url, exc))

    stale = _cache_read(cache_path)
    if stale:
        _log("Usando cache expirada: {0}".format(os.path.basename(cache_path)))
        return stale

    return None


# Parser

_RE_EVENT_BLOCK = re.compile(
    r'<li class="dailyevent">(.*?)</li>', re.DOTALL
)
_RE_SPORT = re.compile(
    r'<span class="dailyday">(.*?)</span>'
)
_RE_HOUR = re.compile(
    r'<strong class="dailyhour">(.*?)</strong>'
)
_RE_COMPETITION = re.compile(
    r'<span class="dailycompetition">(.*?)</span>'
)
_RE_TEAMS = re.compile(
    r'<h4 class="dailyteams">\s*(.*?)\s*</h4>', re.DOTALL
)
_RE_CHANNEL = re.compile(
    r'<span class="dailychannel[^"]*"[^>]*>(.*?)</span>', re.DOTALL
)
_RE_HTML_TAG = re.compile(r'<[^>]+>')


def _strip_tags(text):
    """Elimina etiquetas HTML de un fragmento de texto."""
    return _RE_HTML_TAG.sub("", text).strip()


def _parse_events(html):
    """Extrae la lista de eventos deportivos del HTML de Marca.

    Devuelve lista de diccionarios con claves:
        sport, hour, competition, teams, channel, category, color, label
    """
    if not html:
        return []

    blocks = _RE_EVENT_BLOCK.findall(html)
    events = []

    for block in blocks:
        sport_m = _RE_SPORT.search(block)
        hour_m = _RE_HOUR.search(block)
        comp_m = _RE_COMPETITION.search(block)
        teams_m = _RE_TEAMS.search(block)
        chan_m = _RE_CHANNEL.search(block)

        sport = sport_m.group(1).strip() if sport_m else ""
        hour = hour_m.group(1).strip() if hour_m else ""
        competition = comp_m.group(1).strip() if comp_m else ""
        teams = _strip_tags(teams_m.group(1)) if teams_m else ""
        channel = _strip_tags(chan_m.group(1)) if chan_m else ""

        # Sin hora ni equipos no es un evento valido
        if not hour or not teams:
            continue

        group = _SPORT_GROUPS.get(sport, _DEFAULT_GROUP)

        events.append({
            "sport": sport,
            "hour": hour,
            "competition": competition,
            "teams": teams,
            "channel": channel,
            "category": group["category"],
            "label": group["label"],
            "color": group["color"],
        })

    return events


# Categorias dinamicas


def _build_categories(events):
    """Genera la lista de categorias a partir de los eventos del dia.

    Solo devuelve categorias que tengan al menos un evento.
    Respeta el orden definido en _CATEGORY_ORDER.
    """
    seen = {}
    for ev in events:
        cat = ev["category"]
        if cat not in seen:
            seen[cat] = {"category": cat, "label": ev["label"], "color": ev["color"], "count": 0}
        seen[cat]["count"] += 1

    ordered = []
    for cat_key in _CATEGORY_ORDER:
        if cat_key in seen:
            ordered.append(seen.pop(cat_key))
    # Categorias no previstas en el orden se anaden al final
    for cat_data in seen.values():
        ordered.append(cat_data)

    return ordered


def _filter_events(events, category):
    """Filtra eventos por categoria y los ordena por hora."""
    filtered = [ev for ev in events if ev["category"] == category]
    filtered.sort(key=lambda e: e["hour"])
    return filtered


# Funciones publicas de alto nivel


def fetch_agenda():
    """Descarga, parsea y devuelve la lista completa de eventos de Marca."""
    html = _fetch_url(_MARCA_URL, _MARCA_ENCODING, _CACHE_FILE)
    return _parse_events(html)


# Interfaz Kodi — Menu de categorias


def show_agenda_menu(handle, build_url):
    """Renderiza el menu principal de la Agenda Deportiva."""
    events = fetch_agenda()

    if not events:
        li = xbmcgui.ListItem(
            label="[COLOR grey]No se pudo cargar la agenda deportiva[/COLOR]"
        )
        li.setInfo("video", {"plot":
            "No se ha podido obtener la programación deportiva.\n"
            "Comprueba tu conexión a internet."
        })
        xbmcplugin.addDirectoryItem(
            handle=handle, url="", listitem=li, isFolder=False
        )
        xbmcplugin.endOfDirectory(handle)
        return

    # Entrada: Ver todos los deportes
    li = xbmcgui.ListItem(
        label="[COLOR gold][B]Agenda completa ({0} eventos)[/B][/COLOR]".format(len(events))
    )
    li.setArt({"icon": "DefaultAddonPVRClient.png"})
    li.setInfo("video", {"plot":
        "Programación deportiva completa de hoy ordenada por hora."
    })
    xbmcplugin.addDirectoryItem(
        handle=handle,
        url=build_url(action="agenda_events", sport="all"),
        listitem=li, isFolder=True,
    )

    # Categorias dinamicas
    categories = _build_categories(events)
    for cat in categories:
        li = xbmcgui.ListItem(
            label="[COLOR {color}][B]{label}[/B][/COLOR]  [COLOR grey]({count})[/COLOR]".format(**cat)
        )
        li.setArt({"icon": "DefaultAddonPVRClient.png"})
        xbmcplugin.addDirectoryItem(
            handle=handle,
            url=build_url(action="agenda_events", sport=cat["category"]),
            listitem=li, isFolder=True,
        )

    li = xbmcgui.ListItem(
        label="[COLOR limegreen][B]Fútbol en la TV[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultAddonPVRClient.png"})
    li.setInfo("video", {"plot":
        "Partidos de fútbol televisados hoy con horarios y canales."
    })
    xbmcplugin.addDirectoryItem(
        handle=handle,
        url=build_url(action="agenda_futbolenlatv"),
        listitem=li, isFolder=True,
    )

    xbmcplugin.endOfDirectory(handle)


# Interfaz Kodi — Lista de eventos


def _event_plot(ev):
    """Construye el texto descriptivo (plot) de un evento."""
    lines = []
    competition = ev.get("competition", "")
    teams = ev.get("teams", "")
    channel = ev.get("channel", "")
    hour = ev.get("hour", "")
    sport = ev.get("sport", "")
    if competition:
        lines.append("Competición: {0}".format(competition))
    if teams:
        lines.append("Evento: {0}".format(teams))
    if channel:
        lines.append("Canal: {0}".format(channel))
    if hour:
        lines.append("Hora: {0}".format(hour))
    if sport:
        lines.append("Deporte: {0}".format(sport))
    return "\n".join(lines)


def show_agenda_events(handle, build_url, sport_filter):
    """Renderiza la lista de eventos filtrados por deporte."""
    events = fetch_agenda()

    if not events:
        li = xbmcgui.ListItem(
            label="[COLOR grey]No hay eventos disponibles[/COLOR]"
        )
        xbmcplugin.addDirectoryItem(
            handle=handle, url="", listitem=li, isFolder=False
        )
        xbmcplugin.endOfDirectory(handle)
        return

    if sport_filter and sport_filter != "all":
        events = _filter_events(events, sport_filter)
    else:
        events = sorted(events, key=lambda e: e["hour"])

    if not events:
        li = xbmcgui.ListItem(
            label="[COLOR grey]No hay eventos de este deporte hoy[/COLOR]"
        )
        xbmcplugin.addDirectoryItem(
            handle=handle, url="", listitem=li, isFolder=False
        )
        xbmcplugin.endOfDirectory(handle)
        return

    for ev in events:
        label = (
            "[COLOR gold]{hour}[/COLOR]  [COLOR {color}]{sport}[/COLOR]  {teams}"
            "  ·  [COLOR grey]{channel}[/COLOR]"
        ).format(
            hour=ev["hour"],
            color=ev["color"],
            sport=ev["sport"],
            teams=ev["teams"],
            channel=ev["channel"] or "Sin canal",
        )

        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": "DefaultAddonPVRClient.png"})
        li.setInfo("video", {"plot": _event_plot(ev), "title": ev["teams"]})

        url = build_url(
            action="agenda_event_options",
            teams=ev["teams"],
            competition=ev["competition"],
            channel=ev["channel"],
            hour=ev["hour"],
            sport=ev["sport"],
        )
        xbmcplugin.addDirectoryItem(
            handle=handle, url=url, listitem=li, isFolder=True,
        )

    xbmcplugin.endOfDirectory(handle)


# Interfaz Kodi — Opciones de un evento


def show_event_options(handle, build_url, params):
    """Muestra las opciones disponibles al pulsar un evento."""
    teams = params.get("teams", "")
    competition = params.get("competition", "")
    channel = params.get("channel", "")
    hour = params.get("hour", "")
    sport = params.get("sport", "")

    # Construir query de busqueda razonable
    search_query = teams
    if competition:
        search_query = "{0} {1}".format(teams, competition)

    # 1. Ver informacion completa
    li = xbmcgui.ListItem(
        label="[COLOR lightskyblue][B]Ver información del evento[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultIconInfo.png"})
    li.setInfo("video", {"plot": _event_plot(params)})
    url = build_url(
        action="agenda_event_info",
        teams=teams, competition=competition,
        channel=channel, hour=hour, sport=sport,
    )
    xbmcplugin.addDirectoryItem(
        handle=handle, url=url, listitem=li, isFolder=False,
    )

    # 2. Buscar en YouTube
    li = xbmcgui.ListItem(
        label="[COLOR red][B]Buscar en YouTube[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultMusicVideos.png"})
    li.setInfo("video", {"plot":
        "Busca \"{0}\" en YouTube.".format(search_query)
    })
    url = build_url(action="yt_search_results", query=search_query)
    xbmcplugin.addDirectoryItem(
        handle=handle, url=url, listitem=li, isFolder=True,
    )

    # 3. Buscar en Dailymotion
    li = xbmcgui.ListItem(
        label="[COLOR dodgerblue][B]Buscar en Dailymotion[/B][/COLOR]"
    )
    li.setArt({"icon": "DefaultAddonWebSkin.png"})
    li.setInfo("video", {"plot":
        "Busca \"{0}\" en Dailymotion.".format(search_query)
    })
    url = build_url(action="lfr", q=search_query, ot="", nh=1)
    xbmcplugin.addDirectoryItem(
        handle=handle, url=url, listitem=li, isFolder=True,
    )

    # 4. Copiar informacion
    clipboard_text = "{0} - {1} - {2} ({3})".format(
        hour, teams, channel, competition
    )
    li = xbmcgui.ListItem(
        label="[COLOR grey]Copiar información[/COLOR]"
    )
    li.setArt({"icon": "DefaultIconInfo.png"})
    li.setInfo("video", {"plot": clipboard_text})
    url = build_url(action="copy_url", url=clipboard_text)
    xbmcplugin.addDirectoryItem(
        handle=handle, url=url, listitem=li, isFolder=False,
    )

    xbmcplugin.endOfDirectory(handle)


def show_event_info(params):
    """Muestra la informacion completa de un evento en un cuadro de texto."""
    teams = params.get("teams", "")
    competition = params.get("competition", "")
    channel = params.get("channel", "")
    hour = params.get("hour", "")
    sport = params.get("sport", "")

    lines = []
    lines.append("[B]{0}[/B]".format(teams))
    lines.append("")
    if competition:
        lines.append("Competición: {0}".format(competition))
    lines.append("Hora: {0}".format(hour))
    lines.append("Deporte: {0}".format(sport))
    if channel:
        lines.append("Canal: {0}".format(channel))

    text = "\n".join(lines)
    xbmcgui.Dialog().textviewer("Agenda Deportiva", text)


# Futbolenlatv.es — Parser y renderizado

_RE_FUTBOL_ROW = re.compile(r'(<tr[^>]*>.*?</tr>)', re.DOTALL)
_RE_FUTBOL_HOUR_FMT = re.compile(r'\d{1,2}:\d{2}$')
_RE_FUTBOL_COMP = re.compile(
    r'class="cabeceraCompericion".*?'
    r'<a[^>]*internalLink[^>]*>\s*(.*?)\s*</a>',
    re.DOTALL | re.IGNORECASE,
)
_RE_FUTBOL_HOUR = re.compile(
    r'<td class="hora[^"]*"[^>]*>\s*(.*?)\s*</td>', re.DOTALL
)
_RE_FUTBOL_LOCAL = re.compile(
    r'<td class="local"[^>]*>(.*?)</td>', re.DOTALL
)
_RE_FUTBOL_VISIT = re.compile(
    r'<td class="visitante"[^>]*>(.*?)</td>', re.DOTALL
)
_RE_FUTBOL_CHAN = re.compile(
    r'class="(?:canal-sin-enlace|internalLinkCanal)[^"]*"[^>]*>(.*?)<',
    re.DOTALL,
)


def _parse_futbolenlatv(html):
    """Extrae los partidos de futbolenlatv.es.

    Devuelve lista de dicts con: competition, hour, local, visitor, channel.
    """
    if not html:
        return []

    rows = _RE_FUTBOL_ROW.findall(html)
    current_comp = ""
    matches = []

    for row in rows:
        # Detectar fila de cabecera de competicion
        comp_m = _RE_FUTBOL_COMP.search(row)
        if comp_m:
            current_comp = html_module.unescape(
                _RE_HTML_TAG.sub("", comp_m.group(1))
            ).strip()
            continue

        # Detectar fila de partido
        hour_m = _RE_FUTBOL_HOUR.search(row)
        if not hour_m:
            continue
        hour = _strip_tags(hour_m.group(1))
        if not _RE_FUTBOL_HOUR_FMT.match(hour):
            continue
        if len(hour) == 4:
            hour = "0" + hour

        local_m = _RE_FUTBOL_LOCAL.search(row)
        visit_m = _RE_FUTBOL_VISIT.search(row)
        local = html_module.unescape(_strip_tags(local_m.group(1))) if local_m else ""
        visitor = html_module.unescape(_strip_tags(visit_m.group(1))) if visit_m else ""

        if not local and not visitor:
            continue

        canales = _RE_FUTBOL_CHAN.findall(row)
        canales = [c.strip() for c in canales if c.strip()]
        channel = ", ".join(canales)

        matches.append({
            "competition": current_comp,
            "hour": hour,
            "local": local,
            "visitor": visitor,
            "channel": channel,
        })

    return matches


def fetch_futbolenlatv():
    """Descarga, parsea y devuelve los partidos de futbolenlatv.es."""
    html = _fetch_url(_FUTBOLENLATV_URL, _FUTBOLENLATV_ENCODING, _FUTBOLENLATV_CACHE)
    return _parse_futbolenlatv(html)


def show_futbolenlatv(handle, build_url):
    """Renderiza la lista de partidos de futbolenlatv.es."""
    matches = fetch_futbolenlatv()

    if not matches:
        li = xbmcgui.ListItem(
            label="[COLOR grey]No hay partidos de fútbol disponibles[/COLOR]"
        )
        li.setInfo("video", {"plot":
            "No se han podido obtener los partidos.\n"
            "Comprueba tu conexión a internet."
        })
        xbmcplugin.addDirectoryItem(
            handle=handle, url="", listitem=li, isFolder=False
        )
        xbmcplugin.endOfDirectory(handle)
        return

    matches.sort(key=lambda m: m["hour"])
    current_comp = None

    for m in matches:
        # Separador visual por competicion
        if m["competition"] and m["competition"] != current_comp:
            current_comp = m["competition"]
            li = xbmcgui.ListItem(
                label="[COLOR lightskyblue][B]{0}[/B][/COLOR]".format(current_comp)
            )
            li.setInfo("video", {"plot": current_comp})
            xbmcplugin.addDirectoryItem(
                handle=handle, url="", listitem=li, isFolder=False
            )

        teams = "{0} - {1}".format(m["local"], m["visitor"])
        label = (
            "[COLOR gold]{hour}[/COLOR]  {teams}"
            "  ·  [COLOR grey]{channel}[/COLOR]"
        ).format(
            hour=m["hour"],
            teams=teams,
            channel=m["channel"] or "Sin canal",
        )

        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": "DefaultAddonPVRClient.png"})

        plot = ""
        if m["competition"]:
            plot += "Competición: {0}\n".format(m["competition"])
        plot += "Partido: {0}\n".format(teams)
        if m["channel"]:
            plot += "Canal: {0}\n".format(m["channel"])
        plot += "Hora: {0}".format(m["hour"])
        li.setInfo("video", {"plot": plot, "title": teams})

        url = build_url(
            action="agenda_event_options",
            teams=teams,
            competition=m["competition"],
            channel=m["channel"],
            hour=m["hour"],
            sport="Fútbol",
        )
        xbmcplugin.addDirectoryItem(
            handle=handle, url=url, listitem=li, isFolder=True,
        )

    xbmcplugin.endOfDirectory(handle)