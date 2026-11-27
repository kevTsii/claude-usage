"""Icône dans la barre GNOME via le protocole StatusNotifierItem (D-Bus).

Parle directement à l'extension « ubuntu-appindicators » : aucun paquet à
installer, et pas de conflit GTK 3 / GTK 4 (la bibliothèque AppIndicator
classique charge GTK 3, alors que l'app est en GTK 4).

Ce que l'extension affiche :
  - une image large dessinée ici (PNG dans ~/.cache/claude-usage) : un anneau
    de progression suivi du pourcentage, pour chaque valeur, chacun dans la
    couleur de son niveau, par exemple « ◯ 54 % · ◯ 48 % ». L'extension
    n'affiche une image à sa taille réelle que si elle vient d'un fichier du
    dossier cache et que sa largeur dépasse 1,5 fois sa hauteur ;
  - un menu (com.canonical.dbusmenu) ouvert par un clic gauche ou droit ;
  - un double-clic appelle Activate().
"""

import hashlib
import math
import os
import re
import sys
import time
from pathlib import Path

import cairo
from gi.repository import Gio, GLib

ITEM_PATH = '/StatusNotifierItem'
MENU_PATH = '/StatusNotifierItem/Menu'
ITEM_IFACE = 'org.kde.StatusNotifierItem'
WATCHER_NAME = 'org.kde.StatusNotifierWatcher'
WATCHER_PATH = '/StatusNotifierWatcher'

# Palette GNOME claire, lisible sur la barre sombre. Niveaux : voir
# usage_level() dans claude_usage.py (vert sous 75 %, orange dès 75 %,
# rouge dès 90 %).
LEVEL_COLORS = {
    'ok': (87, 227, 137),
    'warn': (255, 163, 72),
    'crit': (246, 97, 81),
    'idle': (190, 190, 190),
}

STRIP_HEIGHT = 24
RING_DIAMETER = 16
RING_THICKNESS = 3.0
TEXT_GAP = 5
SEPARATOR_GAP = 8
STRIP_PADDING = 2
MIN_STRIP_WIDTH = 40  # sous 1,5 x la hauteur, l'extension réduirait l'image
KEPT_STRIPS = 2
DEFAULT_FONT = 'Ubuntu Sans 11'

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <method name="ContextMenu">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Activate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="orientation" direction="in"/>
    </method>
    <method name="ProvideXdgActivationToken">
      <arg type="s" name="token" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus">
      <arg type="s" name="status"/>
    </signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timeStamp"/>
    </signal>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePaths" type="as" access="read"/>
  </interface>
</node>
"""

# Identifiants fixes des entrées du menu (0 est la racine).
INFO_IDS = (1, 2, 3, 4, 5)
SEPARATOR_TOP = 6
OPEN_ID = 7
REFRESH_ID = 8
SEPARATOR_BOTTOM = 9
QUIT_ID = 10
AUTOSTART_ID = 11


def system_font():
    """Police d'interface du bureau (celle du texte de la barre)."""
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup('org.gnome.desktop.interface', True) is None:
        return DEFAULT_FONT
    settings = Gio.Settings.new('org.gnome.desktop.interface')

    return settings.get_string('font-name') or DEFAULT_FONT


def parse_font(name):
    """« Ubuntu Sans 11 » devient ('Ubuntu Sans', 14,67 pixels à 96 ppp)."""
    match = re.match(r'^(.*?)\s+(\d+(?:\.\d+)?)$', name.strip())
    if match is None:
        return name.strip() or 'Sans', 14.67

    return match.group(1), float(match.group(2)) * 96 / 72


def draw_text(context, text, x, center_y):
    """Écrit le texte à partir de x, centré verticalement sur center_y."""
    extents = context.text_extents(text)
    context.move_to(x, center_y - (extents.y_bearing + extents.height / 2))
    context.show_text(text)


