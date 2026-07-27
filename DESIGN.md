---
name: PELE
description: Sala de señales para leer probabilidad, liquidez y convicción en mercados predictivos.
colors:
  azul-abisal: "#071d2b"
  azul-plano: "#0d3852"
  hielo-de-datos: "#f2f7f8"
  blanco-panel: "#ffffff"
  ambar-de-alerta: "#ffb000"
  cian-de-senal: "#58c5d6"
  tinta-analitica: "#102a39"
  texto-secundario: "#647985"
  linea-tecnica: "#c9d7dc"
  exito-sobrio: "#087058"
  cautela-terrosa: "#8a5b00"
  riesgo-ladrillo: "#a43b25"
typography:
  weights:
    regular: 400
    semibold: 600
    bold: 700
  display:
    fontFamily: "League Gothic, Arial Narrow, Arial, sans-serif"
    fontSize: "clamp(5.2rem, 9.6vw, 10rem)"
    fontWeight: 400
    lineHeight: 0.74
    letterSpacing: "-0.018em"
  headline:
    fontFamily: "League Gothic, Arial Narrow, Arial, sans-serif"
    fontSize: "clamp(3rem, 6vw, 6rem)"
    fontWeight: 400
    lineHeight: 0.82
    letterSpacing: "-0.025em"
  title:
    fontFamily: "Instrument Sans, Arial, Helvetica, sans-serif"
    fontSize: "clamp(1.15rem, 1.55vw, 1.375rem)"
    fontWeight: 700
    lineHeight: 1.23
    letterSpacing: "-0.015em"
  body:
    fontFamily: "Instrument Sans, Arial, Helvetica, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.55
  body-sm:
    fontFamily: "Instrument Sans, Arial, Helvetica, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "IBM Plex Mono, Courier New, monospace"
    fontSize: "0.75rem"
    fontWeight: 700
    lineHeight: 1.35
    letterSpacing: "0.08em"
  label-xs:
    fontFamily: "IBM Plex Mono, Courier New, monospace"
    fontSize: "0.6875rem"
    fontWeight: 400
    lineHeight: 1.35
rounded:
  square: "0"
  status-dot: "50%"
spacing:
  xs: "0.35rem"
  sm: "0.5rem"
  md: "0.75rem"
  lg: "1rem"
  xl: "1.25rem"
  2xl: "1.5rem"
  3xl: "2rem"
components:
  button-primary:
    backgroundColor: "{colors.azul-abisal}"
    textColor: "{colors.blanco-panel}"
    rounded: "{rounded.square}"
    padding: "0 1.2rem"
    height: "48px"
  button-primary-hover:
    backgroundColor: "{colors.ambar-de-alerta}"
    textColor: "{colors.azul-abisal}"
  input-default:
    backgroundColor: "{colors.hielo-de-datos}"
    textColor: "{colors.tinta-analitica}"
    rounded: "{rounded.square}"
    padding: "0 0.9rem"
    height: "48px"
  card-market:
    backgroundColor: "{colors.blanco-panel}"
    textColor: "{colors.tinta-analitica}"
    rounded: "{rounded.square}"
    padding: "1.25rem"
---

# Design System: PELE

## Overview

**Creative North Star: "La Sala de Señales"**

PELE se comporta como una sala de análisis pública: precisa, alerta y editorial. La interfaz convierte datos complejos en señales comparables mediante una jerarquía tipográfica extrema, rieles de probabilidad y estados cromáticos inequívocos. Su densidad es informativa, no decorativa; cada bloque debe ayudar a decidir qué mirar y cuánto creer.

La expresión combina la autoridad de un plano técnico con titulares de portada. Los fondos azul oscuro enmarcan el contexto y los paneles blancos contienen el trabajo operativo. El ámbar señala convicción, foco o advertencia; el cian representa actividad y datos vivos. La profundidad es estructural y gráfica: bordes, capas tonales y sombras duras sustituyen al volumen atmosférico.

