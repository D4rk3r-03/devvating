import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import {
  Check, FileText, GitBranch, Hand, Play, RefreshCw, RotateCcw, Scale, Send,
  Swords, Trash2, TriangleAlert,
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
  | { tipo: "evento"; evento: string; agente: string; texto: string | null }
  | { tipo: "fin"; sintesis: string; sintetizador: string; convergio: boolean; ronda_convergencia: number | null; usage: Record<string, Usage>; transcript: string }
  | { tipo: "error"; mensaje: string; resets_at?: string | null; parcial?: string | null }
  | { tipo: "intervencion_pendiente"; ronda: number; timeout: number }
  | { tipo: "intervencion_resuelta"; ronda: number; texto: string | null }
  | { tipo: "ejecucion_inicio"; transcript: string; repo: string }
  | { tipo: "ejecucion_evento"; evento: string; valor: string }
  | { tipo: "ejecucion_fin"; rama: string; rama_base: string; returncode: number; archivos: string[]; diff: string }
  | { tipo: "ejecucion_error"; mensaje: string }
  | { tipo: "commit_fin"; sha: string; rama: string }
  | { tipo: "descartar_fin"; base: string; rama: string };

type Item =
  | { clase: "separador"; texto: string }
  | { clase: "turno"; agente: string; fase: string; texto: string }
  | { clase: "aviso"; texto: string };

type Rama = { nombre: string; sha: string; fecha: string; asunto: string; actual: boolean };

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
function Pendiente({ agente, fase }: { agente: string; fase: string }) {
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
      <p className="pensando">pensando sobre el código…</p>
    </div>
  );
}

