# Auditoría UX y análisis competitivo de PELE

**Producto:** PELE, dashboard público de inteligencia para mercados de Polymarket<br>
**Corte de investigación:** 27 de julio de 2026<br>
**Superficie auditada:** `web/index.html`, `web/assets/styles.css`, `web/assets/app.js`<br>
**Dispositivos:** escritorio 1440 × 900 y móvil 390 × 844, con contraste adicional en 375 × 812<br>
**Método:** doble evaluación independiente (diseño/heurísticas y detector/navegador), revisión de código y contraste con fuentes primarias de competidores.

## 1. Resumen ejecutivo

PELE tiene una identidad visual propia y una posición competitiva defendible: no intenta ser otro exchange, sino una capa de decisión previa a la ejecución que explica la calidad de una probabilidad mediante liquidez, actividad, spread, cobertura y modelos derivados.

La base es sólida, pero todavía funciona mejor como presentación analítica que como mesa de trabajo recurrente. Los tres obstáculos principales son:

1. El radar queda enterrado bajo un hero de hasta un viewport y cuatro KPI.
2. Las tarjetas muestran demasiadas señales simultáneas y no permiten comparar mercados con rapidez, especialmente en móvil.
3. Cualquier búsqueda, filtro, ordenación o actualización reconstruye las tarjetas y borra la estimación personal y los expedientes abiertos.

Existe además un riesgo de confianza financiera que debe corregirse antes de exponer la herramienta a más usuarios: el valor esperado calculado en `web/assets/app.js:378-381` es una razón adimensional, pero se presenta con formato monetario. El “consenso agregado” también es una media simple de contratos heterogéneos (`web/assets/app.js:130-144`), no un consenso ponderado.

**Puntuaciones:**

| Evaluación | Resultado | Lectura |
|---|---:|---|
| Nielsen | **25/40** | Aceptable; necesita mejoras relevantes |
| Auditoría técnica | **14/20** | Buena base con brechas verificadas |
| Especificidad de diseño | **8/10** | Producto reconocible, no intercambiable |
| Carga cognitiva | **5 fallos de 8** | Alta |

## 2. Alcance y método

### Evidencia de PELE

- Inspección de la aplicación local con navegador, respuesta real de fallo del servidor local y contratos API simulados en memoria para cubrir estados no disponibles durante la sesión.
- Pruebas de estados controlados de carga, éxito, vacío, error, datos parciales y error de proyección.
- Pruebas de búsqueda, filtros, ordenación, paginación, detalles, estimación propia, alertas y actualización automática.
- Revisión semántica, teclado, foco, objetivos táctiles, overflow y reducción de movimiento.
- Detector Impeccable sobre `web/index.html`: un warning `overused-font` por Instrument Sans, considerado falso positivo contextual porque PELE usa deliberadamente tres voces tipográficas.
- Capturas existentes: `pele-frontend-desktop.png`, `pele-frontend-mobile.png`, `pele-contract-desktop.png` y `pele-contract-mobile.png`.

### Evidencia competitiva

- Cinco competidores: Polymarket, Kalshi, Limitless, Manifold y Polymarket Analytics.
- Referente aspiracional adyacente: TradingView.
- Se priorizaron documentación y páginas oficiales vigentes.
- No se crearon cuentas ni se ejecutaron órdenes. Funciones autenticadas, geobloqueadas o no verificables se marcan como limitaciones.

## 3. Qué hace PELE y para quién

PELE descubre mercados públicos de Polymarket mediante Gamma, permite buscar, filtrar y ordenar contratos, explica señales derivadas, guarda vigilancia local, enlaza al contrato original y ofrece una simulación GBM independiente basada en Yahoo Finance.

No ejecuta operaciones desde el dashboard. El bot de trading y su infraestructura viven fuera de esta superficie pública; el informe evalúa sólo la experiencia web descrita en `README.md:106-124`.

### Propuesta observable

