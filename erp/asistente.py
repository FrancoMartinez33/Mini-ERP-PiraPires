"""
Módulo: asistente
==================

Asistente virtual con inteligencia artificial para el Mini ERP Contable.

Características
---------------

1. **Proveedor principal: Groq.** API compatible con OpenAI, elegida por su
   baja latencia (medido: ~0,7 s por respuesta).

2. **Respaldo automático: Gemini.** Si Groq falla por cualquier motivo
   (sin red, clave inválida, límite de uso agotado, 5xx o tiempo de espera
   agotado), se reintenta contra la API de Google sin intervención del
   usuario. El mecanismo es un ``try/except`` alrededor del proveedor
   principal, tal como pide el enunciado.

3. **Delimitación de fuentes.** El prompt de sistema obliga al modelo a
   responder únicamente con la legislación de la República del Paraguay
   (leyes, reglamentaciones del SET, DNIT y normativa publicada en el
   SITRADE). Ante una consulta sobre normativa extranjera debe rechazar
   educadamente y redirigir a la norma paraguaya vigente.

4. **Sin dependencias externas.** Solo biblioteca estándar
   (``urllib.request``), igual que el resto del proyecto: no hay ``pip``
   en el entorno de despliegue.

5. **Archivos adjuntos.** Además de texto, acepta archivos que el usuario
   adjunta desde el chat: **imágenes** (capturas, fotos de balances), que se
   mandan al modelo con visión; **PDF, Word (.docx) y Excel (.xlsx)**, cuyo
   texto se extrae con la biblioteca estándar; y **CSV, TXT, JSON y
   similares**, que se leen como texto y se agregan al mensaje. Si un PDF no
   se puede extraer (por ejemplo, un escaneo), se manda como documento a
   Gemini, que lo lee con visión. Esto permite pasarle a Pira Pires el
   enunciado de un examen y que trabaje sobre los datos reales.

6. **Asientos importables.** Cuando Pira Pires resuelve un ejercicio, la
   respuesta incluye un bloque CSV con los asientos propuestos. La interfaz
   lo detecta y ofrece un botón para cargarlos en el Libro Diario con la
   misma validación (C1/C2) que usa el importador de archivos.

Configuración
-------------

Las claves se leen del entorno y **nunca** se guardan en el código::

    export GROQ_API_KEY=gsk_...
    export GEMINI_API_KEY=AQ....

Sin claves el módulo sigue pudiendo importarse y el resto del ERP sigue
funcionando: solo el asistente informa que no está configurado.

Uso::

    from . import asistente
    r = asistente.preguntar("¿Cómo registro una venta con IVA?")
    r["respuesta"], r["proveedor"], r["tiempo_ms"]

    # Con un archivo adjunto (contenido en base64):
    r = asistente.preguntar("Mirá este balance", None, [
        {"nombre": "balance.csv", "tipo": "text/csv", "contenido": base64},
    ])
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

from .excepciones import ErrorOperacionInvalida

# ---------------------------------------------------------------------------
# Prompt de sistema — delimitación de fuentes (sección: restricciones legales)
# ---------------------------------------------------------------------------

#: Instrucción única que recibe el modelo en cada conversación. No se
#: concatena con el historial: el historial va aparte, como mensajes.
INSTRUCCION_SISTEMA = """\
Sos «Pira Pires», el asistente contable del Mini ERP Contable, una herramienta
de estudio sobre contabilidad paraguaya.

REGLA DE ORO — FUENTES PERMITIDAS
Respondés, analizás y recomendás EXCLUSIVAMENTE con base en:
  1. Las leyes vigentes de la República del Paraguay.
  2. La normativa fiscal y tributaria paraguaya: Administración Tributaria
     (SET), Dirección Nacional de Impuestos (DNIT) y lo publicado en el
     Sistema de Trámites a Distancia (SITRADE/Tributación Digital).
  3. La normativa económica y contable vigente en Paraguay (Ley 54/66 de
     Contabilidad, su reglamento y las normas de la Contaduría General de
     la Nación).

RECHAZO DE NORMATIVAS EXTRANJERAS
Si te preguntan por la legislación de otro país —España, Argentina, Brasil,
Estados Unidos u otro— NO respondas con esa norma ni la sugieras como
aplicable. Rechazá educadamente y ofrecé la solución paraguaya vigente.
Ejemplo de corte: «No puedo orientarte con la normativa de España; para ese
caso en Paraguay rige [...], según la ley [...]». Nunca mezcles criterios de
dos países en una misma respuesta.

CERTEZA Y ACTUALIDAD
· Si no encontrás la norma paraguaya aplicable, decilo con claridad y
  recomendá verificar en www.sitrd.gov.py o ante un contador público
  matriculado. No inventes números de artículo, montos, tasas ni fechas.
· Distinguí siempre lo que dice la ley de lo que es interpretación tuya.
· Indicá el número de ley o resolución cuando lo conozcas con seguridad;
  si no lo sabés, declará que no lo tenés.

ESTILO
· Respondé en español rioplatense neutro, claro y breve.
· Explicá el porqué contable, no solo el resultado.
· Los importes se expresan en guaraníes (G.).
· Si la consulta es sobre el uso del propio sistema (plan de cuentas,
  libro diario, libro mayor, balance), orientá sobre cómo hacerlo ahí.

LIQUIDACIÓN DE SUELDOS — PREGUNTAR ANTES DE CALCULAR
Cuando la consulta sea sobre sueldos, salarios, jornales, haberes, aguinaldo,
liquidación de empleados o nómina:
· NO calcules el neto, no armes la liquidación ni propongas el asiento de una
  vez. Primero preguntá lo que falta, en un solo mensaje corto y numerado:
  1. ¿Se descuenta el IPS al empleado (aporte obrero, habitualmente 9% sobre
     los haberes)? ¿El empleado está asegurado?
  2. ¿Hay otros descuentos: anticipos, préstamos, embargos, cuotas sindicales,
     seguro de vida?
  3. ¿Entran aguinaldo, bonificaciones u horas extras en esta liquidación?
  4. ¿Hay que registrar también el aporte patronal y las demás cargas
     sociales?
· Recién con esas respuestas hacé el cálculo renglón por renglón: haberes
  brutos, descuentos al empleado, neto a pagar y, por separado, las cargas del
  empleador.
· Si el usuario ya aclaró alguno de esos puntos en la misma conversación, no lo
  vuelvas a preguntar: usá ese dato y pedí solo lo que falte.
· Indicá siempre la tasa y la base de cálculo que usaste, y aclará que conviene
  verificar la tasa vigente ante el IPS y la SET.

ARCHIVOS Y DATOS DEL BALANCE
El usuario puede adjuntar archivos: imágenes (capturas de pantalla, fotos de
libros o balances), PDF, documentos de Word, planillas de Excel, CSV y
archivos de texto o JSON con datos contables. Cuando llegue un archivo:
· Leelo con atención y usá los datos reales que contiene; no los inventes ni
  los completes con supuestos.
