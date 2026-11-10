"""
Módulo: excel
=============

Exportación del trabajo contable a **Excel (.xlsx)** — sin dependencias.

Un archivo `.xlsx` es, en realidad, un ZIP con documentos XML adentro
(la especificación OOXML). Como el proyecto se mantiene dentro de la
biblioteca estándar de Python —no hay ``pip`` en el entorno de despliegue—
este módulo escribe esas partes a mano con ``zipfile`` y genera un libro
perfectamente válido para Excel, LibreOffice y Google Sheets.

Contenido del libro generado
----------------------------

Una hoja por reporte, las tres etapas del enunciado en un solo archivo:

1. **Libro Diario** — cada partida de los asientos contabilizados, con
   sus totales (sección 14).
2. **Libro Mayor** — el movimiento cuenta por cuenta con el saldo
   acumulado, calculado del Diario (sección 15).
3. **Balance** — balance de comprobación con subtotales por
   clasificación (sección 16 y control 6).

Los importes se escriben como **números reales** de Excel (no como
texto): el profesor puede sumar, filtrar y ordenar las columnas igual
que en una planilla hecha a mano. Los centavos enteros del dominio se
convierten con ``Decimal`` recién al serializar, así el archivo muestra
exactamente ``1.500.000,00`` según el formato de celda ``#,##0.00``.

Como en el resto del ERP, el Mayor y el Balance no se cargan: se leen de
los mismos módulos que usan la interfaz, con lo que el Excel refleja
exactamente lo contabilizado (controles 4 y 5).

Uso::

    from erp import excel
    nombre, contenido = excel.generar()            # bytes del .xlsx
    Path(nombre).write_bytes(contenido)

    # Con un período acotado y otra base de datos:
    excel.generar("mi_base.db", desde="2026-04-01", hasta="2026-04-30")
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from . import balance as modulo_balance
from . import base_datos, libro_diario, libro_mayor
from .dinero import desde_centavos

#: Espacio de nombres de las hojas de cálculo (OOXML).
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

#: Fecha cero de la seriación de fechas de Excel (30/12/1899).
EPOCH_EXCEL = date(1899, 12, 30)

# ---------------------------------------------------------------------------
# Paleta: la misma del tema «WhatsApp» de la interfaz (ver README), para
# que el archivo salga con la identidad del sistema.
# ---------------------------------------------------------------------------

COLOR_TINTA = "FF09090B"        # texto normal
COLOR_PRIMARIO_900 = "FF053B35"  # título
COLOR_PRIMARIO_700 = "FF075E54"  # fondo del encabezado de tabla
COLOR_PRIMARIO_50 = "FFE7F8F0"   # fondo de subtotales y secciones
COLOR_GRIS_500 = "FF6C6C6D"     # subtítulos y notas

# ---------------------------------------------------------------------------
# Índices de estilo (celdas xf de xl/styles.xml).
# El orden es el de la tabla cellXfs: estos números son el `s=` de cada celda.
# ---------------------------------------------------------------------------

ESTILO_TEXTO = 0            # texto normal
ESTILO_TITULO = 1           # razón social, en grande
ESTILO_SUBTITULO = 2        # reporte y período
ESTILO_ENCABEZADO = 3       # encabezado de tabla (verde sólido, centrado)
ESTILO_ENCABEZADO_NUM = 4   # encabezado de columnas numéricas (a la derecha)
ESTILO_TEXTO_NEGRITA = 5    # texto destacado
ESTILO_SECCION = 6          # cabecera de cuenta del Mayor / clasificación
ESTILO_FECHA = 7           # fecha con formato dd/mm/aaaa
ESTILO_ENTERO = 8          # números de asiento, centrados
ESTILO_DINERO = 9           # importes con formato #.##0,00
ESTILO_DINERO_NEGRITA = 10  # importes destacados
ESTILO_DINERO_TOTAL = 11    # importes de totales (fondo suave + borde)
ESTILO_TEXTO_TOTAL = 12     # etiqueta de totales
ESTILO_NOTA = 13            # aclaraciones en itálica gris


# ---------------------------------------------------------------------------
# Escritor mínimo de hojas de cálculo
# ---------------------------------------------------------------------------

def _letras_columna(indice: int) -> str:
    """Convierte un índice de columna (1) en su letra (A). 27 -> AA."""
    letras = ""
    while indice > 0:
        indice, resto = divmod(indice - 1, 26)
        letras = chr(ord("A") + resto) + letras
    return letras


def _texto_xml(texto: str) -> str:
    """Texto seguro para XML: caracteres de control fuera y `&<>` escapados."""
    texto = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", str(texto))
    return escape(texto)


class Hoja:
    """
    Una hoja de cálculo que se arma en memoria y se serializa a XML.

    Las filas se agregan con :meth:`fila` como listas de celdas: cada celda
    es el valor (texto, número, fecha o ``None`` para dejarla vacía) o la
    tupla ``(valor, estilo)``. La posición en la lista define la columna.
    """

    def __init__(self, nombre: str, anchos: dict[int, float] | None = None,
                 congelar: int = 0):
        self.nombre = nombre[:31] or "Hoja"
        self.anchos: dict[int, float] = anchos or {}
        self.congelar = congelar      # filas fijas arriba (encabezado)
        self._filas: list[list[tuple[int, object, int]]] = []
        self._combinadas: list[str] = []

    # -- construcción ------------------------------------------------------

    def fila(self, celdas: list) -> int:
        """
        Agrega una fila y devuelve su número (1-based).

        Cada celda puede ser ``None`` (columna vacía), un valor suelto
        (estilo por defecto) o la tupla ``(valor, estilo)``.
        """
        numero = len(self._filas) + 1
        serializada: list[tuple[int, object, int]] = []
        for columna, celda in enumerate(celdas, start=1):
            if celda is None:
                continue
            if isinstance(celda, tuple):
                valor, estilo = celda
            else:
                valor, estilo = celda, ESTILO_TEXTO
            serializada.append((columna, valor, int(estilo)))
        self._filas.append(serializada)
        return numero

    def combinar(self, fila: int, desde: int, hasta: int) -> None:
        """Combina en una sola celda el rango horizontal de esa fila."""
        self._combinadas.append(
            f"{_letras_columna(desde)}{fila}:{_letras_columna(hasta)}{fila}"
        )

    # -- serialización -----------------------------------------------------

    @staticmethod
    def _celda(columna: int, fila: int, valor, estilo: int) -> str:
        """Serializa una celda según el tipo del valor."""
        ref = f"{_letras_columna(columna)}{fila}"
        s = f' s="{estilo}"' if estilo else ""

        if isinstance(valor, str):
            texto = _texto_xml(valor)
            return (f'<c r="{ref}"{s} t="inlineStr">'
                    f"<is><t xml:space=\"preserve\">{texto}</t></is></c>")

        if isinstance(valor, date):
            serial = (valor - EPOCH_EXCEL).days
            return f'<c r="{ref}"{s}><v>{serial}</v></c>'

        if isinstance(valor, Decimal):
            # Decimal ya viene cuantizado a 2 decimales: escribirlo con
            # format() evita los artefactos binarios de float.
            return f'<c r="{ref}"{s}><v>{format(valor, "f")}</v></c>'

        if isinstance(valor, bool):  # antes que int: bool es subclase
            return f'<c r="{ref}"{s} t="inlineStr"><is><t>{valor}</t></is></c>'

        if isinstance(valor, (int, float)):
            return f'<c r="{ref}"{s}><v>{valor}</v></c>'

        # Cualquier otra cosa, como texto.
        return (f'<c r="{ref}"{s} t="inlineStr">'
                f"<is><t>{_texto_xml(valor)}</t></is></c>")

    def a_xml(self) -> str:
        """La hoja completa como documento XML de worksheet."""
        piezas: list[str] = [
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            f'<worksheet xmlns="{NS}">',
        ]

        if self._filas:
            ultima = len(self._filas)
            ultima_col = max(
                (col for fila in self._filas for col, _, _ in fila), default=1
            )
            piezas.append(
                f'<dimension ref="A1:{_letras_columna(max(ultima_col, 1))}{ultima}"/>'
            )

        # Encabezado congelado: el título de la tabla queda fijo al hacer
        # scroll, como en la interfaz web.
        vistas = f'<sheetView workbookViewId="0" showGridLines="0">'
        if self.congelar:
            corte = self.congelar + 1
            vistas += (f'<pane ySplit="{self.congelar}" topLeftCell="A{corte}" '
                       f'activePane="bottomLeft" state="frozen"/>')
        vistas += "</sheetView>"
        piezas.append(f"<sheetViews>{vistas}</sheetViews>")
        piezas.append('<sheetFormatPr defaultRowHeight="15"/>')

        if self.anchos:
            columnas = "".join(
                f'<col min="{col}" max="{col}" width="{ancho}" customWidth="1"/>'
                for col, ancho in sorted(self.anchos.items())
            )
            piezas.append(f"<cols>{columnas}</cols>")

        filas: list[str] = ['<sheetData>']
        for indice, fila in enumerate(self._filas, start=1):
            celdas = "".join(
                self._celda(columna, indice, valor, estilo)
                for columna, valor, estilo in fila
            )
            filas.append(f'<row r="{indice}">{celdas}</row>')
        filas.append("</sheetData>")
        piezas.append("".join(filas))

        if self._combinadas:
            celdas = "".join(f'<mergeCell ref="{ref}"/>' for ref in self._combinadas)
            piezas.append(f'<mergeCells count="{len(self._combinadas)}">{celdas}'
                          f"</mergeCells>")

        piezas.append("</worksheet>")
        return "".join(piezas)


# ---------------------------------------------------------------------------
# Partes fijas del paquete OOXML
# ---------------------------------------------------------------------------

def _content_types(hojas: int) -> str:
    hojas_xml = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.'
        f'spreadsheetml.worksheet+xml"/>'
        for i in range(1, hojas + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.'
        "openxmlformats-package.relationships+xml\"/>"
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{hojas_xml}</Types>"
    )


def _rels_raiz() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook(hojas: list[Hoja]) -> str:
    hojas_xml = "".join(
        f'<sheet name="{_texto_xml(h.nombre)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, h in enumerate(hojas, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<bookViews><workbookView/></bookViews>"
        f"<sheets>{hojas_xml}</sheets>"
        "</workbook>"
    )


def _workbook_rels(hojas: int) -> str:
    relaciones = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, hojas + 1)
    )
    relaciones += (
        f'<Relationship Id="rId{hojas + 1}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/styles" Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{relaciones}</Relationships>"
    )


def _fuente(*, negrita=False, italica=False, tamano=11, color=COLOR_TINTA) -> str:
    etiquetas = []
    if negrita:
        etiquetas.append("<b/>")
    if italica:
        etiquetas.append("<i/>")
    etiquetas.append(f'<sz val="{tamano}"/>')
    etiquetas.append(f'<color rgb="{color}"/>')
    etiquetas.append('<name val="Calibri"/><family val="2"/>')
    return f"<font>{''.join(etiquetas)}</font>"


def _estilos() -> str:
    """xl/styles.xml: 14 estilos de celda armados sobre 7 fuentes y 4 fondos."""
    fuentes = [
        _fuente(),                                            # 0 normal
        _fuente(negrita=True),                                # 1 negrita
        _fuente(negrita=True, color="FFFFFFFF"),              # 2 negrita blanca
        _fuente(negrita=True, tamano=14, color=COLOR_PRIMARIO_900),  # 3 título
        _fuente(tamano=10, color=COLOR_GRIS_500),             # 4 subtítulo
        _fuente(italica=True, tamano=10, color=COLOR_GRIS_500),  # 5 nota
        _fuente(negrita=True, tamano=12),                     # 6 sección
    ]
    fondos = [
        '<fill><patternFill patternType="none"/></fill>',
        '<fill><patternFill patternType="gray125"/></fill>',
        f'<fill><patternFill patternType="solid">'
        f'<fgColor rgb="{COLOR_PRIMARIO_700}"/><bgColor indexed="64"/>'
        f"</patternFill></fill>",
        f'<fill><patternFill patternType="solid">'
        f'<fgColor rgb="{COLOR_PRIMARIO_50}"/><bgColor indexed="64"/>'
        f"</patternFill></fill>",
    ]
    bordes = [
        "<border><left/><right/><top/><bottom/><diagonal/></border>",
        '<border><left/><right/><top style="thin">'
        f'<color rgb="{COLOR_TINTA}"/></top><bottom/><diagonal/></border>',
    ]

    def xf(*, numfmt=0, fuente=0, fondo=0, borde=0,
           alineacion=None) -> str:
        banderas = (f'applyNumberFormat="1" ' if numfmt else "")
        banderas += (
            f'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"'
        )
        alineacion = alineacion or '<alignment vertical="top"/>'
        return (f'<xf numFmtId="{numfmt}" fontId="{fuente}" fillId="{fondo}" '
                f'borderId="{borde}" xfId="0" {banderas}>{alineacion}</xf>')

    izquierda = '<alignment vertical="top" horizontal="left" wrapText="1"/>'
    centrado = '<alignment vertical="center" horizontal="center" wrapText="1"/>'
    derecha = '<alignment vertical="center" horizontal="right"/>'

    celdas = [
        # 0 texto normal
        xf(alineacion=izquierda),
        # 1 título (razón social)
        xf(fuente=3, alineacion='<alignment vertical="center" horizontal="left"/>'),
        # 2 subtítulo (reporte y período)
        xf(fuente=4, alineacion=izquierda),
        # 3 encabezado de tabla (texto)
        xf(fuente=2, fondo=2, alineacion=centrado),
        # 4 encabezado de tabla (números, a la derecha)
        xf(fuente=2, fondo=2, alineacion=derecha),
        # 5 texto en negrita
        xf(fuente=1, alineacion=izquierda),
        # 6 sección: cabecera de cuenta / clasificación
        xf(fuente=6, fondo=3, alineacion=izquierda),
        # 7 fecha dd/mm/aaaa
        xf(numfmt=165, alineacion=centrado),
        # 8 entero centrado (números de asiento)
        xf(alineacion=centrado),
        # 9 dinero
        xf(numfmt=164, alineacion=derecha),
        # 10 dinero en negrita
        xf(numfmt=164, fuente=1, alineacion=derecha),
        # 11 dinero de totales (fondo suave, borde superior)
        xf(numfmt=164, fuente=1, fondo=3, borde=1, alineacion=derecha),
        # 12 etiqueta de totales
        xf(fuente=1, fondo=3, borde=1, alineacion=izquierda),
        # 13 nota al pie
        xf(fuente=5, alineacion=izquierda),
    ]

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{NS}">'
        '<numFmts count="2">'
        '<numFmt numFmtId="164" formatCode="#,##0.00"/>'
        '<numFmt numFmtId="165" formatCode="dd/mm/yyyy"/>'
        "</numFmts>"
        f'<fonts count="{len(fuentes)}">{"".join(fuentes)}</fonts>'
        f'<fills count="{len(fondos)}">{"".join(fondos)}</fills>'
        f'<borders count="{len(bordes)}">{"".join(bordes)}</borders>'
        '<cellStyleXfs count="1">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        "</cellStyleXfs>"
        f'<cellXfs count="{len(celdas)}">{"".join(celdas)}</cellXfs>'
        '<cellStyles count="1">'
        '<cellStyle name="Normal" xfId="0" builtinId="0"/>'
        "</cellStyles>"
        "</styleSheet>"
    )


def _empaquetar(hojas: list[Hoja]) -> bytes:
    """Arma el ZIP final: el archivo .xlsx listo para descargar."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archivo:
        archivo.writestr("[Content_Types].xml", _content_types(len(hojas)))
        archivo.writestr("_rels/.rels", _rels_raiz())
        archivo.writestr("xl/workbook.xml", _workbook(hojas))
        archivo.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(hojas)))
        archivo.writestr("xl/styles.xml", _estilos())
        for indice, hoja in enumerate(hojas, start=1):
            archivo.writestr(f"xl/worksheets/sheet{indice}.xml", hoja.a_xml())
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Datos del dominio (los mismos módulos que usa la interfaz)
# ---------------------------------------------------------------------------

