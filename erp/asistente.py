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
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

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


def _llamar_groq(proveedor: dict, clave: str, mensajes: list[dict]) -> dict:
    """Respuesta en formato OpenAI (que Groq implementa)."""
    ultimo_error: Exception | None = None

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

    contenido = [
        {"role": "model" if m["role"] == "assistant" else "user",
         "parts": [{"text": m["content"]}]}
        for m in mensajes if m["role"] in {"user", "assistant"}
    ]

    for modelo in proveedor["modelos"]:
        try:
            r = _pedir(
                proveedor["url"].format(modelo=modelo),
                {
                    "contents": contenido,
                    # El prompt de sistema es un systemInstruction aparte.
                    "systemInstruction": {"parts": [{"text": INSTRUCCION_SISTEMA}]},
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


def preguntar(
    pregunta: str,
    historial: list[dict] | None = None,
) -> dict:
    """
    Envía la consulta al asistente e intenta los proveedores en orden.

    Devuelve ``{respuesta, proveedor, modelo, tiempo_ms, intentos}`` donde
    ``intentos`` lista qué probó y qué pasó en cada caso, para poder
    mostrar al usuario que hubo un respaldo.

    Lanza ``ErrorAsistente`` si no hay ninguna clave configurada o si
    fallan todos los proveedores.
    """
    pregunta = (pregunta or "").strip()
    if not pregunta:
        raise ErrorAsistente("La pregunta no puede estar vacía.")
    if len(pregunta) > 4000:
        raise ErrorAsistente("La pregunta es demasiado larga (máximo 4000 caracteres).")

    mensajes: list[dict] = [{"role": "system", "content": INSTRUCCION_SISTEMA}]
    for mensaje in (historial or [])[-10:]:
        if mensaje.get("role") in {"user", "assistant"} and mensaje.get("content"):
            mensajes.append(
                {"role": mensaje["role"], "content": str(mensaje["content"])[:4000]}
            )
    mensajes.append({"role": "user", "content": pregunta})

    intentos: list[dict] = []
    empezado = time.monotonic()

    for proveedor in PROVEEDORES:
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
