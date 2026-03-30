# -*- coding: utf-8 -*-
# EspaTV — Copyright (C) 2024-2026 RubénSDFA1labernt (github.com/espakodi)
# Licencia: GPL-2.0-or-later — Consulta el archivo LICENSE para mas detalles.
import sys
import os
import json
import urllib.parse
import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs
import time
import core_settings

# Rutas (inicializacion lazy)
_cats_file = None

def _get_cats_file():
    global _cats_file
    if _cats_file is None:
        profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile'))
        if not os.path.exists(profile): os.makedirs(profile)
        _cats_file = os.path.join(profile, 'custom_categories.json')
    return _cats_file

# Auxiliares
def _u(**kwargs):
    return sys.argv[0] + "?" + urllib.parse.urlencode(kwargs)

def _handle():
    return int(sys.argv[1])

def load_cats():
    f = _get_cats_file()
    if not os.path.exists(f): return {}
    try:
        with open(f, 'r', encoding='utf-8') as fp:
            return json.load(fp)
    except Exception: return {}

def save_cats(data):
    f = _get_cats_file()
    try:
        with open(f, 'w', encoding='utf-8') as fp:
            json.dump(data, fp, ensure_ascii=False)
    except Exception: pass

# Acciones

def main_menu():
    """Muestra la lista de categorías creadas por el usuario"""
    cats = load_cats()
    h = _handle()
    
    # 1. Crear Nueva
    li = xbmcgui.ListItem(label="[COLOR green][B]+ CREAR NUEVA CATEGORÍA[/B][/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="cat_create"), listitem=li, isFolder=False)
    
    # 2. Listar Categorías
    for name in sorted(cats.keys()):
        num_items = len(cats[name])
        li = xbmcgui.ListItem(label="{0} [COLOR gray]({1})[/COLOR]".format(name, num_items))
        li.setArt({'icon': 'DefaultFolder.png'})
        
        # Context Menu: Borrar / Renombrar
        cm = [
            ("Renombrar Categoría", "RunPlugin({0})".format(_u(action='cat_rename', name=name))),
            ("Borrar Categoría", "RunPlugin({0})".format(_u(action='cat_delete', name=name)))
        ]
        li.addContextMenuItems(cm)
        
        xbmcplugin.addDirectoryItem(handle=h, url=_u(action="cat_view", name=name), listitem=li, isFolder=True)
    
    # 3. Exportar / Importar
    li = xbmcgui.ListItem(label="[COLOR cyan]Exportar Categorías...[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Guarda tus categorías en un archivo JSON para compartirlas o hacer una copia de seguridad."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="cat_export"), listitem=li, isFolder=False)
    
    li = xbmcgui.ListItem(label="[COLOR cyan]Importar Categorías...[/COLOR]")
    li.setArt({'icon': 'DefaultAddonService.png'})
    li.setInfo('video', {'plot': "Carga categorías desde un archivo JSON. Puedes fusionarlas con las existentes o reemplazarlas."})
    xbmcplugin.addDirectoryItem(handle=h, url=_u(action="cat_import"), listitem=li, isFolder=False)
        
    xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())

def create_category():
    kb = xbmc.Keyboard('', 'Nombre de la nueva categoría')
    kb.doModal()
    if kb.isConfirmed():
        name = kb.getText().strip()
        if not name: return
        
        cats = load_cats()
        if name in cats:
            xbmcgui.Dialog().notification("EspaTV", "La categoría ya existe", xbmcgui.NOTIFICATION_ERROR)
            return
            
        cats[name] = []
        save_cats(cats)
        xbmc.executebuiltin("Container.Refresh")

def delete_category(name):
    if not name: return
    if not xbmcgui.Dialog().yesno("Borrar Categoría", "¿Estás seguro de borrar '{0}'?\nSe perderá la organización de los elementos dentro.".format(name)):
        return
        
    cats = load_cats()
    if name in cats:
        del cats[name]
        save_cats(cats)
        time.sleep(0.2)
        xbmc.executebuiltin("Container.Refresh")
    else:
        xbmcgui.Dialog().notification("EspaTV", "Error: Categoría '{0}' no encontrada".format(name), xbmcgui.NOTIFICATION_ERROR)

def rename_category(name):
    if not name: return
    kb = xbmc.Keyboard(name, 'Renombrar categoría')
    kb.doModal()
    if kb.isConfirmed():
        new_name = kb.getText().strip()
        if not new_name or new_name == name: return
        
        cats = load_cats()
        if new_name in cats:
            xbmcgui.Dialog().notification("EspaTV", "Ya existe una categoría con ese nombre", xbmcgui.NOTIFICATION_ERROR)
            return
            
        items = cats.get(name, [])
        del cats[name]
        cats[new_name] = items
        save_cats(cats)
        xbmc.executebuiltin("Container.Refresh")

def view_category(name):
    """Lista los items guardados en una categoría y los busca al hacer click"""
    if not name: return
    
    cats = load_cats()
    items = cats.get(name, [])
    h = _handle()
    
    if not items:
        li = xbmcgui.ListItem(label="[COLOR gray]Categoría vacía. Añade elementos desde el historial.[/COLOR]")
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(handle=h, url="", listitem=li, isFolder=False)
    else:
        addon = xbmcaddon.Addon()
        icon = addon.getAddonInfo('icon')
        
        for q in items:
            li = xbmcgui.ListItem(label=q)
            li.setArt({'icon': 'DefaultFolder.png', 'thumb': icon})
            
            cm = []
            cm.append(("Editar y buscar", "RunPlugin({0})".format(_u(action='edit_and_search', q=q, ot=icon))))
            cm.append(("Mover a otra categoría...", "RunPlugin({0})".format(_u(action='cat_move_item', from_cat=name, q=q))))
            cm.append(("Quitar de esta categoría", "RunPlugin({0})".format(_u(action='cat_remove_item', cat=name, q=q))))
            li.addContextMenuItems(cm)
            
            xbmcplugin.addDirectoryItem(handle=h, url=_u(action="lfr", q=q, ot=icon, nh=1), listitem=li, isFolder=True)
            
    xbmcplugin.endOfDirectory(h, cacheToDisc=core_settings.is_iptv_cache_active())