def _fecha(texto: str) -> date:
    """Convierte 'AAAA-MM-DD' (formato interno) en fecha de calendario."""
    return date.fromisoformat(str(texto)[:10])


def _importe(centavos) -> Decimal | None:
    """
    Centavos enteros -> Decimal en unidades monetarias para la celda.

    Devuelve ``None`` cuando el importe es cero: en el Diario y el Mayor
    la columna contraria queda vacía, como en un libro contable de papel.
    """
    centavos = int(centavos or 0)
    return desde_centavos(centavos) if centavos else None


def _saldo(centavos) -> Decimal:
    """Igual que _importe pero siempre con valor: los saldos muestran 0,00."""
    return desde_centavos(int(centavos or 0))


def _empresa(ruta) -> dict:
    with base_datos.conectar(ruta) as conexion:
        fila = conexion.execute("SELECT * FROM empresa WHERE id = 1").fetchone()
    return dict(fila) if fila else base_datos.empresa_por_defecto()


def _lineas_diario(ruta, desde, hasta) -> tuple[list[dict], int]:
    """
    Las partidas de los asientos CONFIRMADO del período.

    Devuelve ``(lineas, omitidos)``: `omitidos` cuenta los asientos en
    BORRADOR o ANULADO dentro del rango, para aclararlo al pie de la hoja
    (no contabilizan en el Mayor ni en el Balance).
    """
    asientos = libro_diario.listar_asientos(
        ruta, desde=desde, hasta=hasta, estado="CONFIRMADO"
    )
    todos = libro_diario.listar_asientos(ruta, desde=desde, hasta=hasta)
    omitidos = sum(1 for a in todos if a["estado"] != "CONFIRMADO")

    lineas: list[dict] = []
    for cabecera in asientos:
        asiento = libro_diario.obtener_asiento(cabecera["id"], ruta)
        for partida in asiento["partidas"]:
            lineas.append(
                {
                    "numero": asiento["numero"],
                    "fecha": asiento["fecha"],
                    "concepto": asiento["concepto"],
                    "cuenta": partida["cuenta"],
                    "cuenta_nombre": partida["cuenta_nombre"],
                    "detalle": partida.get("detalle") or "",
                    "debe": partida["debe"],
                    "haber": partida["haber"],
                }
            )
    return lineas, omitidos


