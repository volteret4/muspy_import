# muspy_import

Página estática para seguir en [muspy](https://muspy.com) los artistas de una colección de Discogs o de una exportación de RateYourMusic.

## Cómo funciona

1. El usuario sube uno o varios CSV:
   - **Discogs**: *Collection → Export* (`…-collection-YYYYMMDD-HHMM.csv`).
   - **RYM**: *Profile → Export your data* (`…-music-export.csv`).
2. La página busca cada artista en la [API de MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API), que permite una petición por segundo. Primero busca uno de sus discos, porque el artista acreditado en el disco suele ser el correcto. Si no lo encuentra, busca el nombre. Los resultados se guardan en `localStorage`.
3. El usuario revisa los resultados dudosos y marca qué artistas quiere seguir.
4. El **bookmarklet** sigue los artistas. Hay que pulsarlo dentro de muspy.com, con la sesión iniciada. muspy no permite llamadas a su API desde otros dominios (no envía cabeceras CORS) y además la API pide email y contraseña. Por eso el bookmarklet usa la ruta web `/artists-add?id=<mbid>` con la sesión del propio usuario. No hace falta contraseña ni `userid`.

Los MBID llegan a muspy en el fragmento de la URL (`https://muspy.com/artists#muspy-import=…`), que nunca se envía al servidor. Si el enlace falla, la lista también se puede copiar y pegar.

## Publicar en GitHub Pages

```bash
gh repo create muspy_import --public --source . --push
```

Después, en *Settings → Pages*, elige la rama `main` y la carpeta `/ (root)`.

## Probar en local

```bash
python -m http.server 8000
```