> Convertir precio, liquidez y actividad en una lectura explicable que ayude a decidir qué mercado merece investigación adicional.

### Perfiles primarios

| Perfil | Objetivo | Necesidad principal | Riesgo actual |
|---|---|---|---|
| Analista recurrente | Detectar anomalías en menos de 30 segundos | Escaneo, comparación, vistas persistentes | El hero y la densidad retrasan la señal |
| Trader de Polymarket | Validar una hipótesis antes de abrir el contrato | Spread, profundidad, reglas, procedencia | Debe saltar al mercado para completar contexto |
| Investigador/periodista | Entender qué cree el mercado y con qué calidad | Explicaciones, timestamp y trazabilidad | La jerga y el “consenso” pueden sobreprometer |
| Usuario móvil ocasional | Revisar cambios y alertas en sesiones breves | Resumen, continuidad y targets grandes | Scroll largo y pérdida de contexto |
| Usuario de teclado/lector | Completar el flujo con equivalencia informativa | Semántica, anuncios y contraste | EV dinámico e histograma no son equivalentes |

## 4. Matriz de tareas de PELE

| Tarea | Entrada | Flujo actual | Estado cubierto | Fricción | Prioridad |
|---|---|---|---|---|---|
| Entender la tesis | Llegada directa | Hero → costura Sí/No → KPI | Carga y datos no disponibles | El valor operativo queda después de 1-3 viewports | Alta |
| Encontrar un mercado | Buscar o filtrar | Abrir filtros móvil → 5 criterios → resultados | Éxito y sin coincidencias | Seis decisiones visibles y filtros no persistentes | Alta |
| Priorizar señales | Ordenar o activar actividad inusual | Orden → tarjetas → lectura PELE | Datos completos e insuficientes | No hay vista comparativa ni razón dominante | Alta |
| Investigar un contrato | Abrir expediente | Señal → cálculo → expediente → pruebas | Disponible/no disponible | Demasiados niveles y estado se pierde al rerender | Crítica |
| Contrastar estimación propia | Introducir 1-99 | Cálculo EV inmediato | Falta precio/spread | Unidad monetaria incorrecta y sin anuncio dinámico | Crítica |
| Vigilar un mercado | Activar alerta | Botón → `localStorage` → centro de alertas | Activa/inactiva/evento | Fallos de persistencia se silencian | Alta |
| Abrir contrato original | Ver mercado | Nueva pestaña en Polymarket | URL disponible/fallback | Falta resumen previo de reglas/jurisdicción | Media |
| Simular un activo | Abrir herramienta → símbolo/horizonte | API Yahoo → 5.000 rutas → histograma | Carga/éxito/error/último válido | Se siente como producto paralelo al radar | Media |
| Recuperarse de fallo Gamma | Carga inicial o paginación | Aviso → reintentar | Error total y datos parciales | Buen comportamiento; copy vacío aún se confunde | Baja |

## 5. Auditoría de escritorio y móvil

### 5.1 Salud heurística

| # | Heurística | Score | Hallazgo clave |
|---|---|---:|---|
| 1 | Visibilidad del estado | 3/4 | Carga, live, parcial, error y proyección son visibles |
| 2 | Correspondencia con el mundo real | 2/4 | Gamma, GBM, log-odds, P5-P95 y rotación requieren contexto |
| 3 | Control y libertad | 3/4 | Hay reset y detalles reversibles; falta persistencia/undo |
| 4 | Consistencia y estándares | 3/4 | UI coherente; implementación diverge de `DESIGN.md` |
| 5 | Prevención de errores | 2/4 | Formulario de proyección valida; EV personal no se revisa |
| 6 | Reconocimiento sobre recuerdo | 3/4 | Etiquetas visibles; comparar en móvil depende de memoria |
| 7 | Flexibilidad y eficiencia | 1/4 | Sin comparación, favoritos, vistas guardadas o atajos |
| 8 | Estética y minimalismo | 3/4 | Fuerte jerarquía; tarjetas excesivamente densas |
| 9 | Recuperación de errores | 3/4 | Reintento y último resultado válido; algunos errores son técnicos |
| 10 | Ayuda y documentación | 2/4 | Método visible, pero no contextual en todas las decisiones |
| **Total** |  | **25/40** | **Aceptable** |