def _periodo_texto(lineas, desde, hasta) -> str:
    """Texto del período que cubre el reporte, para el encabezado."""
    if desde or hasta:
        desde_txt = _fecha(desde).strftime("%d/%m/%Y") if desde else "inicio"
        hasta_txt = _fecha(hasta).strftime("%d/%m/%Y") if hasta else "hoy"
        return f"período {desde_txt} a {hasta_txt}"
    if not lineas:
        return "sin movimientos contabilizados"
    fechas = sorted(_fecha(linea["fecha"]) for linea in lineas)
    primera = fechas[0].strftime("%d/%m/%Y")
    ultima = fechas[-1].strftime("%d/%m/%Y")
    return f"{primera} a {ultima}" if primera != ultima else primera


# ---------------------------------------------------------------------------
# Encabezado común de las tres hojas
# ---------------------------------------------------------------------------

def _encabezado(hoja: Hoja, empresa: dict, titulo: str, periodo: str,
                columnas: int) -> None:
    """Razón social, reporte y aclaraciones de moneda, como cabecera."""
    fila = hoja.fila([(empresa.get("nombre") or "Empresa sin nombre",
                       ESTILO_TITULO)])
    hoja.combinar(fila, 1, columnas)

    fila = hoja.fila([(f"{titulo} · {periodo}", ESTILO_SUBTITULO)])
    hoja.combinar(fila, 1, columnas)

    detalles = []
    if empresa.get("cuit"):
        detalles.append(f"RUC: {empresa['cuit']}")
    if empresa.get("actividad"):
        detalles.append(str(empresa["actividad"]))
    detalles.append(f"Moneda: {empresa.get('moneda') or 'G.'}")
    fila = hoja.fila([(" · ".join(detalles), ESTILO_NOTA)])
    hoja.combinar(fila, 1, columnas)

    hoja.fila([])  # respiro antes de la tabla