**Key Characteristics:**
- Tipografía condensada, pesada y en mayúsculas para declaraciones y cifras principales.
- Monoespaciada pequeña para metadatos, etiquetas y lectura instrumental.
- Geometría rectangular, líneas visibles y controles firmes.
- Ámbar escaso para decisiones, énfasis y foco; cian para señal y actividad.
- Datos expresados como escala, riel, porcentaje o estado antes que como adorno.

## Colors

La paleta enfrenta una base fría y técnica con dos señales cálidas y eléctricas de uso controlado.

### Primary
- **Azul Abisal:** estructura principal, barras, botones y superficies de máxima autoridad.
- **Azul Plano:** campo del hero y soporte para cuadrículas, escalas y visualización técnica.
- **Ámbar de Alerta:** foco accesible, énfasis decisivo, umbrales y estados que requieren atención.

### Secondary
- **Cian de Señal:** actividad en directo, progreso, acentos informativos y lectura de probabilidad.

### Neutral
- **Hielo de Datos:** lienzo general y fondo de campos en reposo.
- **Blanco Panel:** tarjetas, controles y contraste sobre fondos oscuros.
- **Tinta Analítica:** texto principal sobre superficies claras.
- **Texto Secundario:** explicaciones, fechas y metadatos no prioritarios.
- **Línea Técnica:** divisores y bordes que construyen la retícula visible.

### Tertiary
- **Éxito Sobrio:** confianza alta y estados favorables confirmados.
- **Cautela Terrosa:** confianza media o información incompleta.
- **Riesgo Ladrillo:** confianza baja y estados adversos sin alarmismo fluorescente.

### Named Rules

**The Two-Signal Rule.** El ámbar comunica atención o decisión; el cian comunica actividad o información. No intercambiar sus papeles.

**The Scarce Amber Rule.** Reservar el ámbar para el punto que debe leerse o accionarse primero; su rareza crea autoridad.

## Typography

**Display Font:** League Gothic, con Arial Narrow y Arial como respaldo.

**Body Font:** Instrument Sans, con Arial, Helvetica y sans-serif como respaldo.

**Label/Mono Font:** IBM Plex Mono, con Courier New y monospace como respaldo.

**Character:** La combinación une titulares condensados de alta presión con texto funcional neutral y una capa monoespaciada que hace que fechas, estados y métricas parezcan instrumentos, no subtítulos decorativos.

### Hierarchy
- **Display:** peso regular condensado, escala fluida y línea muy compacta; solo para la tesis principal del hero.
- **Headline:** peso regular condensado y caja alta; abre secciones y estados vacíos con impacto editorial.
- **Title:** texto de tarjeta en negrita, tamaño moderado y línea compacta; prioriza la pregunta del mercado.
- **Body:** peso regular y línea abierta; explica el producto y los detalles de confianza.
- **Body Small:** texto auxiliar de 14px; nunca sustituye el cuerpo principal.
- **Label:** monoespaciada de 12px, negrita, espaciada y normalmente en mayúsculas; identifica controles, métricas y estados.
- **Label XS:** monoespaciada de 11px y peso regular; se reserva a procedencia y metadatos secundarios, nunca a controles o acciones.

### Named Rules

**The Three-Voice Rule.** Display declara, cuerpo explica y monoespaciada mide. No usar una voz para sustituir a otra.

**The Compression Rule.** La línea compacta pertenece a titulares y cifras grandes; el texto explicativo conserva aire y legibilidad.

## Layout

El sistema ocupa todo el ancho y usa relleno lateral fluido entre `1.25rem` y `4.5rem`. El hero es una composición asimétrica de dos columnas: declaración dominante y escala de probabilidad. La zona operativa organiza filtros en tres columnas y mercados en una cuadrícula de tres columnas, ampliable a cuatro por encima de `1450px`.

La retícula responde por reducción, no por miniaturización: a `980px` las tarjetas pasan a dos columnas; a `680px`, hero, filtros, tarjetas y avisos se apilan; por debajo de `380px`, los KPI forman una sola columna. Los paneles mantienen su geometría y los titulares conservan protagonismo mediante escalas fluidas.

**The Instrument Panel Rule.** Alinear controles, cifras y metadatos sobre ejes claros; la irregularidad pertenece al hero, no al área de operación.

## Elevation & Depth

