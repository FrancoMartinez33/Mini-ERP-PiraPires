/* =============================================================================
 * Mini ERP Contable — lógica de la interfaz
 * =============================================================================
 * Se comunica con el servidor Python mediante la API JSON de /api.
 * No usa ningún framework: solo HTML, Tailwind CSS v4, `ui.js` y este archivo.
 *
 * Convenciones:
 *   · Los importes viajan como enteros en CENTAVOS (ver erp/dinero.py).
 *   · `estado` es el objeto de estado global de la aplicación.
 *   · Los errores del servidor se muestran con el código del control
 *     infringido (C1..C6) para conectar el error con la sección 18.
 *   · Toda la appearance viene de los componentes de ui.js, que replican la
 *     API de TailGrids (variant, appearance, size, iconOnly, pending...).
 * ========================================================================== */

// ============================================================================
// Utilidades generales
// ============================================================================

const $ = (sel, raiz = document) => raiz.querySelector(sel);
const $$ = (sel, raiz = document) => Array.from(raiz.querySelectorAll(sel));

/** Atajo al escapador de ui.js: evita inyecciones y rompe la vista. */
const esc = UI.escapar;

/** Formatea centavos como texto contable: 150000050 -> "G. 1.500.000,50". */
function fmt(centavos, conSimbolo = true) {
  const valor = (Number(centavos) || 0) / 100;
  const texto = new Intl.NumberFormat('es-PY', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(valor);
  return conSimbolo && estado.moneda ? `${estado.moneda} ${texto}` : texto;
}

/** Formatea una fecha ISO (AAAA-MM-DD) como DD/MM/AAAA. */
function fmtFecha(iso) {
  if (!iso) return '';
  const [a, m, d] = String(iso).split('-');
  return d ? `${d}/${m}/${a}` : iso;
}

/**
 * Interpreta lo que el usuario escribe en un campo de importe.
 * Acepta "1.500.000,50", "1,500,000.50", "1500000", "G. 1.500.000", etc.
 * Devuelve un número en la unidad monetaria (no en centavos).
 */
function leerMonto(valor) {
  let texto = String(valor ?? '').trim().replace(/\s/g, '');
  if (!texto) return 0;

  // 1) Se quitan los símbolos de moneda y cualquier letra: "G. 1.500.000" -> "1.500.000"
  texto = texto.replace(/[A-Za-z$€₲]/g, '');
  if (!texto) return 0;

  // 2) Se decide cuál separador es decimal según cuál aparece más a la derecha.
  const ultimoPunto = texto.lastIndexOf('.');
  const ultimaComa = texto.lastIndexOf(',');
  if (ultimoPunto >= 0 && ultimaComa >= 0) {
    texto = ultimoPunto > ultimaComa
      ? texto.replace(/,/g, '')                       // coma = miles
      : texto.replace(/\./g, '').replace(',', '.');   // punto = miles
  } else if (ultimoPunto >= 0) {
    // Un solo punto puede ser decimal ("10.5") o de miles ("1.500.000").
    texto = (texto.match(/\./g) || []).length > 1
      ? texto.replace(/\./g, '')
      : texto.replace(/\./g, '.');
  } else if (ultimaComa >= 0) {
    texto = (texto.match(/,/g) || []).length > 1
      ? texto.replace(/,/g, '')
      : texto.replace(',', '.');
  }

  texto = texto.replace(/[^0-9.\-]/g, '');
  const numero = Number.parseFloat(texto);
  return Number.isFinite(numero) ? numero : 0;
}

/** Formatea un importe en el campo para que se lea mientras se tipea. */
function escribirMonto(campo, centavos) {
  campo.value = (Number(centavos) || 0) === 0
    ? ''
    : (Number(centavos) / 100).toLocaleString('es-PY', { maximumFractionDigits: 2 });
}

/** Retrasa la ejecución de una función (para los campos de búsqueda). */
function debounce(funcion, espera) {
  let temporizador;
  return (...argumentos) => {
    clearTimeout(temporizador);
    temporizador = setTimeout(() => funcion(...argumentos), espera);
  };
}

// ============================================================================
// Estado global
// ============================================================================

const estado = {
  vista: 'panel',
  empresa: null,
  moneda: 'G.',
  cuentas: [],          // plan de cuentas completo
  detalle: [],          // solo cuentas de detalle activas (para los selects)
  periodo: { desde: '', hasta: '' },
  asientoEditando: null,
  lineas: [],           // líneas del asiento en edición
  cuentaEditando: null,
};

// ============================================================================
// Acceso a la API
// ============================================================================

/**
 * Llama a un endpoint de la API.
 * Si el servidor devuelve un error de validación, lanza una excepción con
 * la información del control infringido para poder mostrarla.
 */
async function api(ruta, opciones = {}) {
  const config = { headers: {}, ...opciones };
  if (config.body && typeof config.body !== 'string') {
    config.headers['Content-Type'] = 'application/json';
    config.body = JSON.stringify(config.body);
  }

  const respuesta = await fetch(ruta, config);
  const texto = await respuesta.text();
  let datos;
  try {
    datos = texto ? JSON.parse(texto) : {};
  } catch {
    throw { mensaje: `Respuesta ilegible del servidor (${respuesta.status}).` };
  }

  if (!respuesta.ok) {
    throw {
      mensaje: datos.error || `Error ${respuesta.status}`,
      regla: datos.regla || null,
      titulo: datos.titulo_regla || null,
      detalles: datos.detalles || [],
    };
  }
  return datos;
}

/**
 * Muestra un aviso con el componente Toast.
 * @param {string} mensaje
 * @param {'success'|'danger'|'warning'|'info'} variante
 * @param {string|null} titulo
 * @param {string[]} detalles
 */
function avisar(mensaje, variante = 'info', titulo = null, detalles = []) {
  return UI.toast({
    variant: variante,
    title: titulo ?? (variante === 'success' ? 'Listo' : 'Aviso'),
    description: mensaje,
    detalles,
  });
}

/** Muestra el error de la API con el código de control correspondiente. */
function avisarError(error) {
  const titulo = error.regla
    ? `Regla infringida: ${error.regla}`
    : (error.titulo || 'No se pudo completar la operación');
  UI.toast({
    variant: 'danger',
    title: titulo,
    description: error.mensaje,
    detalles: error.detalles || [],
  });
}

// ============================================================================
// Navegación entre vistas
// ============================================================================

const VISTAS = ['panel', 'cuentas', 'diario', 'mayor', 'balance', 'controles'];

/** Carga los datos de la vista indicada la primera vez que se muestra. */
function refrescarVista(vista) {
  if (vista === 'panel') cargarPanel();
  if (vista === 'cuentas') cargarCuentas();
  if (vista === 'diario') {
    // La primera vez se prepara el formulario con dos líneas vacías.
    if (!$('#lineas-asiento')) iniciarFormulario();
    cargarCuentas();
    cargarDiario();
  }
  if (vista === 'mayor') { cargarCuentas(); cargarMayor(); }
  if (vista === 'balance') cargarBalance();
  if (vista === 'controles') cargarControles();
}

/**
 * Muestra una vista y trae sus datos.
 * Los paneles y las pestañas los administra UI.conectarTabsSincronizados:
 * esta función solo carga contenido y sube la pantalla.
 */
function irA(vista, moverFoco = false) {
  if (!VISTAS.includes(vista)) vista = 'panel';
  estado.vista = vista;
  navegacion.irA(vista, moverFoco);
  window.scrollTo({ top: 0, behavior: moverFoco ? 'auto' : 'smooth' });
  refrescarVista(vista);
}

/** Objeto de navegación, lo crea iniciar(). */
let navegacion = { irA: () => {} };

// ============================================================================
// Arranque
// ============================================================================

async function iniciar() {
  // Los dos menús (lateral y móvil) controlan los mismos paneles y quedan
  // sincronizados: con las flechas se recorren dentro de cada menú.
  navegacion = UI.conectarTabsSincronizados({
    grupos: [$$('#menu [role="tab"]'), $$('#menu-movil [role="tab"]')],
    seleccionar: (vista) => { estado.vista = vista; },
  });

  $('#btn-refrescar').onclick = () => refrescarVista(estado.vista);
  $('#filtro-desde').onchange = leerPeriodo;
  $('#filtro-hasta').onchange = leerPeriodo;

  await cargarEmpresa();
  await cargarCuentas();
  irA('panel');
}

/** Lee el filtro de período de la barra superior. */
function leerPeriodo() {
  estado.periodo = {
    desde: $('#filtro-desde').value,
    hasta: $('#filtro-hasta').value,
  };
  if (estado.vista === 'mayor') cargarMayor();
  if (estado.vista === 'balance') cargarBalance();
  if (estado.vista === 'diario') cargarDiario();
}

/** Construye los parámetros de período para las consultas. */
function conPeriodo(extra = {}) {
  const params = new URLSearchParams({
    ...(estado.periodo.desde ? { desde: estado.periodo.desde } : {}),
    ...(estado.periodo.hasta ? { hasta: estado.periodo.hasta } : {}),
    ...extra,
  });
  const texto = params.toString();
  return texto ? `?${texto}` : '';
}

// ============================================================================
// Vista: Panel
// ============================================================================

async function cargarEmpresa() {
  try {
    estado.empresa = await api('/api/empresa');
    estado.moneda = estado.empresa.moneda || 'G.';
    $('#empresa-nombre').textContent = estado.empresa.nombre;
    $('#empresa-detalle').textContent = [
      estado.empresa.cuit && `CUIT ${estado.empresa.cuit}`,
      estado.empresa.actividad,
      estado.empresa.ejercicio_inicio &&
        `Ejercicio ${fmtFecha(estado.empresa.ejercicio_inicio)} – ${fmtFecha(estado.empresa.ejercicio_fin)}`,
    ].filter(Boolean).join(' · ');

    $('#filtro-desde').value = estado.empresa.ejercicio_inicio || '';
    $('#filtro-hasta').value = estado.empresa.ejercicio_fin || '';
    estado.periodo = { desde: $('#filtro-desde').value, hasta: $('#filtro-hasta').value };

    pintarFormularioEmpresa();
  } catch (error) {
    avisarError(error);
  }
}

function pintarFormularioEmpresa() {
  const campos = [
    ['nombre', 'Nombre o razón social', 'text'],
    ['cuit', 'CUIT / RUC', 'text'],
    ['actividad', 'Actividad principal', 'text'],
    ['ejercicio_inicio', 'Inicio del ejercicio', 'date'],
    ['ejercicio_fin', 'Fin del ejercicio', 'date'],
    ['moneda', 'Símbolo de moneda', 'text'],
  ];
  $('#form-empresa').innerHTML = campos.map(([clave, etiqueta, tipo]) =>
    UI.campoHTML({
      id: `emp-${clave}`,
      label: etiqueta,
      control: UI.inputHTML({ id: `emp-${clave}`, name: clave, type: tipo, value: estado.empresa?.[clave] ?? '' }),
    })
  ).join('')
    + UI.botonHTML({
      label: 'Guardar datos de la empresa',
      attributes: 'type="submit"',
      className: 'w-full',
    });

  $('#form-empresa').onsubmit = async (evento) => {
    evento.preventDefault();
    // Estado pending del componente Button: se atenúa, marca aria-busy y
    // conserva el foco en lugar de desaparecer del recorrido del teclado.
    const boton = evento.target.querySelector('button[type="submit"]');
    boton.classList.add('btn-pending');
    boton.setAttribute('aria-busy', 'true');
    try {
      const datos = Object.fromEntries(new FormData(evento.target).entries());
      estado.empresa = await api('/api/empresa', { method: 'PUT', body: datos });
      estado.moneda = estado.empresa.moneda || 'G.';
      avisar('Datos de la empresa actualizados.', 'success');
      await cargarPanel();
      $('#empresa-nombre').textContent = estado.empresa.nombre;
      $('#empresa-detalle').textContent = [
        estado.empresa.cuit && `CUIT ${estado.empresa.cuit}`,
        estado.empresa.actividad,
      ].filter(Boolean).join(' · ');
    } catch (error) {
      avisarError(error);
    } finally {
      boton.classList.remove('btn-pending');
      boton.removeAttribute('aria-busy');
    }
  };
}

/** Tonos de los indicadores, según el estado del control que muestran. */
const TONO_KPI = {
  primary: 'border-primary-200 bg-primary-50',
  success: 'border-success-200 bg-success-50',
  danger:  'border-danger-200 bg-danger-50',
  neutral: 'border-surface-200 bg-surface-50',
};

async function cargarPanel() {
  try {
    const [resumen, controles] = await Promise.all([
      api('/api/resumen'),
      api('/api/controles'),
    ]);

    const cuadre = resumen.cuadre_libro_diario;
    const totales = resumen.totales_balance;

    const indicadores = [
      {
        titulo: 'Asientos contabilizados',
        valor: cuadre.asientos_confirmados,
        detalle: `${cuadre.asientos_borrador} borrador(es) · ${cuadre.asientos_anulados} anulado(s)`,
        tono: cuadre.asientos_confirmados > 0 ? 'primary' : 'neutral',
      },
      {
        titulo: 'Total de débitos',
        valor: fmt(cuadre.total_debe),
        detalle: cuadre.asientos_confirmados > 0
          ? 'Suma de todos los asientos confirmados'
          : 'Todavía no hay asientos confirmados',
        tono: cuadre.total_debe > 0 ? 'primary' : 'neutral',
      },
      {
        titulo: 'Total de créditos',
        valor: fmt(cuadre.total_haber),
        detalle: cuadre.cuadra
          ? 'Control 1 · idéntico a los débitos'
          : 'Control 1 · NO coincide con los débitos',
        tono: cuadre.cuadra ? 'success' : 'danger',
      },
      {
        titulo: 'Balance',
        valor: resumen.balance_cuadrado ? 'Cuadra' : 'No cuadra',
        detalle: `${resumen.cuentas_detalle} cuentas de detalle · ` +
          `saldos ${fmt(totales.saldo_deudor, false)} = ${fmt(totales.saldo_acreedor, false)}`,
        tono: resumen.balance_cuadrado ? 'success' : 'danger',
      },
    ];

    $('#panel-indicadores').innerHTML = indicadores.map((i) => `
      <div class="kpi ${TONO_KPI[i.tono]}">
        <p class="text-xs font-medium text-surface-500">${esc(i.titulo)}</p>
        <p class="mt-1 text-2xl font-semibold tracking-tight text-surface-900 tabular-nums">${esc(i.valor)}</p>
        <p class="mt-1 text-xs text-surface-500">${esc(i.detalle)}</p>
      </div>`).join('');

    $('#panel-controles').innerHTML = controles.controles.map((c) => `
      <div class="flex items-start gap-3 border-b border-surface-100 py-2.5 last:border-0">
        ${UI.badge(c.codigo, {
          variant: c.cumple ? 'success' : 'danger',
          iconName: c.cumple ? 'cheque' : 'cruz',
        })}
        <div class="min-w-0">
          <p class="text-sm font-medium text-surface-800">${esc(c.titulo)}</p>
          <p class="text-xs text-surface-500">${esc(c.detalle)}</p>
        </div>
      </div>`).join('');
  } catch (error) {
    avisarError(error);
  }
}

// ============================================================================
// Vista: Plan de cuentas
// ============================================================================

async function cargarCuentas() {
  try {
    const datos = await api(
      `/api/cuentas?inactivas=${$('#cuenta-inactivas')?.checked === false ? 0 : 1}` +
      `${$('#cuenta-busqueda')?.value ? `&q=${encodeURIComponent($('#cuenta-busqueda').value)}` : ''}`
    );
    estado.cuentas = datos.cuentas;
    estado.detalle = datos.cuentas.filter(
      (c) => c.recibe_movimiento && c.estado === 'ACTIVA'
    );
    pintarCuentas();
    pintarFormularioCuenta();
    actualizarSelectoresCuenta();
  } catch (error) {
    avisarError(error);
  }
}

/** Las clasificaciones se colorean con los mismos tonos que sus naturalezas. */
const CLASIFICACION = {
  1: 'ACTIVO', 2: 'PASIVO', 3: 'PATRIMONIO NETO',
  4: 'INGRESOS', 5: 'COSTOS', 6: 'GASTOS',
};

/** El color de una cuenta depende de su naturaleza (control 6). */
function badgeNaturaleza(naturaleza, texto = null) {
  return UI.badge(texto ?? (naturaleza === 'D' ? 'Deudora' : 'Acreedora'), {
    variant: naturaleza === 'D' ? 'primary' : 'secondary',
  });
}

function pintarCuentas() {
  const cuerpo = $('#cuentas-tbody');
  if (!estado.cuentas.length) {
    cuerpo.innerHTML = `
      <tr><td colspan="6" class="py-10 text-center text-surface-400">
        No hay cuentas que coincidan con el filtro.
        <p class="mt-1 text-xs">Cargá los datos de prueba desde el Panel o creá la primera cuenta.</p>
      </td></tr>`;
    return;
  }

  const varianteClasificacion = {
    1: 'primary', 2: 'secondary', 3: 'secondary',
    4: 'success', 5: 'warning', 6: 'warning',
  };

  cuerpo.innerHTML = estado.cuentas.map((c) => {
    const sangria = (c.nivel - 1) * 14;
    const agrupadora = !c.recibe_movimiento;
    return `
      <tr>
        <td class="font-mono text-xs text-surface-600">${esc(c.codigo)}</td>
        <td style="padding-left:${12 + sangria}px" class="${agrupadora ? 'font-semibold text-surface-800' : 'font-medium'}">
          ${esc(c.nombre)}
          ${agrupadora
            ? '<span class="ml-1 text-[10px] font-normal text-surface-400">(agrupadora)</span>' : ''}
        </td>
        <td>
          ${UI.badge(`${c.clasificacion} · ${esc(c.clasificacion_nombre)}`, {
            variant: varianteClasificacion[c.clasificacion] ?? 'secondary',
          })}
        </td>
        <td>${badgeNaturaleza(c.naturaleza, esc(c.naturaleza_nombre))}</td>
        <td>
          ${UI.badge(c.estado, { variant: c.estado === 'ACTIVA' ? 'success' : 'secondary' })}
        </td>
        <td>
          <div class="flex justify-end gap-1.5">
            ${UI.botonHTML({
              label: 'Editar', size: 'xs', appearance: 'outline', variant: 'secondary',
              iconName: 'lapiz', attributes: `data-editar="${esc(c.codigo)}"`,
              title: `Editar la cuenta ${esc(c.codigo)}`,
            })}
            ${agrupadora ? '' : UI.botonHTML({
              label: c.estado === 'ACTIVA' ? 'Desactivar' : 'Activar',
              size: 'xs', appearance: 'outline',
              variant: c.estado === 'ACTIVA' ? 'warning' : 'success',
              iconName: c.estado === 'ACTIVA' ? 'prohibido' : 'tilde',
              attributes: `data-estado="${esc(c.codigo)}" data-nuevo="${c.estado === 'ACTIVA' ? 'INACTIVA' : 'ACTIVA'}"`,
            })}
            ${UI.botonHTML({
              label: 'Borrar', size: 'xs', appearance: 'outline', variant: 'danger',
              iconName: 'papelera', attributes: `data-borrar="${esc(c.codigo)}"`,
              title: `Eliminar la cuenta ${esc(c.codigo)}`,
            })}
          </div>
        </td>
      </tr>`;
  }).join('');

  UI.delegar(cuerpo, '[data-editar]', (d) => editarCuenta(d.editar));
  UI.delegar(cuerpo, '[data-estado]', (d) => cambiarEstadoCuenta(d.estado, d.nuevo));
  UI.delegar(cuerpo, '[data-borrar]', (d) => borrarCuenta(d.borrar));
}

function pintarFormularioCuenta() {
  const enEdicion = estado.cuentaEditando
    ? estado.cuentas.find((c) => c.codigo === estado.cuentaEditando)
    : null;

  $('#cuenta-form-titulo').textContent = enEdicion
    ? `Editar cuenta ${enEdicion.codigo}`
    : 'Nueva cuenta';

  $('#form-cuenta').innerHTML = `
    ${UI.alertaHTML('info', 'Cómo se codifica una cuenta',
      'El código es jerárquico (<code class="codigo">1.1.01</code>) y su primer dígito debe '
      + 'coincidir con la clasificación. Una cuenta de nivel 2 o 3 necesita que exista su cuenta padre.')}

    <div class="grid grid-cols-2 gap-3">
      ${UI.campoHTML({
        id: 'cuenta-codigo',
        label: 'Código',
        control: UI.inputHTML({
          id: 'cuenta-codigo', name: 'codigo', value: enEdicion?.codigo ?? '',
          placeholder: '1.1.05', disabled: Boolean(enEdicion), className: 'font-mono',
        }),
        description: enEdicion
          ? 'El código no se modifica: es la identidad de la cuenta.' : null,
      })}
      ${UI.campoHTML({
        id: 'cuenta-clasificacion',
        label: 'Clasificación',
        control: UI.selectHTML({
          id: 'cuenta-clasificacion', name: 'clasificacion',
          value: enEdicion?.clasificacion ?? 1,
          items: Object.entries(CLASIFICACION).map(([n, t]) => ({
            value: n, label: `${n} · ${t}`,
          })),
        }),
      })}
    </div>

    ${UI.campoHTML({
      id: 'cuenta-nombre',
      label: 'Nombre de la cuenta',
      isRequired: true,
      control: UI.inputHTML({
        id: 'cuenta-nombre', name: 'nombre', value: enEdicion?.nombre ?? '',
        placeholder: 'Gastos de publicidad',
      }),
    })}

    ${UI.campoHTML({
      id: 'cuenta-naturaleza',
      label: 'Naturaleza',
      control: UI.selectHTML({
        id: 'cuenta-naturaleza', name: 'naturaleza',
        value: enEdicion?.naturaleza ?? 'D',
        items: [{ value: 'D', label: 'D · Deudora' }, { value: 'A', label: 'A · Acreedora' }],
      }),
      description: 'Se deduce de la clasificación. Se puede cambiar solo en cuentas '
        + 'contrapasivas (la Depreciación Acumulada es un activo con saldo acreedor).',
    })}

    <div class="grid grid-cols-2 gap-3">
      ${UI.campoHTML({
        id: 'cuenta-tipo',
        label: 'Tipo de cuenta',
        control: UI.selectHTML({
          id: 'cuenta-tipo', name: 'recibe_movimiento',
          value: enEdicion ? (enEdicion.recibe_movimiento ? 1 : 0) : 1,
          items: [
            { value: 1, label: 'Detalle (admite movimientos)' },
            { value: 0, label: 'Agrupadora (solo subtotales)' },
          ],
        }),
      })}
      ${UI.campoHTML({
        id: 'cuenta-estado',
        label: 'Estado',
        control: UI.selectHTML({
          id: 'cuenta-estado', name: 'estado',
          value: enEdicion?.estado ?? 'ACTIVA',
          items: ['ACTIVA', 'INACTIVA'],
        }),
      })}
    </div>

    <div class="flex gap-2 pt-1">
      ${UI.botonHTML({
        label: enEdicion ? 'Guardar cambios' : 'Crear cuenta',
        attributes: 'type="submit"',
        className: enEdicion ? '' : 'w-full',
      })}
      ${enEdicion ? UI.botonHTML({
        label: 'Cancelar', variant: 'secondary', appearance: 'outline',
        attributes: 'id="cuenta-cancelar"',
      }) : ''}
    </div>
  `;

  // Al cambiar la clasificación se propone la naturaleza esperada (control 6).
  $('#cuenta-clasificacion').onchange = (evento) => {
    const esperadas = { 1: 'D', 2: 'A', 3: 'A', 4: 'A', 5: 'D', 6: 'D' };
    $('#cuenta-naturaleza').value = esperadas[evento.target.value];
  };

  const cancelar = $('#cuenta-cancelar');
  if (cancelar) cancelar.onclick = () => {
    estado.cuentaEditando = null;
    pintarFormularioCuenta();
  };

  $('#form-cuenta').onsubmit = guardarCuenta;
}

async function guardarCuenta(evento) {
  evento.preventDefault();
  const formulario = Object.fromEntries(new FormData(evento.target).entries());
  const cuerpo = {
    nombre: formulario.nombre,
    clasificacion: Number(formulario.clasificacion),
    recibe_movimiento: formulario.recibe_movimiento === '1',
    naturaleza: formulario.naturaleza,
    estado: formulario.estado,
  };

  try {
    if (estado.cuentaEditando) {
      await api(`/api/cuentas/${encodeURIComponent(estado.cuentaEditando)}`,
        { method: 'PUT', body: cuerpo });
      avisar(`Cuenta ${estado.cuentaEditando} actualizada.`, 'success');
      estado.cuentaEditando = null;
    } else {
      const respuesta = await api('/api/cuentas', {
        method: 'POST', body: { codigo: formulario.codigo, ...cuerpo },
      });
      avisar(`Cuenta ${respuesta.cuenta.codigo} ${respuesta.cuenta.nombre} creada.`, 'success');
    }
    await cargarCuentas();
  } catch (error) {
    avisarError(error);
  }
}

function editarCuenta(codigo) {
  estado.cuentaEditando = codigo;
  pintarFormularioCuenta();
  const nombre = $('#cuenta-nombre');
  nombre.focus();
  nombre.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function cambiarEstadoCuenta(codigo, nuevo) {
  try {
    await api(`/api/cuentas/${encodeURIComponent(codigo)}/estado`, {
      method: 'POST', body: { estado: nuevo },
    });
    avisar(`Cuenta ${codigo} ahora está ${nuevo}.`, 'success');
    await cargarCuentas();
  } catch (error) {
    avisarError(error);
  }
}

async function borrarCuenta(codigo) {
  const aceptado = await UI.confirmar({
    titulo: `Eliminar la cuenta ${codigo}?`,
    descripcion: 'Solo se puede borrar una cuenta que no tenga movimientos '
      + 'asociados ni cuentas hijas dependientes.',
    confirmarTexto: 'Eliminar',
    icono: 'papelera',
  });
  if (!aceptado) return;

  try {
    await api(`/api/cuentas/${encodeURIComponent(codigo)}`, { method: 'DELETE' });
    avisar(`Cuenta ${codigo} eliminada.`, 'success');
    estado.cuentaEditando = null;
    await cargarCuentas();
  } catch (error) {
    avisarError(error);
  }
}

// ============================================================================
// Vista: Libro Diario
// ============================================================================

function opcionesCuentas(seleccionado = '') {
  if (!estado.detalle.length) {
    return '<option value="">No hay cuentas de detalle activas</option>';
  }
  return estado.detalle.map((c) => `
    <option value="${esc(c.codigo)}" ${c.codigo === seleccionado ? 'selected' : ''}>
      ${esc(c.codigo)} · ${esc(c.nombre)} (${c.naturaleza === 'D' ? 'D' : 'A'})
    </option>`).join('');
}

async function cargarDiario() {
  try {
    const datos = await api(`/api/asientos${conPeriodo({
      estado: $('#diario-estado').value,
      ...($('#diario-busqueda').value ? { q: $('#diario-busqueda').value } : {}),
    })}`);

    const cuerpo = $('#diario-tbody');
    if (!datos.asientos.length) {
      cuerpo.innerHTML = `
        <tr><td colspan="9" class="py-10 text-center text-surface-400">
          Todavía no hay asientos en este período.
          <p class="mt-1 text-xs">Usá «+ Nuevo asiento» o cargá los datos de prueba desde el Panel.</p>
        </td></tr>`;
    } else {
      cuerpo.innerHTML = datos.asientos.map((a) => `
        <tr>
          <td class="font-mono text-xs text-surface-600">${a.numero}</td>
          <td class="whitespace-nowrap text-xs">${fmtFecha(a.fecha)}</td>
          <td>
            <p class="font-medium text-surface-800">${esc(a.concepto)}</p>
            ${a.observaciones ? `<p class="text-xs text-surface-400">${esc(a.observaciones)}</p>` : ''}
          </td>
          <td class="text-center text-xs text-surface-500">${a.lineas}</td>
          <td class="num">${fmt(a.total_debe)}</td>
          <td class="num">${fmt(a.total_haber)}</td>
          <td class="text-center">
            ${UI.badge(a.cuadra ? 'Cuadra' : 'Descuadrado', {
              variant: a.cuadra ? 'success' : 'danger',
              iconName: a.cuadra ? 'cheque' : 'cruz',
            })}
          </td>
          <td class="text-center">${insigniaEstadoAsiento(a.estado)}</td>
          <td>
            <div class="flex justify-end gap-1.5">
              ${UI.botonHTML({
                label: 'Ver', size: 'xs', appearance: 'outline', variant: 'secondary',
                iconName: 'ojo', attributes: `data-ver="${a.id}"`,
              })}
              ${a.estado === 'BORRADOR' ? `
                ${UI.botonHTML({
                  label: 'Editar', size: 'xs', appearance: 'outline', variant: 'secondary',
                  iconName: 'lapiz', attributes: `data-editar="${a.id}"`,
                })}
                ${UI.botonHTML({
                  label: 'Confirmar', size: 'xs', appearance: 'outline', variant: 'success',
                  iconName: 'play', attributes: `data-confirmar="${a.id}"`,
                })}
                ${UI.botonHTML({
                  label: 'Borrar', size: 'xs', appearance: 'outline', variant: 'danger',
                  iconName: 'papelera', attributes: `data-borrar="${a.id}"`,
                })}` : ''}
              ${a.estado === 'CONFIRMADO' ? UI.botonHTML({
                label: 'Anular', size: 'xs', appearance: 'outline', variant: 'danger',
                iconName: 'prohibido', attributes: `data-anular="${a.id}"`,
              }) : ''}
            </div>
          </td>
        </tr>`).join('');
    }

    $('#diario-resumen').innerHTML = `
      <span>${datos.asientos.length} asiento(s) en el filtro</span>
      <span>Total Debe <strong class="font-mono">${fmt(datos.totales.debe)}</strong></span>
      <span>Total Haber <strong class="font-mono">${fmt(datos.totales.haber)}</strong></span>
      <span class="ml-auto">${datos.totales.debe === datos.totales.haber
        ? UI.badge('los totales del filtro cuadran', { variant: 'success', iconName: 'cheque' })
        : UI.badge('el filtro no cuadra (puede incluir borradores o anulados)', { variant: 'warning', iconName: 'alerta' })}</span>`;

    UI.delegar(cuerpo, '[data-ver]', (d) => verAsiento(d.ver));
    UI.delegar(cuerpo, '[data-editar]', (d) => editarAsiento(d.editar));
    UI.delegar(cuerpo, '[data-confirmar]', (d) => confirmarAsiento(d.confirmar));
    UI.delegar(cuerpo, '[data-anular]', (d) => anularAsiento(d.anular));
    UI.delegar(cuerpo, '[data-borrar]', (d) => borrarAsiento(d.borrar));
  } catch (error) {
    avisarError(error);
  }
}

function insigniaEstadoAsiento(estadoAsiento) {
  const mapa = {
    CONFIRMADO: { variant: 'success', icon: 'cheque' },
    BORRADOR: { variant: 'warning', icon: 'lapiz' },
    ANULADO: { variant: 'secondary', icon: 'prohibido' },
  };
  const config = mapa[estadoAsiento];
  if (!config) return esc(estadoAsiento);
  return UI.badge(estadoAsiento === 'BORRADOR' ? 'Borrador'
    : estadoAsiento === 'ANULADO' ? 'Anulado' : 'Confirmado',
    { variant: config.variant, iconName: config.icon });
}

// --- Formulario de asiento ------------------------------------------------

function iniciarFormulario(asiento = null) {
  estado.asientoEditando = asiento;
  estado.lineas = asiento
    ? asiento.partidas.map((p) => ({
        cuenta: p.cuenta, detalle: p.detalle || '',
        debe: (p.debe || 0) / 100, haber: (p.haber || 0) / 100,
      }))
    : [{ cuenta: '', detalle: '', debe: 0, haber: 0 },
       { cuenta: '', detalle: '', debe: 0, haber: 0 }];

  $('#diario-form-titulo').textContent = asiento
    ? `Editando asiento N.º ${asiento.numero}`
    : 'Nuevo asiento';
  $('#btn-cancelar-asiento').classList.toggle('oculto', !asiento);

  pintarCamposCabecera(asiento);
  pintarLineas();
}

function pintarCamposCabecera(asiento) {
  const formulario = $('#form-asiento');
  formulario.innerHTML = `
    <div class="grid gap-3 md:grid-cols-[170px_minmax(0,1fr)]">
      ${UI.campoHTML({
        id: 'asiento-fecha',
        label: 'Fecha',
        isRequired: true,
        control: UI.inputHTML({
          id: 'asiento-fecha', name: 'fecha', type: 'date',
          value: asiento?.fecha ?? new Date().toISOString().slice(0, 10),
        }),
      })}
      ${UI.campoHTML({
        id: 'asiento-concepto',
        label: 'Concepto',
        isRequired: true,
        control: UI.inputHTML({
          id: 'asiento-concepto', name: 'concepto', value: asiento?.concepto ?? '',
          placeholder: 'Ej.: Compra de mercaderías al contado',
        }),
      })}
    </div>

    ${UI.campoHTML({
      id: 'asiento-observaciones',
      label: 'Observaciones (opcional)',
      control: UI.inputHTML({
        id: 'asiento-observaciones', name: 'observaciones',
        value: asiento?.observaciones ?? '',
      }),
    })}

    <div>
      <p class="label">Partidas del asiento</p>
      <p class="help-text -mt-0.5 mb-2">
        Cargá al menos dos líneas. El control 1 exige que el total del Debe sea
        igual al total del Haber.
      </p>
      <div id="lineas-asiento" class="space-y-2"></div>
      ${UI.botonHTML({
        label: 'Agregar línea', variant: 'secondary', appearance: 'outline', size: 'sm',
        iconName: 'mas', attributes: 'id="btn-agregar-linea" class="mt-2"',
      })}
    </div>

    <div id="cuadre-asiento"></div>

    <div class="flex flex-wrap items-center gap-2 border-t border-surface-200 pt-4">
      ${UI.botonHTML({
        label: estado.asientoEditando ? 'Guardar cambios' : 'Confirmar y contabilizar',
        iconName: 'cheque', attributes: 'type="submit"',
      })}
      ${UI.botonHTML({
        label: 'Guardar como borrador', variant: 'secondary', appearance: 'outline',
        iconName: 'lapiz', attributes: 'id="btn-guardar-borrador"',
      })}
      <p class="text-xs text-surface-500">
        Un asiento descuadrado o con cuentas inactivas se puede guardar como
        borrador, pero no se contabiliza.
      </p>
    </div>
  `;

  $('#btn-agregar-linea').onclick = agregarLinea;
  $('#btn-guardar-borrador').onclick = (evento) => guardarAsiento(evento, false);
  formulario.onsubmit = (evento) => guardarAsiento(evento, true);
  pintarLineas();
}

function pintarLineas() {
  const contenedor = $('#lineas-asiento');
  contenedor.innerHTML = estado.lineas.map((linea, indice) => `
    <div class="grid grid-cols-12 items-end gap-2 rounded-field border border-surface-200
                bg-surface-50/50 p-3" data-indice="${indice}">
      <div class="col-span-12 mb-1 flex items-center justify-between sm:col-span-11">
        <span class="text-[11px] font-semibold text-surface-500">Línea ${indice + 1}</span>
        ${estado.lineas.length > 2
          ? `<button type="button" class="text-xs font-medium text-surface-400 transition
                                     hover:text-danger-600" data-quitar="${indice}">Quitar</button>`
          : ''}
      </div>
      <div class="col-span-12 sm:col-span-4">
        <label class="label" for="linea-${indice}-cuenta">Cuenta</label>
        <select id="linea-${indice}-cuenta" class="select" data-campo="cuenta" data-indice="${indice}">
          <option value="">Elegir cuenta…</option>
          ${opcionesCuentas(linea.cuenta)}
        </select>
      </div>
      <div class="col-span-12 sm:col-span-4">
        <label class="label" for="linea-${indice}-detalle">Detalle</label>
        <input id="linea-${indice}-detalle" class="input" data-campo="detalle" data-indice="${indice}"
               placeholder="Detalle de la línea" value="${esc(linea.detalle)}">
      </div>
      <div class="col-span-6 sm:col-span-2">
        <label class="label text-right" for="linea-${indice}-debe">Debe</label>
        <input id="linea-${indice}-debe" class="input text-right font-mono tabular-nums"
               data-campo="debe" data-indice="${indice}" inputmode="decimal"
               placeholder="0,00" value="${linea.debe ? linea.debe : ''}">
      </div>
      <div class="col-span-6 sm:col-span-2">
        <label class="label text-right" for="linea-${indice}-haber">Haber</label>
        <input id="linea-${indice}-haber" class="input text-right font-mono tabular-nums"
               data-campo="haber" data-indice="${indice}" inputmode="decimal"
               placeholder="0,00" value="${linea.haber ? linea.haber : ''}">
      </div>
    </div>`).join('');

  $$('#lineas-asiento [data-campo]').forEach((campo) => {
    const indice = Number(campo.dataset.indice);
    const clave = campo.dataset.campo;
    const esImporte = clave === 'debe' || clave === 'haber';

    // 'input' cubre la escritura; 'change' cubre el <select> y el pegado final.
    const sincronizar = () => {
      estado.lineas[indice][clave] = esImporte ? leerMonto(campo.value) : campo.value;
      if (esImporte) calcularCuadre();
    };

    campo.addEventListener('input', sincronizar);
    campo.addEventListener('change', () => {
      sincronizar();
      if (esImporte) {
        // Al salir del campo se normaliza el importe para que se lea mejor.
        escribirMonto(campo, Math.round(estado.lineas[indice][clave] * 100));
        calcularCuadre();
      }
    });
  });

  UI.delegar(contenedor, '[data-quitar]', (d) => {
    estado.lineas.splice(Number(d.quitar), 1);
    pintarLineas();
  });

  calcularCuadre();
}

function agregarLinea() {
  estado.lineas.push({ cuenta: '', detalle: '', debe: 0, haber: 0 });
  pintarLineas();
}

function calcularCuadre() {
  const totales = totalesAsiento();
  const cuadra = totales.debe === totales.haber && totales.debe > 0;
  const diferencia = totales.debe - totales.haber;
  const incompletas = estado.lineas.filter(
    (l) => !l.cuenta || (l.debe === 0 && l.haber === 0)
  ).length;

  $('#cuadre-asiento').innerHTML = `
    <div class="alert ${cuadra ? 'alert-success' : 'alert-warning'} block">
      <div class="flex flex-wrap items-center gap-x-6 gap-y-1">
        <span class="font-medium">${UI.icono(cuadra ? 'cheque' : 'alerta', { tamano: 16 })}
          ${cuadra ? 'El asiento cuadra' : 'El asiento todavía no cuadra'}</span>
        <span>Total Debe: <strong class="font-mono tabular-nums">${fmt(totales.debe)}</strong></span>
        <span>Total Haber: <strong class="font-mono tabular-nums">${fmt(totales.haber)}</strong></span>
        <span>Diferencia: <strong class="font-mono tabular-nums">${fmt(diferencia)}</strong></span>
      </div>
      ${incompletas
        ? `<p class="mt-1 text-xs opacity-80">${incompletas} línea(s) sin cuenta o sin importe.</p>`
        : ''}
    </div>`;
}

function totalesAsiento() {
  return estado.lineas.reduce((acumulado, linea) => ({
    debe: acumulado.debe + Math.round((linea.debe || 0) * 100),
    haber: acumulado.haber + Math.round((linea.haber || 0) * 100),
  }), { debe: 0, haber: 0 });
}

async function guardarAsiento(evento, confirmar) {
  evento?.preventDefault();

  const cabecera = {
    fecha: $('#asiento-fecha').value,
    concepto: $('#asiento-concepto').value,
    observaciones: $('#asiento-observaciones').value,
  };

  const partidas = estado.lineas
    .filter((l) => l.cuenta && (l.debe || l.haber))
    .map((l) => ({
      cuenta: l.cuenta,
      detalle: l.detalle,
      // Se envían los centavos como números enteros.
      debe: Math.round((l.debe || 0) * 100),
      haber: Math.round((l.haber || 0) * 100),
    }));

  if (!partidas.length) {
    avisar('Cargá al menos una línea con cuenta e importe.', 'warning', 'Asiento incompleto');
    return;
  }

  try {
    let respuesta;
    if (estado.asientoEditando) {
      respuesta = await api(`/api/asientos/${estado.asientoEditando}`, {
        method: 'PUT', body: { ...cabecera, partidas },
      });
    } else {
      respuesta = await api('/api/asientos', {
        method: 'POST', body: { ...cabecera, partidas, confirmar },
      });
    }

    const contabilizado = respuesta.asiento.estado === 'CONFIRMADO';
    avisar(
      `Asiento N.º ${respuesta.asiento.numero} ${contabilizado ? 'contabilizado' : 'guardado como borrador'}.`,
      contabilizado ? 'success' : 'info',
      contabilizado ? 'Asiento contabilizado' : 'Borrador guardado',
      contabilizado ? [] : ['Un borrador no suma en el Libro Mayor ni en el Balance hasta confirmarlo.'],
    );
    iniciarFormulario();
    await Promise.all([cargarDiario(), cargarPanel()]);
  } catch (error) {
    avisarError(error);
  }
}

async function verAsiento(id) {
  try {
    const { asiento } = await api(`/api/asientos/${id}`);
    const contenedor = $('#diario-detalle');
    contenedor.classList.remove('oculto');

    contenedor.innerHTML = `
      <div class="card">
        <div class="card-header">
          <h2 class="card-title">Asiento N.º ${asiento.numero} · ${fmtFecha(asiento.fecha)}</h2>
          ${insigniaEstadoAsiento(asiento.estado)}
          <div class="ml-auto">
            ${UI.botonHTML({
              label: 'Cerrar', variant: 'secondary', appearance: 'outline', size: 'sm',
              iconName: 'equis', attributes: 'id="btn-cerrar-detalle"',
            })}
          </div>
        </div>
        <div class="card-body">
          <p class="text-sm font-medium text-surface-800">${esc(asiento.concepto)}</p>
          ${asiento.observaciones
            ? `<p class="mt-0.5 text-xs text-surface-500">${esc(asiento.observaciones)}</p>` : ''}
          <div class="table-shell mt-3 rounded-field border border-surface-200">
            <table class="table">
              <thead>
                <tr>
                  <th class="w-12">Línea</th><th class="w-24">Código</th><th>Cuenta</th>
                  <th>Detalle</th><th class="w-32 text-right">Debe</th><th class="w-32 text-right">Haber</th>
                </tr>
              </thead>
              <tbody>
                ${asiento.partidas.map((p) => `
                  <tr>
                    <td class="text-xs text-surface-400">${p.linea}</td>
                    <td class="font-mono text-xs">${esc(p.cuenta)}</td>
                    <td>
                      ${esc(p.cuenta_nombre)}
                      ${UI.badge(p.cuenta_naturaleza === 'D' ? 'D' : 'A', {
                        variant: p.cuenta_naturaleza === 'D' ? 'primary' : 'secondary',
                        className: 'ml-1',
                      })}
                    </td>
                    <td class="text-xs text-surface-500">${esc(p.detalle ?? '')}</td>
                    <td class="num">${p.debe ? fmt(p.debe) : ''}</td>
                    <td class="num">${p.haber ? fmt(p.haber) : ''}</td>
                  </tr>`).join('')}
              </tbody>
              <tfoot>
                <tr>
                  <td colspan="4">
                    Totales
                    ${asiento.cuadra
                      ? UI.badge('cuadra', { variant: 'success', iconName: 'cheque', className: 'ml-1' })
                      : UI.badge('descuadrado', { variant: 'danger', iconName: 'cruz', className: 'ml-1' })}
                  </td>
                  <td class="num">${fmt(asiento.total_debe)}</td>
                  <td class="num">${fmt(asiento.total_haber)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </div>
      </div>`;

    $('#btn-cerrar-detalle').onclick = () => contenedor.classList.add('oculto');
    contenedor.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    avisarError(error);
  }
}

async function editarAsiento(id) {
  try {
    const { asiento } = await api(`/api/asientos/${id}`);
    $('#diario-detalle').classList.add('oculto');
    iniciarFormulario(asiento);
    $('#form-asiento').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    avisarError(error);
  }
}

async function confirmarAsiento(id) {
  try {
    const { asiento } = await api(`/api/asientos/${id}/confirmar`, { method: 'POST' });
    avisar(`Asiento N.º ${asiento.numero} contabilizado.`, 'success');
    await Promise.all([cargarDiario(), cargarPanel()]);
  } catch (error) {
    avisarError(error);
  }
}

async function anularAsiento(id) {
  // Un asiento confirmado no se edita ni se borra: se anula con un motivo,
  // que queda registrado en el Diario.
  const motivo = await UI.preguntar({
    titulo: 'Anular el asiento',
    descripcion: 'El asiento queda registrado pero deja de sumar en el Libro Mayor '
      + 'y en el Balance. El motivo queda guardado.',
    etiqueta: 'Motivo de la anulación',
    placeholder: 'Ej.: Error de tipeo en el importe',
    multilinea: true,
    requerido: true,
    confirmarTexto: 'Anular asiento',
    icono: 'prohibido',
  });
  if (motivo === null) return;

  try {
    await api(`/api/asientos/${id}/anular`, { method: 'POST', body: { motivo } });
    avisar('Asiento anulado. Ya no suma en el Libro Mayor ni en el Balance.', 'success');
    await Promise.all([cargarDiario(), cargarPanel()]);
  } catch (error) {
    avisarError(error);
  }
}

async function borrarAsiento(id) {
  const aceptado = await UI.confirmar({
    titulo: 'Eliminar el asiento',
    descripcion: 'Se borra definitivamente. Solo se puede hacer con asientos en estado '
      + 'borrador: un asiento confirmado se anula, no se borra.',
    confirmarTexto: 'Eliminar',
    icono: 'papelera',
  });
  if (!aceptado) return;

  try {
    await api(`/api/asientos/${id}`, { method: 'DELETE' });
    avisar('Asiento eliminado.', 'success');
    await Promise.all([cargarDiario(), cargarPanel()]);
  } catch (error) {
    avisarError(error);
  }
}

// ============================================================================
// Vista: Libro Mayor
// ============================================================================

function actualizarSelectoresCuenta() {
  const select = $('#mayor-cuenta');
  if (!select) return;
  const actual = select.value;
  select.innerHTML = '<option value="">Elegir cuenta…</option>' + opcionesCuentas();
  if (actual) select.value = actual;
}

async function cargarMayor(todas = false) {
  if (!todas && !$('#mayor-cuenta').value) {
    $('#mayor-resultado').innerHTML = `
      <div class="card">
        <div class="card-body py-12 text-center text-sm text-surface-400">
          ${UI.icono('balanza', { tamano: 28, clase: 'mx-auto mb-2 text-surface-300' })}
          Elegí una cuenta para ver su movimiento, o usá «Todas las cuentas».
        </div>
      </div>`;
    $('#mayor-cabecera').innerHTML = '';
    return;
  }

  try {
    if (todas) {
      const datos = await api(`/api/libro-mayor${conPeriodo()}`);
      $('#mayor-cabecera').innerHTML = `
        <p class="text-sm text-surface-600">
          Libro Mayor completo · ${datos.cuentas.length} cuenta(s) con movimiento.
        </p>
        <p class="mt-0.5 text-xs text-surface-400">
          Origen: ${esc(datos.origen)} (control ${datos.regla_aplicada}).
          ${UI.badge('Calculado, no cargado', { variant: 'success', className: 'ml-1' })}
        </p>`;
      $('#mayor-resultado').innerHTML = pintarMayorCompleto(datos);
    } else {
      const codigo = $('#mayor-cuenta').value;
      const datos = await api(`/api/cuentas/${encodeURIComponent(codigo)}/mayor${conPeriodo()}`);
      $('#mayor-cabecera').innerHTML = `
        <div class="flex flex-wrap items-center gap-2 pb-3">
          <p class="text-sm text-surface-600">
            Cuenta <strong>${esc(datos.cuenta.codigo)} · ${esc(datos.cuenta.nombre)}</strong>
          </p>
          ${badgeNaturaleza(datos.cuenta.naturaleza, datos.cuenta.naturaleza_nombre)}
          ${UI.badge(datos.cuenta.clasificacion_nombre, { variant: 'secondary' })}
        </div>`;
      $('#mayor-resultado').innerHTML = pintarMayorCuenta(datos);
    }
  } catch (error) {
    avisarError(error);
  }
}

function pintarMayorCuenta(datos) {
  const sinMovimientos = !datos.movimientos.length;
  return `
    <div class="card">
      <div class="table-shell">
        <table class="table">
          <thead>
            <tr>
              <th class="w-28">Fecha</th><th class="w-16">N.º</th>
              <th>Concepto</th><th>Detalle</th>
              <th class="w-32 text-right">Debe</th><th class="w-32 text-right">Haber</th>
              <th class="w-32 text-right">Saldo</th>
            </tr>
          </thead>
          <tbody>
            ${datos.saldo_anterior ? `
              <tr class="bg-surface-50">
                <td colspan="6" class="font-medium text-surface-600">Saldo anterior al período</td>
                <td class="num font-medium">${fmt(datos.saldo_anterior)}</td>
              </tr>` : ''}
            ${sinMovimientos ? `
              <tr><td colspan="7" class="py-10 text-center text-surface-400">
                La cuenta no tiene movimientos confirmados en el período seleccionado.
              </td></tr>` : ''}
            ${datos.movimientos.map((m) => `
              <tr>
                <td class="whitespace-nowrap text-xs">${fmtFecha(m.fecha)}</td>
                <td class="font-mono text-xs text-surface-600">${m.numero}</td>
                <td class="text-sm">${esc(m.concepto)}</td>
                <td class="text-xs text-surface-500">${esc(m.detalle ?? '')}</td>
                <td class="num">${m.debe ? fmt(m.debe) : ''}</td>
                <td class="num">${m.haber ? fmt(m.haber) : ''}</td>
                <td class="num font-semibold">${fmt(m.saldo)}</td>
              </tr>`).join('')}
          </tbody>
          <tfoot>
            <tr>
              <td colspan="4">Totales de la cuenta</td>
              <td class="num">${fmt(datos.totales.debe)}</td>
              <td class="num">${fmt(datos.totales.haber)}</td>
              <td class="num">${fmt(datos.totales.saldo_final)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>`;
}

function pintarMayorCompleto(datos) {
  if (!datos.cuentas.length) {
    return `
      <div class="card">
        <div class="card-body py-12 text-center text-sm text-surface-400">
          Todavía no hay asientos confirmados, así que el Libro Mayor está vacío.
        </div>
      </div>`;
  }

  return datos.cuentas.map((bloque) => `
    <div class="card mb-4">
      <div class="card-header">
        <h2 class="card-title">
          <span class="font-mono text-xs text-surface-500">${esc(bloque.cuenta.codigo)}</span>
          ${esc(bloque.cuenta.nombre)}
        </h2>
        ${badgeNaturaleza(bloque.cuenta.naturaleza, bloque.cuenta.naturaleza_nombre)}
        <span class="ml-auto text-xs text-surface-500">
          ${bloque.movimientos.length} movimiento(s) ·
          Saldo final <strong class="font-mono">${fmt(bloque.totales.saldo_final)}</strong>
        </span>
      </div>
      <div class="table-shell">
        <table class="table">
          <thead>
            <tr>
              <th class="w-28">Fecha</th><th class="w-16">N.º</th>
              <th>Concepto</th><th class="w-32 text-right">Debe</th>
              <th class="w-32 text-right">Haber</th><th class="w-32 text-right">Saldo</th>
            </tr>
          </thead>
          <tbody>
            ${bloque.saldo_anterior ? `
              <tr class="bg-surface-50">
                <td colspan="5" class="text-xs text-surface-500">Saldo anterior</td>
                <td class="num text-xs">${fmt(bloque.saldo_anterior)}</td>
              </tr>` : ''}
            ${bloque.movimientos.map((m) => `
              <tr>
                <td class="whitespace-nowrap text-xs">${fmtFecha(m.fecha)}</td>
                <td class="font-mono text-xs text-surface-600">${m.numero}</td>
                <td class="text-sm">${esc(m.concepto)}</td>
                <td class="num">${m.debe ? fmt(m.debe) : ''}</td>
                <td class="num">${m.haber ? fmt(m.haber) : ''}</td>
                <td class="num font-semibold">${fmt(m.saldo)}</td>
              </tr>`).join('')}
          </tbody>
          <tfoot>
            <tr>
              <td colspan="3">Totales</td>
              <td class="num">${fmt(bloque.totales.debe)}</td>
              <td class="num">${fmt(bloque.totales.haber)}</td>
              <td class="num">${fmt(bloque.totales.saldo_final)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>`).join('')
    + `
    <div class="card mb-4">
      <div class="card-body">
        <p class="text-sm text-surface-700">
          <strong>Total general del Libro Mayor:</strong>
          Debe <span class="font-mono">${fmt(datos.totales.debe)}</span> ·
          Haber <span class="font-mono">${fmt(datos.totales.haber)}</span>
          ${UI.badge(datos.totales.cuadra ? 'cuadra' : 'no cuadra', {
            variant: datos.totales.cuadra ? 'success' : 'danger',
            iconName: datos.totales.cuadra ? 'cheque' : 'cruz',
            className: 'ml-1',
          })}
        </p>
      </div>
    </div>`;
}

// ============================================================================
// Vista: Balance de comprobación
// ============================================================================

async function cargarBalance() {
  try {
    const datos = await api(`/api/balance${conPeriodo({
      vacias: $('#balance-vacias').checked ? 1 : 0,
      agrupado: $('#balance-agrupado').checked ? 1 : 0,
    })}`);

    $('#balance-cabecera').innerHTML = `
      <div class="flex flex-wrap items-center gap-2 pb-3">
        ${UI.badge(datos.cuadrado ? 'Balance cuadrado' : 'Balance descuadrado', {
          variant: datos.cuadrado ? 'success' : 'danger',
          iconName: datos.cuadrado ? 'cheque' : 'cruz',
        })}
        <span class="text-xs text-surface-500">${esc(datos.origen)}</span>
      </div>`;

    $('#balance-resultado').innerHTML = `
      <div class="card mb-4">
        <div class="table-shell">
          <table class="table">
            <thead>
              <tr>
                <th class="w-24">Código</th><th>Cuenta</th><th class="w-40">Clasificación</th>
                <th class="w-24">Naturaleza</th>
                <th class="w-32 text-right">Débitos</th><th class="w-32 text-right">Créditos</th>
                <th class="w-32 text-right">Saldo Deudor</th><th class="w-32 text-right">Saldo Acreedor</th>
              </tr>
            </thead>
            <tbody>
              ${datos.cuentas.length ? datos.cuentas.map((c) => `
                <tr>
                  <td class="font-mono text-xs text-surface-600">${esc(c.codigo)}</td>
                  <td class="${c.estado === 'INACTIVA' ? 'text-surface-400' : ''}">
                    ${esc(c.nombre)}
                    ${c.estado === 'INACTIVA'
                      ? UI.badge('Inactiva', { variant: 'secondary', className: 'ml-1' }) : ''}
                  </td>
                  <td class="text-xs text-surface-500">${c.clasificacion} · ${esc(c.clasificacion_nombre)}</td>
                  <td class="text-xs">${badgeNaturaleza(c.naturaleza, c.naturaleza_nombre)}</td>
                  <td class="num">${c.debe ? fmt(c.debe) : ''}</td>
                  <td class="num">${c.haber ? fmt(c.haber) : ''}</td>
                  <td class="num font-semibold">${c.saldo_deudor ? fmt(c.saldo_deudor) : ''}</td>
                  <td class="num font-semibold">${c.saldo_acreedor ? fmt(c.saldo_acreedor) : ''}</td>
                </tr>`).join('')
                : `<tr><td colspan="8" class="py-10 text-center text-surface-400">
                     No hay cuentas con movimientos en el período seleccionado.
                   </td></tr>`}
            </tbody>
            <tfoot>
              <tr>
                <td colspan="4">TOTALES</td>
                <td class="num">${fmt(datos.totales.debe)}</td>
                <td class="num">${fmt(datos.totales.haber)}</td>
                <td class="num">${fmt(datos.totales.saldo_deudor)}</td>
                <td class="num">${fmt(datos.totales.saldo_acreedor)}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>

      <div class="grid gap-4 lg:grid-cols-2">
        <div class="card">
          <div class="card-header"><h2 class="card-title">Verificaciones</h2></div>
          <div class="card-body space-y-2.5">
            ${Object.entries(datos.verificaciones).map(([clave, v]) => `
              <div class="flex items-start gap-2.5">
                ${UI.badge(v.cumple ? 'OK' : 'Falla', {
                  variant: v.cumple ? 'success' : 'danger',
                  iconName: v.cumple ? 'cheque' : 'cruz',
                })}
                <p class="text-sm ${v.cumple ? 'text-surface-700' : 'text-danger-700'}">${esc(v.mensaje)}</p>
              </div>`).join('')}
            <div class="flex items-start gap-2.5">
              ${UI.badge(datos.cruce_libro_mayor.coincide ? 'OK' : 'Falla', {
                variant: datos.cruce_libro_mayor.coincide ? 'success' : 'danger',
                iconName: datos.cruce_libro_mayor.coincide ? 'cheque' : 'cruz',
              })}
              <p class="text-sm text-surface-700">
                ${esc(datos.cruce_libro_mayor.mensaje)} (control 4 y 5)
              </p>
            </div>
          </div>
        </div>

        ${datos.por_clasificacion ? `
          <div class="card">
            <div class="card-header">
              <h2 class="card-title">Totales por clasificación</h2>
              ${UI.badge('Control 6', { variant: 'primary', className: 'ml-auto' })}
            </div>
            <div class="table-shell">
              <table class="table">
                <thead>
                  <tr>
                    <th>Clasificación</th><th class="w-28">Naturaleza</th>
                    <th class="w-20 text-right">Cuentas</th>
                    <th class="w-28 text-right">Débitos</th>
                    <th class="w-28 text-right">Créditos</th>
                    <th class="w-32 text-right">Saldo</th>
                  </tr>
                </thead>
                <tbody>
                  ${datos.por_clasificacion.filter((g) => g.cuentas).map((g) => `
                    <tr>
                      <td class="font-medium">${esc(g.nombre)}</td>
                      <td>${badgeNaturaleza(g.naturaleza, g.naturaleza_nombre)}</td>
                      <td class="num">${g.cuentas}</td>
                      <td class="num">${fmt(g.debe, false)}</td>
                      <td class="num">${fmt(g.haber, false)}</td>
                      <td class="num font-semibold">${fmt(g.saldo_natural, false)}</td>
                    </tr>`).join('')}
                </tbody>
              </table>
            </div>
          </div>` : ''}
      </div>`;
  } catch (error) {
    avisarError(error);
  }
}

// ============================================================================
// Vista: Controles y reglas
// ============================================================================

async function cargarControles() {
  try {
    const datos = await api('/api/controles');

    $('#controles-tbody').innerHTML = datos.controles.map((c) => `
      <tr>
        <td>
          ${UI.badge(c.codigo, {
            variant: c.cumple ? 'success' : 'danger',
            iconName: c.cumple ? 'cheque' : 'cruz',
          })}
        </td>
        <td class="font-medium text-surface-800">${esc(c.titulo)}</td>
        <td class="text-sm text-surface-600">${esc(c.requisito)}</td>
        <td class="text-xs text-surface-500">${esc(c.implementacion)}</td>
        <td class="text-xs text-surface-500">${esc(c.detalle)}</td>
      </tr>`).join('');

    $('#trazabilidad-tbody').innerHTML = datos.trazabilidad.map((t) => `
      <tr>
        <td class="font-medium text-surface-800">${esc(t.reporte)}</td>
        <td class="font-mono text-xs">${t.tablas.map((x) => esc(x)).join(' + ')}</td>
        <td class="text-sm text-surface-600">${esc(t.descripcion)}</td>
        <td class="text-center">
          ${t.carga_manual
            ? UI.badge('Sí', { variant: 'warning', iconName: 'alerta' })
            : UI.badge('Imposible', { variant: 'success', iconName: 'candado' })}
        </td>
      </tr>`).join('');
  } catch (error) {
    avisarError(error);
  }
}

// ============================================================================
// Utilidades: datos de prueba
// ============================================================================

async function cargarDemo(recrear) {
  try {
    const resultado = await api('/api/datos-demo', {
      method: 'POST', body: { recrear },
    });
    const cuadre = resultado.cuadre_libro_diario;
    UI.toast({
      variant: 'success',
      title: 'Datos de prueba listos',
      description: `${resultado.cuentas_creadas} cuentas y ${resultado.asientos_creados} `
        + `asientos cargados. Total Debe ${fmt(cuadre.total_debe)} = Total Haber ${fmt(cuadre.total_haber)}.`,
    });
    await Promise.all([cargarEmpresa(), cargarCuentas(), cargarPanel(), cargarDiario()]);
  } catch (error) {
    avisarError(error);
  }
}

// ============================================================================
// Arranque de la página
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
  $('#btn-cargar-demo').onclick = async () => {
    const aceptado = await UI.confirmar({
      titulo: 'Cargar los datos de prueba',
      descripcion: 'Se cargan las 21 cuentas del plan mínimo del enunciado y las 15 '
        + 'operaciones de la empresa ficticia. Los asientos y cuentas actuales se borran.',
      confirmarTexto: 'Cargar datos',
      icono: 'descargar',
    });
    if (aceptado) cargarDemo(true);
  };

  $('#btn-reiniciar').onclick = async () => {
    const aceptado = await UI.confirmar({
      titulo: 'Vaciar la base de datos',
      descripcion: 'Esto borra TODOS los asientos y todas las cuentas. No se puede deshacer.',
      confirmarTexto: 'Vaciar todo',
      icono: 'papelera',
    });
    if (!aceptado) return;
    try {
      await api('/api/reiniciar', { method: 'POST' });
      avisar('Base de datos vaciada.', 'success');
      await Promise.all([cargarEmpresa(), cargarCuentas(), cargarPanel()]);
    } catch (error) {
      avisarError(error);
    }
  };

  $('#cuenta-busqueda').oninput = debounce(cargarCuentas, 300);
  $('#cuenta-inactivas').onchange = cargarCuentas;
  $('#diario-estado').onchange = cargarDiario;
  $('#diario-busqueda').oninput = debounce(cargarDiario, 300);
  $('#btn-nuevo-asiento').onclick = () => {
    $('#diario-detalle').classList.add('oculto');
    iniciarFormulario();
    $('#form-asiento').scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  $('#btn-cancelar-asiento').onclick = () => iniciarFormulario();

  $('#btn-ver-mayor').onclick = () => cargarMayor(false);
  $('#btn-mayor-todas').onclick = () => cargarMayor(true);
  $('#btn-ver-balance').onclick = cargarBalance;
  $('#balance-vacias').onchange = cargarBalance;
  $('#balance-agrupado').onchange = cargarBalance;

  $$('[data-accion="imprimir"]').forEach((boton) => {
    boton.onclick = () => window.print();
  });

  iniciar();
});