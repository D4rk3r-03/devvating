# Protocolo operativo del vocero

Cómo usar DEVVATING de punta a punta, con los frenos de seguridad en cada
paso. Las fases y decisiones de diseño (D1–D4) están en `DISENO.md` §3 y §11.

## Flujo completo

```
1. PLANTEAMIENTO   el vocero formula el tema + pista de archivos (D1)
2. DEBATE          apertura a ciegas → rondas de réplica → [inversión] → síntesis
                   (solo lectura; el vocero puede intervenir entre rondas, D4)
3. ARBITRAJE       el vocero lee la síntesis: acuerdos, desacuerdos, plan
4. EJECUCIÓN       solo tras aprobación explícita; en rama; diff al final (D2)
5. CIERRE          el vocero revisa el diff y commitea o descarta la rama
```

### 1. Plantear el tema

```bash
devvating debate "¿<problema, mejora o decisión>?" --files "ruta1.py, ruta2.py"
```

- Formula el tema como decisión debatible, no como orden ("¿conviene X o Y?"
  mejor que "haz X").
- `--files` es una **pista**, no un límite: los agentes pueden leer cualquier
  archivo del repo (solo lectura, confinados a la raíz).
- Defaults por proyecto en `.devvating.json`; los flags mandan.

### 2. Conducir el debate

- `--rounds N` — tope de rondas de réplica (default 2). El debate corta antes
  si ambos declaran convergencia en la misma ronda.
- `--interactivo` — te pregunta una nota antes de cada ronda (D4). Úsalo
  cuando el tema tenga restricciones que los agentes no pueden inferir del
  código (presupuesto, deadlines, preferencias).
- `--profundo` — añade la ronda de inversión (cada agente defiende la postura
  contraria). Duplica coste aproximadamente; resérvalo para decisiones caras
  de revertir.
- `--synthesizer auto` (default) rota quién sintetiza entre debates para no
  sesgar siempre al mismo modelo (D3).

### 3. Arbitrar

La síntesis siempre tiene tres secciones: **Acuerdos**, **Desacuerdos
abiertos** y **Plan propuesto**. Protocolo:

- Si hay desacuerdos abiertos, decídelos tú **antes** de ejecutar — edita el
  plan o relanza el debate con una nota. No ejecutes un plan con ambigüedades
  marcadas como "depende del vocero".
- El transcript queda en `transcripts/<fecha>-<tema>.json`; la síntesis es el
  campo `synthesis`.

### 4. Ejecutar

```bash
devvating ejecutar --repo /ruta/al/proyecto --from-transcript transcripts/xxx.json
# o con un plan editado a mano:
devvating ejecutar --repo /ruta/al/proyecto --plan-file plan.md
```

Guardas automáticas (no desactivables salvo flag explícito):

| Guarda | Comportamiento |
|--------|----------------|
| Repo git limpio | Rechaza árbol con cambios sin confirmar |
| Rama | Todo ocurre en `devvating/<slug>-<fecha>` (o `--branch`) |
| Aprobación | Pregunta y/N antes de tocar nada (omitible con `--yes`) |
| Sin comandos | El agente headless solo puede Read/Edit/Write |
| Sin commit | Los cambios quedan en staging; el commit es tuyo |

`--allow-commands` deja al agente correr comandos arbitrarios
(`--dangerously-skip-permissions`). Úsalo solo cuando el plan lo exija
(instalar dependencias, correr migraciones) y revisa el diff con lupa.

### 5. Cerrar

```bash
git diff --cached            # revisar (ya está en staging)
git commit                   # conservar
# o descartar:
git checkout main && git branch -D devvating/<rama> && git checkout -- .
```

## Checklist de seguridad (resumen)

- [ ] El debate es **solo lectura** por construcción — nunca registrar
      herramientas WRITE en el orquestador.
- [ ] Claves solo en `.env` (jamás versionado; plantilla en `.env.example`).
- [ ] Ejecutar siempre sobre árbol limpio y en rama.
- [ ] `--allow-commands` y `--yes` nunca juntos sin haber leído el plan.
- [ ] Ante un diff sospechoso: descartar la rama; no hay nada commiteado.

## Costes

Cada debate = 2 aperturas + 2×rondas réplicas + síntesis (+2 si `--profundo`),
cada una con hasta `DEVVATING_MAX_TOOL_ITERATIONS` (8) llamadas de herramienta.
Con los defaults: ~7 turnos de agente por debate. Subir `--rounds` o el tope de
iteraciones multiplica el gasto.
