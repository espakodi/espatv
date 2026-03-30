# -*- coding: utf-8 -*-
# ============================================================================
# dm_gujal.py — Integración con Dailymotion
# ============================================================================
#
# LICENCIA: GNU General Public License v3 (GPL-3.0)
#
# Este archivo contiene código derivado del proyecto
# plugin.video.dailymotion_com desarrollado por Gujal00.
#
# Código original: https://github.com/Gujal00/Kodi-Official
#
# Este archivo se distribuye bajo los términos de la GPL v3 y puede ser
# modificado y redistribuido libremente bajo esos mismos términos.
#
# La licencia propietaria de EspaTV NO se aplica a este archivo.
# ============================================================================
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
# ============================================================================

"""
Esto no es una copia exacta del trabajo de Gujal00, pero sí está basado en él.
"""

import re
import urllib.parse
import requests
import xbmc

_UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 7.1.1; Pixel Build/NMF26O) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/55.0.2883.91 Mobile Safari/537.36"
)

HEADERS = {
    "User-Agent": _UA_MOBILE,
    "Origin": "https://www.dailymotion.com",
    "Referer": "https://www.dailymotion.com/",
}

COOKIES = {"lang": "es_ES", "ff": "off"}


def _parse_m3u8(text):
    """Extrae pares (calidad, url) del manifiesto M3U8.
    Busca primero PROGRESSIVE-URI (MP4 directo), luego streams HLS."""
    entries = re.findall(
        r'NAME="([^"]+)",PROGRESSIVE-URI="([^"]+)"', text
    )
    if not entries:
        entries = re.findall(r'NAME="(\d+)".*\n([^\n]+)', text)
    return entries


def _sort_key(elem):
    """Ordena por resolución numérica, descendente."""
    try:
        return int(elem[0].split("@")[0])
    except (ValueError, IndexError):
        return 0


def play_dm_extreme(vid, max_quality=1080):
    """Resolución completa estilo Gujal00 con subtítulos, tokens sec
    y limitador de calidad.

    Args:
        vid: ID del vídeo de Dailymotion.
        max_quality: Calidad máxima permitida (ej. 480, 720, 1080).

    Returns:
        Tupla (url, subtitulos, mime) o (None, None, None) si falla.
    """
    try:
        r = requests.get(
            "https://www.dailymotion.com/player/metadata/video/" + vid,
            headers=HEADERS, cookies=COOKIES, timeout=15,
        )
        if r.status_code != 200:
            raise ConnectionError("HTTP " + str(r.status_code))

        content = r.json()
        if content.get("error"):
            raise RuntimeError(content["error"].get("type", "Unknown"))

        # --- Subtítulos ---
        subs = []
        subs_obj = content.get("subtitles", {})
        if isinstance(subs_obj, dict):
            subs_data = subs_obj.get("data", {})
            if isinstance(subs_data, dict):
                for lang_key in subs_data:
                    urls = subs_data[lang_key].get("urls", [])
                    if urls:
                        subs.append(urls[0])

        # --- Stream HLS ---
        cc = content.get("qualities", {})
        m_url = ""
        if "auto" in cc:
            for item in cc["auto"]:
                if item.get("type") == "application/x-mpegURL":
                    m_url = item.get("url")
                    break

        if not m_url:
            raise RuntimeError("No HLS stream")

        # --- Parsear manifiesto M3U8 ---
        try:
            extra_cookie = None
            if ".m3u8?sec" in m_url:
                parts = m_url.split("?sec=")
                redirect_url = parts[0] + "?redirect=0&sec=" + urllib.parse.quote(parts[1])
                rr = requests.get(
                    redirect_url, cookies=r.cookies.get_dict(),
                    headers=HEADERS, timeout=10,
                )
                if rr.status_code > 200:
                    rr = requests.get(
                        m_url, cookies=r.cookies.get_dict(),
                        headers=HEADERS, timeout=10,
                    )
                mbtext = rr.text
                if rr.headers.get("set-cookie"):
                    extra_cookie = rr.headers["set-cookie"]
            else:
                mbtext = requests.get(m_url, headers=HEADERS, timeout=10).text

            mb = _parse_m3u8(mbtext)
            if mb:
                mb = sorted(mb, key=_sort_key, reverse=True)

                selected_url = None
                for quality, strurl in mb:
                    try:
                        q_val = int(quality.split("@")[0])
                    except (ValueError, IndexError):
                        q_val = 0
                    if q_val <= max_quality:
                        selected_url = strurl.split("#cell")[0]
                        break

                if not selected_url and mb:
                    selected_url = mb[-1][1].split("#cell")[0]

                if selected_url:
                    # Verificación HEAD
                    try:
                        head_r = requests.head(
                            selected_url, headers=HEADERS,
                            cookies=COOKIES, timeout=5,
                        )
                        if head_r.status_code != 200:
                            xbmc.log(
                                "[EspaTV/dm_gujal] HEAD check: " + str(head_r.status_code),
                                xbmc.LOGWARNING,
                            )
                    except Exception:
                        pass

                    if extra_cookie:
                        final_url = (
                            selected_url + "|"
                            + urllib.parse.urlencode(HEADERS)
                            + "&Cookie=" + urllib.parse.quote(extra_cookie)
                        )
                    else:
                        final_url = selected_url + "|" + urllib.parse.urlencode(HEADERS)

                    return final_url, subs, "video/mp4"
            else:
                # Sin calidades parseables: devolver m3u8 master
                final_url = m_url + "|" + urllib.parse.urlencode(HEADERS)
                return final_url, subs, "application/x-mpegURL"

        except Exception as e:
            xbmc.log(
                "[EspaTV/dm_gujal] Extreme m3u8 parse failed: " + str(e),
                xbmc.LOGWARNING,
            )

        # Fallback: devolver m3u8 master directamente
        final_url = m_url + "|" + urllib.parse.urlencode(HEADERS)
        return final_url, subs, "application/x-mpegURL"

    except Exception as e:
        xbmc.log("[EspaTV/dm_gujal] extreme error: " + str(e), xbmc.LOGWARNING)
        return None, None, None


