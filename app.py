"""
app.py — La Terminal Óptica. Fase 3.

Dos estados: un buscador negro y un dashboard.

Decisiones que no son estéticas:

  PRECIO REAL POR DEFECTO. Un activo solo se muestra en índice base 100
  cuando hay más de una serie en pantalla, porque comparar dos escalas
  distintas en un mismo eje exige normalizarlas. Con una sola serie no hay
  nada que normalizar y el número que quieres ver es el precio.

  NUNCA DOS EJES Y. Dos series con escalas independientes parecen
  correlacionadas o ajenas según cómo se escale cada eje. Por eso al añadir
  una comparación el gráfico pasa solo a base 100: un eje, una escala.

  LA LÍNEA SE ROMPE EN LOS HUECOS. Unir los extremos de un hueco de 233
  días dibuja una tendencia que nunca pasó.

  LA PROYECCIÓN LLEVA SU PROPIA DUDA. La banda se abre con la raíz del
  tiempo porque así crece la incertidumbre de verdad, y la consola dice si
  la tendencia estimada se distingue del azar. Casi nunca se distingue.

Uso:
    python app.py          →  http://127.0.0.1:8050
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

from calendario import factores_por_dia
from comun import conectar_lectura, leer_todo

# ---------------------------------------------------------------------------
# Paleta, validada contra fondo negro para visión normal y las tres formas de
# daltonismo. Los grises no tienen saturación: ninguna CVD los confunde con
# el verde o el rojo del activo principal.
# ---------------------------------------------------------------------------
NEGRO, PANEL, BORDE = "#000000", "#0A0A0C", "#232329"
TEXTO, TEXTO_2, TENUE = "#F0F0F2", "#B4B4BC", "#8A8A94"
VERDE, ROJO = "#00BF63", "#FF3131"
CONTEXTO = ["#C2C2C6", "#8A8A92", "#63636B"]
TRAZOS = ["dash", "dot", "dashdot"]
BANDA_SHOCK, BANDA_ESTRES = "rgba(255,49,49,0.11)", "rgba(255,49,49,0.05)"
MONO = "'JetBrains Mono','Cascadia Mono',Consolas,ui-monospace,monospace"

VENTANAS = {"1M": 30, "6M": 182, "1A": 365, "3A": 1095, "MÁX": 20000}
HORIZONTES = {"sin proyección": 0, "+1M": 21, "+3M": 63, "+6M": 126}
DERIVAS = {"deriva cero": "cero", "deriva histórica": "historica"}

sb = conectar_lectura()

ACTIVOS = {a["ticker"]: a for a in sb.table("activos")
           .select("ticker,nombre,tipo,region,moneda,frecuencia,fuente_datos")
           .eq("activo", True).order("ticker").execute().data}

REGIMEN = pd.DataFrame(leer_todo(sb, "regimenes_mercado", "fecha,estado,vix_cierre",
                                 orden="fecha"))
if not REGIMEN.empty:
    REGIMEN["fecha"] = pd.to_datetime(REGIMEN["fecha"])
    REG_SERIE = REGIMEN.set_index("fecha")["estado"]
else:
    REG_SERIE = pd.Series(dtype=object)

_cache: dict[str, pd.Series] = {}


# ---------------------------------------------------------------------------
def serie(ticker: str) -> pd.Series:
    if ticker not in _cache:
        filas = leer_todo(sb, "precios_diarios", "fecha,cierre",
                          filtros={"ticker": ticker}, orden="fecha")
        s = pd.Series(dtype=float)
        if filas:
            s = pd.Series([float(f["cierre"]) for f in filas],
                          index=pd.to_datetime([f["fecha"] for f in filas]))
            s = s[~s.index.duplicated(keep="last")].sort_index()
        _cache[ticker] = s
    return _cache[ticker]


def cortar(s: pd.Series, dias: int) -> pd.Series:
    return s if s.empty else s[s.index >= (s.index.max() - pd.Timedelta(days=dias))]


def retornos(s: pd.Series) -> pd.Series:
    """
    Retornos logarítmicos, saltándose los precios no positivos.

    Hace falta por un dato que NO es un error: el 20 de abril de 2020 el
    WTI cerró en −37,63 dólares. Fue real — los tenedores del contrato de
    mayo pagaban por no recibir el barril. El logaritmo de un negativo no
    existe, así que ese salto se marca como indefinido en vez de dejar que
    pandas emita un NaN silencioso con un aviso por consola.
    """
    v = s.where(s > 0)
    return np.log(v / v.shift(1)).replace([np.inf, -np.inf], np.nan).dropna()


def romper_huecos(s: pd.Series, limite: int) -> pd.Series:
    """Un NaN en mitad de cada hueco para que Plotly corte la línea."""
    if len(s) < 2:
        return s
    piezas, anterior = [], None
    for fecha, valor in s.items():
        if anterior is not None and (fecha - anterior).days > limite:
            piezas.append((anterior + (fecha - anterior) / 2, np.nan))
        piezas.append((fecha, valor))
        anterior = fecha
    idx, val = zip(*piezas)
    return pd.Series(val, index=pd.DatetimeIndex(idx))


def formato(valor: float, moneda: str) -> str:
    if moneda == "PCT":
        return f"{valor:,.2f} %"
    if moneda == "COP":
        return f"$ {valor:,.2f} COP"
    if moneda == "USD":
        return f"$ {valor:,.2f}"
    return f"{valor:,.2f} {moneda}"


def correlacion_desfasada(a: pd.Series, b: pd.Series, max_lag: int = 5):
    ra = np.log(a / a.shift(1)).replace([np.inf, -np.inf], np.nan)
    rb = np.log(b / b.shift(1)).replace([np.inf, -np.inf], np.nan)
    j = pd.concat([ra, rb], axis=1, keys=["a", "b"]).dropna()
    if len(j) < 30:
        return None, None, len(j)
    mejor, mejor_r = 0, 0.0
    for lag in range(-max_lag, max_lag + 1):
        r = j["a"].shift(lag).corr(j["b"])
        if pd.notna(r) and abs(r) > abs(mejor_r):
            mejor, mejor_r = lag, r
    return mejor, mejor_r, len(j)


# ---------------------------------------------------------------------------
ESTADOS = ["normal", "estres", "shock"]
TAU = 0.10          # desviación a priori de la deriva anual, en tanto por uno


def _matriz_transicion() -> np.ndarray:
    """
    Con qué probabilidad el mercado pasa de un régimen a otro mañana.

    Importa porque los shocks se agrupan: un día de pánico casi nunca viene
    solo. Una simulación que sortease el régimen de forma independiente
    cada día borraría los tramos malos largos, que son justo los que hacen
    daño en una cartera.
    """
    P = np.full((3, 3), 1 / 3)
    if REG_SERIE.empty:
        return P
    idx = {e: i for i, e in enumerate(ESTADOS)}
    v = [idx[e] for e in REG_SERIE.values if e in idx]
    C = np.zeros((3, 3))
    for a, b in zip(v, v[1:]):
        C[a, b] += 1
    for i in range(3):
        if C[i].sum() > 0:
            P[i] = C[i] / C[i].sum()
    return P


P_TRANS = _matriz_transicion()


def proyectar(s: pd.Series, horizonte: int, semilla: int = 11,
              factores_evento: np.ndarray | None = None,
              modo_deriva: str = "cero"):
    """
    Espectro de trayectorias posibles, no una línea.

    Qué cambió y por qué. La versión anterior estimaba la deriva usando SOLO
    los días en régimen normal. Suena a tu idea —modelar la tendencia cuando
    no hay guerra— pero es un sesgo grave: los días de shock son
    abrumadoramente negativos, así que borrarlos equivale a pronosticar un
    mundo donde las caídas no existen. Medido sobre tus datos, triplicaba la
    deriva: el S&P 500 pasaba de 13,4 % a 44,5 % anual, y NVIDIA de 63,8 % a
    176,2 %. De ahí que todo apuntara al cielo.

    La forma correcta de expresar la misma idea no es BORRAR los shocks sino
    MODELARLOS. Aquí:

      1. Se estiman media y volatilidad de los retornos POR RÉGIMEN, sobre
         todo el histórico, sin descartar nada.
      2. Se simula el régimen futuro con una cadena de Markov estimada de la
         propia tabla, que conserva la persistencia: los shocks se agrupan.
      3. Se sortean 3.000 trayectorias y se leen sus percentiles.

    Y sobre la deriva: se encoge hacia cero con un factor bayesiano
    tau²/(tau²+ee²). El estimador de la media de retornos es notoriamente
    ruidoso —en una prueba con deriva real del 10 % anual devolvió 23,5 %—
    y el error estándar crece con la volatilidad, así que los activos más
    volátiles se encogen más. Que es exactamente lo que deben hacer.
    """
    limpia = s.dropna()
    if len(limpia) < 250 or horizonte <= 0:
        return None

    ret = retornos(limpia)
    if ret.empty or ret.std() == 0:
        return None

    est = REG_SERIE.reindex(ret.index)
    mu_r, sd_r = np.zeros(3), np.zeros(3)
    for i, e in enumerate(ESTADOS):
        sub = ret[est.values == e]
        if len(sub) >= 20:
            mu_r[i], sd_r[i] = sub.mean(), sub.std()
        else:                       # régimen sin muestra propia: se usa el global
            mu_r[i], sd_r[i] = ret.mean(), ret.std()

    # Deriva. Por defecto, CERO.
    #
    # No es pereza ni pesimismo: es lo que dicen los datos de este propio
    # proyecto. Las 24 series del universo tienen deriva histórica positiva,
    # las 24 sin excepción. Eso no describe a los activos, describe a la
    # ventana: 2016-2026 fue una década en la que subió casi todo.
    # Extrapolarla es asumir que la década se repite.
    #
    # A horizontes de uno a seis meses el retorno pasado no predice al
    # futuro, y el estimador es además pésimo: con deriva real del 10 %
    # devolvió 23,5 % en una prueba controlada. Un martingala —el mejor
    # pronóstico del precio de mañana es el de hoy— es la referencia
    # honesta, y deja que la forma del abanico la marque el riesgo, que
    # es lo único que sí sabemos estimar.
    anios = len(ret) / 252
    mu_anual = ret.mean() * 252
    sd_anual = ret.std() * np.sqrt(252)
    ee_anual = sd_anual / np.sqrt(anios)
    peso = TAU ** 2 / (TAU ** 2 + ee_anual ** 2)
    mu_objetivo = mu_anual * peso if modo_deriva == "historica" else 0.0

    # Distribución estacionaria de la cadena, para saber cuánta deriva
    # aporta la mezcla y corregirla al objetivo encogido.
    vals, vecs = np.linalg.eig(P_TRANS.T)
    pi = np.real(vecs[:, np.argmin(np.abs(vals - 1))])
    pi = pi / pi.sum() if pi.sum() else np.full(3, 1 / 3)
    mu_mezcla_anual = float(pi @ mu_r) * 252
    mu_r = mu_r + (mu_objetivo - mu_mezcla_anual) / 252

    # --- Simulación ---------------------------------------------------------
    rng = np.random.default_rng(semilla)
    n_sim = 3000
    hoy = REG_SERIE.iloc[-1] if not REG_SERIE.empty else "normal"
    estado = np.full(n_sim, ESTADOS.index(hoy) if hoy in ESTADOS else 0)
    s0 = float(limpia.iloc[-1])
    log_s = np.zeros(n_sim)
    caminos = np.empty((horizonte, n_sim))
    acum = np.zeros(3)

    # El ensanchamiento por evento se aplica DENTRO del bucle, día a día.
    # Inflar la banda entera daría una nube uniformemente ancha; aplicarlo
    # solo el día del FOMC produce el escalón real: calma, salto, calma.
    fe = (np.ones(horizonte) if factores_evento is None
          else np.asarray(factores_evento, dtype=float)[:horizonte])
    if len(fe) < horizonte:
        fe = np.concatenate([fe, np.ones(horizonte - len(fe))])

    for d in range(horizonte):
        u = rng.random(n_sim)
        cum = P_TRANS[estado].cumsum(axis=1)
        estado = (u[:, None] > cum).sum(axis=1).clip(0, 2)
        for i in range(3):
            acum[i] += (estado == i).sum()
        log_s += rng.normal(mu_r[estado], sd_r[estado] * fe[d])
        caminos[d] = log_s

    precios = s0 * np.exp(caminos)
    pcts = np.percentile(precios, [5, 25, 50, 75, 95], axis=1)

    return {
        "fechas": pd.bdate_range(limpia.index[-1] + pd.Timedelta(days=1),
                                 periods=horizonte),
        "p05": pcts[0], "p25": pcts[1], "p50": pcts[2],
        "p75": pcts[3], "p95": pcts[4],
        "s0": s0,
        "mu_bruta": (np.exp(mu_anual) - 1) * 100,
        "mu_usada": (np.exp(mu_objetivo) - 1) * 100,
        "encogimiento": peso,
        "sigma_anual": sd_anual * 100,
        "sd_por_regimen": sd_r * np.sqrt(252) * 100,
        "mu_por_regimen": (np.exp(mu_r * 252) - 1) * 100,
        "regimen_hoy": hoy,
        "prob_shock": acum[2] / (n_sim * horizonte) * 100,
        "n": len(ret),
        "prob_subir": float((precios[-1] > s0).mean()) * 100,
        "dias_con_evento": int((fe > 1.001).sum()),
        "factor_evento_max": float(fe.max()),
        "modo_deriva": modo_deriva,
        "prob_caida_5": float((precios[-1] < s0 * 0.95).mean()) * 100,
        "prob_caida_10": float((precios[-1] < s0 * 0.90).mean()) * 100,
    }


# ---------------------------------------------------------------------------
app = Dash(__name__, title="Terminal Óptica", update_title=None,
           external_stylesheets=[
               "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700"
               "&family=Roboto:wght@300;400;500&display=swap"])

app.index_string = """<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>
{%favicon%}{%css%}<style>
  html,body{margin:0;padding:0;background:#000;color:#F0F0F2;
    font-family:Roboto,-apple-system,sans-serif}
  *{box-sizing:border-box}
  ::selection{background:#00BF63;color:#000}
  .Select-control,.Select-menu-outer,.is-focused .Select-control,
  .Select--multi .Select-control{background:#0A0A0C!important;
    border-color:#232329!important;color:#F0F0F2!important}
  .Select-value-label,.Select-placeholder,.Select-input>input,
  .Select--single>.Select-control .Select-value .Select-value-label{color:#F0F0F2!important}
  .Select--multi .Select-value{background:#15151A!important;border-color:#2E2E36!important;
    color:#F0F0F2!important}
  .Select-placeholder{color:#8A8A94!important}
  .VirtualizedSelectOption{background:#0A0A0C;color:#B4B4BC}
  .VirtualizedSelectFocusedOption{background:#17171D;color:#00BF63}
  .btn-v{background:transparent;border:1px solid #232329;color:#B4B4BC;
    padding:5px 12px;margin-right:5px;cursor:pointer;font-size:11.5px;
    letter-spacing:.07em;font-family:'JetBrains Mono',monospace;border-radius:2px}
  .btn-v:hover{border-color:#8A8A94;color:#F0F0F2}
  .btn-v.on{border-color:#00BF63;color:#00BF63;background:rgba(0,191,99,.07)}
  .btn-v:focus-visible{outline:2px solid #00BF63;outline-offset:2px}
  #consola::-webkit-scrollbar{width:6px}
  #consola::-webkit-scrollbar-thumb{background:#232329}
</style></head><body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body></html>"""


def _pie() -> str:
    if REGIMEN.empty:
        return f"{len(ACTIVOS)} activos"
    u = REGIMEN.iloc[-1]
    vix = u["vix_cierre"]
    cola = f"   ·   VIX {float(vix):.1f}" if vix is not None else ""
    return f"{len(ACTIVOS)} activos   ·   régimen hoy: {u['estado'].upper()}{cola}"


buscador = html.Div(id="pantalla-buscador", children=[
    html.Div(style={"maxWidth": "660px", "margin": "0 auto", "padding": "22vh 24px 0"},
             children=[
        html.Div("TERMINAL ÓPTICA", style={
            "fontFamily": MONO, "fontSize": "14px", "letterSpacing": "0.4em",
            "color": TEXTO, "textAlign": "center", "marginBottom": "8px"}),
        html.Div("motor de inferencia causal", style={
            "fontFamily": MONO, "fontSize": "12px", "letterSpacing": "0.16em",
            "color": TEXTO_2, "textAlign": "center", "marginBottom": "44px"}),
        dcc.Dropdown(id="buscar", options=[
            {"label": f"{t}   {a['nombre']}", "value": t} for t, a in ACTIVOS.items()],
            placeholder="Buscar activo…", searchable=True, clearable=False,
            style={"fontFamily": MONO, "fontSize": "15px"}),
        html.Div(_pie(), style={
            "fontFamily": MONO, "fontSize": "12px", "color": TEXTO_2,
            "textAlign": "center", "marginTop": "30px", "letterSpacing": "0.04em"}),
    ])
])

barra = html.Div(style={"borderBottom": f"1px solid {BORDE}", "padding": "12px 20px",
                        "display": "flex", "alignItems": "center", "gap": "16px",
                        "flexWrap": "wrap"}, children=[
    html.Button("←", id="volver", className="btn-v", style={"padding": "5px 11px"}),
    html.Div(style={"minWidth": "210px"}, children=[
        html.Div(id="titulo", style={"fontFamily": MONO, "fontSize": "16px",
                                     "fontWeight": 700, "letterSpacing": "0.04em"}),
        html.Div(id="subtitulo", style={"fontFamily": MONO, "fontSize": "10.5px",
                                        "color": TEXTO_2, "marginTop": "2px"}),
    ]),
    html.Div(style={"minWidth": "190px"}, children=[
        html.Div(id="precio", style={"fontFamily": MONO, "fontSize": "23px",
                                     "fontWeight": 700, "lineHeight": "1.1"}),
        html.Div(id="cambio", style={"fontFamily": MONO, "fontSize": "11.5px",
                                     "marginTop": "3px"}),
    ]),
    html.Div(style={"flex": "1 1 auto", "minWidth": "220px"}, children=[
        dcc.Dropdown(id="comparar", options=[], multi=True, maxHeight=260,
                     placeholder="Comparar contra… (hasta 3)",
                     style={"fontFamily": MONO, "fontSize": "12px"}),
    ]),
    html.Div([
        html.Div([html.Button(k, id={"tipo": "ventana", "k": k}, className="btn-v",
                              n_clicks=0) for k in VENTANAS],
                 style={"marginBottom": "6px"}),
        html.Div([html.Button(k, id={"tipo": "horizonte", "k": k}, className="btn-v",
                              n_clicks=0) for k in HORIZONTES],
                 style={"marginBottom": "6px"}),
        html.Div([html.Button(k, id={"tipo": "deriva", "k": k}, className="btn-v",
                              n_clicks=0) for k in DERIVAS]),
    ]),
])

dashboard = html.Div(id="pantalla-dash", style={"display": "none"}, children=[
    barra,
    html.Div(style={"display": "flex", "alignItems": "stretch", "flexWrap": "wrap"},
             children=[
        html.Div(id="consola", style={
            "flex": "0 0 360px", "minWidth": "290px", "padding": "16px 18px",
            "borderRight": f"1px solid {BORDE}", "background": PANEL,
            "fontFamily": MONO, "fontSize": "11.5px", "lineHeight": "1.72",
            "color": TEXTO_2, "height": "calc(100vh - 92px)",
            "overflowY": "auto", "whiteSpace": "pre-wrap"}),
        html.Div(style={"flex": "1 1 540px", "minWidth": "320px", "padding": "6px 10px"},
                 children=[
            dcc.Graph(id="grafico", config={"displayModeBar": False},
                      style={"height": "calc(100vh - 104px)", "minHeight": "400px"}),
        ]),
    ]),
])

app.layout = html.Div([
    dcc.Store(id="estado", data={"ticker": None, "ventana": "1A",
                                 "horizonte": "sin proyección",
                                 "deriva": "deriva cero"}),
    buscador, dashboard])


# ---------------------------------------------------------------------------
@app.callback(
    Output("estado", "data"),
    Input("buscar", "value"),
    Input("volver", "n_clicks"),
    Input({"tipo": "ventana", "k": ALL}, "n_clicks"),
    Input({"tipo": "horizonte", "k": ALL}, "n_clicks"),
    Input({"tipo": "deriva", "k": ALL}, "n_clicks"),
    State("estado", "data"),
    prevent_initial_call=True,
)
def navegar(ticker, _v, _w, _h, _d, estado):
    d = ctx.triggered_id
    e = dict(estado or {})
    if d == "volver":
        e["ticker"] = None
    elif d == "buscar":
        e["ticker"] = ticker
    elif isinstance(d, dict):
        e[d["tipo"]] = d["k"]
    return e


@app.callback(
    Output("pantalla-buscador", "style"),
    Output("pantalla-dash", "style"),
    Output("buscar", "value"),
    Input("estado", "data"),
)
def cambiar_pantalla(e):
    if e and e.get("ticker"):
        return {"display": "none"}, {"display": "block"}, no_update
    return {"display": "block"}, {"display": "none"}, None


@app.callback(
    Output({"tipo": "ventana", "k": ALL}, "className"),
    Output({"tipo": "horizonte", "k": ALL}, "className"),
    Output({"tipo": "deriva", "k": ALL}, "className"),
    Input("estado", "data"),
)
def marcar(e):
    v = (e or {}).get("ventana", "1A")
    h = (e or {}).get("horizonte", "sin proyección")
    d = (e or {}).get("deriva", "deriva cero")
    return ([("btn-v on" if k == v else "btn-v") for k in VENTANAS],
            [("btn-v on" if k == h else "btn-v") for k in HORIZONTES],
            [("btn-v on" if k == d else "btn-v") for k in DERIVAS])


@app.callback(
    Output("comparar", "options"),
    Output("comparar", "value"),
    Input("estado", "data"),
    State("comparar", "value"),
)
def opciones_comparar(e, actuales):
    t = (e or {}).get("ticker")
    if not t:
        return [], []
    opts = [{"label": f"{k}  {v['nombre'][:32]}", "value": k}
            for k, v in ACTIVOS.items() if k != t]
    return opts, [c for c in (actuales or []) if c != t][:3]


@app.callback(
    Output("titulo", "children"),
    Output("subtitulo", "children"),
    Output("precio", "children"),
    Output("precio", "style"),
    Output("cambio", "children"),
    Output("cambio", "style"),
    Output("consola", "children"),
    Output("grafico", "figure"),
    Input("estado", "data"),
    Input("comparar", "value"),
)
def pintar(estado, comparaciones):
    estilo_precio = {"fontFamily": MONO, "fontSize": "23px", "fontWeight": 700,
                     "lineHeight": "1.1"}
    estilo_cambio = {"fontFamily": MONO, "fontSize": "11.5px", "marginTop": "3px"}

    t = (estado or {}).get("ticker")
    if not t:
        return "", "", "", estilo_precio, "", estilo_cambio, "", go.Figure()

    meta = ACTIVOS[t]
    nombre_v = (estado or {}).get("ventana", "1A")
    nombre_h = (estado or {}).get("horizonte", "sin proyección")
    dias, horizonte = VENTANAS.get(nombre_v, 365), HORIZONTES.get(nombre_h, 0)
    comparaciones = [c for c in (comparaciones or []) if c != t][:3]

    completa = serie(t)
    principal = cortar(completa, dias)
    if principal.empty:
        vacio = go.Figure(layout=dict(paper_bgcolor=NEGRO, plot_bgcolor=NEGRO))
        return (t, meta["nombre"], "—", estilo_precio, "", estilo_cambio,
                "Sin precios cargados para este activo.", vacio)

    ret = (principal.iloc[-1] / principal.iloc[0] - 1) * 100
    color = VERDE if ret >= 0 else ROJO
    estilo_precio["color"] = TEXTO
    estilo_cambio["color"] = color

    # Con una sola serie no hay nada que normalizar: se muestra el precio.
    # Con dos o más, el índice base 100 es la única forma honesta de ponerlas
    # en un mismo eje.
    modo_indice = len(comparaciones) > 0
    lim = 45 if meta["frecuencia"] == "mensual" else 15

    def preparar(s: pd.Series, base: float | None) -> pd.Series:
        s = romper_huecos(s, lim)
        return s / base * 100 if base else s

    fig = go.Figure()

    # Bandas de régimen
    if not REGIMEN.empty:
        r = REGIMEN[REGIMEN["fecha"] >= principal.index.min()]
        fechas_r, n_r = r["fecha"].values, len(r)
        for est, col in (("shock", BANDA_SHOCK), ("estres", BANDA_ESTRES)):
            m, i, bl = (r["estado"] == est).values, 0, 0
            while i < n_r and bl < 80:
                if m[i]:
                    j = i
                    while j + 1 < n_r and m[j + 1]:
                        j += 1
                    fig.add_vrect(x0=fechas_r[i], x1=fechas_r[j], fillcolor=col,
                                  line_width=0, layer="below")
                    bl += 1
                    i = j + 1
                else:
                    i += 1

    etiquetas = []

    def añadir(tk: str, col: str, trazo, ancho: float):
        s = cortar(serie(tk), dias)
        if s.empty:
            return
        base = s.dropna().iloc[0] if modo_indice else None
        d = preparar(s, base)
        mon = ACTIVOS[tk]["moneda"]
        ht = (f"<b>{tk}</b>  %{{y:.1f}}<extra></extra>" if modo_indice
              else f"<b>{tk}</b>  %{{y:,.2f}} {mon}<extra></extra>")
        fig.add_trace(go.Scatter(x=d.index, y=d.values, name=tk, mode="lines",
                                 line=dict(color=col, width=ancho, dash=trazo),
                                 connectgaps=False, hovertemplate=ht))
        u = d.dropna()
        if not u.empty:
            etiquetas.append((u.index[-1], u.iloc[-1], tk, col))

    añadir(t, color, None, 2.2)
    for i, c in enumerate(comparaciones):
        añadir(c, CONTEXTO[i % 3], TRAZOS[i % 3], 1.6)

    # ---- Proyección -------------------------------------------------------
    pr = None
    if horizonte:
        futuras = pd.bdate_range(completa.dropna().index[-1] + pd.Timedelta(days=1),
                                 periods=horizonte)
        try:
            fe = factores_por_dia(sb, t, futuras)
        except Exception:
            fe = None          # sin calendario cargado, se proyecta sin eventos
        pr = proyectar(completa, horizonte, factores_evento=fe,
                       modo_deriva=DERIVAS.get((estado or {}).get("deriva",
                                                                  "deriva cero"), "cero"))
    if pr:
        esc = (100 / principal.dropna().iloc[0]) if modo_indice else 1.0
        fx = pr["fechas"]
        gris = "rgba(190,190,198,"
        # Dos bandas concéntricas: el 90 % central y el 50 % central. El
        # abanico es el mensaje — no hay una línea, hay un rango de futuros.
        for alto, bajo, opa, nom in ((pr["p95"], pr["p05"], "0.09", "90 % central"),
                                     (pr["p75"], pr["p25"], "0.16", "50 % central")):
            fig.add_trace(go.Scatter(x=fx, y=alto * esc, mode="lines",
                                     line=dict(width=0), showlegend=False,
                                     hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=fx, y=bajo * esc, mode="lines",
                                     line=dict(width=0), fill="tonexty",
                                     fillcolor=f"{gris}{opa})", name=nom,
                                     hoverinfo="skip"))
        fig.add_trace(go.Scatter(
            x=[principal.index[-1], *fx],
            y=[(principal.iloc[-1] * esc), *(pr["p50"] * esc)],
            mode="lines", name="mediana",
            line=dict(color=CONTEXTO[0], width=1.6, dash="dot"),
            hovertemplate="mediana %{y:,.2f}<extra></extra>"))
        fig.add_vline(x=principal.index[-1], line=dict(color=TENUE, width=1, dash="dot"),
                      opacity=0.6)

    for x, y, nm, col in etiquetas:
        fig.add_annotation(x=x, y=y, text=f" {nm}", showarrow=False, xanchor="left",
                           font=dict(family=MONO, size=11, color=col))

    fig.update_layout(
        paper_bgcolor=NEGRO, plot_bgcolor=NEGRO,
        font=dict(family=MONO, size=11, color=TEXTO_2),
        margin=dict(l=54, r=78, t=14, b=34), hovermode="x unified",
        hoverlabel=dict(bgcolor=PANEL, bordercolor=BORDE,
                        font=dict(family=MONO, size=11, color=TEXTO)),
        showlegend=len(etiquetas) >= 2,
        legend=dict(orientation="h", y=1.07, x=0, bgcolor="rgba(0,0,0,0)",
                    font=dict(size=10.5)),
        xaxis=dict(showgrid=False, linecolor=BORDE, tickcolor=BORDE),
        yaxis=dict(title=dict(text="base 100" if modo_indice else meta["moneda"],
                              font=dict(size=10, color=TENUE)),
                   showgrid=True, gridcolor="#101014", zeroline=False,
                   linecolor=BORDE, tickcolor=BORDE),
    )
    if modo_indice:
        fig.add_hline(y=100, line=dict(color=TENUE, width=1, dash="dot"), opacity=0.5)

    # ---- Consola ----------------------------------------------------------
    L = ["PROCEDENCIA",
         f"  fuente      {meta['fuente_datos']}",
         f"  frecuencia  {meta['frecuencia']}",
         f"  histórico   {completa.index.min():%Y-%m-%d} → {completa.index.max():%Y-%m-%d}",
         f"  filas       {len(completa):,}",
         "",
         f"VENTANA  {nombre_v}",
         f"  desde       {principal.index.min():%Y-%m-%d}",
         f"  cierre      {formato(float(principal.iloc[-1]), meta['moneda'])}",
         f"  variación   {ret:+.2f} %"]
    vol = retornos(principal).std() * np.sqrt(252) * 100
    if pd.notna(vol):
        L.append(f"  volatilidad {vol:.1f} % anualizada")
    if modo_indice:
        L += ["", "  Escala en base 100: con más de una serie",
              "  es la única forma de compararlas en un eje."]

    if pr:
        m = meta["moneda"]
        L += ["", f"ESPECTRO  {nombre_h}",
              "  método      mezcla por régimen, 3.000 caminos",
              f"  muestra     {pr['n']:,} días",
              f"  parte de    régimen {pr['regimen_hoy']}",
              "",
              f"  p95         {formato(float(pr['p95'][-1]), m)}",
              f"  p75         {formato(float(pr['p75'][-1]), m)}",
              f"  mediana     {formato(float(pr['p50'][-1]), m)}",
              f"  p25         {formato(float(pr['p25'][-1]), m)}",
              f"  p05         {formato(float(pr['p05'][-1]), m)}",
              "",
              f"  prob. de terminar por encima de hoy: {pr['prob_subir']:.0f} %",
              f"  días en shock simulados: {pr['prob_shock']:.1f} %"]
        if pr["dias_con_evento"]:
            L += ["",
                  f"  {pr['dias_con_evento']} día(s) con evento de calendario",
                  f"  ensanchan la banda hasta x{pr['factor_evento_max']:.2f}",
                  "  Ese escalón no lo estima ningún modelo:",
                  "  es el movimiento histórico medido en",
                  "  fechas que ya están publicadas."]
        L += ["",
              "RIESGO A LA BAJA",
              f"  caer más de  5 %:  {pr['prob_caida_5']:.0f} %",
              f"  caer más de 10 %:  {pr['prob_caida_10']:.0f} %",
              "",
              "DERIVA",
              f"  observada   {pr['mu_bruta']:+.1f} % anual",
              f"  usada       {pr['mu_usada']:+.1f} % anual"]
        if pr["modo_deriva"] == "cero":
            L += ["",
                  "  Deriva forzada a cero, y es lo defendible.",
                  "  Los 24 activos de este universo tienen",
                  "  deriva histórica POSITIVA, los 24 sin una",
                  "  sola excepción. Eso no describe a los",
                  "  activos: describe la ventana 2016-2026,",
                  "  una década en la que subió casi todo.",
                  "  Extrapolarla es asumir que se repite.",
                  "",
                  "  Con deriva cero el mejor pronóstico del",
                  "  precio de mañana es el de hoy, y la forma",
                  "  del abanico la marca solo el riesgo — que",
                  "  es lo único que sí se estima bien."]
        else:
            L += ["",
                  f"  Encogida al {pr['encogimiento']*100:.0f} % de la observada. Aun",
                  "  así, ojo: las 24 derivas del universo son",
                  "  positivas. Estás viendo la década pasada",
                  "  proyectada, no una predicción."]
        L += ["",
              "POR RÉGIMEN  (deriva / volatilidad anual)"]
        for i, e_ in enumerate(ESTADOS):
            L.append(f"  {e_:<8} {pr['mu_por_regimen'][i]:+8.1f} %"
                     f"   {pr['sd_por_regimen'][i]:6.1f} %")
        L += ["",
              "  Los shocks NO se borran del cálculo, se",
              "  simulan. Filtrarlos triplicaba la deriva:",
              "  medido en tus datos, el S&P pasaba de 13,4 %",
              "  a 44,5 % anual. Por eso todo apuntaba arriba.",
              "",
              "  Nada de esto anticipa CUÁNDO llega un shock.",
              "  Solo dice con qué frecuencia han ocurrido."]
    elif horizonte:
        L += ["", "ESPECTRO", "  Hacen falta al menos 250 días de",
              "  histórico para estimar los regímenes."]

    if not REGIMEN.empty:
        r = REGIMEN[REGIMEN["fecha"] >= principal.index.min()]
        if len(r):
            c = r["estado"].value_counts()
            L += ["", "RÉGIMEN EN LA VENTANA"]
            for e_ in ("normal", "estres", "shock"):
                n = int(c.get(e_, 0))
                L.append(f"  {e_:<8} {n:>5} días  {100*n/len(r):5.1f} %")

    fechas = principal.index
    huecos = [(a, b, (b - a).days) for a, b in zip(fechas, fechas[1:])
              if (b - a).days > lim]
    if huecos:
        L += ["", "HUECOS EN ESTA VENTANA"]
        for a, b, d_ in huecos[:4]:
            L.append(f"  {a:%Y-%m-%d} → {b:%Y-%m-%d}  ({d_} días)")
        L += ["  La línea se corta ahí a propósito.",
              "  Ningún retorno de ese tramo es real."]

    if meta["frecuencia"] == "mensual":
        L += ["", "AVISO DE FRECUENCIA",
              "  Serie mensual. Los tramos entre puntos",
              "  son interpolación visual, no datos."]

    if comparaciones:
        L += ["", "SINAPSIS  ·  correlación de retornos"]
        for c in comparaciones:
            lag, r_, n = correlacion_desfasada(cortar(serie(t), dias),
                                               cortar(serie(c), dias))
            if r_ is None:
                L.append(f"  {c:<10} muestra insuficiente ({n} d)")
                continue
            quien = (f"{t} adelanta {lag}d" if lag > 0
                     else f"{c} adelanta {-lag}d" if lag < 0 else "simultáneo")
            L.append(f"  {c:<10} r={r_:+.2f}  {quien}  (n={n})")
        L += ["", "  Precedencia temporal, no causa. Con 25",
              "  activos hay 300 pares: algunos van a",
              "  correlacionar por azar. La causalidad",
              "  llega en la fase 5 y solo vale si supera",
              "  al azar en la fase 6."]

    precio_txt = formato(float(principal.iloc[-1]), meta["moneda"])
    cambio_txt = f"{ret:+.2f} %   {nombre_v}   ·   {completa.index.max():%d %b %Y}"
    subt = f"{meta['nombre']}  ·  {meta['tipo']}  ·  {meta['region']}"

    return (t, subt, precio_txt, estilo_precio, cambio_txt, estilo_cambio,
            "\n".join(L), fig)


if __name__ == "__main__":
    print(f"\n  TERMINAL ÓPTICA  ·  {len(ACTIVOS)} activos")
    print("  http://127.0.0.1:8050\n")
    app.run(debug=False, port=8050)