# ---------------------------------------------------------------------------
# Hoja 1: Libro Diario
# ---------------------------------------------------------------------------

def _hoja_diario(empresa, lineas, omitidos, periodo) -> Hoja:
    hoja = Hoja(
        "Libro Diario",
        anchos={1: 6, 2: 12, 3: 48, 4: 11, 5: 34, 6: 30, 7: 16, 8: 16},
        congelar=5,
    )
    _encabezado(hoja, empresa, "Libro Diario", periodo, columnas=8)

    hoja.fila(
        [
            ("N.º", ESTILO_ENCABEZADO),
            ("Fecha", ESTILO_ENCABEZADO),
            ("Concepto", ESTILO_ENCABEZADO),
            ("Código", ESTILO_ENCABEZADO),
            ("Cuenta", ESTILO_ENCABEZADO),
            ("Detalle", ESTILO_ENCABEZADO),
            ("Debe", ESTILO_ENCABEZADO_NUM),
            ("Haber", ESTILO_ENCABEZADO_NUM),
        ]
    )

    for linea in lineas:
        hoja.fila(
            [
                (linea["numero"], ESTILO_ENTERO),
                (_fecha(linea["fecha"]), ESTILO_FECHA),
                linea["concepto"],
                linea["cuenta"],
                linea["cuenta_nombre"],
                linea["detalle"] or None,
                _importe(linea["debe"]),
                _importe(linea["haber"]),
            ]
        )

    total_debe = sum(linea["debe"] for linea in lineas)
    total_haber = sum(linea["haber"] for linea in lineas)
    cuadra = total_debe == total_haber
    fila = hoja.fila(
        [
            ("TOTALES", ESTILO_TEXTO_TOTAL),
            None, None, None, None, None,
            _saldo(total_debe),                   # Debe
            _saldo(total_haber),                  # Haber
        ]
    )
    hoja.combinar(fila, 1, 6)

    verificacion = (
        "Control 1 verificado: total Debe = total Haber."
        if cuadra
        else "ATENCIÓN: el total Debe no es igual al total Haber."
    )
    fila = hoja.fila([(verificacion, ESTILO_NOTA)])
    hoja.combinar(fila, 1, 8)

    if omitidos:
        fila = hoja.fila(
            [(f"Se omiten {omitidos} asiento(s) en BORRADOR o ANULADO: "
              "no contabilizan en el Mayor ni en el Balance.", ESTILO_NOTA)]
        )
        hoja.combinar(fila, 1, 8)

    return hoja


