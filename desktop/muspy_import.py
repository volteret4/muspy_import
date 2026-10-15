#!/usr/bin/env python3
"""muspy_import: busca los artistas de una carpeta de música en MusicBrainz y
genera un CSV que la web https://volteret4.github.io/muspy_import/ usa para
seguirlos en muspy."""

import csv
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import mutagen
except ImportError:
    mutagen = None

APP_NAME = 'muspy_import'
VERSION = '1.0.0'
WEB_URL = 'https://volteret4.github.io/muspy_import/'
USER_AGENT = f'{APP_NAME}/{VERSION} ( https://github.com/volteret4/muspy_import )'
VARIOUS_ARTISTS = '89ad4ac3-39f7-470e-963a-56509c546377'
MB_DELAY = 1.1
AUDIO_EXT = {'.mp3', '.flac', '.ogg', '.oga', '.opus', '.m4a', '.mp4', '.aac', '.wma',
             '.wv', '.ape', '.mpc', '.aiff', '.aif', '.dsf', '.wav'}
MBID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
CONFIDENCE_ORDER = {'alta': 0, 'media': 1, 'baja': 2, 'ninguna': 3}


# ---------- Utilidades de texto ----------

def norm(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace('&', ' and ')
    s = re.sub(r'^(the|el|la|los|las)\s+', '', s)
    s = re.sub(r',\s*the$', '', s)
    return ''.join(c for c in s if c.isalnum())


def similar(a, b):
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    if x == y:
        return True
    s, l = (x, y) if len(x) < len(y) else (y, x)
    return s in l and len(s) / len(l) >= 0.7


def is_various(name):
    return norm(name) in ('various', 'variousartists', 'variosartistas', 'unknownartist', '')


# ---------- Caché ----------

def cache_path():
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home()))
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / APP_NAME / 'musicbrainz.json'


