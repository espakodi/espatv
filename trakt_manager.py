# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
import sys
import json
import urllib.parse
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import core_settings
import requests

def _u(**kwargs):
    return sys.argv[0] + "?" + urllib.parse.urlencode(kwargs)

def _handle():
    return int(sys.argv[1])

def _get_thumb_cache_path(list_id):
    import os
    import xbmcvfs
    p = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
    if not os.path.exists(p): os.makedirs(p)
    return os.path.join(p, "trakt_thumbs_{0}.json".format(list_id))

def _load_thumb_cache(list_id):
    import os
    path = _get_thumb_cache_path(list_id)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception: pass
    return {}

def menu_collections():

    li = xbmcgui.ListItem(label="Listas de Trakt.tv")
    li.setArt({'icon': 'DefaultAddonService.png'})
    xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_root"), listitem=li, isFolder=True)
    
    xbmcplugin.endOfDirectory(_handle(), cacheToDisc=True)

def menu_trakt_root():

    

    li = xbmcgui.ListItem(label="[COLOR blue]Opciones / Importar Lista[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_options"), listitem=li, isFolder=True)
    

    lists = core_settings.get_trakt_lists()
    for l in lists:
        count = l.get('item_count', 0)
        label = "{0} [COLOR gray]({1} ítems)[/COLOR]".format(l['name'], count)
        li = xbmcgui.ListItem(label=label)
        li.setArt({'icon': 'DefaultFolder.png'})
        li.setInfo('video', {'plot': "ID: {0}\nUsuario: {1}\nTotal: {2} elementos.".format(l['id'], l['user'], count)})
        
        cm = [("Eliminar lista", "RunPlugin({0})".format(_u(action='trakt_delete_list', list_id=l['id'])))]
        li.addContextMenuItems(cm)
        
        xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_view_list", list_id=l['id']), listitem=li, isFolder=True)
        
    xbmcplugin.endOfDirectory(_handle(), cacheToDisc=True)

def menu_options():

    

    li = xbmcgui.ListItem(label="[COLOR yellow][B]Guía: Cómo obtener tu API Key (Paso a Paso)[/B][/COLOR]")
    li.setArt({'icon': 'DefaultIconInfo.png'})
    li.setInfo('video', {'plot': "Instrucciones detalladas de cómo registrarte en Trakt.tv para obtener tu propia llave Client ID."})
    xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_help_guide"), listitem=li, isFolder=False)


    key = core_settings.get_trakt_api_key()
    status = "[COLOR green]CONFIGURADA[/COLOR]" if key else "[COLOR red]NO CONFIGURADA[/COLOR]"
    
    li = xbmcgui.ListItem(label="Configurar API Key {0}".format(status))
    li.setArt({'icon': 'DefaultAddonService.png'}) # Usamos este que suele ser un engranaje o llave en la mayoría de skins
    li.setInfo('video', {'plot': "Pulsa aquí para introducir tu Client ID una vez lo tengas."})
    xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_set_key"), listitem=li, isFolder=False)
    

    if key:
        li = xbmcgui.ListItem(label="Importar Lista por ID")
        li.setArt({'icon': 'DefaultAddonService.png'})
        li.setInfo('video', {'plot': "Introduce el ID numérico de la lista pública (ej: 21583416)"})
        xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_import"), listitem=li, isFolder=False)
    
    xbmcplugin.endOfDirectory(_handle(), cacheToDisc=True)

