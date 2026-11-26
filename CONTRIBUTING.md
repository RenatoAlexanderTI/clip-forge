# Cómo contribuir a ClipForge

¡Gracias por querer ayudar! Toda contribución suma: reportar un error, proponer una idea, mejorar la documentación o enviar código.

## Reportar un error

1. Revisa que no exista ya un [Issue](https://github.com/RenatoAlexanderTI/clip-forge/issues) igual.
2. Abre uno nuevo con la plantilla **"Reportar un error"** e incluye:
   - qué hiciste y qué esperabas que pasara;
   - qué pasó en realidad (copia el texto de la consola de ClipForge);
   - el contenido de `clipforge_error.log`, si existe;
   - tu versión de Windows y de Python (`python --version`).

> **Nunca** pegues el contenido de `config.json` ni de `cookies.txt`: tienen tus sesiones de Higgsfield y YouTube.

## Proponer una idea

Abre un Issue con la plantilla **"Proponer una mejora"**. Explica qué problema resuelve y, si puedes, cómo te imaginas que funcione.

## Enviar código

1. Haz un **fork** del repositorio y crea una rama con un nombre descriptivo:
   ```bash
   git checkout -b mejora/nombre-corto
   ```
2. Prepara el entorno:
   ```bash
   python -m pip install -r requirements.txt
   ```
   y las herramientas de la sección *Instalación* del [README](README.md).
3. Haz tus cambios. Estructura del proyecto:
   - `clipforge.py`: todo el backend (cliente MCP de Higgsfield, inicio de sesión OAuth, descarga, recorte 9:16, subtítulos) y la API que usa la interfaz.
   - `ui/index.html`: la interfaz (HTML, CSS y JavaScript en un solo archivo, sin dependencias externas).
4. Prueba la app de punta a punta con un video corto. Para depurar la interfaz en el navegador:
   ```bash
   python clipforge.py --browser
   ```
5. Haz commit con mensajes claros, en español o inglés:
   ```bash
   git commit -m "Agrega estilo de subtítulos Karaoke"
   ```
6. Abre un **Pull Request** explicando qué cambia y por qué. Si cambia la interfaz, agrega una captura.

## Estilo de código

- Python 3.10+, solo con la librería estándar más `pywebview`. Evita agregar dependencias si no son necesarias.
- Mensajes y textos de la interfaz en español.
- Funciones cortas y con nombres claros. Comenta solo lo que no sea obvio.

## Conducta

Trata a todos con respeto. Las críticas van dirigidas al código, no a las personas.
