-- =====================================================================
-- MOTOR DE INFERENCIA CAUSAL — esquema completo, tal como está aplicado
-- Proyecto Supabase: motor-causal (us-east-1)
--
-- Este archivo es la referencia legible. Ya está aplicado en la nube;
-- lo guardas en git para saber qué cambió y cuándo.
-- =====================================================================

-- ---------------------------------------------------------------------
-- FUENTES — la defensa contra la superstición financiera.
-- Toda noticia entra por una fuente con nivel de confianza declarado.
-- Nivel 1 = documento primario (comunicado, acta, rueda de prensa).
-- Nivel 4 = opinión. No se ingesta por defecto.
-- ---------------------------------------------------------------------
create table fuentes (
  id              smallserial primary key,
  nombre          text not null unique,
  url_feed        text,
  tipo            text not null
                  check (tipo in ('gobierno','banco_central','organismo_multilateral',
                                  'agencia_noticias','medio_financiero','opinion')),
  nivel_confianza smallint not null check (nivel_confianza between 1 and 4),
  pais            text,
  idioma          text default 'en',
  activa          boolean not null default true,
  url_verificada  boolean not null default false
);

-- ---------------------------------------------------------------------
-- ACTIVOS — universo deliberadamente pequeño en v1 (21).
-- Ampliar solo cuando v_precision_por_metodo dé razones para hacerlo.
-- ---------------------------------------------------------------------
create table activos (
  ticker        text primary key,
  nombre        text not null,
  tipo          text not null
                check (tipo in ('accion','indice','etf','materia_prima',
                                'divisa','volatilidad','tasa')),
  region        text,
  moneda        text default 'USD',
  fuente_datos  text not null default 'yfinance',
  verificado    boolean not null default false,
  activo        boolean not null default true,
  creado_en     timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- PRECIOS DIARIOS — la tabla que más crece.
-- Clave compuesta, sin id serial: ahorra espacio e impone idempotencia.
-- 21 activos x 10 años ~ 53 000 filas ~ 8 MB. Cabe de sobra en 500 MB.
-- ---------------------------------------------------------------------
create table precios_diarios (
  ticker      text not null references activos(ticker) on delete cascade,
  fecha       date not null,
  apertura    numeric(14,4),
  maximo      numeric(14,4),
  minimo      numeric(14,4),
  cierre      numeric(14,4) not null,
  volumen     bigint,
  primary key (ticker, fecha)
);
create index idx_precios_fecha on precios_diarios (fecha desc);

-- ---------------------------------------------------------------------
-- REGÍMENES DE MERCADO — distingue "día normal" de "shock".
-- El modelo de tendencia solo pretende ser válido en régimen normal.
-- Esta tabla hace explícita esa condición en vez de esconderla.
-- ---------------------------------------------------------------------
create table regimenes_mercado (
  fecha           date primary key,
  vix_cierre      numeric(8,4),
  vol_realizada   numeric(8,5),
  estado          text not null check (estado in ('normal','estres','shock')),
  nota            text,
  calculado_en    timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- NOTICIAS — solo texto. Embeddings (pgvector) en la fase 7.
-- ---------------------------------------------------------------------
create table noticias (
  id            bigserial primary key,
  fuente_id     smallint not null references fuentes(id),
  publicado_en  timestamptz not null,
  titular       text not null,
  cuerpo        text,
  url           text unique,              -- deduplicación gratis
  es_primaria   boolean not null default false,
  procesada_en  timestamptz,              -- null = pendiente del lote nocturno
  creado_en     timestamptz not null default now()
);
create index idx_noticias_pendientes on noticias (publicado_en) where procesada_en is null;
create index idx_noticias_fecha on noticias (publicado_en desc);

-- ---------------------------------------------------------------------
-- RELACIONES CAUSALES — el grafo.
-- Cada peso es una hipótesis firmada por un modelo y una versión de prompt.
-- NO es un parámetro calibrado y NO es comparable entre modelos.
-- ---------------------------------------------------------------------
create table relaciones_causales (
  id            bigserial primary key,
  noticia_id    bigint not null references noticias(id) on delete cascade,
  ticker        text not null references activos(ticker) on delete cascade,
  direccion     smallint not null check (direccion in (-1,0,1)),
  peso          numeric(4,3) not null check (peso >= 0 and peso <= 1),
  horizonte_d   smallint,
  razonamiento  text,                     -- alimenta la Terminal de Sinapsis
  modelo        text not null,
  prompt_ver    text not null,
  creado_en     timestamptz not null default now(),
  unique (noticia_id, ticker, prompt_ver) -- reprocesar no borra el historial
);
create index idx_relaciones_ticker on relaciones_causales (ticker, creado_en desc);

-- ---------------------------------------------------------------------
-- PREDICCIONES — lo que convierte el juguete en instrumento.
--
-- `metodo` es el corazón del experimento:
--   baseline_naive      random walk. El retorno esperado es cero.
--   baseline_tendencia  modelo estadístico puro sobre el histórico.
--   llm_ajustado        el mismo, corregido por el grafo causal.
--   ensamble            combinación de los anteriores.
-- Si llm_ajustado no supera a baseline_tendencia de forma sostenida,
-- el LLM no está aportando información: está aportando prosa.
-- ---------------------------------------------------------------------
create table predicciones (
  id              bigserial primary key,
  ticker          text not null references activos(ticker) on delete cascade,
  emitida_en      date not null,
  horizonte_d     smallint not null check (horizonte_d > 0),
  metodo          text not null
                  check (metodo in ('baseline_naive','baseline_tendencia',
                                    'llm_ajustado','ensamble')),
  direccion       smallint not null check (direccion in (-1,0,1)),
  retorno_esp     numeric(8,5),
  banda_baja      numeric(8,5),   -- una predicción sin intervalo es una opinión
  banda_alta      numeric(8,5),
  confianza       numeric(4,3) check (confianza >= 0 and confianza <= 1),
  modelo          text not null,
  regimen_emision text check (regimen_emision in ('normal','estres','shock')),
  regimen_resol   text check (regimen_resol in ('normal','estres','shock')),
  retorno_real    numeric(8,5),   -- lo rellena el job semanal
  acertada        boolean,
  resuelta_en     date,
  creado_en       timestamptz not null default now(),
  unique (ticker, emitida_en, horizonte_d, metodo, modelo)
);
create index idx_pred_pendientes on predicciones (emitida_en) where resuelta_en is null;
create index idx_pred_ticker on predicciones (ticker, emitida_en desc);

-- ---------------------------------------------------------------------
-- INGESTA LOG — hace que un fallo sea visible en vez de silencioso.
-- ---------------------------------------------------------------------
create table ingesta_log (
  id            bigserial primary key,
  proceso       text not null,
  ticker        text references activos(ticker) on delete cascade,
  ejecutado_en  timestamptz not null default now(),
  exito         boolean not null,
  filas         integer,
  error         text
);
create index idx_ingesta_reciente on ingesta_log (proceso, ejecutado_en desc);

-- =====================================================================
-- RLS: el frontend Dash usa la clave anon y SOLO puede leer.
-- Toda escritura pasa por los scripts con service_role, que salta RLS.
-- Así una clave filtrada no borra la base.
-- =====================================================================
alter table fuentes             enable row level security;
alter table activos             enable row level security;
alter table precios_diarios     enable row level security;
alter table regimenes_mercado   enable row level security;
alter table noticias            enable row level security;
alter table relaciones_causales enable row level security;
alter table predicciones        enable row level security;
alter table ingesta_log         enable row level security;

create policy "lectura publica" on fuentes             for select to anon, authenticated using (true);
create policy "lectura publica" on activos             for select to anon, authenticated using (true);
create policy "lectura publica" on precios_diarios     for select to anon, authenticated using (true);
create policy "lectura publica" on regimenes_mercado   for select to anon, authenticated using (true);
create policy "lectura publica" on noticias            for select to anon, authenticated using (true);
create policy "lectura publica" on relaciones_causales for select to anon, authenticated using (true);
create policy "lectura publica" on predicciones        for select to anon, authenticated using (true);
create policy "lectura publica" on ingesta_log         for select to anon, authenticated using (true);

-- =====================================================================
-- VISTAS. security_invoker = las políticas RLS siguen aplicando.
-- =====================================================================

-- El marcador del motor. Responde a la única pregunta que importa:
-- ¿el LLM le gana al modelo estadístico, o no?
create view v_precision_por_metodo with (security_invoker = true) as
select
  p.metodo, p.modelo, p.horizonte_d, p.regimen_emision,
  count(*)                                           as resueltas,
  count(*) filter (where p.acertada)                 as aciertos,
  round(100.0 * count(*) filter (where p.acertada)
        / nullif(count(*),0), 1)                     as tasa_acierto_pct,
  round(avg(abs(p.retorno_real - p.retorno_esp))::numeric, 5) as error_abs_medio,
  min(p.emitida_en) as desde,
  max(p.emitida_en) as hasta
from predicciones p
where p.resuelta_en is not null
group by p.metodo, p.modelo, p.horizonte_d, p.regimen_emision;

-- Estado de la ingesta de un vistazo: qué activo lleva días sin actualizarse.
create view v_salud_ingesta with (security_invoker = true) as
select
  a.ticker, a.nombre, a.verificado,
  max(pd.fecha)                as ultimo_precio,
  current_date - max(pd.fecha) as dias_de_retraso,
  count(pd.fecha)              as filas_totales
from activos a
left join precios_diarios pd on pd.ticker = a.ticker
where a.activo
group by a.ticker, a.nombre, a.verificado;

-- Última foto de cada activo, para el autocompletado y las tarjetas.
create view v_ultimo_cierre with (security_invoker = true) as
select distinct on (pd.ticker)
  pd.ticker, a.nombre, a.tipo, pd.fecha, pd.cierre, pd.volumen
from precios_diarios pd
join activos a on a.ticker = pd.ticker
order by pd.ticker, pd.fecha desc;