### 5.2 Auditoría técnica

| Dimensión | Score | Hallazgo clave |
|---|---:|---|
| Accesibilidad | 3/4 | Buena semántica y foco; targets y anuncios incompletos |
| Rendimiento | 3/4 | Render completo de cuadrícula en cada pulsación |
| Responsive | 3/4 | Sin overflow; móvil legible pero muy largo |
| Theming | 2/4 | Tokens presentes; CSS acumulativo y paleta divergente |
| Integridad | 3/4 | Sistema distintivo; pérdida de estado en `render()` |
| **Total** | **14/20** | **Bueno con reservas** |

### 5.3 Estados e interacciones verificados

| Estado/interacción | Resultado | Evidencia |
|---|---|---|
| Carga de mercados | Correcto | Skeletons, `aria-busy=true`, “Conectando” |
| Datos live | Correcto | Timestamp Gamma y KPI actualizados |
| Error Gamma | Correcto | No inventa datos, explica y ofrece reintento |
| Datos parciales | Correcto | Conserva mercados recuperados |
| Fuente vacía | Incorrecto | Usa copy de “sin coincidencias” y reset aunque no hay filtros |
| Búsqueda/filtros | Funcional | Filtrado instantáneo y reset con foco |
| Ordenación | Funcional con regresión | Reordena, pero borra estado interno de tarjetas |
| Detalles con teclado | Correcto | `<summary>` nativo funciona con Enter |
| Estimación propia | Incorrecto | Formato monetario para una razón y sin `aria-live` |
| Alerta | Parcial | `aria-pressed` correcto; persistencia puede fallar en silencio |
| Cargar más | Correcto | Paginación y prevención de offset inválido |
| Proyección | Correcto | Valida, anuncia, conserva último resultado ante error |
| Histograma | Parcial | Resumen accesible, pero bins sólo mediante `title` |
| Responsive | Correcto | 3/4 → 2 → 1 columnas; filtros colapsables en móvil |
| Reducción de movimiento | Parcial | Regla global de `0.01ms` elimina feedback útil |

### 5.4 Hallazgos priorizados

#### P1. El radar queda debajo de una portada de marketing

**Evidencia:** el hero usa `min-height: min(850px, calc(100vh - 66px))` y precede a cuatro KPI (`web/assets/styles.css:446-486`). En móvil, el área de contratos aparece alrededor del píxel 1.389 en una vista de 375 px.

**Impacto:** el usuario recurrente paga el coste de la primera visita cada vez.

**Acción:** crear modo recurrente compacto o ancla operativa persistente; en móvil, adelantar el estado del radar y el control de filtros.

#### P1. Filtrar, ordenar o actualizar borra trabajo en curso

**Evidencia:** `render()` recrea todas las tarjetas con `replaceChildren(...visible.map(card))` (`web/assets/app.js:408-430`). Se reprodujo: expediente abierto + estimación 80 → ordenar → expediente cerrado y campo vacío.

**Impacto:** pérdida silenciosa de contexto y entrada, especialmente grave durante refresh automático.

**Acción:** guardar estado de UI por `marketKey` o reconciliar nodos existentes en vez de reconstruirlos.

#### P1. Las tarjetas no sirven como superficie de comparación

**Evidencia:** cada tarjeta combina probabilidad, cambio, solidez, rotación, presión, spread, dos disclosures y acciones; su altura mínima es 520 px (`web/assets/styles.css:514-529`).

**Impacto:** la interfaz responde “todo lo que sabemos” antes de “qué merece atención y por qué”. En móvil exige recordar tarjetas previas.

