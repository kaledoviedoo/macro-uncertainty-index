# 📜 DOCUMENTO MAESTRO DE ARQUITECTURA (MASTER_SPEC_V2.md)
**Proyecto:** Motor de Inferencia Causal Geopolítica-Financiera (Terminal Óptica)
**Diseñado para:** Entornos locales de bajos recursos con procesamiento delegado a APIs y renderizado web local.
**Estilo Visual:** Editorial Cuantitativo (Blanco, Negro, Gris; Acentos Verde/Rojo).

---

## 1. VISIÓN GENERAL DEL PRODUCTO

Este sistema es un **Motor Neuro-Simbólico Local con Memoria Cloud**. Ingesta noticias globales y datos macro; utiliza un pipeline LLM para trazar una red hiper-compleja de relaciones causales (Noticias ↔ Índices ↔ Acciones), y almacena este "conocimiento" en una base de datos relacional en la nube.
El usuario interactúa a través de una interfaz web local minimalista que inicia como un motor de búsqueda. Al buscar un activo, el sistema consulta la nube mediante SQL, procesa el razonamiento, y despliega un dashboard interactivo de proyecciones y análisis.

## 2. EL ALCANCE DE LOS ACTIVOS (El Universo Invertible)

El motor está pre-configurado para rastrear e interconectar la siguiente red de activos:
*   **Mercado Estadounidense:** Acciones del S&P 500 (AAPL, NVDA, MSFT, etc.) y los principales ETFs.
*   **Índices Globales (Los 50 Principales):**
    *   *EE.UU:* S&P 500, Nasdaq 100, Dow Jones, VIX (Volatilidad).
    *   *Europa:* Euro Stoxx 50, FTSE 100 (UK), DAX (Alemania).
    *   *Asia:* Nikkei 225 (Japón), Hang Seng (Hong Kong), Shanghai Composite (China).
    *   *Latinoamérica:* Bovespa (Brasil), MSCI Colcap (Colombia), IPC (México).
*   **Materias Primas y Divisas:** Oro (XAU), Petróleo (Brent/WTI), DXY (Índice Dólar).

## 3. ARQUITECTURA DE DATOS: LA RED NEURONAL SQL (Cloud)

Para evitar que tu computadora consuma memoria, eliminamos las bases de datos locales. Toda la historia, precios y la "red de relaciones" vivirá en **Supabase (PostgreSQL)** en su capa gratuita.

*   **¿Cómo funciona la "Red Neuronal" en SQL?**
    Se utilizan tablas relacionales para crear un "Grafo de Conocimiento". Cuando ocurre un evento geopolítico, se almacena, y mediante SQL (tablas intermedias) se relaciona con múltiples índices y acciones simultáneamente.
*   **Esquema SQL Básico:**
    1.  `Activos` (Tickers, Nombres, Tipo).
    2.  `Noticias_Geopoliticas` (Texto, Fecha, Vector de Embedding usando la extensión *pgvector* de PostgreSQL).
    3.  `Relaciones_Causales` (Tabla intermedia que une `Noticias` con `Activos` indicando el *Peso del Impacto* calculado por el LLM).
*   **La Ventaja Técnica:** Tu script local en Python solo ejecutará una consulta como `SELECT * FROM Activos WHERE ticker = 'NVDA'`, descargando solo los datos exactos que necesitas ver, manteniendo tu RAM intacta.

## 4. FLUJO DE USUARIO Y UI (Search-First)

El diseño de la aplicación se divide en dos estados principales:

### Estado 1: El Motor de Búsqueda (Pantalla de Inicio)
*   **Diseño:** Extremadamente minimalista. Fondo negro, una gran barra de búsqueda central (estilo Google o Spotlight de Mac).
*   **Interacción:** Escribes "Nikkei", "NVIDIA" o "Efecto Aranceles China". El sistema hace un autocompletado rápido leyendo de la tabla SQL `Activos`.
*   **Ejecución:** Al dar *Enter*, el sistema pasa al Estado 2 (solo cargando la información de ese activo específico y sus relaciones directas).

### Estado 2: La Terminal Óptica (El Dashboard)
*   **Paleta de Colores Exclusiva:** Negro dominante. Gris para contextos. Verde (`#00BF63`) y Rojo (`#FF3131`) *solo* para la serie de tiempo. y lineas graficas verdes y rojas para el precio. Tipografía natural de terminal o fuente roboto, profesional.
*   **Panel Superior:** Nombre del Activo buscado y botones de selección de tiempo (1W, 1M, 6M, 1Y).
*   **Panel Izquierdo (Terminal de Sinapsis):** Una consola de texto monoespaciado. Aquí, el LLM muestra cómo conectó los puntos en la base de datos SQL para ese activo específico.
*   **Panel Central:** Gráfico interactivo (Plotly). Doble eje Y comparando el precio del activo seleccionado contra índices relacionados o métricas macroeconómicas, revelando la correlación visualmente.

## 5. STACK TECNOLÓGICO FINAL

| Capa | Tecnología | Función y Justificación |
| :--- | :--- | :--- |
| **Frontend UI (Local)** | Python + **Dash** + Plotly | Levanta la interfaz "Buscador ➡️ Dashboard" localmente. Cero HTML/JS manual, gráficos profesionales. |
| **Computación Local** | Python (`pandas`, `numpy`) | Procesa la data traída de la BD antes de enviarla al gráfico. |
| **Base de Datos (Nube)**| **Supabase** (PostgreSQL) | Almacena precios históricos, noticias y la "red de relaciones". Cero consumo de RAM local. |
| **Motor RAG (Nube)** | API de Gemini / Groq  / IA gratuita de preferencia | Actúa como el agente de razonamiento. Infiere el impacto de la geopolítica en los activos. |
| **Fuentes de Datos** | `yfinance`, `fredapi`, RSS | Extraen los datos estructurados para alimentar la base de datos de Supabase de fondo. |

---

## 6. PRÓXIMOS PASOS PARA EL DESARROLLO

1.  **Configurar Supabase:** Crear el proyecto gratuito y ejecutar los comandos SQL para crear las tablas de Activos, Histórico y Relaciones.
2.  **Script de Llenado (Python):** Escribir un script de Python que descargue la lista del S&P 500 y los índices globales usando `yfinance`, y los suba a tu tabla SQL en Supabase.
3.  **Construir el Frontend (Dash):**
    *   Crear la pantalla de búsqueda y programar el botón para que ejecute una consulta `SELECT` a tu Supabase.
    *   Diseñar el gráfico de Plotly para que renderice los resultados de esa consulta.
4.  **Integrar el LLM (La Sinapsis):** Conectar la API del modelo de lenguaje para que lea las relaciones de la BD y genere la cadena de texto de la "Terminal Izquierda".