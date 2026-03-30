# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
Escáner HTML de vídeos embebidos.

Descarga el HTML de una página y extrae URLs de vídeo usando
patrones comunes: etiquetas HTML5 ``<video>``/``<source>``, meta
Open Graph, iframes de plataformas conocidas, enlaces directos
con extensión de vídeo y datos estructurados JSON-LD.

Funciona sin dependencias externas — solamente ``urllib`` y ``re``
de la librería estándar — lo que permite ejecutarlo en cualquier
plataforma, incluido Android.
"""
import json
import re
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen, Request

try:
    import xbmc
    _log = lambda msg: xbmc.log("[EspaTV/scanner] {0}".format(msg), xbmc.LOGDEBUG)
except ImportError:
    _log = lambda msg: None

_VIDEO_EXTENSIONS = frozenset((
    ".mp4", ".m3u8", ".webm", ".mkv", ".avi", ".mpd",
    ".ts", ".flv", ".mov", ".wmv", ".m3u",
))

_MAX_HTML_BYTES = 512 * 1024  # 512 KB
_MAX_RESULTS = 50
_HEAD_TIMEOUT = 4
_DOWNLOAD_TIMEOUT = 10

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# Funciones públicas


def scan_page(url, timeout=None):
    """Descarga *url* y devuelve los vídeos detectados en el HTML.

    Args:
        url:     Dirección de la página a analizar.
        timeout: Segundos máximos para la descarga del HTML.

    Returns:
        Lista de dicts con claves ``title``, ``url``, ``filesize``
        (int o None) y ``ext``.  Lista vacía si no se encuentra nada
        o si la descarga falla.
    """
    if timeout is None:
        timeout = _DOWNLOAD_TIMEOUT

    html = _download_html(url, timeout)
    if not html:
        return []

    seen = set()
    results = []

    extractors = (
        _extract_og_videos,
        _extract_html5_videos,
        _extract_jsonld_videos,
        _extract_iframes,
        _extract_direct_urls,
    )

    for extract in extractors:
        for entry in extract(html, url):
            video_url = entry.get("url", "")
            if not video_url or video_url in seen:
                continue
            seen.add(video_url)
            results.append(entry)
            if len(results) >= _MAX_RESULTS:
                return results

    # Obtener tamaños en bloque
    for entry in results:
        if entry.get("filesize") is None and _has_video_extension(entry["url"]):
            entry["filesize"] = _get_filesize(entry["url"])

    return results


def format_size(byte_count):
    """Convierte bytes a formato legible (``1.2 GB``, ``340 MB``, …).

    Devuelve cadena vacía si *byte_count* es ``None`` o negativo.
    """
    if byte_count is None or byte_count < 0:
        return ""
    if byte_count == 0:
        return "0 B"

    units = ("B", "KB", "MB", "GB", "TB")
    size = float(byte_count)
    for unit in units:
        if size < 1024.0:
            if unit == "B":
                return "{0:.0f} {1}".format(size, unit)
            return "{0:.1f} {1}".format(size, unit)
        size /= 1024.0
    return "{0:.1f} PB".format(size)


def format_duration(seconds):
    """Convierte segundos a ``HH:MM:SS`` o ``MM:SS``.

    Devuelve cadena vacía si *seconds* es ``None`` o negativo.
    """
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return "{0:02d}:{1:02d}:{2:02d}".format(h, m, s)
    return "{0:02d}:{1:02d}".format(m, s)


# Descarga


def _download_html(url, timeout):
    """Descarga los primeros bytes del HTML de *url*."""
    try:
        req = Request(url, headers={"User-Agent": _UA})
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/xml" not in content_type:
                _log("Content-Type no soportado: {0}".format(content_type))
                return ""
            raw = resp.read(_MAX_HTML_BYTES)
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        _log("Error descargando {0}: {1}".format(url, exc))
        return ""


# Extractores


def _extract_og_videos(html, base_url):
    """Extrae URLs de vídeo desde meta tags ``og:video``."""
    results = []
    pattern = re.compile(
        r'<meta\s+[^>]*?(?:property|name)=["\']og:video(?::url)?["\']'
        r'[^>]*?content=["\']([^"\']+)',
        re.I,
    )
    pattern_rev = re.compile(
        r'<meta\s+[^>]*?content=["\']([^"\']+)["\']'
        r'[^>]*?(?:property|name)=["\']og:video(?::url)?["\']',
        re.I,
    )
    og_title = _extract_meta_content(html, "og:title") or ""

    urls_found = set()
    for m in pattern.finditer(html):
        urls_found.add(m.group(1).strip())
    for m in pattern_rev.finditer(html):
        urls_found.add(m.group(1).strip())

    for video_url in urls_found:
        video_url = urljoin(base_url, video_url)
        results.append({
            "title": og_title or _title_from_url(video_url),
            "url": video_url,
            "filesize": None,
            "ext": _get_extension(video_url),
        })
    return results


def _extract_html5_videos(html, base_url):
    """Extrae URLs desde ``<video>`` y ``<source>`` HTML5."""
    results = []

    # <video src="...">
    for m in re.finditer(r'<video\s[^>]*?src=["\']([^"\']+)', html, re.I):
        src = urljoin(base_url, m.group(1).strip())
        if _has_video_extension(src):
            results.append({
                "title": _title_from_url(src),
                "url": src,
                "filesize": None,
                "ext": _get_extension(src),
            })

    # <source src="..." type="video/...">
    source_re = re.compile(r'<source\s([^>]+)', re.I)
    for m in source_re.finditer(html):
        attrs = m.group(1)
        src_m = re.search(r'src=["\']([^"\']+)', attrs)
        if not src_m:
            continue
        src = urljoin(base_url, src_m.group(1).strip())
        # Incluir solo si tiene extensión de vídeo o type="video/..."
        has_video_type = bool(re.search(r'type=["\']video/', attrs, re.I))
        if not _has_video_extension(src) and not has_video_type:
            continue
        results.append({
            "title": _title_from_url(src),
            "url": src,
            "filesize": None,
            "ext": _get_extension(src),
        })

    return results


def _extract_iframes(html, base_url):
    """Extrae vídeos embebidos en iframes de YouTube y Dailymotion."""
    results = []
    iframe_re = re.compile(r'<iframe\s[^>]*?src=["\']([^"\']+)', re.I)

    for m in iframe_re.finditer(html):
        src = m.group(1).strip()

        # YouTube embed
        yt = re.search(r'youtube\.com/embed/([a-zA-Z0-9_-]+)', src)
        if yt:
            vid = yt.group(1)
            results.append({
                "title": "YouTube: {0}".format(vid),
                "url": "https://www.youtube.com/watch?v={0}".format(vid),
                "filesize": None,
                "ext": "",
            })
            continue

        # YouTube nocookie
        yt2 = re.search(r'youtube-nocookie\.com/embed/([a-zA-Z0-9_-]+)', src)
        if yt2:
            vid = yt2.group(1)
            results.append({
                "title": "YouTube: {0}".format(vid),
                "url": "https://www.youtube.com/watch?v={0}".format(vid),
                "filesize": None,
                "ext": "",
            })
            continue

        # Dailymotion embed
        dm = re.search(r'dailymotion\.com/embed/video/([a-zA-Z0-9]+)', src)
        if dm:
            vid = dm.group(1)
            results.append({
                "title": "Dailymotion: {0}".format(vid),
                "url": "https://www.dailymotion.com/video/{0}".format(vid),
                "filesize": None,
                "ext": "",
            })
            continue

    return results


def _extract_direct_urls(html, base_url):
    """Busca URLs con extensiones de vídeo en el HTML."""
    results = []
    url_re = re.compile(
        r'(https?://[^\s"\'<>]+?\.(?:mp4|m3u8|webm|mkv|avi|mpd|ts|flv|mov|wmv))'
        r'(?:[?#][^\s"\'<>]*)?',
        re.I,
    )

    for m in url_re.finditer(html):
        raw = m.group(0).strip()
        # Limpiar caracteres de cierre que puedan haberse colado
        raw = raw.rstrip(")")
        results.append({
            "title": _title_from_url(raw),
            "url": raw,
            "filesize": None,
            "ext": _get_extension(raw),
        })

    return results


def _extract_jsonld_videos(html, base_url):
    """Extrae vídeos desde datos estructurados JSON-LD."""
    results = []
    for m in re.finditer(
        r'<script\s+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S,
    ):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue

        videos = []
        if isinstance(data, list):
            videos = data
        elif isinstance(data, dict):
            videos = [data]

        for item in videos:
            vtype = item.get("@type", "")
            if vtype not in ("VideoObject", "Video"):
                continue
            content_url = item.get("contentUrl") or item.get("embedUrl") or ""
            if not content_url:
                continue
            content_url = urljoin(base_url, content_url)
            title = item.get("name") or _title_from_url(content_url)
            duration_iso = item.get("duration") or ""
            seconds = _parse_iso_duration(duration_iso)
            results.append({
                "title": title,
                "url": content_url,
                "filesize": None,
                "ext": _get_extension(content_url),
                "duration": seconds,
            })

    return results


# Utilidades internas


def _extract_meta_content(html, prop):
    """Devuelve el valor ``content`` de un meta tag ``property=prop``."""
    m = re.search(
        r'<meta\s+[^>]*?(?:property|name)=["\']' + re.escape(prop)
        + r'["\'][^>]*?content=["\']([^"\']+)',
        html, re.I,
    )
    if not m:
        m = re.search(
            r'<meta\s+[^>]*?content=["\']([^"\']+)["\']'
            r'[^>]*?(?:property|name)=["\']' + re.escape(prop) + r'["\']',
            html, re.I,
        )
    return m.group(1).strip() if m else None


def _has_video_extension(url):
    """Comprueba si la URL tiene una extensión de vídeo conocida."""
    try:
        path = urlparse(url).path.lower()
        for ext in _VIDEO_EXTENSIONS:
            if path.endswith(ext):
                return True
    except Exception:
        pass
    return False


def _get_extension(url):
    """Devuelve la extensión de vídeo de la URL, sin el punto."""
    try:
        path = urlparse(url).path.lower()
        for ext in _VIDEO_EXTENSIONS:
            if path.endswith(ext):
                return ext.lstrip(".")
    except Exception:
        pass
    return ""


def _title_from_url(url):
    """Genera un título legible a partir de la URL."""
    try:
        path = urlparse(url).path
        name = path.rstrip("/").rsplit("/", 1)[-1]
        # Quitar extensión
        if "." in name:
            name = name.rsplit(".", 1)[0]
        # Reemplazar guiones y underscores por espacios
        name = name.replace("-", " ").replace("_", " ")
        return name[:80] if name else url[:80]
    except Exception:
        return url[:80]


def _get_filesize(url):
    """Obtiene el tamaño del archivo mediante petición HEAD.

    Devuelve el número de bytes (int) o ``None`` si no se pudo obtener.
    """
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urlopen(req, timeout=_HEAD_TIMEOUT) as resp:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception:
        pass
    return None


def _parse_iso_duration(iso):
    """Convierte duración ISO 8601 (``PT1H30M15S``) a segundos.

    Devuelve ``None`` si el formato no es reconocido.
    """
    if not iso:
        return None
    m = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$",
        iso, re.I,
    )
    if not m:
        return None
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    secs = int(m.group(3) or 0)
    total = hours * 3600 + mins * 60 + secs
    return total if total > 0 else None