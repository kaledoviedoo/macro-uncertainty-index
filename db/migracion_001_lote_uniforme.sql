-- ============================================================================
-- migracion_001_lote_uniforme.sql
--
-- Ejecutar UNA VEZ en Supabase:  Dashboard > SQL Editor > New query > Run.
-- Es idempotente: volver a ejecutarla no rompe nada ni duplica nada.
--
-- POR QUÉ EXISTE ESTE ARCHIVO
--
-- `extraer.py` llevaba desde la fase 5 escribiendo dos columnas,
-- `lote_uniforme` y `long_documento`, que NO están en db/schema.sql. La
-- segunda se escribe en cada fila y funciona, así que la columna existe en la
-- base aunque el archivo no la mencione. La primera solo se escribía cuando
-- saltaba un detector que —como se descubrió el 2026-08-28— no saltaba nunca,
-- así que nadie sabe si llegó a crearse.
--
-- Eso es deriva de esquema: el archivo que sirve para reconstruir la base ya
-- no describe la base. Mientras dure, `db/schema.sql` es documentación falsa
-- y el proyecto no es reproducible.
--
-- Esta migración no arregla la deriva entera (falta la tabla `impactos`
-- completa, ver nota al final). Arregla lo que bloquea el cambio de hoy.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. La columna, con default y sin nulos.
--
-- El default importa tanto como la columna. Con NULL, la consulta de
-- `predecir.py` tendría que acordarse siempre de que `NULL = false` es
-- desconocido en SQL, no falso; el día que alguien escriba un `eq` a secas,
-- el historial entero desaparece del pronóstico sin un solo mensaje de error.
-- ----------------------------------------------------------------------------
alter table impactos
    add column if not exists lote_uniforme boolean;

update impactos
   set lote_uniforme = false
 where lote_uniforme is null;

alter table impactos
    alter column lote_uniforme set default false;

alter table impactos
    alter column lote_uniforme set not null;

comment on column impactos.lote_uniforme is
    'La cita está verificada pero la atribución es relleno: abanico (varios '
    'activos, un canal, factores en escalera) o titular sin cuerpo estirado a '
    'varias filas. Se conserva para poder medirla; predecir.py la ignora.';


-- ----------------------------------------------------------------------------
-- 2. long_documento: existe, pero que quede declarada.
-- ----------------------------------------------------------------------------
alter table impactos
    add column if not exists long_documento integer;

comment on column impactos.long_documento is
    'Caracteres del documento contra el que se verificó la cita. Por debajo '
    'de ~600 es un titular, y un titular no sostiene varias afirmaciones.';


-- ----------------------------------------------------------------------------
-- 3. Índice para la consulta de predecir.py.
--
-- Se ejecuta 20 veces por corrida (una por activo) y filtra siempre por los
-- mismos tres campos. Sin índice son 20 escaneos completos de la tabla.
-- ----------------------------------------------------------------------------
create index if not exists ix_impactos_prediccion
    on impactos (ticker, cita_verificada, lote_uniforme, horizonte_d);


-- ----------------------------------------------------------------------------
-- 4. Comprobación. Debe devolver una fila con lote_uniforme = false.
-- ----------------------------------------------------------------------------
select count(*)                                   as filas,
       count(*) filter (where lote_uniforme)      as apartadas,
       count(*) filter (where lote_uniforme is null) as sin_marcar
  from impactos;


-- ============================================================================
-- PENDIENTE, y conviene no olvidarlo:
--
-- `db/schema.sql` no contiene la tabla `impactos`. Tiene `relaciones_causales`,
-- que es como se llamaba antes de la fase 5. Quien clone este repositorio y
-- ejecute schema.sql se queda con una base que no puede correr el motor.
--
-- El arreglo es volcar el esquema real desde Supabase y sustituir el archivo,
-- no seguir apilando migraciones sobre una base que nadie puede reconstruir.
-- ============================================================================
