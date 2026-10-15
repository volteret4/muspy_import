# muspy_import

Página estática para seguir en [muspy](https://muspy.com) los artistas de una colección de Discogs o de una exportación de RateYourMusic.

## Cómo funciona

1. El usuario sube uno o varios CSV:
   - **Discogs**: *Collection → Export* (`…-collection-YYYYMMDD-HHMM.csv`).
   - **RYM**: *Profile → Export your data* (`…-music-export.csv`).
   - **App de escritorio** (`desktop/`): CSV con las columnas `artist,mbid,albums,source_name`.
   - Last.fm no necesita CSV: muspy lo importa en https://muspy.com/import.
2. La página busca cada artista en la [API de MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API), que permite una petición por segundo. Primero busca uno de sus discos, porque el artista acreditado en el disco suele ser el correcto. Si no lo encuentra, busca el nombre. Los resultados se guardan en `localStorage`.
3. El usuario revisa los resultados dudosos y marca qué artistas quiere seguir.
4. El **bookmarklet** sigue los artistas. Hay que pulsarlo dentro de muspy.com, con la sesión iniciada. muspy no permite llamadas a su API desde otros dominios (no envía cabeceras CORS) y además la API pide email y contraseña. Por eso el bookmarklet usa la ruta web `/artists-add?id=<mbid>` con la sesión del propio usuario. No hace falta contraseña ni `userid`.

Los MBID llegan a muspy en el fragmento de la URL (`https://muspy.com/artists#muspy-import=…`), que nunca se envía al servidor. Si el enlace falla, la lista también se puede copiar y pegar.

## App de escritorio

`desktop/muspy_import.py` lee las etiquetas de una carpeta de música con [mutagen](https://mutagen.readthedocs.io/). Si los archivos están etiquetados con Picard, toma los MBID directamente de las etiquetas. El resto de artistas los busca en MusicBrainz con la misma lógica que la web. Después se revisan, se editan y se exporta el CSV. La caché se guarda en `~/.cache/muspy_import/` (en Windows, `%LOCALAPPDATA%\muspy_import\`).

En Linux:

```bash
pip install mutagen && python3 desktop/muspy_import.py
```

En Arch, Tkinter viene en el paquete `tk` y mutagen en `python-mutagen`.

El `.exe` de Windows lo compila GitHub Actions con PyInstaller al subir una etiqueta `v*`. Se publica en *Releases* junto con el `.py`:

```bash
git tag v1.0.1 && git push origin v1.0.1
```

## Publicar en GitHub Pages

```bash
gh repo create muspy_import --public --source . --push
```

Después, en *Settings → Pages*, elige la rama `main` y la carpeta `/ (root)`.

## Probar en local

```bash
python -m http.server 8000
```