def render_strip(items, font_name):
    """Dessine la bande : pour chaque valeur un anneau puis son pourcentage.

    items : liste de dict {'text', 'fraction' (0 à 1), 'level'}.
    """
    family, pixels = parse_font(font_name)

    def prepare(context):
        # lissage en niveaux de gris : pas de franges colorées sur fond transparent
        options = cairo.FontOptions()
        options.set_antialias(cairo.ANTIALIAS_GRAY)
        context.set_font_options(options)
        context.select_font_face(
            family,
            cairo.FONT_SLANT_NORMAL,
            cairo.FONT_WEIGHT_BOLD,
        )
        context.set_font_size(pixels)

    probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    prepare(probe)
    widths = [probe.text_extents(item['text']).x_advance for item in items]
    separator_width = probe.text_extents('·').x_advance

    width = 2 * STRIP_PADDING + sum(RING_DIAMETER + TEXT_GAP + w for w in widths)
    width += (len(items) - 1) * (2 * SEPARATOR_GAP + separator_width)
    width = max(math.ceil(width), MIN_STRIP_WIDTH)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, STRIP_HEIGHT)
    context = cairo.Context(surface)
    prepare(context)
    center_y = STRIP_HEIGHT / 2
    radius = (RING_DIAMETER - RING_THICKNESS) / 2
    x = STRIP_PADDING

    for index, item in enumerate(items):
        if index:
            x += SEPARATOR_GAP
            context.set_source_rgba(1, 1, 1, 0.55)
            draw_text(context, '·', x, center_y)
            x += separator_width + SEPARATOR_GAP

        red, green, blue = (
            channel / 255
            for channel in LEVEL_COLORS.get(item['level'], LEVEL_COLORS['idle'])
        )
        fraction = max(0.0, min(item['fraction'], 1.0))
        center_x = x + RING_DIAMETER / 2
        context.new_path()  # le texte précédent laisse un point courant
        context.set_line_width(RING_THICKNESS)
        context.set_source_rgba(red, green, blue, 0.32)
        context.arc(center_x, center_y, radius, 0, 2 * math.pi)
        context.stroke()
        if fraction > 0:
            context.set_source_rgba(red, green, blue, 1)
            context.arc(
                center_x,
                center_y,
                radius,
                -math.pi / 2,
                -math.pi / 2 + 2 * math.pi * fraction,
            )
            context.stroke()
        x += RING_DIAMETER + TEXT_GAP

        context.set_source_rgba(red, green, blue, 1)
        draw_text(context, item['text'], x, center_y)
        x += widths[index]

    return surface