# ---------------------------------------------------------------------------
# Hoja 2: Libro Mayor
# ---------------------------------------------------------------------------

def _hoja_mayor(empresa, mayor, periodo) -> Hoja:
    hoja = Hoja(
        "Libro Mayor",
        anchos={1: 12, 2: 6, 3: 48, 4: 30, 5: 16, 6: 16, 7: 16},
        congelar=5,
    )
    _encabezado(hoja, empresa, "Libro Mayor", periodo, columnas=7)

    hoja.fila(
        [
            ("Fecha", ESTILO_ENCABEZADO),
            ("N.º", ESTILO_ENCABEZADO),
            ("Concepto", ESTILO_ENCABEZADO),
            ("Detalle", ESTILO_ENCABEZADO),
            ("Debe", ESTILO_ENCABEZADO_NUM),
            ("Haber", ESTILO_ENCABEZADO_NUM),
            ("Saldo", ESTILO_ENCABEZADO_NUM),
        ]
    )

    total_debe = 0
    total_haber = 0
    for bloque in mayor["cuentas"]:
        cuenta = bloque["cuenta"]
        fila = hoja.fila(
            [
                (
                    f"{cuenta['codigo']} · {cuenta['nombre']} — "
                    f"{cuenta['clasificacion_nombre']} "
                    f"(naturaleza {cuenta['naturaleza_nombre'].lower()})",
                    ESTILO_SECCION,
                )
            ]
        )
        hoja.combinar(fila, 1, 7)

        for movimiento in bloque["movimientos"]:
            hoja.fila(
                [
                    (_fecha(movimiento["fecha"]), ESTILO_FECHA),
                    (movimiento["numero"], ESTILO_ENTERO),
                    movimiento["concepto"],
                    movimiento.get("detalle") or None,
                    _importe(movimiento["debe"]),
                    _importe(movimiento["haber"]),
                    _saldo(movimiento["saldo"]),
                ]
            )

        totales = bloque["totales"]
        total_debe += totales["debe"]
        total_haber += totales["haber"]
        fila = hoja.fila(
            [
                ("Totales de la cuenta", ESTILO_TEXTO_TOTAL),
                None, None, None,
                _saldo(totales["debe"]),
                _saldo(totales["haber"]),
                _saldo(totales["saldo_final"]),
            ]
        )
        hoja.combinar(fila, 1, 4)
        hoja.fila([])

    if mayor["cuentas"]:
        fila = hoja.fila(
            [
                ("TOTALES DEL MAYOR", ESTILO_TEXTO_TOTAL),
                None, None, None,
                _saldo(total_debe),
                _saldo(total_haber),
                None,
            ]
        )
        hoja.combinar(fila, 1, 4)

    return hoja