def load_cache():
    try:
        return json.loads(cache_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    except OSError:
        pass


# ---------- MusicBrainz ----------

class MusicBrainz:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0

    def get(self, path, params=None):
        params = dict(params or {}, fmt='json')
        url = f'https://musicbrainz.org/ws/2/{path}?{urllib.parse.urlencode(params)}'
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
        for attempt in range(5):
            with self.lock:
                wait = self.last + MB_DELAY - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self.last = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                if e.code == 404:
                    return None
                raise RuntimeError(f'MusicBrainz respondió {e.code}') from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                raise ConnectionError('No se puede conectar con musicbrainz.org') from e
        raise RuntimeError('MusicBrainz no responde')


def lucene(s):
    return '"' + re.sub(r'([\\"])', r'\\\1', s) + '"'


def describe(x):
    return ' · '.join(v for v in (x.get('disambiguation'), x.get('type'), x.get('country')) if v)


def option(artists, desc=None):
    return {
        'ids': [a['id'] for a in artists],
        'names': [a['name'] for a in artists],
        'label': ' + '.join(a['name'] for a in artists),
        'desc': desc if desc is not None else (describe(artists[0]) if len(artists) == 1 else ''),
    }


def search_artist(mb, query):
    data = mb.get('artist/', {'limit': 8, 'query': f'artist:{lucene(query)}'})
    found = [x for x in (data or {}).get('artists', []) if x.get('score', 0) >= 50]
    return found


def resolve(mb, artist):
    """Devuelve {confidence, options, choice, checked} como la web."""
    short = re.split(r'\s+[-–]\s+|\s+/\s+', artist.name)[0].strip()
    names = [artist.name, short] if short != artist.name else [artist.name]

    # 1) Por uno de sus discos: el artista acreditado es casi seguro el correcto.
    for title in artist.albums[:2]:
        data = mb.get('release-group/', {'limit': 5, 'query': f'releasegroup:{lucene(title)} AND artist:{lucene(artist.name)}'})
        for rg in (data or {}).get('release-groups', []):
            if rg.get('score', 0) < 80:
                break
            credit = rg.get('artist-credit', [])
            credited = [c for c in credit if c['artist']['id'] != VARIOUS_ARTISTS]
            if not credited:
                continue
            joined = ''.join(c['name'] + c.get('joinphrase', '') for c in credit)
            if norm(joined) == norm(artist.name):
                return {'confidence': 'alta', 'options': [option([c['artist'] for c in credited])], 'choice': 0, 'checked': True}
            exact = next((c for c in credited if any(norm(n) in (norm(c['name']), norm(c['artist']['name'])) for n in names)), None)
            if exact:
                return {'confidence': 'alta', 'options': [option([exact['artist']])], 'choice': 0, 'checked': True}
            close = next((c for c in credited if any(similar(n, c['artist']['name']) for n in names)), None)
            if close and rg.get('score', 0) >= 95:
                return {'confidence': 'media', 'options': [option([close['artist']])], 'choice': 0, 'checked': True}

    # 2) Por nombre.
    query = names[-1]
    found = search_artist(mb, query)
    options = [option([x]) for x in found]
    exact = [x for x in found if norm(x['name']) == norm(query) or any(norm(a['name']) == norm(query) for a in x.get('aliases', []))]
    if len(exact) == 1 and exact[0].get('score', 0) >= 90:
        return {'confidence': 'media', 'options': options, 'choice': found.index(exact[0]), 'checked': True}
    if exact:
        return {'confidence': 'baja', 'options': options, 'choice': found.index(exact[0]), 'checked': False}
    if options:
        return {'confidence': 'baja', 'options': options, 'choice': -1, 'checked': False}
    return {'confidence': 'ninguna', 'options': [], 'choice': -1, 'checked': False}


# ---------- Lectura de etiquetas ----------

@dataclass
class Artist:
    key: str
    name: str
    albums: list = field(default_factory=list)
    tag_ids: dict = field(default_factory=dict)  # mbid -> nombre, de etiquetas de Picard
    files: int = 0
    result: dict | None = None


def tag_values(tags, *keys):
    for k in keys:
        v = tags.get(k)
        if v:
            vals = v if isinstance(v, list) else [v]
            out = [str(x).strip() for x in vals if str(x).strip()]
            if out:
                return out
    return []


def read_file(path):
    """Devuelve [(nombres, mbids, álbum)] de un archivo de audio."""
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return None
    if f is None or f.tags is None:
        return None
    tags = {k.lower(): v for k, v in f.tags.items()}
    album = (tag_values(tags, 'album') or [''])[0]
    album_artist = tag_values(tags, 'albumartist', 'album artist', 'wm/albumartist')
    if album_artist and not is_various(album_artist[0]):
        return album_artist, tag_values(tags, 'musicbrainz_albumartistid', 'musicbrainz album artist id'), album
    return tag_values(tags, 'artist', 'author'), tag_values(tags, 'musicbrainz_artistid', 'musicbrainz artist id'), album


def scan_folder(folder, artists, progress, stop):
    files = [p for p in Path(folder).rglob('*') if p.suffix.lower() in AUDIO_EXT and p.is_file()]
    for i, path in enumerate(files):
        if stop.is_set():
            break
        if i % 25 == 0:
            progress(i, len(files))
        info = read_file(path)
        if not info:
            continue
        names, ids, album = info
        # Las etiquetas multivalor separan los MBID con "/" o ";" en algunos formatos.
        ids = [m.lower() for v in ids for m in MBID_RE.findall(v)]
        name = names[0] if len(names) == 1 else ' & '.join(names)
        if is_various(name):
            continue
        key = norm(name) or name.lower()
        a = artists.get(key)
        if not a:
            a = artists[key] = Artist(key, name)
        a.files += 1
        if album and album not in a.albums:
            a.albums.append(album)
        if ids:
            if len(ids) == len(names):
                for mbid, n in zip(ids, names):
                    a.tag_ids[mbid] = n
            else:
                for mbid in ids:
                    a.tag_ids.setdefault(mbid, name if len(ids) == 1 else mbid)
    progress(len(files), len(files))
    return len(files)


def result_from_tags(a):
    ids = list(a.tag_ids)
    names = [a.tag_ids[i] for i in ids]
    opt = {'ids': ids, 'names': names, 'label': ' + '.join(names), 'desc': 'MBID de las etiquetas'}
    return {'confidence': 'alta', 'options': [opt], 'choice': 0, 'checked': True}


# ---------- Interfaz ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('muspy_import — artistas de tu música')
        self.geometry('1000x680')
        self.minsize(720, 480)
        self.artists = {}
        self.cache = load_cache()
        self.mb = MusicBrainz()
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.filter = tk.StringVar(value='all')
        self.search = tk.StringVar()
        self.status = tk.StringVar(value='Elige la carpeta donde guardas tu música.')
        self.build()
        self.after(100, self.poll)
        if mutagen is None:
            messagebox.showerror(APP_NAME, 'Falta la librería «mutagen». Instálala con:\n\npip install mutagen')

    # --- construcción ---

    def build(self):
        style = ttk.Style(self)
        if 'clam' in style.theme_names() and sys.platform != 'win32':
            style.theme_use('clam')
        style.configure('Treeview', rowheight=24)

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill='x')
        self.btn_folder = ttk.Button(top, text='1. Elegir carpeta…', command=self.choose_folder)
        self.btn_folder.pack(side='left')
        self.btn_resolve = ttk.Button(top, text='2. Buscar en MusicBrainz', command=self.resolve_all, state='disabled')
        self.btn_resolve.pack(side='left', padx=6)
        self.btn_stop = ttk.Button(top, text='Detener', command=self.stop.set, state='disabled')
        self.btn_stop.pack(side='left')
        self.btn_export = ttk.Button(top, text='3. Exportar CSV…', command=self.export, state='disabled')
        self.btn_export.pack(side='right')
        ttk.Button(top, text='Abrir la web', command=lambda: webbrowser.open(WEB_URL)).pack(side='right', padx=6)

        prog = ttk.Frame(self, padding=(10, 0))
        prog.pack(fill='x')
        self.progress = ttk.Progressbar(prog, mode='determinate')
        self.progress.pack(fill='x', pady=(4, 2))
        ttk.Label(prog, textvariable=self.status, foreground='#555').pack(anchor='w')

        bar = ttk.Frame(self, padding=(10, 8, 10, 4))
        bar.pack(fill='x')
        self.filter_buttons = {}
        for value, text in (('all', 'Todos'), ('review', 'Revisar'), ('none', 'Sin encontrar')):
            b = ttk.Radiobutton(bar, text=text, value=value, variable=self.filter, command=self.refresh)
            b.pack(side='left', padx=(0, 10))
            self.filter_buttons[value] = (b, text)
        entry = ttk.Entry(bar, textvariable=self.search, width=28)
        entry.pack(side='right')
        ttk.Label(bar, text='Filtrar:').pack(side='right', padx=4)
        self.search.trace_add('write', lambda *_: self.refresh())

        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill='both', expand=True)
        cols = ('sel', 'artist', 'albums', 'mb', 'conf')
        self.tree = ttk.Treeview(body, columns=cols, show='headings', selectmode='extended')
        for col, text, width, stretch, anchor in (
                ('sel', '✔', 34, False, 'center'), ('artist', 'Artista en tus archivos', 260, True, 'w'),
                ('albums', 'Discos', 60, False, 'e'), ('mb', 'MusicBrainz', 340, True, 'w'),
                ('conf', 'Confianza', 90, False, 'center')):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch, anchor=anchor)
        sb = ttk.Scrollbar(body, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        for conf, color in (('alta', '#15803d'), ('media', '#b45309'), ('baja', '#b91c1c'), ('ninguna', '#b91c1c'), ('pendiente', '#888')):
            self.tree.tag_configure(conf, foreground=color)
        self.tree.tag_configure('off', foreground='#999')
        self.tree.bind('<Button-1>', self.on_click)
        self.tree.bind('<Double-1>', lambda e: self.edit_selected())
        self.tree.bind('<Return>', lambda e: self.edit_selected())
        self.tree.bind('<space>', lambda e: self.toggle_selected())

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill='x')
        ttk.Button(bottom, text='Editar…', command=self.edit_selected).pack(side='left')
        ttk.Button(bottom, text='Marcar visibles', command=lambda: self.check_visible(True)).pack(side='left', padx=6)
        ttk.Button(bottom, text='Desmarcar visibles', command=lambda: self.check_visible(False)).pack(side='left')
        ttk.Button(bottom, text='Borrar caché', command=self.clear_cache).pack(side='right')
        self.count_label = ttk.Label(bottom, text='')
        self.count_label.pack(side='right', padx=10)
        ttk.Label(self, text='Clic en ✔ o barra espaciadora: marcar/desmarcar · Doble clic o Intro: elegir otro artista o pegar un MBID',
                  foreground='#777', padding=(10, 0, 10, 8)).pack(anchor='w')

    # --- hilo de fondo ---

    def run_bg(self, fn, done=None):
        def target():
            try:
                res = fn()
                self.events.put(('done', done, res, None))
            except Exception as e:  # noqa: BLE001 — se muestra al usuario
                self.events.put(('done', done, None, e))
        threading.Thread(target=target, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, *args = self.events.get_nowait()
                if kind == 'done':
                    cb, res, err = args
                    if cb:
                        cb(res, err)
                elif kind == 'progress':
                    value, total, text = args
                    self.progress['maximum'] = max(total, 1)
                    self.progress['value'] = value
                    self.status.set(text)
                elif kind == 'row':
                    self.update_row(args[0])
                    self.update_counts()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def set_busy(self, busy):
        self.busy = busy
        self.btn_folder['state'] = 'disabled' if busy else 'normal'
        self.btn_resolve['state'] = 'disabled' if busy or not self.artists else 'normal'
        self.btn_stop['state'] = 'normal' if busy else 'disabled'

    # --- pasos ---

    def choose_folder(self):
        if mutagen is None:
            messagebox.showerror(APP_NAME, 'Falta la librería «mutagen».')
            return
        folder = filedialog.askdirectory(title='Carpeta de música')
        if not folder:
            return
        self.stop.clear()
        self.set_busy(True)
        found = {}

        def progress(i, total):
            self.events.put(('progress', i, total, f'Leyendo etiquetas: {i} de {total} archivos…'))

        def work():
            return scan_folder(folder, found, progress, self.stop)

        def done(total, err):
            self.set_busy(False)
            if err:
                messagebox.showerror(APP_NAME, f'Error leyendo la carpeta:\n{err}')
                return
            for key, a in found.items():
                old = self.artists.get(key)
                if old:
                    old.files += a.files
                    old.albums += [x for x in a.albums if x not in old.albums]
                    old.tag_ids.update(a.tag_ids)
                else:
                    self.artists[key] = a
            tagged = 0
            for a in self.artists.values():
                if a.tag_ids:
                    a.result = result_from_tags(a)
                    tagged += 1
                elif a.result is None and a.key in self.cache:
                    a.result = self.cache[a.key]
            pending = sum(1 for a in self.artists.values() if a.result is None)
            self.status.set(f'{total} archivos · {len(self.artists)} artistas · {tagged} con MBID en las etiquetas · '
                            f'{pending} por buscar en MusicBrainz')
            self.refresh()
            self.set_busy(False)

        self.run_bg(work, done)

    def resolve_all(self):
        todo = [a for a in sorted(self.artists.values(), key=lambda a: a.name.lower()) if a.result is None]
        if not todo:
            messagebox.showinfo(APP_NAME, 'Todos los artistas están ya buscados.')
            return
        self.stop.clear()
        self.set_busy(True)

        def work():
            started = time.monotonic()
            failures = 0
            i = 0
            while i < len(todo) and not self.stop.is_set():
                a = todo[i]
                left = ''
                if i:
                    mins = (time.monotonic() - started) / i * (len(todo) - i) / 60
                    left = f' · quedan ~{mins:.0f} min'
                self.events.put(('progress', i, len(todo), f'Buscando {i + 1} de {len(todo)}: {a.name}{left}'))
                try:
                    a.result = resolve(self.mb, a)
                except ConnectionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    if failures >= 3:
                        raise
                    self.events.put(('progress', i, len(todo), f'{e} — reintentando en 10 s'))
                    time.sleep(10)
                    continue
                failures = 0
                self.cache[a.key] = a.result
                if i % 10 == 0:
                    save_cache(self.cache)
                self.events.put(('row', a))
                i += 1
            save_cache(self.cache)
            return i, len(todo)

        def done(res, err):
            self.set_busy(False)
            save_cache(self.cache)
            if err:
                self.status.set(f'Detenido: {err}')
                messagebox.showerror(APP_NAME, f'{err}\n\nComprueba tu conexión o prueba con una VPN. Lo ya buscado queda guardado.')
            else:
                done_n, total = res
                self.status.set(f'Terminado: {done_n} de {total} buscados.' if done_n == total else f'Detenido: {done_n} de {total} buscados.')
            self.refresh()

        self.run_bg(work, done)

    def export(self):
        rows = []
        for a in sorted(self.artists.values(), key=lambda a: a.name.lower()):
            r = a.result
            if r and r['checked'] and r['choice'] >= 0:
                opt = r['options'][r['choice']]
                rows.append({'artist': opt['label'], 'mbid': ';'.join(opt['ids']), 'albums': ' | '.join(a.albums[:5]), 'source_name': a.name})
        if not rows:
            messagebox.showinfo(APP_NAME, 'No hay artistas marcados con un MBID.')
            return
        path = filedialog.asksaveasfilename(title='Guardar CSV', defaultextension='.csv', initialfile='muspy-artists.csv',
                                            filetypes=[('CSV', '*.csv')])
        if not path:
            return
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['artist', 'mbid', 'albums', 'source_name'])
            w.writeheader()
            w.writerows(rows)
        if messagebox.askyesno(APP_NAME, f'Guardados {len(rows)} artistas en\n{path}\n\n¿Abrir la web para subirlo?'):
            webbrowser.open(WEB_URL)

    def clear_cache(self):
        if not messagebox.askyesno(APP_NAME, '¿Borrar las búsquedas guardadas? Los artistas sin MBID en las etiquetas se tendrán que buscar de nuevo.'):
            return
        self.cache = {}
        save_cache(self.cache)
        for a in self.artists.values():
            if not a.tag_ids:
                a.result = None
        self.refresh()

    # --- tabla ---

    def needs_review(self, a):
        r = a.result
        return bool(r) and (r['confidence'] in ('baja', 'ninguna') or (r['confidence'] != 'alta' and len(r['options']) > 1))

    def visible(self, a):
        q = norm(self.search.get())
        if q and q not in norm(a.name):
            return False
        f = self.filter.get()
        if f == 'review':
            return self.needs_review(a)
        if f == 'none':
            return bool(a.result) and a.result['confidence'] == 'ninguna'
        return True

    def row_values(self, a):
        r = a.result
        if not r:
            return ('', a.name, len(a.albums), 'pendiente', '—'), ('pendiente',)
        chosen = r['options'][r['choice']] if r['choice'] >= 0 else None
        on = r['checked'] and chosen is not None
        mb = '— ninguno —' if not chosen else chosen['label'] + (f'  ({chosen["desc"]})' if chosen['desc'] else '')
        if len(r['options']) > 1:
            mb += f'   [{len(r["options"])} opciones]'
        tags = (r['confidence'],) if on else ('off',)
        return ('✔' if on else '', a.name, len(a.albums), mb, r['confidence']), tags

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for a in sorted(self.artists.values(), key=lambda a: a.name.lower()):
            if self.visible(a):
                values, tags = self.row_values(a)
                self.tree.insert('', 'end', iid=a.key, values=values, tags=tags)
        self.update_counts()

    def update_row(self, a):
        if self.tree.exists(a.key):
            values, tags = self.row_values(a)
            self.tree.item(a.key, values=values, tags=tags)
        elif self.visible(a):
            self.refresh()

    def update_counts(self):
        arts = list(self.artists.values())
        counts = {'all': len(arts), 'review': sum(map(self.needs_review, arts)),
                  'none': sum(1 for a in arts if a.result and a.result['confidence'] == 'ninguna')}
        for value, (b, text) in self.filter_buttons.items():
            b['text'] = f'{text} ({counts[value]})'
        selected = {i for a in arts if a.result and a.result['checked'] and a.result['choice'] >= 0
                    for i in a.result['options'][a.result['choice']]['ids']}
        self.count_label['text'] = f'{len(selected)} artistas marcados'
        self.btn_export['state'] = 'normal' if selected else 'disabled'
        if not self.busy:
            self.btn_resolve['state'] = 'normal' if any(a.result is None for a in arts) else 'disabled'

    def set_checked(self, a, value):
        if a.result and a.result['choice'] >= 0:
            a.result['checked'] = value
            if not a.tag_ids:
                self.cache[a.key] = a.result
            self.update_row(a)

    def on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == 'cell' and self.tree.identify_column(event.x) == '#1':
            key = self.tree.identify_row(event.y)
            a = self.artists.get(key)
            if a and a.result:
                self.set_checked(a, not a.result['checked'])
                self.update_counts()
                save_cache(self.cache)
            return 'break'

    def toggle_selected(self):
        for key in self.tree.selection():
            a = self.artists[key]
            if a.result:
                self.set_checked(a, not a.result['checked'])
        self.update_counts()
        save_cache(self.cache)
        return 'break'

    def check_visible(self, value):
        for key in self.tree.get_children():
            self.set_checked(self.artists[key], value)
        self.update_counts()
        save_cache(self.cache)

    def edit_selected(self):
        sel = self.tree.selection() or (self.tree.focus(),)
        a = self.artists.get(sel[0]) if sel and sel[0] else None
        if not a:
            return
        if a.result is None:
            a.result = {'confidence': 'ninguna', 'options': [], 'choice': -1, 'checked': False}
        EditDialog(self, a)