**Acción:** capa de escaneo con pregunta, probabilidad, cambio, solidez y una razón principal; detalles bajo demanda; comparación fija de 2-3 mercados.

#### P1. El valor esperado se comunica con una unidad incorrecta

**Evidencia:** `belief / probability - 1 - spread / probability` se formatea con `Intl.NumberFormat` de USD (`web/assets/app.js:42,378-381`).

**Impacto:** puede interpretarse como beneficio absoluto en dólares y afectar decisiones financieras.

**Acción:** mostrar porcentaje o múltiplo, definir stake y supuestos, y anunciar el resultado dinámico.

#### P2. “Consenso agregado” sobrepromete el cálculo

**Evidencia:** media aritmética simple de las probabilidades cargadas (`web/assets/app.js:130-144`).

**Impacto:** contratos heterogéneos y una muestra paginada no constituyen un consenso comparable.

**Acción:** renombrar como “media simple de la muestra” o ponderar por una metodología explicada.

#### P2. Persistencia y recuperación no cumplen todo el copy

**Evidencia:** errores de `localStorage` se silencian (`web/assets/app.js:5-13,105-109`); filtros no viven en URL ni almacenamiento.

**Impacto:** alertas aparentemente activas pueden no sobrevivir y el usuario móvil pierde contexto al volver.

**Acción:** verificar escritura, anunciar fallo y persistir filtros relevantes.

#### P2. Brechas táctiles y de equivalencia accesible

**Evidencia:** input personal de 40 px y botón de alerta de 42 px (`web/assets/styles.css:325-329`); EV sin región viva; histograma con detalle sólo en `title`.

**Impacto:** más errores táctiles y menor equivalencia para lector de pantalla.

**Acción:** 44-48 px, `aria-live` para cálculo/confirmación y resumen tabular de distribución.

#### P2. Sistema visual implementado y documentado divergen

**Evidencia:** `DESIGN.md` define azul abisal, ámbar `#ffb000` y cian; CSS efectivo usa azul eléctrico y coral (`web/assets/styles.css:1-18`) y redefine grandes bloques desde la línea 431.

**Impacto:** deuda, regresiones entre breakpoints y pérdida de una fuente de verdad.

**Acción:** consolidar CSS y decidir si la implementación o `DESIGN.md` representa la identidad vigente.

### 5.5 Fortalezas que deben preservarse

- La costura Sí/No, rieles de probabilidad y tres voces tipográficas producen una gramática propia de mercados predictivos.
- La aplicación no sustituye fallos por datos ficticios y conserva resultados válidos de proyección.
- Hay skip link, landmarks, encabezados coherentes, labels, controles nativos, foco visible y estados `aria-live` relevantes.
- El responsive reduce columnas y colapsa filtros sin miniaturizar la interfaz.
- Procedencia, cobertura y advertencias distinguen dato observado, modelo y derivado.

## 6. Matriz competitiva

Escala UX: 1 deficiente, 3 adecuada, 5 referente. La nota resume evidencia pública; no sustituye pruebas autenticadas.

