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
5. Envía cada capítulo a Gemini (agrupando hasta `MAX_CHARS_PER_REQUEST` caracteres por petición),
   con throttling de `GEMINI_RPM` peticiones/minuto y reintentos con backoff ante 429/5xx.
6. Guarda la traducción en `translated/` y `translated-plain/`, y en una caché por capítulo que permite
   **reanudar** si se corta la conexión o se agota la cuota diaria (vuelve a ejecutar el mismo comando).
7. Reconstruye el EPUB copiando byte a byte todo lo que no es texto (imágenes, CSS, fuentes), cambia
   `dc:language`, `xml:lang`, el índice (NCX) y da al libro un identificador nuevo para que el lector
   no lo confunda con el original.
8. Valida el resultado (XML bien formado, mismas imágenes/enlaces/tablas, recursos intactos) y genera
   `work/<libro>/report.json` con segmentos posiblemente sin traducir y avisos de formato.

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

## Glosario (opcional)

Para mantener nombres y términos consistentes entre capítulos, crea un archivo (ver
`glossary.example.txt`) y apúntalo con `GLOSSARY_FILE=glossary.txt` en el `.env`.

## Limitaciones

- EPUB con DRM no se pueden traducir (se detecta y se avisa).
- No se traduce texto dentro de imágenes, SVG, `<pre>`/`<code>` ni el título en los metadatos.
- La limpieza es heurística y conservadora: prefiere dejar un artefacto a borrar texto de la historia.

## Tests

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```