class EditDialog(tk.Toplevel):
    """Elegir entre los candidatos, buscar otro nombre o pegar un MBID."""

    def __init__(self, app, artist):
        super().__init__(app)
        self.app, self.a = app, artist
        self.options = [dict(o) for o in artist.result['options']]
        self.title(f'Editar: {artist.name}')
        self.geometry('640x460')
        self.transient(app)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text=artist.name, font=('TkDefaultFont', 12, 'bold')).pack(anchor='w')
        if artist.albums:
            ttk.Label(frm, text='Discos: ' + ' · '.join(artist.albums[:4]), foreground='#666', wraplength=600).pack(anchor='w', pady=(2, 8))

        self.listbox = tk.Listbox(frm, height=10, activestyle='dotbox', exportselection=False)
        self.listbox.pack(fill='both', expand=True)
        self.listbox.bind('<Double-1>', lambda e: self.accept())

        row = ttk.Frame(frm)
        row.pack(fill='x', pady=8)
        self.query = tk.StringVar(value=artist.name)
        ttk.Entry(row, textvariable=self.query).pack(side='left', fill='x', expand=True)
        self.btn_search = ttk.Button(row, text='Buscar este nombre', command=self.search)
        self.btn_search.pack(side='left', padx=6)

        row2 = ttk.Frame(frm)
        row2.pack(fill='x')
        self.mbid = tk.StringVar()
        ttk.Entry(row2, textvariable=self.mbid).pack(side='left', fill='x', expand=True)
        self.btn_mbid = ttk.Button(row2, text='Usar MBID o enlace', command=self.use_mbid)
        self.btn_mbid.pack(side='left', padx=6)

        self.msg = ttk.Label(frm, text='', foreground='#b45309')
        self.msg.pack(anchor='w', pady=4)

        btns = ttk.Frame(frm)
        btns.pack(fill='x', pady=(6, 0))
        ttk.Button(btns, text='Ver en MusicBrainz', command=self.open_mb).pack(side='left')
        ttk.Button(btns, text='Cancelar', command=self.destroy).pack(side='right')
        ttk.Button(btns, text='Aceptar', command=self.accept).pack(side='right', padx=6)

        self.fill(artist.result['choice'])
        self.bind('<Escape>', lambda e: self.destroy())
        self.grab_set()

    def fill(self, choice):
        self.listbox.delete(0, 'end')
        self.listbox.insert('end', '— ninguno (no seguir) —')
        for o in self.options:
            self.listbox.insert('end', o['label'] + (f'  ({o["desc"]})' if o['desc'] else ''))
        self.listbox.selection_clear(0, 'end')
        self.listbox.selection_set(choice + 1)
        self.listbox.see(choice + 1)

    def current(self):
        sel = self.listbox.curselection()
        return (sel[0] - 1) if sel else -1

    def search(self):
        q = self.query.get().strip()
        if not q:
            return
        self.btn_search['state'] = 'disabled'
        self.msg['text'] = 'Buscando…'

        def done(found, err):
            if not self.winfo_exists():
                return
            self.btn_search['state'] = 'normal'
            if err:
                self.msg['text'] = str(err)
                return
            known = {tuple(o['ids']) for o in self.options}
            new = [option([x]) for x in found if (x['id'],) not in known]
            self.options += new
            self.msg['text'] = f'{len(new)} resultados nuevos.' if new else 'Sin resultados nuevos.'
            self.fill(len(self.options) - len(new) if new else self.current())

        self.app.run_bg(lambda: search_artist(self.app.mb, q), done)

    def use_mbid(self):
        m = MBID_RE.search(self.mbid.get())
        if not m:
            self.msg['text'] = 'No encuentro un MBID en el texto.'
            return
        mbid = m.group(0).lower()
        self.btn_mbid['state'] = 'disabled'
        self.msg['text'] = 'Comprobando…'

        def done(data, err):
            if not self.winfo_exists():
                return
            self.btn_mbid['state'] = 'normal'
            artist = data if data else {'id': mbid, 'name': mbid}
            self.msg['text'] = '' if data else ('No se pudo comprobar el nombre; se usará el MBID.' if err else 'MusicBrainz no conoce ese MBID.')
            if not data and not err:
                return
            self.options.append(option([artist]))
            self.fill(len(self.options) - 1)

        self.app.run_bg(lambda: self.app.mb.get('artist/' + mbid), done)

    def open_mb(self):
        i = self.current()
        if i >= 0:
            webbrowser.open('https://musicbrainz.org/artist/' + self.options[i]['ids'][0])
        else:
            webbrowser.open('https://musicbrainz.org/search?type=artist&query=' + urllib.parse.quote(self.query.get()))

    def accept(self):
        choice = self.current()
        r = self.a.result
        r['options'] = self.options
        r['choice'] = choice
        r['checked'] = choice >= 0
        if choice >= 0 and r['confidence'] in ('baja', 'ninguna', 'media'):
            r['confidence'] = 'alta'
        if not self.a.tag_ids:
            self.app.cache[self.a.key] = r
            save_cache(self.app.cache)
        self.app.update_row(self.a)
        self.app.update_counts()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