| Producto | Tipo | Descubrimiento | Calidad de señal | Contexto de contrato | Alertas/watchlist | Simulación | Móvil | UX |
|---|---|---|---|---|---|---|---|---:|
| Polymarket | Directo | Categorías, tendencias, búsqueda, tags, series | Probabilidad, volumen; libro/spread vía ficha/API | Reglas, outcomes, resolución y ejecución | Parcialmente autenticado | Payout binario | Web + iPhone | 4 |
| Kalshi | Directo | Categoría → serie → evento → mercado | Bid/ask, tamaños, volumen 24h, OI | Reglas y liquidación de $1 | Cuenta requerida | Payout/hedging | iOS + Android | 4 |
| Limitless | Directo | Categorías, packs, feed, leaderboard | Probabilidad, multiplicador, libro y spread | Yes/No, market/limit orders | Cartera/feed | Payout y PnL | Web | 3 |
| Manifold | Indirecto | Temas, búsqueda, Best/Hot/New, comunidad | Probabilidad y liquidez conceptual | Comentarios, resolución del creador | Seguimiento/notificaciones | Payout en mana | Web | 4 |
| Polymarket Analytics | Directo | Markets, Traders, Hot Traders, watchlist | PnL, wallets, volumen, OI, actividad | Análisis; ejecución delegada | Watchlist/copy trade | Rendimiento histórico | Web + Telegram | 3 |
| **PELE** | Propio | Búsqueda + filtros cuantitativos | Solidez, cambio, actividad, spread, GBM | Expediente + salto a Polymarket | Local en navegador | GBM externo | Web responsive | 3 |
| TradingView | Aspiracional | Screeners, búsqueda, listas, mapas | Indicadores, perfiles, multitemporal | Gráfico/ficha y brokers | Multicondición y multidispositivo | Strategy Tester/what-if | Web + desktop + apps | 5 |

## 7. Perfiles competitivos

### 7.1 Polymarket

**Fortaleza:** continuidad entre descubrimiento, reglas, precio, libro y ejecución; API pública amplia [S1-S3].<br>
**Gap para PELE:** las tarjetas priorizan probabilidad/volumen y no explican de forma compuesta la calidad de la señal.<br>
**Patrón transferible:** incluir reglas, fuente de resolución y estado antes de abrir el contrato.<br>
**Limitación:** producto global y producto estadounidense tienen restricciones y experiencias distintas [S4, S19].

### 7.2 Kalshi

**Fortaleza:** arquitectura financiera clara, bid/ask y tamaños, volumen 24h, open interest y reglas [S5-S7].<br>
**Gap para PELE:** menos fricción pública y una explicación transversal pueden diferenciar a PELE.<br>
**Patrón transferible:** distinguir evento, serie y mercado; usar open interest/profundidad en vez de una cifra de liquidez opaca.<br>
**Limitación:** exploración completa condicionada por registro; `liquidity_dollars` está deprecado y devuelve cero [S6].

### 7.3 Limitless

**Fortaleza:** probabilidad, multiplicador, spread y tipos de orden se explican de forma directa [S8-S11].<br>
**Gap para PELE:** dependencia de wallet, Base y USDC crea una oportunidad para un radar informativo sin fricción.<br>
**Patrón transferible:** explicar spread con lenguaje operativo y mostrar payout esperado antes de salir.<br>
**Limitación:** no se verificó app nativa; hay restricciones jurisdiccionales [S20].

### 7.4 Manifold

**Fortaleza:** descubrimiento social, enorme diversidad temática y aprendizaje sin riesgo financiero [S12-S13].<br>
**Gap para PELE:** no ofrece un screener cuantitativo comparable ni dinero convertible.<br>
**Patrón transferible:** contexto comunitario y explicación pedagógica de cómo una operación cambia la probabilidad.<br>
**Limitación:** la resolución suele depender del creador y mana no se convierte a efectivo.

### 7.5 Polymarket Analytics

**Fortaleza:** convierte actividad de wallets, PnL y open interest en señales centradas en quién mueve el mercado [S14-S16].<br>
**Gap para PELE:** depende más del rendimiento histórico de traders y menos de una explicación prospectiva de la calidad del contrato.<br>
**Patrón transferible:** watchlists, ranking por OI y actividad de “smart money”.<br>
**Limitación:** funciones autenticadas no probadas; sus términos reconocen retrasos y dependencia de terceros [S15].

### 7.6 TradingView, referente aspiracional

TradingView no compite por el mismo inventario. Es el referente del flujo completo **screener → ficha profunda → alerta → escenario → ejecución** [S17-S18].

Patrones transferibles:

- Vista tabla configurable además de tarjetas.
- Filtros, columnas y watchlists persistentes.
- Alertas multicondición sincronizadas.
- Profundidad progresiva y comandos para expertos.
- Comparación y escenarios what-if sin perder contexto.
- Continuidad web/móvil/escritorio.

