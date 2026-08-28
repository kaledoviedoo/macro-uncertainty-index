# Motor de Inferencia Causal — Estado del proyecto

**Qué es:** un sistema local que lee documentos oficiales de bancos centrales y
gobiernos, los conecta con 25 activos financieros, y estima **cuánta
incertidumbre** hay sobre cada uno — no si va a subir o bajar.

Esa distinción es el proyecto entero. Volvemos a ella al final.

---

## 1. Dónde vamos

| Fase | Qué es | Estado |
|---|---|---|
| **1** | Base de datos en la nube, esquema, universo de activos | ✅ Terminada |
| **2** | Ingesta de precios, 10 años de histórico, régimen de mercado | ✅ Terminada |
| **3** | La Terminal Óptica: buscador y gráfico | ✅ Terminada |
| **4** | Ingesta de noticias por RSS + texto completo | ✅ Terminada |
| **5** | Extracción con LLM + calendario de eventos | ✅ Funcionando en seco |
| **6** | Marcador: ¿el motor acierta? | 🔬 Línea base medida |
| **7** | Búsqueda semántica y ampliación del universo | ⬜ Sin empezar |

### Lo que hay dentro ahora mismo

- **68.000 filas de precios**, 25 activos, de 2016 a hoy
- **2.515 días clasificados** por régimen: 68,9 % normal · 25,0 % estrés · 6,1 % shock
- **16 fuentes oficiales verificadas** — Fed, BCE, Banco de Inglaterra, Banco de
  Japón, Banrep, BLS (empleo, IPC, IPP), BEA, SEC, USTR, EIA
- **141 fechas de eventos** con su impacto histórico medido
- **4 canales colombianos**: TRM completa desde 1991, tasa de política,
  IBR overnight, COLCAP mensual, más el ADR de Bancolombia

---

## 2. Los tres números que importan hoy

### El detector de caídas ya funciona, y cabe en una línea

```
VIX por encima de su percentil 80 del último año
```

Medido sobre 2.009 días fuera de muestra, para caídas mayores al 3 % en 5 días:

| | Valor |
|---|---|
| Frecuencia base de caídas | 11,25 % |
| Marca | 20 % de los días |
| De los días marcados, caen | **23,4 %** |
| **Elevación** | **2,08×** |
| **Cobertura** (caídas capturadas) | **41 %** |

Una regresión logística de diez variables con validación walk-forward dio 1,59×
y 32 % a la misma tasa de aviso. **La regla de una línea gana.** Eso ahorra
meses de complejidad inútil.

### El objetivo del 88 % era una trampa

Solo el 3,5 % de los días caen más del 2 %. Un modelo que **nunca** anuncie una
caída acierta el 96,5 %. Perseguir "88 % de exactitud" habría producido algo
peor que el silencio, con apariencia de funcionar.

Las métricas reales son tres: **elevación** sobre la base, **cobertura** de las
caídas, y **calibración** — cuando dice 18 %, ¿pasa el 18 % de las veces?

### El extractor diferencia el signo por activo

Sobre un artículo del FMI titulado *"AI to fuel global growth as investment
spreads beyond US"*, el modelo devolvió:

| Activo | Factor | Cola | Horizonte | Confianza |
|---|---|---|---|---|
| ^GSPC | 1,12 | ↓ | 30 d | 0,78 |
| DX-Y.NYB | 1,10 | ↓ | 20 d | 0,75 |
| **^STOXX50E** | **1,15** | **↑** | 30 d | 0,80 |

Bolsa estadounidense y dólar a la baja, bolsa europea al alza, ante una
noticia sobre crecimiento que se desplaza fuera de Estados Unidos. Es el
razonamiento correcto, y salió de tres juicios distintos: tres factores,
tres horizontes, dos direcciones. Dos versiones antes del prompt, ese mismo
documento producía tres filas idénticas con `1.10 / izquierda`.

**Citas inventadas: 0 %.** Cada fila lleva una frase literal del documento,
verificada por código contra el texto original.

### El informe de empleo de EE.UU. mueve más al peso que al S&P 500

| Activo | Factor en día de evento | p |
|---|---|---|
| **TRM (peso colombiano)** | **1,53×** | 0,0000 |
| Bono 10 años EE.UU. | 1,51× | 0,0036 |
| S&P 500 | 1,24× | 0,0364 |
| Nasdaq 100 | 1,23× | 0,0253 |

Ese es el efecto de segundo orden que justifica todo el proyecto: un dato
laboral estadounidense agita el peso colombiano un 53 % por encima de lo normal.

---

## 3. Qué falta

**Inmediato — quitar el `--seco`.** El extractor funciona pero corre en modo
prueba: procesa todo y no escribe. Una pasada sin esa bandera puebla el grafo
de impactos por primera vez.

**Corto plazo**

- Automatizar la secuencia nocturna con el Programador de tareas de Windows
- Emitir predicciones fechadas: sin una fila con fecha y hora no hay nada que
  evaluar después, y esa es la fase 6
- Importar los calendarios oficiales de FOMC, IPC y juntas del Banrep (hoy solo
  está el informe de empleo, derivado por regla)
- Los 4 PDFs del Banco de Japón entran bien, pero algunos comunicados siguen
  siendo tablas: revisar si el recorte a 12.000 caracteres corta lo importante

**Medio plazo**

