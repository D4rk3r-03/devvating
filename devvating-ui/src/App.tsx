import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import {
  Ban, Check, FileText, GitBranch, Hand, ListChecks, Play, RefreshCw, RotateCcw,
  Scale, Send, Swords, Trash2, TriangleAlert, Boxes,
} from "lucide-react";
import "./App.css";

// ---------------------------------------------------------------- tipos
type Usage = {
  input_tokens: number; output_tokens: number;
  cache_read_tokens: number; cache_creation_tokens: number;
  cost_usd: number | null;
};
type Msg =
  | { tipo: "inicio"; config: { tema: string; agentes: string[]; rounds: number; profundo: boolean; interactivo?: boolean; sesgos?: string[] } }
  | { tipo: "capacidades"; streaming: Record<string, boolean> }
  | { tipo: "evento"; evento: string; agente: string; texto: string | null }
  | { tipo: "fin"; sintesis: string; sintetizador: string; convergio: boolean; ronda_convergencia: number | null; usage: Record<string, Usage>; transcript: string; decisiones?: Decision[]; estado?: string }
  | { tipo: "error"; mensaje: string; resets_at?: string | null; parcial?: string | null }
  | { tipo: "intervencion_pendiente"; ronda: number; timeout: number }
  | { tipo: "intervencion_resuelta"; ronda: number; texto: string | null }
  | { tipo: "ejecucion_inicio"; transcript: string; repo: string }
  | { tipo: "ejecucion_evento"; evento: string; valor: string }
  | { tipo: "ejecucion_fin"; rama: string; rama_base: string; returncode: number; archivos: string[]; diff: string }
  | { tipo: "ejecucion_error"; mensaje: string }
  | { tipo: "commit_fin"; sha: string; rama: string }
  | { tipo: "descartar_fin"; base: string; rama: string }
  | { tipo: "cancelado"; parcial: string | null };

type Item =
  | { clase: "separador"; texto: string }
  | { clase: "turno"; agente: string; fase: string; texto: string }
  | { clase: "aviso"; texto: string };

type Decision = {
  id: string; pregunta: string; opciones: string[]; recomendada: string;
  crucial: boolean; contra: string; contra_en_debate: boolean;
  resuelta: boolean; eleccion: string;
};

type Rama = { nombre: string; sha: string; fecha: string; asunto: string; actual: boolean };
// Worktree de ejecución que quedó colgando. `tiene_cambios` = trabajo sin
// commitear que se perdería al retirarlo (la rama y sus commits, nunca).
type Worktree = { path: string; rama: string; existe: boolean; tiene_cambios: boolean };
// Un debate leído del disco (transcript). Lo mismo que el evento `fin` ofrece
// en vivo, pero recuperable en cualquier momento: reiniciar el Hub ya no
// cuesta perder el plan ni la posibilidad de aplicarlo.
type Archivado = {
  nombre: string; tema: string; sintesis: string; sintetizador: string;
  convergio: boolean; decisiones: Decision[]; estado: string; parcial: boolean;
};

// ------------------------------------------------------------- helpers
const escapeHtml = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const md = (s: string) => ({ __html: marked.parse(escapeHtml(s)) as string });

function nombreAgente(roster: string, alias: Record<string, string>): string {
  const canon = alias[roster] ?? roster;
  return canon.replace(/-(api|cli)$/, "");
}

const FASES: Record<string, string> = {
  propuesta: "Propuesta inicial", replica: "Réplica",
  inversion: "Inversión (steelman)", sintesis: "Síntesis",
};