PELE no debe copiar su densidad. Debe adaptar su arquitectura de trabajo y conservar la síntesis editorial propia.

## 8. Oportunidades priorizadas

La puntuación usa impacto (1-5), confianza de evidencia (1-5) y esfuerzo inverso (5 = menor esfuerzo). Total máximo: 15.

| Rank | Oportunidad | Impacto | Confianza | Esfuerzo inverso | Total | Horizonte |
|---:|---|---:|---:|---:|---:|---|
| 1 | Corregir unidad de EV y renombrar/ponderar consenso | 5 | 5 | 5 | **15** | Inmediato |
| 2 | Preservar estado de expedientes y estimaciones al rerender | 5 | 5 | 4 | **14** | Inmediato |
| 3 | Crear capa de escaneo y comparación de 2-3 mercados | 5 | 5 | 2 | **12** | Próximo ciclo |
| 4 | Dar acceso recurrente directo al radar | 4 | 5 | 3 | **12** | Próximo ciclo |
| 5 | Persistir filtros, vistas y alertas con feedback verificable | 4 | 5 | 3 | **12** | Próximo ciclo |
| 6 | Añadir reglas, resolución y jurisdicción antes del salto | 5 | 4 | 2 | **11** | Estratégico |
| 7 | Incorporar profundidad/open interest y calidad de ejecución | 5 | 4 | 2 | **11** | Estratégico |
| 8 | Vista profesional configurable tipo screener | 5 | 4 | 1 | **10** | Estratégico |
| 9 | Equivalencia accesible y targets de 44-48 px | 3 | 5 | 4 | **12** | Inmediato |
| 10 | Consolidar CSS y reconciliar `DESIGN.md` | 3 | 5 | 3 | **11** | Mantenimiento |

### Tesis de producto recomendada

**PELE debe ser la capa que responde “¿qué probabilidad merece confianza y por qué?” antes de que el usuario abra el exchange.**

Esto evita competir frontalmente con Polymarket o Kalshi en ejecución y evita competir con Polymarket Analytics sólo en rankings de traders. El espacio propio combina:

1. Descubrimiento cuantitativo sin login.
2. Calidad explicable de probabilidad.
3. Comparación de contratos.
4. Procedencia y frescura verificables.
5. Vigilancia persistente.
6. Salto seguro al mercado original.

## 9. Secuencia recomendada

### Fase 0: confianza y pérdida de datos

- Corregir la unidad del EV.
- Renombrar el consenso o ponderarlo.
- Preservar estimaciones y detalles al filtrar/ordenar/refrescar.
- Distinguir fuente vacía de filtros sin resultados.

### Fase 1: velocidad de decisión

- Añadir modo compacto recurrente.
- Reducir la tarjeta a cinco señales de escaneo.
- Permitir fijar y comparar 2-3 mercados.
- Persistir filtros en URL y vistas locales.

### Fase 2: confianza operacional

- Incluir reglas, fuente de resolución, estado y jurisdicción.
- Añadir profundidad/open interest cuando la fuente lo permita.
- Alertas multicondición con confirmación y estado de almacenamiento.

### Fase 3: mesa profesional

- Vista tabla configurable.
- Watchlists sincronizadas con backend/cuenta opcional.
- Historial de señales y comparación temporal.
- Atajos y comandos para usuarios avanzados.

## 10. Referencias anotadas

Todas las fuentes web se consultaron el **27-07-2026**. “s. f.” indica que la página no muestra una fecha editorial verificable.