# ---------------------------------------------------------------------------
# Hoja 3: Balance de Comprobación
# ---------------------------------------------------------------------------

def _hoja_balance(empresa, balance_comprobacion, periodo) -> Hoja:
    hoja = Hoja(
        "Balance",
        anchos={1: 11, 2: 38, 3: 20, 4: 16, 5: 16, 6: 16, 7: 17},
        congelar=5,
    )
    _encabezado(hoja, empresa, "Balance de Comprobación", periodo, columnas=7)

    hoja.fila(
        [
            ("Código", ESTILO_ENCABEZADO),
            ("Cuenta", ESTILO_ENCABEZADO),
            ("Clasificación", ESTILO_ENCABEZADO),
            ("Débitos", ESTILO_ENCABEZADO_NUM),
            ("Créditos", ESTILO_ENCABEZADO_NUM),
            ("Saldo Deudor", ESTILO_ENCABEZADO_NUM),
            ("Saldo Acreedor", ESTILO_ENCABEZADO_NUM),
        ]
    )

    for cuenta in balance_comprobacion["cuentas"]:
        hoja.fila(
            [
                cuenta["codigo"],
                cuenta["nombre"],
                cuenta["clasificacion_nombre"],
                _importe(cuenta["debe"]),
                _importe(cuenta["haber"]),
                _importe(cuenta["saldo_deudor"]),
                _importe(cuenta["saldo_acreedor"]),
            ]
        )

    totales = balance_comprobacion["totales"]
    fila = hoja.fila(
        [
            ("TOTALES", ESTILO_TEXTO_TOTAL),
            None, None,
            _saldo(totales["debe"]),
            _saldo(totales["haber"]),
            _saldo(totales["saldo_deudor"]),
            _saldo(totales["saldo_acreedor"]),
        ]
    )
    hoja.combinar(fila, 1, 3)

    verificaciones = balance_comprobacion["verificaciones"]
    fila = hoja.fila(
        [
            (
                "Verificaciones: " + verificaciones["debitos_igual_creditos"]["mensaje"]
                + ". " + verificaciones["saldos_iguales"]["mensaje"] + ".",
                ESTILO_NOTA,
            )
        ]
    )
    hoja.combinar(fila, 1, 7)

    # -- Subtotales por clasificación (control 6) --------------------------
    grupos = [
        g for g in balance_comprobacion.get("por_clasificacion", [])
        if g.get("cuentas", 0) > 0
    ]
    if grupos:
        hoja.fila([])
        fila = hoja.fila([("Subtotales por clasificación (control 6)",
                           ESTILO_TEXTO_NEGRITA)])
        hoja.combinar(fila, 1, 7)

        hoja.fila(
            [
                ("Clasificación", ESTILO_ENCABEZADO),
                ("Naturaleza", ESTILO_ENCABEZADO),
                ("Cuentas", ESTILO_ENCABEZADO_NUM),
                ("Débitos", ESTILO_ENCABEZADO_NUM),
                ("Créditos", ESTILO_ENCABEZADO_NUM),
                ("Saldo Deudor", ESTILO_ENCABEZADO_NUM),
                ("Saldo Acreedor", ESTILO_ENCABEZADO_NUM),
            ]
        )
        for grupo in grupos:
            hoja.fila(
                [
                    (grupo["nombre"], ESTILO_SECCION),
                    (grupo["naturaleza_nombre"], ESTILO_SECCION),
                    (grupo["cuentas"], ESTILO_SECCION),
                    _importe(grupo["debe"]),
                    _importe(grupo["haber"]),
                    _importe(grupo["saldo_deudor"]),
                    _importe(grupo["saldo_acreedor"]),
                ]
            )

        fila = hoja.fila(
            [
                ("TOTAL GENERAL", ESTILO_TEXTO_TOTAL),
                None,
                sum(g["cuentas"] for g in grupos),
                _saldo(sum(g["debe"] for g in grupos)),
                _saldo(sum(g["haber"] for g in grupos)),
                _saldo(sum(g["saldo_deudor"] for g in grupos)),
                _saldo(sum(g["saldo_acreedor"] for g in grupos)),
            ]
        )
        hoja.combinar(fila, 1, 2)

    fila = hoja.fila(
        [
            (
                "Balance generado automáticamente con los asientos CONFIRMADOS "
                "del Libro Diario (controles 4 y 5).",
                ESTILO_NOTA,
            )
        ]
    )
    hoja.combinar(fila, 1, 7)

    return hoja


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def _sin_acentos(texto: str) -> str:
    """Quita acentos para armar un nombre de archivo ASCII."""
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _nombre_archivo(empresa: dict) -> str:
    """Nombre sugerido para la descarga, seguro en cualquier sistema."""
    base = _sin_acentos(empresa.get("nombre") or "ERP contable")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-") or "ERP-contable"
    return f"{slug}-diario-mayor-balance-{datetime.now():%Y%m%d-%H%M}.xlsx"