// ---------------------------------------------------------- componentes
function Pendiente(
  { agente, fase, parcial, streaming }:
  { agente: string; fase: string; parcial: string; streaming: boolean },
) {
  const [seg, setSeg] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setSeg((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const mm = String(Math.floor(seg / 60)).padStart(2, "0");
  const ss = String(seg % 60).padStart(2, "0");
  return (
    <div className={`turno pendiente agente-${agente} animate-fade-in`}>
      <header>
        <span className="quien">{agente}</span>
        <span className="fase">{FASES[fase] ?? fase}…</span>
        <span className="cronometro">
          <RefreshCw size={13} className="girando" /> {mm}:{ss}
        </span>
      </header>
      {parcial ? (
        // Tokens en vivo: el texto crudo del stream (puede incluir la marca de
        // convergencia); al cerrar el turno lo reemplaza el _fin ya despojado.
        <div className="cuerpo streaming">
          <span dangerouslySetInnerHTML={md(parcial)} />
          <span className="cursor-stream" />
        </div>
      ) : streaming ? (
        <p className="pensando">pensando sobre el código…</p>
      ) : (
        // Degradación por capacidad: este agente no emite deltas; se avisa en
        // vez de dejar un vacío ambiguo. El turno llegará completo al terminar.
        <p className="pensando sin-stream">
          sin vista en vivo — el turno aparece completo al terminar
        </p>
      )}
    </div>
  );
}

// Panel de resolución de decisiones (F2): por decisión, opciones + recomendada
// + 'contra' (con marca si la cita no se verificó) + escribir la propia +
// confirmar/desmarcar 'crucial'. Al guardar, persiste en el transcript y
// devuelve qué decisiones cruciales siguen pendientes (gatean la ejecución).
function PanelDecisiones({ decisiones, transcript, post, onResuelto }: {
  decisiones: Decision[]; transcript: string;
  post: (u: string, b?: unknown) => Promise<Response>;
  onResuelto: (pendientes: string[]) => void;
}) {
  const [elec, setElec] = useState<Record<string, string>>({});
  const [propia, setPropia] = useState<Record<string, string>>({});
  const [usaPropia, setUsaPropia] = useState<Record<string, boolean>>({});
  const [crucial, setCrucial] = useState<Record<string, boolean>>(
    () => Object.fromEntries(decisiones.map((d) => [d.id, d.crucial])),
  );
  const [guardando, setGuardando] = useState(false);
  const [guardado, setGuardado] = useState(false);

  const eleccionDe = (d: Decision) =>
    (usaPropia[d.id] ? propia[d.id] : elec[d.id]) ?? "";

  const guardar = async () => {
    setGuardando(true);
    const r = await post("/api/decisiones", {
      transcript,
      decisiones: decisiones.map((d) => ({
        id: d.id, eleccion: eleccionDe(d),
        resuelta: !!eleccionDe(d).trim(), crucial: crucial[d.id],
      })),
    });
    setGuardando(false);
    if (r.ok) {
      const { pendientes } = await r.json();
      setGuardado(true);
      onResuelto(pendientes ?? []);
    }
  };

  return (
    <div className="panel-decisiones">
      <h4><ListChecks size={15} /> Decisiones para cerrar el plan</h4>
      {decisiones.map((d) => (
        <div key={d.id} className={`decision${crucial[d.id] ? " es-crucial" : ""}`}>
          <p className="d-pregunta">
            {d.pregunta}
            {crucial[d.id] && <span className="d-crucial">crucial</span>}
          </p>
          <div className="d-opciones">
            {d.opciones.map((o) => (
              <label key={o}>
                <input type="radio" name={`d-${d.id}`}
                  checked={!usaPropia[d.id] && elec[d.id] === o}
                  onChange={() => { setUsaPropia({ ...usaPropia, [d.id]: false }); setElec({ ...elec, [d.id]: o }); }} />
                <span className="d-op-txt">{o}</span>
                {o === d.recomendada && <b className="d-reco">★ recomendada</b>}
              </label>
            ))}
            <label className="d-propia">
              <input type="radio" name={`d-${d.id}`} checked={!!usaPropia[d.id]}
                onChange={() => setUsaPropia({ ...usaPropia, [d.id]: true })} />
              <input type="text" placeholder="…o escribe la tuya"
                value={propia[d.id] ?? ""}
                onFocus={() => setUsaPropia({ ...usaPropia, [d.id]: true })}
                onChange={(e) => setPropia({ ...propia, [d.id]: e.target.value })} />
            </label>
          </div>
          {d.contra && (
            <p className="d-contra">
              <b>Contra:</b> {d.contra}
              {!d.contra_en_debate && (
                <span className="d-marca" title="La cita no se localizó en la transcripción; verifícala a mano.">
                  <TriangleAlert size={12} /> cita no verificada
                </span>
              )}
            </p>
          )}
          <label className="check d-check">
            <input type="checkbox" checked={crucial[d.id]}
              onChange={(e) => setCrucial({ ...crucial, [d.id]: e.target.checked })} />
            crucial — bloquea la ejecución hasta resolverla
          </label>
        </div>
      ))}
      <div className="d-acciones">
        <button className="guardar-decisiones" onClick={guardar} disabled={guardando}>
          <Check size={14} /> {guardado ? "Guardar cambios" : "Guardar decisiones"}
        </button>
        {guardado && <span className="d-guardado">Guardado en el transcript.</span>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ App
export default function App() {
  const [roster, setRoster] = useState<string[]>([]);
  const [alias, setAlias] = useState<Record<string, string>>({});
  const [sesgosDisp, setSesgosDisp] = useState<string[]>([]);
  // Token anti-CSRF (paso 0, auto-auditoría): lo entrega /api/roster al montar
  // y viaja en cada POST mutante; sin este header el Hub responde 403.
  const [csrfToken, setCsrfToken] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [corriendo, setCorriendo] = useState(false);
  const [transcripts, setTranscripts] = useState<string[]>([]);
  const [ramas, setRamas] = useState<Rama[]>([]);
  const [worktrees, setWorktrees] = useState<Worktree[]>([]);
  const [huerfanos, setHuerfanos] = useState<string[]>([]);
  const [aviso, setAviso] = useState("");
  // Debate archivado que el vocero abrió del historial. El estado del debate
  // EN CURSO vive en memoria del servidor y se pierde al reiniciarlo; esto lo
  // recupera desde el transcript en disco, que es la fuente duradera.
  const [archivado, setArchivado] = useState<Archivado | null>(null);
  const [pendientesArchivado, setPendientesArchivado] = useState<string[]>([]);

  const [tema, setTema] = useState("");
  const [files, setFiles] = useState("");
  const [rounds, setRounds] = useState(2);
  const [profundo, setProfundo] = useState(false);
  const [interactivo, setInteractivo] = useState(false);
  const [parA, setParA] = useState("claude-cli");
  const [parB, setParB] = useState("gemini-api");
  const [sesgoA, setSesgoA] = useState("audaz");
  const [sesgoB, setSesgoB] = useState("cauto");
  const [nota, setNota] = useState("");
  const [ejecutando, setEjecutando] = useState(false);
  const [commitMsg, setCommitMsg] = useState("");
  // Preguntas de decisiones cruciales aún sin resolver: gatean "Ejecutar plan".
  const [decisionesPendientes, setDecisionesPendientes] = useState<string[]>([]);

  const finRef = useRef<HTMLDivElement>(null);

  // Auto-debate: el mismo agente dos veces (mismo nombre base). Solo entonces
  // tiene sentido asignar sesgos opuestos para romper el eco.
  const esAutodebate = useMemo(
    () => nombreAgente(parA, alias) === nombreAgente(parB, alias),
    [parA, parB, alias],
  );

  // POST mutante con el token CSRF adjunto (ver _requiere_csrf en hub.py).
  const post = (url: string, body?: unknown) =>
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Devvating-CSRF": csrfToken },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

  const cargarTranscripts = () =>
    fetch("/api/transcripts").then((r) => r.json())
      .then((d) => setTranscripts(d.transcripts));

  const cargarRamas = () =>
    fetch("/api/ramas").then((r) => r.json()).then((d) => setRamas(d.ramas ?? []));

  const borrarRama = async (nombre: string) => {
    if (!window.confirm(`¿Borrar la rama ${nombre}? Se pierde lo que no hayas fusionado.`))
      return;
    const r = await post("/api/ramas/borrar", { rama: nombre });
    if (!r.ok) setAviso((await r.json()).detail ?? "No se pudo borrar la rama.");
    cargarRamas();
  };

  // Abre un debate del historial con todas sus acciones vivas (ejecutar,
  // resolver decisiones, cerrar plan, reanudar si quedó a medias).
  const abrirArchivado = async (nombre: string) => {
    const r = await fetch(`/api/transcripts/${encodeURIComponent(nombre)}`);
    if (!r.ok) { setAviso("No se pudo leer el transcript."); return; }
    const d = await r.json();
    const decisiones: Decision[] = d.decisiones ?? [];
    setArchivado({
      nombre,
      tema: d.topic?.prompt ?? "(sin tema)",
      sintesis: d.synthesis ?? "",
      sintetizador: d.synthesizer ?? "?",
      convergio: !!d.converged,
      decisiones,
      estado: d.estado ?? "",
      parcial: nombre.endsWith(".partial.json"),
    });
    // Mismo gate que en vivo: las decisiones cruciales sin resolver bloquean
    // la ejecución. Se recalcula desde el disco, que ya guarda las resueltas.
    setPendientesArchivado(
      decisiones.filter((x) => x.crucial && !x.resuelta).map((x) => x.pregunta),
    );
  };

  const cargarWorktrees = () =>
    fetch("/api/worktrees").then((r) => r.json()).then((d) => {
      setWorktrees(d.worktrees ?? []); setHuerfanos(d.huerfanos ?? []);
    });

  // `forzar` incluye los que tienen cambios sin commitear: eso SÍ se pierde,
  // así que va detrás de una confirmación aparte y explícita.
  const limpiarWorktrees = async (forzar: boolean) => {
    const conTrabajo = worktrees.filter((w) => w.tiene_cambios).length;
    if (forzar && !window.confirm(
      `Se descartarán los cambios sin commitear de ${conTrabajo} worktree(s). ` +
      "Las ramas y sus commits se conservan. ¿Continuar?"
    )) return;
    const r = await post("/api/worktrees/limpiar", { forzar });
    if (!r.ok) {
      setAviso((await r.json()).detail ?? "No se pudo limpiar.");
      return;
    }
    const d = await r.json();
    const partes = [];
    if (d.retirados) partes.push(`${d.retirados} worktree(s) retirados`);
    if (d.huerfanos) partes.push(`${d.huerfanos} huérfano(s) borrados`);
    setAviso(partes.length ? partes.join(" · ") : "No había nada que limpiar.");
    cargarWorktrees();
  };

  useEffect(() => {
    fetch("/api/roster").then((r) => r.json()).then((d) => {
      setRoster(d.agentes); setAlias(d.alias); setSesgosDisp(d.sesgos ?? []);
      setCsrfToken(d.csrf_token ?? "");
    });
    cargarTranscripts();
    cargarRamas();
    cargarWorktrees();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.tipo === "historial") {
        setMsgs(m.eventos); setCorriendo(m.corriendo);
      } else {
        setMsgs((prev) => [...prev, m]);
        if (m.tipo === "fin" || m.tipo === "error" || m.tipo === "cancelado") {
          setCorriendo(false); cargarTranscripts();
        }
      }
    };
    ws.onclose = () => setAviso("Conexión perdida con el Hub — recarga la página.");
    return () => ws.close();
  }, []);

  // Reducción de mensajes → items del feed + turno pendiente + paneles.
  const { items, pendiente, capacidades, config, fin, error, cancelado, intervencion, ejecucion, cierre } = useMemo(() => {
    const items: Item[] = [];
    let pendiente: { agente: string; fase: string; parcial: string } | null = null;
    let capacidades: Record<string, boolean> = {};
    let config: Extract<Msg, { tipo: "inicio" }>["config"] | null = null;
    let fin: Extract<Msg, { tipo: "fin" }> | null = null;
    let error: Extract<Msg, { tipo: "error" }> | null = null;
    let cancelado: Extract<Msg, { tipo: "cancelado" }> | null = null;
    let intervencion: { ronda: number } | null = null;
    let ejecucion:
      | { estado: "corriendo"; detalle: string }
      | { estado: "fin"; rama: string; rama_base: string; archivos: string[]; diff: string; returncode: number }
      | { estado: "error"; mensaje: string }
      | null = null;
    let cierre:
      | { tipo: "commit"; sha: string; rama: string }
      | { tipo: "descartar"; base: string }
      | null = null;
    for (const m of msgs) {
      if (m.tipo === "inicio") config = m.config;
      else if (m.tipo === "capacidades") capacidades = m.streaming;
      else if (m.tipo === "fin") { fin = m; pendiente = null; }
      else if (m.tipo === "error") { error = m; pendiente = null; }
      else if (m.tipo === "cancelado") { cancelado = m; pendiente = null; intervencion = null; }
      else if (m.tipo === "intervencion_pendiente") intervencion = { ronda: m.ronda };
      else if (m.tipo === "intervencion_resuelta") {
        intervencion = null;
        items.push({
          clase: "aviso",
          texto: m.texto ? `vocero (ronda ${m.ronda}): ${m.texto}` : `ronda ${m.ronda}: sin nota del vocero`,
        });
      }
      else if (m.tipo === "ejecucion_inicio")
        ejecucion = { estado: "corriendo", detalle: "preparando rama…" };
      else if (m.tipo === "ejecucion_evento")
        ejecucion = { estado: "corriendo", detalle: `${m.evento}: ${m.valor}` };
      else if (m.tipo === "ejecucion_fin")
        ejecucion = { estado: "fin", rama: m.rama, rama_base: m.rama_base, archivos: m.archivos, diff: m.diff, returncode: m.returncode };
      else if (m.tipo === "ejecucion_error")
        ejecucion = { estado: "error", mensaje: m.mensaje };
      else if (m.tipo === "commit_fin") cierre = { tipo: "commit", sha: m.sha, rama: m.rama };
      else if (m.tipo === "descartar_fin") cierre = { tipo: "descartar", base: m.base };
      else if (m.tipo === "evento") {
        const { evento, agente, texto } = m;
        if (evento === "ronda") items.push({ clase: "separador", texto: agente });
        else if (evento === "convergencia")
          items.push({ clase: "separador", texto: `✓ convergencia en ${agente}` });
        else if (evento === "reintento")
          items.push({ clase: "aviso", texto: `${agente}: ${texto}` });
        else if (evento === "delta") {
          // Token en vivo del turno en curso: se acumula en el pendiente y se
          // muestra al momento; el _fin lo reemplaza por el texto despojado.
          if (pendiente && texto != null)
            pendiente = {
              agente: pendiente.agente, fase: pendiente.fase,
              parcial: pendiente.parcial + texto,
            };
        }
        else if (evento.endsWith("_inicio"))
          pendiente = { agente, fase: evento.replace(/_inicio$/, ""), parcial: "" };
        else if (evento.endsWith("_fin") && texto != null) {
          pendiente = null;
          items.push({ clase: "turno", agente, fase: evento.replace(/_fin$/, ""), texto });
        }
      }
    }
    return { items, pendiente, capacidades, config, fin, error, cancelado, intervencion, ejecucion, cierre };
  }, [msgs]);

  // Al llegar la síntesis, las decisiones cruciales sin resolver gatean la
  // ejecución (el PanelDecisiones actualiza esto al guardar).
  useEffect(() => {
    const ds = fin?.decisiones ?? [];
    setDecisionesPendientes(ds.filter((d) => d.crucial && !d.resuelta).map((d) => d.pregunta));
  }, [fin]);

  useEffect(() => {
    if (ejecucion && ejecucion.estado !== "corriendo") setEjecutando(false);
    // Sugerencia editable de mensaje de commit: el tema del debate.
    if (ejecucion?.estado === "fin" && !commitMsg && config?.tema)
      setCommitMsg(config.tema);
  }, [ejecucion, config, commitMsg]);

  // El historial de ramas cambia al ejecutar (aparece una) o al commitear/
  // descartar (cambia su estado o se borra): refrescarlo entonces. Los
  // worktrees siguen el mismo ciclo — cada ejecución crea uno, y commitear o
  // descartar lo retira.
  useEffect(() => { cargarRamas(); cargarWorktrees(); }, [cierre, ejecucion?.estado]);

  useEffect(() => {
    finRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items.length, pendiente, fin, intervencion, ejecucion]);

  const ladoDe = (agente: string) =>
    config && nombreAgente(config.agentes[1], alias) === agente ? "der" : "izq";

  const lanzar = async () => {
    setAviso("");
    const r = await post("/api/debates", {
      tema, files, rounds, profundo, interactivo, agentes: [parA, parB],
      sesgos: esAutodebate ? [sesgoA, sesgoB] : [],
    });
    if (r.ok) setCorriendo(true);
    else setAviso((await r.json()).detail ?? "No se pudo lanzar el debate.");
  };

  const cancelarDebate = async () => {
    const r = await post("/api/debates/cancelar");
    if (!r.ok) setAviso((await r.json()).detail ?? "No se pudo cancelar el debate.");
  };

  const enviarNota = async (texto: string) => {
    await post("/api/intervencion", { nota: texto });
    setNota("");
  };

  const ejecutarPlan = async (transcript: string, forzar = false) => {
    setEjecutando(true);
    const r = await post("/api/ejecutar", { transcript, forzar_decisiones: forzar });
    if (!r.ok) {
      setEjecutando(false);
      setAviso((await r.json()).detail ?? "No se pudo lanzar la ejecución.");
    }
  };

  const commitear = async () => {
    const r = await post("/api/commit", { mensaje: commitMsg });
    if (!r.ok) setAviso((await r.json()).detail ?? "No se pudo commitear.");
    else setCommitMsg("");
  };

  const descartar = async () => {
    const r = await post("/api/descartar");
    if (!r.ok) setAviso((await r.json()).detail ?? "No se pudo descartar.");
  };

  const reanudar = async (parcial: string) => {
    setAviso("");
    const r = await post("/api/debates", {
      agentes: [parA, parB], rounds, resume: parcial,
      sesgos: esAutodebate ? [sesgoA, sesgoB] : [],
    });
    if (r.ok) setCorriendo(true);
    else setAviso((await r.json()).detail ?? "No se pudo reanudar el debate.");
  };

  // F3 — ronda de cierre: re-sintetiza con las decisiones ya resueltas como
  // restricciones fijas, para producir un plan cerrado (una ronda nueva).
  const cerrarPlan = async (transcript: string) => {
    setAviso("");
    const r = await post("/api/cerrar-plan", {
      transcript, agentes: [parA, parB],
      sesgos: esAutodebate ? [sesgoA, sesgoB] : [],
    });
    if (r.ok) setCorriendo(true);
    else setAviso((await r.json()).detail ?? "No se pudo cerrar el plan.");
  };

  return (
    <div className="app-container">
      <aside className="sidebar">
        <header className="sidebar-header">
          <h1 className="text-gradient marca"><Swords size={20} /> Devvating Hub</h1>
          <p className="lema">Dos agentes debaten sobre tu código. Tú arbitras.</p>
        </header>

        <div className="sidebar-scroll">
          <section className="sidebar-seccion">
            <h2 className="titulo-lista"><Play size={13} /> Nuevo debate</h2>
            <label>Tema del debate
              <textarea value={tema} onChange={(e) => setTema(e.target.value)}
                rows={3} placeholder="¿Conviene X o Y?" disabled={corriendo} />
            </label>
            <label>Archivos pista (opcional)
              <input value={files} onChange={(e) => setFiles(e.target.value)}
                placeholder="src/a.py, src/b.py" disabled={corriendo} />
            </label>
            <div className="fila">
              <label>Agente A
                <select value={parA} onChange={(e) => setParA(e.target.value)} disabled={corriendo}>
                  {roster.map((n) => <option key={n}>{n}</option>)}
                </select>
              </label>
              <label>Agente B
                <select value={parB} onChange={(e) => setParB(e.target.value)} disabled={corriendo}>
                  {roster.map((n) => <option key={n}>{n}</option>)}
                </select>
              </label>
            </div>
            {esAutodebate && (
              <>
                <p className="pista-sesgos">
                  <Scale size={13} /> Mismo agente dos veces: asígnales inclinaciones
                  opuestas para que debatan de verdad y no hagan eco.
                </p>
                <div className="fila">
                  <label>Sesgo A
                    <select value={sesgoA} onChange={(e) => setSesgoA(e.target.value)} disabled={corriendo}>
                      {sesgosDisp.map((s) => <option key={s}>{s}</option>)}
                    </select>
                  </label>
                  <label>Sesgo B
                    <select value={sesgoB} onChange={(e) => setSesgoB(e.target.value)} disabled={corriendo}>
                      {sesgosDisp.map((s) => <option key={s}>{s}</option>)}
                    </select>
                  </label>
                </div>
              </>
            )}
            <div className="fila">
              <label>Rondas
                <input type="number" min={1} max={5} value={rounds}
                  onChange={(e) => setRounds(+e.target.value)} disabled={corriendo} />
              </label>
              <label className="check">
                <input type="checkbox" checked={profundo}
                  onChange={(e) => setProfundo(e.target.checked)} disabled={corriendo} />
                profundo
              </label>
              <label className="check">
                <input type="checkbox" checked={interactivo}
                  onChange={(e) => setInteractivo(e.target.checked)} disabled={corriendo} />
                interactivo
              </label>
            </div>
            <button className="lanzar" onClick={lanzar} disabled={corriendo || !tema.trim()}>
              <Play size={16} /> {corriendo ? "Debate en curso…" : "Lanzar debate"}
            </button>
            {aviso && <p className="aviso-form"><TriangleAlert size={14} /> {aviso}</p>}
          </section>

          <section className="sidebar-seccion">
            <h2 className="titulo-lista"><FileText size={14} /> Debates anteriores</h2>
            <ul className="lista-transcripts">
              {transcripts.map((t) => (
                <li key={t} className={archivado?.nombre === t ? "abierto" : ""}>
                  <button className="abrir-transcript" title={`Abrir ${t}`}
                    onClick={() => abrirArchivado(t)}>
                    {t.replace(/\.partial\.json$|\.json$/, "").slice(16)}
                    {t.endsWith(".partial.json") && <span className="etiqueta-parcial">a medias</span>}
                  </button>
                  <a href={`/api/transcripts/${encodeURIComponent(t)}/html`} target="_blank"
                    rel="noreferrer" className="ver-html" title="Abrir el reporte completo">
                    <FileText size={12} />
                  </a>
                </li>
              ))}
              {transcripts.length === 0 && <li className="vacio">aún ninguno</li>}
            </ul>
          </section>

          <section className="sidebar-seccion">
            <h2 className="titulo-lista"><GitBranch size={14} /> Ramas de ejecución</h2>
            <ul className="lista-ramas">
              {ramas.map((r) => (
                <li key={r.nombre} className={r.actual ? "actual" : ""}>
                  <div className="rama-info">
                    <span className="rama-nombre" title={r.nombre}>
                      {r.nombre.replace(/^devvating\//, "")}
                    </span>
                    <span className="rama-asunto" title={r.asunto}>
                      {r.asunto || "(sin commit)"}
                    </span>
                  </div>
                  {r.actual ? (
                    <span className="rama-actual" title="rama actual">aquí</span>
                  ) : (
                    <button className="rama-borrar" title="Borrar rama"
                      onClick={() => borrarRama(r.nombre)}>
                      <Trash2 size={13} />
                    </button>
                  )}
                </li>
              ))}
              {ramas.length === 0 && <li className="vacio">ninguna</li>}
            </ul>
          </section>

          {(worktrees.length > 0 || huerfanos.length > 0) && (
            <section className="sidebar-seccion">
              <h2 className="titulo-lista"><Boxes size={14} /> Worktrees colgando</h2>
              <ul className="lista-ramas">
                {worktrees.map((w) => (
                  <li key={w.path}>
                    <div className="rama-info">
                      <span className="rama-nombre" title={w.path}>
                        {w.rama.replace(/^devvating\//, "")}
                      </span>
                      <span className="rama-asunto">
                        {w.tiene_cambios ? "cambios sin commitear" : "sin cambios"}
                      </span>
                    </div>
                    {w.tiene_cambios && (
                      <span className="wt-marca" title="Tiene trabajo sin commitear: no se retira sin forzar.">
                        <TriangleAlert size={13} />
                      </span>
                    )}
                  </li>
                ))}
                {huerfanos.map((h) => (
                  <li key={h}>
                    <div className="rama-info">
                      <span className="rama-nombre" title={h}>{h}</span>
                      <span className="rama-asunto">huérfano · su repo ya no existe</span>
                    </div>
                  </li>
                ))}
              </ul>
              <div className="wt-acciones">
                <button className="wt-limpiar" onClick={() => limpiarWorktrees(false)}
                  title="Retira los que no tienen cambios sin commitear, y los huérfanos.">
                  <Trash2 size={13} /> Limpiar
                </button>
                {worktrees.some((w) => w.tiene_cambios) && (
                  <button className="wt-limpiar peligro" onClick={() => limpiarWorktrees(true)}
                    title="Retira TAMBIÉN los que tienen cambios sin commitear: esos cambios se pierden (las ramas no).">
                    <TriangleAlert size={13} /> Forzar
                  </button>
                )}
              </div>
            </section>
          )}
        </div>
      </aside>

      <main className="main-content">
        <header className="barra-estado glass-panel">
          <span className={`status-dot ${corriendo ? "progress" : "active"}`} />
          <span>{corriendo ? "debate en curso" : "en reposo"}</span>
          {config && <span className="tema-actual" title={config.tema}>
            {nombreAgente(config.agentes[0], alias)} <Swords size={13} /> {nombreAgente(config.agentes[1], alias)}
            {" · "}≤{config.rounds} rondas{config.profundo ? " · profundo" : ""}
            {config.sesgos && config.sesgos.length === 2 ? ` · ${config.sesgos.join("/")}` : ""}
          </span>}
          {corriendo && (
            <button className="cancelar-debate" onClick={cancelarDebate}
              title="Detener el debate en curso (se guarda lo hecho para reanudar)">
              <Ban size={14} /> Cancelar
            </button>
          )}
        </header>

        <section className="feed">
          {items.length === 0 && !pendiente && !fin && (
            <div className="vacio-feed">
              <Swords size={40} />
              <p>La arena está lista. Plantea un tema y lanza el debate.</p>
            </div>
          )}
          {items.map((it, i) =>
            it.clase === "separador" ? (
              <div key={i} className="separador"><span>{it.texto}</span></div>
            ) : it.clase === "aviso" ? (
              <div key={i} className="aviso-feed"><TriangleAlert size={14} /> {it.texto}</div>
            ) : (
              <article key={i}
                className={`turno agente-${it.agente} lado-${ladoDe(it.agente)} animate-fade-in`}>
                <header>
                  <span className="quien">{it.agente}</span>
                  <span className="fase">{FASES[it.fase] ?? it.fase}</span>
                </header>
                <div className="cuerpo" dangerouslySetInnerHTML={md(it.texto)} />
              </article>
            )
          )}
          {pendiente && (
            <Pendiente agente={pendiente.agente} fase={pendiente.fase}
              parcial={pendiente.parcial} streaming={capacidades[pendiente.agente] ?? false} />
          )}

          {intervencion && (
            <div className="panel-intervencion glass-panel animate-fade-in">
              <h3><Hand size={16} /> Tu turno, vocero — antes de la ronda {intervencion.ronda}</h3>
              <p className="pista-intervencion">Inyecta una directriz para los agentes,
                o continúa sin nota. El debate espera.</p>
              <div className="fila-nota">
                <input value={nota} onChange={(e) => setNota(e.target.value)}
                  placeholder="ej.: prioricen la opción con menos dependencias…"
                  onKeyDown={(e) => e.key === "Enter" && enviarNota(nota)} autoFocus />
                <button onClick={() => enviarNota(nota)} disabled={!nota.trim()}>
                  <Send size={14} /> Enviar
                </button>
                <button className="secundario" onClick={() => enviarNota("")}>
                  Continuar sin nota
                </button>
              </div>
            </div>
          )}

          {error && (
            <div className="panel-error glass-panel">
              <h3><TriangleAlert size={16} /> Debate interrumpido</h3>
              <p>{error.mensaje}</p>
              {error.resets_at && <p>La cuota se reinicia a las <b>{error.resets_at}</b>.</p>}
              {error.parcial && (
                <>
                  <p>Turnos pagados a salvo en <code>{error.parcial}</code>. Nada se perdió.</p>
                  <button className="reanudar" onClick={() => reanudar(error.parcial!)}
                    disabled={corriendo}>
                    <RotateCcw size={15} /> Reanudar debate
                  </button>
                </>
              )}
            </div>
          )}

          {cancelado && (
            <div className="panel-cancelado glass-panel">
              <h3><Ban size={16} /> Debate cancelado</h3>
              {cancelado.parcial ? (
                <>
                  <p>Lo detuviste. Los turnos completados quedaron a salvo en{" "}
                    <code>{cancelado.parcial}</code>.</p>
                  <button className="reanudar" onClick={() => reanudar(cancelado.parcial!)}
                    disabled={corriendo}>
                    <RotateCcw size={15} /> Reanudar debate
                  </button>
                </>
              ) : (
                <p>Lo detuviste antes de que hubiera turnos completados.</p>
              )}
            </div>
          )}

          {archivado && (
            <div className="panel-sintesis glass-panel animate-fade-in archivado">
              <h3>
                <FileText size={16} /> Debate archivado
                {!archivado.parcial && <> · síntesis por {archivado.sintetizador}</>}
                {archivado.estado === "pendiente_decision" &&
                  <span className="badge-estado" title="Tiene decisiones cruciales sin resolver.">
                    decisión pendiente</span>}
                <button className="cerrar-archivado" title="Cerrar"
                  onClick={() => { setArchivado(null); setPendientesArchivado([]); }}>
                  <Ban size={14} />
                </button>
              </h3>
              <p className="tema-archivado">{archivado.tema}</p>

              {archivado.parcial ? (
                <p>Este debate quedó a medias. Puedes reanudarlo: los turnos ya
                  pagados se reutilizan y solo corre lo que falta.</p>
              ) : (
                <div className="cuerpo" dangerouslySetInnerHTML={md(archivado.sintesis)} />
              )}

              {archivado.decisiones.length > 0 && (
                <PanelDecisiones decisiones={archivado.decisiones} transcript={archivado.nombre}
                  post={post} onResuelto={setPendientesArchivado} />
              )}

              <footer>
                <a href={`/api/transcripts/${encodeURIComponent(archivado.nombre)}/html`}
                  target="_blank" rel="noreferrer" className="ver-reporte">
                  <FileText size={14} /> reporte completo
                </a>
                {archivado.parcial ? (
                  <button className="ejecutar" disabled={corriendo}
                    onClick={() => reanudar(archivado.nombre)}>
                    <RotateCcw size={14} /> Reanudar debate
                  </button>
                ) : (
                  <>
                    {archivado.decisiones.length > 0 && (
                      <button className="cerrar-plan" disabled={corriendo}
                        onClick={() => cerrarPlan(archivado.nombre)}
                        title="Re-sintetiza con tus decisiones ya fijadas para dejar un plan sin ambigüedades.">
                        <ListChecks size={14} /> Cerrar plan
                      </button>
                    )}
                    <button className="ejecutar"
                      disabled={ejecutando || corriendo || pendientesArchivado.length > 0 || !archivado.sintesis}
                      onClick={() => ejecutarPlan(archivado.nombre)}
                      title={pendientesArchivado.length > 0
                        ? "Resuelve las decisiones cruciales antes de ejecutar."
                        : "Aplica el plan en una rama nueva; nada se commitea."}>
                      <GitBranch size={14} /> {ejecutando ? "Ejecutando…" : "Ejecutar plan"}
                    </button>
                  </>
                )}
              </footer>
              {pendientesArchivado.length > 0 && (
                <p className="pista-gate">
                  <TriangleAlert size={13} /> Faltan decisiones cruciales:{" "}
                  {pendientesArchivado.join("; ")}.{" "}
                  <button className="forzar-link"
                    onClick={() => ejecutarPlan(archivado.nombre, true)}>
                    forzar ejecución bajo mi riesgo
                  </button>
                </p>
              )}
            </div>
          )}

          {fin && (
            <div className="panel-sintesis glass-panel">
              <h3><Scale size={16} /> Síntesis (por {fin.sintetizador}) ·{" "}
                {fin.convergio ? `convergieron en la ronda ${fin.ronda_convergencia}` : "sin convergencia"}
                {fin.estado === "pendiente_decision" &&
                  <span className="badge-estado" title="El plan tiene decisiones cruciales sin resolver.">
                    pendiente de decisión</span>}</h3>
              <div className="cuerpo" dangerouslySetInnerHTML={md(fin.sintesis)} />
              {fin.decisiones && fin.decisiones.length > 0 && (
                <PanelDecisiones decisiones={fin.decisiones} transcript={fin.transcript}
                  post={post} onResuelto={setDecisionesPendientes} />
              )}
              <footer>
                {Object.entries(fin.usage).map(([n, u]) => (
                  <span key={n} className="uso">
                    <b>{n}</b> {u.input_tokens.toLocaleString()}→{u.output_tokens.toLocaleString()} tok
                    {u.cost_usd != null && ` · $${u.cost_usd.toFixed(4)}`}
                  </span>
                ))}
                <a href={`/api/transcripts/${encodeURIComponent(fin.transcript)}/html`}
                  target="_blank" rel="noreferrer" className="ver-reporte">
                  <FileText size={14} /> reporte completo
                </a>
                {fin.decisiones && fin.decisiones.length > 0 && (
                  <button className="cerrar-plan" disabled={corriendo}
                    onClick={() => cerrarPlan(fin.transcript)}
                    title="Corre una ronda de cierre que re-sintetiza con tus decisiones ya fijadas, para dejar un plan sin ambigüedades.">
                    <ListChecks size={14} /> Cerrar plan
                  </button>
                )}
                <button className="ejecutar"
                  disabled={ejecutando || corriendo || decisionesPendientes.length > 0}
                  onClick={() => ejecutarPlan(fin.transcript)}
                  title={decisionesPendientes.length > 0
                    ? "Resuelve las decisiones cruciales antes de ejecutar."
                    : "Aplica el plan en una rama nueva; nada se commitea."}>
                  <GitBranch size={14} /> {ejecutando ? "Ejecutando…" : "Ejecutar plan"}
                </button>
              </footer>
              {decisionesPendientes.length > 0 && (
                <p className="pista-gate">
                  <TriangleAlert size={13} /> Faltan decisiones cruciales:{" "}
                  {decisionesPendientes.join("; ")}.{" "}
                  <button className="forzar-link" onClick={() => ejecutarPlan(fin.transcript, true)}>
                    forzar ejecución bajo mi riesgo
                  </button>
                </p>
              )}
            </div>
          )}

          {ejecucion && (
            <div className={`panel-ejecucion glass-panel animate-fade-in ${ejecucion.estado}`
              + (ejecucion.estado === "fin" && ejecucion.returncode !== 0 ? " fin-con-error" : "")}>
              {ejecucion.estado === "corriendo" && (
                <h3><RefreshCw size={16} className="girando" /> Ejecutando el plan — {ejecucion.detalle}</h3>
              )}
              {ejecucion.estado === "error" && (
                <>
                  <h3><TriangleAlert size={16} /> La ejecución no pudo correr</h3>
                  <p>{ejecucion.mensaje}</p>
                </>
              )}
              {ejecucion.estado === "fin" && (
                <>
                  {ejecucion.returncode !== 0 ? (
                    <h3><TriangleAlert size={16} /> El plan no se aplicó limpio (código{" "}
                      {ejecucion.returncode}) · rama <code>{ejecucion.rama}</code></h3>
                  ) : (
                    <h3><GitBranch size={16} /> Cambios en staging · rama <code>{ejecucion.rama}</code></h3>
                  )}
                  {ejecucion.archivos.length === 0 ? (
                    <p>El ejecutor no produjo cambios.</p>
                  ) : (
                    <>
                      <p>{ejecucion.archivos.length} archivo(s): {ejecucion.archivos.join(", ")}</p>
                      <pre className="diff">{ejecucion.diff.split("\n").map((l, i) => (
                        <span key={i} className={
                          l.startsWith("+") && !l.startsWith("+++") ? "mas"
                          : l.startsWith("-") && !l.startsWith("---") ? "menos"
                          : l.startsWith("@@") ? "hunk" : ""
                        }>{l + "\n"}</span>
                      ))}</pre>
                    </>
                  )}
                  {cierre ? (
                    <p className="cierre-hecho">
                      <Check size={15} />{" "}
                      {cierre.tipo === "commit"
                        ? <>Commiteado en <code>{cierre.rama}</code> (<code>{cierre.sha}</code>).
                            Fusiónalo a tu rama cuando lo revises.</>
                        : <>Descartado: volviste a <code>{cierre.base}</code> y la rama de
                            ejecución se borró. El repo quedó como estaba.</>}
                    </p>
                  ) : ejecucion.returncode !== 0 ? (
                    <div className="acciones-cierre">
                      <p className="pista-cierre fallo">
                        El backend terminó con error: el plan quedó a medias. Revisa el diff
                        de arriba, pero <b>no se ofrece commit</b> — commitear un fallo
                        presentaría éxito sobre un plan roto. Descarta para volver a{" "}
                        <code>{ejecucion.rama_base}</code>.
                      </p>
                      <button className="descartar" onClick={descartar}>
                        <Trash2 size={14} /> Descartar
                      </button>
                    </div>
                  ) : ejecucion.archivos.length > 0 ? (
                    <div className="acciones-cierre">
                      <label>Mensaje de commit (editable)
                        <textarea value={commitMsg} rows={2}
                          onChange={(e) => setCommitMsg(e.target.value)} />
                      </label>
                      <div className="fila-cierre">
                        <button className="commit" onClick={commitear}
                          disabled={!commitMsg.trim()}>
                          <GitBranch size={14} /> Commit en la rama
                        </button>
                        <button className="descartar" onClick={descartar}>
                          <Trash2 size={14} /> Descartar
                        </button>
                      </div>
                      <p className="pista-cierre">El commit queda en <code>{ejecucion.rama}</code>,
                        no toca tu rama de trabajo. Descartar vuelve a{" "}
                        <code>{ejecucion.rama_base}</code> y borra la rama.</p>
                    </div>
                  ) : (
                    <button className="descartar" onClick={descartar}>
                      <Trash2 size={14} /> Descartar rama vacía
                    </button>
                  )}
                </>
              )}
            </div>
          )}
          <div ref={finRef} />
        </section>
      </main>
    </div>
  );
}
