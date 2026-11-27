#!/usr/bin/env python3
"""Suivi de l'utilisation du forfait Claude (session, semaine, crédits).

    python3 claude_usage.py           fenêtre GTK + icône dans la barre GNOME
    python3 claude_usage.py --hidden  démarre réduit dans la barre, sans fenêtre
    python3 claude_usage.py --text    affichage console, une seule fois

L'icône (status_tray.py) affiche les pourcentages session et semaine à côté
d'un anneau de progression. Clic : menu avec le détail ; double-clic : ouvre
la fenêtre. Fermer la fenêtre la réduit dans la barre ; « Quitter » est dans
le menu de l'icône. Sans l'extension « ubuntu-appindicators », fermer la
fenêtre quitte l'app.

La case « Démarrer avec Ubuntu » (menu de la fenêtre et de l'icône) crée ou
supprime ~/.config/autostart/local.ClaudeUsage.desktop, comme le fait
l'outil « Applications au démarrage » ; l'app démarre alors avec --hidden.

Les données viennent du même endpoint que Paramètres > Utilisation sur
claude.ai, interrogé avec le jeton OAuth de Claude Code
(~/.claude/.credentials.json, ou $CLAUDE_CONFIG_DIR si défini).

Le jeton n'est lu qu'en mémoire : jamais affiché, jamais copié, jamais
rafraîchi par cette app (c'est Claude Code qui s'en charge). La connexion
passe par `claude auth login`, donc exactement comme /login : le navigateur
s'ouvre, puis les identifiants sont écrits par Claude Code lui-même.
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from status_tray import LEVEL_COLORS, StatusTray  # noqa: E402

APP_ID = 'local.ClaudeUsage'
USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
NETWORK_REFRESH_SECONDS = 120
RENDER_REFRESH_SECONDS = 30
FOCUS_REFRESH_MIN_SECONDS = 30
LOGIN_HOSTS = ('claude.ai', 'claude.com', 'anthropic.com')
URL_PATTERN = re.compile(r'https://[^\s<>"\']+')
DAYS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']

LEVELS = ('ok', 'warn', 'crit')

# Même échelle que l'icône de la barre ; teintes plus foncées en thème clair.
PALETTES = {
    'dark': {
        level: '#%02x%02x%02x' % LEVEL_COLORS[level] for level in LEVELS
    },
    'light': {
        'ok': '#26a269',
        'warn': '#c64600',
        'crit': '#c01c28',
    },
}


def build_css(palette):
    css = 'progressbar trough, progressbar progress { min-height: 8px; }\n'
    for level, color in palette.items():
        css += 'progressbar.lvl-%s progress { background: %s; }\n' % (level, color)
        css += 'label.lvl-%s { color: %s; font-weight: 600; }\n' % (level, color)

    return css


class AuthRequired(Exception):
    """Aucun jeton exploitable : l'utilisateur doit se (re)connecter."""


class UsageError(Exception):
    """Echec temporaire (réseau, limitation, erreur serveur)."""


# --------------------------------------------------------------------------
# Données
# --------------------------------------------------------------------------

def config_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def load_credentials():
    path = config_dir() / '.credentials.json'
    try:
        oauth = json.loads(path.read_text())['claudeAiOauth']
        token = oauth['accessToken']
        expires_at = oauth['expiresAt'] / 1000
    except (OSError, ValueError, KeyError, TypeError):
        raise AuthRequired("Vous n'êtes pas connecté à Claude.")

    if expires_at < time.time() + 30:
        raise AuthRequired('Votre session a expiré.')

    return {'token': token, 'plan': oauth.get('subscriptionType') or ''}


def autostart_path():
    return Path(GLib.get_user_config_dir()) / 'autostart' / (APP_ID + '.desktop')


def autostart_enabled():
    """Vrai si l'entrée existe et n'a pas été désactivée (Applications au démarrage)."""
    try:
        lines = autostart_path().read_text().splitlines()
    except OSError:
        return False
    disabled = ('Hidden=true', 'X-GNOME-Autostart-enabled=false')

    return not any(line.strip() in disabled for line in lines)


