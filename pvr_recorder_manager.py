# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
# Coordinador de Interfaz y Background para el PVR HLS

import os
import re
import time
import xbmc
import xbmcgui
import xbmcaddon
from datetime import datetime

from hls_recorder import HLSRecorder

_KODI_WINDOW = xbmcgui.Window(10000)


def _get_setting(setting_id):
    return xbmcaddon.Addon().getSetting(setting_id)


def get_record_path():
    path = _get_setting("record_path")
    if not path:
        xbmcgui.Dialog().notification("EspaTV", "No has configurado la carpeta de grabaciones", xbmcgui.NOTIFICATION_WARNING)
        path = xbmcgui.Dialog().browse(3, "Elige dónde guardar las grabaciones de TDT", "files")
        if path:
            xbmcaddon.Addon().setSetting("record_path", path)
    return path


def _parse_time_label(label):
    """Convierte etiquetas legibles como '1 hora', '1h 30min', '30 min' a minutos."""
    if not label:
        return 0
    label = label.strip().lower()
    if label in ("ahora", "sin límite", "sin limite", "infinito", ""):
        return 0

    # Formato compacto "Xh YYmin" (ej: "1h 30min")
    m = re.match(r'^(\d+)\s*h\s*(\d+)\s*min', label)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # Formato "X hora(s)" (ej: "1 hora", "2 horas")
    m = re.match(r'^(\d+)\s*hora', label)
    if m:
        return int(m.group(1)) * 60

    # Formato "X min" (ej: "30 min", "5 min")
    m = re.match(r'^(\d+)\s*min', label)
    if m:
        return int(m.group(1))

    # Número directo
    try:
        return int(label)
    except ValueError:
        return 0


def get_timer_mins():
    return _parse_time_label(_get_setting("record_timer"))


def get_delay_mins():
    return _parse_time_label(_get_setting("record_delay"))


def get_max_bandwidth():
    """Devuelve el limite de ancho de banda en bps segun la calidad elegida."""
    val = _get_setting("record_quality")
    if not val:
        return 0
    val_lower = val.strip().lower()
    # Mapeo aproximado de resoluciones a bitrates tipicos de HLS
    if "1080" in val_lower or "máxima" in val_lower or "maxima" in val_lower:
        return 0  # Sin limite
    elif "720" in val_lower or "alta" in val_lower:
        return 3000000  # ~3 Mbps
    elif "480" in val_lower or "media" in val_lower:
        return 1500000  # ~1.5 Mbps
    elif "360" in val_lower or "baja" in val_lower:
        return 800000   # ~800 Kbps
    return 0

def _sanitize_filename(raw_name):
    """Genera un nombre de archivo seguro para cualquier sistema operativo."""
    safe = "".join(c for c in raw_name if c.isalnum() or c in (' ', '-', '_')).strip()
    if not safe:
        safe = "Grabacion_TDT"
    # Limitar longitud para evitar PATH_MAX en Windows (260 chars)
    return safe[:80]


def _get_pvr_subfolder(base_path):
    """Devuelve la subcarpeta dedicada a grabaciones PVR, creandola si no existe."""
    pvr_dir = os.path.join(base_path, "Grabaciones_PVR")
    if not os.path.exists(pvr_dir):
        os.makedirs(pvr_dir, exist_ok=True)
    return pvr_dir