class StatusTray:
    """Icône de barre : appelle on_activate / on_refresh / on_quit.

    on_autostart est appelé quand on clique sur la case « Démarrer avec
    Ubuntu » ; l'état affiché se règle avec set_autostart().
    """

    def __init__(self, title, on_activate, on_refresh, on_quit,
                 on_autostart=None):
        self.title = title
        self.on_activate = on_activate
        self.on_refresh = on_refresh
        self.on_quit = on_quit
        self.on_autostart = on_autostart
        self.autostart = False
        self.on_availability = None
        self.available = False

        self.connection = None
        self.bus_name = None
        self.owner_id = 0
        self.watch_id = 0
        self.registrations = []
        self.name_acquired = False
        self.watcher_present = False
        self.registered = False
        self.activation_token = None

        self.icon_key = None
        self.icon_path = ''
        self.strip_dir = Path(GLib.get_user_cache_dir()) / 'claude-usage'
        self.strip_files = []
        self.font = DEFAULT_FONT
        self.revision = 1
        self.lines = [''] * len(INFO_IDS)

    # -- cycle de vie ---------------------------------------------------------

    def start(self):
        self.font = system_font()
        self.clean_old_strips()
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.registrations = [
            self.connection.register_object(
                ITEM_PATH,
                item_info,
                self.on_item_call,
                self.on_item_get,
                None,
            ),
            self.connection.register_object(
                MENU_PATH,
                menu_info,
                self.on_menu_call,
                self.on_menu_get,
                None,
            ),
        ]
        self.bus_name = 'org.kde.StatusNotifierItem-%d-1' % os.getpid()
        self.owner_id = Gio.bus_own_name_on_connection(
            self.connection,
            self.bus_name,
            Gio.BusNameOwnerFlags.NONE,
            self.on_name_acquired,
            self.on_name_lost,
        )
        self.watch_id = Gio.bus_watch_name_on_connection(
            self.connection,
            WATCHER_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self.on_watcher_appeared,
            self.on_watcher_vanished,
        )

    def stop(self):
        if self.connection is None:
            return
        if self.watch_id:
            Gio.bus_unwatch_name(self.watch_id)
        for registration in self.registrations:
            self.connection.unregister_object(registration)
        if self.owner_id:
            Gio.bus_unown_name(self.owner_id)
        self.registrations = []
        self.watch_id = self.owner_id = 0
        self.set_available(False)
        for path in self.strip_files:
            path.unlink(missing_ok=True)
        self.strip_files = []

    def set_available(self, available):
        if available == self.available:
            return
        self.available = available
        if self.on_availability:
            self.on_availability(available)

    # -- enregistrement auprès de l'extension -----------------------------

    def on_name_acquired(self, _connection, _name):
        self.name_acquired = True
        self.try_register()

    def on_name_lost(self, _connection, _name):
        self.name_acquired = False
        self.registered = False
        self.set_available(False)

    def on_watcher_appeared(self, _connection, _name, _owner):
        self.watcher_present = True
        self.registered = False
        self.try_register()

    def on_watcher_vanished(self, _connection, _name):
        self.watcher_present = False
        self.registered = False
        self.set_available(False)

    def try_register(self):
        if self.registered or not (self.name_acquired and self.watcher_present):
            return
        self.registered = True
        self.connection.call(
            WATCHER_NAME,
            WATCHER_PATH,
            WATCHER_NAME,
            'RegisterStatusNotifierItem',
            GLib.Variant('(s)', (self.bus_name,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self.on_register_done,
        )

    def on_register_done(self, connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error:
            self.registered = False
            self.set_available(False)

            return
        self.set_available(True)

    # -- mise à jour depuis l'app -------------------------------------------

    def update(self, items, lines):
        """items : valeurs à dessiner (voir render_strip) ; lines : 5 textes."""
        if self.connection is None:
            return

        key = tuple(
            (item['text'], round(item['fraction'] * 100), item['level'])
            for item in items
        )
        if key != self.icon_key:
            try:
                path = self.write_strip(items, key)
            except Exception as error:  # on garde l'image précédente
                print('Icône de barre : %s' % error, file=sys.stderr)
            else:
                self.icon_key = key
                self.icon_path = str(path)
                self.emit(ITEM_PATH, ITEM_IFACE, 'NewIcon', None)

        if list(lines) != self.lines:
            self.lines = list(lines)
            self.bump_menu()

    def write_strip(self, items, key):
        """Écrit l'image dans le cache, sous un nom neuf à chaque contenu.

        Un nom neuf évite que le shell réutilise une ancienne version en
        cache ; le pid évite de supprimer les fichiers d'une autre instance.
        """
        digest = hashlib.sha1(repr(key).encode()).hexdigest()[:10]
        path = self.strip_dir / ('strip-%d-%s.png' % (os.getpid(), digest))
        if not path.exists():
            self.strip_dir.mkdir(parents=True, exist_ok=True)
            render_strip(items, self.font).write_to_png(str(path))
        if path in self.strip_files:
            self.strip_files.remove(path)
        self.strip_files.append(path)
        while len(self.strip_files) > KEPT_STRIPS:
            self.strip_files.pop(0).unlink(missing_ok=True)

        return path

    def clean_old_strips(self):
        """Supprime les images oubliées par une exécution interrompue."""
        limit = time.time() - 24 * 3600
        for path in self.strip_dir.glob('strip-*.png'):
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
            except OSError:
                pass

    def set_autostart(self, enabled):
        """Coche ou décoche « Démarrer avec Ubuntu » dans le menu."""
        if enabled == self.autostart:
            return
        self.autostart = enabled
        if self.connection is not None:
            self.bump_menu()

    def bump_menu(self):
        self.revision += 1
        self.emit(
            MENU_PATH,
            'com.canonical.dbusmenu',
            'LayoutUpdated',
            GLib.Variant('(ui)', (self.revision, 0)),
        )

    def emit(self, path, interface, signal, parameters):
        self.connection.emit_signal(None, path, interface, signal, parameters)

    # -- interface org.kde.StatusNotifierItem -----------------------------

    def on_item_get(self, _connection, _sender, _path, _interface, name):
        values = {
            'Category': GLib.Variant('s', 'ApplicationStatus'),
            'Id': GLib.Variant('s', 'claude-usage'),
            'Title': GLib.Variant('s', self.title),
            'Status': GLib.Variant('s', 'Active'),
            'WindowId': GLib.Variant('i', 0),
            'IconThemePath': GLib.Variant('s', ''),
            'Menu': GLib.Variant('o', MENU_PATH),
            'ItemIsMenu': GLib.Variant('b', False),
            'IconName': GLib.Variant('s', self.icon_path),
            'IconPixmap': GLib.Variant('a(iiay)', []),
        }

        return values.get(name)

    def on_item_call(self, _connection, _sender, _path, _interface, method,
                     parameters, invocation):
        if method == 'Activate':
            token, self.activation_token = self.activation_token, None
            self.defer(self.on_activate, token)
        elif method == 'ProvideXdgActivationToken':
            self.activation_token = parameters.unpack()[0]
        elif method not in ('ContextMenu', 'SecondaryActivate', 'Scroll'):
            invocation.return_dbus_error(
                'org.freedesktop.DBus.Error.UnknownMethod',
                'Méthode inconnue : %s' % method,
            )

            return
        invocation.return_value(None)

    # -- interface com.canonical.dbusmenu ---------------------------------

    def menu_items(self):
        """Liste (id, libellé ou None pour un séparateur, action)."""
        items = [
            (item_id, text, 'activate')
            for item_id, text in zip(INFO_IDS, self.lines)
        ]
        items += [
            (SEPARATOR_TOP, None, None),
            (OPEN_ID, 'Ouvrir la fenêtre', 'activate'),
            (REFRESH_ID, 'Actualiser', 'refresh'),
            (AUTOSTART_ID, 'Démarrer avec Ubuntu', 'autostart'),
            (SEPARATOR_BOTTOM, None, None),
            (QUIT_ID, 'Quitter', 'quit'),
        ]

        return items

    def item_props(self, item_id, text):
        if text is None:
            return {'type': GLib.Variant('s', 'separator')}

        props = {
            'label': GLib.Variant('s', text.replace('_', '__')),
            'enabled': GLib.Variant('b', True),
            'visible': GLib.Variant('b', bool(text)),
        }
        if item_id == AUTOSTART_ID:
            props['toggle-type'] = GLib.Variant('s', 'checkmark')
            props['toggle-state'] = GLib.Variant('i', 1 if self.autostart else 0)

        return props

    def node(self, item_id, depth):
        """Structure (ia{sv}av) d'une entrée, avec ses enfants si depth != 0."""
        if item_id == 0:
            children = []
            if depth != 0:
                children = [
                    GLib.Variant('(ia{sv}av)', self.node(child_id, depth - 1))
                    for child_id, _text, _action in self.menu_items()
                ]

            return (0, {'children-display': GLib.Variant('s', 'submenu')}, children)

        for child_id, text, _action in self.menu_items():
            if child_id == item_id:
                return (item_id, self.item_props(item_id, text), [])

        return (item_id, {}, [])

    def on_menu_get(self, _connection, _sender, _path, _interface, name):
        values = {
            'Version': GLib.Variant('u', 3),
            'TextDirection': GLib.Variant('s', 'ltr'),
            'Status': GLib.Variant('s', 'normal'),
            'IconThemePaths': GLib.Variant('as', []),
        }

        return values.get(name)

    def on_menu_call(self, _connection, _sender, _path, _interface, method,
                     parameters, invocation):
        try:
            result = self.menu_method(method, parameters.unpack())
        except Exception as error:  # ne jamais laisser l'appelant en attente
            invocation.return_dbus_error(
                'org.freedesktop.DBus.Error.Failed',
                str(error),
            )

            return
        invocation.return_value(result)

    def menu_method(self, method, args):
        if method == 'GetLayout':
            parent, depth, _names = args

            return GLib.Variant('(u(ia{sv}av))', (self.revision, self.node(parent, depth)))
        if method == 'GetGroupProperties':
            ids, _names = args
            wanted = ids or [0] + [item[0] for item in self.menu_items()]
            nodes = [self.node(item_id, 0) for item_id in wanted]

            return GLib.Variant('(a(ia{sv}))', ([(n[0], n[1]) for n in nodes],))
        if method == 'GetProperty':
            item_id, name = args
            value = self.node(item_id, 0)[1].get(name, GLib.Variant('s', ''))

            return GLib.Variant('(v)', (value,))
        if method == 'Event':
            item_id, event, _data, _timestamp = args
            self.handle_event(item_id, event)

            return None
        if method == 'EventGroup':
            for item_id, event, _data, _timestamp in args[0]:
                self.handle_event(item_id, event)

            return GLib.Variant('(ai)', ([],))
        if method == 'AboutToShow':
            return GLib.Variant('(b)', (False,))
        if method == 'AboutToShowGroup':
            return GLib.Variant('(aiai)', ([], []))

        raise ValueError('Méthode inconnue : %s' % method)

    def handle_event(self, item_id, event):
        if event != 'clicked':
            return
        for child_id, _text, action in self.menu_items():
            if child_id != item_id:
                continue
            if action == 'activate':
                self.defer(self.on_activate, None)
            elif action == 'refresh':
                self.defer(self.on_refresh)
            elif action == 'autostart' and self.on_autostart:
                self.defer(self.on_autostart)
            elif action == 'quit':
                self.defer(self.on_quit)

    @staticmethod
    def defer(callback, *args):
        """Exécute le rappel hors du traitement de l'appel D-Bus."""
        GLib.idle_add(lambda: callback(*args) and False)
