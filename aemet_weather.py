# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
Modulo de prediccion meteorologica
"""
import json
import os
import time
import xml.etree.ElementTree as ET

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

try:
    import requests
except ImportError:
    requests = None

# Constantes

_ADDON = xbmcaddon.Addon()
_ADDON_PATH = os.path.dirname(os.path.abspath(__file__))
_PROFILE_DIR = xbmcvfs.translatePath(_ADDON.getAddonInfo("profile"))
_TEMP_DIR = xbmcvfs.translatePath("special://temp/")
_LOG_TAG = "[EspaTV-AEMET]"

_AEMET_XML_URL = "https://www.aemet.es/xml/municipios/localidad_{code}.xml"
_AEMET_ICON_URL = os.path.join(os.path.dirname(__file__), "resources", "weather", "{code}.png")
_MUNICIPIOS_FILE = os.path.join(_ADDON_PATH, "resources", "municipios_aemet.json")
_LOCATIONS_FILE = os.path.join(_PROFILE_DIR, "aemet_locations.json")
_CACHE_TTL_SECONDS = 45 * 60  # 45 minutos

_ACCENT_MAP = {
    "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
    "ü": "u", "ñ": "n", "ç": "c",
    "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U",
    "Ü": "U", "Ñ": "N", "Ç": "C",
    "à": "a", "è": "e", "ì": "i", "ò": "o", "ù": "u",
    "À": "A", "È": "E", "Ì": "I", "Ò": "O", "Ù": "U",
}

_DIAS_SEMANA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
_MESES = [
    "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
]

_WIND_DIRECTIONS = {
    "N": "Norte", "NE": "Noreste", "E": "Este", "SE": "Sureste",
    "S": "Sur", "SO": "Suroeste", "O": "Oeste", "NO": "Noroeste",
    "C": "Calma",
}

# Logging

def _log(msg, level=xbmc.LOGINFO):
    xbmc.log("{tag} {msg}".format(tag=_LOG_TAG, msg=msg), level)


def _log_err(msg):
    _log(msg, xbmc.LOGERROR)


# Normalizacion de texto (busqueda tolerante a acentos)


def normalize(text):
    """Elimina acentos y pasa a minusculas para busqueda tolerante.

    Usa un diccionario manual en vez de unicodedata para maxima
    compatibilidad con builds de Kodi que pueden no incluirlo.
    """
    return "".join(_ACCENT_MAP.get(c, c) for c in text).lower()

# Base de datos de municipios

_municipios_cache = None


def _load_municipios():
    """Carga el JSON de municipios en memoria (con cache en variable global)."""
    global _municipios_cache
    if _municipios_cache is not None:
        return _municipios_cache
    try:
        with open(_MUNICIPIOS_FILE, "r", encoding="utf-8") as fh:
            _municipios_cache = json.load(fh)
        _log("Municipios cargados: {0}".format(len(_municipios_cache)))
    except Exception as exc:
        _log_err("Error cargando municipios: {0}".format(exc))
        _municipios_cache = {}
    return _municipios_cache


def search_municipios(query, limit=50):
    """Busca municipios cuyo nombre normalizado contenga la query.

    Devuelve lista de tuplas (nombre_display, codigo_ine) ordenada
    por relevancia: primero los que empiezan por la query, luego
    los que la contienen.
    """
    db = _load_municipios()
    if not query or not db:
        return []

    q = normalize(query.strip())
    if not q:
        return []

    starts = []
    contains = []

    for display_name, code in db.items():
        normalized = normalize(display_name)
        # Comparar contra el nombre sin la provincia entre parentesis
        name_part = normalized.split(" (")[0] if " (" in normalized else normalized
        if name_part.startswith(q):
            starts.append((display_name, code))
        elif q in normalized:
            contains.append((display_name, code))

    starts.sort(key=lambda x: x[0])
    contains.sort(key=lambda x: x[0])
    return (starts + contains)[:limit]


# Ubicaciones guardadas

def _ensure_profile_dir():
    if not os.path.exists(_PROFILE_DIR):
        os.makedirs(_PROFILE_DIR)


def load_locations():
    """Carga la lista de ubicaciones guardadas por el usuario."""
    if not os.path.exists(_LOCATIONS_FILE):
        return []
    try:
        with open(_LOCATIONS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except Exception as exc:
        _log_err("Error leyendo ubicaciones: {0}".format(exc))
    return []


def _save_locations(locations):
    _ensure_profile_dir()
    try:
        with open(_LOCATIONS_FILE, "w", encoding="utf-8") as fh:
            json.dump(locations, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        _log_err("Error guardando ubicaciones: {0}".format(exc))


def save_location(name, code):
    """Anade una ubicacion a la lista de guardadas si no existe ya."""
    code = str(code).zfill(5)
    locations = load_locations()
    for loc in locations:
        if loc.get("code") == code:
            xbmcgui.Dialog().notification(
                "EspaTV", "Esta ubicación ya está guardada",
                xbmcgui.NOTIFICATION_WARNING, 3000,
            )
            return
    locations.append({"name": name, "code": code})
    _save_locations(locations)
    xbmcgui.Dialog().notification(
        "EspaTV", "Ubicación guardada: {0}".format(name),
        xbmcgui.NOTIFICATION_INFO, 3000,
    )


def remove_location(code):
    """Elimina una ubicacion de la lista de guardadas."""
    code = str(code).zfill(5)
    locations = load_locations()
    filtered = [loc for loc in locations if loc.get("code") != code]
    if len(filtered) < len(locations):
        _save_locations(filtered)
        xbmcgui.Dialog().notification(
            "EspaTV", "Ubicación eliminada",
            xbmcgui.NOTIFICATION_INFO, 2000,
        )


# Cache local de XML


def _cache_path(code):
    return os.path.join(_TEMP_DIR, "aemet_{0}.xml".format(code))


def _cache_is_valid(path):
    """Comprueba si el archivo cacheado existe y no ha expirado."""
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < _CACHE_TTL_SECONDS


def _cache_age_text(path):
    """Devuelve texto legible de la antiguedad de la cache."""
    if not os.path.exists(path):
        return ""
    minutes = int((time.time() - os.path.getmtime(path)) / 60)
    if minutes < 60:
        return "hace {0} min".format(minutes)
    hours = minutes // 60
    return "hace {0}h {1}min".format(hours, minutes % 60)


# Descarga y parseo de XML


def _download_xml(code):
    """Descarga el XML de prediccion de AEMET para un municipio."""
    code = str(code).zfill(5)
    url = _AEMET_XML_URL.format(code=code)
    cache = _cache_path(code)

    if _cache_is_valid(cache):
        _log("Cache valida para {0}".format(code))
        try:
            with open(cache, "rb") as fh:
                return fh.read()
        except Exception:
            pass

    if requests is None:
        _log_err("Modulo requests no disponible")
        return _read_stale_cache(cache)

    try:
        _log("Descargando XML de AEMET: {0}".format(url))
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Kodi/EspaTV",
        })
        if resp.status_code != 200:
            _log_err("HTTP {0} desde AEMET".format(resp.status_code))
            return _read_stale_cache(cache)
        content = resp.content
        # Guardar en cache
        try:
            with open(cache, "wb") as fh:
                fh.write(content)
        except Exception as exc:
            _log_err("Error escribiendo cache: {0}".format(exc))
        return content
    except Exception as exc:
        _log_err("Error descargando XML: {0}".format(exc))
        return _read_stale_cache(cache)


def _read_stale_cache(cache_path):
    """Lee cache expirada como fallback (degradacion graceful)."""
    if os.path.exists(cache_path):
        _log("Usando cache expirada: {0}".format(cache_path))
        try:
            with open(cache_path, "rb") as fh:
                return fh.read()
        except Exception:
            pass
    return None


def _safe_text(element):
    """Extrae texto de un elemento XML de forma segura."""
    if element is None:
        return ""
    return (element.text or "").strip()


def _safe_attr(element, attr, default=""):
    """Extrae un atributo de un elemento XML de forma segura."""
    if element is None:
        return default
    return element.attrib.get(attr, default)


def _pick_best_period_value(elements, preferred_periods=None):
    """De una lista de elementos con atributo 'periodo', selecciona el mejor.

    Prioridad:
      1. Periodo de 24h (00-24) si tiene valor
      2. Primer periodo especifico con valor no vacio
    """
    if preferred_periods is None:
        preferred_periods = ["00-24", "00-12", "12-24", "00-06", "06-12", "12-18", "18-24"]

    by_period = {}
    for elem in elements:
        periodo = _safe_attr(elem, "periodo")
        text = _safe_text(elem)
        by_period[periodo] = (text, elem)

    for pref in preferred_periods:
        if pref in by_period:
            text, elem = by_period[pref]
            if text:
                return text, elem
    # Si ninguno de los preferidos tiene valor, devolver el primero con valor
    for text, elem in by_period.values():
        if text:
            return text, elem
    # Sin datos, devolver el primero que exista
    if by_period:
        first_key = next(iter(by_period))
        return by_period[first_key]
    return "", None


def _pick_best_sky(elements):
    """Selecciona el mejor estado_cielo para mostrar como resumen del dia.

    Prioridad: 00-24 con descripcion > 12h diurno > primer 6h con descripcion.
    """
    by_period = {}
    for elem in elements:
        periodo = _safe_attr(elem, "periodo")
        desc = _safe_attr(elem, "descripcion")
        icon_code = _safe_text(elem)
        by_period[periodo] = {"desc": desc, "icon": icon_code}

    preferred = ["00-24", "06-12", "12-18", "00-12", "12-24", "18-24", "00-06"]
    for pref in preferred:
        if pref in by_period:
            data = by_period[pref]
            if data["desc"]:
                return data["desc"], data["icon"]

    for data in by_period.values():
        if data["desc"]:
            return data["desc"], data["icon"]

    return "", ""


def _format_date(fecha_str):
    """Convierte '2026-03-30' en 'Lun 30 Mar'."""
    try:
        t = time.strptime(fecha_str, "%Y-%m-%d")
        return "{dow} {day} {mon}".format(
            dow=_DIAS_SEMANA[t.tm_wday],
            day=t.tm_mday,
            mon=_MESES[t.tm_mon - 1],
        )
    except (ValueError, TypeError, AttributeError):
        return fecha_str


def _is_today(fecha_str):
    """Comprueba si la fecha coincide con hoy."""
    try:
        t = time.strptime(fecha_str, "%Y-%m-%d")
        now = time.localtime()
        return (t.tm_year == now.tm_year and 
                t.tm_mon == now.tm_mon and 
                t.tm_mday == now.tm_mday)
    except (ValueError, TypeError, AttributeError):
        return False


def _icon_url(code):
    """Construye la URL del icono de estado del cielo."""
    if not code:
        return ""
    return _AEMET_ICON_URL.format(code=code)


def _wind_label(direction, speed):
    """Formatea viento: 'Norte a 25 km/h' o 'Calma'."""
    if not direction and not speed:
        return ""
    if direction == "C" or (not direction and speed == "0"):
        return "Calma"
    if speed and speed != "0":
        dir_name = _WIND_DIRECTIONS.get(direction, direction)
        if dir_name:
            return "{0} a {1} km/h".format(dir_name, speed)
        return "{0} km/h".format(speed)
    dir_name = _WIND_DIRECTIONS.get(direction, direction)
    return dir_name if dir_name else ""


def _parse_day(dia_elem):
    """Parsea un elemento <dia> del XML de AEMET."""
    fecha = _safe_attr(dia_elem, "fecha")
    es_hoy = _is_today(fecha)

    # Temperatura
    temp_elem = dia_elem.find("temperatura")
    temp_max = _safe_text(temp_elem.find("maxima")) if temp_elem is not None else ""
    temp_min = _safe_text(temp_elem.find("minima")) if temp_elem is not None else ""

    # Sensacion termica
    sens_elem = dia_elem.find("sens_termica")
    sens_max = _safe_text(sens_elem.find("maxima")) if sens_elem is not None else ""
    sens_min = _safe_text(sens_elem.find("minima")) if sens_elem is not None else ""

    # Humedad relativa
    hum_elem = dia_elem.find("humedad_relativa")
    hum_max = _safe_text(hum_elem.find("maxima")) if hum_elem is not None else ""
    hum_min = _safe_text(hum_elem.find("minima")) if hum_elem is not None else ""

    # UV
    uv = _safe_text(dia_elem.find("uv_max"))

    # Estado del cielo (resumen)
    sky_desc, sky_icon = _pick_best_sky(dia_elem.findall("estado_cielo"))

    # Precipitacion (mejor disponible)
    precip_val, _ = _pick_best_period_value(dia_elem.findall("prob_precipitacion"))

    # Viento (resumen: preferir 00-24 o el periodo con mas velocidad)
    best_wind_dir = ""
    best_wind_vel = ""
    max_vel = -1
    for v_elem in dia_elem.findall("viento"):
        d = _safe_text(v_elem.find("direccion"))
        vel_str = _safe_text(v_elem.find("velocidad"))
        try:
            vel_num = int(vel_str)
        except (ValueError, TypeError):
            vel_num = 0
        periodo = _safe_attr(v_elem, "periodo")
        if periodo == "00-24" and d:
            best_wind_dir = d
            best_wind_vel = vel_str
            break
        if vel_num > max_vel and d:
            max_vel = vel_num
            best_wind_dir = d
            best_wind_vel = vel_str

    # Racha maxima (mejor disponible)
    racha_val, _ = _pick_best_period_value(dia_elem.findall("racha_max"))

    # Datos horarios (temperatura, sensacion, humedad)
    hourly = []
    if temp_elem is not None:
        for dato in temp_elem.findall("dato"):
            hora = _safe_attr(dato, "hora")
            t_val = _safe_text(dato)
            s_val = ""
            h_val = ""
            if sens_elem is not None:
                for sd in sens_elem.findall("dato"):
                    if _safe_attr(sd, "hora") == hora:
                        s_val = _safe_text(sd)
                        break
            if hum_elem is not None:
                for hd in hum_elem.findall("dato"):
                    if _safe_attr(hd, "hora") == hora:
                        h_val = _safe_text(hd)
                        break
            hourly.append({
                "hora": hora,
                "temp": t_val or "—",
                "sens": s_val or "—",
                "humedad": h_val or "—",
            })

    # Periodos detallados de cielo
    periodos = []
    for ec in dia_elem.findall("estado_cielo"):
        periodo = _safe_attr(ec, "periodo")
        desc = _safe_attr(ec, "descripcion")
        icon = _safe_text(ec)
        if desc:
            p_precip = ""
            for pp in dia_elem.findall("prob_precipitacion"):
                if _safe_attr(pp, "periodo") == periodo:
                    p_precip = _safe_text(pp)
                    break
            p_wind_dir = ""
            p_wind_vel = ""
            for wv in dia_elem.findall("viento"):
                if _safe_attr(wv, "periodo") == periodo:
                    p_wind_dir = _safe_text(wv.find("direccion"))
                    p_wind_vel = _safe_text(wv.find("velocidad"))
                    break
            p_racha = ""
            for rm in dia_elem.findall("racha_max"):
                if _safe_attr(rm, "periodo") == periodo:
                    p_racha = _safe_text(rm)
                    break
            periodos.append({
                "periodo": periodo,
                "estado": desc,
                "icon_code": icon,
                "icon_url": _icon_url(icon),
                "prob_precip": p_precip,
                "viento_dir": p_wind_dir,
                "viento_vel": p_wind_vel,
                "racha": p_racha,
            })

    return {
        "fecha": fecha,
        "fecha_display": _format_date(fecha),
        "es_hoy": es_hoy,
        "temp_max": temp_max,
        "temp_min": temp_min,
        "estado_cielo": sky_desc,
        "icon_code": sky_icon,
        "icon_url": _icon_url(sky_icon),
        "prob_precip": precip_val,
        "viento_dir": best_wind_dir,
        "viento_vel": best_wind_vel,
        "racha_max": racha_val,
        "sens_max": sens_max,
        "sens_min": sens_min,
        "humedad_max": hum_max,
        "humedad_min": hum_min,
        "uv_max": uv,
        "periodos": periodos,
        "temp_horaria": hourly,
    }


def fetch_forecast(code):
    """Descarga y parsea la prediccion completa de un municipio.

    Devuelve dict con nombre, provincia, elaborado y lista de
    hasta 7 dias de prediccion. Devuelve None si falla.
    """
    xml_bytes = _download_xml(code)
    if not xml_bytes:
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        _log_err("XML invalido para {0}: {1}".format(code, exc))
        return None

    nombre = _safe_text(root.find(".//nombre"))
    provincia = _safe_text(root.find(".//provincia"))
    elaborado = _safe_text(root.find(".//elaborado"))

    prediccion = root.find(".//prediccion")
    if prediccion is None:
        _log_err("XML sin bloque <prediccion> para {0}".format(code))
        return None

    dias = []
    for dia_elem in prediccion.findall("dia"):
        dias.append(_parse_day(dia_elem))

    cache = _cache_path(code)
    stale = not _cache_is_valid(cache)

    return {
        "nombre": nombre,
        "provincia": provincia,
        "elaborado": elaborado,
        "stale": stale,
        "cache_age": _cache_age_text(cache) if stale else "",
        "dias": dias,
    }


# Interfaz Kodi


def _build_forecast_plot(day):
    """Construye el texto 'plot' (descripcion extendida) para un dia."""
    lines = []

    if day["temp_max"] or day["temp_min"]:
        lines.append("Temperatura: {0}°C máx / {1}°C mín".format(
            day["temp_max"] or "—", day["temp_min"] or "—"))

    if day["sens_max"] or day["sens_min"]:
        lines.append("Sensación: {0}°C máx / {1}°C mín".format(
            day["sens_max"] or "—", day["sens_min"] or "—"))

    if day["humedad_max"] or day["humedad_min"]:
        lines.append("Humedad: {0}% — {1}%".format(
            day["humedad_max"] or "—", day["humedad_min"] or "—"))

    wind = _wind_label(day["viento_dir"], day["viento_vel"])
    if wind:
        wind_line = "Viento: {0}".format(wind)
        if day["racha_max"]:
            wind_line += " (ráfagas: {0} km/h)".format(day["racha_max"])
        lines.append(wind_line)

    if day["prob_precip"] and day["prob_precip"] != "0":
        lines.append("Prob. lluvia: {0}%".format(day["prob_precip"]))

    if day["uv_max"]:
        lines.append("Índice UV: {0}".format(day["uv_max"]))

    # Datos horarios
    valid_hourly = [h for h in day.get("temp_horaria", []) if h["temp"] != "—"]
    if valid_hourly:
        lines.append("")
        lines.append("Detalle horario:")
        for h in valid_hourly:
            t = "{0}°C".format(h["temp"])
            s = "{0}°C".format(h["sens"]) if h["sens"] != "—" else "—"
            hu = "{0}%".format(h["humedad"]) if h["humedad"] != "—" else "—"
            lines.append("  {hora}h: {t}   Sens: {s}   Hum: {hu}".format(
                hora=h["hora"], t=t, s=s, hu=hu,
            ))

    # Periodos detallados
    if day.get("periodos"):
        lines.append("")
        lines.append("Por tramos:")
        for p in day["periodos"]:
            tramo = "  {0}: {1}".format(p["periodo"], p["estado"])
            extras = []
            if p["prob_precip"]:
                extras.append("lluvia {0}%".format(p["prob_precip"]))
            pw = _wind_label(p["viento_dir"], p["viento_vel"])
            if pw:
                extras.append(pw)
            if extras:
                tramo += " — " + ", ".join(extras)
            lines.append(tramo)

    return "\n".join(lines)


def show_aemet_menu(handle, build_url):
    """Renderiza el menu principal de El Tiempo."""
    # Boton: Anadir ubicacion
    li = xbmcgui.ListItem(label="[COLOR limegreen][B]Añadir ubicación[/B][/COLOR]")
    li.setArt({"icon": "DefaultAddSource.png"})
    li.setInfo("video", {"plot":
        "Busca un municipio de España y añádelo a tu lista.\n"
        "Podrás consultar su predicción en cualquier momento."})
    xbmcplugin.addDirectoryItem(
        handle=handle, url=build_url(action="aemet_search"),
        listitem=li, isFolder=True)

    # Ubicaciones guardadas
    locations = load_locations()
    if not locations:
        li = xbmcgui.ListItem(label="[COLOR grey]No tienes ubicaciones guardadas[/COLOR]")
        li.setInfo("video", {"plot":
            "Pulsa 'Añadir ubicación' para buscar tu municipio.\n"
            "Una vez guardado, podrás ver el pronóstico de 7 días."})
        xbmcplugin.addDirectoryItem(handle=handle, url="", listitem=li, isFolder=False)
    else:
        for loc in locations:
            name = loc.get("name", "")
            code = loc.get("code", "")
            li = xbmcgui.ListItem(label=name)
            li.setArt({"icon": "DefaultAddonWeather.png"})
            li.setInfo("video", {"plot":
                "Pulsa para ver el pronóstico de 7 días.\n"
                "Código AEMET: {0}".format(code)})
            ctx = [(
                "Quitar de guardados",
                "RunPlugin({0})".format(build_url(action="aemet_remove", code=code)),
            )]
            li.addContextMenuItems(ctx)
            xbmcplugin.addDirectoryItem(
                handle=handle,
                url=build_url(action="aemet_forecast", code=code, name=name),
                listitem=li, isFolder=True)

    xbmcplugin.endOfDirectory(handle)


def search_location_ui(handle, build_url):
    """Dialogo de busqueda de municipio con teclado Kodi."""
    kb = xbmc.Keyboard("", "Buscar municipio (ej: Madrid, Bilbao...)")
    kb.doModal()
    if not kb.isConfirmed() or not kb.getText().strip():
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    query = kb.getText().strip()
    results = search_municipios(query)

    if not results:
        xbmcgui.Dialog().notification(
            "EspaTV",
            "No se encontró ningún municipio para '{0}'".format(query),
            xbmcgui.NOTIFICATION_WARNING, 4000)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    for display_name, code in results:
        li = xbmcgui.ListItem(label=display_name)
        li.setArt({"icon": "DefaultAddSource.png"})
        li.setInfo("video", {"plot":
            "Pulsa para añadir '{0}' a tus ubicaciones.\n"
            "Código: {1}".format(display_name, code)})
        xbmcplugin.addDirectoryItem(
            handle=handle,
            url=build_url(action="aemet_save", name=display_name, code=code),
            listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(handle)


def show_forecast(handle, build_url, code, name):
    """Renderiza el pronostico de 7 dias de un municipio."""
    code = str(code).zfill(5)

    dp = xbmcgui.DialogProgress()
    dp.create("El Tiempo", "Cargando pronóstico de {0}...".format(name))
    forecast = fetch_forecast(code)
    dp.close()

    if not forecast or not forecast.get("dias"):
        xbmcgui.Dialog().notification(
            "EspaTV", "No se pudo obtener el pronóstico",
            xbmcgui.NOTIFICATION_ERROR, 4000)
        return

    if forecast.get("stale"):
        xbmcgui.Dialog().notification(
            "EspaTV",
            "Datos no actualizados ({0})".format(forecast.get("cache_age", "")),
            xbmcgui.NOTIFICATION_WARNING, 4000)

    # Cabecera con info de actualizacion
    elaborado = forecast.get("elaborado", "")
    if elaborado:
        elab_display = elaborado.replace("T", " a las ")[:22]
        li = xbmcgui.ListItem(label="[COLOR grey]{0} — Actualizado: {1}[/COLOR]".format(
            name, elab_display))
        li.setArt({"icon": "DefaultIconInfo.png"})
        xbmcplugin.addDirectoryItem(handle=handle, url="", listitem=li, isFolder=False)

    for idx, day in enumerate(forecast["dias"]):
        # Label principal
        fecha = day["fecha_display"]
        estado = day.get("estado_cielo", "")
        t_max = day.get("temp_max", "—")
        t_min = day.get("temp_min", "—")

        if day["es_hoy"]:
            label = "[COLOR gold]HOY[/COLOR] — {fecha} — {estado} — {tmax}°/{tmin}°".format(
                fecha=fecha, estado=estado, tmax=t_max, tmin=t_min)
        else:
            label = "{fecha} — {estado} — {tmax}°/{tmin}°".format(
                fecha=fecha, estado=estado, tmax=t_max, tmin=t_min)

        li = xbmcgui.ListItem(label=label)
        li.setArt({
            "icon": day.get("icon_url", "DefaultAddonWeather.png"),
            "thumb": day.get("icon_url", ""),
        })

        plot = _build_forecast_plot(day)
        li.setInfo("video", {"plot": plot, "title": label})
        
        # Al pulsar el dia, abrimos un visor de texto con la misma info
        day_url = build_url(action="aemet_day_info", code=code, name=name, day_index=str(idx))
        xbmcplugin.addDirectoryItem(handle=handle, url=day_url, listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(handle)


def show_day_info(code, name, day_index):
    """Muestra la informacion del dia seleccionado en un dialogo de texto modal."""
    code = str(code).zfill(5)
    forecast = fetch_forecast(code)
    if not forecast or not forecast.get("dias"):
        return
    
    try:
        idx = int(day_index)
        day = forecast["dias"][idx]
    except (IndexError, ValueError):
        return

    fecha = day["fecha_display"]
    label = "{0} - {1}".format(name, fecha)
    plot = _build_forecast_plot(day)
    
    xbmcgui.Dialog().textviewer(label, plot)