def help_guide():
    heading = "[COLOR yellow]GUÍA: CONFIGURAR TRAKT.TV[/COLOR]"
    
    text = (
        "[B]PASO 1: REGISTRO[/B]\n"
        "Entra en tu navegador (PC o móvil) a:\n"
        "[COLOR cyan]https://trakt.tv/oauth/applications[/COLOR]\n"
        "Identifícate con tu cuenta de usuario.\n\n"
        
        "[B]PASO 2: CREAR APLICACIÓN[/B]\n"
        "Pulsa el botón verde [B]NEW APPLICATION[/B].\n\n"
        
        "[B]PASO 3: RELLENAR DATOS[/B]\n"
        "Copia y pega exactamente esto en los cuadros:\n"
        " • [B]Name:[/B] EspaTV\n"
        " • [B]Description:[/B] Addon de Kodi\n"
        " • [B]Redirect uri:[/B] urn:ietf:wg:oauth:2.0:oob\n"
        " • [B]Javascript (cors) origins:[/B] (Vacío)\n"
        " • [B]Permissions:[/B] No marques nada.\n\n"
        "Baja al final y dale al botón morado [B]SAVE APP[/B].\n\n"
        
        "[B]PASO 4: OBTENER EL CÓDIGO[/B]\n"
        "Tras guardar, aparecerán varios códigos.\n"
        "Busca el que dice [B]Client ID[/B] (es una línea de letras y números).\n\n"
        "Copia ese código y pégalo en el menú 'Configurar API Key' de este addon.\n\n"
        "--------------------------------------------------\n"
        "[I]* Nota: La API Key es personal y gratuita. Solo sirve para que el addon pueda leer las listas públicas de Trakt.tv.[/I]"
    )
    
    xbmcgui.Dialog().textviewer(heading, text)

def set_api_key():
    k = xbmc.Keyboard(core_settings.get_trakt_api_key(), 'Introduce Trakt Client ID (API Key)')
    k.doModal()
    if k.isConfirmed():
        key = k.getText().strip()
        core_settings.set_trakt_api_key(key)
        xbmcgui.Dialog().notification("EspaTV", "API Key Guardada", xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin("Container.Refresh")

def import_list():
    key = core_settings.get_trakt_api_key()
    if not key:
        xbmcgui.Dialog().notification("Error", "Configura la API Key primero", xbmcgui.NOTIFICATION_ERROR)
        return

    k = xbmc.Keyboard('', 'Introduce ID numérico de la lista')
    k.doModal()
    if k.isConfirmed():
        list_id = k.getText().strip()
        if not list_id: return
        

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': key
        }
        

        pDialog = xbmcgui.DialogProgress()
        pDialog.create("EspaTV", "Importando lista de Trakt...")
        
        try:
    
            r = requests.get('https://api.trakt.tv/lists/{0}'.format(list_id), headers=headers)
            if r.status_code != 200:
                pDialog.close()
                xbmcgui.Dialog().notification("Error Trakt", "Error {0}: No se pudo leer la lista".format(r.status_code), xbmcgui.NOTIFICATION_ERROR)
                return
            
            data = r.json()
            name = data.get('name', 'Lista {0}'.format(list_id))
            user = data.get('user', {}).get('username', 'Unknown')
            item_count = data.get('item_count', 0)
            
    
            core_settings.add_trakt_list(list_id, name, user, item_count)
            
            pDialog.close()
            
            # UX Mejorada: Preguntar si quiere abrirla o ir a ver sus listas
            if xbmcgui.Dialog().yesno("EspaTV", "Lista '{0}' importada con éxito.\n\n¿Quieres abrirla ahora mismo?".format(name)):
                xbmc.executebuiltin("Container.Update({0})".format(_u(action='trakt_view_list', list_id=list_id)))
            else:
                # Si dice que no, lo sacamos de "Opciones" y lo llevamos a la raíz de Trakt
                # donde verá su nueva lista recién añadida
                xbmc.executebuiltin("Container.Update({0})".format(_u(action='trakt_root')))
            
        except Exception as e:
            if 'pDialog' in locals(): pDialog.close()
            xbmcgui.Dialog().notification("Error", str(e), xbmcgui.NOTIFICATION_ERROR)

