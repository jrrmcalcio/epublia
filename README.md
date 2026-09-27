# epublia

Traduce libros EPUB con Gemini (free tier) conservando la estructura original: estilos, imágenes,
portada, índice, ids, enlaces y formato inline (cursivas, negritas, saltos de línea). Solo cambia el texto.

## Instalación

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # Windows
# .venv/bin/pip install -r requirements.txt        # Linux / macOS
copy .env.example .env                             # y rellena GEMINI_API_TOKEN
```

Verifica la key y el modelo (1 petición de prueba; muestra los modelos Flash disponibles):

```bash
epublia --check
```

## Uso

Copia tus `.epub` en `books-input/` y ejecuta con el nombre completo o solo una palabra:

```bash
epublia firstborn                          # traduce todos los EPUB cuyo nombre contenga "firstborn"
epublia "Firstborn_-_Christie_Golden.epub" # nombre exacto
epublia christie golden                    # todas las palabras deben aparecer en el nombre
epublia C:\ruta\a\libro.epub               # ruta directa
epublia firstborn --list                   # solo muestra qué libros coinciden
epublia firstborn --extract-only           # solo extrae los TXT por capítulo (sin API)
epublia firstborn --lang FR                # otro idioma sin tocar el .env
epublia firstborn --no-cache               # vuelve a traducir todo
```

`epublia` es `.\epublia.bat` (PowerShell/cmd) o `./epublia.sh` (bash); también sirve
`.venv\Scripts\python -m epublia`.

Códigos de salida: `0` ok, `1` algún libro falló, `2` error de configuración, `3` cuota diaria agotada
(el progreso queda guardado).

El libro traducido queda en `books-outputs/<nombre>_<IDIOMA>.epub`.

## Cómo funciona

1. Lee el EPUB (OPF, spine, manifest, NCX) y recorre cada documento XHTML en orden de lectura.
2. Divide cada capítulo en segmentos (párrafos, títulos, texto suelto). El formato inline se
   sustituye por marcadores `<x1>…</x1>` / `<x2/>` para que el modelo pueda moverlo con las palabras.
3. Escribe `work/<libro>/source/NNN_capitulo.txt` (con marcadores `[[n]]`) y `source-plain/` (texto limpio).
4. Envía cada capítulo a Gemini (agrupando hasta `MAX_CHARS_PER_REQUEST` caracteres por petición),
   con throttling de `GEMINI_RPM` peticiones/minuto y reintentos con backoff ante 429/5xx.
5. Guarda la traducción en `translated/` y `translated-plain/`, y en una caché por capítulo que permite
   **reanudar** si se corta la conexión o se agota la cuota diaria (vuelve a ejecutar el mismo comando).
6. Reconstruye el EPUB copiando byte a byte todo lo que no es texto (imágenes, CSS, fuentes), cambia
   `dc:language`, `xml:lang`, el índice (NCX) y da al libro un identificador nuevo para que el lector
   no lo confunda con el original.
7. Valida el resultado (XML bien formado, mismas imágenes/enlaces/tablas, recursos intactos) y genera
   `work/<libro>/report.json` con segmentos posiblemente sin traducir y avisos de formato.

## Free tier de Gemini

- Los límites (peticiones/minuto y por día) dependen del modelo y la cuenta; consúltalos en
  <https://aistudio.google.com/rate-limit>. Ajusta `GEMINI_RPM` a tu límite.
- Si se agota la cuota **diaria**, epublia se detiene limpiamente (código 3) y al día siguiente
  continúa donde quedó.
- Para no pagar nunca: crea la key en un proyecto **sin cuenta de facturación**. Con facturación
  activa, Google cobra por uso. La API no permite comprobarlo desde el código.
- En el free tier Google puede usar el contenido enviado para mejorar sus productos.

## Glosario (opcional)

Para mantener nombres y términos consistentes entre capítulos, crea un archivo (ver
`glossary.example.txt`) y apúntalo con `GLOSSARY_FILE=glossary.txt` en el `.env`.

## Limitaciones

- EPUB con DRM no se pueden traducir (se detecta y se avisa).
- No se traduce texto dentro de imágenes, SVG, `<pre>`/`<code>` ni el título en los metadatos.
- EPUB convertidos desde PDF pueden traer cabeceras de página sueltas ("2 CHRISTIE GOLDEN"); se
  traducen tal cual porque forman parte del texto original.

## Tests

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```
