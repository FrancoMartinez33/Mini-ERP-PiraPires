#!/usr/bin/env python3
"""
Mini ERP Contable — punto de entrada.

Uso:

    python3 main.py                      arranca en http://127.0.0.1:8000
    python3 main.py --puerto 9000        usa otro puerto
    python3 main.py --datos otra.db      usa otro archivo de base de datos
    python3 main.py --cargar-demo        carga la empresa y las 15 operaciones
    python3 main.py --auto-sembrar       carga la demo solo si no hay asientos
    python3 main.py --reiniciar          vacía la base de datos y sale
    python3 main.py --consola            ejecuta una consulta SQL de lectura

No requiere instalar nada: sólo la biblioteca estándar de Python 3.

En un hosting que inyecta las variables de entorno `HOST` y `PORT` —como
Render— los valores por defecto pasan a ser esos, así que alcanza con
`python3 main.py --auto-sembrar`.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser

from erp import base_datos, datos_demo
from erp.dinero import formato_monto


def _crear_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mini ERP Contable · Etapas 1 a 3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Dirección de escucha (por defecto, la variable HOST o 127.0.0.1)",
    )
    parser.add_argument(
        "--puerto",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Puerto HTTP (por defecto, la variable PORT o 8000)",
    )
    parser.add_argument("--datos", default=None, help="Ruta del archivo de base de datos")
    parser.add_argument(
        "--cargar-demo", action="store_true",
        help="Carga la empresa ficticia con las 15 operaciones de prueba y sale",
    )
    parser.add_argument(
        "--auto-sembrar", action="store_true",
        help=(
            "Al arrancar el servidor, carga los datos de prueba únicamente si "
            "la base todavía no tiene asientos. Es lo que se usa en un hosting "
            "con sistema de archivos efímero: nunca pisa lo que ya hay."
        ),
    )
    parser.add_argument(
        "--reiniciar", action="store_true",
        help="Borra la base de datos (asientos y cuentas) y sale",
    )
    parser.add_argument(
        "--consola", default=None, metavar="SQL",
        help="Ejecuta una consulta SQL de lectura y muestra el resultado",
    )
    parser.add_argument(
        "--abrir", action="store_true", help="Abre el navegador al arrancar"
    )
    return parser


def main() -> int:
    argumentos = _crear_parser().parse_args()

    # -- Modo consola: una consulta SQL de lectura ------------------------
    if argumentos.consola:
        from erp import balance, libro_mayor

        base_datos.inicializar(argumentos.datos)
        consulta = argumentos.consola.strip().lower()
        if consulta.startswith("select") and "libro mayor" in consulta:
            resultado = libro_mayor.libro_mayor_completo(argumentos.datos)
        elif consulta.startswith("select") and "balance" in consulta:
            resultado = balance.balance_comprobacion(argumentos.datos)
        else:
            with base_datos.conectar(argumentos.datos) as conexion:
                resultado = [dict(f) for f in conexion.execute(argumentos.consola).fetchall()]
        for fila in resultado:
            print(fila)
        return 0

    # -- Modo reiniciar ---------------------------------------------------
    if argumentos.reiniciar:
        ruta = base_datos.reiniciar(argumentos.datos)
        with base_datos.conectar(ruta) as conexion:
            base_datos.asegurar_empresa(conexion)
        print(f"Base de datos vaciada: {ruta}")
        return 0

    # -- Modo carga de datos de prueba ------------------------------------
    if argumentos.cargar_demo:
        resultado = datos_demo.cargar(argumentos.datos, recrear=True)
        cuadre = resultado["cuadre_libro_diario"]
        print(f"Empresa:       {resultado['empresa']['nombre']}")
        print(f"Cuentas:       {resultado['cuentas_creadas']}")
        print(f"Asientos:      {resultado['asientos_creados']}")
        print(f"Total Debe:    {formato_monto(cuadre['total_debe'])}")
        print(f"Total Haber:   {formato_monto(cuadre['total_haber'])}")
        print(f"Cuadra:        {'sí' if cuadre['cuadra'] else 'NO'}")
        if resultado["errores"]:
            print(f"Errores:       {len(resultado['errores'])}")
            for error in resultado["errores"]:
                print(f"  - {error['fecha']} {error['concepto']}: {error['error']}")
        return 0

    # -- Modo servidor web -------------------------------------------------
    from erp import servidor as modulo_servidor

    servidor, archivo = modulo_servidor.crear_servidor(
        argumentos.host, argumentos.puerto, argumentos.datos
    )

    # Siembra automática: solo cuando la base está vacía, para que el
    # sistema sea usable desde la primera visita. En un plan gratuito el
    # disco se borra en cada reinicio, así que esto se repite.
    sembrado = False
    if argumentos.auto_sembrar:
        with base_datos.conectar(archivo) as conexion:
            cuantos = conexion.execute("SELECT COUNT(*) FROM asiento").fetchone()[0]
        if cuantos == 0:
            datos_demo.cargar(archivo)
            sembrado = True

    direccion = f"http://{argumentos.host}:{argumentos.puerto}"
    print("=" * 62)
    print("  MINI ERP CONTABLE · Etapas 1 a 3")
    print("=" * 62)
    print(f"  Interfaz web : {direccion}")
    print(f"  Base de datos: {archivo}")
    if sembrado:
        print("  Datos de prueba: cargados (la base estaba vacía)")
    print("  Flujo: Plan de cuentas → Libro Diario → Libro Mayor → Balance")
    print("  Para demostrar, usá el botón «Cargar datos de prueba» en el Panel.")
    print("  Detener el servidor: Ctrl+C")
    if os.environ.get("RENDER"):
        # Aviso real: en el plan gratuito de Render el sistema de archivos
        # es efímero y se pierde también cuando el servicio se apaga a los
        # 15 minutos de inactividad.
        print("  ────────────────────────────────────────────────────")
        print("  hosting: plan gratuito con disco efímero.")
        print("  Los asientos que cargues se pierden si el servicio se")
        print("  apaga por inactividad o se vuelve a desplegar.")
    print("=" * 62)

    if argumentos.abrir:
        try:
            webbrowser.open(direccion)
        except Exception:  # pragma: no cover
            pass

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())