def view_list(list_id, show_covers=False):
    try:
        key = core_settings.get_trakt_api_key()
        if not key:
            xbmcgui.Dialog().notification("Error", "Falta API Key", xbmcgui.NOTIFICATION_ERROR)
            return

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': key
        }
        
        url = 'https://api.trakt.tv/lists/{0}/items?extended=full'.format(list_id)
        
        try:
            r = requests.get(url, headers=headers, timeout=15)
            items = r.json()
        except Exception:
            xbmcplugin.endOfDirectory(_handle(), cacheToDisc=True)
            return

        if not isinstance(items, list):
            msg = items.get('message', 'Error desconocido') if isinstance(items, dict) else "Respuesta no válida"
            xbmcgui.Dialog().ok("Error Trakt", "No se pudieron cargar los elementos:\n{0}".format(msg))
            xbmcplugin.endOfDirectory(_handle(), cacheToDisc=True)
            return

        addon = xbmcaddon.Addon()
        ai = addon.getAddonInfo('icon')
        af = addon.getAddonInfo('fanart')


        cache = _load_thumb_cache(list_id)
        has_cache = len(cache) > 0


        if not has_cache:
            li = xbmcgui.ListItem(label="[COLOR yellow][B]>>> Descargar y GUARDAR Carátulas permanentemente <<<[/B][/COLOR]")
            li.setArt({'icon': 'DefaultAddonService.png'})
            li.setInfo('video', {'plot': "Esta opción buscará las fotos de TODA la lista y las guardará en tu dispositivo.\n\n[COLOR red][B]ADVERTENCIA:[/B][/COLOR] Una vez guardadas, la lista tardará un poco más en cargar cada vez que entres, pero se verá mucho mejor."})
            xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_cache_covers", list_id=list_id), listitem=li, isFolder=False)
            
            if not show_covers:
                li = xbmcgui.ListItem(label="[COLOR green]Ver carátulas solo esta vez (Sin guardar)[/COLOR]")
                li.setArt({'icon': 'DefaultAddonService.png'})
                xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_view_list", list_id=list_id, show_covers="1"), listitem=li, isFolder=True)
            else:
                li = xbmcgui.ListItem(label="[COLOR cyan]Ocultar carátulas temporales[/COLOR]")
                li.setArt({'icon': 'DefaultAddonService.png'})
                xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_view_list", list_id=list_id, show_covers="0"), listitem=li, isFolder=True)
        else:
            li = xbmcgui.ListItem(label="[COLOR red][B]Borrar carátulas guardadas (Volver a carga rápida)[/B][/COLOR]")
            li.setArt({'icon': 'DefaultIconError.png'})
            li.setInfo('video', {'plot': "Elimina las fotos guardadas para que la lista vuelva a cargar al instante."})
            xbmcplugin.addDirectoryItem(handle=_handle(), url=_u(action="trakt_clear_cache", list_id=list_id), listitem=li, isFolder=False)

        for item in items:
            if not isinstance(item, dict): continue
            t = item.get('type')
            data = item.get(t)
            if not data: continue
            
            title = data.get('title')
            year = data.get('year')
            overview = data.get('overview', '')
            tagline = data.get('tagline', '')
            
            item_key = "{0}_{1}".format(title, year)
            label = "{0} ({1})".format(title, year) if year else title
            li = xbmcgui.ListItem(label=label)
            
            thumb = ai
            if has_cache and item_key in cache:
                thumb = cache[item_key]
            elif show_covers:
                try:
                    search_url = "https://api.dailymotion.com/videos"
                    params = {"search": "{0} {1} movie poster".format(title, year), "fields": "thumbnail_720_url,thumbnail_360_url", "limit": 1}
                    rs = requests.get(search_url, params=params, timeout=5).json()
                    if rs.get('list'):
                        best_img = rs['list'][0]
                        thumb = best_img.get('thumbnail_720_url') or best_img.get('thumbnail_360_url') or ai
                except Exception: thumb = ai

            li.setArt({'icon': 'DefaultVideo.png', 'thumb': thumb, 'poster': thumb, 'fanart': af})
            
            video_info = {
                'title': title,
                'year': year,
                'plot': "[B]{0}[/B]\n\n{1}".format(tagline, overview) if tagline else overview,
                'mediatype': 'movie' if t == 'movie' else 'tvshow'
            }
            li.setInfo('video', video_info)
            
            cm = []
            

            params_json = json.dumps({'q': title})
            fav_url = _u(action='add_favorite', title=title, fav_url=title, icon=thumb, platform='Trakt', fav_action='lfr', params=params_json)
            cm.append(("Añadir a Mis Favoritos", "RunPlugin({0})".format(fav_url)))
            
            li.addContextMenuItems(cm)
            
            play_url = _u(action='lfr', q=title)
            xbmcplugin.addDirectoryItem(handle=_handle(), url=play_url, listitem=li, isFolder=True)
            
        xbmcplugin.endOfDirectory(_handle(), cacheToDisc=True)
    except Exception as e:
        xbmcgui.Dialog().ok("Error Trakt", "Ocurrió un error al ver la lista:\n{0}".format(e))