def play_dm_basic(vid):
    """Resolución básica con User-Agent móvil y cookies.

    Args:
        vid: ID del vídeo de Dailymotion.

    Returns:
        Tupla (url, mime) o (None, None) si falla.
    """
    try:
        r = requests.get(
            "https://www.dailymotion.com/player/metadata/video/" + vid,
            headers=HEADERS, cookies=COOKIES, timeout=15,
        )
        if r.status_code != 200:
            raise ConnectionError("HTTP " + str(r.status_code))

        content = r.json()
        if content.get("error"):
            raise RuntimeError(content["error"].get("type", "Unknown"))

        cc = content.get("qualities", {})
        m_url = ""
        if "auto" in cc:
            for item in cc["auto"]:
                if item.get("type") == "application/x-mpegURL":
                    m_url = item.get("url")
                    break

        if not m_url:
            raise RuntimeError("No HLS stream")

        try:
            mbtext = requests.get(m_url, headers=HEADERS, timeout=10).text
            mb = _parse_m3u8(mbtext)

            if mb:
                mb = sorted(mb, key=_sort_key, reverse=True)
                best_url = mb[0][1].split("#cell")[0]
                final_url = best_url + "|" + urllib.parse.urlencode(HEADERS)
                return final_url, "video/mp4"
            else:
                final_url = m_url + "|" + urllib.parse.urlencode(HEADERS)
                return final_url, "application/x-mpegURL"

        except Exception as e:
            xbmc.log(
                "[EspaTV/dm_gujal] Basic m3u8 parse failed: " + str(e),
                xbmc.LOGWARNING,
            )

        # Fallback: devolver m3u8 master directamente
        final_url = m_url + "|" + urllib.parse.urlencode(HEADERS)
        return final_url, "application/x-mpegURL"

    except Exception as e:
        xbmc.log("[EspaTV/dm_gujal] basic error: " + str(e), xbmc.LOGWARNING)
        return None, None