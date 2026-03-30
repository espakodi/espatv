# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
"""
Universo EspaKodi
=================
Ventana informativa con enlaces a los proyectos relacionados,
canales de Telegram y contacto.
"""
import os
import xbmc
import xbmcgui
import xbmcaddon
import xbmcvfs

_ADDON_PATH = xbmcaddon.Addon().getAddonInfo("path")
_MEDIA = os.path.join(_ADDON_PATH, "resources", "media")
_TEX = os.path.join(_MEDIA, "white.png")
_TRANSP = os.path.join(_MEDIA, "transparent.png")
_FOCUS = os.path.join(_MEDIA, "focus.png")

W, H = 1280, 720
PW, PH = 520, 480
PX, PY = (W - PW) // 2, (H - PH) // 2


def _generate_stars(path):
    """Genera un PNG con estrellas aleatorias (fondo decorativo)."""
    import struct
    import zlib
    import random

    sw, sh = 180, 120
    rng = random.Random(42)
    pixels = bytearray(sw * sh * 4)
    for _ in range(60):
        x = rng.randint(1, sw - 2)
        y = rng.randint(1, sh - 2)
        alpha = rng.randint(40, 200)
        idx = (y * sw + x) * 4
        pixels[idx] = 255
        pixels[idx + 1] = 255
        pixels[idx + 2] = 255
        pixels[idx + 3] = alpha

    raw = b""
    for row in range(sh):
        raw += b"\x00" + bytes(pixels[row * sw * 4 : (row + 1) * sw * 4])

    compressed = zlib.compress(raw, 9)

    def chunk(ctype, data):
        c = ctype + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", sw, sh, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