- Propagación por el grafo a dos y tres saltos, que es donde está la ventaja
- Rellenar el hueco de 233 días en CBR e IBR usando los comunicados del Banrep
- Medir si el LLM supera el 2,08× de la regla del VIX

**Nunca, a propósito**

- Predecir el precio de mañana
- Recomendar comprar o vender

---

## 4. Tecnologías

| Capa | Herramienta | Por qué |
|---|---|---|
| Lenguaje | **Python 3.13** | El ecosistema de datos vive aquí |
| Base de datos | **Supabase (PostgreSQL)** | En la nube, capa gratuita, cero RAM local |
| Interfaz | **Dash + Plotly** | Gráficos profesionales sin escribir JavaScript |
| Datos | **pandas · NumPy** | Manipulación de series temporales |
| Modelos | **scikit-learn** | Regresión logística con validación walk-forward |
| Validación | **Pydantic** | El contrato que el LLM debe cumplir |
| Fuentes | **yfinance · SDMX · Socrata · RSS · GDELT** | Cinco protocolos distintos, un esquema |
| Documentos | **trafilatura · pdfplumber** | El texto real detrás del titular, HTML y PDF |
| Razonamiento | **Groq o Gemini** | Capa gratuita, en lote nocturno |
| Seguridad | **RLS de PostgreSQL** | La app solo lee; escribir requiere otra clave |

### Cómo se ejecuta

Todo pasa por `motor.ps1`, que resuelve solo la ruta del entorno virtual:

```powershell
cd C:\Users\User\OneDrive\Desktop\finance

.\motor.ps1                    # lista los comandos
.\motor.ps1 estado             # panel de control
.\motor.ps1 app                # la Terminal, en :8050

.\motor.ps1 precios            # diario
.\motor.ps1 noticias           # diario
.\motor.ps1 enriquecer         # texto completo de las fuentes oficiales
.\motor.ps1 extraer --limite 20

.\motor.ps1 senales            # marcador de señales
.\motor.ps1 caidas             # validación fuera de muestra
```

---

## 5. Por qué este proyecto es interesante

**Porque se niega a mentirte.** Casi todo el software financiero está diseñado
para producir confianza. Este está diseñado para producir dudas cuantificadas.
Cada componente lleva su propio detector de errores:

- La línea del gráfico **se corta** en los huecos de datos, en vez de dibujar
  una tendencia que nunca ocurrió
- La deriva está **forzada a cero** por defecto, porque las 24 series del
  universo tienen tendencia histórica positiva y eso describe la década
  2016-2026, no a los activos
- Cada cita del LLM se **verifica mecánicamente** contra el documento original
- La comparación entre modelos **iguala la tasa de aviso** antes de comparar

**Porque midió su propio sesgo y lo publicó.** La primera versión estimaba la
tendencia usando solo días tranquilos. Sonaba sensato y triplicaba la deriva:
el S&P 500 pasaba de 13,4 % a 44,5 % anual y NVIDIA a 176 %. Borrar los
shocks equivale a pronosticar un mundo sin caídas. Ahora se simulan.

**Porque encontró que la complejidad no paga.** Diez variables, walk-forward,
embargo, regularización — y pierde contra una regla de una línea. Saber eso
vale más que el modelo.

**Porque mira donde nadie mira.** El peso colombiano, el COLCAP y el IBR tienen
una fracción de los ojos que vigilan el S&P 500. La información se incorpora
más despacio ahí, y eso es exactamente donde un sistema paciente puede aportar.

---

## 6. Para qué sirve, después de la reorientación

Empezó como "predecir el precio". Eso no funciona, y perseguirlo produce
sistemas que suenan convincentes y aciertan como una moneda.

Ahora sirve para tres cosas, ordenadas por cuán seguras son:

### Muy probable — un instrumento de contexto honesto
Buscas un activo y ves, en una pantalla: su precio real, si el mercado está en
régimen normal o alterado, el espectro de dónde puede estar en tres meses con
su banda de incertidumbre, qué eventos de calendario vienen, qué otros activos
se mueven antes que él y con cuántos días de ventaja, y de dónde salió cada
dato. **Eso ya vale la pena sin afirmar nada sobre el futuro.**

### Plausible — un aviso de riesgo calibrado
No "va a caer", sino: *"hoy la probabilidad de una caída mayor al 3 % en cinco
días es del 23 %, el doble de lo normal, porque el VIX está en su percentil 85
y el jueves hay dato de empleo."* Verificable, con un marcador que dice si esa
probabilidad se cumple.

### Posible, sin probar — ventaja en las cadenas lentas
Un arancel al acero chino mueve a las acereras en segundos. Pero la cadena
completa —menor demanda de materias primas, presión sobre las divisas
exportadoras, libro de crédito de los bancos locales— tarda días en recorrerse,
y casi nadie la recorre entera. Ahí es donde el grafo causal podría tener una
ventaja real. **Es una hipótesis, y el sistema está construido para falsarla.**

---

## 7. La regla de la casa

> Una predicción sin marcador es una opinión con formato de dato.

Por eso la fase 6 no es opcional, la tabla `predicciones` existió desde el
primer día, y el listón para el LLM es un número concreto: **superar 2,08× de
elevación con 41 % de cobertura y menos de 5 puntos de error de calibración.**

Si no lo supera, no entra — por muy bien que suenen sus explicaciones.