def start_recording_ui(url, canonical_name):
    """Inicia la UI interactiva y el proceso de grabacion HLS en segundo plano."""
    dest_path = get_record_path()
    if not dest_path:
        return

    # Validar que la carpeta base es accesible (USB desconectado, red caída, etc.)
    if not os.path.isdir(dest_path):
        xbmcgui.Dialog().ok(
            "Error de grabación",
            "La carpeta de grabaciones no existe o no es accesible:\n\n{0}\n\nComprueba que el disco o USB está conectado.".format(dest_path)
        )
        return

    mins = get_timer_mins()
    delay_mins = get_delay_mins()
    pvr_dir = _get_pvr_subfolder(dest_path)

    # --- Cuenta atrás si hay delay configurado ---
    if delay_mins > 0:
        dp_delay = xbmcgui.DialogProgress()
        dp_delay.create(
            "Grabación programada: {0}".format(canonical_name),
            "La grabación comenzará en {0} minutos...\nPulsa cancelar para abortar.".format(delay_mins)
        )
        total_secs = delay_mins * 60
        elapsed = 0
        while elapsed < total_secs:
            if dp_delay.iscanceled():
                dp_delay.close()
                xbmcgui.Dialog().notification("EspaTV", "Grabación programada cancelada.", xbmcgui.NOTIFICATION_INFO)
                return
            remaining = total_secs - elapsed
            rm = remaining // 60
            rs = remaining % 60
            pct = int((elapsed / float(total_secs)) * 100)
            dp_delay.update(pct, "Faltan {0}m {1}s para empezar a grabar...".format(rm, rs))
            time.sleep(1)
            elapsed += 1
        dp_delay.close()

    safe_name = _sanitize_filename(canonical_name)
    date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = "{0} - {1}.ts".format(safe_name, date_str)

    out_file = os.path.join(pvr_dir, filename)
    max_bw = get_max_bandwidth()
    xbmc.log("[EspaTV-PVR] Grabacion iniciada. Destino: {0} | Calidad max: {1} bps".format(out_file, max_bw if max_bw else 'Sin limite'), xbmc.LOGINFO)

    _KODI_WINDOW.setProperty("espatv_active_recordings", "1")

    recorder = HLSRecorder(url, out_file, name=canonical_name, max_duration_mins=mins, max_bandwidth=max_bw)
    recorder.start()

    dp = xbmcgui.DialogProgress()
    dp.create("Grabando: {0}".format(canonical_name), "Conectando al stream en directo...")

    in_background = False
    bg_dp = None
    start_time = time.time()

    # Esperar a que el hilo resuelva la playlist (determinista, no basado en tiempo fijo)
    recorder.playlist_resolved.wait(timeout=10)
    if max_bw > 0 and recorder.quality_status != "matched":
        if recorder.quality_status == "forced_lowest":
            xbmcgui.Dialog().notification(
                "EspaTV", "No hay calidad tan baja. Se graba en la más baja disponible.",
                xbmcgui.NOTIFICATION_WARNING, 5000
            )
        elif recorder.quality_status in ("single", "error"):
            xbmcgui.Dialog().notification(
                "EspaTV", "Este canal solo ofrece una calidad. Se graba en la disponible.",
                xbmcgui.NOTIFICATION_INFO, 4000
            )

    while recorder.is_alive():
        if not in_background and dp.iscanceled():
            # Dialogo unificado
            ans = xbmcgui.Dialog().yesno(
                "Grabar en Segundo Plano",
                "Has pulsado cancelar en la ventana principal.\n¿Quieres detener la grabación o dejarla funcionando de fondo?",
                nolabel="Detener y Guardar",
                yeslabel="Seguir de fondo"
            )

            if not ans:  # 0 -> Detener
                dp.update(100, "Cerrando archivo...")
                recorder.stop()
                recorder.join(timeout=5.0)
                break
            else:  # 1 -> Segundo plano
                dp.close()
                in_background = True
                bg_dp = xbmcgui.DialogProgressBG()
                bg_dp.create("EspaTV: Grabando", canonical_name)

        run_mins = recorder.get_run_time_mins()
        run_secs = int(time.time() - start_time)

        pct = 0
        if mins > 0:
            pct = min(int((run_mins / float(mins)) * 100), 100)

        sin_datos = (recorder.downloaded_bytes == 0 and run_secs > 10)
        estado_red = "[COLOR orange]Reconectando...[/COLOR]" if sin_datos else "Conexión estable"

        if in_background:
            if bg_dp and not bg_dp.isFinished():
                bg_msg = "Datos: {0} | {1}".format(recorder.format_bytes(recorder.downloaded_bytes), estado_red)
                bg_dp.update(pct, heading="Grabando: {0}".format(canonical_name), message=bg_msg)
        else:
            line1 = "Duración actual: {0} min / {1}".format(
                run_mins, "{0} min".format(mins) if mins > 0 else "Infinito"
            )
            line2 = "Descargado: {0}".format(recorder.format_bytes(recorder.downloaded_bytes))
            line3 = "Estado de red: {0}".format(estado_red)
            dp.update(pct, line1 + "\n" + line2 + "\n" + line3)

        time.sleep(0.5)

    if in_background and bg_dp:
        bg_dp.close()
    elif not in_background:
        dp.close()

    if recorder.error_msg:
        xbmcgui.Dialog().notification("Error en grabación", recorder.error_msg, xbmcgui.NOTIFICATION_WARNING)
    elif recorder.downloaded_bytes == 0:
        xbmcgui.Dialog().notification(
            "Grabación fallida",
            "No se pudo descargar ningún dato del canal.",
            xbmcgui.NOTIFICATION_WARNING
        )
    else:
        xbmcgui.Dialog().notification(
            "Grabación Completada",
            "{0} ({1})".format(canonical_name, recorder.format_bytes(recorder.downloaded_bytes)),
            xbmcgui.NOTIFICATION_INFO, 5000
        )

    _KODI_WINDOW.clearProperty("espatv_active_recordings")


def stop_all_recordings():
    """Fuerza la parada de todas las grabaciones activas via propiedad global."""
    c = _KODI_WINDOW.getProperty("espatv_active_recordings")
    if not c:
        xbmcgui.Dialog().notification("EspaTV", "No hay grabaciones activas.", xbmcgui.NOTIFICATION_INFO)
        return

    if xbmcgui.Dialog().yesno(
        "Detener Grabaciones",
        "¿Estás seguro de que quieres finalizar todas las grabaciones de TDT en segundo plano?"
    ):
        _KODI_WINDOW.setProperty("espatv_force_stop", "1")
        xbmcgui.Dialog().notification(
            "EspaTV", "Señal de cierre enviada. Tardará unos segundos.",
            xbmcgui.NOTIFICATION_INFO
        )
        time.sleep(3)
        _KODI_WINDOW.clearProperty("espatv_active_recordings")
        _KODI_WINDOW.clearProperty("espatv_force_stop")