class UniversoDialog(xbmcgui.WindowDialog):
    """Diálogo con enlaces al ecosistema EspaKodi."""

    def __init__(self):
        super().__init__()
        self.url_map = {}
        self.all_buttons = []
        self.close_btn_id = -1
        self.reveal_btn_id = -1
        self.bg_img = None
        self.all_controls = []
        self._build()

    def _img(self, x, y, w, h, color, img=None):
        c = xbmcgui.ControlImage(x, y, w, h, img or _TEX, colorDiffuse=color)
        self.addControl(c)
        self.all_controls.append(c)

    def _lbl(self, x, y, w, h, text, font="font13", color="FFFFFFFF", align=0):
        c = xbmcgui.ControlLabel(
            x, y, w, h, text, font=font, textColor=color, alignment=align
        )
        self.addControl(c)
        self.all_controls.append(c)

    def _btn(self, x, y, w, h, text, tc="FFFFFFFF", fc="FFFFFFFF", align=0x04):
        c = xbmcgui.ControlButton(
            x, y, w, h, text,
            font="font12", textColor=tc, focusedColor=fc, alignment=align,
            noFocusTexture=_TRANSP, focusTexture=_FOCUS,
        )
        self.addControl(c)
        self.all_controls.append(c)
        return c

    def _build(self):
        # Fondo oscuro
        self._img(0, 0, W, H, "DD000000")
        self._img(PX, PY, PW, PH, "F0111922")

        # Fondo: ascii.jpg descargado (preferido) o estrellas generadas (fallback)
        _profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
        bg_path = os.path.join(_profile, 'ascii.jpg')
        if os.path.exists(bg_path):
            self.bg_img = xbmcgui.ControlImage(PX, PY, PW, PH, bg_path, colorDiffuse='30FFFFFF')
            self.addControl(self.bg_img)
        else:
            stars_path = os.path.join(_MEDIA, "stars.png")
            if not os.path.exists(stars_path):
                _generate_stars(stars_path)
            self._img(PX, PY, PW, PH, "FFFFFFFF", img=stars_path)

        # Línea superior decorativa
        self._img(PX, PY, PW, 3, "FF3090FF")

        # Título con botón reveal
        y = PY + 22
        reveal = xbmcgui.ControlButton(PX, y, PW, 25,
                          '[B]Universo EspaKodi[/B]',
                          font='font13', textColor='FF3090FF', focusedColor='FFFFFFFF', alignment=0x02,
                          noFocusTexture=_TRANSP, focusTexture=_FOCUS)
        self.addControl(reveal)
        self.all_controls.append(reveal)
        self.reveal_btn_id = reveal.getId()
        self.all_buttons.append(reveal)
        y += 35
        self._img(PX + 35, y, PW - 70, 1, "30FFFFFF")
        y += 15

        # Enlaces
        links = [
            ("FF3090FF", "Web Principal (EspaKodi)", "https://github.com/espakodi"),
            ("FF3090FF", "FullStackCurso", "https://github.com/fullstackcurso"),
            ("FF3090FF", "LoioLoio", "https://github.com/loioloio"),
            (None, None, None),
            ("FF2AABEE", "Canal de Telegram", "https://t.me/espadaily"),
            ("FF2AABEE", "Telegram EspaKodi", "https://t.me/espakodi"),
        ]

        for color, label, url in links:
            if color is None:
                self._img(PX + 35, y, PW - 70, 1, "30FFFFFF")
                y += 15
                continue
            self._lbl(
                PX + 50, y, PW - 100, 22,
                "[B]{0}[/B]".format(label),
                font="font12", color=color,
            )
            y += 22
            btn = xbmcgui.ControlButton(
                PX + 35, y, PW - 70, 24,
                "  " + url.replace("https://", ""),
                font="font13", textColor="FFCCCCCC", focusedColor="FFFFFFFF",
                alignment=0x04,
                noFocusTexture=_TRANSP, focusTexture=_FOCUS,
            )
            self.addControl(btn)
            self.all_controls.append(btn)
            self.url_map[btn.getId()] = url
            self.all_buttons.append(btn)
            y += 27

        # Separador
        self._img(PX + 35, y + 3, PW - 70, 1, "30FFFFFF")
        y += 18

        # Contacto
        self._lbl(
            PX + 50, y, PW - 100, 22,
            "[B]Contacto[/B]",
            font="font12", color="FFFFD700",
        )
        y += 24

        c1 = xbmcgui.ControlButton(
            PX + 35, y, PW - 70, 24,
            "  t.me/rubensdfa1labernt",
            font="font13", textColor="FFCCCCCC", focusedColor="FFFFFFFF",
            alignment=0x04,
            noFocusTexture=_TRANSP, focusTexture=_FOCUS,
        )
        self.addControl(c1)
        self.all_controls.append(c1)
        self.url_map[c1.getId()] = "https://t.me/rubensdfa1labernt/?direct"
        self.all_buttons.append(c1)
        y += 22

        c2 = xbmcgui.ControlButton(
            PX + 35, y, PW - 70, 24,
            "  fullstackcurso.github.io/donaciones/#mensaje",
            font="font13", textColor="FFCCCCCC", focusedColor="FFFFFFFF",
            alignment=0x04,
            noFocusTexture=_TRANSP, focusTexture=_FOCUS,
        )
        self.addControl(c2)
        self.all_controls.append(c2)
        self.url_map[c2.getId()] = "https://fullstackcurso.github.io/donaciones/#mensaje"
        self.all_buttons.append(c2)
        y += 30

        # Separador final
        self._img(PX + 35, y, PW - 70, 1, "30FFFFFF")
        y += 15

        # Botón cerrar
        close = self._btn(
            PX + (PW - 180) // 2, y, 180, 32,
            "[B]Cerrar[/B]", tc="FF888888", align=0x02 | 0x04,
        )
        self.close_btn_id = close.getId()
        self.all_buttons.append(close)

        # Navegación circular entre botones
        for i in range(len(self.all_buttons)):
            b = self.all_buttons[i]
            if i > 0:
                b.controlUp(self.all_buttons[i - 1])
            if i < len(self.all_buttons) - 1:
                b.controlDown(self.all_buttons[i + 1])
        self.all_buttons[0].controlUp(self.all_buttons[-1])
        self.all_buttons[-1].controlDown(self.all_buttons[0])
        self.setFocus(self.all_buttons[0])

    def _handle_click(self, control_id):
        if control_id == self.close_btn_id:
            self.close()
            return
        if control_id == self.reveal_btn_id:
            if self.bg_img:
                for c in self.all_controls:
                    c.setVisible(False)
                self.bg_img.setColorDiffuse('FFFFFFFF')
                self.bg_img.setVisible(True)
                import time as _t
                _t.sleep(3)
                self.bg_img.setColorDiffuse('30FFFFFF')
                for c in self.all_controls:
                    c.setVisible(True)
            return
        url = self.url_map.get(control_id)
        if url:
            try:
                if xbmc.getCondVisibility("System.Platform.Android"):
                    xbmc.executebuiltin(
                        'StartAndroidActivity("","android.intent.action.VIEW","","{0}")'.format(url)
                    )
                else:
                    import webbrowser
                    webbrowser.open(url)
                xbmcgui.Dialog().notification(
                    "Universo EspaKodi", "Abriendo...",
                    xbmcgui.NOTIFICATION_INFO, 2000,
                )
            except Exception:
                xbmcgui.Dialog().ok(
                    "Universo EspaKodi",
                    "Abre esta URL:\n\n" + url,
                )

    def onAction(self, action):
        aid = action.getId()
        if aid in (10, 92, 110):
            self.close()
        elif aid in (7, 100, 101):
            self._handle_click(self.getFocusId())

    def onClick(self, controlId):
        self._handle_click(controlId)


def show():
    """Muestra el diálogo Universo EspaKodi."""
    dlg = UniversoDialog()
    dlg.doModal()
    del dlg