- **[S1] Polymarket, “Discover Markets”**. Eventos, mercados, series, búsqueda y tags públicos sin autenticación. <https://docs.polymarket.com/market-data/discover-markets> (s. f.).
- **[S2] Polymarket, “Prices and Order Books”**. Precios, midpoint, spread, histórico y libro. <https://docs.polymarket.com/market-data/prices-order-books> (s. f.).
- **[S3] Polymarket**. Interfaz pública, categorías, fichas y ejecución Sí/No. <https://polymarket.com/> (s. f.).
- **[S4] Apple App Store, “Polymarket”**. Evidencia de app oficial para iPhone y producto estadounidense. <https://apps.apple.com/us/app/polymarket/id6648798962> (versión vigente en julio de 2026).
- **[S5] Kalshi Help Center, “Who are you trading with?”**. Audiencias y mecánica de trading. <https://help.kalshi.com/en/articles/13823808-how-to-trade-on-kalshi> (10-03-2026).
- **[S6] Kalshi, “Get Markets”**. Estados, bid/ask, tamaños, volumen 24h, OI, reglas y deprecación de `liquidity_dollars`. <https://docs.kalshi.com/api-reference/market/get-markets> (OpenAPI 3.26.0).
- **[S7] Google Play, “Kalshi: Trade on Anything”**. Evidencia de soporte móvil Android. <https://play.google.com/store/apps/details?id=com.kalshi.mobile> (actualizada 26-07-2026).
- **[S8] Limitless Exchange**. Navegación y oferta pública. <https://limitless.exchange/> (s. f.).
- **[S9] Limitless, “Making Your First Trade”**. Wallet, USDC en Base y flujo de compra Sí/No. <https://docs.limitless.exchange/user-guide/making-your-first-trade> (s. f.).
- **[S10] Limitless, “CLOB Overview”**. Libro Sí/No, bids, asks, spread y órdenes market/limit. <https://docs.limitless.exchange/user-guide/clob-overview> (s. f.).
- **[S11] Limitless, “Get PnL Chart (Public)”**. PnL realizado, no realizado y total. <https://docs.limitless.exchange/api-reference/public-portfolio/pnl-chart> (s. f.).
- **[S12] Manifold, “About”**. Posicionamiento social, creación de mercados y moneda de juego. <https://manifold.markets/about> (s. f.).
- **[S13] Manifold, “FAQ”**. Tipos de mercado, mana, liquidez, órdenes y resolución por creadores. <https://docs.manifold.markets/faq> (edición vigente 2026).
- **[S14] Polymarket Analytics, “Markets”**. Markets, Traders, Hot Traders, categorías y open interest. <https://polymarketanalytics.com/markets> (s. f.).
- **[S15] Polymarket Analytics, “Terms of Service”**. Dependencia de terceros, retrasos y no garantía de resultados futuros. <https://polymarketanalytics.com/terms> (vigente al corte).
- **[S16] Polymarket Analytics, “PolyGun”**. Copy trading y operaciones mediante Telegram. <https://polymarketanalytics.com/copy-trade> (s. f.).
- **[S17] TradingView, “Features”**. Screeners, más de 400 filtros, alertas, Strategy Tester, brokers y escenarios de opciones. <https://www.tradingview.com/features/> (s. f.; copyright 2026).
- **[S18] TradingView, “Mobile Applications”**. Continuidad entre web, escritorio y móvil. <https://www.tradingview.com/mobile/> (s. f.).
- **[S19] Polymarket, “Geographic Restrictions”**. Restricciones por jurisdicción. <https://docs.polymarket.com/api-reference/geoblock> (s. f.).
- **[S20] Limitless, “Terms of Service”**. Restricciones geográficas. <https://docs.limitless.exchange/user-guide/terms-of-service> (19-06-2026).

## 11. Limitaciones y nivel de confianza

- **Alta confianza:** hallazgos de código, estados reproducidos, responsive, teclado, rerender y semántica del EV.
- **Media-alta:** comparación de funciones documentadas públicamente por los competidores.
- **Media:** calidad de experiencias autenticadas, móviles nativas y ejecución real, porque no se crearon cuentas ni órdenes.
- Las interfaces y restricciones financieras cambian con frecuencia. La matriz debe revalidarse trimestralmente o antes de una decisión estratégica importante.