// ------------------------------------------------------------------ App
export default function App() {
  const [roster, setRoster] = useState<string[]>([]);
  const [alias, setAlias] = useState<Record<string, string>>({});
  const [sesgosDisp, setSesgosDisp] = useState<string[]>([]);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [corriendo, setCorriendo] = useState(false);
  const [transcripts, setTranscripts] = useState<string[]>([]);
  const [ramas, setRamas] = useState<Rama[]>([]);
  const [aviso, setAviso] = useState("");

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

  const finRef = useRef<HTMLDivElement>(null);

  // Auto-debate: el mismo agente dos veces (mismo nombre base). Solo entonces
  // tiene sentido asignar sesgos opuestos para romper el eco.
  const esAutodebate = useMemo(
    () => nombreAgente(parA, alias) === nombreAgente(parB, alias),
    [parA, parB, alias],
  );

  const cargarTranscripts = () =>
    fetch("/api/transcripts").then((r) => r.json())
      .then((d) => setTranscripts(d.transcripts));

  const cargarRamas = () =>
    fetch("/api/ramas").then((r) => r.json()).then((d) => setRamas(d.ramas ?? []));

  const borrarRama = async (nombre: string) => {
    if (!window.confirm(`¿Borrar la rama ${nombre}? Se pierde lo que no hayas fusionado.`))
      return;
    const r = await fetch("/api/ramas/borrar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rama: nombre }),
    });
    if (!r.ok) setAviso((await r.json()).detail ?? "No se pudo borrar la rama.");
    cargarRamas();
  };

  useEffect(() => {
    fetch("/api/roster").then((r) => r.json()).then((d) => {
      setRoster(d.agentes); setAlias(d.alias); setSesgosDisp(d.sesgos ?? []);
    });
    cargarTranscripts();
    cargarRamas();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.tipo === "historial") {
        setMsgs(m.eventos); setCorriendo(m.corriendo);
      } else {
        setMsgs((prev) => [...prev, m]);
        if (m.tipo === "fin" || m.tipo === "error") {
          setCorriendo(false); cargarTranscripts();
        }
      }
    };
    ws.onclose = () => setAviso("Conexión perdida con el Hub — recarga la página.");
    return () => ws.close();
  }, []);

  // Reducción de mensajes → items del feed + turno pendiente + paneles.
  const { items, pendiente, config, fin, error, intervencion, ejecucion, cierre } = useMemo(() => {
    const items: Item[] = [];
    let pendiente: { agente: string; fase: string } | null = null;
    let config: Extract<Msg, { tipo: "inicio" }>["config"] | null = null;
    let fin: Extract<Msg, { tipo: "fin" }> | null = null;
    let error: Extract<Msg, { tipo: "error" }> | null = null;
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
      else if (m.tipo === "fin") { fin = m; pendiente = null; }
      else if (m.tipo === "error") { error = m; pendiente = null; }
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
        else if (evento.endsWith("_inicio"))
          pendiente = { agente, fase: evento.replace(/_inicio$/, "") };
        else if (evento.endsWith("_fin") && texto != null) {
          pendiente = null;
          items.push({ clase: "turno", agente, fase: evento.replace(/_fin$/, ""), texto });
        }
      }
    }
    return { items, pendiente, config, fin, error, intervencion, ejecucion, cierre };
  }, [msgs]);

  useEffect(() => {
    if (ejecucion && ejecucion.estado !== "corriendo") setEjecutando(false);
    // Sugerencia editable de mensaje de commit: el tema del debate.
    if (ejecucion?.estado === "fin" && !commitMsg && config?.tema)
      setCommitMsg(config.tema);
  }, [ejecucion, config, commitMsg]);

  // El historial de ramas cambia al ejecutar (aparece una) o al commitear/
  // descartar (cambia su estado o se borra): refrescarlo entonces.
  useEffect(() => { cargarRamas(); }, [cierre, ejecucion?.estado]);

  useEffect(() => {
    finRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items.length, pendiente, fin, intervencion, ejecucion]);

  const ladoDe = (agente: string) =>
    config && nombreAgente(config.agentes[1], alias) === agente ? "der" : "izq";

  const lanzar = async () => {
    setAviso("");
    const r = await fetch("/api/debates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tema, files, rounds, profundo, interactivo, agentes: [parA, parB],
        sesgos: esAutodebate ? [sesgoA, sesgoB] : [],
      }),
    });
    if (r.ok) setCorriendo(true);
    else setAviso((await r.json()).detail ?? "No se pudo lanzar el debate.");
  };

  const enviarNota = async (texto: string) => {
    await fetch("/api/intervencion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nota: texto }),
    });
    setNota("");
  };

  const ejecutarPlan = async (transcript: string) => {
    setEjecutando(true);
    const r = await fetch("/api/ejecutar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript }),
    });
    if (!r.ok) {
      setEjecutando(false);
      setAviso((await r.json()).detail ?? "No se pudo lanzar la ejecución.");
    }
  };

  const commitear = async () => {
    const r = await fetch("/api/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mensaje: commitMsg }),
    });
    if (!r.ok) setAviso((await r.json()).detail ?? "No se pudo commitear.");
    else setCommitMsg("");
  };

  const descartar = async () => {
    const r = await fetch("/api/descartar", { method: "POST" });
    if (!r.ok) setAviso((await r.json()).detail ?? "No se pudo descartar.");
  };

  const reanudar = async (parcial: string) => {
    setAviso("");
    const r = await fetch("/api/debates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agentes: [parA, parB], rounds, resume: parcial,
        sesgos: esAutodebate ? [sesgoA, sesgoB] : [],
      }),
    });
    if (r.ok) setCorriendo(true);
    else setAviso((await r.json()).detail ?? "No se pudo reanudar el debate.");
  };

  return (
    <div className="app-container">
      <aside className="sidebar">
        <h1 className="text-gradient marca"><Swords size={20} /> Devvating Hub</h1>
        <p className="lema">Dos agentes debaten sobre tu código. Tú arbitras.</p>

        <label>Tema del debate
          <textarea value={tema} onChange={(e) => setTema(e.target.value)}
            rows={4} placeholder="¿Conviene X o Y?" disabled={corriendo} />
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

        <h2 className="titulo-lista"><FileText size={14} /> Debates anteriores</h2>
        <ul className="lista-transcripts">
          {transcripts.map((t) => (
            <li key={t}>
              <a href={`/api/transcripts/${encodeURIComponent(t)}/html`} target="_blank" rel="noreferrer"
                title={t}>{t.replace(/\.json$/, "").slice(16)}</a>
            </li>
          ))}
          {transcripts.length === 0 && <li className="vacio">aún ninguno</li>}
        </ul>

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
          {pendiente && <Pendiente agente={pendiente.agente} fase={pendiente.fase} />}

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

          {fin && (
            <div className="panel-sintesis glass-panel">
              <h3><Scale size={16} /> Síntesis (por {fin.sintetizador}) ·{" "}
                {fin.convergio ? `convergieron en la ronda ${fin.ronda_convergencia}` : "sin convergencia"}</h3>
              <div className="cuerpo" dangerouslySetInnerHTML={md(fin.sintesis)} />
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
                <button className="ejecutar" disabled={ejecutando || corriendo}
                  onClick={() => ejecutarPlan(fin.transcript)}
                  title="Aplica el plan en una rama nueva; nada se commitea.">
                  <GitBranch size={14} /> {ejecutando ? "Ejecutando…" : "Ejecutar plan"}
                </button>
              </footer>
            </div>
          )}

          {ejecucion && (
            <div className={`panel-ejecucion glass-panel animate-fade-in ${ejecucion.estado}`}>
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
                  <h3><GitBranch size={16} /> Cambios en staging · rama <code>{ejecucion.rama}</code></h3>
                  {ejecucion.returncode !== 0 && (
                    <p className="aviso-feed">el backend salió con código {ejecucion.returncode}</p>
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
