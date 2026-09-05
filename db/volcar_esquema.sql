-- ============================================================================
-- volcar_esquema.sql — Saca el esquema REAL de la base, para reconstruir
-- db/schema.sql cuando se ha quedado atrás.
--
-- Ejecutar en Supabase:  Dashboard > SQL Editor > New query > Run.
-- Solo LEE del catálogo de PostgreSQL. No modifica nada.
--
-- POR QUÉ HACE FALTA
--
-- `db/schema.sql` describe una base que ya no existe. Le falta la tabla
-- `impactos` entera —tiene `relaciones_causales`, como se llamaba antes de
-- la fase 5—, le falta la vista `v_marcador`, y la columna que el código
-- llama `resuelta_el` aparece allí como `resuelta_en`.
--
-- Eso no es un detalle de orden. Ese archivo es lo que permite levantar el
-- proyecto desde cero: quien clone el repositorio y lo ejecute se queda con
-- una base que no puede correr el motor. Y mientras la deriva dure, el
-- archivo no es documentación incompleta, es documentación FALSA, que es
-- peor: se lee con confianza.
--
-- QUÉ HACER CON LA SALIDA
--
-- Devuelve UNA celda con todo el DDL. La rejilla del editor la recorta al
-- mostrarla, así que no la copies a ojo: pulsa **Export > CSV** y adjunta
-- el archivo. Con eso se reconstruye `db/schema.sql` completo.
-- ============================================================================

select string_agg(ddl, E'\n\n' order by orden, nombre) as esquema
from (

  -- ------------------------------------------------------------ SECUENCIAS
  -- Van primero y no son un adorno. Una columna `bigserial` se vuelca como
  -- `bigint default nextval('tabla_id_seq')`, y si la secuencia no existe
  -- todavía, esa tabla no se puede crear. Sin esta rama el volcado parece
  -- completo y no arranca — que es exactamente el fallo que venimos a
  -- corregir, repetido en el archivo que lo corrige.
  select 0 as orden,
         c.relname as nombre,
         'create sequence if not exists ' || c.relname || ';' as ddl
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'S'

  union all

  -- ---------------------------------------------------------------- TABLAS
  select 1,
         c.relname as nombre,
         'create table ' || c.relname || E' (\n' ||
         string_agg(
             '    ' || a.attname || ' ' ||
             format_type(a.atttypid, a.atttypmod) ||
             case when a.attnotnull then ' not null' else '' end ||
             coalesce(' default ' || pg_get_expr(d.adbin, d.adrelid), ''),
             E',\n' order by a.attnum
         ) || E'\n);' as ddl
  from pg_class c
  join pg_namespace n  on n.oid = c.relnamespace
  join pg_attribute a  on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
  left join pg_attrdef d on d.adrelid = c.oid and d.adnum = a.attnum
  where n.nspname = 'public' and c.relkind = 'r'
  group by c.relname

  union all

  -- ----------------------------------------------- RESTRICCIONES (PK/FK/UQ)
  -- Van aparte de las tablas a propósito: así el orden de creación no
  -- importa y el archivo se puede ejecutar de arriba abajo sin que una
  -- clave foránea apunte a una tabla que aún no existe.
  select 2,
         con.conname,
         'alter table ' || rel.relname ||
         ' add constraint ' || con.conname || ' ' ||
         pg_get_constraintdef(con.oid) || ';'
  from pg_constraint con
  join pg_class rel    on rel.oid = con.conrelid
  join pg_namespace n  on n.oid = rel.relnamespace
  where n.nspname = 'public'

  union all

  -- --------------------------------------------------------------- ÍNDICES
  -- Se excluyen los que respaldan una restricción: ya salieron arriba y
  -- crearlos dos veces da error.
  select 3,
         i.indexname,
         i.indexdef || ';'
  from pg_indexes i
  where i.schemaname = 'public'
    and not exists (
        select 1 from pg_constraint con
        where con.conname = i.indexname
    )

  union all

  -- ---------------------------------------------------------------- VISTAS
  select 4,
         c.relname,
         'create view ' || c.relname ||
         ' with (security_invoker = true) as' || E'\n' ||
         pg_get_viewdef(c.oid, true)
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'v'

  union all

  -- ------------------------------------------------- ACTIVAR RLS POR TABLA
  -- Sin esto el volcado crea las políticas pero deja las tablas abiertas.
  -- Una política sin RLS activado no restringe nada: está escrita, se lee
  -- como protección en el archivo, y no protege. Es el peor tipo de error
  -- de seguridad, el que además tranquiliza a quien lo revisa.
  select 5,
         c.relname,
         'alter table ' || c.relname || ' enable row level security;'
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity

  union all

  -- ------------------------------------------------------- POLÍTICAS (RLS)
  select 6,
         p.policyname,
         'create policy "' || p.policyname || '" on ' || p.tablename ||
         ' for ' || p.cmd ||
         ' to ' || array_to_string(p.roles, ', ') ||
         coalesce(' using (' || p.qual || ')', '') ||
         coalesce(' with check (' || p.with_check || ')', '') || ';'
  from pg_policies p
  where p.schemaname = 'public'

) t;