La base es plana y estratificada. La profundidad aparece mediante contraste tonal, bordes de un píxel y sombras duras desplazadas, nunca mediante neblina o vidrio. Los controles muestran una sombra estructural permanente; las tarjetas la adquieren al elevarse en hover.

### Shadow Vocabulary
- **Desplazamiento de panel** (`7px 7px 0 #dce7ea`): separa el bloque de filtros del lienzo.
- **Desplazamiento interactivo** (`8px 8px 0 #dce7ea`): confirma que una tarjeta ha entrado en estado activo.
- **Halo de estado** (`0 0 0 4px` con color translúcido): reservado para indicadores circulares en directo o espera.

### Named Rules

**The Structural Shadow Rule.** Toda sombra debe parecer una segunda placa desplazada, no iluminación ambiental.

## Shapes

La forma dominante es rectangular y sin radio. Tarjetas, campos, botones, etiquetas y avisos se construyen con esquinas rectas y bordes técnicos. Los círculos se reservan a puntos de estado y marcadores sobre escalas. El riel vertical de probabilidad es la silueta distintiva del sistema.

**The Circle Means Position Rule.** Un círculo indica un punto, estado o posición medible; nunca se usa como decoración genérica.

## Components

Los componentes son firmes y legibles: alto contraste, estados evidentes y ornamentación mínima.

### Buttons
- **Shape:** rectángulo sin radio y altura mínima de `48px` para acciones principales.
- **Primary:** fondo Azul Abisal, texto Blanco Panel y peso alto.
- **Hover / Focus:** el hover cambia a Ámbar de Alerta con texto oscuro; el foco visible usa un contorno ámbar de `3px` separado `4px`.
- **Secondary:** conserva la misma estructura; no introduce cápsulas, degradados ni sombras suaves.

### Chips
- **Style:** etiqueta rectangular compacta, fondo cian muy claro, texto azul verdoso y tipografía monoespaciada en mayúsculas.
- **State:** se usa para categoría o factor; no debe competir con una acción primaria.

### Cards / Containers
- **Corner Style:** esquinas rectas.
- **Background:** Blanco Panel sobre Hielo de Datos.
- **Shadow Strategy:** plana en reposo y desplazamiento duro en hover.
- **Border:** Línea Técnica de un píxel.
- **Internal Padding:** `1.25rem` como base.
- **Signature:** el riel izquierdo traduce probabilidad a altura y sitúa el valor con una marca ámbar.

### Inputs / Fields
- **Style:** fondo Hielo de Datos, sin radio, borde transparente en reposo y altura mínima de `48px`.
- **Focus:** contorno global ámbar; el hover aclara el fondo y revela borde gris azulado.
- **Labels:** siempre encima del campo, monoespaciadas, compactas y en mayúsculas.

### Navigation
- **Style:** barra Azul Abisal con marca a la izquierda y estado de datos a la derecha. La marca combina texto de alto peso con un glifo de tres barras; los metadatos usan monoespaciada pequeña. En móvil se oculta información secundaria, nunca la identidad ni el estado.

### Probability Rail

Riel vertical estrecho con base azul grisácea, relleno cian proporcional y marca ámbar. Debe conservar una relación directa y verificable con el porcentaje mostrado en la tarjeta.

## Do's and Don'ts

### Do:
- **Do** usar jerarquía tipográfica extrema para separar tesis, pregunta, cifra y metadato.
- **Do** mantener controles rectangulares, contrastados y con foco visible.
- **Do** representar probabilidad y confianza con señales gráficas además del texto.
- **Do** conservar el comportamiento responsive por apilado y reducción de columnas.
- **Do** usar sombras duras solo para separación estructural o respuesta interactiva.

### Don't:
- **Don't** convertir PELE en una fintech genérica con degradados púrpura, glassmorphism, brillos especulativos o botones cápsula.
- **Don't** llenar la pantalla de ámbar y cian; ambos colores pierden significado cuando dejan de ser señales.
- **Don't** redondear tarjetas y controles por defecto.
- **Don't** usar la tipografía display para párrafos o datos densos.
- **Don't** depender solo del color para comunicar probabilidad, confianza o estado.