def cache_covers(list_id):
    try:
        if not xbmcgui.Dialog().yesno("EspaTV", 
            "[B]AVISO DE CARGA:[/B]\n\n"
            "Al guardar las carátulas, el addon mostrará las fotos siempre que entres.\n\n"
            "Esto hará que la lista [COLOR yellow]tarde más en cargar[/COLOR] cada vez.\n\n"
            "¿Quieres continuar con la descarga?"):
            return

        key = core_settings.get_trakt_api_key()
        headers = {'Content-Type': 'application/json', 'trakt-api-version': '2', 'trakt-api-key': key}
        url = 'https://api.trakt.tv/lists/{0}/items?extended=full'.format(list_id)
        
        try:
            r = requests.get(url, headers=headers, timeout=15)
            items = r.json()
        except Exception: 
            xbmcgui.Dialog().notification("Error", "No se pudo conectar con Trakt", xbmcgui.NOTIFICATION_ERROR)
            return

        if not isinstance(items, list):
            msg = items.get('message', 'Error desconocido') if isinstance(items, dict) else "Respuesta no válida"
            xbmcgui.Dialog().ok("Error Trakt", "No se pudo obtener la lista para cachear:\n{0}".format(msg))
            return

        cache = {}
        pDialog = xbmcgui.DialogProgress()
        pDialog.create("EspaTV", "Descargando carátulas...")
        
        total = len(items)
        if total == 0:
            pDialog.close()
            xbmcgui.Dialog().notification("Trakt", "La lista está vacía", xbmcgui.NOTIFICATION_INFO)
            return

        for i, item in enumerate(items):
            if pDialog.iscanceled(): break
            if not isinstance(item, dict): continue
            
            t = item.get('type')
            data = item.get(t)
            if not data: continue
            
            title = data.get('title')
            year = data.get('year')
            item_key = "{0}_{1}".format(title, year)
            
            percent = int((float(i) / total) * 100)

            pDialog.update(percent, "Procesando ({0}/{1}): {2}".format(i, total, title))
            
            try:
                search_url = "https://api.dailymotion.com/videos"
                params = {"search": "{0} {1} movie poster".format(title, year), "fields": "thumbnail_720_url,thumbnail_360_url", "limit": 1}
                rs = requests.get(search_url, params=params, timeout=5).json()
                if rs.get('list'):
                    best_img = rs['list'][0]
                    cache[item_key] = best_img.get('thumbnail_720_url') or best_img.get('thumbnail_360_url')
            except Exception: pass
            
        pDialog.close()
        
        if cache:
            with open(_get_thumb_cache_path(list_id), 'w', encoding='utf-8') as f:
                json.dump(cache, f)
            xbmcgui.Dialog().notification("EspaTV", "Carátulas guardadas correctamente", xbmcgui.NOTIFICATION_INFO)
            xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        xbmcgui.Dialog().ok("Error Cache", "Error al guardar carátulas:\n{0}".format(e))

def clear_cache(list_id):
    try:
        import os
        path = _get_thumb_cache_path(list_id)
        if os.path.exists(path):
            os.remove(path)
            xbmcgui.Dialog().notification("EspaTV", "Cache eliminada: Volvemos a carga rápida", xbmcgui.NOTIFICATION_INFO)
            xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        xbmcgui.Dialog().notification("Error", str(e), xbmcgui.NOTIFICATION_ERROR)

def delete_list(list_id):
    try:
        if xbmcgui.Dialog().yesno("Eliminar Lista", "¿Seguro que quieres borrar esta lista de tu colección?"):
            core_settings.remove_trakt_list(list_id)
            xbmc.executebuiltin("Container.Refresh")
    except Exception as e:
        xbmcgui.Dialog().notification("Error", str(e), xbmcgui.NOTIFICATION_ERROR)