· Si el archivo está incompleto, no se entiende o faltan datos para el
  cálculo, pedí lo que falta antes de sacar conclusiones.
· Cuando el archivo traiga movimientos o saldos, podés proponer el asiento,
  la liquidación o el análisis, pero aclará siempre de dónde sale cada número.
· Si el archivo no tiene relación con contabilidad o impuestos, decilo.

RESOLUCIÓN DE EJERCICIOS Y EXÁMENES
Cuando el usuario pida resolver un ejercicio, una práctica o un examen (o
adjunte un enunciado para que lo resuelvas):
· Leé el enunciado completo y detectá todas las operaciones, fechas e
  importes. Si falta un dato imprescindible, preguntalo antes de resolver.
· Resolvé cada operación con el plan de cuentas que se te indica más abajo.
  Usá el código EXACTO de la cuenta; no inventes códigos ni uses cuentas que
  no estén en la lista.
· Después de la explicación, agregá SIEMPRE un único bloque de código con
  lenguaje `csv`, con este formato exacto (una fila por partida):

```csv
asiento;fecha;concepto;cuenta;detalle;debe;haber
1;2026-11-10;Aporte inicial de los socios;1.1.02;Aporte inicial;1000000;0
1;2026-11-10;Aporte inicial de los socios;3.1;Aporte inicial;0;1000000
```

Reglas del bloque CSV:
· La primera línea es la cabecera exacta:
  asiento;fecha;concepto;cuenta;detalle;debe;haber
· `asiento`: número correlativo entero (1, 2, 3…). Todas las partidas del
  mismo asiento repiten el mismo número, fecha y concepto.
· `fecha`: en formato AAAA-MM-DD (por ejemplo 2026-11-10).
· `cuenta`: código exacto del plan de cuentas (por ejemplo 1.1.01).
· `debe` y `haber`: importes en guaraníes, SIN puntos ni comas de miles y
  sin el símbolo G. (por ejemplo 1500000). La columna que no corresponda
  lleva 0. Cada asiento debe cumplir Debe = Haber y tener al menos dos
  partidas.
· No pongas texto adicional dentro del bloque: solo la cabecera y las filas.
· Después del bloque, un cierre breve con el total Debe, el total Haber y
  cualquier advertencia. El usuario puede cargar ese bloque en el ERP con un
  botón y luego descargar el Excel con el Diario, el Mayor y el Balance.