def add_item_dialog(q):
    """Muestra dialogo para añadir 'q' a una categoría"""
    if not q: return
    
    cats = load_cats()
    
    if not cats:
        if xbmcgui.Dialog().yesno("Sin Categorías", "No tienes categorías creadas.\n¿Quieres crear una ahora?"):
            create_category()
            cats = load_cats()
            if not cats: return
        else:
            return

    cat_names = sorted(cats.keys())
    
    sel = xbmcgui.Dialog().select("Añadir '{0}' a...".format(q), cat_names)
    if sel < 0: return
    
    target_cat = cat_names[sel]
    
    if q not in cats[target_cat]:
        cats[target_cat].append(q)
        save_cats(cats)
        xbmcgui.Dialog().notification("EspaTV", "Añadido a '{0}'".format(target_cat), xbmcgui.NOTIFICATION_INFO)
    else:
        xbmcgui.Dialog().notification("EspaTV", "Ya estaba en esa categoría", xbmcgui.NOTIFICATION_INFO)
    
    return True

def remove_item(cat, q):
    if not cat or not q: return
    cats = load_cats()
    if cat in cats and q in cats[cat]:
        cats[cat].remove(q)
        save_cats(cats)
        xbmc.executebuiltin("Container.Refresh")

def move_item(from_cat, q):
    """Mueve un elemento de una categoría a otra"""
    if not from_cat or not q: return
    
    cats = load_cats()
    
    # Categorías disponibles (excluyendo la actual)
    other_cats = [c for c in sorted(cats.keys()) if c != from_cat]
    
    if not other_cats:
        xbmcgui.Dialog().notification("EspaTV", "No hay otras categorías", xbmcgui.NOTIFICATION_INFO)
        return
    
    sel = xbmcgui.Dialog().select("Mover '{0}' a...".format(q), other_cats)
    if sel < 0: return
    
    target_cat = other_cats[sel]
    
    # Quitar de la categoría origen
    if from_cat in cats and q in cats[from_cat]:
        cats[from_cat].remove(q)
    
    # Añadir al destino (si no existe ya)
    if q not in cats[target_cat]:
        cats[target_cat].append(q)
    
    save_cats(cats)
    xbmcgui.Dialog().notification("EspaTV", "Movido a '{0}'".format(target_cat), xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin("Container.Refresh")

def export_categories():
    """Exporta las categorías a un archivo JSON"""
    import time
    
    cats = load_cats()
    if not cats:
        xbmcgui.Dialog().notification("EspaTV", "No hay categorías para exportar", xbmcgui.NOTIFICATION_INFO)
        return
    
    ts = time.strftime("%Y%m%d_%H%M")
    default_name = "EspaTV_categorias_{0}.json".format(ts)
    
    d = xbmcgui.Dialog().browse(3, 'Guardar Categorías', 'files', '', False, False, default_name)
    if not d: return
    
    if os.path.isdir(d):
        d = os.path.join(d, default_name)
    elif not d.lower().endswith(".json"):
        d += ".json"
    
    try:
        with open(d, 'w', encoding='utf-8') as f:
            json.dump(cats, f, ensure_ascii=False, indent=2)
        
        total_items = sum(len(v) for v in cats.values())
        xbmcgui.Dialog().ok("Exportar Categorías", "Guardado correctamente:\n{0}\n\nCategorías: {1}\nElementos: {2}".format(os.path.basename(d), len(cats), total_items))
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))

def import_categories():
    """Importa categorías desde un archivo JSON"""
    f = xbmcgui.Dialog().browse(1, 'Seleccionar archivo de categorías', 'files', '.json', False, False, '')
    if not f: return
    
    try:
        with open(f, 'r', encoding='utf-8') as fp:
            new_cats = json.load(fp)
        
        if not isinstance(new_cats, dict):
            xbmcgui.Dialog().ok("Error", "El archivo no tiene el formato correcto.")
            return
        
        # Modo de importación
        opts = ["Fusionar con las existentes", "Reemplazar todas"]
        sel = xbmcgui.Dialog().select("¿Cómo importar?", opts)
        if sel < 0: return
        
        if sel == 0:
            # Fusionar
            existing = load_cats()
            for cat_name, items in new_cats.items():
                if cat_name in existing:
                    # Añadir solo los que no existan
                    for item in items:
                        if item not in existing[cat_name]:
                            existing[cat_name].append(item)
                else:
                    existing[cat_name] = items
            save_cats(existing)
            xbmcgui.Dialog().notification("EspaTV", "Categorías fusionadas", xbmcgui.NOTIFICATION_INFO)
        else:
            # Reemplazar todo
            save_cats(new_cats)
            xbmcgui.Dialog().notification("EspaTV", "Categorías reemplazadas", xbmcgui.NOTIFICATION_INFO)
        
        xbmc.executebuiltin("Container.Refresh")
        
    except Exception as e:
        xbmcgui.Dialog().ok("Error", str(e))