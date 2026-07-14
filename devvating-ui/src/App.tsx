import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import {
  FileText, Play, RefreshCw, Scale, Swords, TriangleAlert,
} from "lucide-react";
import "./App.css";

// ---------------------------------------------------------------- tipos
type Usage = {
  input_tokens: number; output_tokens: number;
  cache_read_tokens: number; cache_creation_tokens: number;
  cost_usd: number | null;
};
type Msg =
  | { tipo: "inicio"; config: { tema: string; agentes: string[]; rounds: number; profundo: boolean } }
  | { tipo: "evento"; evento: string; agente: string; texto: string | null }
  | { tipo: "fin"; sintesis: string; sintetizador: string; convergio: boolean; ronda_convergencia: number | null; usage: Record<string, Usage>; transcript: string }
  | { tipo: "error"; mensaje: string; resets_at?: string | null; parcial?: string | null };

type Item =
  | { clase: "separador"; texto: string }
  | { clase: "turno"; agente: string; fase: string; texto: string }
  | { clase: "aviso"; texto: string };

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
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [corriendo, setCorriendo] = useState(false);
  const [transcripts, setTranscripts] = useState<string[]>([]);
  const [aviso, setAviso] = useState("");

  const [tema, setTema] = useState("");
  const [files, setFiles] = useState("");
  const [rounds, setRounds] = useState(2);
  const [profundo, setProfundo] = useState(false);
  const [parA, setParA] = useState("claude-cli");
  const [parB, setParB] = useState("gemini-api");

  const finRef = useRef<HTMLDivElement>(null);

  const cargarTranscripts = () =>
    fetch("/api/transcripts").then((r) => r.json())
      .then((d) => setTranscripts(d.transcripts));

  useEffect(() => {
    fetch("/api/roster").then((r) => r.json()).then((d) => {
      setRoster(d.agentes); setAlias(d.alias);
    });
    cargarTranscripts();
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

  // Reducción de mensajes → items del feed + turno pendiente.
  const { items, pendiente, config, fin, error } = useMemo(() => {
    const items: Item[] = [];
    let pendiente: { agente: string; fase: string } | null = null;
    let config: Extract<Msg, { tipo: "inicio" }>["config"] | null = null;
    let fin: Extract<Msg, { tipo: "fin" }> | null = null;
    let error: Extract<Msg, { tipo: "error" }> | null = null;
    for (const m of msgs) {
      if (m.tipo === "inicio") config = m.config;
      else if (m.tipo === "fin") { fin = m; pendiente = null; }
      else if (m.tipo === "error") { error = m; pendiente = null; }
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
    return { items, pendiente, config, fin, error };
  }, [msgs]);

  useEffect(() => {
    finRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items.length, pendiente, fin]);

  const ladoDe = (agente: string) =>
    config && nombreAgente(config.agentes[1], alias) === agente ? "der" : "izq";

  const lanzar = async () => {
    setAviso("");
    const r = await fetch("/api/debates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tema, files, rounds, profundo, agentes: [parA, parB] }),
    });
    if (r.ok) setCorriendo(true);
    else setAviso((await r.json()).detail ?? "No se pudo lanzar el debate.");
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
      </aside>

      <main className="main-content">
        <header className="barra-estado glass-panel">
          <span className={`status-dot ${corriendo ? "progress" : "active"}`} />
          <span>{corriendo ? "debate en curso" : "en reposo"}</span>
          {config && <span className="tema-actual" title={config.tema}>
            {nombreAgente(config.agentes[0], alias)} <Swords size={13} /> {nombreAgente(config.agentes[1], alias)}
            {" · "}≤{config.rounds} rondas{config.profundo ? " · profundo" : ""}
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

          {error && (
            <div className="panel-error glass-panel">
              <h3><TriangleAlert size={16} /> Debate interrumpido</h3>
              <p>{error.mensaje}</p>
              {error.resets_at && <p>La cuota se reinicia a las <b>{error.resets_at}</b>.</p>}
              {error.parcial && <p>Turnos pagados a salvo en <code>{error.parcial}</code> —
                reanudable con <code>devvating debate --resume</code>.</p>}
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
              </footer>
            </div>
          )}
          <div ref={finRef} />
        </section>
      </main>
    </div>
  );
}