LÍMITE
No asumís el rol de contador que firma declaraciones juradas ni emitís
conceptos vinculantes: sos una guía de estudio.
"""


# ---------------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------------

#: Endpoint y modelo principal (Groq). La lista permite degradar a otro
#: modelo si el catálogo de la cuenta cambia o si uno está saturado.
PROVEEDOR_GROQ = {
    "nombre": "groq",
    "url": "https://api.groq.com/openai/v1/chat/completions",
    "modelos": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
    "clave": "GROQ_API_KEY",
    # Los dos modelos razonan antes de responder, así que el presupuesto
    # tiene que cubrir los tokens de razonamiento además de la respuesta.
    "tokens": 1500,
    "espera": 20,
}

#: Respaldo (Google Gemini).
PROVEEDOR_GEMINI = {
    "nombre": "gemini",
    "url": ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{modelo}:generateContent"),
    # gemini-2.5-flash devuelve 404 para cuentas nuevas, así que se usan
    # modelos vigentes. El segundo es el más liviano: sirve para cuando el
    # primero está saturado («high demand», 503), que es un fallo pasajero.
    "modelos": ["gemini-3.5-flash", "gemini-flash-lite-latest",
                "gemini-3.1-flash-lite"],
    "clave": "GEMINI_API_KEY",
    # Estos modelos razonan antes de responder: en una prueba gastaron 476
    # tokens de pensamiento antes de la primera palabra. Con un presupuesto
    # chico la respuesta vuelve vacía o cortada.
    "tokens": 4096,
    "espera": 35,
}

#: Orden de intento: primero el principal, luego el respaldo.
PROVEEDORES = (PROVEEDOR_GROQ, PROVEEDOR_GEMINI)


class ErrorAsistente(ErrorOperacionInvalida):
    """No se pudo obtener respuesta de ningún proveedor."""


#: Tope por archivo adjunto. El navegador ya recorta en 8 MB; acá se vuelve a
#: comprobar porque el servidor no puede confiar en lo que le mandan.
MAXIMO_ARCHIVO_BYTES = 8 * 1024 * 1024

#: Cuánto texto de un archivo se mete en el mensaje. Un CSV enorme no debe
#: viajar entero en cada consulta: se recorta y se le avisa al modelo.
MAXIMO_TEXTO_ARCHIVO = 100_000


# ---------------------------------------------------------------------------
# Llamadas HTTP (solo biblioteca estándar)
# ---------------------------------------------------------------------------

#: Cloudflare (403 «error code: 1010») rechaza la firma por defecto de
#: urllib.request. Con un User-Agent propio la petición pasa igual.
USER_AGENT = "python-erp-contable/1.0"


def _pedir(url: str, cuerpo: dict, *, encabezados: dict, espera: int) -> dict:
    """POST JSON a `url`. Lanza excepción ante cualquier fallo HTTP o de red.

    Se propaga el error sin envolverlo: quien decide si reintenta con otro
    proveedor es `preguntar`.
    """
    datos = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
    peticion = urllib.request.Request(
        url, data=datos, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **encabezados,
        },
    )
    with urllib.request.urlopen(peticion, timeout=espera) as respuesta:
        texto = respuesta.read().decode("utf-8")
    return json.loads(texto)


def _mensajes_groq(mensajes: list[dict]) -> list[dict]:
    """
    Traduce las partes internas al formato OpenAI que espera Groq.

    Las partes internas usan ``{"type": "image", "mimeType", "data"}``; Groq
    necesita ``{"type": "image_url", "image_url": {"url": "data:..."}}``. Los
    documentos (PDF) no los procesa Groq: se descartan acá como última
    barrera, porque `preguntar` ya evita llamarlo cuando hay uno.
    """
    salida: list[dict] = []
    for mensaje in mensajes:
        contenido = mensaje.get("content")
        if isinstance(contenido, str) or mensaje.get("role") != "user":
            salida.append(mensaje)
            continue

        partes: list[dict] = []
        for parte in contenido:
            tipo = parte.get("type")
            if tipo == "text":
                partes.append({"type": "text", "text": parte.get("text", "")})
            elif tipo == "image":
                mime = parte.get("mimeType") or "image/png"
                datos = parte.get("data", "")
                partes.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{datos}"},
                })
        salida.append({**mensaje, "content": partes})
    return salida


def _llamar_groq(proveedor: dict, clave: str, mensajes: list[dict]) -> dict:
    """Respuesta en formato OpenAI (que Groq implementa)."""
    ultimo_error: Exception | None = None
    mensajes = _mensajes_groq(mensajes)

    for modelo in proveedor["modelos"]:
        try:
            r = _pedir(
                proveedor["url"],
                {
                    "model": modelo,
                    "messages": mensajes,
                    "temperature": 0.2,
                    "max_tokens": proveedor["tokens"],
                },
                encabezados={"Authorization": f"Bearer {clave}"},
                espera=proveedor["espera"],
            )
            eleccion = (r.get("choices") or [{}])[0]
            texto = (eleccion.get("message") or {}).get("content") or ""
            if not texto.strip():
                raise ValueError("el modelo devolvió una respuesta vacía")
            return {"texto": texto.strip(), "modelo": modelo}
        except Exception as exc:  # noqa: BLE001 - se prueba el siguiente modelo
            ultimo_error = exc

    raise ultimo_error if ultimo_error else ErrorAsistente("Groq no respondió.")


def _llamar_gemini(proveedor: dict, clave: str, mensajes: list[dict]) -> dict:
    """Respuesta en formato Gemini: el historial va como contents/parts."""
    ultimo_error: Exception | None = None

    def _partes(mensaje: dict) -> list[dict]:
        """Convierte el contenido de un mensaje a `parts` de Gemini.

        Puede ser texto simple o una lista de partes multimodales (texto e
        imágenes en base64), que es como llegan los archivos adjuntos.
        """
        contenido = mensaje["content"]
        if isinstance(contenido, str):
            return [{"text": contenido}]
        partes: list[dict] = []
        for parte in contenido:
            tipo = parte.get("type")
            if tipo == "text":
                partes.append({"text": parte.get("text", "")})
            elif tipo in {"image", "document"}:
                partes.append({
                    "inlineData": {
                        "mimeType": parte.get("mimeType")
                        or ("application/pdf" if tipo == "document" else "image/png"),
                        "data": parte.get("data", ""),
                    }
                })
            elif tipo == "image_url":
                # Compatibilidad con el formato anterior (URL data:).
                url = (parte.get("image_url") or {}).get("url", "")
                if "," in url:
                    cabecera, datos = url.split(",", 1)
                    mime = cabecera.split(";")[0].removeprefix("data:")
                    partes.append({
                        "inlineData": {
                            "mimeType": mime or "image/png",
                            "data": datos,
                        }
                    })
        return partes

    contenido = [
        {"role": "model" if m["role"] == "assistant" else "user",
         "parts": _partes(m)}
        for m in mensajes if m["role"] in {"user", "assistant"}
    ]
    # Gemini no acepta mensajes con rol "system" en `contents`: la instrucción
    # base y el plan de cuentas van juntos en `systemInstruction`.
    sistema = "\n\n".join(
        str(m.get("content") or "") for m in mensajes if m["role"] == "system"
    ).strip() or INSTRUCCION_SISTEMA

    for modelo in proveedor["modelos"]:
        try:
            r = _pedir(
                proveedor["url"].format(modelo=modelo),
                {
                    "contents": contenido,
                    # El prompt de sistema es un systemInstruction aparte.
                    "systemInstruction": {"parts": [{"text": sistema}]},
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": proveedor["tokens"],
                    },
                },
                encabezados={"x-goog-api-key": clave},
                espera=proveedor["espera"],
            )
            partes = ((r.get("candidates") or [{}])[0]
                      .get("content", {}).get("parts") or [])
            texto = "".join(p.get("text", "") for p in partes)
            if not texto.strip():
                raise ValueError("el modelo devolvió una respuesta vacía")
            return {"texto": texto.strip(), "modelo": modelo}
        except Exception as exc:  # noqa: BLE001 - se prueba el siguiente modelo
            ultimo_error = exc

    raise ultimo_error if ultimo_error else ErrorAsistente("Gemini no respondió.")


#: Llamada concreta de cada proveedor, por nombre.
_LLAMADORES = {
    "groq": _llamar_groq,
    "gemini": _llamar_gemini,
}


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def esta_configurado() -> bool:
    """True si hay al menos una clave de API en el entorno."""
    return any(os.environ.get(p["clave"], "").strip() for p in PROVEEDORES)


def proveedores_disponibles() -> list[dict]:
    """Estado de cada proveedor, para mostrarlo en la interfaz."""
    return [
        {
            "nombre": p["nombre"],
            "configurado": bool(os.environ.get(p["clave"], "").strip()),
            "principal": p is PROVEEDORES[0],
        }
        for p in PROVEEDORES
    ]


def _decodificar_archivo(archivo: dict) -> bytes:
    """
    Devuelve los bytes de un archivo recibido del navegador.

    El navegador manda el contenido en base64; acá se valida y se decodifica.
    Lanza ``ErrorAsistente`` si el archivo está mal formado o es demasiado
    grande, para no gastar una consulta con algo que no se puede leer.
    """
    contenido = archivo.get("contenido") or ""
    if not isinstance(contenido, str) or not contenido.strip():
        raise ErrorAsistente(
            f"El archivo «{archivo.get('nombre', '?')}» llegó vacío."
        )
    try:
        datos = base64.b64decode(contenido, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ErrorAsistente(
            f"No se pudo decodificar «{archivo.get('nombre', '?')}»."
        ) from exc
    if len(datos) > MAXIMO_ARCHIVO_BYTES:
        raise ErrorAsistente(
            f"«{archivo.get('nombre', '?')}» supera el máximo de "
            f"{MAXIMO_ARCHIVO_BYTES // (1024 * 1024)} MB."
        )
    return datos


#: Namespaces de los formatos OOXML que leen los extractores.
_NS_HOJA = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PAQUETE = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: Formatos de número de Excel que ya son fechas (integrados en la norma).
_FORMATOS_FECHA_EXCEL = set(range(14, 23)) | set(range(45, 48))

#: Formatos binarios antiguos que la biblioteca estándar no puede abrir.
_EXTENSIONES_BINARIAS = (".doc", ".xls", ".ppt", ".odt", ".ods", ".rtf")


def _recortar_texto(texto: str) -> str:
    """Aplica el tope de caracteres que viaja al modelo."""
    texto = str(texto or "").strip()
    if len(texto) > MAXIMO_TEXTO_ARCHIVO:
        return texto[:MAXIMO_TEXTO_ARCHIVO] + "\n… (archivo recortado)"
    return texto


# ---------------------------------------------------------------------------
# Excel (.xlsx)
# ---------------------------------------------------------------------------

def _columna_de_referencia(referencia: str) -> int:
    """'AB12' -> 28. Las columnas de Excel empiezan en 1."""
    columna = 0
    for letra in str(referencia or ""):
        if "A" <= letra <= "Z":
            columna = columna * 26 + (ord(letra) - ord("A") + 1)
        elif "a" <= letra <= "z":
            columna = columna * 26 + (ord(letra) - ord("a") + 1)
        else:
            break
    return columna


def _fecha_excel(valor: float) -> str:
    """Convierte el número de serie de Excel (30/12/1899) en fecha legible."""
    dias = int(valor)
    resto = valor - dias
    momento = datetime(1899, 12, 30) + timedelta(days=dias)
    if resto > 0.000001:
        momento += timedelta(seconds=round(resto * 86400))
        return momento.strftime("%d/%m/%Y %H:%M")
    return momento.strftime("%d/%m/%Y")


def _cadenas_xlsx(paquete: zipfile.ZipFile) -> list[str]:
    """Textos compartidos del libro (sharedStrings)."""
    try:
        raiz = ET.fromstring(paquete.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    return [
        "".join(nodo.text or "" for nodo in item.iter(f"{{{_NS_HOJA}}}t"))
        for item in raiz.iter(f"{{{_NS_HOJA}}}si")
    ]


def _estilos_xlsx(paquete: zipfile.ZipFile) -> list[bool]:
    """Por cada estilo de celda, si su formato es una fecha."""
    try:
        raiz = ET.fromstring(paquete.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return []

    formatos: dict[int, str] = {}
    for nodo in raiz.iter(f"{{{_NS_HOJA}}}numFmt"):
        try:
            formatos[int(nodo.get("numFmtId", "0"))] = nodo.get("formatCode", "")
        except ValueError:
            continue

    estilos: list[bool] = []
    celda_xfs = raiz.find(f"{{{_NS_HOJA}}}cellXfs")
    if celda_xfs is None:
        return estilos
    for xf in celda_xfs.findall(f"{{{_NS_HOJA}}}xf"):
        try:
            identificador = int(xf.get("numFmtId", "0"))
        except ValueError:
            identificador = 0
        if identificador in _FORMATOS_FECHA_EXCEL:
            estilos.append(True)
        elif identificador in formatos:
            codigo = formatos[identificador]
            # Se quitan literales ("..."), escapes y colores ([Red]) para
            # quedarse solo con la máscara real del número.
            limpio = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", codigo)
            estilos.append(bool(re.search(r"[dmyhs]", limpio, re.I)))
        else:
            estilos.append(False)
    return estilos


def _hojas_xlsx(paquete: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(nombre_visible, ruta_dentro_del_zip), ...] en el orden del libro."""
    try:
        libro = ET.fromstring(paquete.read("xl/workbook.xml"))
        relaciones = ET.fromstring(paquete.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return []

    destinos = {
        relacion.get("Id"): relacion.get("Target", "")
        for relacion in relaciones.iter(f"{{{_NS_PAQUETE}}}Relationship")
    }

    hojas: list[tuple[str, str]] = []
    for hoja in libro.iter(f"{{{_NS_HOJA}}}sheet"):
        destino = destinos.get(hoja.get(f"{{{_NS_REL}}}id"), "")
        if not destino:
            continue
        if destino.startswith("/"):
            ruta = destino.lstrip("/")
        else:
            ruta = "xl/" + destino.lstrip("./")
        hojas.append((hoja.get("name", "Hoja"), ruta))
    return hojas


def _valor_celda_xlsx(celda, cadenas: list[str], fechas: list[bool]) -> str:
    """Texto visible de una celda: números, fechas, fórmulas o texto."""
    tipo = celda.get("t", "")
    if tipo == "inlineStr":
        return "".join(nodo.text or "" for nodo in celda.iter(f"{{{_NS_HOJA}}}t"))

    valor = celda.find(f"{{{_NS_HOJA}}}v")
    texto = valor.text if valor is not None and valor.text is not None else ""

    if tipo == "s":
        try:
            return cadenas[int(texto)]
        except (ValueError, IndexError):
            return ""
    if tipo == "b":
        return "FALSO" if texto.strip() in ("", "0") else "VERDADERO"
    if tipo in {"str", "e", "d"}:
        return texto
    if not texto:
        return ""

    try:
        numero = float(texto)
    except ValueError:
        return texto
    try:
        indice = int(celda.get("s", "0"))
    except ValueError:
        indice = 0
    if 0 <= indice < len(fechas) and fechas[indice]:
        return _fecha_excel(numero)
    if numero.is_integer():
        return str(int(numero))
    return format(numero, ".10g")


def _texto_de_xlsx(datos: bytes) -> str | None:
    """
    Extrae las hojas de un libro `.xlsx` como tablas de texto.

    Un `.xlsx` es un ZIP con XML (OOXML); se lee con ``zipfile`` y
    ``ElementTree`` sin instalar nada. Cada fila queda con las celdas
    separadas por `` | `` y cada hoja con su nombre, así el modelo entiende
    la estructura de la planilla. Las fechas reales de Excel se convierten
    a DD/MM/AAAA y los importes conservan su valor numérico.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as paquete:
            cadenas = _cadenas_xlsx(paquete)
            fechas = _estilos_xlsx(paquete)
            hojas = _hojas_xlsx(paquete)
            if not hojas:
                return None

            partes: list[str] = []
            for nombre, ruta in hojas:
                try:
                    raiz = ET.fromstring(paquete.read(ruta))
                except (KeyError, ET.ParseError):
                    continue

                filas: list[str] = []
                for fila in raiz.iter(f"{{{_NS_HOJA}}}row"):
                    valores: dict[int, str] = {}
                    maximo = 0
                    for celda in fila.findall(f"{{{_NS_HOJA}}}c"):
                        columna = _columna_de_referencia(celda.get("r", ""))
                        if not columna:
                            continue
                        valores[columna] = _valor_celda_xlsx(celda, cadenas, fechas)
                        maximo = max(maximo, columna)
                    if not valores:
                        continue
                    celdas = [valores.get(i, "") for i in range(1, maximo + 1)]
                    if any(celda.strip() for celda in celdas):
                        filas.append(" | ".join(celdas).rstrip())

                if filas:
                    partes.append(f"### Hoja: {nombre}\n" + "\n".join(filas))

            if not partes:
                return None
            return _recortar_texto("\n\n".join(partes))
    except (zipfile.BadZipFile, OSError):
        return None


# ---------------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------------

def _texto_de_docx(datos: bytes) -> str | None:
    """
    Extrae párrafos y tablas de un documento Word `.docx`.

    También es un ZIP con XML: ``word/document.xml`` guarda el cuerpo. Se
    conservan los saltos de párrafo y las celdas de tabla (separadas por
    `` | ``), que es justo lo que hace falta para leer un enunciado.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as paquete:
            xml = paquete.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        return None

    try:
        raiz = ET.fromstring(xml)
    except ET.ParseError:
        return None

    W = f"{{{_NS_WORD}}}"

    def texto_parrafo(parrafo) -> str:
        partes: list[str] = []
        for nodo in parrafo.iter():
            if nodo.tag == f"{W}t":
                partes.append(nodo.text or "")
            elif nodo.tag == f"{W}tab":
                partes.append("\t")
            elif nodo.tag in {f"{W}br", f"{W}cr"}:
                partes.append("\n")
        return "".join(partes)

    lineas: list[str] = []

    def recorrer(elemento) -> None:
        for hijo in elemento:
            if hijo.tag == f"{W}p":
                lineas.append(texto_parrafo(hijo))
            elif hijo.tag == f"{W}tbl":
                for fila in hijo.findall(f"{W}tr"):
                    celdas: list[str] = []
                    for celda in fila.findall(f"{W}tc"):
                        textos = [
                            texto_parrafo(parrafo).strip()
                            for parrafo in celda.findall(f"{W}p")
                        ]
                        celdas.append(" ".join(t for t in textos if t))
                    lineas.append(" | ".join(celdas))
            elif hijo.tag in {
                f"{W}sdt", f"{W}sdtContent", f"{W}txbxContent", f"{W}tc"
            }:
                recorrer(hijo)

    cuerpo = raiz.find(f"{W}body")
    recorrer(cuerpo if cuerpo is not None else raiz)

    texto = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    return _recortar_texto(texto) if texto else None


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _tokens_pdf(flujo: bytes):
    """
    Recorre un flujo de contenido PDF y devuelve sus tokens.

    Produce ``(tipo, valor)`` con tipo ``cadena``, ``hex``, ``numero``,
    ``nombre`` u ``op``. Las cadenas entre paréntesis se desescapan
    (incluidos los octales) y los arreglos ``[ ... ]`` se manejan como
    operadores para que el lector de ``TJ`` los pueda agrupar.
    """
    i = 0
    largo = len(flujo)
    espacios = b" \t\r\n\f\x00"
    delimitadores = b" \t\r\n\f/[]<>()%{}"

    while i < largo:
        caracter = flujo[i:i + 1]
        if caracter in [bytes([b]) for b in espacios]:
            i += 1
            continue
        if caracter == b"%":
            salto = flujo.find(b"\n", i)
            i = largo if salto < 0 else salto + 1
            continue

        if caracter == b"(":
            i += 1
            profundidad = 1
            partes = bytearray()
            escapes = {
                ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8,
                ord("f"): 12, ord("("): 40, ord(")"): 41, ord("\\"): 92,
            }
            while i < largo and profundidad:
                byte = flujo[i]
                if byte == 0x5C:  # barra invertida
                    i += 1
                    if i >= largo:
                        break
                    escape = flujo[i]
                    if escape in escapes:
                        partes.append(escapes[escape])
                        i += 1
                    elif 48 <= escape <= 55:  # octal \ddd
                        octal = bytearray()
                        for _ in range(3):
                            if i < largo and 48 <= flujo[i] <= 55:
                                octal.append(flujo[i])
                                i += 1
                            else:
                                break
                        partes.append(int(octal, 8) & 0xFF)
                    elif escape == 0x0A:
                        i += 1
                    elif escape == 0x0D:
                        i += 1
                        if i < largo and flujo[i] == 0x0A:
                            i += 1
                    else:
                        partes.append(escape)
                        i += 1
                elif byte == 0x28:  # (
                    profundidad += 1
                    partes.append(byte)
                    i += 1
                elif byte == 0x29:  # )
                    profundidad -= 1
                    if profundidad:
                        partes.append(byte)
                    i += 1
                else:
                    partes.append(byte)
                    i += 1
            yield ("cadena", bytes(partes))
            continue

        if caracter == b"<":
            if flujo[i:i + 2] == b"<<":
                yield ("op", b"<<")
                i += 2
                continue
            fin = flujo.find(b">", i + 1)
            if fin < 0:
                break
            hexa = re.sub(rb"\s", b"", flujo[i + 1:fin])
            yield ("hex", hexa)
            i = fin + 1
            continue

        if caracter == b">" and flujo[i:i + 2] == b">>":
            yield ("op", b">>")
            i += 2
            continue
        if caracter == b"[":
            yield ("op", b"[")
            i += 1
            continue
        if caracter == b"]":
            yield ("op", b"]")
            i += 1
            continue
        if caracter == b"/":
            fin = i + 1
            while fin < largo and flujo[fin:fin + 1] not in delimitadores:
                fin += 1
            yield ("nombre", flujo[i + 1:fin])
            i = fin
            continue

        fin = i
        while fin < largo and flujo[fin:fin + 1] not in delimitadores:
            fin += 1
        if fin == i:
            i += 1
            continue
        palabra = flujo[i:fin]
        if re.fullmatch(rb"[+-]?(?:\d+\.?\d*|\.\d+)", palabra):
            try:
                yield ("numero", float(palabra))
            except ValueError:
                yield ("op", palabra)
        else:
            yield ("op", palabra)
        i = fin


def _pdf_parece_texto(texto: str) -> bool:
    """
    Heurística de calidad para saber si el PDF se extrajo de verdad.

    Los PDF con fuentes incrustadas sin mapa Unicode producen bytes que se
    decodifican a símbolos sin espacios. En ese caso conviene mandar el PDF
    a Gemini (que lo lee con visión) en lugar de pasarle basura al modelo.
    """
    if len(texto) < 60:
        return False
    if texto.count(" ") < max(3, len(texto) // 80):
        return False
    comunes = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        "áéíóúÁÉÍÓÚñÑüÜ .,;:()[]%/-_'\"\n\t\r¿?¡!°ºª$@#&*+=<>"
    )
    visibles = sum(1 for caracter in texto if not caracter.isspace())
    if not visibles:
        return False
    return sum(1 for caracter in texto if caracter in comunes) / visibles >= 0.88


def _texto_de_pdf(datos: bytes) -> str | None:
    """
    Extrae el texto de un PDF con la biblioteca estándar.

    No es un visor completo: recorre los objetos, descomprime los flujos
    ``FlateDecode`` y ejecuta los operadores de texto (``Tj``, ``TJ``,
    ``'``, ``"``). Si el PDF trae mapas ``ToUnicode`` se usan para
    decodificar las fuentes incrustadas; si no, se prueba con UTF-8 y
    Windows-1252. Devuelve ``None`` cuando no hay texto aprovechable (por
    ejemplo, un escaneo), y en ese caso el archivo se manda como documento
    a Gemini.
    """
    objetos: dict[int, bytes] = {}
    for coincidencia in re.finditer(
        rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", datos, re.S
    ):
        try:
            objetos[int(coincidencia.group(1))] = coincidencia.group(3)
        except ValueError:
            continue
    if not objetos:
        return None

    def flujo(cuerpo: bytes) -> bytes | None:
        marca = re.search(rb"stream\r?\n", cuerpo)
        if marca is None:
            return None
        inicio = marca.end()
        fin = cuerpo.find(b"endstream", inicio)
        if fin < 0:
            return None
        crudo = cuerpo[inicio:fin]
        if crudo.endswith(b"\r\n"):
            return crudo[:-2]
        if crudo.endswith(b"\n"):
            return crudo[:-1]
        return crudo

    def descomprimir(cuerpo: bytes) -> bytes | None:
        crudo = flujo(cuerpo)
        if crudo is None:
            return None
        cabecera = cuerpo[: cuerpo.find(b"stream")]
        filtros = re.findall(rb"/([A-Za-z0-9]+Decode)", cabecera)
        salida = crudo
        for filtro in reversed(filtros):
            if filtro == b"FlateDecode":
                try:
                    salida = zlib.decompress(salida)
                except zlib.error:
                    try:
                        salida = zlib.decompress(salida, -15)
                    except zlib.error:
                        return None
            elif filtro == b"ASCII85Decode":
                try:
                    limpio = re.sub(rb"\s", b"", salida).strip(b"~>")
                    salida = base64.a85decode(limpio)
                except Exception:  # noqa: BLE001 - formato inválido
                    return None
            elif filtro == b"ASCIIHexDecode":
                try:
                    limpio = re.sub(rb"[^0-9A-Fa-f]", b"", salida.split(b">")[0])
                    if len(limpio) % 2:
                        limpio += b"0"
                    salida = bytes.fromhex(limpio.decode("ascii"))
                except Exception:  # noqa: BLE001 - formato inválido
                    return None
            else:
                return None
        return salida

    def texto_de_hex(hexa: bytes) -> str:
        try:
            crudo = bytes.fromhex(hexa.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return ""
        if crudo.startswith(b"\xfe\xff"):
            return crudo[2:].decode("utf-16-be", "replace")
        if len(crudo) % 2 == 0 and crudo and crudo[0] == 0:
            return crudo.decode("utf-16-be", "replace")
        return crudo.decode("cp1252", "replace")

    def mapa_unicode(numero: int) -> dict | None:
        cuerpo = objetos.get(numero)
        if cuerpo is None:
            return None
        crudo = descomprimir(cuerpo)
        if crudo is None:
            return None
        contenido = crudo.decode("latin-1", "replace")
        mapa: dict[bytes, str] = {}

        def agregar_rango(lo_hex: str, hi_hex: str, base_hex: str) -> None:
            if len(lo_hex) % 2 or len(hi_hex) % 2 or len(base_hex) % 2:
                return
            try:
                lo = int(lo_hex, 16)
                hi = int(hi_hex, 16)
                base = int(base_hex, 16)
            except ValueError:
                return
            if hi < lo or hi - lo > 4096:
                return
            largo = len(lo_hex) // 2
            largo_destino = len(base_hex) // 2
            for desplazamiento in range(hi - lo + 1):
                clave = (lo + desplazamiento).to_bytes(largo, "big")
                destino = (base + desplazamiento).to_bytes(largo_destino, "big")
                valor = texto_de_hex(destino.hex().encode("ascii"))
                if valor:
                    mapa[clave] = valor

        for bloque in re.findall(r"beginbfchar(.*?)endbfchar", contenido, re.S):
            for origen, destino in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", bloque
            ):
                if len(origen) % 2:
                    continue
                valor = texto_de_hex(destino.encode("ascii"))
                if valor:
                    mapa[bytes.fromhex(origen)] = valor

        patron = re.compile(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*"
            r"(?:<([0-9A-Fa-f]+)>|\[(.*?)\])",
            re.S,
        )
        for bloque in re.findall(r"beginbfrange(.*?)endbfrange", contenido, re.S):
            for coincidencia in patron.finditer(bloque):
                lo, hi, destino, arreglo = coincidencia.groups()
                if arreglo is not None:
                    if len(lo) % 2:
                        continue
                    try:
                        inicio = int(lo, 16)
                    except ValueError:
                        continue
                    for desplazamiento, hexa in enumerate(
                        re.findall(r"<([0-9A-Fa-f]+)>", arreglo)
                    ):
                        clave = (inicio + desplazamiento).to_bytes(
                            len(lo) // 2, "big"
                        )
                        valor = texto_de_hex(hexa.encode("ascii"))
                        if valor:
                            mapa[clave] = valor
                else:
                    agregar_rango(lo, hi, destino)

        if not mapa:
            return None
        largos = {len(clave) for clave in mapa}
        return {"mapa": mapa, "largo": 2 if 2 in largos else 1}

    def cmap_de_fuente(numero: int) -> dict | None:
        cuerpo = objetos.get(numero)
        if cuerpo is None:
            return None
        referencia = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", cuerpo)
        if referencia is None:
            return None
        return mapa_unicode(int(referencia.group(1)))

    # Nombre de recurso (/F1, /F2…) -> mapa Unicode de su fuente.
    fuentes: dict[str, dict | None] = {}
    for cuerpo in objetos.values():
        recurso = re.search(rb"/Font\s*<<(.*?)>>", cuerpo, re.S)
        if recurso is None:
            continue
        for nombre, numero in re.findall(
            rb"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+0\s+R", recurso.group(1)
        ):
            fuentes[nombre.decode("latin-1")] = cmap_de_fuente(int(numero))

    # Si no se pudo mapear el recurso, un único ToUnicode global sirve igual.
    sueltos = [
        cmap for numero in objetos if (cmap := cmap_de_fuente(numero)) is not None
    ]
    cmap_unico = sueltos[0] if len(sueltos) == 1 else None

    def decodificar(crudo: bytes) -> str:
        if not crudo:
            return ""
        try:
            return crudo.decode("utf-8")
        except UnicodeDecodeError:
            return crudo.decode("cp1252", "replace")

    def con_mapa(crudo: bytes, mapa: dict | None) -> str:
        if not mapa:
            return decodificar(crudo)
        largo = mapa["largo"]
        salida: list[str] = []
        for inicio in range(0, len(crudo) - largo + 1, largo):
            clave = crudo[inicio:inicio + largo]
            salida.append(mapa["mapa"].get(clave) or decodificar(clave))
        resto = len(crudo) % largo
        if resto:
            salida.append(decodificar(crudo[-resto:]))
        return "".join(salida)

    lineas: list[str] = []
    actual: list[str] = []

    def cerrar_linea() -> None:
        if not actual:
            return
        texto = re.sub(r"[ \t]+", " ", "".join(actual)).strip()
        if texto:
            lineas.append(texto)
        actual.clear()

    for cuerpo in objetos.values():
        if b"stream" not in cuerpo:
            continue
        cabecera = cuerpo[: cuerpo.find(b"stream")]
        if b"/Image" in cabecera:
            continue
        contenido = descomprimir(cuerpo)
        if not contenido:
            continue
        if b"BT" not in contenido or (
            b"Tj" not in contenido and b"TJ" not in contenido
        ):
            continue
        contenido = re.sub(rb"\bBI\b.*?\bEI\b", b" ", contenido, flags=re.S)

        operandos: list[tuple[str, object]] = []
        arreglos: list[list] = []
        mapa_actual: dict | None = None

        def agregar(elemento: tuple[str, object]) -> None:
            (arreglos[-1] if arreglos else operandos).append(elemento)

        for tipo, valor in _tokens_pdf(contenido):
            if tipo == "op" and valor == b"[":
                arreglos.append([])
                continue
            if tipo == "op" and valor == b"]":
                if arreglos:
                    agregar(("arreglo", arreglos.pop()))
                continue
            if tipo != "op":
                agregar((tipo, valor))
                continue

            if valor == b"Tf":
                nombres = [dato for clase, dato in operandos if clase == "nombre"]
                if nombres:
                    nombre = nombres[-1].decode("latin-1")
                    mapa_actual = fuentes.get(nombre) or cmap_unico
                operandos.clear()
            elif valor in (b"Tj", b"TJ"):
                if valor == b"Tj":
                    if operandos and operandos[-1][0] in {"cadena", "hex"}:
                        actual.append(con_mapa(operandos[-1][1], mapa_actual))
                else:
                    if operandos and operandos[-1][0] == "arreglo":
                        for clase, dato in operandos[-1][1]:
                            if clase in {"cadena", "hex"}:
                                actual.append(con_mapa(dato, mapa_actual))
                            elif clase == "numero" and dato <= -100:
                                if actual and not actual[-1].endswith(" "):
                                    actual.append(" ")
                operandos.clear()
            elif valor in (b"'", b'"'):
                cerrar_linea()
                cadena = next(
                    (dato for clase, dato in reversed(operandos)
                     if clase in {"cadena", "hex"}),
                    None,
                )
                if cadena is not None:
                    actual.append(con_mapa(cadena, mapa_actual))
                operandos.clear()
            elif valor in (b"ET", b"T*"):
                cerrar_linea()
                operandos.clear()
            elif valor in (b"Td", b"TD"):
                numeros = [dato for clase, dato in operandos if clase == "numero"]
                if len(numeros) >= 2 and abs(numeros[-1]) > 0.01:
                    cerrar_linea()
                operandos.clear()
            else:
                operandos.clear()

    cerrar_linea()
    texto = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    if not texto or len(texto) < 30:
        return None
    return _recortar_texto(texto)


# ---------------------------------------------------------------------------
# Despacho por tipo de archivo
# ---------------------------------------------------------------------------

def _texto_de_archivo(archivo: dict, datos: bytes) -> str | None:
    """
    Extrae el texto de un archivo, si es de un tipo que se pueda leer.

    Devuelve ``None`` cuando el archivo no es texto (por ejemplo una imagen):
    en ese caso se manda como imagen al modelo, no como texto.
    """
    nombre = str(archivo.get("nombre", "")).lower()
    tipo = str(archivo.get("tipo", "")).lower()

    if tipo.startswith("image/"):
        return None

    if nombre.endswith(".pdf") or tipo == "application/pdf":
        return _texto_de_pdf(datos)
    if nombre.endswith(".xlsx") or tipo == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ):
        return _texto_de_xlsx(datos)
    if nombre.endswith(".docx") or tipo == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        return _texto_de_docx(datos)

    extensiones_texto = (".txt", ".csv", ".json", ".md", ".log", ".tsv", ".xml")
    if not (tipo.startswith("text/") or tipo in {"application/json", "application/csv"}
            or nombre.endswith(extensiones_texto)):
        return None

    for codificacion in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            texto = datos.decode(codificacion)
            # Un tope generoso: el archivo entero no debería viajar en cada
            # consulta. Si es más largo, se recorta y se avisa al modelo.
            return _recortar_texto(texto)
        except UnicodeDecodeError:
            continue
    return None


