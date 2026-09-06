-- ============================================================================
-- migracion_002_razonamiento.sql
--
-- Ejecutar UNA VEZ en Supabase: Dashboard > SQL Editor > New query > Run.
-- Idempotente: volver a ejecutarla no rompe nada.
--
-- QUÉ ARREGLA
--
-- El modelo devuelve, por cada impacto, una explicación de dos frases que va
-- de la cita al activo. Está en el esquema Pydantic desde la fase 5, con su
-- validación y su límite de 400 caracteres, y su descripción dice: "se
-- muestra al usuario en la Terminal de Sinapsis".
--
-- Nunca se escribió. `extraer.py` construía la fila sin ese campo, así que
-- cada noche el modelo razonaba, el razonamiento se validaba, y se perdía al
-- llegar a la base.
--
-- No es un adorno. El grafo causal tiene dos mitades: la cita, que demuestra
-- que la frase existe —y eso ya se comprueba mecánicamente—, y el
-- razonamiento, que explica por qué esa frase toca a ESE activo. Sin la
-- segunda, un factor de 1,66 sobre el petróleo es un número que hay que
-- creerse. Con ella, se puede discutir.
--
-- Los impactos escritos antes de hoy se quedan sin razonamiento: no se puede
-- recuperar lo que no se guardó. `sinapsis.py` los muestra igual, solo que
-- sin la línea explicativa.
-- ============================================================================

alter table impactos
    add column if not exists razonamiento text;

comment on column impactos.razonamiento is
    'De la cita al activo, en una o dos frases. Es la mitad explicativa del '
    'grafo: la cita prueba que la frase existe, esto dice por qué toca a este '
    'activo. Nulo en los impactos anteriores al 2026-09-06.';


-- ----------------------------------------------------------------------------
-- Comprobación. Tras la próxima corrida de `extraer.py`, la columna
-- `con_razonamiento` debe empezar a crecer.
-- ----------------------------------------------------------------------------
select count(*)                                            as impactos,
       count(*) filter (where razonamiento is not null)     as con_razonamiento,
       count(*) filter (where lote_uniforme)                as apartados,
       count(*) filter (where cita_verificada)              as con_cita
  from impactos;
