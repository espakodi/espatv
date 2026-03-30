# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
# Grabador nativo HLS PVR (Threaded)

import os
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
import re
import collections
import errno

import xbmc
import xbmcgui


class HLSRecorder(threading.Thread):
    def __init__(self, m3u8_url, output_file, name="", max_duration_mins=0, max_bandwidth=0, callback_progress=None, callback_finish=None):
        super(HLSRecorder, self).__init__()
        self.m3u8_url = m3u8_url
        self.output_file = output_file
        self.channel_name = name
        self.max_duration_mins = max_duration_mins
        self.max_bandwidth = max_bandwidth  # 0 = sin limite (maxima calidad)
        self.callback_progress = callback_progress
        self.callback_finish = callback_finish

        self.daemon = True
        self._stop_event = threading.Event()
        self.downloaded_bytes = 0

        self.downloaded_queue = collections.deque(maxlen=1000)
        self.downloaded_set = set()

        self.start_time = time.time()
        self.is_recording = False
        self.quality_status = "pending"  # pending | matched | single | forced_lowest | error
        self.playlist_resolved = threading.Event()
        self.error_msg = ""

        self.ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def stop(self):
        self._stop_event.set()

    def get_run_time_mins(self):
        return int((time.time() - self.start_time) / 60)

    def format_bytes(self, num):
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if num < 1024.0:
                return "{0:3.1f} {1}".format(num, unit)
            num /= 1024.0
        return "{0:.1f} PB".format(num)

    def _http_get(self, url, is_binary=False):
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        resp = urllib.request.urlopen(req, timeout=15)
        try:
            data = resp.read()
        finally:
            resp.close()
        return data if is_binary else data.decode('utf-8', errors='ignore')

    def _resolve_master_playlist(self, url):
        """Si la URL es una lista maestra, encuentra la variante adecuada segun max_bandwidth."""
        try:
            content = self._http_get(url)
            lines = content.splitlines()
            variants = []
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    bw = 0
                    m = re.search(r'BANDWIDTH=(\d+)', line)
                    if m:
                        bw = int(m.group(1))

                    for j in range(i + 1, len(lines)):
                        candidate = lines[j].strip()
                        if candidate and not candidate.startswith("#"):
                            variant_url = urllib.parse.urljoin(url, candidate)
                            variants.append((bw, variant_url))
                            break

            if variants:
                variants.sort(key=lambda x: x[0], reverse=True)

                if self.max_bandwidth > 0:
                    # Elegir la variante mas alta que no supere el limite
                    eligible = [v for v in variants if v[0] <= self.max_bandwidth]
                    if eligible:
                        chosen = eligible[0]  # Ya esta ordenado desc, el primero es el mejor dentro del limite
                        self.quality_status = "matched"
                    else:
                        chosen = variants[-1]  # Si todas superan el limite, coger la mas baja
                        self.quality_status = "forced_lowest"
                        xbmc.log("[EspaTV-PVR] Ninguna variante cumple el limite de {0} bps. Usando la mas baja: {1} bps".format(
                            self.max_bandwidth, chosen[0]), xbmc.LOGWARNING)
                else:
                    chosen = variants[0]  # Sin limite: maxima calidad
                    self.quality_status = "matched"

                xbmc.log("[EspaTV-PVR] Variantes disponibles: {0}".format(
                    [(v[0], v[1].split('/')[-1]) for v in variants]), xbmc.LOGINFO)
                xbmc.log("[EspaTV-PVR] Variante elegida: {0} bps".format(chosen[0]), xbmc.LOGINFO)
                
                # Si la lista maestra solo tenia 1 variante, cuenta como quality no ajustable
                if len(variants) <= 1:
                    xbmc.log("[EspaTV-PVR] La lista maestra solo contenia una variante de calidad.", xbmc.LOGINFO)
                    self.quality_status = "single"
                    
                return chosen[1]

            # No hay variantes (es un m3u8 plano directo): el canal solo ofrece una calidad
            xbmc.log("[EspaTV-PVR] Canal sin lista maestra de variantes. Se grabará en la única calidad disponible.", xbmc.LOGINFO)
            self.quality_status = "single"
            return url
        except Exception as e:
            xbmc.log("[EspaTV-PVR] Error resolving master playlist: {0}".format(e), xbmc.LOGWARNING)
            self.quality_status = "error"
            return url

    def run(self):
        self.is_recording = True
        xbmc.log("[EspaTV-PVR] Grabacion iniciada: {0}".format(self.channel_name), xbmc.LOGINFO)

        try:
            os.makedirs(os.path.dirname(self.output_file), exist_ok=True)

            best_playlist_url = self._resolve_master_playlist(self.m3u8_url)
            self.playlist_resolved.set()
            xbmc.log("[EspaTV-PVR] M3U8 objetivo: {0}".format(best_playlist_url), xbmc.LOGINFO)

            with open(self.output_file, "ab") as f_out:
                consecutive_errors = 0
                empty_cycles = 0  # Ciclos sin descargar nada (playlist OK pero sin chunks nuevos)

                while not self._stop_event.is_set():
                    # Comprobar señal global de parada desde ajustes
                    try:
                        if xbmcgui.Window(10000).getProperty("espatv_force_stop") == "1":
                            xbmc.log("[EspaTV-PVR] Señal global de parada recibida.", xbmc.LOGINFO)
                            break
                    except Exception:
                        pass

                    # Comprobar temporizador
                    if self.max_duration_mins > 0 and self.get_run_time_mins() >= self.max_duration_mins:
                        xbmc.log("[EspaTV-PVR] Temporizador alcanzado ({0} min).".format(self.max_duration_mins), xbmc.LOGINFO)
                        break

                    try:
                        content = self._http_get(best_playlist_url)
                        lines = content.splitlines()
                        chunks_downloaded_in_cycle = 0

                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue

                            # Abortar si el stream esta encriptado con AES
                            if line.startswith("#EXT-X-KEY:METHOD=AES-128") or line.startswith("#EXT-X-KEY:METHOD=SAMPLE-AES"):
                                xbmc.log("[EspaTV-PVR] Canal encriptado (AES). Abortando.", xbmc.LOGWARNING)
                                self.error_msg = "El canal está encriptado con clave externa (AES). La copia nativa descargaría bytes ilegibles, por seguridad se ha cancelado."
                                self._stop_event.set()
                                break

                            # Fin de VOD: el archivo esta completo
                            if line.startswith("#EXT-X-ENDLIST"):
                                xbmc.log("[EspaTV-PVR] Endlist alcanzada. VOD descargado.", xbmc.LOGINFO)
                                self.error_msg = ""
                                self._stop_event.set()
                                break

                            if line.startswith("#"):
                                continue

                            chunk_url = urllib.parse.urljoin(best_playlist_url, line)
                            chunk_id = line

                            if chunk_id not in self.downloaded_set:
                                if self._stop_event.is_set():
                                    break

                                if self.callback_progress:
                                    self.callback_progress(self, "downloading")

                                chunk_data = self._http_get(chunk_url, is_binary=True)
                                f_out.write(chunk_data)
                                f_out.flush()

                                self.downloaded_bytes += len(chunk_data)

                                if len(self.downloaded_queue) == 1000:
                                    oldest = self.downloaded_queue.popleft()
                                    self.downloaded_set.discard(oldest)

                                self.downloaded_queue.append(chunk_id)
                                self.downloaded_set.add(chunk_id)
                                chunks_downloaded_in_cycle += 1

                        if self._stop_event.is_set():
                            break

                        if chunks_downloaded_in_cycle > 0:
                            consecutive_errors = 0
                            empty_cycles = 0
                            time.sleep(1)
                        else:
                            empty_cycles += 1
                            if empty_cycles > 120:  # ~6 min sin datos nuevos
                                xbmc.log("[EspaTV-PVR] Demasiados ciclos sin datos nuevos.", xbmc.LOGWARNING)
                                self.error_msg = "El canal dejó de emitir datos."
                                break
                            time.sleep(3)
                            if self.callback_progress:
                                self.callback_progress(self, "waiting")

                    except urllib.error.HTTPError as e:
                        if getattr(e, 'code', None) in (404, 403, 410, 451):
                            xbmc.log("[EspaTV-PVR] Servidor respondio HTTP {0}. Emision terminada.".format(e.code), xbmc.LOGINFO)
                            self.error_msg = ""
                            break
                        else:
                            consecutive_errors += 1
                            xbmc.log("[EspaTV-PVR] Error HTTP temporal {0}.".format(getattr(e, 'code', '?')), xbmc.LOGWARNING)
                            if self.callback_progress:
                                self.callback_progress(self, "reconnecting")
                            time.sleep(5)

                    except urllib.error.URLError as e:
                        consecutive_errors += 1
                        xbmc.log("[EspaTV-PVR] Error de red (intento {0}): {1}".format(consecutive_errors, e.reason), xbmc.LOGWARNING)
                        if self.callback_progress:
                            self.callback_progress(self, "reconnecting")
                        time.sleep(5)

                        if consecutive_errors > 720:
                            self.error_msg = "Se perdió la conexión por demasiado tiempo."
                            break

                    except OSError as e:
                        if getattr(e, 'errno', None) == errno.ENOSPC:
                            xbmc.log("[EspaTV-PVR] Sin espacio en disco (ENOSPC).", xbmc.LOGERROR)
                            self.error_msg = "La grabación se detuvo porque no hay espacio libre en el disco duro."
                            self._stop_event.set()
                            break
                        else:
                            consecutive_errors += 1
                            xbmc.log("[EspaTV-PVR] Error OS: {0}".format(e), xbmc.LOGWARNING)
                            time.sleep(5)
                            if consecutive_errors > 20:
                                self.error_msg = "Error persistente del sistema de archivos: {0}".format(e)
                                break

                    except Exception as e:
                        consecutive_errors += 1
                        xbmc.log("[EspaTV-PVR] Error inesperado (intento {0}): {1}".format(consecutive_errors, e), xbmc.LOGWARNING)
                        if self.callback_progress:
                            self.callback_progress(self, "reconnecting")
                        time.sleep(5)

                        if consecutive_errors > 720:
                            self.error_msg = "Se perdió la conexión por demasiado tiempo."
                            break

        except Exception as e:
            self.error_msg = str(e)
            xbmc.log("[EspaTV-PVR] Error fatal del hilo: {0}".format(e), xbmc.LOGERROR)

        finally:
            self.is_recording = False
            self.playlist_resolved.set()  # Siempre señalizar para desbloquear el hilo UI

            # Limpiar archivos de 0 bytes (grabaciones fallidas)
            try:
                if os.path.exists(self.output_file) and os.path.getsize(self.output_file) == 0:
                    os.remove(self.output_file)
                    xbmc.log("[EspaTV-PVR] Archivo vacio eliminado: {0}".format(self.output_file), xbmc.LOGINFO)
            except OSError:
                pass

            xbmc.log("[EspaTV-PVR] Hilo de grabacion finalizado: {0}".format(self.channel_name), xbmc.LOGINFO)
            if self.callback_finish:
                self.callback_finish(self)