def _bloque_archivos(archivos: list[dict] | None) -> tuple[str, list[dict]]:
    """
    Separa los adjuntos en texto y en piezas multimodales.

    Devuelve ``(texto_para_el_prompt, adjuntos)``. El texto se agrega al
    mensaje del usuario con una cabecera por archivo, así el modelo sabe de
    dónde salió cada cosa. Las imágenes y los PDF que no se pudieron
    extraer se devuelven aparte para mandarlos con visión; el resto de los
    formatos (Word, Excel, CSV…) ya viajan como texto.
    """
    if not archivos:
        return "", []

    partes_texto: list[str] = []
    adjuntos: list[dict] = []

    for archivo in archivos:
        if not isinstance(archivo, dict):
            continue
        datos = _decodificar_archivo(archivo)
        nombre = str(archivo.get("nombre", "archivo"))
        nombre_lower = nombre.lower()
        tipo = str(archivo.get("tipo", "")).lower()

        if tipo.startswith("image/"):
            adjuntos.append({
                "tipo": "image",
                "mime": tipo or "image/png",
                "contenido": archivo.get("contenido", ""),
            })
            continue

        if nombre_lower.endswith(_EXTENSIONES_BINARIAS):
            partes_texto.append(
                f"### Archivo adjunto: {nombre}\n"
                "(No puedo leer este formato antiguo. Guardalo como .docx, "
                ".xlsx o PDF con texto seleccionable, o pasalo como imagen.)"
            )
            continue

        texto = _texto_de_archivo(archivo, datos)
        es_pdf = nombre_lower.endswith(".pdf") or tipo == "application/pdf"

        if es_pdf and not _pdf_parece_texto(texto or ""):
            # Un PDF escaneado o con fuentes sin mapa Unicode: Gemini lo lee
            # como documento (visión) si hay clave configurada.
            if os.environ.get("GEMINI_API_KEY", "").strip():
                adjuntos.append({
                    "tipo": "document",
                    "mime": "application/pdf",
                    "contenido": archivo.get("contenido", ""),
                })
                if texto:
                    partes_texto.append(
                        f"### Archivo adjunto: {nombre}\n"
                        "(El texto extraído es incompleto; se adjunta el PDF "
                        "para que lo leas entero.)\n```\n"
                        f"{_recortar_texto(texto)}\n```"
                    )
                else:
                    partes_texto.append(
                        f"### Archivo adjunto: {nombre}\n"
                        "(Se adjunta el PDF como documento para que lo leas.)"
                    )
                continue
            if texto:
                partes_texto.append(
                    f"### Archivo adjunto: {nombre}\n"
                    "(Texto extraído automáticamente; puede tener errores.)\n"
                    f"```\n{_recortar_texto(texto)}\n```"
                )
            else:
                partes_texto.append(
                    f"### Archivo adjunto: {nombre}\n"
                    "(No pude extraer texto de este PDF. Pasalo como imagen o "
                    "guardalo con texto seleccionable.)"
                )
            continue

        if texto is None:
            partes_texto.append(
                f"### Archivo adjunto: {nombre}\n"
                f"(No pude leer este archivo: el formato «{tipo or 'desconocido'}» "
                "no se puede procesar. Si necesitás que lo analice, pasalo como "
                "imagen, CSV, TXT, PDF, Word o Excel .xlsx.)"
            )
            continue

        partes_texto.append(
            f"### Archivo adjunto: {nombre}\n```\n{texto}\n```"
        )

    return "\n\n".join(partes_texto), adjuntos