def set_autostart(enabled):
    """Crée ou supprime ~/.config/autostart/<id>.desktop (comme Ubuntu le fait)."""
    path = autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)

        return

    # paquet installé : la commande système ; sinon le script courant
    command = shutil.which('claude-usage')
    if command is None:
        command = '"%s" "%s"' % (sys.executable, Path(__file__).resolve())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[Desktop Entry]\n'
        'Type=Application\n'
        'Name=Utilisation Claude\n'
        "Comment=Suivi de l'utilisation du forfait Claude\n"
        'Exec=%s --hidden\n'
        'Icon=claude-usage\n'
        'Terminal=false\n'
        'X-GNOME-Autostart-enabled=true\n'
        'X-GNOME-Autostart-Delay=3\n' % command
    )


def find_claude():
    found = shutil.which('claude')
    if found:
        return found
    local = Path.home() / '.local' / 'bin' / 'claude'

    return str(local) if local.exists() else None


def fetch_usage():
    credentials = load_credentials()
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            'Authorization': 'Bearer ' + credentials['token'],
            'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'claude-usage-widget/0.1',
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise AuthRequired('Votre session a expiré.')
        if error.code == 429:
            raise UsageError('Trop de requêtes, nouvel essai plus tard.')
        raise UsageError('Erreur du serveur (HTTP %d).' % error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UsageError('Connexion impossible.')
    except ValueError:
        raise UsageError('Réponse inattendue du serveur.')

    return parse_usage(data, credentials['plan'])


def parse_window(raw):
    if not isinstance(raw, dict) or raw.get('utilization') is None:
        return None

    resets_at = None
    if raw.get('resets_at'):
        try:
            resets_at = datetime.fromisoformat(raw['resets_at'])
        except ValueError:
            resets_at = None

    return {'percent': float(raw['utilization']), 'resets_at': resets_at}


def parse_credits(raw):
    if not isinstance(raw, dict):
        return None

    scale = 10 ** int(raw.get('decimal_places') or 2)
    limit = raw.get('monthly_limit')

    return {
        'enabled': bool(raw.get('is_enabled')),
        'used': (raw.get('used_credits') or 0) / scale,
        'limit': None if limit is None else limit / scale,
        'currency': raw.get('currency') or 'USD',
        'reason': raw.get('disabled_reason'),
    }


PRODUCT_NAMES = {
    'claude_code': 'Claude Code',
    'chat': 'Conversations',
    'cowork': 'Cowork',
    'other': 'Autre',
}


def parse_breakdown(raw):
    """Répartition de la semaine par produit (parts dont la somme vaut 100 %)."""
    if not isinstance(raw, dict):
        return None

    rows = []
    for row in raw.get('rows') or []:
        if not isinstance(row, dict) or row.get('percent') is None:
            continue
        name = PRODUCT_NAMES.get(row.get('key')) or row.get('display_name') or '?'
        rows.append({'name': name, 'percent': float(row['percent'])})

    return rows or None


def parse_usage(data, plan):
    if not isinstance(data, dict):
        raise UsageError('Réponse inattendue du serveur.')

    return {
        'plan': plan,
        'session': parse_window(data.get('five_hour')),
        'week': parse_window(data.get('seven_day')),
        'credits': parse_credits(data.get('extra_usage')),
        'breakdown': parse_breakdown(data.get('seven_day_breakdown')),
        'fetched_at': datetime.now().astimezone(),
    }


# --------------------------------------------------------------------------
# Mise en forme
# --------------------------------------------------------------------------

def percent_text(percent):
    return '%d %% utilisés' % round(percent)


def money_text(value, currency):
    text = ('%.2f' % value).replace('.', ',')
    if text.endswith(',00'):
        text = text[:-3]
    unit = '$US' if currency == 'USD' else currency

    return '%s %s' % (text, unit)


def remaining_text(seconds):
    minutes = max(int(seconds // 60), 0)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return '%d j %d h' % (days, hours)
    if hours:
        return '%d h %02d' % (hours, mins)

    return '%d min' % mins


def local_reset(resets_at):
    """Heure locale arrondie à la minute (09:59:59 s'affiche 10:00)."""
    rounded = resets_at + timedelta(seconds=30)

    return rounded.astimezone().replace(second=0, microsecond=0)


def reset_text(window, now):
    if window['resets_at'] is None:
        return 'Aucune période en cours'

    local = local_reset(window['resets_at'])
    clock = local.strftime('%H:%M')
    if local.date() == now.date():
        label = 'Réinitialisation à %s' % clock
    else:
        label = 'Réinitialisation le %s %s' % (DAYS[local.weekday()], clock)
    remaining = (window['resets_at'] - now).total_seconds()

    return '%s · dans %s' % (label, remaining_text(remaining))


def weekly_headline(week, now):
    """Estimation maison du rythme, l'API ne fournit pas de message."""
    if week is None or week['resets_at'] is None:
        return None

    day = DAYS[local_reset(week['resets_at']).weekday()]
    if week['percent'] >= 100:
        return ('Limite hebdomadaire atteinte. Elle sera levée le %s.' % day, 'crit')

    total = timedelta(days=7).total_seconds()
    elapsed = total - (week['resets_at'] - now).total_seconds()
    if elapsed < total * 0.05:
        return None

    projected = week['percent'] / (elapsed / total)
    if projected <= 100:
        return (
            'En bonne voie. Vous devriez atteindre la réinitialisation '
            'de %s avec de la marge.' % day,
            'ok',
        )

    return (
        'Rythme soutenu. À ce rythme, la limite hebdomadaire sera atteinte '
        'avant la réinitialisation de %s.' % day,
        'warn',
    )


def credits_text(credits):
    used = money_text(credits['used'], credits['currency'])
    if credits['limit'] is None:
        return '%s dépensés ce mois-ci' % used

    return '%s sur %s ce mois-ci' % (
        used,
        money_text(credits['limit'], credits['currency']),
    )


SPEND_NOTE = (
    'Les crédits sont dépensés automatiquement au-delà des limites de '
    "votre forfait, jusqu'à ce plafond."
)


def ago_text(then, now):
    """« à l'instant », « il y a 3 min », puis l'heure."""
    seconds = (now - then).total_seconds()
    if seconds < 45:
        return "à l'instant"
    minutes = round(seconds / 60)
    if minutes < 60:
        return 'il y a %d min' % minutes

    return 'à %s' % then.strftime('%H:%M')


def usage_level(percent):
    if percent >= 90:
        return 'crit'
    if percent >= 75:
        return 'warn'

    return 'ok'


def set_level(widget, level):
    """Applique la couleur du niveau (classe lvl-*) à un widget."""
    for name in LEVELS:
        widget.remove_css_class('lvl-' + name)
    if level:
        widget.add_css_class('lvl-' + level)


def tray_summary(usage, now):
    """Contenu de l'icône : un anneau et un pourcentage par valeur, 5 lignes de menu."""
    session, week = usage['session'], usage['week']

    def item(window):
        if window is None:
            return {'text': '?', 'fraction': 0.0, 'level': 'idle'}

        return {
            'text': '%d %%' % round(window['percent']),
            'fraction': min(window['percent'] / 100, 1),
            'level': usage_level(window['percent']),
        }

    lines = []
    for title, window in (('Session actuelle', session), ('Cette semaine', week)):
        if window is None:
            lines += ['%s : indisponible' % title, '']
        else:
            lines += [
                '%s : %s' % (title, percent_text(window['percent'])),
                reset_text(window, now),
            ]
    credits = usage['credits']
    lines.append(('Crédits : %s' % credits_text(credits)) if credits else '')

    return {'items': [item(session), item(week)], 'lines': lines}


def tray_idle(label, message):
    return {
        'items': [{'text': label, 'fraction': 0.0, 'level': 'idle'}],
        'lines': [message, '', '', '', ''],
    }


def print_text():
    try:
        usage = fetch_usage()
    except AuthRequired as error:
        print('%s Lancez : claude auth login' % error, file=sys.stderr)
        return 1
    except UsageError as error:
        print(error, file=sys.stderr)
        return 1

    now = datetime.now().astimezone()
    print('Claude %s' % usage['plan'].capitalize())
    for label, key in (('Session actuelle', 'session'), ('Cette semaine', 'week')):
        window = usage[key]
        if window is None:
            print('%-17s: indisponible' % label)
        else:
            print('%-17s: %s · %s' % (
                label,
                percent_text(window['percent']),
                reset_text(window, now),
            ))
    if usage['credits']:
        print('%-17s: %s' % ('Crédits', credits_text(usage['credits'])))
    if usage['breakdown']:
        print('Par produit (semaine) :')
        for row in usage['breakdown']:
            print('  %-15s: %d %%' % (row['name'], round(row['percent'])))
    headline = weekly_headline(usage['week'], now)
    if headline:
        print(headline[0])

    return 0


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------

class UsageRow(Gtk.Box):
    def __init__(self, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        top = Gtk.Box(spacing=12)
        name = Gtk.Label(label=title, xalign=0, hexpand=True)
        name.add_css_class('heading')
        self.percent = Gtk.Label(xalign=1)
        top.append(name)
        top.append(self.percent)

        self.bar = Gtk.ProgressBar()
        self.subtitle = Gtk.Label(xalign=0, wrap=True)
        self.subtitle.add_css_class('dim-label')
        self.subtitle.add_css_class('caption')

        self.append(top)
        self.append(self.bar)
        self.append(self.subtitle)

    def update(self, window, now):
        if window is None:
            set_level(self.bar, None)
            set_level(self.percent, None)
            self.percent.set_text('Indisponible')
            self.bar.set_fraction(0)
            self.subtitle.set_text('')

            return

        level = usage_level(window['percent'])
        set_level(self.bar, level)
        set_level(self.percent, level)
        self.percent.set_text(percent_text(window['percent']))
        self.bar.set_fraction(min(window['percent'] / 100, 1))
        self.subtitle.set_text(reset_text(window, now))


class ProductRow(Gtk.Box):
    """Une ligne « produit, barre, part de la semaine » (barre en bleu neutre)."""

    def __init__(self):
        super().__init__(spacing=12)
        self.product_label = Gtk.Label(xalign=0, width_chars=14)
        self.bar = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
        self.share = Gtk.Label(xalign=1, width_chars=5)
        self.share.add_css_class('dim-label')
        for widget in (self.product_label, self.bar, self.share):
            self.append(widget)

    def update(self, row):
        self.product_label.set_text(row['name'])
        self.bar.set_fraction(min(row['percent'] / 100, 1))
        self.share.set_text('%d %%' % round(row['percent']))


class UsageWindow(Adw.ApplicationWindow):
    def __init__(self, app, tray=None):
        super().__init__(application=app, title='Utilisation Claude')
        self.set_default_size(520, 720)
        self.tray = tray
        self.usage = None
        self.error = None
        self.idle = ('…', 'Chargement…')
        self.busy = False
        self.expect_login = False
        self.login_proc = None
        self.login_cancelled = False
        self.last_fetch = 0.0

        self.window_title = Adw.WindowTitle(title='Utilisation Claude', subtitle='')
        header = Adw.HeaderBar(title_widget=self.window_title)
        self.refresh_button = Gtk.Button(
            icon_name='view-refresh-symbolic',
            tooltip_text='Actualiser',
        )
        self.refresh_button.connect('clicked', lambda *_: self.refresh())
        header.pack_start(self.refresh_button)

        menu = Gio.Menu()
        menu.append('Démarrer avec Ubuntu', 'app.autostart')
        menu.append('Se reconnecter', 'win.reconnect')
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu))
        reconnect = Gio.SimpleAction.new('reconnect', None)
        reconnect.connect('activate', lambda *_: self.show_login())
        self.add_action(reconnect)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.build_loading(), 'loading')
        self.stack.add_named(self.build_usage(), 'usage')
        self.stack.add_named(self.build_login(), 'login')
        self.stack.add_named(self.build_error(), 'error')

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(header)
        root.append(self.stack)
        self.set_content(root)

        self.connect('close-request', self.on_close)
        self.connect('notify::is-active', self.on_active_changed)
        GLib.timeout_add_seconds(NETWORK_REFRESH_SECONDS, self.on_network_tick)
        GLib.timeout_add_seconds(RENDER_REFRESH_SECONDS, self.on_render_tick)
        self.publish_tray()
        self.refresh()

    # -- construction des pages ---------------------------------------------

    def build_loading(self):
        return Gtk.Spinner(
            spinning=True,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            width_request=32,
            height_request=32,
            vexpand=True,
        )

    def build_usage(self):
        page = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
            margin_top=20,
            margin_bottom=16,
            margin_start=24,
            margin_end=24,
        )

        title_row = Gtk.Box(spacing=8)
        title = Gtk.Label(label='Votre utilisation', xalign=0)
        title.add_css_class('title-3')
        self.plan_label = Gtk.Label(valign=Gtk.Align.CENTER)
        self.plan_label.add_css_class('dim-label')
        title_row.append(title)
        title_row.append(self.plan_label)

        self.headline = Gtk.Label(xalign=0, wrap=True)
        self.headline.add_css_class('title-4')
        self.session_row = UsageRow('Session actuelle')
        self.week_row = UsageRow('Cette semaine')

        credits = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        credits_title = Gtk.Label(label='Limite de dépenses mensuelle', xalign=0)
        credits_title.add_css_class('heading')
        self.credits_value = Gtk.Label(xalign=0, wrap=True)
        credits_note = Gtk.Label(label=SPEND_NOTE, xalign=0, wrap=True)
        credits_note.add_css_class('dim-label')
        credits_note.add_css_class('caption')
        for widget in (credits_title, self.credits_value, credits_note):
            credits.append(widget)
        self.credits_box = credits

        self.products_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        products_title = Gtk.Label(
            label='Utilisation de cette semaine par produit',
            xalign=0,
        )
        products_title.add_css_class('heading')
        self.products_box.append(products_title)
        self.product_rows = []

        footer = Gtk.Box(spacing=12, valign=Gtk.Align.END, vexpand=True)
        self.status = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.status.add_css_class('dim-label')
        self.status.add_css_class('caption')
        footer.append(self.status)
        footer.append(Gtk.LinkButton(
            uri='https://claude.ai/settings/usage',
            label='Ouvrir sur claude.ai',
        ))

        for widget in (
            title_row,
            self.headline,
            Gtk.Separator(),
            self.session_row,
            self.week_row,
            credits,
            self.products_box,
            footer,
        ):
            page.append(widget)

        return Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vexpand=True,
            child=page,
        )

    def build_login(self):
        self.login_page = Adw.StatusPage(
            icon_name='dialog-password-symbolic',
            title='Connexion à Claude',
            description=(
                'Connectez-vous pour suivre votre utilisation ici. '
                "Votre navigateur s'ouvrira pour valider la connexion, "
                'comme avec /login dans Claude Code.'
            ),
        )
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
        )
        self.login_button = Gtk.Button(label='Se connecter avec Claude')
        self.login_button.add_css_class('suggested-action')
        self.login_button.add_css_class('pill')
        self.login_button.connect('clicked', self.on_login_clicked)

        self.login_cancel = Gtk.Button(label='Annuler', visible=False)
        self.login_cancel.add_css_class('pill')
        self.login_cancel.connect('clicked', self.on_login_cancel)

        self.login_note = Gtk.Label(
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=44,
            visible=False,
        )
        self.login_note.add_css_class('dim-label')
        self.login_link = Gtk.LinkButton(
            uri='https://claude.ai',
            label="Le navigateur ne s'ouvre pas ? Ouvrir le lien",
            visible=False,
        )

        for widget in (
            self.login_button,
            self.login_cancel,
            self.login_note,
            self.login_link,
        ):
            box.append(widget)
        self.login_page.set_child(box)

        return self.login_page

    def build_error(self):
        page = Adw.StatusPage(
            icon_name='network-offline-symbolic',
            title="Impossible de récupérer l'utilisation",
        )
        self.error_page = page
        retry = Gtk.Button(label='Réessayer', halign=Gtk.Align.CENTER)
        retry.add_css_class('suggested-action')
        retry.add_css_class('pill')
        retry.connect('clicked', lambda *_: self.refresh())
        page.set_child(retry)

        return page

    # -- récupération des données -------------------------------------------

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.refresh_button.set_sensitive(False)
        if self.usage is None and self.stack.get_visible_child_name() != 'login':
            self.stack.set_visible_child_name('loading')
        threading.Thread(target=self.fetch_worker, daemon=True).start()

    def fetch_worker(self):
        try:
            result = ('ok', fetch_usage())
        except AuthRequired as error:
            result = ('auth', str(error))
        except UsageError as error:
            result = ('error', str(error))
        except Exception:  # ne jamais laisser le thread mourir en silence
            result = ('error', 'Erreur inattendue.')
        GLib.idle_add(self.on_fetch_result, *result)

    def on_fetch_result(self, kind, payload):
        self.busy = False
        self.refresh_button.set_sensitive(True)
        self.last_fetch = time.monotonic()

        if kind == 'ok':
            self.usage = payload
            self.error = None
            self.expect_login = False
            self.render()
            self.stack.set_visible_child_name('usage')
        elif kind == 'auth':
            self.usage = None
            self.idle = ('Connexion', 'Connexion à Claude requise')
            if self.login_proc is None:
                if self.expect_login:
                    payload = "Connexion terminée, mais aucun identifiant n'a été trouvé."
                    self.expect_login = False
                self.show_login(payload)
        else:
            self.error = payload
            if self.usage is None:
                self.idle = ('Hors ligne', payload)
                self.error_page.set_description(payload)
                self.stack.set_visible_child_name('error')
            else:
                self.render()

        self.publish_tray()

        return GLib.SOURCE_REMOVE

    def publish_tray(self):
        if self.tray is None:
            return
        if self.usage is not None:
            summary = tray_summary(self.usage, datetime.now().astimezone())
        else:
            summary = tray_idle(*self.idle)
        self.tray.update(**summary)

    def show_window(self, token=None):
        """Affiche la fenêtre ; le jeton d'activation évite le vol de focus."""
        surface = self.get_surface()
        if token and surface is not None:
            try:
                surface.set_startup_id(token)
            except (AttributeError, TypeError):
                pass
        self.set_visible(True)
        self.present()

    def render(self):
        if self.usage is None:
            return
        now = datetime.now().astimezone()
        usage = self.usage
        self.plan_label.set_text(usage['plan'].capitalize())
        text, level = weekly_headline(usage['week'], now) or ('', None)
        self.headline.set_text(text)
        self.headline.set_visible(bool(text))
        set_level(self.headline, level)
        self.session_row.update(usage['session'], now)
        self.week_row.update(usage['week'], now)

        credits = usage['credits']
        self.credits_box.set_visible(credits is not None)
        if credits:
            self.credits_value.set_text(credits_text(credits))

        rows = usage['breakdown'] or []
        self.products_box.set_visible(bool(rows))
        while len(self.product_rows) < len(rows):
            self.product_rows.append(ProductRow())
            self.products_box.append(self.product_rows[-1])
        for index, widget in enumerate(self.product_rows):
            widget.set_visible(index < len(rows))
            if index < len(rows):
                widget.update(rows[index])

        status = 'Dernière mise à jour : %s' % ago_text(usage['fetched_at'], now)
        if self.error:
            status += ' · %s' % self.error
        self.status.set_text(status)
        self.publish_tray()

    # -- connexion (équivalent de /login) -----------------------------------

    def show_login(self, message=None):
        self.set_login_running(False, message)
        self.stack.set_visible_child_name('login')

    def set_login_running(self, running, message=None):
        self.login_button.set_visible(not running)
        self.login_cancel.set_visible(running)
        self.login_note.set_visible(bool(message))
        self.login_note.set_text(message or '')
        if not running:
            self.login_link.set_visible(False)

    def on_login_clicked(self, *_):
        claude = find_claude()
        if claude is None:
            self.set_login_running(
                False,
                'Commande « claude » introuvable. Installez Claude Code '
                'puis réessayez.',
            )

            return

        try:
            proc = Gio.Subprocess.new(
                [claude, 'auth', 'login', '--claudeai'],
                Gio.SubprocessFlags.STDIN_PIPE
                | Gio.SubprocessFlags.STDOUT_PIPE
                | Gio.SubprocessFlags.STDERR_MERGE,
            )
        except GLib.Error:
            self.set_login_running(False, 'Impossible de lancer « claude auth login ».')

            return

        self.login_proc = proc
        self.login_cancelled = False
        self.set_login_running(
            True,
            "Votre navigateur s'ouvre. Terminez la connexion sur claude.ai, "
            'puis revenez ici.',
        )
        stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
        self.read_login_line(stream, proc)
        proc.wait_async(None, self.on_login_exit)

    def read_login_line(self, stream, proc):
        stream.read_line_async(
            GLib.PRIORITY_DEFAULT,
            None,
            self.on_login_line,
            proc,
        )

    def on_login_line(self, stream, result, proc):
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return
        if line is None:
            return

        if proc is self.login_proc:
            for url in URL_PATTERN.findall(line):
                host = urllib.parse.urlparse(url).hostname or ''
                if any(host == h or host.endswith('.' + h) for h in LOGIN_HOSTS):
                    self.login_link.set_uri(url)
                    self.login_link.set_visible(True)
                    break
        self.read_login_line(stream, proc)

    def on_login_cancel(self, *_):
        self.cancel_login()

    def cancel_login(self):
        if self.login_proc is not None:
            self.login_cancelled = True
            self.login_proc.force_exit()

    def on_login_exit(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is not self.login_proc:
            return

        self.login_proc = None
        if self.login_cancelled:
            self.show_login('Connexion annulée.')
        elif proc.get_successful():
            self.expect_login = True
            self.refresh()
        else:
            self.show_login('La connexion a échoué ou a été interrompue. Réessayez.')

    # -- minuteries et cycle de vie -----------------------------------------

    def on_network_tick(self):
        self.refresh()

        return GLib.SOURCE_CONTINUE

    def on_render_tick(self):
        self.render()

        return GLib.SOURCE_CONTINUE

    def on_active_changed(self, *_):
        stale = time.monotonic() - self.last_fetch > FOCUS_REFRESH_MIN_SECONDS
        if self.is_active() and stale:
            self.refresh()

    def on_close(self, *_):
        if self.tray is not None and self.tray.available:
            self.set_visible(False)

            return True

        self.get_application().quit_app()

        return False


class UsageApp(Adw.Application):
    def __init__(self, hidden=False):
        super().__init__(application_id=APP_ID)
        self.hidden = hidden
        self.window = None
        self.tray = None
        self.quitting = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        style = Adw.StyleManager.get_default()
        style.connect('notify::dark', lambda *_: self.apply_palette())
        self.apply_palette()

        # Case « Démarrer avec Ubuntu » : action partagée fenêtre + icône
        self.autostart_action = Gio.SimpleAction.new_stateful(
            'autostart',
            None,
            GLib.Variant.new_boolean(autostart_enabled()),
        )
        self.autostart_action.connect('change-state', self.on_autostart_change)
        self.add_action(self.autostart_action)

        self.hold()  # l'app continue de tourner fenêtre fermée (icône seule)
        self.tray = StatusTray(
            'Utilisation Claude',
            on_activate=self.on_tray_activate,
            on_refresh=self.on_tray_refresh,
            on_quit=self.quit_app,
            on_autostart=self.toggle_autostart,
        )
        self.tray.set_autostart(self.autostart_action.get_state().get_boolean())
        self.tray.on_availability = self.on_tray_availability
        try:
            self.tray.start()
        except GLib.Error:
            self.tray = None  # pas de bus de session : fenêtre seule

    def apply_palette(self):
        dark = Adw.StyleManager.get_default().get_dark()
        self.css_provider.load_from_string(
            build_css(PALETTES['dark' if dark else 'light'])
        )

    def do_activate(self):
        first = self.window is None
        if first:
            self.window = UsageWindow(self, self.tray)
        if first and self.hidden:
            GLib.timeout_add_seconds(5, self.show_if_no_tray)

            return
        self.window.show_window()

    def show_if_no_tray(self):
        """Démarrage réduit : sans icône disponible, mieux vaut montrer la fenêtre."""
        if self.tray is None or not self.tray.available:
            self.window.show_window()

        return GLib.SOURCE_REMOVE

    def on_tray_activate(self, token):
        if self.window is not None:
            self.window.show_window(token)

    def on_tray_refresh(self):
        if self.window is not None:
            self.window.refresh()

    def toggle_autostart(self):
        current = self.autostart_action.get_state().get_boolean()
        self.autostart_action.change_state(GLib.Variant.new_boolean(not current))

    def on_autostart_change(self, action, value):
        enabled = value.get_boolean()
        try:
            set_autostart(enabled)
        except OSError as error:
            # état inchangé : la coche revient à sa position précédente
            print('Démarrage automatique impossible : %s' % error, file=sys.stderr)

            return
        action.set_state(value)
        if self.tray is not None:
            self.tray.set_autostart(enabled)

    def on_tray_availability(self, available):
        """Icône disparue (extension coupée) : ne pas laisser l'app invisible."""
        if available or self.quitting or self.window is None:
            return
        if not self.window.get_visible():
            self.window.show_window()

    def quit_app(self):
        self.quitting = True
        if self.window is not None:
            self.window.cancel_login()
        if self.tray is not None:
            self.tray.stop()
        self.quit()


def main():
    args = sys.argv[1:]
    if '--text' in args:
        return print_text()

    return UsageApp(hidden='--hidden' in args).run(sys.argv[:1])


if __name__ == '__main__':
    sys.exit(main())
