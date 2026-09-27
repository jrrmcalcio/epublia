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
epublia firstborn --clean-only             # solo limpia basura de PDF -> <nombre>_CLEAN.epub (sin API)
epublia firstborn --no-clean               # traduce sin limpieza previa
epublia firstborn --lang FR                # otro idioma sin tocar el .env
epublia firstborn --no-cache               # vuelve a traducir todo
epublia firstborn --dry-run                # qué se traduciría, cuántas peticiones y problemas de nombres (sin API)
epublia firstborn --glossary-only          # genera el glosario del libro y para, para revisarlo antes
epublia firstborn --fix-names              # retraduce solo los segmentos que no respetan el glosario
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
3. **Limpia la basura de conversión PDF** (ver abajo) y registra cada cambio en `work/<libro>/cleanup.txt`.
4. Escribe `work/<libro>/source/NNN_capitulo.txt` (con marcadores `[[n]]`) y `source-plain/` (texto limpio).
5. Genera el **glosario** del libro la primera vez (ver [Coherencia de nombres](#coherencia-de-nombres)).
6. Envía cada capítulo a Gemini (agrupando hasta `MAX_CHARS_PER_REQUEST` caracteres por petición),
   con el glosario aplicable y el pasaje anterior como contexto, throttling de `GEMINI_RPM`
   peticiones/minuto y reintentos con backoff ante 429/5xx. Los párrafos muy largos (típicos de
   PDFs convertidos) se envían en trozos de hasta `SEGMENT_SPLIT_CHARS` cortados en fin de frase.
   Si Gemini bloquea o se salta un segmento, se reintenta en mitades para que solo quede sin
   traducir el fragmento problemático.
7. Guarda la traducción en `work/<libro>/<idioma>/` (`translated/`, `translated-plain/` y una caché
   por capítulo) que permite **reanudar** si se corta la conexión o se agota la cuota diaria. Cada
   idioma tiene su propia caché y glosario.
8. Reconstruye el EPUB copiando byte a byte todo lo que no es texto (imágenes, CSS, fuentes), cambia
   `dc:language`, `xml:lang`, el índice (NCX) y da al libro un identificador nuevo para que el lector
   no lo confunda con el original. Si el modelo olvida cerrar una etiqueta de formato, se cierra
   donde terminaba en el original.
9. Valida el resultado (XML bien formado, mismas imágenes/enlaces/tablas, recursos intactos) y genera
   `work/<libro>/<idioma>/report.json` con segmentos sin traducir, **posiblemente incompletos**
   (mucho más cortos que el original o con frases aún en el idioma original), problemas de nombres
   y avisos de formato. Los dudosos se muestran también en la consola como `CHECK:`.

## Limpieza de basura de PDF

Muchos EPUB vienen de PDFs convertidos. Antes de traducir, epublia elimina o corrige:

| Problema | Ejemplo | Acción |
|---|---|---|
| Encabezados/pies de página | `2 CHRISTIE GOLDEN`, `FIRSTBORN 3` | Se eliminan (título/autor + número, o línea numerada que se repite ≥3 veces) |
| Encabezados repetidos en mayúsculas | `CHAPTER ONE` en cada página | Se eliminan si aparecen ≥5 veces a mitad de párrafo |
| Números de página sueltos | `12`, `- 12 -`, `[12]`, `Page 12`, `12 of 300`, `xii` | Se eliminan (nunca en títulos `<h1>`…) |
| Marcas de agua | `OceanofPDF.com`, "Scanned by…", "This page intentionally left blank" | Se eliminan |
| Líneas cortadas a mitad de oración | `had done ⏎ to get inside` | Se unen |
| Párrafos partidos | `</p><p>` seguido de minúscula | Se fusionan |
| Palabras con guion de corte | `some- ⏎ thing`, `sepa- rate` | Se unen (respeta `pre- and post-war`) |
| Ligaduras y caracteres invisibles | `ﬁ ﬂ ﬀ`, soft hyphen, zero-width | Se normalizan |
| Letras espaciadas | `C H A P T E R` | Se unen |
| Espacio antes de puntuación | `word ,` | Se corrige (respeta `. . .`) |
| Notas al pie de PDF incrustadas | `1 See the appendix…` a mitad de párrafo | **Solo se reportan** en `cleanup.txt`: no se pueden distinguir con seguridad del texto |

Las notas al pie reales de EPUB (enlaces/`noteref`) se conservan y se traducen. Revisa
`work/<libro>/cleanup.txt` y, si quieres ver el resultado sin gastar cuota, usa `--clean-only`.
Se desactiva con `--no-clean` o `CLEANUP=false`.

## Free tier de Gemini

- Los límites (peticiones/minuto y por día) dependen del modelo y la cuenta; consúltalos en
  <https://aistudio.google.com/rate-limit>. Ajusta `GEMINI_RPM` a tu límite.
- Si se agota la cuota **diaria**, epublia se detiene limpiamente (código 3) y al día siguiente
  continúa donde quedó.
- Para no pagar nunca: crea la key en un proyecto **sin cuenta de facturación**. Con facturación
  activa, Google cobra por uso. La API no permite comprobarlo desde el código.
- En el free tier Google puede usar el contenido enviado para mejorar sus productos.

## Coherencia de nombres

Cada capítulo se traduce en peticiones separadas, así que sin ayuda el modelo puede escribir
`Tigre Gris` en un capítulo y `Gray Tiger` en otro. epublia lo evita de tres formas:

1. **Glosario automático.** Antes de traducir, busca en el libro (sin API) los nombres propios y
   términos inventados que se repiten y pide al modelo, en 1–2 peticiones, cómo traducir cada uno.
   El resultado queda en `work/<libro>/<idioma>/glossary.txt`, editable, y no se regenera mientras exista
   (bórralo para crear uno nuevo). Los candidatos brutos están en `glossary-candidates.txt`.
   Cada petición incluye solo las entradas que aparecen en sus segmentos.
2. **Contexto.** Cada petición lleva el final del pasaje anterior ya traducido (`CONTEXT_CHARS`,
   1500 por defecto; `0` lo desactiva) para mantener nombres, tono y tratamientos (tú/usted).
3. **Revisión.** Tras cada ejecución, `work/<libro>/<idioma>/names.txt` (y `report.json`) lista los segmentos
   que no respetan el glosario: traducción distinta o mayúsculas incoherentes (`los shelak` frente
   a `los Shelak`). `--fix-names` retraduce solo esos segmentos. Si la nueva versión sale peor
   (bloqueada, incompleta o con más fallos), se conserva la anterior.

Formato del glosario (una regla por línea, `#` para comentarios):

```
Gray Tiger = Tigre Gris
Preserver = Preservador | Preservadora    # alternativas según género/número
Shelak = Shelak
```

Para reglas que valgan para todos tus libros, crea un archivo (ver `glossary.example.txt`) y
apúntalo con `GLOSSARY_FILE=glossary.txt` en el `.env`: sus entradas prevalecen sobre las del
glosario automático. `AUTO_GLOSSARY=false` desactiva la generación automática.

Todo es automático: `epublia libro` genera el glosario (si no existe) y traduce en una sola
ejecución. Si prefieres revisar el glosario antes de gastar la cuota de la traducción:

```bash
epublia libro --dry-run          # coste estimado
epublia libro --glossary-only    # 1–2 peticiones; revisa work/<libro>/<idioma>/glossary.txt
epublia libro                    # traduce
```

Para un libro ya traducido: `--glossary-only`, revisa el glosario, `--dry-run --fix-names` para ver
el coste y después `--fix-names`.

## Limitaciones

- EPUB con DRM no se pueden traducir (se detecta y se avisa).
- No se traduce texto dentro de imágenes, SVG, `<pre>`/`<code>` ni el título en los metadatos.
- La limpieza es heurística y conservadora: prefiere dejar un artefacto a borrar texto de la historia.

## Tests

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```