def _bloque_cuentas(cuentas: list[dict] | None) -> str:
    """
    Arma el plan de cuentas que se le pasa al modelo como contexto.

    Sin esto, el modelo inventaría códigos y el CSV de asientos no se podría
    importar. Se listan solo las cuentas de detalle activas (las que reciben
    movimientos), con su código exacto y su nombre.
    """
    filas: list[str] = []
    for cuenta in (cuentas or [])[:400]:
        codigo = str(cuenta.get("codigo") or "").strip()
        nombre = str(cuenta.get("nombre") or "").strip()
        if codigo and nombre:
            filas.append(f"{codigo} | {nombre}")
    if not filas:
        return ""
    return _recortar_texto(
        "PLAN DE CUENTAS DEL SISTEMA (usá estos códigos exactos para los "
        "asientos; no inventes otros):\n" + "\n".join(filas)
    )


#: Campos que tiene que mencionar la cabecera del CSV de asientos.
_CAMPOS_CSV_ASIENTOS = ("asiento", "fecha", "concepto", "cuenta", "debe", "haber")


def _csv_de_respuesta(texto: str) -> str | None:
    """
    Busca en la respuesta un bloque CSV importable de asientos.

    El prompt le pide al modelo que cierre con un bloque ```csv`` con la
    cabecera ``asiento;fecha;concepto;cuenta;detalle;debe;haber``. Acá se
    extrae ese bloque tal cual para que la interfaz pueda validarlo y
    cargarlo con el importador existente.
    """
    for bloque in re.findall(
        r"```[ \t]*[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)```", str(texto or ""), re.S
    ):
        lineas = [linea.strip() for linea in bloque.strip().splitlines()
                  if linea.strip()]
        if not lineas:
            continue
        cabecera = lineas[0].lower()
        if all(campo in cabecera for campo in _CAMPOS_CSV_ASIENTOS):
            return "\n".join(lineas)

    # Sin cercas de Markdown: se acepta también el bloque a texto plano.
    lineas = str(texto or "").splitlines()
    for indice, linea in enumerate(lineas):
        if not all(campo in linea.lower() for campo in _CAMPOS_CSV_ASIENTOS):
            continue
        bloque = []
        for siguiente in lineas[indice:]:
            if not siguiente.strip():
                break
            bloque.append(siguiente.strip())
        if len(bloque) >= 2:
            return "\n".join(bloque)
        break
    return None