def generar(ruta=None, *, desde=None, hasta=None) -> tuple[str, bytes]:
    """
    Genera el libro Excel con el trabajo resuelto.

    Devuelve ``(nombre_sugerido, contenido)`` donde `contenido` son los
    bytes del `.xlsx` listo para escribir o devolver por HTTP. El libro
    tiene tres hojas —Libro Diario, Libro Mayor y Balance de
    Comprobación— generadas con los asientos CONFIRMADO del período.

    ``desde`` y ``hasta`` (formato ``AAAA-MM-DD``) acotan el período; sin
    ellos se exporta todo lo contabilizado.
    """
    empresa = _empresa(ruta)
    lineas, omitidos = _lineas_diario(ruta, desde, hasta)
    periodo = _periodo_texto(lineas, desde, hasta)

    mayor = libro_mayor.libro_mayor_completo(ruta, desde=desde, hasta=hasta)
    balance_comprobacion = modulo_balance.balance_comprobacion(
        ruta, desde=desde, hasta=hasta
    )

    hojas = [
        _hoja_diario(empresa, lineas, omitidos, periodo),
        _hoja_mayor(empresa, mayor, periodo),
        _hoja_balance(empresa, balance_comprobacion, periodo),
    ]
    return _nombre_archivo(empresa), _empaquetar(hojas)