def preguntar(
    pregunta: str,
    historial: list[dict] | None = None,
    archivos: list[dict] | None = None,
    cuentas: list[dict] | None = None,
) -> dict:
    """
    Envía la consulta al asistente e intenta los proveedores en orden.

    ``archivos`` es una lista de adjuntos ``{nombre, tipo, contenido}``, con el
    contenido en base64: imágenes (capturas, fotos de balances), PDF, Word
    (.docx), Excel (.xlsx), CSV, TXT y JSON. Las imágenes viajan al modelo con
    visión; el texto se agrega al mensaje como contexto. Los PDF que no se
    pueden extraer se mandan como documento a Gemini.

    ``cuentas`` es el plan de cuentas del sistema. Si se pasa, el modelo
    recibe los códigos exactos para que el CSV de asientos sea importable.

    Devuelve ``{respuesta, asientos_csv, proveedor, modelo, tiempo_ms,
    intentos}``. ``asientos_csv`` es el bloque CSV propuesto (o ``None``), y
    ``intentos`` lista qué probó y qué pasó en cada caso, para poder mostrar
    al usuario que hubo un respaldo.

    Lanza ``ErrorAsistente`` si no hay ninguna clave configurada o si
    fallan todos los proveedores.
    """
    pregunta = (pregunta or "").strip()
    texto_archivos, adjuntos = _bloque_archivos(archivos)
    imagenes = [a for a in adjuntos if a["tipo"] == "image"]
    documentos = [a for a in adjuntos if a["tipo"] == "document"]
    if not pregunta and not texto_archivos and not adjuntos:
        raise ErrorAsistente("La pregunta no puede estar vacía.")
    if len(pregunta) > 4000:
        raise ErrorAsistente("La pregunta es demasiado larga (máximo 4000 caracteres).")

    mensajes: list[dict] = [{"role": "system", "content": INSTRUCCION_SISTEMA}]
    plan = _bloque_cuentas(cuentas)
    if plan:
        mensajes.append({"role": "system", "content": plan})
    for mensaje in (historial or [])[-10:]:
        if mensaje.get("role") in {"user", "assistant"} and mensaje.get("content"):
            mensajes.append(
                {"role": mensaje["role"], "content": str(mensaje["content"])[:4000]}
            )

    # El mensaje del usuario se arma con la pregunta y los archivos de texto.
    # Si además hay imágenes o PDFs, se usa el formato multimodal: una lista
    # de partes, donde cada archivo viaja en base64.
    cuerpo = pregunta or "Analizá el archivo adjunto."
    if texto_archivos:
        cuerpo = f"{cuerpo}\n\n{texto_archivos}"
    if imagenes or documentos:
        partes: list[dict] = [{"type": "text", "text": cuerpo}]
        for imagen in imagenes:
            partes.append({
                "type": "image",
                "mimeType": imagen["mime"],
                "data": imagen["contenido"],
            })
        for documento in documentos:
            partes.append({
                "type": "document",
                "mimeType": documento["mime"],
                "data": documento["contenido"],
            })
        mensajes.append({"role": "user", "content": partes})
    else:
        mensajes.append({"role": "user", "content": cuerpo})

    intentos: list[dict] = []
    empezado = time.monotonic()

    for proveedor in PROVEEDORES:
        # Groq no procesa documentos PDF: si hay uno, se salta directo al
        # respaldo de Gemini, que sí los lee con visión.
        if documentos and proveedor["nombre"] == "groq":
            intentos.append({
                "proveedor": proveedor["nombre"],
                "estado": "omitido",
                "detalle": "el PDF se analiza con Gemini (documento con visión)",
            })
            continue

        clave = os.environ.get(proveedor["clave"], "").strip()
        if not clave:
            intentos.append({
                "proveedor": proveedor["nombre"],
                "estado": "omitido",
                "detalle": f"falta la variable {proveedor['clave']}",
            })
            continue

        llamador = _LLAMADORES[proveedor["nombre"]]
        try:
            r = llamador(proveedor, clave, mensajes)
        except Exception as exc:  # noqa: BLE001 - ése es justamente el failover
            intentos.append({
                "proveedor": proveedor["nombre"],
                "estado": "falló",
                "detalle": _detalle_fallo(exc),
            })
            continue

        intentos.append({
            "proveedor": proveedor["nombre"],
            "estado": "respondió",
            "detalle": r["modelo"],
        })
        return {
            "respuesta": r["texto"],
            "asientos_csv": _csv_de_respuesta(r["texto"]),
            "proveedor": proveedor["nombre"],
            "modelo": r["modelo"],
            "tiempo_ms": int((time.monotonic() - empezado) * 1000),
            "intentos": intentos,
        }

    fallidos = [i["detalle"] for i in intentos if i["estado"] == "falló"]
    if not any(i["estado"] == "respondió" for i in intentos) and not fallidos:
        raise ErrorAsistente(
            "El asistente no está configurado: faltan las claves de API en "
            "GROQ_API_KEY y GEMINI_API_KEY."
        )
    raise ErrorAsistente(
        "El asistente no está disponible: fallaron todos los proveedores. "
        + " | ".join(fallidos)
    )


def _detalle_fallo(exc: Exception) -> str:
    """Texto corto y legible para el registro de intentos."""
    if isinstance(exc, urllib.error.HTTPError):
        cuerpo = ""
        try:
            cuerpo = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - no importa el detalle
            pass
        return f"HTTP {exc.code}: {cuerpo or exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"sin conexión: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return "tiempo de espera agotado"
    return f"{type(exc).__name__}: {exc}"
